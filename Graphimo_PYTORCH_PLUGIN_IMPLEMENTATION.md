# Graphimo PyTorch Addon - Implementation Contract for Codex

Status: **normative implementation contract** for the separately installable `graphimo-pytorch` addon.  
Companion documents: `Graphimo_CODEX_IMPLEMENTATION.md` and `Graphimo_GRAPHICAL_UX_IMPLEMENTATION.md`.  
Target repository during initial development: `SorbetUP/graphimo`, with the addon kept as an independently installable package inside the monorepo.  
Primary product goal: provide a Torch-Canvas-like model architecture editor **inside Graphimo**, while preserving normal Graphimo/marimo execution outside the MetaBlock and adding live model inspection, telemetry, checkpointing, and TorchLens.

---

# 0. Implementation posture - normative

This document is not a feasibility study. The mechanisms specified here are the implementation contract for the first PyTorch addon. Codex must implement them as written. Tests are conformance/regression checks, not architecture-selection experiments.

The addon boundary is a packaging and responsibility boundary only. It must **not** reduce the capabilities of the PyTorch MetaBlock compared with an in-tree implementation.

The following invariants are mandatory:

- Graphimo Core contains no `torch`, `torchlens`, `nn.Module`, optimizer, gradient, activation, checkpoint, layer, tensor-shape, or PyTorch-specific branches.
- `graphimo-pytorch` is a separate Python distribution discovered through the Graphimo MetaBlock entry-point mechanism.
- The PyTorch MetaBlock is still rendered and navigated as Graphimo. It is not an iframe, webview, foreign canvas, or second application.
- The MetaBlock owns a specialized **PyTorch semantic graph**. Normal Graphimo `@node` cells continue to be executed by marimo.
- The MetaBlock defines/materializes a model. It does not become a second training scheduler.
- Training/inference can happen in an ordinary external Graphimo node or Group Block.
- Live activations/gradients/TorchLens results must route back to the originating MetaBlock and internal element.
- The reliable V1 authoring direction is **visual PyTorch graph -> generated runnable PyTorch**. Arbitrary Python/PyTorch -> perfect graph reconstruction is explicitly not a V1 prerequisite.
- The addon must be usable without Zeron. The standalone Graphimo Workbench is the primary development/test host. Zeron is a later renderer/host adapter.

If an implementation choice below conflicts with a generic Graphimo contract, the generic contract wins unless this document explicitly states that the PyTorch dialect specializes that behavior.

---

# 1. User mental model

A PyTorch MetaBlock is a model-definition scope inside Graphimo.

From the parent Graphimo scope it behaves like one component:

```text
normal Graphimo scope

Dataset ---> Train Group Block ---> Checkpoint / Metrics
                 ^
                 |
          PyTorch MetaBlock
          "Llama model"
```

Entering the MetaBlock opens a specialized Graphimo scope:

```text
input_ids
    |
Embedding
    |
LlamaDecoderLayer x 32
    |
RMSNorm
    |
Linear
    |
logits
```

Opening the repeated `LlamaDecoderLayer` Group Block exposes its internals:

```text
RMSNorm
  |\
  | Q Linear -> split heads -> RoPE ------\
  | K Linear -> split heads -> RoPE -> T --@-> mask -> softmax --\
  | V Linear -> split heads ------------------------------------ @ -> merge heads -> O Linear
  |                                                                    |
  +--------------------------------------------------------------------+ residual
                                                                       |
                                                                    RMSNorm
                                                                    /      \
                                                               Gate Linear  Up Linear
                                                                    |         |
                                                                   SiLU       |
                                                                      \       /
                                                                       multiply
                                                                          |
                                                                     Down Linear
                                                                          |
                                                                    residual add
```

The user does not need to understand generated module paths, hook handles, tuple indices, or runtime registries to use this workflow.

---

# 2. Package boundary and repository structure

Create the addon as a separate installable package:

```text
addons/
  pytorch/
    pyproject.toml
    graphimo_pytorch/
      __init__.py
      registration.py
      version.py
      schema.py
      serialization.py
      revision.py
      diagnostics.py
      model_definition.py
      catalog.py
      parameters.py
      shapes.py
      graph_validate.py
      generated_source.py
      compiler/
        __init__.py
        compiler.py
        names.py
        topology.py
        codegen.py
        runtime_bindings.py
      elements/
        __init__.py
        base.py
        registry.py
        io.py
        layers.py
        activations.py
        tensor_ops.py
        math_ops.py
        attention.py
        losses.py
        groups.py
      runtime/
        __init__.py
        instances.py
        subscriptions.py
        tensor_summary.py
        hooks.py
        events.py
      torchlens_adapter/
        __init__.py
        inspect.py
        selectors.py
        interventions.py
        export.py
      checkpoints.py
      migration.py
      templates/
        __init__.py
        basic.py
        attention.py
        llama.py
      workbench_projection.py
    tests/
      test_registration.py
      test_schema_roundtrip.py
      test_revision.py
      test_catalog.py
      test_graph_validation.py
      test_shapes.py
      test_compile_basic.py
      test_compile_groups.py
      test_generated_source.py
      test_runtime_bindings.py
      test_live_hooks.py
      test_gradients.py
      test_torchlens.py
      test_checkpoints.py
      test_templates_llama.py
      test_workbench_projection.py
```

Do not place any of these modules under Graphimo Core.

## 2.1 Packaging

`addons/pytorch/pyproject.toml` must define an entry point in the Graphimo MetaBlock group. Use the exact group chosen by the core specification; if the core uses the recommended value, configure:

```toml
[project.entry-points."graphimo.metablocks"]
pytorch = "graphimo_pytorch.registration:registration"
```

The distribution name is `graphimo-pytorch`. The import package is `graphimo_pytorch`.

Recommended V1 dependency floor:

```text
Python >= 3.10
PyTorch >= 2.6
TorchLens == 2.34.1
```

Do not pin a CUDA build in package metadata. Let the user's PyTorch installation/platform resolution choose the appropriate wheel. The addon must work on CPU; CUDA is an optional runtime capability.

TorchLens is Apache-2.0 and currently exposes module-path selectors, activation/gradient capture and interventions. PyTorch module-level forward/backward hooks are the default lightweight live path; TorchLens is the deep-inspection path, not the basic routing mechanism.

---

# 3. Addon registration contract

`graphimo_pytorch.registration:registration` must expose a Graphimo MetaDialect registration object with at least:

```python
registration = MetaDialectRegistration(
    dialect_id="pytorch.model",
    addon_version=__version__,
    api_version=1,
    factory=create_pytorch_dialect,
)
```

The object produced by `create_pytorch_dialect(...)` may use Graphimo's V1 experimental MetaBlock host interface. Its responsibilities are:

- provide the context-sensitive PyTorch palette;
- create/update/delete PyTorch graph elements;
- validate PyTorch internal connections;
- expose external MetaBlock ports;
- serialize/deserialize addon state;
- materialize a `PyTorchModelDefinition` from the persisted graph plus external parameter bindings;
- provide Graphimo-facing summaries/inspectors/actions for PyTorch elements;
- manage live runtime subscriptions for model instances produced by this addon;
- route addon events through the generic Graphimo event seam;
- access Graphimo ArtifactStore only through the host-provided generic artifact API.

The addon must not ask Graphimo Core to understand PyTorch element kinds.

---

# 4. Persistence model - fixed for V1

The V1 PyTorch MetaBlock uses **addon-owned JSON as the canonical semantic graph**. Generated Python is deterministic, readable, and runnable, but is a derived representation in V1.

This is an intentional specialization from normal Graphimo Python scopes. A MetaBlock is allowed to use a different semantic persistence model because its dialect is supplied by an addon.

## 4.1 Project files

For a project notebook:

```text
project.py
.graphimo.json
.graphimo/
  metablocks/
    <metablock_id>.pytorch.json
```

Example:

```text
research.py
.graphimo.json
.graphimo/metablocks/model_7ab2c1f0.pytorch.json
```

`.graphimo.json` remains the general Graphimo presentation/layout store.

The PyTorch JSON file stores **scientific/model semantics**, not node positions or renderer-only state.

## 4.2 What belongs in the PyTorch semantic file

Persist:

- schema version;
- MetaBlock ID;
- display/model name;
- external parameters exposed to the parent Graphimo scope;
- model-level metadata required for deterministic code generation;
- element IDs and kinds;
- element parameters;
- edge topology;
- reusable internal Group definitions;
- Group instances/repetition counts;
- named model entrypoints/method graphs;
- input/output tensor contracts;
- initializer choices;
- explicitly configured inspectability options if they alter generated runtime structure.

Do not persist:

- x/y positions;
- zoom/pan;
- selected element;
- open inspector tab;
- transient tensor values;
- active hook handles;
- runtime model objects;
- live training metrics;
- TorchLens Trace objects unless explicitly exported as an Artifact.

## 4.3 Layout keys

All internal PyTorch visual elements use stable element IDs. General Graphimo layout keys must be scoped by the MetaBlock and element ID, for example conceptually:

```text
scope=model_7ab2c1f0 / element=n_qproj_02
```

Repeated Group instances use the logical element ID plus repetition selection for runtime inspection; the template layout is stored once.

---

# 5. Exact V1 JSON schema model

Implement immutable Python dataclasses and JSON conversion around the following conceptual schema. Field names are normative unless a collision with the existing Graphimo serializer requires a documented compatibility rename.

```python
@dataclass(frozen=True)
class PyTorchMetaBlockState:
    schema_version: int
    metablock_id: str
    name: str
    external_parameters: tuple[ExternalParameter, ...]
    groups: tuple[GroupDefinition, ...]
    entrypoints: tuple[MethodGraph, ...]
    metadata: dict[str, JsonValue]
```

V1 `schema_version` is `1`.

## 5.1 Elements

```python
@dataclass(frozen=True)
class ElementRecord:
    id: str
    kind: str
    name: str
    params: dict[str, JsonValue]
    parameter_bindings: dict[str, ParameterBinding]
    metadata: dict[str, JsonValue]
```

Rules:

- `id` is stable and never derived from canvas position.
- `kind` is an addon-owned registry key such as `linear`, `rms_norm`, `split_heads`.
- `name` is the human/module name and must be a valid generated-Python attribute fragment after sanitization.
- `params` stores literal defaults/configuration.
- `parameter_bindings` stores parameters promoted to external MetaBlock inputs.
- `metadata` may contain addon-specific semantic hints, but must remain JSON-safe.

## 5.2 Ports and edges

Element port identity is string-based inside the addon.

```python
@dataclass(frozen=True)
class Endpoint:
    element_id: str
    port: str

@dataclass(frozen=True)
class EdgeRecord:
    id: str
    source: Endpoint
    target: Endpoint
```

Edges are directed. A target input port may have at most one incoming edge unless an element kind explicitly declares variadic-input semantics.

## 5.3 Method graphs

A model may expose multiple methods/entrypoints.

```python
@dataclass(frozen=True)
class MethodGraph:
    name: str
    inputs: tuple[MethodInput, ...]
    outputs: tuple[MethodOutput, ...]
    elements: tuple[ElementRecord, ...]
    edges: tuple[EdgeRecord, ...]
    group_instances: tuple[GroupInstance, ...]
```

V1 requires a `forward` method graph.

The schema also supports optional `loss` and `predict` method graphs. The shipped Llama template uses all three:

- `forward(input_ids) -> logits`
- `loss(logits, input_ids) -> loss`
- `predict(input_ids) -> probabilities`

This allows the generated source to resemble idiomatic PyTorch rather than forcing all computations into a single `forward()` return tuple.

## 5.4 Groups

```python
@dataclass(frozen=True)
class GroupDefinition:
    id: str
    name: str
    inputs: tuple[GroupPort, ...]
    outputs: tuple[GroupPort, ...]
    elements: tuple[ElementRecord, ...]
    edges: tuple[EdgeRecord, ...]
    group_instances: tuple[GroupInstance, ...]

@dataclass(frozen=True)
class GroupInstance:
    id: str
    group_id: str
    name: str
    repeat: int
    params: dict[str, JsonValue]
```

