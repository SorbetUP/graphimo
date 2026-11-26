# PyTorch addon

The PyTorch addon is a separate `graphimo-pytorch` distribution. Graphimo Core
discovers it through the `graphimo.metablocks` entry-point group; Core does not
interpret PyTorch elements or model state. The addon package is under
`addons/pytorch/graphimo_pytorch`.

## Semantic state schema

`graphimo_pytorch.schema` defines immutable V1 dataclasses and JSON helpers:

- `PyTorchMetaBlockState` holds the schema version, MetaBlock identity and name,
  external parameters, reusable groups, method entrypoints and semantic
  metadata. V1 requires a `forward` method.
- `MethodGraph` holds ordered inputs, outputs, elements, edges and Group
  instances. `loss` and `predict` are supported as additional entrypoints.
- `GroupDefinition` holds its ports, elements, edges and nested Group
  instances. `GroupInstance.repeat` must be at least one.
- `ElementRecord` preserves the addon-owned `kind`, parameters, external
  parameter bindings and JSON-safe metadata. Unknown kinds round-trip unchanged;
  support checks and materialization are the registry/compiler's responsibility.
- `ExternalParameter` supports `int`, `float`, `bool` and `str` defaults.
- `Diagnostic` carries a stable code, message, severity and optional graph
  identity fields. `PyTorchSchemaError.code` carries stable schema error codes.

The specification names `MethodInput`, `MethodOutput` and `GroupPort` without
defining their fields. This implementation uses the minimal tensor contract
`MethodInput(name, shape, dtype, semantic=None)`, plus named endpoint references
for `MethodOutput` and `GroupPort`. A shape dimension is a positive integer or a
symbolic string. This is a schema representation choice, not a product decision;
shape inference and compatibility validation belong to the later registry
milestone.

The P2 port convention is: ordinary unary elements use `input` and `output`;
`add`, `multiply` and `matmul` use `left` and `right`; `cross_entropy` uses
`logits` and `target`; `concatenate` uses variadic `input`; a Group instance
uses the named ports declared by its referenced Group. A visual `input` element
matches its `name` parameter to a `MethodInput` of the same name. These names
fill in the port identity that the schema's conceptual definitions leave open.

Use `load_state(path)` and `save_state(path, state)` for project sidecars.
Writes are UTF-8, indented, flushed, fsynced where supported, and atomically
replaced. A schema version other than `1` raises
`PyTorchSchemaError(code="pytorch_state_schema_unsupported")`. Invalid JSON or
invalid state raises `pytorch_state_invalid`.

`canonical_state_json(state)` emits deterministic semantic JSON, and
`graph_revision(state)` is its SHA-256 digest. Canvas/editor metadata such as
layout, renderer state, selection, zoom and pan is omitted from the serialized
semantic metadata and revision; Graphimo's presentation store owns that data.
Ordered scientific lists remain ordered, so element, edge, method and repeat
changes affect the revision.

## Importing an existing MetaBlock

The Zeron **Import PyTorch state** action accepts a Graphimo-PyTorch semantic
JSON state or a `pytorch.model` sidecar envelope. It creates a new MetaBlock
sidecar in the active project, assigns the new project-local MetaBlock ID, and
preserves the imported name and graph. Import input is limited to 3 MiB.
Existing project sidecars are reopened through the normal MetaBlock list.
Arbitrary Python `nn.Module` source is not parsed into a visual graph; the
addon returns `pytorch_import_unsupported` with an explanation instead of
creating a partial model.

## Current milestone boundary

P1 supplies the semantic schema. P2 adds the exact V1 element catalog,
structured validation, symbolic tensor shapes and shape propagation.
`validate_model_graph(state, external_values)` reports
structural, port, cycle, parameter and binding diagnostics. The normative error
codes include `pytorch_unknown_element_kind`, `pytorch_unknown_port`,
`pytorch_edge_invalid`, `pytorch_cycle_invalid`,
`pytorch_group_cycle_invalid`, `pytorch_param_invalid`,
`pytorch_external_param_missing`, `pytorch_shape_incompatible` and
`pytorch_shape_divisibility_error`. Each diagnostic carries the MetaBlock ID;
method, element, Group and edge identities are attached when known.

