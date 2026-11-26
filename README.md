# Graphimo

Graphimo is a small Zeron v3 plugin that projects a live marimo notebook onto a
canvas. marimo owns the notebook, dependency graph, execution, cell outputs,
and cache. Graphimo reads that state, offers conservative source edits, and
stores only cell presentation state in `.graphimo.json`.

## What works

- Discover and select active marimo notebook sessions.
- Render every marimo cell with its refs/defs and edges derived from marimo's
  current dependency graph, including cells not created by Graphimo.
- Pan, zoom, fit, drag cards, and persist positions keyed by cell ID.
- Run or delete a cell through the live marimo session, show its current
  status/output summary, and open the marimo editor. Output summaries preserve
  marimo's MIME type; HTML is reduced to readable text instead of injected into
  Zeron's UI.
- Open an existing `.py` notebook or create a new ordinary marimo notebook
  from the Graphimo page. Graphimo reuses an already-running local marimo
  session for that file or starts `marimo edit` on marimo's default free port;
  the user never enters a URL, port, or token.
- Inspect saved events and problems in **Logs**. Problems are appended to
  `~/.local/state/graphimo/problems.log`; managed marimo stdout/stderr is kept
  under `~/.local/state/graphimo/` and can be read or exported from the panel.
  The on-screen log state survives page reloads.
- Discover public callables in `.py` files and packages under the notebook's
  `components/` directory, then insert a readable ordinary Python cell.
- Browse the discovered library in the collapsible **Components** panel, search
  component IDs/categories/callable details, and expand a component to add one
  of its callables. **Terminal** and its saved logs can be closed independently.
- Drag an output to an input for one deliberately narrow Python edit. Ambiguous
  edits are refused and the source remains available in marimo's editor.
- Detect source hash changes for inserted components and update their marker
  in the marimo cell, allowing marimo to mark and recompute it.

## Install and connect

Install Graphimo through Zeron's plugin manager and allow its `files` and
`commands` permissions. Choose **Open notebook** or **New notebook**. Graphimo
uses Zeron's native file dialog, starts or reuses a local marimo editor, then
opens it once so marimo can establish its live session. Marimo chooses its own
free port. Graphimo uses an existing environment with the pinned marimo version
when available; otherwise it creates a private environment under
`~/.local/share/graphimo/` and installs `marimo==0.25.2`. The global Python
installation is not modified. This first setup needs access to the package
index. Graphimo invokes `curl` for marimo's local session API and `python3` for
the minimal detached-process bridge and sidecar layout; it does not own cell
execution or dependency scheduling. Zeron and marimo must see the same local
notebook path for component discovery and layout persistence.

If startup fails, open **Logs** and choose **Read marimo log**. The full
environment setup and marimo output are stored under
`~/.local/state/graphimo/`. See [the marimo runtime guide](docs/marimo-runtime.md)
for interpreter selection and recovery details.

Component discovery uses the convention:

```text
notebook.py
components/
  data-processing/
    normalize.py
    image/
      __init__.py
      open_jpg.py
```

Public functions are discovered with `inspect.signature`; `__all__` is
respected when present. No Graphimo base class, decorator, manifest, or
annotation is required. A selected function becomes a normal marimo cell, for
example `normalized = ...normalize(image=image)`, which can be edited freely.
The Components panel groups entries by their `category` metadata (or **Components**
for root-level files) and searches callable names, signatures, and descriptions.
Parameter metadata is inspected safely for discovery; dedicated parameter editing
controls are not implemented yet, so adjust values in the generated Python cell.
Generated component cards can show a read-only Base Block summary of safe,
signature-derived inputs and outputs; these labels are not interactive ports.
Bridge inspection can inventory live marimo `slider` and `dropdown` widgets on the
pinned marimo 0.25.2 runtime, but Graphimo does not render or edit those controls;
use marimo for widget interaction.

## Persistence and boundaries

`.graphimo.json` contains only visual metadata (`x`, `y`, collapse/size hints,
and optional group), scoped by notebook path and stable marimo cell ID. It does
not contain code, edges, values, execution state, or cache. Removing it loses
custom placement only; marimo still opens the notebook normally.

The Zeron-specific canvas and host binding live in `plugin/`. Because Zeron
plugins run in Luau, production requests go through `plugin/marimo_bridge.luau`;
the Python module in `adapter/` is an SDK-independent contract harness used by
the pytest suite, not a runtime dependency of the plugin. Both expose the same
operations and JSON result envelope: successful results have `ok: true`, and
errors have `ok: false` with `error.code`, `error.message`, and optional
`error.cell_id`. Contract tests exercise the Python seam; they do not execute
the Luau bridge, so the host binding still needs an end-to-end Zeron check.

