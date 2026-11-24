# Graphimo V1 - Implementation Contract for Codex

# 0. Implementation posture - normative

This document is an **implementation contract**, not a feasibility study. Once a mechanism is specified here and the referenced dependency behavior is documented/pinned, Codex must treat that mechanism as decided.

Codex must **not** run architectural spikes to decide whether a specified mechanism is possible, replace it with an alternative because another design seems easier, or reinterpret a requirement as provisional. Codex implements the contract as written.

Tests in this document are **conformance and regression tests** only. They verify that the implementation matches the contract; they do not decide whether the architecture is valid. If a test fails, first fix the implementation. Escalate to the specification only when there is a concrete contradiction between two requirements or a pinned dependency behaves differently from its documented contract.

When a product decision is explicitly marked `HUMAN DECISION REQUIRED`, Codex stops at that boundary. It must not prototype alternatives, choose one silently, or turn the open decision into an implementation experiment. Once the decision is added to this document, it becomes normative like the rest.

Status: implementation specification, not a product essay.  
Target repository: `SorbetUP/graphimo`  
Target starting branch: `feat/graphimo-core-vertical-slice`  
Observed branch head when this document was written: `8e9d2719d850b52a09551caf877585231d9b4402`.

**FULL IMPLEMENTATION READINESS - NOT READY YET.** The base Python/marimo architecture is specified far enough for the early core milestones, but Codex must not yet be told to implement the entire product through the PyTorch addon. Before a full implementation handoff, this document still needs: (1) a host-independent development/test workbench contract so the functional layer can be exercised visually without coupling it to Zeron; (2) the exact Group Block marimo backend; and (3) a dedicated, concrete PyTorch addon contract covering persisted graph schema, V1 element semantics, compiler outputs, shape/type propagation, telemetry routing, TorchLens behavior, and the external model value. The final Zeron adapter is deliberately deferred until the functional contracts are stable.

**Decision update - 2026-11-24: typed output ports.** Prefer explicit Python return annotations (`-> float`, `-> tuple[Tensor, float]`). The annotation is the static type contract; `return` expressions provide the human-readable source description; runtime values provide bounded previews. Multiple return paths are supported when output arity is stable.

## 0. How Codex must use this document

Implement the milestones in order. Do not redesign the architecture while implementing a milestone. Do not implement a later milestone early because a convenient abstraction suggests it. When a section says **HUMAN DECISION REQUIRED**, stop that feature and record the unresolved requirement as a TODO/reference to this specification. Do not create a feasibility prototype and do not invent a product decision.

Every milestone ends with an explicit test gate. Do not start the next milestone until the gate passes. Keep the current Zeron plugin usable during migration.

The V1 architecture is intentionally small:

- marimo owns Python execution, dependency analysis, cell execution, stale state, rich output and session memory;
- Graphimo owns the canvas, generated block-instance calls, source editing, layout, reusable `@node` definitions, Group Blocks, native ArtifactStore API and the bounded addon/MetaBlock API;
- Graphimo must not introduce a second scheduler, second DAG runtime or generic Python snapshot engine.

The existing experimental `core/graphimo_core/` code is reference/prototype code. It must not become the production executor.

---

## 1. Required user-visible semantics

### 1.1 A node definition is a normal Python function

A reusable block is defined once:

```python
from graphimo import node

@node
def normalize(dataset, mean=0.0, std=1.0):
    normalized = (dataset - mean) / std
    stats = {"mean": normalized.mean(), "std": normalized.std()}
    return normalized, stats
```

The user edits this function body. `@node` must not schedule the function or move execution to another runtime. Calling the decorated function from normal Python must behave like calling the original function.

### 1.2 A canvas block instance is a function call

Creating the same block twice creates two calls, never two copies of the function body.

Graphimo-managed marimo cell body for instance 1:

```python
# Graphimo node: normalize
# Graphimo instance: 4f2a7c91
g_normalize_4f2a7c91 = normalize(
    dataset=g_load_a19d0c21,
    mean=0.0,
    std=1.0,
)
```

Instance 2:

```python
# Graphimo node: normalize
# Graphimo instance: a71c30de
g_normalize_a71c30de = normalize(
    dataset=g_load_a19d0c21,
    mean=0.5,
    std=2.0,
)
```

The opaque global symbol is always:

```text
g_<sanitized_function_name>_<8_hex_chars>
```

Example: `g_train_4f2a7c91`.

The user does not edit this generated name. It exists only to give marimo one stable global definition per instance.

### 1.3 Connections are ordinary Python arguments

If `train(dataset, labels)` consumes two outputs of `prepare`, the generated call may be:

```python
# Graphimo node: train
# Graphimo instance: 12bc09ef
g_train_12bc09ef = train(
    dataset=g_prepare_77af2100[0],
    labels=g_prepare_77af2100[1],
)
```

Graphimo shows named ports (`dataset`, `labels`) from the `train` signature. The tuple indices are an internal wiring detail and are never the port labels.

Reconnect `dataset` to another output by changing only the `dataset=...` expression in this generated call. Never edit the `train` function body to reconnect a link.

### 1.4 One instance has one global result symbol

Never generate one global variable per output. This is forbidden:

```python
model_12bc = ...
metrics_12bc = ...
```

The required form is:

```python
g_train_12bc09ef = train(...)
```

If `train` returns multiple values, they remain inside the returned tuple/object and downstream calls select the required item.

### 1.5 Output ports use the declared return type + return expression + runtime value

The preferred Graphimo style is to annotate node return types explicitly. The return annotation is the **static type contract** used by the UI before execution. The source expression is taken from the `return` statement. The runtime value enriches the same port after execution.

For:

```python
@node
def price(...) -> float:
    ...
    return c
```

before execution show:

```text
float (c)
```

and if runtime `c == 4.2`, show:

```text
float (4.2 | c)
```

For:

```python
@node
def total(prix, nbr) -> float:
    return prix * nbr
```

and runtime result `34.2`, show:

```text
float (34.2 | prix * nbr)
```

For:

```python
@node
def encode(x) -> Tensor:
    return calc(x)
```

show before execution:

```text
Tensor (calc(x))
```

and after execution a bounded semantic preview, for example:

```text
Tensor (shape=(32, 768), dtype=float32 | calc(x))
```

If the function has no return annotation, Graphimo may fall back to `?` before execution and to the runtime type after execution. Do not require annotations for Python execution, but treat explicit annotations as the preferred Graphimo authoring style.

For multiple outputs, prefer a tuple return annotation whose arity matches the tuple return:

```python
@node
def prepare(...) -> tuple[Tensor, Tensor, dict]:
    ...
    return images, labels, stats
```

This creates three output ports, in order:

```text
Tensor (images)
Tensor (labels)
dict (stats)
```

After execution each port receives its bounded runtime preview.

Lists, dicts and arbitrary objects are one output unless they are separate top-level expressions in a tuple return. Example:

```python
return {"images": images, "labels": labels}
```

is one output, not two ports.

### 1.6 Multiple control-flow returns are supported when output arity is stable

Do not reject a node merely because it contains several `return` statements. Inspect every relevant `return` in the function body, excluding nested functions/classes.

Example:

```python
@node
def choose(x) -> float:
    if x > 0:
        c = x * 2
        return c
    d = -x
    return d
```

There is one output port because every return path has arity 1. Its static UI description is:

```text
float (c | d)
```

The final intended runtime UI is:

```text
float (4.2 | c)
```

when the `c` return path was taken, or:

```text
float (3.1 | d)
```

when the `d` return path was taken.

For V1 static inspection, collecting the alternative expressions is mandatory. Tracking the exact executed return path is a separate runtime concern; do not add expensive instrumentation silently in Milestone 1. The active-path mechanism must be implemented only behind a measured, tested runtime hook.

All relevant return paths must have the same output arity. This is invalid as a Graphimo node contract:

```python
if condition:
    return a
return a, b
```

The Python function may still execute normally, but Graphimo must emit a structured `node_output_arity_inconsistent` diagnostic and must not expose unstable ports for wiring.

Do not invent user-facing names such as `output_1`. Port identity is positional for wiring; port display is based on declared type, return expression(s), and runtime preview.

---

## 2. Repository migration rule

Keep the current plugin functional while the new semantics are introduced.

### Keep and evolve

- `plugin/init.luau`
- `plugin/canvas.luau`
- `plugin/layout.luau`
- `plugin/component_library.luau`
- `plugin/marimo_bridge.luau`
- `adapter/graphimo_adapter.py`
- existing marimo live tests and packaging tests
- the useful existing ArtifactStore logic after the ArtifactStore milestone

### Do not use as production execution architecture

- `core/graphimo_core/runtime.py`
- `core/graphimo_core/worker.py`
- `WorkspaceGraph` as the authoritative runtime DAG
- `execute_node(source)` as a product execution format
- `vertical_demo.py` as the product execution path

Do not delete these files during Milestones 1-4. Add a short `EXPERIMENTAL.md` note later if necessary. Deletion/move is a cleanup task after the marimo-based production path is implemented and its conformance tests pass.

---

# MILESTONE 1 - Python `graphimo` package and `@node` contract

## 3. Files to create

Create:

```text
graphimo/
  __init__.py
  node.py
  outputs.py
  preview.py

tests/
  test_node_decorator.py
  test_node_outputs.py
  test_value_preview.py
```

Modify `pyproject.toml` so the `graphimo` package is included in the distribution.