`infer_model_shapes(state, external_values)` returns `ShapeInferenceResult`:
`port_shapes` and `edge_shapes` map scoped IDs to `TensorSpec`, with validation
and shape diagnostics included. It uses bounded exact symbolic matching,
simple products/quotients, broadcast rules and concrete shape constraints. It
does not construct a PyTorch model or execute user code.

P3 adds `compile_model(state, external_values)`. It validates and infers before
generating trusted, deterministic Python source, compiles that source in a
private addon namespace, and returns a factory, runtime-site bindings, shape
results and materialization key. Compilation refuses graphs with error
diagnostics. Generated source uses normal `torch`, `torch.nn` and
`torch.nn.functional` APIs plus inspectable wrappers for functional operations;
it contains no scheduler, Workbench or TorchLens code. MLP and repeated Group
models are executable, and registration now materializes these semantic graphs
as `PyTorchModelDefinition` values. Until the compiler supports every kind in a
state, unknown kinds must remain non-materializable.

## P4 Workbench projection and editing

`graphimo_pytorch.workbench_projection.project_model()` creates a renderer-
neutral JSON view model from the semantic state and static inference results.
It provides the addon palette, method tabs, outer ports, internal elements,
parameter descriptors, edge/output tensor shapes, validation diagnostics,
Group templates/instances and the generated readable Python source when the
graph compiles. It contains no `nn.Module` objects. For a Group scope, inferred
shapes are taken from its saved model call sites; uninstantiated Groups have no
runtime-specific shape preview.

The PyTorch dialect accepts semantic `metablock.apply` intents for adding,
renaming, deleting and parameterizing elements, connecting/disconnecting edges,
creating/deleting Group instances and changing repetition counts. The host
application persists state after successful projection. Incomplete graphs may
be saved and display diagnostics; malformed parameter values are rejected
before persistence.

The standalone Workbench is launched with
`python -m graphimo.workbench --project /path/to/notebook.py`. It is a local
renderer over `GraphimoApplication`, serves only on loopback, and persists
canvas geometry through the generic Core layout actions. It does not attach to
an active marimo session: the materialization action previews the generated
cell source and says explicitly that it was not inserted. The addon returns a
canonical materialization-cell specification that recreates the
`PyTorchModelDefinition` inside the active marimo Python process. Connecting
that specification to an active marimo session belongs to the later persistent
service and Zeron-adapter milestones; this Workbench limitation does not add a
second scheduler or keep a model object in the renderer process.

## P5 process-local runtime and live telemetry

Every `PyTorchModelDefinition.instantiate()` creates fresh private runtime
metadata and registers its model in the addon's weak `RuntimeRegistry`. The
registry does not keep models alive; garbage collection removes entries and
emits `runtime.released`. `release_runtime_model(model)` removes active hooks,
subscriptions and registry metadata explicitly. Multiple instances from one
MetaBlock coexist and have different `runtime_instance_id` values.

`RuntimeSite` bindings include a concrete `module_path` and a typed
`group_instance_path` of `GroupRuntimeIndex(group_instance_id,
repetition_index)` values. The binding map produced during compilation remains
the authority for mapping a visual element to the live module.

`graphimo_pytorch.runtime` exposes `SubscriptionSpec`, `summarize_tensor`,
`summarize_tree`, `emit_metric`, `emit_cuda_resources` and the event/registry
types. Activation and output-gradient hooks are installed only for subscribed
modules. They are shared by subscriptions on the same module and removed when
the last subscription ends. Gradient observation uses output tensor hooks and
returns each gradient unchanged. Inspection failures produce bounded warning
events and do not fail the model forward/backward pass.

Tensor summaries do not retain tensor objects. They report metadata and safe
scalar statistics; histogram bins are capped at 128 (default 64), and samples
at 4096 values. CUDA summaries remain on-device until bounded scalar values
are read. CPU-only models make no CUDA calls. The event bus is process-local,
JSON-safe and bounded to 10,000 events. Its cursor is scoped per MetaBlock and
reports dropped records after eviction.

Addon actions `runtime.list`, `runtime.subscribe`, `runtime.unsubscribe` and
`runtime.release` run through `metablock.invoke`; `metablock.events` drains the
per-MetaBlock event stream. The standalone Workbench shows active runtimes
registered in its own Python process and can subscribe to activation summary,
activation histogram/sample and output-gradient summary events. It shows the
actual output shape and module parameter count when a matching event/runtime
binding exists.