`repeat >= 1`.

A repeated Group is represented once visually as a reusable template and compiled to repeated runtime instances. `LlamaDecoderLayer x 32` is the canonical acceptance case.

## 5.5 External parameters

```python
@dataclass(frozen=True)
class ExternalParameter:
    id: str
    name: str
    type: str
    default: JsonValue
    structural: bool

@dataclass(frozen=True)
class ParameterBinding:
    external_parameter_id: str
```

Supported external parameter types in V1:

```text
int
float
bool
str
```

An element parameter is either stored literally in `params` or replaced by one external parameter binding.

Structural external parameters, such as `d_model`, `n_heads`, `ff_width`, `vocab_size`, alter model materialization. A changed value produces a different materialization key and makes dependent external execution stale through normal marimo reactivity.

---

# 6. Serialization and revision hashing

Implement:

```python
def load_state(path: Path) -> PyTorchMetaBlockState: ...
def save_state(path: Path, state: PyTorchMetaBlockState) -> None: ...
def canonical_state_json(state: PyTorchMetaBlockState) -> str: ...
def graph_revision(state: PyTorchMetaBlockState) -> str: ...
```

Requirements:

- JSON is UTF-8, deterministic, human-readable, sorted where ordering is semantically irrelevant.
- Scientific list ordering that affects generated code remains stable and intentional.
- writes are atomic (`temp file -> fsync where practical -> replace`);
- unknown schema versions fail with `pytorch_state_schema_unsupported` without modifying the file;
- unknown element kinds preserve raw state but prevent materialization until the addon version supports them;
- `graph_revision` is SHA-256 over canonical semantic JSON, excluding renderer/layout data;
- two semantically identical states produce the same revision;
- moving an element on canvas does not change `graph_revision`;
- changing a layer width, edge, repetition count, initializer, method graph, or external-parameter declaration changes the revision.

---

# 7. Parent Graphimo cell and external model value

This decision is fixed for V1: a PyTorch model MetaBlock exports a **`PyTorchModelDefinition`** object, not a bare class and not an already-instantiated `nn.Module`.

The parent Graphimo-managed marimo cell is conceptually:

```python
# Graphimo metablock: pytorch.model
# Graphimo instance: 7ab2c1f0
g_model_7ab2c1f0 = graphimo_pytorch.materialize_definition(
    project_file=__file__,
    metablock_id="7ab2c1f0",
    revision="<graph_revision>",
    d_model=4096,
    n_heads=32,
)
```

The exact project-path helper may differ if marimo provides a safer notebook path context, but the generated source must contain the MetaBlock ID and revision so a graph edit changes the marimo cell source/dependency state.

## 7.1 `PyTorchModelDefinition`

Implement:

```python
@dataclass(frozen=True)
class PyTorchModelDefinition:
    metablock_id: str
    graph_revision: str
    materialization_key: str
    model_name: str
    generated_source: str
    external_parameters: Mapping[str, JsonScalar]
    bindings: RuntimeBindingMap
    _factory: Callable[[], nn.Module]

    def instantiate(
        self,
        *,
        device: str | torch.device | None = None,
        dtype: torch.dtype | None = None,
    ) -> nn.Module: ...

    def __call__(self, **kwargs) -> nn.Module: ...
```

`__call__` is a convenience alias to `instantiate` with keyword-only options.

Do not serialize `_factory`. The definition is recreated by evaluating the MetaBlock marimo cell.

`generated_source` is the readable current source shown/exported to the user.

`materialization_key` hashes `graph_revision + resolved external parameter values + addon compiler version`.

## 7.2 Why V1 uses a definition handle

The definition object carries the information required to:

- instantiate a fresh model;
- know which MetaBlock produced it;
- know which exact graph revision produced it;
- map logical visual sites to runtime module paths;
- regenerate readable source;
- check checkpoint compatibility;
- route live telemetry back to the originating MetaBlock.

A bare `nn.Module` loses this provenance unless private metadata is reconstructed separately. A bare class cannot carry one materialization's resolved external parameters as cleanly.

---

# 8. Runtime instance metadata

`PyTorchModelDefinition.instantiate()` returns a real `nn.Module` generated by the addon. It also registers lightweight private Graphimo-PyTorch metadata on the model instance.

Use a private attribute namespace unlikely to collide, conceptually:

```python
model.__graphimo_pytorch_runtime__ = RuntimeModelInfo(...)
```

Do not put this metadata in `state_dict()`.

```python
@dataclass
class RuntimeModelInfo:
    runtime_instance_id: str
    metablock_id: str
    graph_revision: str
    materialization_key: str
    bindings: RuntimeBindingMap
```

`runtime_instance_id` is newly generated for every model instantiation.

The addon runtime registry holds **weak references** to active model instances. Graphimo Core does not own this registry.

---

# 9. Runtime binding map

The compiler must produce the model and a binding map in the same compilation operation.

Do not infer Graphimo-to-PyTorch correspondence after execution.

## 9.1 Binding records

```python
@dataclass(frozen=True)
class RuntimeSite:
    logical_element_id: str
    module_path: str
    group_instance_path: tuple[GroupRuntimeIndex, ...]
    inspectable: bool

@dataclass(frozen=True)
class GroupRuntimeIndex:
    group_instance_id: str
    repetition_index: int

@dataclass(frozen=True)
class RuntimeBindingMap:
    sites: tuple[RuntimeSite, ...]
```

Example for a single q projection:

```text
logical_element_id = n_qproj_02
module_path        = layers.0.q_proj
```

For a repeated group:

```text
logical element q_proj
  repetition 0  -> layers.0.q_proj
  repetition 1  -> layers.1.q_proj
  ...
  repetition 31 -> layers.31.q_proj
```

The same logical element may therefore have many runtime sites.

## 9.2 Stable path rule

Generated module names must be deterministic from semantic IDs/names, not canvas position or creation order alone.

Renaming a display label may alter generated source names only when the user explicitly changes the semantic module name. Pure visual relabeling must not silently break checkpoint names.

---

# 10. PyTorch graph validation

Implement a validation pass before shape inference or compilation.

```python
def validate_model_graph(state: PyTorchMetaBlockState) -> ValidationReport: ...
```

Validation must detect and report structured diagnostics for:

- duplicate element IDs;
- duplicate edge IDs;
- unknown element kinds;
- missing edge endpoints;
- unknown ports;
- multiple incoming edges on non-variadic inputs;
- required inputs left unconnected without defaults;
- directed cycles in a method/group dataflow graph unless an explicitly supported recurrent element owns that recurrence;
- invalid Group references;
- recursive Group-definition cycles;
- invalid `repeat` values;
- parameter type/range violations;
- unresolved external parameter bindings;
- invalid semantic module names;
- conflicting generated module paths;
- shape rule failures;
- unsupported method graph names/configurations.

The validator returns all safely discoverable diagnostics in one pass rather than stopping after the first error.

Do not compile a graph with error-level diagnostics.

---

# 11. Tensor specification and symbolic dimensions

The PyTorch addon owns shape/type propagation for its dialect.

Implement:

```python
@dataclass(frozen=True)
class Dim:
    expr: str
    value: int | None

@dataclass(frozen=True)
class TensorSpec:
    shape: tuple[Dim, ...]
    dtype: str | None
    semantic: str | None
```

Examples:

```text
(e, t)            int64 token ids
(e, t, d)         float model activations
(e, h, t, dh)     attention heads
(e, h, t, t)      attention scores
```

`expr` is a short symbolic expression such as `e`, `t`, `d`, `d/h`, `ff`, `vocab`.

If an integer value is resolved, set `value`; otherwise keep it symbolic.

## 11.1 Symbol equality

V1 shape inference supports:

- exact integer equality;
- exact symbolic-string equality;
- simple quotient/product expressions created by addon rules (`d/h`, `h*dh`);
- basic broadcast compatibility for Add/Multiply;
- explicit constraints such as `d % h == 0` when both resolve to integers.

Do not build a general symbolic algebra system in V1.

## 11.2 Runtime truth

Static shape inference is an authoring aid. Actual runtime hook observations are the runtime truth.

If a runtime shape contradicts the expected static shape, emit a `runtime_shape_mismatch` warning/event attached to the element. Do not rewrite the persisted graph automatically.

---

# 12. Element registry contract

Every V1 element kind is registered by the addon.

Conceptual interface:

```python
class ElementKind(Protocol):
    kind: str
    input_ports: tuple[PortSpec, ...]
    output_ports: tuple[PortSpec, ...]

    def validate_params(self, params, external_values) -> list[Diagnostic]: ...
    def infer_shapes(self, inputs, params, external_values) -> tuple[TensorSpec, ...]: ...
    def build_module(self, params, external_values) -> nn.Module | None: ...
    def emit_forward(self, ctx, element, input_exprs) -> CodegenResult: ...
    def presentation(self, element, inferred) -> ElementPresentation: ...
```

This protocol is internal to `graphimo-pytorch`; it is not a Graphimo Core plugin API.

An element may be:

- a true `nn.Module` site (`Linear`, `RMSNorm`, `Embedding`);
- an inspectable wrapper module for a functional op (`Softmax`, `MatMul`, `RoPE`);
- a structural/codegen-only operation when live inspection is not meaningful;
- an I/O or method boundary;
- a Group instance.

If an element is rendered as independently inspectable in the canvas, V1 should prefer an addressable runtime module site so live hooks and TorchLens targeting remain straightforward.

---

# 13. V1 element catalog - exact minimum

The following element kinds are required for the first complete addon. The palette may expose additional aliases/presets, but these semantic kinds must exist.

```text
I/O
  input
  output

Layers
  embedding
  linear
  rms_norm
  dropout

Activations
  relu
  gelu
  silu
  softmax

Tensor transforms
  reshape
  transpose
  flatten
  unflatten
  split_heads
  merge_heads
  concatenate

Math / attention
  add
  multiply
  scale
  matmul
  rope
  causal_mask

Losses
  cross_entropy

Structure
  group_instance
```

The initial palette may omit rarely used options from display, but the Llama template requires at least `embedding`, `linear`, `rms_norm`, `silu`, `softmax`, `split_heads`, `merge_heads`, `transpose`, `scale`, `matmul`, `rope`, `causal_mask`, `add`, `multiply`, and `cross_entropy`.

---

# 14. Required shape and compile semantics by element

This section is normative. Element implementations must follow these rules.

## 14.1 `input`

Parameters:

```text
name: str
shape: list[str|int]
dtype: str
semantic: optional str
```

The output `TensorSpec` is exactly the declared input spec.

Method inputs are represented as method-boundary records; a visual `input` element may project them into the graph.

## 14.2 `output`

One input, no tensor transformation. Marks a named method output.

## 14.3 `embedding`

Parameters:

```text
num_embeddings: int > 0
embedding_dim: int > 0
init_std: float | None
```

Input: integer tensor of shape `(...dims)`.

Output:

```text
(...dims, embedding_dim)
```

Compiler:

```python
nn.Embedding(num_embeddings, embedding_dim)
```

If `init_std` is not `None`, emit deterministic initialization code using `nn.init.normal_`.

## 14.4 `linear`

Parameters:

```text
in_features: int > 0
out_features: int > 0
bias: bool
init_std: float | None
```

Input last dimension must equal `in_features` when known.

Output replaces the last dimension with `out_features`.

Compiler:

```python
nn.Linear(in_features, out_features, bias=bias)
```

Optional normal initializer mirrors the embedding rule.

## 14.5 `rms_norm`

Parameters:

```text
normalized_shape: int > 0
eps: float > 0
```

Input last dimension must equal `normalized_shape` when known. Output shape unchanged.

Compiler:

```python
nn.RMSNorm(normalized_shape, eps=eps)
```

## 14.6 `dropout`

Parameter `p` in `[0, 1)`.

Output shape unchanged.

Compiler: `nn.Dropout(p)`.

## 14.7 `relu`, `gelu`, `silu`

Output shape unchanged.

Compile as inspectable small `nn.Module` instances. Native `nn.ReLU`, `nn.GELU`, `nn.SiLU` may be used.

## 14.8 `softmax`

Parameter:

```text
dim: int
```

Output shape unchanged.

Use an inspectable wrapper module or `nn.Softmax(dim)`.

## 14.9 `reshape`

Parameter `target_shape` is a list of symbolic/int tokens.

Preserve total element count when statically resolvable. Support one inferred `-1` dimension.

Compiler emits a named inspectable reshape module in V1 when the node is visible as its own element.

## 14.10 `transpose`

Parameters `dim0`, `dim1`.

Output swaps the selected dimensions.

Compile through a small inspectable module wrapper.

## 14.11 `flatten`

Parameters `start_dim`, `end_dim`.

Follow PyTorch flatten semantics for shape inference.

## 14.12 `unflatten`

Parameters:

```text
dim: int
sizes: list[int|symbol]
```

The product of new sizes must match the old dimension when known.

## 14.13 `split_heads`

Parameters:

```text
n_heads: int > 0
layout: "e_t_d_to_e_h_t_dh"
```

Input canonical shape:

```text
(e, t, d)
```

Require `d % n_heads == 0` when `d` is concrete.

Output:

```text
(e, h, t, dh)
where h=n_heads and dh=d/h
```

Compiler may use `unflatten(-1, (n_heads, -1)).transpose(1, 2)` but must wrap the operation in an addressable inspectable module for V1 live inspection.

## 14.14 `merge_heads`

Input canonical shape `(e, h, t, dh)`.

Output `(e, t, h*dh)`.

Compiler may use `transpose(1, 2).flatten(2)` inside an inspectable wrapper.

## 14.15 `concatenate`

Variadic inputs. Parameter `dim`.

All non-concatenated dimensions must be compatible. Concatenated dimension is the symbolic/int sum.

## 14.16 `add`

Two inputs. Standard broadcast-compatible shape rules.

Compiler uses an inspectable binary-op module so the visual node has an addressable site.

## 14.17 `multiply`

Same shape/broadcast rules as `add`.

## 14.18 `scale`

One tensor input plus scalar parameter/external value `factor`.

Shape unchanged.

Compiler uses an inspectable scale module.

## 14.19 `matmul`

Two tensor inputs. Implement the V1 rules needed for batched attention matmul and ordinary matrix multiplication.

For attention acceptance:

```text
(e,h,t,dh) @ (e,h,dh,t) -> (e,h,t,t)
(e,h,t,t)  @ (e,h,t,dh) -> (e,h,t,dh)
```

Use `torch.matmul` semantics. Produce a structured shape diagnostic when static dimensions are incompatible.

## 14.20 `rope`

Parameters:

```text
base: float > 0, default 10000.0
```

Expected input shape `(..., t, d)` with even final `d` when known.

Output shape unchanged.

Compiler uses the same pair-rotation semantics as the user-provided Llama reference:

```python
inv_freq = 1.0 / base ** (torch.arange(0, d, 2, device=x.device, dtype=torch.float32) / d)
ang = torch.outer(torch.arange(t, device=x.device, dtype=torch.float32), inv_freq)
ang = torch.cat((ang, ang), dim=-1)
x1, x2 = x[..., : d // 2], x[..., d // 2 :]
return x * ang.cos().to(x.dtype) + torch.cat((-x2, x1), dim=-1) * ang.sin().to(x.dtype)
```

Wrap as an inspectable module.

## 14.21 `causal_mask`

Input attention scores with last two dimensions `(t, t)`.

Output shape unchanged.

Semantics: positions strictly above the diagonal become `-inf`.

Wrap as an inspectable module.

## 14.22 `cross_entropy`

V1 supports next-token language-model cross entropy and generic logits/target mode.

Parameters:

```text
mode: "generic" | "next_token"
```

For `next_token`:

```python
F.cross_entropy(
    logits[:, :-1].flatten(0, -2),
    input_ids[:, 1:].flatten(),
)
```

Output is a scalar float tensor spec `()`.

This element may live in a named `loss` method graph instead of `forward`.

## 14.23 `group_instance`

References a `GroupDefinition` and optional repetition count.

The compiler generates a nested `nn.Module` class for the group. `repeat=1` creates one module; `repeat>1` creates an `nn.ModuleList`/equivalent repeated module structure and emits a loop in generated source.

Group input/output shape propagation is computed by evaluating the GroupDefinition shape rules once symbolically, then applying them through each repetition when the group's output feeds its own next instance.

---

# 15. Parameters, inspectors, and promotion to parent ports

Every element parameter must have a descriptor:

```python
@dataclass(frozen=True)
class ParameterSpec:
    name: str
    type: str
    default: JsonValue
    minimum: float | int | None
    maximum: float | int | None
    choices: tuple[JsonValue, ...] | None
    structural: bool
```

The Workbench inspector uses these descriptors to render appropriate editors.

Examples:

```text
Linear.out_features -> integer input, structural
Dropout.p           -> float control [0,1), structural model behavior
Softmax.dim         -> integer input
RMSNorm.eps         -> float input
```

## 15.1 Promotion

The user can choose **Promote parameter to MetaBlock input** for supported scalar parameters.

Promotion:

1. creates an `ExternalParameter` on the MetaBlock;
2. replaces the literal parameter value with a `ParameterBinding`;
3. creates an external input port on the MetaBlock outer card;
4. allows an ordinary Graphimo literal/control connection to drive it;
5. causes marimo to rematerialize the `PyTorchModelDefinition` when that value changes.

Demotion restores a literal value and removes the external port only if it has no other bindings.

Structural changes never mutate an actively training model in place. They create a new materialization definition. Existing runtime instances continue until their external execution ends or is cancelled.

---

# 16. Shape propagation engine

Implement:

```python
def infer_model_shapes(
    state: PyTorchMetaBlockState,
    external_values: Mapping[str, JsonScalar],
) -> ShapeInferenceResult: ...
```

The engine:

1. validates the graph;
2. topologically orders each MethodGraph/GroupDefinition;
3. resolves input TensorSpecs;
4. applies each element kind's shape rule;
5. annotates every output port/edge with a TensorSpec or diagnostic;
6. evaluates GroupDefinitions recursively without allowing recursive group cycles;
7. supports repeated groups;
8. returns all shape diagnostics without invoking a real model forward.

The Workbench displays inferred shapes on element cards/edges.

A shape error prevents compilation only if it proves incompatibility. Unknown symbolic dimensions may remain unresolved.

## 16.1 Runtime cross-check

When live hooks observe actual output shapes, compare them with the inferred spec when comparison is meaningful.

A mismatch emits an element-level warning containing:

```text
expected shape
actual shape
module path
runtime_instance_id
```

Do not modify inference state from runtime observations automatically.

---

# 17. Compiler architecture

The compiler is deterministic and takes only:

```text
PyTorchMetaBlockState
resolved external parameter values
addon compiler version
```

It returns:

```python
@dataclass(frozen=True)
class CompileResult:
    generated_source: str
    factory: Callable[[], nn.Module]
    bindings: RuntimeBindingMap
    shape_result: ShapeInferenceResult
    diagnostics: tuple[Diagnostic, ...]
```

No compiled result is produced if error-level diagnostics exist.

## 17.1 Compiler pipeline

Required stages:

```text
load state
 -> schema validation
 -> semantic validation
 -> resolve external params
 -> shape inference
 -> deterministic names
 -> topological schedules
 -> nested Group class generation
 -> model class generation
 -> runtime-site binding generation
 -> compile generated Python
 -> obtain class/factory
 -> return source + factory + bindings
```

The generated source and runtime factory must represent the **same** semantic graph revision.

## 17.2 Source compilation

V1 may compile the addon-generated trusted source using Python `compile()`/`exec()` inside a private addon namespace. This is not Graphimo's generic execution mechanism and must never accept arbitrary untrusted user source through this path.

The namespace should expose only the imports/helpers intentionally used by generated code.

Compile errors are addon defects for a validated graph and must return `pytorch_codegen_failed` with source context.

---

# 18. Generated Python source contract

The code pane must show readable Python that a PyTorch developer can understand.

Required style:

- standard `import torch`, `import torch.nn as nn`, `import torch.nn.functional as F` as needed;
- deterministic class names from model/group names;
- nested reusable Group definitions emitted as classes;
- repeated Groups emitted using `nn.ModuleList` when each repetition owns parameters;
- semantic module names such as `q_proj`, not opaque Graphimo IDs, where uniqueness permits;
- comments may include Graphimo element IDs for traceability, but user-facing names stay readable;
- helper modules/functions such as RoPE may be emitted clearly;
- no Graphimo scheduler calls in `forward`;
- no Workbench/UI calls in generated model code;
- no TorchLens dependency in generated model source;
- no runtime hook code embedded directly in model source unless required for a specialized inspectable wrapper. Hook registration belongs to the addon runtime layer.

## 18.1 Generated header

Include a short machine-readable comment header:

```python
# Generated by graphimo-pytorch
# graphimo-metablock-id: 7ab2c1f0
# graphimo-graph-revision: <sha256>
# graphimo-materialization-key: <sha256>
```

The header is informational and useful for diagnostics/export. It is not the canonical state store.

## 18.2 Export

The Workbench action **Export Python** writes the generated source to a user-selected `.py` path.

Export does not change the MetaBlock's source of truth.

V1 exported code may depend on `graphimo_pytorch.runtime_ops` only if an operation cannot reasonably be expressed in short standard PyTorch. Prefer ordinary PyTorch for the required V1 element set.

---

# 19. Inspectable functional operations

Operations that are visually first-class and need live inspection must have stable runtime sites.

V1 uses small `nn.Module` wrappers where needed.

Required internal wrappers include at least:

```text
ReshapeOp
TransposeOp
SplitHeadsOp
MergeHeadsOp
AddOp
MultiplyOp
ScaleOp
MatMulOp
RoPEOp
CausalMaskOp
```

`nn.Softmax` may be used directly.

Each wrapper is deterministic and contains no parameters unless the visual operation truly owns them.

The compiler assigns every inspectable visual element a module path and includes it in `RuntimeBindingMap`.

The pretty exporter may later lower wrappers to idiomatic functional expressions, but that optimization is not required for V1.

---

# 20. Runtime model registry

The addon maintains a process-local weak runtime registry:

```python
class RuntimeRegistry:
    def register(self, model: nn.Module, info: RuntimeModelInfo) -> None: ...
    def resolve(self, runtime_instance_id: str) -> nn.Module | None: ...
    def list_for_metablock(self, metablock_id: str) -> list[RuntimeModelInfo]: ...
```

Requirements:

- weak references prevent the registry from keeping finished models alive;
- registry entries are removed automatically when models are collected;
- explicit `release_runtime_model(model)` removes hooks/subscriptions and registry metadata;
- registry state is not persisted;
- multiple runtime instances from the same MetaBlock may coexist;
- events always include `runtime_instance_id` so the UI can distinguish them.

---

# 21. Generic PyTorch telemetry event schema

All addon live events sent to the Graphimo application layer use a JSON-safe envelope.

```python
@dataclass(frozen=True)
class PyTorchEvent:
    schema_version: int
    event_id: str
    kind: str
    timestamp: float
    metablock_id: str
    graph_revision: str
    runtime_instance_id: str
    logical_element_id: str | None
    module_path: str | None
    group_instance_path: tuple[GroupRuntimeIndex, ...]
    step: int | None
    payload: dict[str, JsonValue]
```

V1 schema version is `1`.

Required kinds:

```text
runtime.created
runtime.released
activation.summary
gradient.summary
metric.scalar
resource.cuda
inspection.torchlens.started
inspection.torchlens.completed
inspection.torchlens.failed
runtime.warning
runtime.error
```

Graphimo Core treats `kind`/`payload` as addon data and routes them generically. It does not interpret activation or gradient semantics.

---

# 22. Subscription model - no always-on heavy capture

Live inspection is subscription-driven.

Implement:

```python
@dataclass(frozen=True)
class SubscriptionSpec:
    subscription_id: str
    runtime_instance_id: str
    logical_element_id: str
    repetition_index: int | None
    signal: str
    rate_hz: float
    detail: str
```

Supported V1 `signal` values:

```text
activation
gradient
```

