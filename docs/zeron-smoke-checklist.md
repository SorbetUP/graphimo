# Zeron renderer smoke checklist

Automated tests cover the Luau projections and the Python service; they cannot
prove the real renderer. Run this checklist on a live Zeron host after any
change to `plugin/*.luau` or to `notebook.inspect`:

## Canvas

- [ ] The Graphimo page shows the robot glyph (`bot`) in the
      sidebar and rail; the plugin tile in Settings → Plugins shows
      `graphimo.svg` (the robot mark).
- [ ] The page header shows the Graphimo mark on an accent-tinted tile, the
      notebook path, and icon buttons (refresh, fit, zoom, grid).
- [ ] Cards show a role stripe + glyph, a friendly title (node name, variable
      name, metablock label — never a raw cell id), and a status pill
      (idle=success, running=info, stale=warning, error/blocked=danger,
      disabled=neutral).
- [ ] Input ports read as dots at the card's left edge (filled accent when
      bound, hollow when unbound/default); output ports as rings at the right
      edge. Edge arrowheads land exactly on the target dot.
- [ ] With no notebook open, the canvas shows the empty state with
      Open/New actions.
- [ ] Nodes render with formal input/output ports (not refs/defs positions) for
      Graphimo instances; ordinary Python cells keep refs/defs ports.
- [ ] A 3-output node shows three distinct output dots; each can be dragged to
      a different target and lands on the correct `output_index`.
- [ ] Port dots, edge endpoints and hitboxes coincide (single geometry in
      `Canvas.port` / `Canvas.draw_ports`).
- [ ] Library/technical cells are hidden by default and appear with
      "Technical +".
- [ ] Zoom, pan and multi-select still work; edges redraw without O(E×V) lag on
      a ~50-node graph.

## Inspector

- [ ] Right-click a node → "Inspect node…" opens the inspector with inputs
      (binding kind: variable / literal / default / unbound), outputs
      (available vs stale) and Run / Disable / Enable.
- [ ] Editing a literal on an unbound input works; disconnect works; the edge
      disappears from the canvas immediately.
- [ ] Disable keeps stale edges visible but marked; Enable restores them on
      the same ports.

## Mutations

- [ ] Connect from an output port validates the target input name; a cycle is
      refused with a diagnostic and the canvas is unchanged.
- [ ] Undo/redo restores cells and edges after add component / connect /
      group creation.
- [ ] A failed action (e.g. `component_unavailable`) shows the error code and
      leaves the previous graph intact.

## MetaBlocks / PyTorch

- [ ] Creating a `pytorch.model` MetaBlock shows the projection and runtime
      selector; `checkpoint.restore` from the picker restores weights in the
      live kernel (verify via TorchLens inspect in the inspector).
- [ ] Events (telemetry) stream into the panel with bounded rates.

## Artifacts

- [ ] The artifact panel lists recent artifacts (kind, size, metadata) via
      `artifact.list`, supports pin/unpin, and snapshots via capture output.

## Blocks library & keyboard

- [ ] The left panel shows a "Blocks" section: pipeline presets
      ("Insert · …") and block files ("Add block · …") from
      `plugin/presets.luau`.
- [ ] "Insert · LFW face recognition (sklearn)" writes
      `components/ml/lfw_sklearn.py`, inserts the 4 node instances, wires all
      edges with the correct `output_index`, runs them in order, and lays the
      cards out left-to-right.
- [ ] Keyboard on the canvas: ⌘/Ctrl+Z / ⇧⌘Z undo-redo, ⌘/Ctrl+C copy,
      ⌘/Ctrl+V paste (offset +40px), ⌘/Ctrl+A select all, ⌘/Ctrl+D duplicate,
      Delete/Backspace, Esc clears selection, R run, O open code, arrows nudge
      (⇧ = 1px, else 10px), +/− zoom, ⌘0 fit.
- [ ] Card titles, subtitles and previews are selectable text
      (⌘A / ⌘C inside a card).