The registry and event bus are process-local. The standalone Workbench does
not attach to the marimo execution process, so it cannot show or subscribe to
runtimes instantiated only in a separate marimo process. The Core spec leaves
the cross-process telemetry transport unspecified; this implementation does
not invent one. A host adapter that shares the Python process can use the same
application actions today; marimo-to-Workbench live routing remains pending
that product/architecture decision.

## P6 checkpoints and conservative migration

`save_checkpoint(model, optimizer=None, scheduler=None, metadata=None)` returns
an `ArtifactRef` of kind `pytorch-checkpoint`. It stores `state_dict()` values,
runtime provenance, the resolved external parameters, PyTorch/addon versions,
optional optimizer and safely representable scheduler state, and JSON user
metadata through Graphimo's ArtifactStore. It never serializes an arbitrary
model object. The helper rejects custom non-tensor model state and unsupported
optimizer/scheduler values before writing an artifact.

`load_checkpoint(model_def, artifact, strict=True)` restores a matching graph
revision after validating tensor names and shapes before copying weights. A
revision mismatch fails with
`CheckpointError(code="pytorch_checkpoint_revision_mismatch")`. Matching graph
revisions also compare materialized tensor names/shapes, so structural
external-parameter changes cannot partially overwrite a model. `strict=False`
uses `migrate_state_dict` and attaches a `WeightMigrationReport` to the returned
model; `get_migration_report(model)` returns it. Optimizer state is preserved
in the artifact but is never migrated automatically.

`migrate_state_dict(source_state, target_model)` copies only exact-name,
exact-shape tensors. It reports `copied`, `missing_in_source`,
`missing_in_target` and each `ShapeMismatch`; it never slices, interpolates or
resizes weights. `preview_checkpoint_migration(model_def, artifact)` creates a
fresh temporary target model and returns the report without changing a live
runtime. `inspect_checkpoint(artifact)` returns provenance without allocating
the model weights in host memory.

The Workbench can inspect an artifact ID, compare checkpoint graph revision
with the current graph, and explicitly request an exact-shape migration
preview. Save at a chosen training boundary by calling `save_checkpoint` from
normal Python code; pass the optimizer/scheduler when their state should be
included. After reviewing a migration report, call
`load_checkpoint(..., strict=False)` explicitly to create the migrated model.
The Workbench never treats a preview as an applied migration.

## P7 explicit TorchLens inspection

TorchLens is a selective deep-inspection path, separate from per-batch live
telemetry. The public API follows the addon contract:

```python
from graphimo_pytorch import TorchLensRequest, inspect_with_torchlens

request = TorchLensRequest(
    runtime_instance_id="...",
    logical_element_id="...",
    repetition_index=0,  # only needed when the binding is repeated
    capture_gradients=False,
)
result = inspect_with_torchlens(request, input_tensor)
```

Inputs are explicit. The authoritative binding map resolves a logical element
and repetition to its generated module path. Inspection runs on a copy of the
eager model so it does not change the live training instance. The result
contains bounded activation/gradient statistics, up to 32 relevant operation
summaries, non-finite diagnostics and trace metadata; the full TorchLens Trace
is transient and never added to project JSON. Gradient capture uses
`loss_fn(output)` when supplied through the Python API, or a scalar tensor
model output.

`TorchLensIntervention("zero_ablation")` and
`TorchLensIntervention("scale", factor=0.5)` run an explicit experimental
capture. The result is labeled `experimental` and identifies the intervention.
TorchLens edits activations during the copied-model pass; Graphimo graph
semantics and model weights are unchanged.

The Workbench offers a JSON fixture editor on selected inspectable runtime
elements. A nested numeric array is one tensor, an object is passed as keyword
arguments, and `{"$args": [...], "$kwargs": {}}` represents multiple
positional/keyword inputs. The fixture includes a tensor dtype selector so
integer JSON values can be used as floating point inputs or as token IDs.
Workbench gradient capture requires a scalar output. The explicit **Save full
trace artifact** action performs a fresh inspection, captures all activations,
and stores an export bundle in ArtifactStore: complete TorchLens operation
metadata as JSON and activation/gradient tensors in a tensor-only PyTorch file,
along with the bounded inspector report and provenance manifest. The manifest records MetaBlock ID, graph revision,
materialization key, runtime ID, selected element/module, and TorchLens/PyTorch
versions. Inspection is never silently equated with a current or next training
batch.