Supported `detail` values:

```text
summary
histogram
sample
```

`summary` is the default.

Requirements:

- no activation/gradient hooks are installed for an element with no active subscription, except minimal plugin-wide metrics explicitly enabled by the user;
- subscribing resolves the element to one or more runtime module paths through the binding map;
- unsubscribing removes hook handles when no remaining subscription uses them;
- a collapsed repeated Group can subscribe to one repetition, a selected set, or an aggregate in future; V1 requires one repetition at a time;
- rate limiting happens before UI event emission;
- never transfer a full GPU tensor to CPU merely to render summary statistics.

---

# 23. Tensor summary rules

Implement bounded tensor summaries without retaining full tensors.

```python
def summarize_tensor(tensor: torch.Tensor) -> dict[str, JsonValue]: ...
```

Required fields when applicable:

```text
shape
dtype
device
numel
finite_fraction
mean
std
min
max
```

Rules:

- statistics run under `torch.no_grad()`;
- for floating/complex tensors, finite checks must not crash on empty tensors;
- for huge tensors, scalar reductions may be computed on-device and only scalar results transferred;
- integer/bool tensors omit meaningless mean/std where appropriate or compute only explicitly defined safe summaries;
- summary must not store the original tensor;
- failures produce a bounded warning, not a training failure.

## 23.1 Histogram

Histogram subscriptions are opt-in. Use a bounded bin count (default 64) and configurable sampling for very large tensors.

Payload contains only bin edges/counts and tensor metadata.

## 23.2 Sample

Sample subscriptions return a bounded flattened sample or selected slices. Default maximum payload is 4096 scalar values unless a later UI contract explicitly raises it.

---

# 24. Forward activation hooks

PyTorch `nn.Module.register_forward_hook` is the V1 live activation mechanism.

When an activation subscription exists:

1. resolve the module path with `model.get_submodule()`;
2. install `register_forward_hook`;
3. receive module output after `forward()`;
4. summarize the tensor/tree according to subscription detail;
5. emit `activation.summary` or detail-specific event;
6. never change the module output;
7. catch/contain inspection errors so they do not fail model execution.

If module output is a tuple/list/dict, recursively summarize a bounded structural view and require the UI to select a leaf for full histogram/sample detail.

Do not use PyTorch global module hooks for normal operation; global hooks add process-global state and are intended for debugging/profiling.

---

# 25. Gradient observation

V1 gradient observation targets the gradient of an element's output tensor.

Preferred mechanism:

- during the subscribed module's forward hook, identify Tensor leaves in the output that require gradients;
- attach `Tensor.register_hook` to those output tensors for that forward;
- when backward produces the gradient, summarize it and emit `gradient.summary`;
- the hook returns the original gradient unchanged.

`register_full_backward_hook` may be used for module-level diagnostics when appropriate, but V1 output-gradient semantics must remain explicit and documented.

Do not retain autograd graphs solely for UI inspection.

If an output does not require grad, emit a one-shot `gradient_unavailable` warning for the subscription rather than failing training.

---

# 26. Scalar metrics from external training code

The model MetaBlock is not the training loop. External Graphimo nodes/groups may emit metrics associated with a runtime model.

Expose addon helpers:

```python
def emit_metric(
    model: nn.Module,
    name: str,
    value: float | int | torch.Tensor,
    *,
    step: int | None = None,
) -> None: ...

def emit_cuda_resources(
    model: nn.Module,
    *,
    step: int | None = None,
) -> None: ...
```

`emit_metric` resolves the model's private runtime metadata and emits `metric.scalar` with the originating MetaBlock/runtime identity.

Tensor metric values must be scalar tensors; detach and convert safely.

A provided optional training Group/template may call these helpers automatically, but user-authored training code can call them directly.

Example:

```python
@node
def train(model_def: PyTorchModelDefinition, batch, steps: int = 100):
    model = model_def(device="cuda")
    opt = torch.optim.AdamW(model.parameters(), lr=3e-4)
    for step in range(steps):
        ...
        loss.backward()
        opt.step()
        graphimo_pytorch.emit_metric(model, "train/loss", loss, step=step)
    return model
```

The plugin may later provide richer helpers/context managers, but this simple path must work.

---

# 27. CUDA/resource telemetry

`emit_cuda_resources()` emits bounded data when CUDA is available:

```text
memory_allocated
memory_reserved
max_memory_allocated
max_memory_reserved
device_index
device_name when cheaply available
```

No CUDA call is attempted when the model/runtime is CPU-only.

Resource telemetry is rate-limited like other live events.

---

# 28. Workbench live model experience

When the user enters a PyTorch MetaBlock and an active runtime instance exists, the Workbench provides a runtime selector if more than one instance exists.

Selecting an internal element must show at least:

```text
semantic kind
module path
inferred input/output shapes
actual latest runtime output shape when observed
parameter count where applicable
activation summary when subscribed
gradient summary when subscribed
runtime instance / repetition index
```

Required actions:

```text
Subscribe activation summary
Subscribe gradient summary
Show histogram
Show bounded sample
Stop live inspection
Deep inspect with TorchLens
```

For repeated groups such as `LlamaDecoderLayer x 32`, the inspector provides a repetition index chooser (`0..31`) and uses the corresponding runtime path.

The canvas should keep compact information on nodes/edges and reserve the full detail for the inspector panel.

---

# 29. TorchLens role - fixed

TorchLens is the **deep inspection and intervention layer**. It is not used to determine basic Graphimo element mapping and is not run continuously on every batch.

Current reference dependency: `torchlens==2.34.1`.

TorchLens supports module-path selection, forward/backward activation/gradient capture and interventions. It should operate on ordinary eager PyTorch models. Do not make `torch.export.ExportedProgram` the live TorchLens path; current TorchLens documentation explicitly treats `torch.export`/TorchScript forwards as incompatible with normal TorchLens interception.

## 29.1 V1 deep inspection API

Expose:

```python
@dataclass(frozen=True)
class TorchLensRequest:
    runtime_instance_id: str
    logical_element_id: str
    repetition_index: int | None
    capture_gradients: bool
    intervention: TorchLensIntervention | None


def inspect_with_torchlens(
    request: TorchLensRequest,
    inputs: object,
    *,
    loss_fn: Callable | None = None,
) -> TorchLensResult: ...
```

`inputs` are explicit for V1. Do not silently capture arbitrary training batches in the background merely to satisfy TorchLens.

A later **inspect next batch** feature may arm one-shot input capture, but it is not required to make the V1 deep-inspection path functional.

## 29.2 Selector mapping

The addon resolves:

```text
Graphimo logical element + repetition index
 -> RuntimeSite.module_path
 -> TorchLens module selector
```

The binding map remains authoritative.

## 29.3 Required TorchLens V1 results

The result must expose bounded structured data for:

- selected module/path identity;
- relevant operation list/summary in the selected module or region;
- saved activation metadata;
- gradient metadata when requested and a loss/backward path is provided;
- non-finite diagnostics when available;
- trace metadata useful for an inspector;
- an optional ArtifactRef to a persisted/exported full trace when the user explicitly saves it.

Do not serialize a complete TorchLens Trace into Graphimo's normal project JSON.

## 29.4 Interventions

V1 advanced inspector supports at least:

```text
zero ablation
scale by scalar
```

These are explicit user-triggered experimental actions, not persistent architecture edits.

Use TorchLens intervention APIs rather than modifying model weights for these experiments.

Intervention runs must be visibly labeled as experimental/modified runs in the inspector results.

---

# 30. TorchLens result persistence

Default TorchLens inspection results are transient runtime data.

The user action **Save inspection** may export a trace/report to Graphimo ArtifactStore.

Artifact kind examples:

```text
pytorch-torchlens-trace
pytorch-inspection-report
pytorch-activation-snapshot
```

The stored artifact metadata should include:

```text
metablock_id
graph_revision
materialization_key
runtime_instance_id
logical_element_id
module_path
TorchLens version
PyTorch version
```

Do not pin/save massive tensors without explicit user action.

---

# 31. Checkpoint contract

The addon provides explicit checkpoint helpers using Graphimo ArtifactStore.

```python
def save_checkpoint(
    model: nn.Module,
    *,
    optimizer: torch.optim.Optimizer | None = None,
    scheduler: object | None = None,
    metadata: Mapping[str, JsonValue] | None = None,
) -> ArtifactRef: ...


def load_checkpoint(
    model_def: PyTorchModelDefinition,
    artifact: ArtifactRef,
    *,
    device: str | torch.device | None = None,
    dtype: torch.dtype | None = None,
    strict: bool = True,
) -> nn.Module: ...
```

Persist at minimum:

```text
model state_dict
metablock_id
graph_revision
materialization_key
external parameters
PyTorch version
addon version
optional optimizer state
optional scheduler state when safely serializable by the helper
user metadata
```

Use `torch.save` into a temporary file, then store through Graphimo ArtifactStore.

Do not persist a whole arbitrary Python model object as the primary checkpoint format.

## 31.1 Exact revision behavior

If checkpoint `graph_revision` and current definition revision match, `strict=True` loads normally.

If they differ:

- `strict=True` refuses with `pytorch_checkpoint_revision_mismatch` before silently loading partial weights;
- `strict=False` may invoke the explicit migration helper described below and must return/report missing/unexpected/mismatched keys.

---

# 32. Weight migration helper

V1 includes a conservative migration helper because architecture editing is a core expected use case.

```python
@dataclass(frozen=True)
class WeightMigrationReport:
    copied: tuple[str, ...]
    missing_in_source: tuple[str, ...]
    missing_in_target: tuple[str, ...]
    shape_mismatch: tuple[ShapeMismatch, ...]


def migrate_state_dict(
    source_state: Mapping[str, Tensor],
    target_model: nn.Module,
) -> WeightMigrationReport: ...
```

Only exact-name + exact-shape tensors are copied in V1.

Never resize/interpolate/slice weights implicitly.

The user must see the migration report before considering a forked architecture equivalent to the source checkpoint.

Optimizer state is not migrated automatically after structural changes.

---

# 33. Initializers

V1 layer elements support these initializer modes where meaningful:

```text
default
normal(std)
zeros
ones
```

The Llama template uses `normal(std=0.02)` on embedding and linear weights to match the provided reference code.

Initializer semantics are part of the model graph and affect revision hashing.

The generated source must visibly include non-default initialization.

---

# 34. Model methods and entrypoint tabs

The PyTorch MetaBlock Workbench provides a method/entrypoint selector when the model contains more than `forward`.

For the Llama V1 template:

```text
[ forward ] [ loss ] [ predict ]
```

Each method is edited as a PyTorch-dialect graph using the same canvas mechanics.

Shared module elements defined in the model may be referenced by method graphs through explicit module-call elements or generated method calls. Do not duplicate parameter-owning modules merely because multiple methods use them.

The shipped Llama template may implement `predict` compactly as a generated method calling `self(input_ids)` then softmax rather than duplicating the whole forward graph visually. The semantic state should still make this relationship explicit and reconstructible.

---

# 35. Templates and component library

The addon component catalog is visible only inside `pytorch.model` scopes, except the top-level **PyTorch Model** MetaBlock creation entry.

Required palette categories:

```text
Inputs / Outputs
Layers
Activations
Tensor Ops
Math / Attention
Losses
Groups / Templates
```

Required shipped templates:

```text
MLP
Self Attention
Llama Decoder Layer
Llama-style Causal LM
```

Templates instantiate ordinary addon graph records; they are not magic runtime classes hidden from the user.

After insertion the user may open/edit the resulting Group definitions/elements.

---

# 36. Basic template: MLP

Ship a simple acceptance template:

```text
Input (e,d_in)
 -> Linear(d_in, hidden)
 -> ReLU
 -> Linear(hidden, d_out)
 -> Output
```

Default dimensions:

```text
d_in=16
hidden=32
d_out=4
```

This template is used by early compiler/live-inspection tests before Llama.

---

# 37. Self-attention template

Ship a reusable attention Group template that includes:

```text
RMSNorm/input normalization optional preset
Q Linear
K Linear
V Linear
split heads x3
RoPE on Q/K configurable
scale Q
K transpose
QK matmul
causal mask optional
softmax
attention x V matmul
merge heads
output Linear
```

