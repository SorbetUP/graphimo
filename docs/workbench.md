# Standalone Graphimo Workbench

The Workbench is a local development renderer for the host-independent
`GraphimoApplication`. It serves a browser canvas on a loopback address and
forwards JSON actions to the same application seam used by future host
adapters. The renderer owns only selection, camera movement, pointer handling
and DOM presentation. Graph state remains in addon sidecars; layout is stored
in the project `.graphimo.json` presentation file.

Install the PyTorch addon, then launch it against a local marimo project file:

```bash
python -m pip install -e '.[dev]'
python -m pip install -e addons/pytorch
python -m graphimo.workbench --project /path/to/notebook.py
```

By default the server binds to `127.0.0.1:8765` and opens the browser. Use
`--port 0` to ask the operating system to select a free port, or `--no-browser`
to print the address without opening a tab. The server refuses non-loopback
bind addresses.

## Current surface

- Create and open persisted PyTorch MetaBlocks.
- Choose a Multihead MLP, Self Attention Group, Llama Decoder Layer, full-size
  Llama Causal LM editing fixture, or runnable reduced Llama fixture when
  creating a MetaBlock.
- Browse the addon-provided contextual palette and insert registered element
  kinds through `metablock.apply`.
- Select and move canvas cards, pan and zoom, and persist positions by model
  and semantic scope.
- Connect output/input ports, edit supported parameters, rename/delete cards,
  navigate methods and Group definitions, and change Group repetition counts.
- Inspect ports and inferred shapes, validation diagnostics, and readable
  generated Python.
- Request the generated marimo materialization source.
- Inspect process-local PyTorch runtime instances, select a repeated decoder
  layer through its runtime binding, subscribe to bounded activation/gradient
  telemetry, and view scalar metric histories.
- Inspect a Graphimo checkpoint artifact, view its provenance, and preview
  exact-name/exact-shape migration when the graph revision differs.
- Run a selected-site TorchLens deep inspection using explicit JSON sample
  inputs, optional scalar-output gradients, zero-ablation/scale experiments,
  and an explicit full-trace ArtifactStore save action.

The standalone surface does not attach to a running marimo session, so its
materialization action presents source for review and explicitly marks that it
was not inserted into a live notebook cell. The exact supported marimo cell
update mechanism remains subject to the `HUMAN DECISION REQUIRED` contract in
the Core implementation specification.

Runtime discovery and telemetry are process-local. A model instantiated in a
separate marimo process is not visible to this standalone Workbench; the Core
specification does not yet define that cross-process transport. The UI reports
when no runtime is registered in the Workbench process instead of presenting
stale or inferred runtime data.

TorchLens inspection is also process-local and uses an explicit input fixture;
it does not claim to inspect the current or next training batch. Saving an
inspection runs a fresh pass, and intervention results are labeled as
experimental copies of the selected runtime.

## HTTP/action boundary

`GET /api/project` returns project metadata, persisted MetaBlock summaries and
installed addon entry-point metadata. `POST /api/action` accepts
`{"action":"...","args":{...}}` and forwards the request to
`GraphimoApplication.execute`. The body is limited to 256 KiB, responses use a
bounded JSON envelope, and the server serves only files from the packaged
Workbench assets directory. There is no renderer-side addon state mutation.

The Python controller and HTTP contract are covered by pytest independently
of the browser. Browser interaction logic does not run Python graph rules; all
semantic edits go through the application and the PyTorch dialect.