If TorchLens is missing or broken, the action returns a clear diagnostic.
Compiled or wrapped models are not silently substituted; V1 deep inspection
requires an eager, copyable model.

## P8 attention and Llama templates

`graphimo_pytorch.templates` exposes `LlamaConfig`,
`build_self_attention_group`, `build_llama_decoder_group`,
`build_llama_state` and `create_template_state`. The Workbench template picker
creates a reusable Self Attention Group, a standalone decoder Group, the
normative full-size `LlamaForCausalLM` editing fixture, or a runnable reduced
fixture (`vocab=128`, `d=32`, `heads=4`, `ff=64`, `layers=2`, `sequence=8`).
Creating or projecting the full template compiles its class definition and
static shapes without constructing or allocating the model's weights.

The Llama semantic state has one `LlamaDecoderLayer` Group definition and one
repeated Group instance. Compilation specializes the addon's generated Python
module layout for this template, preserving reference module names and runtime
paths such as `layers.17.q_proj`. The canvas still contains one logical Group;
the Workbench selects a repetition through the binding map. External dimensions
are structural parameters, so changing `d_model`, `n_heads`, `ff_size` or
`n_layers` revises generated shapes/source; divisibility errors are reported
before model construction. The `predict` graph records a method-call semantic
annotation on its forward-result node and generates `@torch.no_grad()` plus
`self.forward(input_ids)`.

The addon also exports `train_pytorch_model`, a normal function decorated with
Graphimo Core's `@node` marker. It does not own graph scheduling or start a
second execution engine:

```python
from graphimo_pytorch import train_pytorch_model

model, summary, checkpoint = train_pytorch_model(
    model_def,
    token_batches,
    epochs=1,
    lr=3e-4,
    persist_checkpoint=False,
)
```

The helper instantiates the definition, uses AdamW, emits `train/loss`, and can
optionally emit gradient norm and rate-limited CUDA resource metrics. A
checkpoint ArtifactStore write occurs only when `persist_checkpoint=True`; the
returned value is an `ArtifactRef` or `None`. `batches` accepts a re-iterable,
a per-epoch provider, token batches, mappings with `input_ids`, or
input/target pairs for a supplied `loss_fn`. It is a convenience component for
ordinary Python/marimo execution; user code can continue to own the loop.

The Workbench template selector includes full and reduced Llama scenarios.
Runtime scalar metrics retain up to 5000 points per runtime/metric with
decimation and draw a bounded history sparkline in the inspector. The full
template remains a code-generation and editing fixture; tests and interactive
training use the reduced fixture.

## P9 integration hardening

The Core-only subprocess test blocks imports of both `torch` and `torchlens`
while importing and opening Graphimo Core. The addon lifecycle test builds a
wheel, installs it into an isolated target, creates/edits a MetaBlock and
layout, projects that saved record with an empty addon entry-point set, verifies
the sidecar bytes and layout are unchanged, then reopens through fresh
discovery and checks the same revision, model name and layout before
materialization/checkpoint use. This exercises the unavailable-addon manager
path without uninstalling the developer's active Python environment.

Runtime conformance also fakes the monotonic clock at a 2 Hz activation
subscription: three ordinary forwards still return normally while the
telemetry seam emits only two summaries. Existing bounds tests cover event
history eviction, 128-bin histogram caps and 4096-value sample caps. The addon
wheel lifecycle check confirms its entry point and template/training modules
are in the separately installable distribution. Through `GraphimoApplication`,
that lifecycle also creates and materializes a model, lists and subscribes its
runtime, drains the activation event and explicitly requests TorchLens
inspection. Projection, semantic editing, materialization, runtime actions,
event retrieval and deep inspection therefore share the host application
operation boundary.

The authenticated service integration test adds an agent-style path: a batch
creates and rewires the graph, the service materializes it, the ordinary
training component runs one step, activation telemetry and TorchLens inspection
are requested through service actions, and the checkpoint is inspected and
previewed against a changed revision. Separate import tests exercise raw
semantic JSON, sidecar envelopes, the generic `metablock.create` application
route, and the explicit unsupported-source diagnostic.