Parameters include at least:

```text
d_model
n_heads
causal
rope
init_std
```

Shape inference must display the canonical transforms.

---

# 38. Llama Decoder Layer template - normative acceptance graph

The template must be semantically equivalent to the user-provided reference implementation.

Parameters:

```text
d = 4096
n_head = 32
ff = 11008
rms_eps = 1e-5
init_std = 0.02
rope_base = 10000.0
```

Required structure:

```text
x (e,t,d)
 |
 +------------------------------ residual A ------------------------------+
 |                                                                         |
RMSNorm                                                                    |
 |                                                                         |
 +-> q_proj -> split_heads -> RoPE -> scale ------------------+            |
 |                                                            |            |
 +-> k_proj -> split_heads -> RoPE -> transpose ------------ matmul        |
 |                                                            |            |
 +-> v_proj -> split_heads -------------------------------+    |            |
                                                          | causal_mask     |
                                                          |    |            |
                                                          | softmax         |
                                                          |    |            |
                                                          +-- matmul        |
                                                               |            |
                                                          merge_heads       |
                                                               |            |
                                                            o_proj          |
                                                               |            |
 +------------------------------------------------------------- add <------+
                                                               |
                                                               x'
                                                               |
 +------------------------------ residual B ------------------------------+
 |                                                                         |
RMSNorm                                                                    |
 |\                                                                        |
 | gate_proj -> SiLU --\                                                   |
 |                    multiply -> down_proj                                |
 | up_proj -------------/              |                                   |
 +------------------------------------- add <-------------------------------+
                                       |
                                     output
```

`q` scale factor is `(d / n_head) ** -0.5`.

The generated source should closely resemble idiomatic PyTorch and preserve the semantic module names from the reference: `input_layernorm`, `q_proj`, `k_proj`, `v_proj`, `o_proj`, `post_attention_layernorm`, `gate_proj`, `up_proj`, `down_proj`.

---

# 39. Llama-style Causal LM template - normative acceptance model

Parameters:

```text
vocab_size = 32000
d = 4096
n_head = 32
ff = 11008
n_layers = 32
seq_len_for_shape_preview = 64
init_std = 0.02
```

Forward graph:

```text
input_ids (e,t)
 -> Embedding(vocab_size,d)
 -> LlamaDecoderLayer Group x n_layers
 -> RMSNorm(d)
 -> Linear(d,vocab_size)
 -> logits (e,t,vocab_size)
```

Loss graph:

```text
logits + input_ids
 -> CrossEntropy(mode=next_token)
 -> loss scalar
```

Predict graph:

```text
input_ids
 -> forward(input_ids)
 -> softmax(dim=-1)
 -> probabilities
```

The generated model source must provide:

```python
class LlamaForCausalLM(nn.Module):
    def forward(self, input_ids): ...
    def loss(self, logits, input_ids): ...
    @torch.no_grad()
    def predict(self, input_ids): ...
```

The generated source need not be byte-identical to the reference, but shape semantics and computation must match.

---

# 40. Runtime binding acceptance for repeated Llama layers

For `n_layers=32`, runtime bindings must resolve internal logical elements per repetition.

Example:

```text
logical q_proj element ->
  layers.0.q_proj
  layers.1.q_proj
  ...
  layers.31.q_proj
```

The Workbench collapsed Group card shows `LlamaDecoderLayer x 32`.

Opening the Group shows one logical template graph. Selecting `q_proj` shows a repetition selector. Choosing index `17` targets `layers.17.q_proj`.

The binding implementation must not create 32 duplicate visual graphs merely to support runtime selection.

---

# 41. External training Group/template supplied by addon

The model MetaBlock itself does not own training, but the addon should ship a convenient normal-Graphimo training component/template so the product can be demonstrated end-to-end without users writing boilerplate.

This training helper lives outside the MetaBlock dialect and is exposed as an addon-provided normal Graphimo component/Group where the generic addon host permits it.

Minimum behavior:

```text
inputs:
  model_def
  train_data/loader or batch provider
  epochs/steps
  lr

outputs:
  trained model or checkpoint artifact
  metrics/history summary
```

It must:

- instantiate the definition;
- use AdamW by default;
- emit `train/loss` via addon telemetry;
- optionally emit grad norm and CUDA resources;
- allow explicit checkpoint persistence;
- not become a Graphimo scheduler;
- remain ordinary Python/marimo execution.

The helper is convenience, not a requirement that all user training code use it.

---

# 42. Live loss/history presentation

Scalar metrics are stored as a bounded in-memory series per active runtime/external execution for the Workbench.

Default maximum retained live points per metric: `5000` with decimation after the limit.

The UI can plot:

```text
train/loss
validation/loss
learning_rate
grad_norm
custom scalar metrics
```

The plugin runtime may expose a structured history snapshot to the Workbench.

Historical long-term experiment tracking is outside V1 and must not silently become a new MLflow-like subsystem.

---

# 43. Performance requirements

The addon is intended for serious ML workloads. Inspection must be opt-in and bounded.

Required defaults:

- no full activation tensor transfer when nothing is selected;
- no global forward hook;
- no full TorchLens trace during ordinary training;
- live activation/gradient summary hooks only for subscribed elements;
- default live event rate per subscription <= 10 Hz;
- histogram/sample modes may default <= 2 Hz;
- rate limiting drops/coalesces UI events rather than blocking the training thread;
- tensor summary operations must avoid unnecessary CPU synchronization beyond scalar results;
- Workbench rendering must not be called directly from PyTorch hooks; hooks emit to an event queue/seam;
- hook failures are isolated from model execution whenever possible.

A debug option may show inspection overhead statistics.

---

# 44. Threading/event delivery

PyTorch hooks may execute on the training thread. They must do minimal bounded work.

Required event path:

```text
PyTorch hook
 -> compute bounded summary
 -> enqueue PyTorchEvent
 -> return immediately
 -> Graphimo application/event pump
 -> Workbench renderer
```

Do not perform blocking UI RPC from a hook.

If the host event seam is not thread-safe, the addon uses a process-local thread-safe queue and a host-safe drain operation.

---

# 45. Diagnostics and error codes

Use structured error codes. Minimum set:

```text
pytorch_state_schema_unsupported
pytorch_state_invalid
pytorch_unknown_element_kind
pytorch_unknown_port
pytorch_edge_invalid
pytorch_cycle_invalid
pytorch_group_cycle_invalid
pytorch_param_invalid
pytorch_external_param_missing
pytorch_shape_incompatible
pytorch_shape_divisibility_error
pytorch_codegen_failed
pytorch_runtime_binding_missing
pytorch_runtime_not_found
pytorch_subscription_invalid
pytorch_gradient_unavailable
pytorch_torchlens_unavailable
pytorch_torchlens_failed
pytorch_checkpoint_revision_mismatch
pytorch_checkpoint_invalid
pytorch_weight_shape_mismatch
pytorch_import_unsupported
```

Diagnostics must include the relevant MetaBlock/element/group/method IDs when available.

---

# 46. Source import / round-trip contract for V1

V1 does **not** promise arbitrary existing PyTorch source -> editable Graphimo graph.

The import command supports only these cases:

1. a Graphimo-PyTorch semantic JSON file;
2. a project MetaBlock already persisted by this addon;
3. optionally generated source carrying a matching Graphimo header **only when** the corresponding semantic state/revision is also available.

For arbitrary `nn.Module` source, return `pytorch_import_unsupported` with an explanation that general source-to-graph import is a future feature.

Do not create a misleading partial graph silently.

Future versions may add a supported parser subset and opaque fallback elements, inspired by Torch Canvas, but V1 model creation/editing does not depend on this reverse direction.

---

# 47. `torch.fx`, `torch.export`, and `torch.compile` policy

These technologies are optional helpers, not Graphimo-PyTorch's source of truth.

## `torch.fx`

May be used in tests/debug validation where the generated model is traceable. Do not require all supported PyTorch MetaBlocks to round-trip through FX.

## `torch.export`

Not used as the normal live runtime or TorchLens model. It may become a future export/validation feature. Current TorchLens behavior is designed around ordinary eager execution and documents `torch.export` forwards as incompatible with interception.

## `torch.compile`

V1 default runtime is eager mode for predictable inspection. User code may choose to compile a model outside the addon, but live Graphimo-PyTorch inspection is only guaranteed for the eager model/runtime path in V1.

Do not make compile/export optimization a prerequisite for basic functionality.

---

# 48. Workbench projection contract

`graphimo_pytorch.workbench_projection` converts addon semantic/runtime data into host-independent Graphimo view models.

The renderer must not inspect raw `nn.Module` objects to discover UI structure.

Required projections:

```text
PyTorch palette items
MetaBlock outer summary
method tabs
internal element cards
edge shape labels
parameter inspector descriptors
shape diagnostics
runtime selector
activation/gradient summaries
metric series
TorchLens inspector summaries
checkpoint/migration reports
```

The PyTorch addon provides the specialized data; Graphimo Workbench uses general canvas/panel infrastructure.

---

# 49. Element card minimum UX

Inside a PyTorch MetaBlock, cards should expose compact semantic information.

Examples:

```text
Linear
4096 -> 4096
bias: false
init: N(0,0.02)
out: e x t x 4096
```

```text
RMSNorm
4096
eps: 1e-5
out: e x t x 4096
```

```text
split heads
e t (h d) -> e h t d
32 heads
```

```text
Softmax
dim=-1
out: e x h x t x t
```

When live subscribed, the card may additionally show bounded latest information such as:

```text
runtime: 8 x 64 x 4096
mean: 0.013
std: 0.82
```

Do not permanently expand cards with full histograms/trace details; use the inspector.

---

# 50. Edge UX

Edges inside PyTorch MetaBlocks carry tensor-shape information when inferred.

Display compact shape labels where useful:

```text
e x t x d

e x h x t x dh
```

Shape-incompatible connections are rejected or shown as invalid before compilation. Do not silently insert reshapes/coercions.

Dragging a connection into an incompatible port should show a specific compatibility reason when known.

---

# 51. Structural edit behavior during active training

A structural MetaBlock edit while a runtime model is currently training does **not** mutate that model.

Required behavior:

1. edit persisted MetaBlock state;
2. create new `graph_revision`;
3. update the parent marimo MetaBlock materialization cell revision;
4. normal marimo dependency semantics mark downstream/new model work stale;
5. currently active runtime instance remains associated with its old revision and may continue;
6. Workbench visibly marks that runtime as `older revision` relative to the edited graph;
7. user may stop the old run and start a new materialization/training run;
8. optional explicit weight migration can be invoked after training/checkpoint as a separate action.

Never patch module structure in the middle of a backward pass automatically.

---

# 52. Checkpoint/model provenance in the UI

A checkpoint inspector must show at least:

```text
Artifact ID
MetaBlock ID
Graph revision
Materialization key
Model name
PyTorch version
Addon version
Saved step/epoch if supplied
Optimizer state present yes/no
Compatibility with current graph
```

If revisions differ, show migration as an explicit action with report; do not label the checkpoint as directly compatible.

---

# 53. Missing addon behavior

If `graphimo-pytorch` is not installed, Graphimo Core preserves the PyTorch MetaBlock state and layout but renders an unavailable addon placeholder.

No PyTorch semantic JSON is deleted or rewritten.

After reinstall/discovery, the same MetaBlock loads using its persisted state.

The addon itself must support schema migrations from older supported schema versions in future; V1 only needs schema version 1.

---

# 54. Testing strategy - layers

Tests are split into five levels.

## Level A - pure schema/graph tests

No real PyTorch execution required except type imports.

Covers serialization, revision, validation, shape rules, catalog, Group topology.

## Level B - compiler tests

Compile small graphs into real `nn.Module`, inspect generated source/bindings, run CPU forward.

## Level C - runtime inspection tests

Run CPU training/backward, subscribe to activations/gradients, verify event routing and rate-safe bounded payloads.

CUDA-specific tests are optional/skipped when unavailable.

## Level D - TorchLens tests