## 4. Exact public API for Milestone 1

`graphimo/__init__.py` must expose:

```python
from .node import node, is_node, inspect_node
from .preview import preview_value, describe_result

__all__ = [
    "node",
    "is_node",
    "inspect_node",
    "preview_value",
    "describe_result",
]
```

### 4.1 `@node`

Implement:

```python
def node(fn): ...
def is_node(value) -> bool: ...
```

Requirements:

- `node(fn)` returns a callable with identical call semantics;
- use `functools.wraps` if a wrapper is used;
- preserve `__name__`, `__qualname__`, `__doc__`, `__annotations__`, and inspectable signature;
- mark it with an internal boolean attribute, e.g. `__graphimo_node__ = True`;
- do not import marimo inside the decorator;
- do not register a global scheduler or registry as a side effect.

Preferred simplest implementation: mark the function and return the same function object.

### 4.2 Node descriptor types

Create immutable dataclasses in `graphimo/node.py` or `graphimo/outputs.py`:

```python
@dataclass(frozen=True)
class InputPort:
    name: str
    required: bool
    annotation: str | None
    has_default: bool
    default: object | None

@dataclass(frozen=True)
class OutputPort:
    index: int
    declared_type: str | None
    expressions: tuple[str, ...]

@dataclass(frozen=True)
class NodeSpec:
    name: str
    inputs: tuple[InputPort, ...]
    outputs: tuple[OutputPort, ...]
    diagnostic: str | None
```

Do not add more fields without a test that needs them.

### 4.3 `inspect_node(fn)`

Implement:

```python
def inspect_node(fn) -> NodeSpec: ...
```

Rules:

1. reject a value that is not decorated with `@node` using `ValueError` or a small dedicated `NodeContractError`;
2. inputs come from `inspect.signature(fn)` in signature order;
3. do not evaluate forward references or arbitrary annotations;
4. use the existing safe annotation/default logic from `adapter/graphimo_adapter.py` where possible instead of creating a second unsafe implementation;
5. obtain source with `inspect.getsource`, `textwrap.dedent`, then `ast.parse`;
6. ignore `return` statements inside nested functions/classes;
7. normalize every relevant return into an ordered list of top-level output expressions:
   - `return None` or bare `return` => arity 0;
   - `return a, b` or `return (a, b)` => arity 2;
   - any other expression => arity 1;
8. all relevant return statements must have the same arity; otherwise return a `NodeSpec` with diagnostic `node_output_arity_inconsistent` and no wireable output contract;
9. for each output index, collect the unique source expressions from all return paths in source order; prefer `ast.get_source_segment`, fallback to `ast.unparse`;
10. parse the function return annotation without evaluating arbitrary forward references:
    - for arity 1, the whole safe annotation is the declared type of output 0;
    - for arity N > 1, a fixed `tuple[T1, ..., TN]` / `typing.Tuple[...]` annotation supplies one declared type per output;
    - if a tuple annotation arity conflicts with the return arity, set diagnostic `node_return_annotation_arity_mismatch`;
    - if no safe per-output annotation is available, use `declared_type=None`;
11. multiple return paths with stable arity are supported and must not produce an `output_1` fallback.

## 5. Runtime preview API

Implement:

```python
def preview_value(value, *, max_chars: int = 80, max_items: int = 6) -> dict: ...

def describe_result(value, spec: NodeSpec) -> list[dict]: ...
```

### 5.1 `preview_value`

Return JSON-safe data only. Required keys:

```python
{
    "runtime_type": "float",
    "preview": "4.2",
}
```

Special previews required without mandatory optional dependencies:

- built-in scalar: bounded `repr`;
- `str`: bounded text, with truncation marker;
- list/tuple/dict: type + length + bounded sample, never full large serialization;
- object with class module/name `torch.Tensor`: include shape/dtype/device if attributes exist;
- object with class module/name `numpy.ndarray`: include shape/dtype if attributes exist;
- pandas DataFrame-like object: rows/columns if safely available;
- unknown objects: class name + bounded safe `repr`; catch exceptions from `repr` and return a fallback string.

Never call arbitrary serialization methods.

### 5.2 `describe_result`

For a supported `NodeSpec`:

- if zero outputs, return `[]`;
- if one output, preview the returned value itself;
- if N outputs, require a tuple of length N and preview each item by index;
- add `declared_type` and `expressions` from the corresponding `OutputPort`;
- result example:

```python
[
    {
        "index": 0,
        "declared_type": "float",
        "runtime_type": "float",
        "preview": "4.2",
        "expressions": ["c"],
        "active_expression": null,
    },
]
```

Formatting `float (4.2 | c)` is a UI concern; keep the Python API structured. `active_expression` remains `null` until the separately specified active-return-path runtime hook exists.

If runtime arity does not match the statically described output count, return/raise a clear `node_output_arity_mismatch` diagnostic. Do not silently truncate.

## 6. Milestone 1 tests

Mandatory tests:

1. decorated function still computes the same result as before decoration;
2. signature/name/docstring/annotations are preserved;
3. `def f(...) -> float: return c` gives one output with `declared_type="float"` and expressions `("c",)`;
4. `return prix * nbr` preserves expression `prix * nbr`;
5. `return calc(x)` preserves expression `calc(x)`;
6. `-> tuple[float, int, str]` + `return a, b, c` gives three typed ports in order;
7. `return {"a": a, "b": b}` gives one port;
8. two return paths `return c` / `return d` with `-> float` give one port with expressions `("c", "d")`;
9. inconsistent return arity produces `node_output_arity_inconsistent`;
10. tuple annotation arity mismatch produces `node_return_annotation_arity_mismatch`;
11. preview of float matches runtime type `float`, preview `4.2`;
12. a large list is bounded;
13. an object whose `__repr__` raises does not crash preview;
14. tuple runtime arity mismatch is detected.

### Gate M1

Run:

```bash
pytest -q tests/test_node_decorator.py tests/test_node_outputs.py tests/test_value_preview.py
ruff check graphimo tests/test_node_decorator.py tests/test_node_outputs.py tests/test_value_preview.py
```

Do not start Milestone 2 until green.

---

# MILESTONE 2 - Canonical block-instance source generation

## 7. Files to create

Create:

```text
graphimo/instance.py
tests/test_instance_source.py
```

## 8. Exact instance model

Create:

```python
@dataclass(frozen=True)
class Binding:
    input_name: str
    expression: str

@dataclass(frozen=True)
class InstanceSpec:
    instance_id: str
    node_name: str
    result_var: str
    bindings: tuple[Binding, ...]
```

### 8.1 ID and variable generation

Implement:

```python
def new_instance_id() -> str: ...
def result_variable(node_name: str, instance_id: str) -> str: ...
```

Rules:

- ID is exactly 8 lowercase hex characters;
- use a cryptographically/randomly generated value (`secrets.token_hex(4)` is sufficient);
- sanitize function name to a valid Python identifier fragment;
- output exactly `g_<name>_<id>`;
- reject/correct names that would create invalid Python.

Example:

```text
g_train_4f2a7c91
```

## 9. Canonical marimo cell BODY

Important: Graphimo inserts cell body source through the live marimo session. Do not generate the serialized `@app.cell def _(...):` wrapper yourself. Marimo owns that serialization.

Implement:

```python
def build_instance_source(
    node_name: str,
    bindings: Mapping[str, str],
    *,
    instance_id: str | None = None,
) -> tuple[str, InstanceSpec]: ...
```

Canonical output:

```python
# Graphimo node: normalize
# Graphimo instance: 4f2a7c91
g_normalize_4f2a7c91 = normalize(
    dataset=g_load_a19d0c21,
    mean=0.0,
    std=1.0,
)
```

Rules:

- markers are mandatory and exact;
- use keyword arguments only in generated instances;
- preserve the node signature order when the caller supplies the node spec; otherwise preserve insertion order;
- each binding expression must parse as a Python expression;
- the generated source contains only the markers and one assignment/call;
- do not add hidden scheduler calls, Graphimo runtime wrappers or duplicated function body;
- literals such as `0.5`, `True`, strings, lists etc. may be bindings if they are valid bounded source literals;
- connected values use expressions such as `g_prepare_77af2100[0]`.

## 10. Parse only Graphimo-owned instance cells

Implement:

```python
def parse_instance_source(source: str) -> InstanceSpec | None: ...
```

It must return a spec only if:

- both exact marker lines exist;
- there is exactly one assignment statement after them;
- the target exactly matches the derived `g_<node>_<id>` name;
- the RHS is a direct call to the marked node name;
- all arguments are keyword arguments;
- there are no extra statements.

Otherwise return `None`. Never treat arbitrary user Python as a generated instance.

## 11. Reconnection

Implement:

```python
def reconnect_instance_source(
    source: str,
    *,
    input_name: str,
    expression: str,
) -> str: ...
```

Rules:

- only accept a source that `parse_instance_source` owns;
- replace exactly one keyword argument;
- validate `expression` with `ast.parse(..., mode="eval")`;
- if the input does not exist, fail without modifying source;
- regenerate canonical source;
- never modify another argument;
- never modify the node definition.

## 12. Output connection expressions

Implement a helper:

```python
def output_expression(instance: InstanceSpec, output_index: int, output_count: int) -> str: ...
```

Rules:

- one-output node => `g_name_id`;
- N-output node => `g_name_id[index]`;
- validate index bounds.

