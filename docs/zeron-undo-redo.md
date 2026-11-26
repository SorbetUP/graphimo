# Zeron canvas undo and redo

The Zeron canvas keeps a bounded, in-memory history for actions performed in
the active notebook scope. Use the **Undo** and **Redo** buttons or
`Cmd/Ctrl+Z`, `Cmd/Ctrl+Shift+Z`, and `Ctrl+Y` while the canvas has keyboard
focus. Delete and Backspace remove the selected ordinary marimo cells.

Each entry is tied to the notebook path, live marimo session, and Graphimo
scope where it was created. Switching notebooks or entering another scope does
not apply a history entry to the wrong graph. The current history is limited
to 50 actions and is not persisted across plugin reloads.

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

This history covers canvas actions issued by the Zeron UI. Agent operations
that mutate a notebook directly, edits made in marimo outside Graphimo, and
PyTorch MetaBlock structural changes are not yet added to this canvas history.
Those changes remain governed by their own application/host transaction
contracts. Graphimo refuses stale source-history edits rather than guessing how
to merge them.