Target selected module with explicit inputs, verify activation/deep inspection and optional gradient/intervention paths.

## Level E - Workbench/manual product conformance

Human uses the standalone Graphimo Workbench to edit a model, inspect shapes, train externally, subscribe live, run TorchLens, save checkpoint, edit architecture, migrate/load.

Zeron is not required for Levels A-E.

---

# 55. Mandatory unit/conformance tests

At minimum implement automated tests for:

1. addon entry-point registration/discovery;
2. Core import without addon/torch;
3. schema JSON round-trip;
4. deterministic graph revision;
5. layout-only changes not affecting revision;
6. unknown element preservation + materialization refusal;
7. invalid edge diagnostics;
8. cycle diagnostics;
9. Group recursion diagnostics;
10. external parameter resolution;
11. Linear shape inference;
12. Embedding shape inference;
13. Split/Merge heads inference and divisibility failure;
14. MatMul attention shapes;
15. Add/Multiply broadcasting;
16. RoPE even-width check;
17. next-token cross entropy shape contract;
18. basic MLP compile/run;
19. generated source imports/executes;
20. source and factory numerical equivalence for a fixed seed where practical;
21. stable runtime binding paths;
22. Group compile;
23. repeated Group compile/binding map;
24. MetaBlock definition materialization;
25. separate runtime instance IDs from same definition;
26. activation subscription receives correct node event;
27. unsubscribed node emits no activation event;
28. gradient subscription receives correct output-gradient event;
29. tensor summaries are bounded;
30. histogram/sample payload bounds;
31. external metric routes to correct MetaBlock/runtime;
32. checkpoint exact-revision load;
33. checkpoint revision mismatch refusal;
34. exact-name/exact-shape weight migration report;
35. TorchLens selected-module inspection;
36. TorchLens zero-ablation/scale intervention on an explicit inspection run;
37. unavailable TorchLens diagnostic if dependency is missing/broken;
38. Llama Decoder Layer shape propagation;
39. Llama `n_layers=2` compile/run as lightweight CI fixture;
40. Llama template generated source contains expected semantic module names;
41. repeated q_proj binding resolution for selected repetition;
42. structural edit produces new graph revision without altering old runtime metadata;
43. addon uninstall placeholder/reinstall state preservation through generic Graphimo tests.

---

# 56. Numerical reference tests for the Llama template

Do not instantiate the full 32-layer 4096-wide model in routine CI.

Use a scaled fixture preserving topology, for example:

```text
vocab_size=128
d=32
n_head=4
ff=64
n_layers=2
sequence_length=8
batch=2
```

Build:

1. generated Graphimo-PyTorch model;
2. a direct reference implementation following the same formulas;
3. copy identical state dictionaries or initialize under a deterministic seed;
4. compare `forward` logits within appropriate floating-point tolerance;
5. compare loss;
6. compare predict probabilities;
7. compare selected intermediate shapes.

The full-size Llama template is a UI/code-generation fixture, not a routine execution test.

---

# 57. Workbench acceptance scenario - MLP first

Before the Llama workflow, manually verify:

1. create PyTorch MetaBlock;
2. enter it;
3. palette switches to PyTorch;
4. add Input -> Linear A -> ReLU -> Linear B -> Output;
5. set dimensions and see edge shapes update;
6. intentionally make Linear B incompatible and see a shape diagnostic;
7. fix it;
8. inspect generated source;
9. return to parent scope;
10. connect model definition to addon training helper;
11. train a few steps;
12. reopen MetaBlock while training;
13. select Linear A and enable activation summary;
14. select Linear B and see a distinct summary;
15. enable gradient on Linear A;
16. run a deep TorchLens inspection with explicit sample inputs;
17. save a checkpoint;
18. change Linear B width;
19. observe new revision/old runtime marker;
20. inspect migration report from old checkpoint.

No generated wiring code needs manual editing.

---

# 58. Workbench acceptance scenario - Llama/Torch-Canvas target

After the MLP scenario passes:

1. create a Llama-style Causal LM template;
2. see the outer architecture comparable in structure to the provided Torch Canvas reference: input -> Embedding -> repeated Decoder Layer -> RMSNorm -> LM head -> logits;
3. see `LlamaDecoderLayer x 32` as one reusable/repeated Group presentation;
4. open the Group and see Q/K/V, RoPE, attention, residual, RMSNorm and gated MLP structure;
5. see shape labels such as `(e,t,d)`, `(e,h,t,dh)`, `(e,h,t,t)`;
6. change `n_head` and see shape propagation/update or divisibility error;
7. change `d`/`ff` and see generated source update;
8. use the method selector to inspect `loss` and `predict` graphs;
9. display generated code closely matching the reference class organization;
10. materialize the scaled runnable fixture;
11. run external train/inference;
12. inspect `q_proj` for repetition index 0 and 1 and confirm different runtime paths;
13. show live activation/gradient summaries;
14. deep inspect selected `q_proj` with TorchLens;
15. save/export generated Python;
16. save checkpoint/Artifact;
17. close/reopen project and preserve semantic graph + layout.

This scenario defines the intended V1 experience more strongly than a minimal hidden unit test.

---

# 59. Commit/milestone sequence for Codex

Implement the addon in this order. Do not jump to TorchLens or Llama before the lower layers are green.

## P1 - package + schema

Commit intent:

```text
pytorch-addon: add package registration and semantic state schema
```

Deliver:

- package/entry point;
- schema dataclasses;
- JSON load/save;
- canonical revision hashing;
- diagnostics base.

Gate: schema/registration tests green.

## P2 - registry + shapes

```text
pytorch-addon: add element registry validation and shape inference
```

Deliver:

- V1 element descriptors;
- graph validation;
- TensorSpec/Dim;
- shape rules;
- MLP shape tests;
- attention/Llama shape unit tests.

## P3 - compiler + generated source

```text
pytorch-addon: compile semantic graph to runnable readable pytorch
```

Deliver:

- topology/codegen;
- helper op modules;
- runtime binding map;
- `PyTorchModelDefinition`;
- MLP compile/run;
- Group/repeat compile.

## P4 - Workbench projection/editing

```text
pytorch-addon: expose contextual model palette and inspectors
```

Deliver:

- palette;
- element/parameter inspectors;
- edge shapes/errors;
- generated source panel;
- Group navigation/repetition UI data;
- outer MetaBlock ports/materialization cell integration.

## P5 - runtime live inspection

```text
pytorch-addon: route subscribed activations gradients and metrics
```

Deliver:

- weak runtime registry;
- subscriptions;
- forward/output-gradient hooks;
- tensor summaries;
- event queue;
- metric/resource helpers;
- Workbench live selector.

## P6 - checkpoints + migration

```text
pytorch-addon: persist checkpoints and report conservative migrations
```

Deliver exact-revision load and exact-name/exact-shape migration.

## P7 - TorchLens

```text
pytorch-addon: add selected-site torchlens deep inspection
```

Deliver explicit-input deep inspection, gradients where requested, zero-ablate/scale, artifact export.

## P8 - Llama templates

```text
pytorch-addon: add attention and llama architecture templates
```

Deliver decoder group, repeated model, loss/predict methods, scaled numerical reference tests, Workbench Llama scenario.

## P9 - integration hardening

```text
pytorch-addon: harden project reopen addon isolation and performance
```

Deliver uninstall/reinstall, missing addon state preservation, performance/rate tests, documentation and packaging.

---

# 60. Definition of done for the dedicated PyTorch addon

The first addon is complete when all of the following are true:

- `graphimo-pytorch` installs separately from Graphimo Core;
- Core works without PyTorch installed;
- a project can create/open a `pytorch.model` MetaBlock;
- the component palette changes contextually inside the MetaBlock;
- the V1 element catalog can represent MLP, attention, and the Llama acceptance architecture;
- shapes/types propagate visually before runtime;
- invalid shape connections produce clear diagnostics;
- Groups and repeated Groups work, including `LlamaDecoderLayer x N`;
- model state persists in readable addon JSON and layout remains in Graphimo layout state;
- generated Python is readable and runnable;
- MetaBlock materialization returns `PyTorchModelDefinition`;
- external normal Graphimo/marimo code can instantiate/train/infer with the model;
- runtime bindings are produced at compile time;
- live activation and output-gradient inspection works by subscription;
- scalar loss/custom metrics route to the correct MetaBlock/runtime;
- TorchLens deep inspection targets a selected Graphimo element through the binding map;
- TorchLens is not continuously tracing ordinary training;
- checkpoint ArtifactStore integration works;
- architecture edits create a new revision rather than mutating active training instances;
- conservative weight migration reports are available;
- scaled Llama numerical reference tests pass;
- the standalone Workbench can perform the MLP and Llama acceptance scenarios without Zeron;
- removing the addon leaves Graphimo usable and preserves MetaBlock state;
- reinstalling the addon restores the MetaBlock without data loss.

---

# 61. Explicitly out of V1

Codex must not add these features while implementing this contract unless a later specification explicitly promotes them:

- arbitrary existing PyTorch source -> perfect editable Graphimo graph;
- universal Python control-flow visualization;
- automatic model surgery during an active backward pass;
- implicit weight resizing/interpolation during migration;
- distributed/FSDP/DeepSpeed training orchestration;
- multi-process/distributed telemetry transport;
- graph-level quantization tooling;
- ONNX/export deployment pipelines;
- mandatory `torch.export`/`torch.compile` execution;
- automatic capture of every activation/gradient every batch;
- a generic experiment tracker competing with MLflow/W&B;
- silent automatic checkpoint persistence;
- a PyTorch-specific scheduler in Graphimo;
- PyTorch semantics in Graphimo Core;
- a second foreign canvas UI for the addon.

---

# 62. Reference behavior from external projects/libraries

These are implementation references, not additional sources of truth over this contract.

## Torch Canvas

Torch Canvas demonstrates several UX/product targets relevant to Graphimo-PyTorch:

- visual model editing with generated PyTorch;
- visible tensor shapes on connections;
- shape mismatch feedback;
- reusable/repeated blocks;
- explanation of operations;
- model templates/library including Llama-like architectures;
- training/code export.

Graphimo-PyTorch may borrow these product ideas but must integrate them into Graphimo's own scope/group/addon architecture rather than embedding Torch Canvas as a foreign application.

## PyTorch hooks

Use module `register_forward_hook` for selected live activation observation and removable handles for cleanup. Use explicit tensor gradient hooks/module backward hooks only for subscribed gradient inspection. Avoid global module hooks in normal operation.

## TorchLens

TorchLens is used for selective deep traces, gradients and interventions. The addon maps Graphimo logical elements to module paths first; TorchLens does not define that mapping.

---

# 63. Codex final handoff rule

When this document, the main Graphimo implementation contract, and the Graphical UX contract are supplied together, Codex must treat them as one specification set:

1. `Graphimo_CODEX_IMPLEMENTATION.md` defines Graphimo Core/marimo/addon boundaries.
2. `Graphimo_GRAPHICAL_UX_IMPLEMENTATION.md` defines the host-independent graphical product behavior.
3. `Graphimo_PYTORCH_PLUGIN_IMPLEMENTATION.md` defines the complete first MetaBlock addon.

For PyTorch-specific questions, this document wins over generic examples in the other two documents, provided it does not violate the hard Core isolation rules.

Codex must not reinterpret the PyTorch addon as a toy demo. The MLP is the first implementation fixture; the Llama/Torch-Canvas-style architecture is the V1 acceptance target.

---

# Appendix A - Complete MLP semantic-state example

The following example is normative in structure. UUID-like IDs may differ, but the persisted scientific information must be equivalent.