This is internal wiring. UI labels still come from `OutputPort.declared_type`, `OutputPort.expressions`, and the runtime preview.

## 13. Milestone 2 tests

Mandatory tests:

- exact canonical source snapshot;
- two calls to `build_instance_source` get different IDs;
- `g_<function>_<id>` format;
- parse/build round-trip;
- arbitrary similar Python without markers is rejected;
- extra statements are rejected;
- reconnect changes only requested keyword;
- invalid Python connection expression is rejected;
- one-output connection uses bare result variable;
- multi-output connection uses index;
- user-visible output metadata is not renamed to tuple indices.

### Gate M2

Run Milestone 1 tests + Milestone 2 tests + Ruff.

---

# MILESTONE 3 - Integrate the new node/instance semantics into the existing adapter

## 14. Files to modify

Primary:

```text
adapter/graphimo_adapter.py
pyproject.toml
examples/simple_pipeline.py
examples/ml_pipeline.py
```

Add tests:

```text
tests/test_node_component_discovery.py
tests/test_node_instance_adapter.py
```

Do not redesign `plugin/init.luau` yet except for changes strictly required by an adapter contract already covered by tests.

## 15. Component discovery migration

Current component discovery finds public callables. Change the execution-block rule:

- only callables marked with `@node` are executable Graphimo Base Blocks;
- non-`@node` callables may remain visible as ordinary library/helper information only if the current UI needs backward compatibility, but they must not be insertable as executable Base Blocks after migration is complete;
- reuse the safe signature/default inspection already present in `adapter/graphimo_adapter.py`;
- use `graphimo.inspect_node` for output ports instead of maintaining a second output inference implementation.

Expected component item fragment:

```json
{
  "name": "normalize",
  "graphimo_node": true,
  "inputs": [...],
  "outputs": [
    {"index": 0, "declared_type": "Tensor", "expressions": ["normalized"]},
    {"index": 1, "declared_type": "dict", "expressions": ["stats"]}
  ]
}
```

Do not add runtime values here; discovery is static.

## 16. Insertion migration

Change the existing `add_component`/source-generation path so a selected `@node` creates a Graphimo-owned instance cell using `graphimo.instance.build_instance_source`.

Do not duplicate code-generation rules in the adapter.

Example inserted cell BODY:

```python
# Graphimo node: normalize
# Graphimo instance: 4f2a7c91
g_normalize_4f2a7c91 = normalize(
    dataset=g_load_a19d0c21,
    mean=0.0,
    std=1.0,
)
```

If a required input has no connection/value, insertion must either:

- refuse with a structured `missing_required_binding` error; or
- use the existing UI flow to collect a binding before insertion.

Do not insert undefined placeholders that make the notebook invalid.

## 17. Connection migration

Change the existing `connect` path:

- the destination must be a parsed Graphimo-owned instance cell;
- destination input name must exist in the target node signature;
- source output expression comes from `output_expression(...)`;
- update only the destination call argument with `reconnect_instance_source`;
- let marimo recompute refs/defs and validate the resulting graph.

Do not maintain a second edge database.

## 18. Instance runtime inspection

Add a Python adapter operation, exact name can be `inspect_node_instance`:

Inputs:

```json
{
  "session_id": "...",
  "cell_id": "..."
}
```

Behavior:

1. inspect the cell source;
2. require a valid Graphimo instance marker/call;
3. obtain the node definition/name and `NodeSpec`;
4. access the result variable in the live marimo namespace through the existing marimo context boundary;
5. call `graphimo.describe_result(value, spec)`;
6. return JSON-safe structured outputs.

Expected response fragment:

```json
{
  "ok": true,
  "instance_id": "4f2a7c91",
  "node": "normalize",
  "outputs": [
    {
      "index": 0,
      "declared_type": "ndarray",
      "runtime_type": "ndarray",
      "preview": "shape=(128, 64), dtype=float32",
      "expressions": ["normalized"],
      "active_expression": "normalized"
    }
  ]
}
```

Do not use raw full `repr` for arbitrary large values.

## 19. Example migration

Update at least one example to define a node once and instantiate it twice.

Required semantic example:

```python
@node
def scale(x, factor=1.0):
    y = x * factor
    return y
```

Two instances with different factors must exist as two separate marimo cells.

## 20. Gate M3

Mandatory conformance checks:

- one `@node` definition appears once in library;
- two inserted instances call the same function;
- reconnecting one instance does not alter the other;
- marimo sees separate cells and separate result globals;
- no `WorkspaceGraph` or `LocalPythonWorker` is used;
- adapter returns runtime preview with type/value/expression.

Run the whole existing Python contract suite after adapting expected fixtures.

---

# DEFERRED PRODUCTION ADAPTER - Zeron Luau bridge parity + live marimo conformance

## 21. Files to modify

```text
plugin/marimo_bridge.luau
plugin/init.luau            # only necessary rendering/interaction changes
plugin/base_block.luau      # adapt projection to new node/instance contract
plugin/canvas.luau          # only if port labels need the new descriptor
tests/test_luau_marimo_live.py
```

The production Luau bridge and Python adapter must expose equivalent semantics. Do not reimplement AST/source logic in Luau.

Where possible, the Luau-generated marimo-side Python should import and call the installed `graphimo` Python helpers instead of duplicating parsing/code-generation logic in Luau strings.

## 22. Card rendering contract

For an instance card show:

- node function name;
- input ports from node signature;
- output ports from the declared return annotation plus static return expression(s);
- if runtime inspection is available, format each output as:

```text
<Type> (<preview> | <expression>)
```

Examples:

```text
float (4.2 | c)
float (34.2 | prix * nbr)
Tensor (shape=(32, 768), dtype=float32 | calc(x))
```

Before first successful execution, prefer the declared return type. Example for `-> float`:

```text
float (prix * nbr)
```

If no safe return annotation exists, use `?` as the type placeholder.

Do not show `output_1` when an expression exists.

## 23. Live marimo conformance scenario

Automate this scenario in the live marimo test:

1. create/open a temporary marimo notebook;
2. define `@node scale(x, factor)` once;
3. create source data cell;
4. create `scale` instance A with `factor=2`;
5. create `scale` instance B with `factor=3` using the same definition;
6. execute;
7. inspect both instance outputs and verify type/value/expression;
8. reconnect instance B to a different source;
9. verify instance A source is unchanged byte-for-byte;
10. verify marimo recomputes B correctly;
11. save/reopen notebook and repeat inspection.

### Deferred Zeron adapter gate

When the production Zeron adapter is scheduled, the full repo tests + live marimo test must pass. This deferred gate does **not** block the host-independent disabled-block, ArtifactStore, Group Block, MetaBlock or PyTorch functional milestones. Those are exercised first through the standalone development workbench specified in Section 40.

---

# MILESTONE 5 - Disabled instance blocks

## 24. Exact behavior

A disabled instance remains a marimo cell but its body is comments so it leaves the executable DAG.

Canonical disabled body:

```python
# graphimo:disabled-begin
# Graphimo node: normalize
# Graphimo instance: 4f2a7c91
# g_normalize_4f2a7c91 = normalize(
#     dataset=g_load_a19d0c21,
#     mean=0.0,
#     std=1.0,
# )
# graphimo:disabled-end
```

Implement:

```python
def disable_instance_source(source: str) -> str: ...
def enable_instance_source(source: str) -> str: ...
```

Rules:

- only disable Graphimo-owned generated instance source;
- enabling must recover byte-equivalent canonical active source;
- nested/duplicate markers are rejected;
- disabled instance remains visible in Graphimo using the markers and `.graphimo.json` layout;
- its output ports are visible but marked unavailable/stale;
- links that depend on it must be shown broken/stale rather than silently rewired.

**HUMAN DECISION REQUIRED:** exact UX for downstream cells when an upstream instance is disabled. Do not auto-delete links.

---

# MILESTONE 6 - Native ArtifactStore, independent from dataset UI

## 25. Product decision already fixed

ArtifactStore is native because persisted artifacts are useful for datasets, checkpoints, models, predictions, embeddings, images, PDFs, exports and traces. It is not a Dataset MetaBlock.

Persistence is explicit in V1. Do not automatically persist arbitrary Python outputs.

## 26. Public API target

Create/refactor to:

```text
graphimo/artifacts/
  __init__.py
  local.py
  types.py
```

Public API:

```python
artifact = graphimo.artifacts.put(path, kind="checkpoint")
artifact = graphimo.artifacts.put_bytes(data, kind="blob")
path = graphimo.artifacts.materialize(artifact, destination)
stats = graphimo.artifacts.stats()
graphimo.artifacts.pin(artifact, owner="saved-model")
graphimo.artifacts.unpin(artifact, owner="saved-model")
graphimo.artifacts.gc(referenced=...)
```

`ArtifactRef` minimum fields:

```python
@dataclass(frozen=True)
class ArtifactRef:
    id: str
    kind: str
```

Keep backend details private.

## 27. Reuse existing ArtifactStore code, but fix large-file behavior

The existing `LocalArtifactStore` implementation may be reused after these mandatory changes:

- SHA-256 hashing must stream file chunks; never `read_bytes()` an arbitrary large source solely to hash it;
- verification of large blobs must stream;
- directory manifests deduplicate unchanged files;
- APFS clonefile/Linux FICLONE may be used when supported;
- fallback copy must stream;
- keep pin/unpin + mark/sweep GC;
- add logical bytes, physical stored bytes and deduplicated bytes stats;
- no Borg/Kopia/FastCDC dependency in V1.

