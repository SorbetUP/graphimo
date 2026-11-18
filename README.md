# Graphimo

Graphimo is a native Zeron plugin that presents a live **marimo notebook as a
block canvas**.

The architectural rule is intentionally strict:

> **marimo owns Python execution, dependencies, reactivity, state, cache and
> notebook sessions. Graphimo owns the visual projection and editing UX.**

Graphimo does not implement a workflow engine, scheduler, ML runtime, artifact
store or second DAG.

## Block model

Every live marimo cell is a Graphimo block. Existing notebooks do not need to
be converted.

Graphimo currently projects these visual families:

- **Base Block** — ordinary Python/marimo code. Its real refs/defs are ports.
  Function-backed components additionally expose signature defaults, annotations
  and conservative return-expression metadata.
- **Control Block** — real marimo `mo.ui.slider` and `mo.ui.dropdown`
  elements rendered with native Zeron controls. Changes are sent back to the
  original marimo UIElement, so marimo remains responsible for downstream
  reactivity.
- **Display Block** — a connected display-only block. Images and Markdown are
  rendered directly; supported HTML/media is shown in a Zeron webview using
  marimo's rendered output.
- **Annotation** — Markdown/documentation.
- **Group Block** — a reference to a reusable group of real marimo blocks.
  Instances point to one saved definition; they are not copies.
- **Meta Block** — a domain-specific definition made of internal **Elements**
  that are not marimo cells. The first implementation is a PyTorch Model Meta
  Block that compiles its Elements into one `torch.nn.Module`.

## Ports

For a component such as:

```python
def obtenir_infos(
    prix: float,
    quantite: int,
    stock: int,
) -> tuple[float, int, int]:
    flower = 3 + stock
    return prix + 0.2, quantite + stock, flower
```

the block exposes three input ports and three output ports.

Inputs use the function signature:

- `prix (float)`
- `quantite (int)`
- `stock (int)`

A default such as `prix: float = 19.2` is shown compactly as
`prix (19.2)`; its full hover text is `prix (19.2 | float)`.

Outputs use the declared type and the actual return expression:

- `float (prix + 0.2)`
- `int (quantite + stock)`
- `int (flower)`

After a successful run, JSON-safe runtime values are added without replacing
the expression, for example `float (19.4 | prix + 0.2)`.

One output may feed any number of downstream inputs.

For arbitrary Python that cannot be inferred cleanly, Graphimo supports an
optional manual port contract. Python remains authoritative; the declaration is
only the visual interface.

## Canvas and editing

The Zeron canvas supports:

- pan, zoom and fit;
- drag blocks;
- Shift-click multi-selection and group movement;
- per-block resize, persisted as presentation metadata;
- output-to-input wiring;
- right-click creation of Code, Annotation, Display, Component, Group and Meta
  blocks;
- run selected block through marimo;
- edit cell source inside Zeron with `@ds/code_editor`;
- delete a block;
- save an ad-hoc block into the reusable component library only when explicitly
  requested;
- save multiple selected blocks as a reusable Group Block.

`.graphimo.json` stores only visual state such as x/y/width/height. Deleting
it cannot change the notebook program.

## Components

A project may contain:

```text
notebook.py
components/
  data-processing/
    open_csv.py
    normalize.py
  models/
    bodypart/
      __init__.py
  groups/
  meta/
    pytorch/
```

Folders form categories. Normal `.py` files and Python packages are discovered
automatically.

There is no mandatory Graphimo base class, decorator or manifest for ordinary
components. Public callables are inspected with `inspect.signature`;
`__all__` is respected when present.

New canvas blocks are **not** automatically saved to this library. Use
**Save component** when a block has become reusable.

## Group Blocks

Selecting two or more blocks and choosing **Save group** creates a referenced
definition under:

```text
components/groups/<category>/<name>/
  group.py
  group.json
```

`group.py` is an ordinary marimo notebook. A parent notebook reuses it with
marimo app composition (`clone().embed(defs=...)`).

Therefore:

- the group definition exists once;
- instances remain references;
- changing the definition changes future executions of its instances;
- the parent graph sees only the group's boundary inputs/outputs;
- **Edit group** opens the group's own Graphimo graph.

## PyTorch Meta Block

A PyTorch Meta Block is intentionally different from a Group Block.