```json
{
  "schema_version": 1,
  "metablock_id": "model_7ab2c1f0",
  "name": "TinyMLP",
  "external_parameters": [],
  "groups": [],
  "entrypoints": [
    {
      "name": "forward",
      "inputs": [
        {
          "id": "in_x",
          "name": "x",
          "tensor": {
            "shape": ["e", 16],
            "dtype": "float32",
            "semantic": "features"
          }
        }
      ],
      "outputs": [
        {"id": "out_y", "name": "y"}
      ],
      "elements": [
        {
          "id": "n_input",
          "kind": "input",
          "name": "x",
          "params": {"method_input": "in_x"},
          "parameter_bindings": {},
          "metadata": {}
        },
        {
          "id": "n_linear_a",
          "kind": "linear",
          "name": "linear_a",
          "params": {
            "in_features": 16,
            "out_features": 32,
            "bias": true,
            "init_std": null
          },
          "parameter_bindings": {},
          "metadata": {}
        },
        {
          "id": "n_relu",
          "kind": "relu",
          "name": "relu",
          "params": {},
          "parameter_bindings": {},
          "metadata": {}
        },
        {
          "id": "n_linear_b",
          "kind": "linear",
          "name": "linear_b",
          "params": {
            "in_features": 32,
            "out_features": 4,
            "bias": true,
            "init_std": null
          },
          "parameter_bindings": {},
          "metadata": {}
        },
        {
          "id": "n_output",
          "kind": "output",
          "name": "y",
          "params": {"method_output": "out_y"},
          "parameter_bindings": {},
          "metadata": {}
        }
      ],
      "edges": [
        {
          "id": "e1",
          "source": {"element_id": "n_input", "port": "out"},
          "target": {"element_id": "n_linear_a", "port": "x"}
        },
        {
          "id": "e2",
          "source": {"element_id": "n_linear_a", "port": "out"},
          "target": {"element_id": "n_relu", "port": "x"}
        },
        {
          "id": "e3",
          "source": {"element_id": "n_relu", "port": "out"},
          "target": {"element_id": "n_linear_b", "port": "x"}
        },
        {
          "id": "e4",
          "source": {"element_id": "n_linear_b", "port": "out"},
          "target": {"element_id": "n_output", "port": "x"}
        }
      ],
      "group_instances": []
    }
  ],
  "metadata": {}
}
```

The compiler must infer:

```text
x             e x 16
linear_a out  e x 32
relu out      e x 32
linear_b out  e x 4
y             e x 4
```

A renderer may show `e` as `batch` in explanatory text, but the semantic symbolic shape remains stable.

---

# Appendix B - Expected generated MLP source

The generated source does not need to be byte-identical, but its semantics and readability should be equivalent to:

```python
# Generated by graphimo-pytorch
# graphimo-metablock-id: model_7ab2c1f0
# graphimo-graph-revision: ...
# graphimo-materialization-key: ...

import torch
import torch.nn as nn


class TinyMLP(nn.Module):
    def __init__(self):
        super().__init__()
        self.linear_a = nn.Linear(16, 32, bias=True)  # graphimo: n_linear_a
        self.relu = nn.ReLU()                         # graphimo: n_relu
        self.linear_b = nn.Linear(32, 4, bias=True)   # graphimo: n_linear_b

    def forward(self, x):
        x = self.linear_a(x)
        x = self.relu(x)
        x = self.linear_b(x)
        return x
```

`RuntimeBindingMap` must include:

```text
n_linear_a -> linear_a
n_relu     -> relu
n_linear_b -> linear_b
```

Input/output boundary elements do not require module sites.

---

# Appendix C - Parent Graphimo model cell example

A materialized MetaBlock appears in the parent marimo notebook as one Graphimo-owned cell body. Example:

```python
# Graphimo metablock: pytorch.model
# Graphimo instance: 7ab2c1f0
g_tiny_mlp_7ab2c1f0 = graphimo_pytorch.materialize_definition(
    project_file=__file__,
    metablock_id="model_7ab2c1f0",
    revision="4e0c...",
)
```

If `hidden` is promoted to an external Graphimo control:

```python
# Graphimo metablock: pytorch.model
# Graphimo instance: 7ab2c1f0
g_tiny_mlp_7ab2c1f0 = graphimo_pytorch.materialize_definition(
    project_file=__file__,
    metablock_id="model_7ab2c1f0",
    revision="4e0c...",
    hidden=g_slider_a81f2c40.value,
)
```

The user never edits these generated arguments manually in normal use. The Graphimo application layer updates them through the same canonical source-editing path used for ordinary block instances.

The outer MetaBlock output preview should be concise, for example:

```text
PyTorchModelDefinition
TinyMLP
revision 4e0c...
params: 676
```

When materialization fails because the internal graph is invalid, the parent cell must produce a structured diagnostic rather than a partially valid model definition.

---

# Appendix D - External training example

A normal Graphimo `@node` may train the model without knowing about internal Graphimo-PyTorch graph structure:

```python
from graphimo import node
import torch
import graphimo_pytorch


@node
def train_classifier(
    model_def: graphimo_pytorch.PyTorchModelDefinition,
    x: torch.Tensor,
    y: torch.Tensor,
    *,
    steps: int = 100,
    lr: float = 3e-4,
):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model_def(device=device)
    x = x.to(device)
    y = y.to(device)

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)

    for step in range(steps):
        optimizer.zero_grad(set_to_none=True)
        logits = model(x)
        loss = torch.nn.functional.cross_entropy(logits, y)
        loss.backward()
        optimizer.step()

        graphimo_pytorch.emit_metric(
            model,
            "train/loss",
            loss,
            step=step,
        )

        if step % 10 == 0:
            graphimo_pytorch.emit_cuda_resources(model, step=step)

    return model
```

Nothing in this function must manually route events to `n_linear_a`. Live activation subscriptions are already attached through the runtime model's binding metadata.

The user may write completely different training code. `emit_metric` is an optional convenience for metrics that cannot be inferred from module hooks.

---

# Appendix E - Exact event examples

## Activation summary

```json
{
  "schema_version": 1,
  "event_id": "evt_01",
  "kind": "activation.summary",
  "timestamp": 1795521000.125,
  "metablock_id": "model_7ab2c1f0",
  "graph_revision": "4e0c...",
  "runtime_instance_id": "runtime_d918c2f1",
  "logical_element_id": "n_linear_a",
  "module_path": "linear_a",
  "group_instance_path": [],
  "step": 42,
  "payload": {
    "shape": [32, 32],
    "dtype": "float32",
    "device": "cuda:0",
    "numel": 1024,
    "finite_fraction": 1.0,
    "mean": 0.0132,
    "std": 0.821,
    "min": -3.71,
    "max": 4.04
  }
}
```

## Gradient summary

```json
{
  "schema_version": 1,
  "event_id": "evt_02",
  "kind": "gradient.summary",
  "timestamp": 1795521000.132,
  "metablock_id": "model_7ab2c1f0",
  "graph_revision": "4e0c...",
  "runtime_instance_id": "runtime_d918c2f1",
  "logical_element_id": "n_linear_a",
  "module_path": "linear_a",
  "group_instance_path": [],
  "step": 42,
  "payload": {
    "shape": [32, 32],
    "dtype": "float32",
    "device": "cuda:0",
    "mean": -0.0008,
    "std": 0.014,
    "min": -0.11,
    "max": 0.09,
    "l2_norm": 0.447
  }
}
```

## Scalar metric

```json
{
  "schema_version": 1,
  "event_id": "evt_03",
  "kind": "metric.scalar",
  "timestamp": 1795521000.140,
  "metablock_id": "model_7ab2c1f0",
  "graph_revision": "4e0c...",
  "runtime_instance_id": "runtime_d918c2f1",
  "logical_element_id": null,
  "module_path": null,
  "group_instance_path": [],
  "step": 42,
  "payload": {
    "name": "train/loss",
    "value": 1.734
  }
}
```

The Workbench must not assume every event has an element target. Model/run-wide metrics use `logical_element_id=null`.

---

# Appendix F - Repeated Group binding example

For a semantic Group instance:

```json
{
  "id": "gi_layers",
  "group_id": "g_decoder",
  "name": "layers",
  "repeat": 32,
  "params": {}
}
```

and a logical child element:

```json
{
  "id": "n_qproj",
  "kind": "linear",
  "name": "q_proj",
  "params": {
    "in_features": 4096,
    "out_features": 4096,
    "bias": false,
    "init_std": 0.02
  }
}
```

produce runtime sites conceptually equivalent to:

```python
[
    RuntimeSite(
        logical_element_id="n_qproj",
        module_path="layers.0.q_proj",
        group_instance_path=(GroupRuntimeIndex("gi_layers", 0),),
        inspectable=True,
    ),
    RuntimeSite(
        logical_element_id="n_qproj",
        module_path="layers.1.q_proj",
        group_instance_path=(GroupRuntimeIndex("gi_layers", 1),),
        inspectable=True,
    ),
    ...
]
```

The Workbench uses `logical_element_id=n_qproj` to select the displayed node and `repetition_index` to select one concrete runtime site.

Nested repeated Groups extend `group_instance_path` rather than flattening identity into a fragile string.

---

# Appendix G - Exact semantic edit operations

The Graphimo application layer should expose PyTorch-addon edits as semantic operations so undo/redo and persistence remain coherent.

The exact generic host transaction system is owned by Graphimo, but the addon needs operations equivalent to:

```text
CreateElement
DeleteElement
UpdateElementParameter
RenameElementSemanticName
CreateEdge
DeleteEdge
CreateGroupDefinition
CreateGroupInstance
UpdateGroupRepeat
MoveElementsIntoGroup
PromoteParameter
DemoteParameter
CreateMethodGraph
DeleteMethodGraph
InsertTemplate
```

Every operation:

1. starts from a known graph revision;
2. validates requested IDs/parameters;
3. creates a new immutable semantic state value;
4. validates enough invariants to reject obviously invalid destructive mutations;
5. saves atomically;
6. yields a new revision;
7. tells the parent Graphimo application that the MetaBlock materialization source must update to the new revision;
8. leaves layout operations to the general Graphimo layout transaction system.

Do not let renderer code mutate JSON dictionaries directly.

---

# Appendix H - Semantic rename versus visual label

PyTorch models have module names that affect `state_dict` keys and checkpoint compatibility. Therefore two naming concepts are required:

```text
semantic name: q_proj
visual label: Query projection
```

Changing only the visual label:

- does not change generated module path;
- does not change `state_dict` key names;
- does not change graph revision if visual label lives in layout/presentation metadata outside scientific state.

Changing the semantic name:

- changes generated source/module path;
- changes graph revision;
- may break checkpoint exact-key compatibility;
- must display a warning when saved checkpoints/current runtime instances exist.

The inspector must clearly distinguish these actions.

---

# Appendix I - Parameter edit behavior matrix

| Parameter example | Structural/recompile | Existing runtime changed? | Checkpoint key shape risk | UI behavior |
| --- | --- | --- | --- | --- |
| Linear `out_features` | yes | no | high | new revision; shape propagation |
| Linear `bias` | yes | no | medium | new revision |
| Dropout `p` | yes definition change | no | low | new revision |
| RMSNorm `eps` | yes definition behavior | no | none for shape | new revision |
| `n_heads` | yes | no | high | new revision; divisibility checks |
| RoPE `base` | yes model behavior | no | no parameter-key change | new revision |
| visual x/y | no | no | none | layout only |
| visual label | no | no | none | presentation only |
| semantic module name | yes | no | key rename | new revision + warning |

Graphimo-PyTorch never mutates an existing runtime instance merely because the persisted MetaBlock was edited.

---

# Appendix J - Checkpoint manifest example

A checkpoint payload stored in an Artifact may use a dictionary equivalent to:

```python
{
    "format": "graphimo-pytorch-checkpoint",
    "format_version": 1,
    "metablock_id": "model_7ab2c1f0",
    "graph_revision": "4e0c...",
    "materialization_key": "9c13...",
    "model_name": "TinyMLP",
    "external_parameters": {},
    "pytorch_version": torch.__version__,
    "addon_version": graphimo_pytorch.__version__,
    "model_state_dict": model.state_dict(),
    "optimizer_state_dict": optimizer.state_dict() if optimizer else None,
    "scheduler_state_dict": scheduler.state_dict() if supported else None,
    "metadata": {...},
}
```

The Artifact itself is content-addressed by Graphimo's storage backend. The checkpoint dictionary does not invent a second content-addressable store.

When loading, validate the manifest before applying any state dictionary.

---

# Appendix K - Runtime revision mismatch UX