Do not implement content-defined chunking in V1. Reconsider it only after measured real Graphimo workloads show that file-level deduplication causes unacceptable storage waste; that later optimization does not alter the V1 ArtifactStore contract.

---

# MILESTONE 7 - Group Blocks

## 28. User semantics fixed, backend choice still open

A Group Block is a reusable workflow defined once and called like a function from another Graphimo project/file. Multiple group instances are multiple calls, not copied source.

Target parent usage:

```python
from groups.bodypart import bodypart_pipeline

g_bodypart_12ab34cd = bodypart_pipeline(
    dataset=g_dataset_9988aa11,
    config=config,
)
```

Future optional command: “import as copy/vendor” copies the group implementation into the current project instead of keeping a reference.

**HUMAN DECISION REQUIRED before implementation:** exact marimo mechanism for executing a nested workflow (`app.run(defs=...)`, generated callable wrapper, or another minimal marimo-supported path). Codex must not prototype or choose among these alternatives unless explicitly asked. Do not implement a custom scheduler.

Codex must not implement Group Blocks before this decision.

---

# MILESTONE 8 - MetaBlock semantic scopes and extension seam

## 29. MetaBlock semantics - fixed

A MetaBlock is **not** a separate mini-application, iframe, webview or second canvas engine.
It is still Graphimo.

Graphimo has nested graph scopes. A scope has a semantic dialect:

- the root/default dialect is normal Graphimo Python: executable block instances are calls to `@node` definitions and marimo owns execution;
- a **Group Block** wraps/reuses a subgraph without changing the semantic dialect of that subgraph;
- a **MetaBlock** wraps a subgraph whose internal semantic dialect is provided by an extension/plugin.

Consequences:

- Group Blocks may exist inside MetaBlocks;
- MetaBlocks may exist inside Group Blocks;
- MetaBlocks may contain other MetaBlocks;
- nesting is recursive;
- all of them are rendered inside the normal Graphimo canvas and navigation model;
- opening a MetaBlock means entering that nested Graphimo scope, not launching an unrelated application.

A PyTorch-model MetaBlock therefore contains PyTorch-architecture elements such as `Linear`, `RMSNorm`, `Softmax`, residual additions, head-splitting operations, RoPE, losses and nested reusable groups. Those internal elements are **not** required to be normal `@node` calls or independent marimo cells. The PyTorch extension owns the rules that map that internal graph to/from PyTorch code.

The MetaBlock does not become a new scheduler. It must not replace marimo for normal Graphimo execution.

## 30. Core graph model - normative minimum

Implement only the minimum internal concept specified below for nested semantic scopes. Do not add a larger public plugin API until its remaining product decisions are explicitly specified.

Target conceptual model:

```python
@dataclass
class GraphScope:
    id: str
    dialect_id: str
    elements: list[GraphElement]
    edges: list[GraphEdge]

@dataclass
class GroupBlock:
    id: str
    scope: GraphScope

@dataclass
class MetaBlock:
    id: str
    plugin_id: str
    scope: GraphScope
```

The exact Python classes/files may differ if the existing codebase has a better representation, but the semantics above are mandatory.

A Group Block **does not switch dialect**. A MetaBlock **does switch dialect**.

## 31. MetaBlock addons are physically optional - fixed

A MetaBlock dialect is an **installable Graphimo addon**. PyTorch is the first reference addon; it is not part of Graphimo Core semantics.

The following invariants are mandatory:

- `graphimo-core` must import and run with no `torch`, TorchLens, PyTorch element type, model compiler or PyTorch telemetry code installed;
- Graphimo Core must never contain branches such as `if dialect == "pytorch"`;
- removing the PyTorch addon must not break normal `@node`, Group Block, ArtifactStore or canvas behavior;
- a project containing a MetaBlock whose addon is missing must still open without data loss. Graphimo shows that MetaBlock as unavailable/opaque and preserves its serialized state until the addon is installed again;
- addons are discovered dynamically. V1 should use Python package entry points rather than hard-coded imports. Recommended entry-point group: `graphimo.metablocks`;
- the PyTorch addon may live in the same monorepo while being developed, but it must be a **separate independently installable package** with its own dependencies and tests.

Recommended repository boundary:

```text
graphimo/                    # core; no torch dependency
  ...
addons/
  pytorch/                   # separate Python distribution
    pyproject.toml
    graphimo_pytorch/
    tests/
```

Recommended discovery shape:

```python
from importlib.metadata import entry_points

def discover_metablock_addons():
    return entry_points(group="graphimo.metablocks")
```

The addon boundary is normative: addons are separate installable packages discovered dynamically and Core contains no addon-specific semantics. The exact **stable public handler API** is not yet frozen because several MetaBlock product decisions remain explicitly open later in this document. Codex must implement only the registration/discovery contract specified here and must not invent additional public methods. Once the remaining human decisions are written into this document, that API becomes normative.

Conceptual minimum:

```python
@dataclass(frozen=True)
class MetaDialectRegistration:
    dialect_id: str
    addon_version: str
    api_version: int
    factory: Callable[[ExperimentalGraphimoAPI], object]
```

The object returned by `factory(...)` may use private/internal interfaces while the remaining MetaBlock decisions are still unresolved, but those interfaces must not leak PyTorch-specific semantics into Graphimo Core. Codex must not promote an internal method to the public addon contract without an explicit specification update.

Do **not** create a closed public list such as `LineChart`, `Slider`, `Histogram`, `Canvas`, etc. as the definition of what a MetaBlock is allowed to do. A MetaBlock remains inside Graphimo and reuses Graphimo's graph/canvas/navigation model, while its addon supplies the specialized semantics and element behavior.

The stable addon API will be frozen by an explicit specification decision, using the already-defined PyTorch addon requirements as the first concrete client. Codex does not decide that API by experimentation.

### 31.1 Mandatory isolation conformance test

The implementation must satisfy all of the following:

1. install Graphimo Core without the PyTorch addon; core tests pass and Graphimo starts;
2. install the PyTorch addon separately; discovery returns the `pytorch` dialect without modifying Core code;
3. create/save a tiny PyTorch MetaBlock;
4. remove/uninstall the addon; Graphimo still opens the project, preserves the MetaBlock state and renders an unavailable-addon placeholder;
5. reinstall the addon; the same MetaBlock becomes available again without migration or data loss;
6. `grep`/dependency inspection confirms that Graphimo Core has no direct imports of `torch` or `torchlens`.

If any assertion fails, the implementation is non-conformant. Fix the implementation. Do not redesign the architecture unless a concrete contradiction in this specification is identified.

### 31.2 Addon separation must not reduce MetaBlock capability

The addon boundary is a **packaging and ownership boundary only**. It is not a capability sandbox and it must not force a MetaBlock to become a weak widget plugin. Moving PyTorch out of Graphimo Core must preserve the complete PyTorch MetaBlock experience defined in this specification.

The following rule is normative:

> If a capability is valid for the PyTorch MetaBlock when implemented in-tree, making PyTorch an installable addon must not remove that capability merely because it now lives outside Core.

Graphimo Core owns only the generic host mechanisms needed by semantic scopes and addons. The PyTorch addon owns all PyTorch-specific behavior. The boundary must therefore permit the PyTorch addon to implement, at minimum, all of the following without adding PyTorch-specific code to Core:

- a full nested Graphimo canvas for PyTorch architecture editing;
- specialized internal element kinds such as `Linear`, `Embedding`, `RMSNorm`, activations, shape transforms, residual operators, attention operations, losses and custom PyTorch operations;
- normal Group Blocks inside a PyTorch MetaBlock, including reusable/repeated groups such as `LlamaDecoderLayer x 32`;
- nested MetaBlocks where required by the model representation;
- generation of real readable PyTorch code and real runnable `nn.Module` objects;
- shape/type propagation through the architecture;
- editable layer/module parameters and model hyperparameters;
- stable mapping from visual element identity to generated/runtime PyTorch sites;
- live forward outputs, activations, gradients, bounded tensor statistics, shape, dtype and device information;
- live training metrics such as loss curves, gradient norms, VRAM/resource metrics and plugin-defined measurements;
- selection of an internal layer while training/inference happens in an external normal block or Group Block;
- on-demand TorchLens inspection, including deeper operation traces, gradients, causal/intervention workflows and other TorchLens capabilities the addon chooses to expose;
- checkpoints, persisted model artifacts and addon-specific serialization;
- future architecture editing, weight migration or model-analysis features without requiring PyTorch semantics in Core;
- an addon-local extension mechanism in the future, if the PyTorch addon itself needs optional inspectors or model-domain extensions.

Graphimo Core must **not** implement these features directly. It must provide generic mechanisms that are sufficient for an addon to own them. In particular, Core must not contain concepts such as `Linear`, `Tensor`, `nn.Module`, `TorchLens`, activation, gradient, optimizer or checkpoint semantics.

The correct dependency direction is:

```text
Graphimo Core
    provides generic graph scopes, nesting, canvas integration, project state,
    addon discovery, generic event routing, artifact access and host/runtime seams

        ^
        | generic addon/semantic-scope contract
        |

graphimo-pytorch
    provides PyTorch element semantics, compiler, runtime bindings, hooks,
    telemetry interpretation, model editor behavior, TorchLens integration,
    checkpoint/model logic and PyTorch-specific nested behavior
```

