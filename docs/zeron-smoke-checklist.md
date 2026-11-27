# Zeron renderer smoke checklist

Automated tests cover the Luau projections and the Python service; they cannot
prove the real renderer. Run this checklist on a live Zeron host after any
change to `plugin/*.luau` or to `notebook.inspect`:

## Layout

- [ ] Top bar is light: Graphimo mark + name + notebook basename on the left,
      kernel status dot ("Kernel connected" / "No notebook"), then at most
      Refresh / Open notebook / New notebook on the right.
- [ ] Left rail (300 px) = Library: search field on top, category tabs
      (Nodes / Controls / MetaBlocks / Presets / Artifacts), and only the
      active category's content below. Presets keeps "Insert · …" pipelines and
      "Add block · …" block files; Artifacts lists `artifact.list` / `run.list`.
- [ ] Center = the canvas well (darker surface); Run / Open code / Delete /
      Undo / Redo live in a floating pill at the canvas's top-left; zoom %,
      fit and grid toggle in a pill at the bottom-left; breadcrumbs appear as a
      centered pill when inside a Group Block or MetaBlock.
- [ ] Right rail (320 px) appears on selection / MetaBlock / Logs tab, with
      Inspect | Logs tabs. Inspect shows the selected node (inputs/outputs,
      Run / Open code / Delete / Disable) or the MetaBlock editor; Logs shows
      events, problems, the technical-cells toggle and export.
- [ ] Library rows have a grip button ("Place on canvas"): arm it, then click
      the canvas to drop the block at that exact position; Esc cancels.

## Canvas

- [ ] The Graphimo page shows the robot glyph (`bot`) in the
      sidebar and rail; the plugin tile in Settings → Plugins shows
      `graphimo.svg` (the robot mark).
- [ ] With no notebook open, the canvas shows the workspace empty state:
      "Start a Graphimo workspace", Open/New notebook buttons, and template
      cards from `plugin/presets.luau`.
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
      "Show technical cells" in the Logs tab.
- [ ] Zoom, pan and multi-select still work; edges redraw without O(E×V) lag on
      a ~50-node graph.

## Inspector

- [ ] Clicking a node opens the right rail on the Inspect tab; right-click →
      "Inspect node…" also lands there. It shows inputs (binding kind:
      variable / literal / default / unbound), outputs (available vs stale)
      and Run / Open code / Delete / Disable / Enable.
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

- [ ] The Library's Presets category shows pipeline presets
      ("Insert · …") and block files ("Add block · …") from
      `plugin/presets.luau`; the empty-canvas template cards insert the same
      block files once a notebook is open.
- [ ] "Insert · LFW face recognition (sklearn)" writes
      `components/ml/lfw_sklearn.py`, inserts the 4 node instances, wires all
      edges with the correct `output_index`, runs them in order, and lays the
      cards out left-to-right.
- [ ] Keyboard on the canvas: ⌘/Ctrl+Z / ⇧⌘Z / ⌘Y undo-redo, ⌘/Ctrl+C copy,
      ⌘/Ctrl+V paste (offset +40px), ⌘/Ctrl+A select all, ⌘/Ctrl+D duplicate,
      Delete/Backspace, Esc clears selection, R run, O open code, arrows nudge
      (⇧ = 1px, else 10px), +/− zoom, ⌘/Ctrl+0 fit (Fit button must not crash).
- [ ] Pointer: drag empty canvas or middle-button drag pans; mouse wheel and
      trackpad pinch (ctrl) zoom around the cursor; shift+wheel and horizontal
      trackpad scroll pan sideways; drag a node moves the selection; drag an
      output port onto an input port wires `notebook.connect` (with the right
      0-based `output_index` for multi-output nodes).
- [ ] Card titles, subtitles and previews are selectable text
      (⌘A / ⌘C inside a card).
- [ ] The agent tool `open_page` (permission `navigate`) opens this page in the
      Zeron window; without the permission it returns `navigate_denied` instead
      of crashing.