If the user edits the MetaBlock while an older model runtime is active, the Workbench should expose the distinction directly:

```text
Current graph revision: 9c31...
Active runtime:         4e0c...   [older revision]
```

Selecting a node shows live data only from the selected runtime revision.

The user may have multiple runtimes:

```text
Runtime A - revision 4e0c - training
Runtime B - revision 9c31 - evaluation
```

The runtime selector is therefore not merely a debugging detail. It is necessary for correct model inspection during iterative research.

---

# Appendix L - Workbench parameter/control examples

## Structural control promoted from MetaBlock

A PyTorch MetaBlock exposes `n_heads` as a parent input.

Parent Graphimo scope:

```text
Number Control: 32
      |
      v
Llama Model [n_heads]
      |
      v
Train
```

Changing `32 -> 16`:

1. changes the normal Graphimo control value;
2. marimo re-evaluates the MetaBlock materialization cell;
3. the addon resolves `n_heads=16`;
4. shape inference runs;
5. a new `materialization_key` is created;
6. if `d=4096`, `dh=256` is shown;
7. downstream training/evaluation becomes stale according to marimo mode;
8. currently active `n_heads=32` runtime remains untouched.

This is the preferred way to connect Graphimo's general interactive controls to structural PyTorch parameters without putting marimo controls inside the PyTorch compiler itself.

---

# Appendix M - Live inspector rendering rules

The PyTorch inspector should separate **static**, **materialized**, and **live** information.

Example:

```text
Linear - q_proj

Static definition
  4096 -> 4096
  bias: false
  init: Normal(std=0.02)

Materialized site
  runtime: runtime_d918c2f1
  module: layers.17.q_proj
  revision: 4e0c...

Latest activation
  shape: 8 x 64 x 4096
  dtype: float16
  mean: 0.013
  std: 0.82
  finite: 100%

Latest gradient
  L2 norm: 0.447
  max abs: 0.11

[activation histogram]
[gradient histogram]

Actions
  Deep inspect with TorchLens
  Save activation snapshot
  Stop live inspection
```

If no runtime exists, only static/inferred information is shown and live actions explain that a model must be instantiated/executed first.

---

# Appendix N - TorchLens deep-inspection Workbench flow

Required V1 interaction:

1. user selects an internal element;
2. user chooses a concrete runtime instance and repetition index if required;
3. user clicks **Deep inspect**;
4. Workbench asks for/provides explicit sample inputs from an available Graphimo value or selected test fixture;
5. addon resolves the selected `RuntimeSite.module_path`;
6. addon runs TorchLens on the model with those inputs;
7. inspector displays selected-site information and bounded nearby operation metadata;
8. optional gradient inspection requires either a supplied `loss_fn` or a known scalar method/result;
9. intervention controls (`zero ablate`, `scale`) run a fresh explicit inspection experiment;
10. the result is labeled as a modified/intervention run;
11. no training model state is silently mutated;
12. **Save inspection** persists an explicit Artifact.

The UI must not imply that a TorchLens rerun is the same forward pass as the current live training batch unless it actually is.

---

# Appendix O - Model parameter counting and summaries

`PyTorchModelDefinition` and the MetaBlock outer card should expose a parameter summary after materialization without forcing a forward pass.

Required values:

```text
total_parameters
trainable_parameters
parameter_bytes by current/default dtype when known
```

For a large model template, this lets the user immediately see the effect of changing width/layers/heads.

The inspector may also show per-element/group parameter counts by summing generated module parameters associated with runtime bindings.

Do not instantiate the full 7B-scale Llama on GPU merely to display a parameter estimate when a static formula can be computed. The template may provide static parameter-count estimates; an actual materialized small/CPU model can validate them in tests.

---

# Appendix P - Large-model safety

The shipped full Llama dimensions are useful as an architecture/template target but must not accidentally allocate multi-gigabyte model weights when the user merely inserts the template.

Rules:

- inserting/editing a MetaBlock performs static graph/shape work only;
- generated source does not execute model construction merely to display code;
- `materialize_definition()` creates a factory/definition, not necessarily the full module instance;
- the full `nn.Module` is allocated only when external code explicitly calls `definition.instantiate()`/`definition()`;
- Workbench must warn before an obviously huge model is instantiated locally when a static parameter-memory estimate exceeds a configurable threshold;
- acceptance tests use scaled Llama dimensions.

This rule is essential to make large architecture editing safe on laptops.

---

# Appendix Q - Dtype/device behavior

The semantic graph describes architecture, not a fixed deployment device.

By default generated module constructors create standard CPU parameters using PyTorch defaults.

`PyTorchModelDefinition.instantiate(device=..., dtype=...)`:

1. constructs the model;
2. moves/casts using `model.to(device=device, dtype=dtype)` where safe;
3. registers runtime metadata after construction;
4. preserves integer/buffer semantics according to PyTorch `.to()` behavior.

Element semantic state should not hard-code `cuda:0`.

A future device-placement dialect feature may be added later; it is out of V1.

---

# Appendix R - Randomness and deterministic tests

The addon does not own global training determinism, but compiler tests must be reproducible.

Tests may use:

```python
torch.manual_seed(...)
```

before constructing both reference and generated models.

Generated model code must not call `manual_seed` internally unless a future explicit initialization-seed feature is introduced.

Checkpoint helpers may record RNG state only if/when the external training helper explicitly requests resumable-training state. Basic model checkpoints do not need to snapshot every Python/NumPy RNG automatically.

---

# Appendix S - Source formatting stability

Generated source formatting is deterministic enough for snapshot tests.

Use a consistent formatter/AST-unparser strategy. Do not make output depend on dictionary hash order.

Snapshot tests should assert meaningful stable sections rather than pinning irrelevant whitespace if a formatter version may change.

Semantic names and generated class/method organization are part of the readable-source contract.

---

# Appendix T - Exact test commands

From repository root, Codex should provide commands equivalent to:

```bash
pytest -q addons/pytorch/tests/test_registration.py
pytest -q addons/pytorch/tests/test_schema_roundtrip.py addons/pytorch/tests/test_revision.py
pytest -q addons/pytorch/tests/test_graph_validation.py addons/pytorch/tests/test_shapes.py
pytest -q addons/pytorch/tests/test_compile_basic.py addons/pytorch/tests/test_compile_groups.py
pytest -q addons/pytorch/tests/test_runtime_bindings.py addons/pytorch/tests/test_live_hooks.py addons/pytorch/tests/test_gradients.py
pytest -q addons/pytorch/tests/test_checkpoints.py addons/pytorch/tests/test_torchlens.py
pytest -q addons/pytorch/tests/test_templates_llama.py
```

A final addon gate runs:

```bash
pytest -q addons/pytorch/tests
ruff check addons/pytorch/graphimo_pytorch addons/pytorch/tests
```

The root project gate additionally runs Graphimo Core/Workbench tests with and without the addon installed according to the main implementation contract.

---

# Appendix U - CI matrix guidance

Minimum automated CI variants:

```text
Core only, no graphimo-pytorch
Core + graphimo-pytorch CPU
Core + graphimo-pytorch + TorchLens
```

At least one supported Python version required by both Graphimo and TorchLens must be tested. Wider Python/PyTorch compatibility can be added without changing semantics.

CUDA behavior should have a dedicated environment when infrastructure exists; CPU CI remains authoritative for the core addon contract except explicitly CUDA-specific metrics.

---

# Appendix V - Compatibility with `torch.compile`

The V1 guarantee is eager execution.

If users call `torch.compile` on an instantiated model, normal numerical execution may work, but Graphimo live hooks/TorchLens behavior is not guaranteed by this contract.

The inspector should identify a compiled/wrapped model when reliably detectable and may explain that deep inspection requires an eager instance.

Do not silently decompile or replace a user model in arbitrary training code.

A later version may deliberately integrate TorchLens's current eager-force behavior for compiled models after dedicated validation.

---

# Appendix W - Plugin isolation acceptance after development

The final packaging check must execute this lifecycle:

```text
1. create project with PyTorch MetaBlock
2. save semantic graph/layout
3. close project
4. uninstall graphimo-pytorch
5. open project in Graphimo Core/Workbench
6. see unavailable-addon placeholder; no state lost
7. close project
8. reinstall exact addon version
9. reopen project
10. MetaBlock graph appears with same IDs, parameters, layout and revision
```

Graphimo Core should not import torch while the addon is absent.

This is a conformance requirement of the packaging boundary, not an architecture experiment.

---

# Appendix X - Final Codex checklist before marking the addon complete

Codex must be able to answer **yes** to every item:

- Is `graphimo-pytorch` independently installable?
- Can Graphimo start with no PyTorch addon?
- Is PyTorch semantic state separate from generic layout?
- Does every scientific edit change graph revision deterministically?
- Can the MLP be created entirely from the Workbench?
- Can shapes be seen before execution?
- Can incompatible connections be diagnosed before execution?
- Is generated Python readable?
- Does the generated model numerically execute?
- Are bindings generated at compile time, not inferred after the fact?
- Can one visual element map to many repeated runtime sites?
- Can the user select a repetition index?
- Are forward activation hooks subscription-only?
- Are gradients observed without changing them?
- Are payloads bounded?
- Can loss metrics be emitted from external user training code?
- Does the MetaBlock remain separate from the training scheduler?
- Can TorchLens target a selected Graphimo element through module path mapping?
- Is TorchLens deep inspection explicit rather than continuous?
- Can checkpoints be stored through Graphimo ArtifactStore?
- Does a revision mismatch refuse strict loading?
- Is weight migration conservative and reported?
- Does structural editing leave active old runtimes untouched?
- Can the Llama Decoder Layer be represented as a reusable Group?
- Can the model show `LlamaDecoderLayer x 32` without duplicating the visual group 32 times?
- Do the `forward`, `loss`, and `predict` method graphs compile correctly for the scaled reference fixture?
- Can a full-size Llama template be edited without allocating the model?
- Can the addon be removed/reinstalled without state loss?
- Is there zero PyTorch-specific semantic branching in Graphimo Core?

If any answer is no, the V1 PyTorch addon is incomplete.

---

# Appendix Y - External technical references consulted

This contract is grounded in the current documented mechanisms below. These references inform implementation details; the Graphimo contract remains normative for product behavior.

## PyTorch `nn.Module` hooks

Official PyTorch documentation documents:

- `register_forward_hook(...)` called after a module `forward()` computes output;
- `register_full_backward_hook(...)` for module backward observation;
- removable hook handles;
- warnings around global hooks and in-place mutation.

Reference: `https://docs.pytorch.org/docs/stable/generated/torch.nn.Module`

## PyTorch export/compiler notes

`torch.export` produces an ahead-of-time tensor graph and supports symbolic/dynamic dimensions through `Dim`, but this addon does not use it as its V1 live inspection source of truth.

References:

- `https://docs.pytorch.org/docs/stable/export.html`
- `https://docs.pytorch.org/docs/stable/user_guide/torch_compiler/export/api_reference.html`

## TorchLens

Current TorchLens documentation/repository describes:

- tracing/selecting operations and modules by module path;
- activation and gradient capture;
- module selectors;
- interventions such as zero ablation and scaling;
- current package version `2.34.1` in its project metadata at the time this specification was written;
- eager PyTorch as the full-feature backend;
- current incompatibility of normal TorchLens interception with `torch.export`/TorchScript forwards.

References:

- `https://github.com/johnmarktaylor91/torchlens`
- `https://github.com/johnmarktaylor91/torchlens/blob/main/docs/intervention_api.md`
- `https://github.com/johnmarktaylor91/torchlens/blob/main/docs/for-ai-agents.md`

## Torch Canvas

Torch Canvas is used as a product/UX reference for:

- diagram-to-PyTorch editing;
- visible tensor shapes and mismatch highlighting;
- reusable/repeated blocks;
- operation explanations;
- model templates/library including Llama-like models;
- generated training/code artifacts.

Reference: `https://github.com/JesseTNRoberts/torch-canvas`

Graphimo-PyTorch must not embed Torch Canvas as a foreign UI. The relevant ideas are implemented through Graphimo's own MetaBlock/group/canvas system.