The addon is therefore allowed to be large and highly domain-specific. `graphimo-pytorch` may contain substantial internal subsystems such as:

```text
graphimo_pytorch/
  graph/
  elements/
  compiler/
  runtime/
  bindings/
  telemetry/
  torchlens/
  checkpoints/
  shape_inference/
  source_export/
  extensions/
```

This does **not** make Graphimo PyTorch-specific because those packages are outside Core and can be absent entirely.

The generic MetaBlock/addon API must therefore be judged by one criterion: **can an addon implement its full domain-specific semantic scope without requiring domain-specific branches in Core?** Do not simplify the PyTorch MetaBlock merely to fit an artificially small plugin API. If an addon needs an additional generic host capability, add that capability only when it is genuinely domain-agnostic and place the PyTorch-specific interpretation in the addon.

## 32. PyTorch MetaBlock execution boundary

The PyTorch model MetaBlock represents/produces a model definition. It is not itself the training loop.

A model MetaBlock may compile to readable Python similar to:

```python
import torch
import torch.nn as nn
import torch.nn.functional as F


def apply_rope(...):
    ...


class LlamaDecoderLayer(nn.Module):
    ...


class LlamaForCausalLM(nn.Module):
    ...
```

The generated definition is made available to normal Graphimo code as a Python symbol/factory/class. A normal block or Group Block outside the MetaBlock then performs work such as:

```python
g_train_4f2a = train(
    model=g_model_a13f,
    dataset=g_dataset_812c,
)
```

or, if the final contract chooses a class/factory rather than an instance:

```python
g_train_4f2a = train(
    model_factory=LlamaForCausalLM,
    dataset=g_dataset_812c,
)
```

**HUMAN DECISION REQUIRED:** whether the external PyTorch-model port carries an instantiated `nn.Module`, a model class/factory, or a small explicit model-definition handle. Do not hide this choice behind an arbitrary wrapper without discussing it.

Evaluating the marimo cell that defines the class/factory is allowed and expected. That is definition/materialization, not model training or inference.

## 33. Live information flows back into the MetaBlock

Training/inference may happen outside the model MetaBlock, while the user still needs to inspect the model MetaBlock live.

Therefore the architecture must support this direction:

1. the PyTorch MetaBlock produces the model definition;
2. an external normal block or Group Block consumes that model for training/inference;
3. that external execution emits loss, gradient, activation, profiler and TorchLens information;
4. Graphimo routes those observations back to the originating PyTorch MetaBlock and its internal elements.

Graphimo must be able to associate telemetry emitted during the external run with the MetaBlock instance that supplied the model. The exact transport is not fixed yet.

At minimum, the eventual event model must be capable of identifying:

- the Graphimo run producing the event;
- the originating MetaBlock/model relationship;
- a component/module path inside the model when applicable;
- event kind and payload;
- step/time when applicable.

Do not require the MetaBlock itself to own the training loop merely to receive these events.

---

# MILESTONE 9 - PyTorch MetaBlock reference implementation

## 34. Minimum required behavior

The first PyTorch addon is the reference implementation of the MetaBlock addon contract. It must implement the required semantic-scope behavior on a real architecture, not a toy widget-only demo. It is not part of Graphimo Core.

The target visual language should be capable of representing structures like the provided Torch Canvas examples:

- `Embedding`, `Linear`, `RMSNorm`, activations and loss elements;
- tensor/shape transformations such as split-heads and merge-heads;
- matrix operations, residual additions and causal masking;
- reusable nested groups such as `LlamaDecoderLayer x 32`;
- opening a group to inspect its internal structure;
- connections carrying shape/type information;
- a complete model represented as a PyTorch MetaBlock while remaining inside Graphimo.

Required behaviors:

1. a PyTorch MetaBlock rendered using Graphimo's own canvas infrastructure;
2. at least one Group Block inside the PyTorch MetaBlock;
3. nested MetaBlock support at the data-model level, even if the first demo uses only one PyTorch dialect;
4. generation of real readable PyTorch source representing the architecture;
5. import of a supported PyTorch source subset back into the internal model graph, or an explicit diagnostic for unsupported constructs;
6. an external normal `@node`/Group Block consuming the model definition and running training or inference;
7. live loss information from that external execution;
8. at least one activation or gradient observation mapped back to the corresponding internal model element;
9. on-demand TorchLens inspection of a selected internal module/layer;
10. checkpoint persistence via the native ArtifactStore when explicitly requested.

TorchLens is an implementation detail of the PyTorch extension. Graphimo core must not import TorchLens.

Do not create a second PyTorch-specific scheduler. Normal execution stays in marimo/normal Graphimo blocks.

The PyTorch extension may later expose its own extension/plugin mechanism. That remains outside Graphimo Core until it is separately specified.

---

# 35. Things Codex must NOT implement unless explicitly requested

Do not implement any of the following while following this document:

- custom Python scheduler;
- custom reactive DAG runtime;
- `WorkspaceGraph` as runtime authority;
- `LocalPythonWorker` as normal execution path;
- Kishu/snapshot-kernel style namespace persistence;
- Ray/distributed execution;
- multiple transparent Python environments;
- remote runtimes/SSH;
- MLflow/W&B/DVC as mandatory core dependencies;
- automatic persistence of arbitrary Python results;
- Dataset MetaBlock;
- native PyTorch MetaBlock in core;
- content-defined chunking/FastCDC in ArtifactStore;
- MetaBlocks implemented as unrelated mini-apps/webviews with their own graph/runtime model;
- arbitrary live architecture surgery in the middle of a backward pass;
- Group Block execution backend without the required human decision.

---

# 36. Required commit sequence

Codex should prefer small commits in this order:

1. `core: add transparent @node contract and output inspection`
2. `core: add canonical graphimo instance source generation`
3. `adapter: project @node definitions and generated call instances`
4. `application: expose host-independent project/node/instance operations`
5. `dev-workbench: exercise Graphimo functionality without Zeron`
6. `core: support disabling generated instances as comments`
7. `artifacts: expose native streaming content-addressed store`
8. after the Group Block backend decision: `groups: add reusable imported workflow blocks`
9. after MetaBlock blocking decisions are resolved: `metablock: implement nested semantic scopes and addon discovery`
10. `pytorch-metablock: implement the separately installable PyTorch dialect, compiler, runtime bindings, telemetry and TorchLens inspection`
11. only after the functional path is stable: `zeron-adapter: bind the existing Zeron plugin UI/bridge to the host-independent application/event interfaces`

Do not squash unrelated migration work into one giant commit.

---

# 37. Definition of V1 core success before PyTorch work

The V1 Graphimo Core implementation is complete when all of the following are true:

- the project remains an ordinary marimo Python file;
- a single `@node` function can be called by multiple independent block instances;
- each instance has exactly one generated global result symbol `g_<node>_<id>`;
- connections are represented only by normal Python call arguments;
- reconnecting a link changes only the target generated instance call;
- marimo remains the only execution/dependency engine;
- Graphimo statically shows each output declared type and its return expression alternative(s);
- after execution Graphimo shows type + bounded value preview + return expression;
- large values are never fully serialized merely for UI preview;
- disabled instances can be commented out without deleting the node definition;
- native ArtifactStore can persist arbitrary files/directories with streaming hashing and file-level deduplication;
- the product path does not depend on `LocalPythonWorker`, `WorkspaceGraph`, Ray, DVC, Kishu or MLflow.

After those Core requirements are implemented and their conformance tests pass, implement the separately installable PyTorch MetaBlock addon. The addon may be developed in the same monorepo, but Core must remain installable and fully functional without it.

---

# 38. Open decisions - do not let Codex decide these silently

These are the remaining product decisions, not implementation gaps.

### OPEN-A - Exact active return-path tracking

Static semantics are fixed: multiple return paths are supported when arity is stable, and their expressions are collected (for example `float (c | d)`). Final desired runtime UI shows only the expression of the branch that actually returned (for example `float (4.2 | c)`). The implementation mechanism is not fixed yet. Do not add global tracing/profiling overhead to every node without benchmarking it.

### OPEN-G - Declared type versus runtime type mismatch

The declared return annotation is the static port contract. Runtime inspection also knows the actual type. Decide the exact UI for a mismatch such as `-> float` returning an `int`. The implementation must preserve both values in structured metadata and must not silently replace the declared contract with the runtime type.

### OPEN-B - Group Block marimo backend

User semantics are fixed (import + call; reusable by reference; optional vendor/copy later). Exact marimo execution mechanism is not fixed.

### OPEN-C - Telemetry transport

Public API names can be fixed. Whether transport uses marimo outputs, a dedicated marimo primitive or a narrow side channel must be measured on real training.

### OPEN-D - MetaBlock source persistence / round-trip

The PyTorch MetaBlock should expose readable Python, but it is not yet decided whether Python source is the complete source of truth for reconstructing the internal model graph, whether a readable sidecar stores information that cannot be recovered losslessly, or whether a hybrid is required. The sidecar may contain layout, but indispensable scientific semantics must not exist only in transient UI memory.

### OPEN-E - Final canvas renderer

Current Zeron canvas, Zeron Design and an Excalidraw-like renderer remain implementation choices for Graphimo as a whole. A MetaBlock uses the same Graphimo canvas/rendering system; renderer choice is not a per-MetaBlock mini-app choice.

### OPEN-H - PyTorch MetaBlock external value

