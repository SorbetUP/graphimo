# Zeron canvas undo and redo

The Zeron canvas keeps a bounded history for actions performed in the active
scope. Ordinary notebook actions live in the plugin session; MetaBlock
semantic history is owned by the Graphimo application session. Use the **Undo** and **Redo** buttons or
`Cmd/Ctrl+Z`, `Cmd/Ctrl+Shift+Z`, and `Ctrl+Y` while the canvas has keyboard
focus. Delete and Backspace remove the selected ordinary marimo cells.

Each entry is tied to the notebook path, live marimo session, and Graphimo
scope where it was created. Switching notebooks or entering another scope does
not apply a history entry to the wrong graph. Each stack is limited to 50
actions; addon state snapshots are additionally bounded to 16 MiB per
application session. Histories are not persisted across a Graphimo service
restart. Ordinary notebook history is not persisted across plugin reloads.

## Covered actions

- Moving one or more cells records the prior and resulting layout. Undo changes
  saved canvas positions only; it does not alter Python.
- Connecting two ordinary cells records the exact source before and after the
  connection. Undo/redo uses a marimo edit guarded by an exact expected-source
  check. If the cell disappeared or its code changed externally, Graphimo
  refuses the history operation and leaves the entry available for recovery.
- Adding a component or Control Block, deleting selected ordinary cells, and
  replacing selected cells with a Group Block are recorded as source-and-layout
  transactions. Restored marimo cells receive new cell IDs; marimo recomputes
  dependencies from their Python source.
- A Control Block and its reactive value cell form one insertion entry when
  both cells were created successfully. If only the control cell was inserted,
  that partial result still has its own undo entry.
- PyTorch MetaBlock edits use `metablock.undo` and `metablock.redo` through the
  shared application layer. Core stores opaque addon-state snapshots; it does
  not interpret the PyTorch graph. The UI and agents can use the same actions.
  An external sidecar change marks the history stale and undo/redo refuses to
  overwrite it.

Cell restoration uses one marimo code-mode request for the full transaction.
Graphimo validates the list size, source size, and Python syntax before adding
any cells. If marimo rejects a later cell, Graphimo attempts to delete cells
created earlier in that same request and returns the rollback diagnostics.
History source edits similarly use an expected-source guard and do not
overwrite a newer external edit.

The reusable Group definition remains in the project's Group library when the
canvas grouping action is undone; undo restores the original ordinary notebook
cells and removes the Group call from that scope. This keeps an already-created
reusable definition available for other instances.

## Current boundaries

Ordinary notebook source/layout history covers actions issued by the Zeron UI.
Agent operations or edits made directly in marimo are outside that host history.
MetaBlock operations made through `GraphimoApplication` do share the addon-state
history. Graphimo refuses stale source or sidecar history rather than guessing
how to merge an external edit.