Its internal items are **Elements**, not Graphimo blocks. They have no marimo
cell, Run action, cache or stdout of their own.

The initial element library includes:

- Input / Output;
- Conv1d / Conv2d / Conv3d;
- Linear;
- ReLU / GELU;
- BatchNorm1d / BatchNorm2d / LayerNorm;
- Dropout;
- Flatten;
- MaxPool2d / AdaptiveAvgPool2d;
- Identity;
- Add / Concat;
- Custom `nn.Module`.

The editor validates a directed acyclic graph and generates one readable:

```python
class GraphimoModel(nn.Module):
    ...
    def forward(self, ...):
        ...
```

plus `build_model()`.

On the main Graphimo canvas the Meta Block is therefore one normal block whose
output is a `torch.nn.Module`. PyTorch, autograd, CUDA and optional
`torch.compile` operate on that model normally; Graphimo never executes every
layer as a separate notebook cell.

## marimo controls and rich output

Graphimo inventories live marimo controls from the active kernel.

For supported controls, Zeron renders the native control while sending the
frontend value back to the original marimo UIElement through marimo's official
kernel endpoint. No duplicate reactive state machine is introduced.

Display Blocks use marimo's already-rendered output rather than reimplementing
DataFrame/chart/video renderers.

## Terminal and logs

`print()`/stdout/stderr are not displayed inside normal Base Blocks.

The bottom panel has two modes:

- **Terminal** — console output grouped by block;
- **All logs** — Graphimo bridge/runtime diagnostics.

## Agents

Graphimo publishes tools to Zeron agents for inspecting and changing the same
live notebook used by the human UI, including:

- inspect;
- create/edit/delete/run block;
- add/connect components;
- manual ports;
- save/add Group Blocks;
- create/get/save/add PyTorch Meta Blocks;
- set marimo control values;
- save a block as a reusable Component.

The UI also offers **Create with agent** and **Agent**. They open a normal Zeron
chat scoped to the current project and instruct it to use Graphimo's tools.
There is no separate AI graph representation.

## marimo session and Python environment

Graphimo pins `marimo==0.25.2` while it relies on marimo private code-mode
APIs.

The top bar can choose:

- Auto;
- project `.venv`;
- project `venv`;
- system Python;
- Graphimo-managed environment.

If Graphimo must start `marimo edit`, the editor is **not opened visibly**.
An off-screen Zeron webview establishes the marimo websocket/session while the
user stays in Graphimo.

The managed runtime and logs live under:

```text
~/.local/share/graphimo/
~/.local/state/graphimo/
```

## Boundaries

The production bridge is `plugin/marimo_bridge.luau`. It is the single
boundary for marimo private APIs and local HTTP calls.

Graphimo deliberately does **not** own:

- dependency resolution;
- execution ordering;
- notebook namespace;
- cache;
- model training/checkpoint policy;
- distributed scheduling;
- remote command routing.

Future Zeron device execution can be consumed as a host capability without
changing Graphimo's block semantics.

The Python `adapter/` directory is an older SDK-independent contract harness
used by existing tests. Production behavior is defined by the Luau bridge; new
Graphimo features should be tested against that bridge or the live marimo
runtime rather than assuming the adapter is authoritative.

## Development

Install the pinned notebook runtime and Python test tools:

```bash
python -m pip install -e '.[notebook,dev]' websockets
pytest -q
ruff check adapter examples components tests
```

The repository contains optional Luau/live-marimo tests. When the Luau CLI is
available:

```bash
GRAPHIMO_MARIMO_PYTHON="$(command -v python)" pytest -q tests/test_luau_marimo_live.py
```

CI runs Python contracts and architectural invariant checks automatically.

### End-to-end host check

The final integration layer is Zeron itself. Before a release, test a
disposable notebook in the current Zeron build and verify at minimum:

1. notebook bootstrap stays invisible;
2. cards/ports render;
3. output wiring edits Python and marimo recomputes downstream cells;
4. Zeron code editing saves into the live cell;
5. slider/dropdown updates rerun dependents;
6. Display HTML/image output renders;
7. Group save/reinsert/edit works;
8. PyTorch Meta creation/edit/save produces an importable `nn.Module`;
9. agent tools operate on the same live notebook.