Decide whether a model MetaBlock exports an instantiated `nn.Module`, a model class/factory, or a small explicit model-definition handle. The choice must make normal Python use readable and make external training/inference blocks straightforward.

### OPEN-I - PyTorch import fidelity

Decide the supported import contract for existing PyTorch code: exact supported subset, fallback to opaque internal elements for unsupported regions, or explicit refusal. Do not claim arbitrary `nn.Module` round-trip until demonstrated.

### OPEN-J - External-run telemetry routing

Training can happen in a normal block/Group Block outside the model MetaBlock. Define the smallest mechanism that lets telemetry identify the model MetaBlock and internal module path that produced an activation/gradient/TorchLens observation, without making the MetaBlock own execution.

### OPEN-F - Automatic artifact persistence

V1 is explicit. Later decide whether specific well-known cases may opt into automatic persistence.


# 39. Candidate concrete implementation for the PyTorch MetaBlock

Status: **normative implementation design for the first PyTorch addon**, subject only to the explicit `HUMAN DECISION REQUIRED` items that remain open elsewhere in this document. Nothing in this section is a Graphimo Core semantic requirement unless it is explicitly stated in Milestone 8. All PyTorch-specific mapping, hooks, compiler behavior and TorchLens integration remain inside the separately installable addon.

## 39.1 Core rule: Graphimo already knows which visual element created which runtime site

Do **not** start by importing arbitrary PyTorch code and then trying to guess which Python operation corresponds to which visual node.

For a PyTorch MetaBlock created/edited inside Graphimo, the MetaBlock graph is created first. Every internal visual element has a stable Graphimo node ID. All PyTorch-specific compilation, binding and inspection code described below lives in the PyTorch addon package, never in Graphimo Core.

Example internal model state:

```text
node_id = n_norm_01   kind = RMSNorm
node_id = n_qproj_02  kind = Linear
node_id = n_rope_03   kind = RoPE
node_id = n_soft_04   kind = Softmax
```

The PyTorch compiler generates the runnable model from this graph and, at the same time, returns a runtime binding map.

Example:

```python
bindings = {
    "n_norm_01": "layers.0.input_layernorm",
    "n_qproj_02": "layers.0.q_proj",
    "n_rope_03": "layers.0.q_rope",
    "n_soft_04": "layers.0.attn_softmax",
}
```

This binding map is the answer to the question "how does Graphimo know which graphical layer produced this tensor?". It does not infer this after the fact. The compiler created both the module and the mapping.

## 39.2 V1 requirement: every inspectable visual operation must have an addressable runtime site

For native `nn.Module` operations this is immediate:

```python
self.q_proj = nn.Linear(d, d, bias=False)
```

The visual node can bind directly to `q_proj` (or its full nested module path).

For functional operations that are visually represented as their own nodes, V1 should compile them to tiny inspectable modules rather than leaving them as anonymous expressions.

Example:

```python
class SoftmaxOp(nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim

    def forward(self, x):
        return torch.softmax(x, dim=self.dim)
```

Then generated runtime code can contain:

```python
self.attn_softmax = SoftmaxOp(dim=-1)
...
att = self.attn_softmax(att)
```

Use the same principle for visual operations such as RoPE, causal mask, explicit matmul nodes, reshape/split-head operations, etc., when they need to be independently inspectable.

This is a V1 implementation convenience, not a requirement that exported pretty Python must forever expose all wrapper classes. A future exporter may lower known wrappers back to idiomatic functional PyTorch. The runtime semantics must still originate from the same MetaBlock graph.

## 39.3 Normal live inspection does not require TorchLens

The default live path is ordinary PyTorch hooks.

Given:

```text
n_qproj_02 -> layers.0.q_proj
```

resolve the module once:

```python
module = model.get_submodule("layers.0.q_proj")
```

and attach a `register_forward_hook` only when the UI has subscribed to that node (or when a lightweight always-on metric is explicitly enabled).

The hook emits bounded metadata such as:

```python
{
    "metablock_id": "model_7ab2",
    "node_id": "n_qproj_02",
    "run_id": "run_...",
    "shape": [8, 64, 4096],
    "dtype": "float16",
    "device": "cuda:0",
    "mean": 0.013,
    "std": 0.82,
    "min": -4.1,
    "max": 4.7,
}
```

Do not stream the complete activation tensor by default.

If the user explicitly asks to inspect the tensor, the plugin may request a bounded sample, histogram, selected channels, image projection, or an explicitly persisted artifact.

Backward/gradient observation follows the same mapping. The plugin attaches a gradient hook only to subscribed sites or uses the corresponding TorchLens deep-inspection path.

## 39.4 TorchLens is the deep-inspection layer, not the basic routing mechanism

The binding map remains authoritative for Graphimo's own nodes.

When the user selects `n_qproj_02`, the PyTorch extension resolves its module address and can ask TorchLens to inspect that module boundary or the region contained inside it.

Conceptually:

```text
Graphimo node n_qproj_02
        -> module address layers.0.q_proj
        -> TorchLens module selector / gradient selector
```

TorchLens is used for tasks such as:

- operation-level trace inside a selected module/group;
- saved activations beyond the lightweight live summary;
- gradients;
- NaN/Inf localization;
- interventions/ablation/patching;
- causal inspection;
- comparing internal behavior between runs/checkpoints.

Do not run a full TorchLens trace on every training batch merely to display normal layer outputs.

## 39.5 Group Blocks and repeated modules

A Group Block inside a PyTorch MetaBlock does not need to be a single runtime operation. It is a namespace/structural scope containing child nodes.

For a repeated `LlamaDecoderLayer x 32`, the template visual nodes can have stable logical IDs and each runtime repetition has an instance-qualified path.

Example:

```text
logical node: q_proj
runtime instance 0: layers.0.q_proj
runtime instance 1: layers.1.q_proj
...
runtime instance 31: layers.31.q_proj
```

The UI may display the repeated group collapsed as `LlamaDecoderLayer x 32`, then allow the user to choose an instance when inspecting live data.

The exact UX for selecting one or many repeated instances remains open, but the runtime mapping is not ambiguous.

## 39.6 Minimal runtime object produced by the PyTorch compiler

Do not create a large generic MetaBlock framework first. The first PyTorch implementation may use a concrete internal result such as:

```python
@dataclass(frozen=True)
class CompiledModelDefinition:
    factory: Callable[[], nn.Module]
    bindings: Mapping[str, str]
    graph_revision: str
    metablock_id: str
```

The exact public type is not fixed yet. The important invariant is that model construction and node bindings are produced together from the same MetaBlock graph revision.

An external normal Graphimo block/Group Block may then instantiate/train/run the model. The model MetaBlock itself does not own the training scheduler.

## 39.7 External training and routing back into the MetaBlock

If training happens outside the model MetaBlock, the execution still carries the model-definition identity and binding map.

Conceptually:

```text
PyTorch MetaBlock graph
    -> CompiledModelDefinition(meta_id + bindings + factory)
    -> normal train(...) block
    -> instantiated nn.Module
    -> hooks / TorchLens observations
    -> events tagged with meta_id + node_id
    -> Graphimo routes the event back to the correct internal visual node
```

The MetaBlock does not need to own execution. It only needs stable identity and stable node-to-runtime bindings.

## 39.8 Code shown to the user in V1

For V1, prefer **visual graph -> generated PyTorch** as the reliable direction.

The code pane may show/export readable PyTorch generated from the MetaBlock. Do not require arbitrary edited PyTorch source to round-trip perfectly back into the graph in the first implementation.

Importing arbitrary existing PyTorch is a separate feature. Torch Canvas demonstrates that diagram-to-code and code-to-diagram are achievable for a useful subset, but Graphimo V1 must not make this difficult reverse problem a prerequisite for basic model editing and inspection.

If unsupported imported code is later accepted, unsupported regions may need an opaque fallback rather than pretending to understand them.

## 39.9 Conformance test for the first PyTorch MetaBlock implementation

Build a tiny model MetaBlock containing:

```text
Input -> Linear A -> ReLU -> Linear B -> Output
```

Required assertions:

1. every internal visual node has a stable node ID;
2. compiler generates a real runnable `nn.Module`;
3. compiler returns bindings for Linear A, ReLU and Linear B;
4. a normal external Graphimo node trains the model for a few steps;
5. selecting Linear A during the run yields its live output shape and bounded statistics;
6. selecting Linear B yields different correctly routed statistics;
7. selecting ReLU also works, proving inspectable functional operations;
8. a gradient observation can be routed back to one selected node;
9. an on-demand TorchLens inspection targeted from one selected Graphimo node succeeds;
10. no full TorchLens trace runs continuously when nothing is selected;
11. after these assertions pass, repeat with one nested/repeated Group Block before attempting Llama-scale models.

These assertions are requirements of the implementation. A failure is an implementation defect unless it exposes a concrete contradiction in this specification.

## 39.10 Explicitly deferred problems

Do not solve these in the first PyTorch MetaBlock implementation:

- arbitrary PyTorch source -> perfect Graphimo graph reconstruction;
- arbitrary Python control flow visualization;
- perfect pretty-code round-trip;
- every torch functional/operator as a first-class visual node;
- distributed training;
- architecture mutation in the middle of backward;
- a universal MetaBlock API for unrelated domains;
- automatic inspection of every layer on every batch.



---

# 40. Readiness before asking Codex to implement everything

