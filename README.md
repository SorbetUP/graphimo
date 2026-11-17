# Graphimo

Graphimo is intended to be a Zeron canvas view over a live marimo notebook. The
notebook and marimo runtime own cells, dependencies, execution, outputs, and
cache. Graphimo's domain adapter is deliberately small: it projects a fresh
marimo snapshot, inserts ordinary Python, delegates Run, and stores only node
presentation state in `.graphimo.json`.

## Current implementation

`adapter.graphimo_adapter` is the SDK-independent operation seam. The Zeron
binding supplies a `context` with a `marimo` port implementing:

- `list_active_notebooks()`
- `inspect(session_id)` returning the current notebook, cells, and edges
- `add_cell(session_id, source)`
- `replace_cell_source(session_id, cell_id, source)`
- `run_cell(session_id, cell_id)`
- `delete_cell(session_id, cell_id)`

The optional `context.open_code(cell_id)` callback navigates through Zeron's
existing code-opening capability. Call `dispatch({"op": ...}, context)` for
`inspect`, `list_components`, `add_component`, `connect`, `run`, `delete`, and
`open_code`. Every operation returns JSON-shaped data with `ok`; errors carry
`error.code`, `error.message`, and `error.cell_id`.

Layout helpers implement the `.graphimo.json` v1 shape:

```json
{"version":1,"notebooks":{"notebook.py":{"cells":{"cell-id":{"x":80,"y":80}}}}}
```

Only visual keys are persisted. Graph, source, refs/defs, output, and execution
state are never written there. Missing cells receive a simple grid position.

## Marimo support

The examples target marimo 0.25.2 (`pip install -e '.[notebook]'`). The
`[tool.marimo.runtime] cache_cells = true` setting uses marimo's own cell
cache. Open an example normally with `marimo edit examples/simple_pipeline.py`.

Marimo's documented MCP server provides live notebook discovery, cell maps,
runtime details, outputs, and dependency graphs. Its documented tools do not
edit or execute cells; marimo directs notebook manipulation to marimo-pair.
Graphimo therefore accepts the live-session port as an injected boundary and
does not claim a second executor or private marimo integration. The concrete
Zeron-to-marimo transport and canvas registration still need an SDK binding
that can reach the same live session for both inspection and mutation.

## Development

```bash
pip install -e '.[dev]'
rtk pytest -q
rtk ruff check adapter
```

The contract tests use a fake live-session port; they verify translation
behavior without starting a notebook server. They do not constitute an
end-to-end Zeron/marimo integration test.