`plugin/marimo_bridge.luau` is the single marimo transport and private API
boundary. It uses marimo's local HTTP session endpoints and the code-mode
context API to read current cell/graph data and ask the active session to edit
or run cells. Graphimo does not implement a graph store, executor, scheduler,
or cache. The private bridge is pinned to marimo 0.25.2 in `pyproject.toml`.

The host-independent MetaBlock entry-point and application contracts live in
[`graphimo/application.py`](graphimo/application.py) and
[`docs/metablock-host-api.md`](docs/metablock-host-api.md). Core preserves
addon-owned sidecar state when an addon is missing. The separate PyTorch
distribution registers through this API. Its P1-P9 implementation includes
semantic graph editing, model compilation, live runtime inspection, checkpoints,
TorchLens integration, Llama templates, and package/unavailable-addon lifecycle
coverage. The host-independent application API is the current integration seam;
the later persistent service and Zeron migration remain separate milestones.
See [`docs/pytorch-addon.md`](docs/pytorch-addon.md) for the implemented surface
and its process-boundary limitations.

The standalone Workbench exercises the shared application layer without
Zeron. After installing the PyTorch addon, run
`python -m graphimo.workbench --project /path/to/notebook.py`. It provides a
local model canvas, contextual PyTorch palette, inspectors, shape diagnostics,
generated source, and scoped layout persistence. See the
[Workbench guide](docs/workbench.md) for its local HTTP boundary and the
current marimo materialization limitation.

## Development

For the Zeron plugin API reference and examples from Graphimo and Orchestrator Builder, see [the plugin development guide](docs/zeron-plugin-api.md).
For Codex plugin work in this repository, use the reusable [Zeron Plugin Builder skill](.agents/skills/zeron-plugin-builder/SKILL.md).

The host-independent [`@node` and generated instance API](docs/node-instance-contract.md)
keeps marimo as the reactive engine. Explicit file/directory persistence is
available through [`graphimo.artifacts`](docs/artifacts.md); arbitrary Python
results are not saved automatically.
Reusable marimo sub-workflows use the reference-based
[`Group Block API`](docs/groups.md), backed by `App.clone()` and
`App.run(defs=...)`.

Install the pinned notebook runtime and development tools, then run the local
verification gate:

```bash
python3 -m pip install -e '.[notebook,dev]'
python3 -m pip install -e addons/pytorch
./scripts/check --fast
```

Use `./scripts/check --full` before a pre-push after installing the addon and
its pinned dependencies. `./scripts/install-hooks` optionally installs a local
pre-push hook that runs the full gate. No hosted GitHub Actions workflow is
required by the current project validation policy.

The pytest suite exercises `adapter.graphimo_adapter` through a fake marimo
port. It does not replace an end-to-end run inside Zeron with a live marimo
server. Example notebooks are ordinary marimo `.py` files and use marimo's
`cache_cells = true` setting.

An isolated architecture spike now lives in `core/graphimo_core/`. Its first
slice validates the artifact store, workspace ledger, persistent Python worker,
PyTorch model, and checkpoint artifacts. A second slice sends dataset branches
through ordinary Python nodes, supports cooperative live model probes during a
run, resumes optimizer/RNG state after worker restart, and records immutable
node revisions. The core is not wired into the Zeron canvas or marimo bridge.
See the [first](docs/core-vertical-slice.md) and
[second](docs/core-second-vertical-slice.md) validation reports.
Run the local prototype with:

```bash
PYTHONPATH=core python -m graphimo_pytorch.vertical_demo /path/to/workspace
```

Install the separate `addons/pytorch` package first for the PyTorch vertical
slice. PyTorch and TorchLens dependencies belong to that addon; Graphimo Core
does not import them.

For a live marimo integration check of the production Luau bridge's generated
requests (inspect, component discovery/insertion, connect, and Run dispatch), install
the pinned marimo extra and the Luau CLI, then run:

```bash
GRAPHIMO_MARIMO_PYTHON="$(command -v python3)" rtk proxy pytest -q tests/test_luau_marimo_live.py
```

This test launches an isolated marimo server and executes Python generated by
`plugin/marimo_bridge.luau` against it. Its small Luau driver replaces only
Zeron's command transport and promise/JSON host, so the test still does not
exercise Zeron's rendered page or permission prompt. That final host check must
be done in Zeron with a disposable notebook.