This section is normative and supersedes any earlier sequencing implication that production Zeron integration must be completed before the core, Group Block, MetaBlock, or PyTorch functional contracts can be exercised.

## 40.1 Separate functional behavior from host rendering

The new Graphimo functional path must be testable without the Zeron plugin UI. Before production Zeron integration, add a **standalone development workbench** (name may change) whose only purpose is to exercise the same host-independent Graphimo application/controller layer that the future Zeron adapter will use.

The workbench is not a second Graphimo product and must contain no business semantics. It may use a simple local browser UI or another disposable development surface. Graphimo core/application code must never import the workbench. The workbench must not become the source of truth for project state.

Required architectural direction:

```text
Graphimo functional/application layer
    - node discovery / instances / wiring
    - marimo session operations
    - Group/MetaBlock state once specified
    - ArtifactStore
    - addon discovery
    - generic events

        ^                         ^
        |                         |
standalone dev workbench     Zeron adapter (later)
```

The workbench must eventually let a developer manually exercise at least: open/create project, discover `@node` definitions, add repeated instances, connect/reconnect ports, run/inspect through marimo, disable/enable, inspect ArtifactStore state, enter/leave nested scopes, create an installed MetaBlock dialect, edit a PyTorch MetaBlock, view generated code, and observe live plugin telemetry.

Production Zeron UI/bridge integration is a later adapter milestone. Do not place business logic in Luau merely because the final host is Zeron.

## 40.2 Current PyTorch documentation is not yet sufficient for a full automatic handoff

Sections 31-39 correctly define the addon boundary, required capabilities, node-to-runtime binding concept, lightweight hooks, TorchLens role, and the desired model-editor behavior. Section 41 now fixes the generic graphical-development experience that Core and addons must preserve. The PyTorch-specific scientific/compilation contract is **still not yet a complete coding contract** for the whole addon. Before `code everything`, add a dedicated PyTorch addon implementation specification that fixes at least the following:

1. persisted PyTorch MetaBlock graph schema and schema versioning: element IDs, kinds, parameters, ports, edges, nested groups, repeated groups and addon-owned state;
2. the exact V1 element catalog and each element's inputs, outputs, editable parameters and compile semantics; the provided Llama/Torch-Canvas example should be a canonical acceptance fixture, while `Input -> Linear -> ReLU -> Linear -> Output` remains the minimal conformance fixture;
3. shape/type propagation rules and diagnostics for unknown or incompatible shapes;
4. exact compiler result structure: generated readable source, callable/factory or model-definition value, runtime binding map, graph revision and MetaBlock identity;
5. exact persistence location for scientific MetaBlock state and generated Python, including what remains usable if the addon or sidecar is temporarily unavailable;
6. exact generic telemetry event envelope and subscription lifecycle for forward outputs, gradients, loss/resource metrics and selected-node inspection;
7. exact routing identity for an external training/inference block to send observations back to the originating model MetaBlock and internal node;
8. exact TorchLens integration contract: on-demand targeting, capture limits, lifecycle, error handling and what is persisted versus ephemeral;
9. exact error/diagnostic codes for unsupported element, invalid connection, shape mismatch, compile failure, missing addon/runtime binding and unavailable inspection;
10. V1 import policy for existing PyTorch source. If reverse import is deferred, say so explicitly rather than requiring Codex to invent a supported subset.

Until these are fixed, Codex may implement earlier independent milestones but must not invent the missing PyTorch contracts.

## 40.3 Blocking versus deferrable open decisions

Before a full end-to-end implementation request, the following are blocking and must be fixed in this specification: `OPEN-B` Group Block backend, `OPEN-D` MetaBlock persistence/source-of-truth, `OPEN-H` PyTorch external model value, and `OPEN-J` external-run telemetry routing. `OPEN-C` telemetry transport must at least be fixed at the host-independent API boundary; its Zeron transport adapter may remain deferred. `OPEN-I` PyTorch import fidelity can be made non-blocking by explicitly deferring arbitrary source-to-graph import from V1.

The following do not need to block initial full functional implementation if their V1 fallback is written explicitly: `OPEN-A` active return-path instrumentation may remain static alternatives only; `OPEN-G` declared/runtime type mismatch may expose both structured values without a polished final warning UI; `OPEN-E` final production renderer may remain undecided because the development workbench is not the production renderer; and `OPEN-F` automatic artifact persistence remains deferred because V1 persistence is already explicit.

## 40.4 Zeron sequencing rule

The existing Zeron plugin must remain available as reference/legacy UI during migration, but new functionality must not depend on it. Do not require the new core/addon implementation to be wired into Zeron merely to test it. Once the standalone workbench and functional contracts are stable, implement the Zeron adapter against the same application/controller/event interfaces and remove duplicated host-specific business logic.


---

# 41. Graphical development experience - normative product contract

This section is normative. Graphimo is not only a Python/marimo execution layer with a canvas attached. Its primary product purpose is to make experimentation, composition and inspection **faster through direct graphical manipulation**. A functionally correct implementation that forces the user back into source editing for ordinary composition, parameter tuning, navigation or inspection is incomplete.

The standalone development workbench and the later Zeron adapter must expose the same application semantics. Styling may differ during development, but the interaction model described below must be represented in the host-independent application layer instead of being invented independently by each renderer.

## 41.1 Scope-aware component library

The component library is contextual. Entering a scope changes which components are offered. Do not display every installed component everywhere.

### Normal Graphimo Python scope

The normal/root scope may show categories such as:

- **Nodes**: reusable user/project `@node` definitions;
- **Groups**: reusable Group Blocks available to the project;
- **MetaBlocks / Addons**: entry points for installed MetaBlock dialects such as PyTorch;
- **Controls**: reactive interactive values such as sliders, numeric inputs, selects and toggles;
- **Display / utility elements** where explicitly supported by the host/application contract;
- **Annotations / organization** such as notes or visual grouping when implemented.

PyTorch-internal architecture elements such as `Linear`, `RMSNorm`, `RoPE`, `MatMul`, `Softmax` or a `LlamaDecoderLayer` template **must not pollute the normal Graphimo palette**.

### Group Block scope

A Group Block does not change semantic dialect. It inherits the same component catalog/dialect as its parent scope, subject only to ordinary project/import visibility.

### MetaBlock scope

Entering a MetaBlock switches the component library to the active addon dialect. The addon supplies the specialized catalog for that scope.

For a PyTorch MetaBlock this may include categories such as:

- **Inputs / Outputs**;
- **Layers**: `Linear`, `Embedding`, `Conv*`, `RMSNorm`, normalization/activation elements supported by the V1 addon;
- **Tensor operations**: reshape, split/merge heads, transpose, flatten/unflatten, concatenate where supported;
- **Math / attention operations**: add, multiply, matmul, softmax, causal mask, RoPE, etc.;
- **Losses / heads**: cross-entropy and other explicitly supported elements;
- **Reusable Groups / Templates**: e.g. `Attention`, `MLP`, `LlamaDecoderLayer`, complete architecture templates such as a Llama-style model where shipped by the addon;
- **Compatible nested MetaBlocks** when the current dialect allows them.

The exact PyTorch V1 catalog belongs in the dedicated PyTorch addon specification. The generic requirement is that **addon-only components are visible only inside appropriate MetaBlock scopes** unless they are explicitly exposed as a top-level MetaBlock entry.

### Navigation and palette behavior

Required behavior:

1. entering a Group Block or MetaBlock updates the breadcrumb/current-scope indicator;
2. the component library refreshes immediately for the active scope;
3. leaving the scope restores the parent's catalog;
4. search operates on the current visible catalog, not all hidden addon internals by default;
5. component categories are collapsible/searchable;
6. drag/drop or click-to-insert creates the same underlying element through the application layer;
7. uninstalling an addon removes its creatable entries but preserves existing unavailable MetaBlock state as already specified;
8. repeated/group/template items create references/instances according to their own semantic contract rather than blindly copying source.

The user must always be able to tell **which scope they are editing** and why a component is or is not available there.

## 41.2 Templates are first-class catalog entries, not hard-coded Core concepts

An addon may expose higher-level templates or reusable architecture fragments in its component library.

For example `graphimo-pytorch` may expose:

```text
PyTorch MetaBlock palette
  Templates
    Attention
    MLP
    LlamaDecoderLayer
    Llama-style causal LM
```

Selecting `LlamaDecoderLayer` may create a nested Group Block/subgraph containing the required PyTorch elements. Selecting a complete Llama-style model may create a larger prewired architecture similar to the Torch Canvas examples.

Graphimo Core does not know what a Llama model is. It only hosts the catalog item and resulting graph elements supplied by the addon.

User-created reusable groups/templates inside a dialect should eventually be able to appear in the same contextual catalog, subject to the Group Block persistence/import rules.

## 41.3 Interactive Control Blocks are a first-class Graphimo feature

Graphimo must support visual controls whose value is a normal reactive Python value and can be connected to block inputs. Controls are not MetaBlocks and do not require a custom scheduler.

In normal Python/marimo scopes, V1 controls should be implemented on top of marimo's native reactive UI elements whenever an equivalent exists. A control is stored as a Graphimo-owned marimo cell/global so marimo can propagate value changes through its dependency graph.

Baseline V1 control set:

- Slider;
- Range Slider;
- Number input;
- Dropdown / Select;
- Checkbox or Switch;
- Text input;
- Button / Run trigger where useful.

Additional marimo-native controls may be exposed later without changing the control model.

### Canonical slider semantics

Conceptually, a Graphimo slider instance corresponds to a marimo global UI object:

```python
# Graphimo control: slider
# Graphimo instance: a81f2c40
g_slider_a81f2c40 = mo.ui.slider(
    start=0.0,
    stop=1.0,
    step=0.01,
    value=0.2,
    debounce=False,
    label="threshold",
)
```

The Graphimo card renders the slider itself. Its output value is connected downstream using the marimo UI value:

```python
g_thresholded_82af9d10 = threshold(
    x=g_data_99a1c003,
    threshold=g_slider_a81f2c40.value,
)
```

The user should never need to type `.value` manually for a normal Graphimo connection.

### Reactive behavior

When the user moves the control:

1. the control value changes;
2. marimo updates the global UI element value;
3. dependent cells are re-evaluated according to the active marimo execution mode;
4. affected Graphimo cards visibly enter running/stale/updated states;
5. their output previews/rich outputs update;
6. the canvas therefore shows the consequence of parameter changes directly, like an interactive marimo demonstration.

This behavior must not be reimplemented with a Graphimo polling loop.

### Live versus commit/debounced controls

Rapid controls such as sliders must expose an execution policy that avoids forcing expensive downstream computation on every mouse movement when the user does not want it.

At minimum support:

- **Live**: update while dragging (`debounce=False` where the marimo control supports it);
- **On release / debounced**: send/commit when the interaction finishes (`debounce=True` where supported).

The UI must make this mode visible/configurable for controls where it matters. The default may be chosen per control type, but Graphimo must not silently force a long model-training path to restart on every slider pixel if the control has been configured for commit/debounced behavior.

For expensive downstream nodes under marimo lazy execution, changing a control may mark descendants stale until the user runs the desired target. Graphimo must display this state rather than pretending the change has already propagated.

## 41.4 Controls inside/around MetaBlocks

MetaBlock addons may expose parameters that can be driven by ordinary Graphimo controls when the parameter is part of the MetaBlock's external contract.

Example:

```text
Slider learning_rate ---> Train Group Block
Number n_heads -------> PyTorch model MetaBlock parameter
```

The addon decides what a parameter means. Graphimo only provides the value/connection mechanism.

For a PyTorch MetaBlock, distinguish conceptually between:

- **runtime/non-structural parameters** that can safely affect external execution without rebuilding the architecture (for example an optimizer learning rate when owned by a training block);
- **structural architecture parameters** such as hidden width or number of heads, which may require recompiling/materializing a new model definition and must not imply arbitrary in-place surgery during an active backward pass.

The dedicated PyTorch addon specification must mark the behavior of each parameter. Graphimo Core must not contain PyTorch-specific parameter categories.

## 41.5 Rich output and inspection experience

Graphimo is intended for rapid visual development, so an output should be inspectable without forcing the user to open source code or raw logs.

Every normal block card must support a compact output summary. Depending on value/type this may include:

- scalar value;
- short text;
- type and shape;
- table dimensions/sample;
- image thumbnail;
- plot/chart preview when the marimo output provides one;
- progress/status;
- structured error state.

Large outputs remain bounded as specified by `preview_value`; opening a richer viewer may request more data through the application layer. Never serialize a full huge tensor/dataframe only to paint a card.

A card with multiple outputs must make each port independently inspectable.

The host should provide an expandable detail surface for a selected block/output so the user can see richer marimo outputs, errors, images, tables or plots without permanently enlarging every card.

MetaBlock addons may add domain-specific inspectors to their internal elements through the generic addon/application seam. For PyTorch this includes layer shape, activation statistics, gradients and TorchLens views. The same principle applies: compact information on the canvas, deeper information on selection/inspection.

## 41.6 Required execution-state feedback

The canvas must clearly distinguish at least these states where applicable:

- idle / never run;
- running;
- completed/up-to-date;
- stale because an upstream dependency changed;
- error;
- disabled;
- unavailable because an addon is missing;
- waiting for an explicitly lazy/manual run where marimo reports such state.

A running block should be able to expose progress or the latest bounded live metric without opening a terminal. Errors must be visible on the block and expandable to diagnostics/source context.

State must not be represented only by color. Text/icon/badge/shape differences must make the state understandable with different themes and for accessibility.

## 41.7 Minimum canvas interaction contract

The development workbench must implement these interactions before it is considered an adequate manual test surface. The later Zeron renderer must preserve them unless an explicitly better host-native interaction is substituted without changing semantics.

Required:

- pan and zoom;
- fit graph / focus selection;
- select one block;
- multi-select blocks;
- drag/move selection;
- create connection by dragging output -> compatible input;
- reconnect an input without editing source;
- disconnect an edge;
- select/delete a block through an explicit action;
- duplicate an instance where semantically valid;
- disable/enable normal executable instances;
- create Group Block from a selection once Group Blocks are implemented;
- open/enter Group Block;
- open/enter MetaBlock;
- navigate back through breadcrumbs;
- search/add a component from the contextual palette;
- open block source for editable `@node` definitions;
- inspect a block/output;
- expose errors without requiring the user to read raw process logs;
- preserve layout in `.graphimo.json` or the later normative project-layout store.

Undo/redo is a required product capability before Graphimo is considered ready for serious daily use, but its exact source-edit transaction implementation may follow the initial functional milestones. When implemented it must undo semantic operations (insert/connect/reconnect/delete/move/group) coherently rather than only canvas pixel movement.

## 41.8 Card anatomy - minimum information hierarchy

A normal executable block card should have a consistent conceptual anatomy:

```text
[ status ] NodeName                         [ actions ]

input_a   o                                 o output A
input_b   o      compact body/controls      o output B

runtime/output summary when useful
```

Exact styling is renderer-specific. Semantics are not.

Required information:

- display name/function name;
- execution state;
- input ports and editable literal/default values where supported;
- output ports with declared type/return expression/runtime preview as already specified;
- compact error/progress information;
- clear affordance to open source/inspector where applicable.

For a Control Block, the control itself is the body of the card.

For a MetaBlock, the outer card shows its external ports and enough summary to identify the addon/dialect/model; entering it reveals the internal specialized graph.

## 41.9 Graphical editing must remain source-faithful

Graphical convenience must not create a hidden second program.

For normal Python scopes:

- connecting/reconnecting edits the Graphimo-owned function-call cell;
- literal parameter editing edits the canonical call source;
- disabling comments out the Graphimo-owned call as specified;
- a Control Block is represented by a Graphimo-owned marimo UI cell;
- marimo remains authoritative for execution/dependencies.

For MetaBlocks, the addon-owned persisted graph/source contract will be fixed in the dedicated addon specification. The canvas must not keep indispensable scientific semantics only in transient renderer state.

## 41.10 Component/catalog host contract - internal V1 shape

Do not hard-code root and PyTorch palette contents in renderer code. The host-independent application layer should expose a scope-resolved catalog model.

A V1 internal model may be equivalent to:

```python
@dataclass(frozen=True)
class PaletteItem:
    id: str
    title: str
    category: str
    kind: str
    description: str | None
    search_terms: tuple[str, ...]
    source_provider: str

@dataclass(frozen=True)
class ScopePalette:
    scope_id: str
    dialect_id: str
    items: tuple[PaletteItem, ...]
```

The renderer consumes this model and sends back an item ID + placement intent. The application/addon layer performs the actual creation.

`kind` is an internal routing/display hint, not a universal semantic taxonomy. Addons remain free to own the created element's domain-specific state/behavior.

The important invariants are:

- palette visibility is resolved by active scope/dialect;
- renderer code does not know PyTorch component lists;
- addon components can appear/disappear with addon installation;
- templates/groups can be presented alongside primitive elements;
- search/category behavior is generic.

## 41.11 Workbench UX conformance scenario

Before Zeron integration, the standalone workbench must allow a human to perform the following end-to-end scenario without editing generated wiring source manually:

1. open a Graphimo/marimo project;
2. see `@node` definitions in the normal component library;
3. add one node twice;
4. connect both instances to upstream data;
5. add a Slider Control Block and connect its value to one numeric input;
6. move the slider and see the affected downstream result change/react or become visibly stale according to execution mode;
7. inspect the updated output value/plot/table/image if provided;
8. group selected normal blocks once Group Blocks are available and enter/exit the group;
9. install/use the PyTorch addon and add a PyTorch MetaBlock;
10. enter the MetaBlock and observe that the palette changes to PyTorch-only/internal architecture components plus permitted structural items;
11. create/open a nested architecture Group/Template such as the later Llama fixture;
12. return to the parent scope and observe that PyTorch-internal components disappear from the normal palette;
13. run an external training/inference block and inspect live information routed to the PyTorch MetaBlock when that milestone exists;
14. close/reopen the project and preserve graph/layout/control values according to the relevant persistence contracts.

This scenario is a manual/product conformance scenario. Automated tests should cover the underlying application operations independently of rendering.

## 41.12 Production-quality UX is part of V1 acceptance, not an optional postscript

The early workbench may be visually plain, but Graphimo V1 is not complete merely because unit tests and headless marimo execution work. Before calling the product usable, the chosen production renderer must provide a coherent, low-friction implementation of the behaviors above.

The purpose of Graphimo is rapid graphical development. Therefore component discoverability, direct manipulation, interactive controls, immediate feedback, nested-scope navigation and readable live outputs are product requirements, not cosmetic extras.
