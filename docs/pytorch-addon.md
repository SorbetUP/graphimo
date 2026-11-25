# PyTorch MetaBlock addon

`graphimo-pytorch` lives at `addons/pytorch` and builds as its own Python
distribution. It registers in `graphimo.metablocks`; Graphimo Core does not
import Torch or TorchLens. Package metadata follows the addon contract:
Python 3.11+ (the current Graphimo Core floor), `torch>=2.6`, and
`torchlens==2.34.1`, with no CUDA wheel pinned.

The [2026-11-25 implementation update](../Graphimo_IMPLEMENTATION_UPDATE_2026-11-25.md)
supersedes earlier host-boundary TODOs: the external parent value is
`PyTorchModelDefinition`, and the generic addon host is implemented in U2,
PyTorch integration in U3, and Zeron rendering in U6.

## P1 delivered

- `graphimo_pytorch.registration.registration` declares dialect
  `pytorch.model`, API version 1 and a factory callable.
- The semantic schema includes MetaBlock state, external parameters, method
  graphs, element/edge records, reusable groups and repeated group instances.
- `save_state` writes human-readable sorted UTF-8 JSON atomically;
  `load_state` validates V1; `canonical_state_json` and `graph_revision`
  produce stable SHA-256 revisions from semantic fields.
- Unknown element kinds keep their JSON schema fields and remain serializable.
  The later registry/compiler milestone will diagnose them as unmaterializable.
- Schema and registration imports do not import `torch` or `torchlens`.
- When a semantic JSON file is present but the plugin is absent, the Core Workbench displays its generic opaque placeholder and leaves the file byte-for-byte unchanged.

P1 established the independent schema and entry point. The registration
factory becomes an active host dialect during U2; P1 does not put PyTorch
semantics in Graphimo Core.

## P2 delivered: element registry and shape validation

The addon now exposes its internal V1 catalog through
`graphimo_pytorch.element_kinds()`, `ELEMENT_REGISTRY`, and
`get_element_kind(kind)`. Descriptors declare ports and parameter types,
defaults, bounds, choices, and required values. This registry belongs only to
the PyTorch dialect; it is not the Graphimo Core addon-handler interface.

`validate_model_graph(state, external_values={})` returns a
`ValidationReport` containing all safely discoverable diagnostics. Its
`valid` property is false when any diagnostic has error severity. Validation
covers known element kinds and ports, connected required inputs, IDs and
module names, parameter bindings/ranges, method boundaries, Group references
and cycles, multiple incoming edges and dataflow cycles.

`infer_model_shapes(state, external_values={})` first runs graph validation,
then returns `ShapeInferenceResult` with element output specs, edge specs and
diagnostics. `TensorSpec` contains symbolic/concrete `Dim` values plus dtype
and semantic labels. Static known incompatibilities are errors; distinct
unresolved symbols remain unresolved in V1 rather than being treated as a
proven mismatch. Shape inference does not run PyTorch or call model forward.

The V1 registry and shape rules cover the full catalog in addon contract
§§12–16. Tests include MLP/attention paths, batched matmul, broadcasting,
tensor transforms, RoPE checks, recursive Groups and diagnostic behavior.
The visual Workbench projection remains a later milestone.

## P3 delivered: model compiler

`compile_model(state, external_values={})` validates the semantic state,
resolves external values, infers shapes, then emits readable Python and
compiles that addon-generated source into a fresh-model factory. Invalid
graphs raise `GraphimoPytorchError` with structured diagnostic codes before
source execution. The generated source includes the MetaBlock ID, semantic
revision and materialization key; the key changes when resolved external
values or the addon compiler version changes.

The result exposes `generated_source`, `factory`, `bindings`, `shape_result`,
`diagnostics` and a `PyTorchModelDefinition`. The definition's
`instantiate(device=..., dtype=...)` returns an independent real `nn.Module`
and registers process-local runtime metadata through weak references. Call
`release_runtime_model(model)` to explicitly release its addon registration.

Generated model code includes inspectable wrappers for functional operations
and uses `nn.ModuleList` for repeated Groups. Binding sites retain logical
element IDs, module paths and repetition indexes. The generated source is
trusted addon code; this path does not accept arbitrary Python source.
U3 will use this definition to build the canonical marimo materialization cell
and expose it as an opaque parent value.

## P5 independent runtime inspection subset

`PyTorchModelDefinition.instantiate()` assigns a new runtime instance ID,
stores Graphimo metadata on the module outside its `state_dict()`, registers it
weakly and emits `runtime.created`. `release_runtime_model(model)` removes
subscriptions and registry metadata and emits `runtime.released`; collection
also clears weak runtime/subscription entries.

`SubscriptionSpec` selects an activation or output-gradient signal for one
logical element, one repeated-group index, a rate and `summary`, `histogram`,
or `sample` detail. `subscribe(model, spec)` resolves the compiler binding map
and installs local removable module hooks only for that subscription.
`unsubscribe(id)` removes them. Output and gradient hooks return their input
unchanged. Gradient inspection reports a one-shot warning when a selected
output does not require gradients.

`summarize_tensor` returns JSON-safe shape/dtype/device/count/statistics.
Histogram bins are bounded; sample payloads are capped at 4096 values. GPU
statistics stay on-device except scalar results and bounded samples. Nested
module outputs get a bounded tree summary; histogram/sample on multi-leaf
outputs asks the caller to select one leaf. `emit_metric` associates finite
scalar values with runtime provenance. `emit_cuda_resources` returns without
calling CUDA APIs for CPU models and rate-limits CUDA resource events.

Events are written to a bounded, non-blocking addon `EventQueue`. U3 will
expose addon subscriptions and event draining through the generic host API;
the addon does not add a separate live UI or Zeron-only execution path.

## P6 delivered: checkpoint artifacts and migration reports

`save_checkpoint(model, optimizer=None, scheduler=None, metadata={})` saves a
state dictionary and provenance into a temporary `.pt` file, then stores it
as a `pytorch-checkpoint` through `graphimo.artifacts`. It records the
MetaBlock/revision/materialization, external parameter values, PyTorch/addon
versions, optional optimizer/scheduler state dictionaries and finite JSON
metadata. It never saves a whole Python model object.

`load_checkpoint(definition, artifact, strict=True)` checks graph revision and
materialization key before creating/loading the target. With `strict=False`,
`migrate_state_dict` copies only exact-name, exact-shape tensors and the
returned model carries a `WeightMigrationReport` at
`__graphimo_pytorch_migration_report__`. The report lists copied keys, keys
missing on each side and shape mismatches. Weight resizing and optimizer
migration are not automatic.

## P7 on-demand TorchLens inspection

The addon exports `TorchLensRequest`, `TorchLensIntervention`, `TorchLensResult`,
`inspect_with_torchlens`, and `save_torchlens_inspection`. Deep inspection lazy-loads
the pinned `torchlens==2.34.1` only when requested; importing or registering the
addon does not import TorchLens.

`inspect_with_torchlens(request, inputs, loss_fn=None)` requires explicit input
data. Tensors are passed as one positional model input, tuples/lists as positional
inputs, and string-keyed mappings as keyword inputs. The binding map resolves the
logical element and optional repeated-Group index to the exact compiler module
path. An ambiguous repeated element requires a repetition index. No training batch
is captured in the background.

The result includes bounded selected-module activation statistics, up to 128
operation labels/function names (plus the total operation count), loss-gradient
statistics when both `capture_gradients=True` and a scalar tensor `loss_fn` are
provided, non-finite findings, trace identity/version/timing metadata, and any
structured diagnostics. The inspection restores existing parameter gradients and
module buffers after the explicit replay.

`TorchLensIntervention("zero")` and `TorchLensIntervention("scale", factor)` use
TorchLens capture-time selectors. They are one-run experiments; the result marks
them `experimental=True` and summarizes the post-intervention activation. They do
not edit graph records or model weights.

Inspection traces are transient by default. Calling
`save_torchlens_inspection(result, store)` explicitly exports the TorchLens trace
and an `inspection.json` provenance report into a `pytorch-torchlens-trace`
ArtifactStore directory. The report records MetaBlock, graph revision,
materialization key, runtime instance, element, module path, both package versions,
operation summary, activation/gradient statistics, and intervention status. The
full trace is never added to project JSON.

The standalone API and CPU tests are complete against the exact 2.34.1 wheel.
U3 will expose the inspection action and bounded results through the generic
host contract; U6 will render those actions in the existing Graphimo plugin.

## P4 independent projection/editor subset

`graphimo_pytorch.workbench_projection.project_workbench(...)` returns
renderer-safe data for the contextual PyTorch palette, MetaBlock summary and
external parameters, method tabs, group scope cards, parameter inspectors,
edge shape labels/errors, Group navigation/repeat counts, generated source,
runtime instances and binding paths. It derives this information from
semantic records and compiler outputs; it does not inspect a raw model.

The addon also exposes immutable `set_element_parameter`,
`promote_parameter`, and `demote_parameter` operations so the application can
own user actions without putting semantic edits in UI code. These operations
are covered by pure tests. U2 will add the generic host API; U3 will adapt this
projection/editing layer and implement parent materialization.

## Resolved parent value; U3 implementation pending

The implementation update selects `PyTorchModelDefinition` as the opaque
parent-scope value. U3 generates and updates its marimo materialization cell
from the addon state inside the active marimo kernel. The service and renderer
do not return or construct a live `nn.Module` themselves.

P1 through P9 addon backend APIs and tests are in place. U2/U3/U6 complete
host, materialization and renderer integration. The addon remains independent
from the Graphimo Core scheduler and contains all Torch-specific semantics.

## P8 attention and Llama templates

The addon exports `mlp_template`, `self_attention_template`,
`llama_decoder_layer_template`, and `llama_causal_lm_template`, together with
`LlamaTemplateConfig`, `estimate_llama_parameters`, and `ParameterEstimate`.
Each template returns normal `PyTorchMetaBlockState` graph records that can be
serialized, edited and compiled. The full default Llama dimensions are safe to
insert or compile for source/shape inspection: creating the template does not
allocate an `nn.Module`; only an explicit `definition.instantiate()` allocates
weights.

The causal LM has a repeated `LlamaDecoderLayer` Group, `forward`, next-token
`loss`, and `predict` methods. An internal `method_call` record makes the
`predict -> forward -> softmax` relationship explicit in saved graph state and
generated Python. It is compiler semantics, not an insertable V1 palette item,
so the exact public element catalog remains unchanged. Static parameter and
parameter-byte estimates are available without constructing the model.

The scaled acceptance model (`vocab_size=128`, `d=32`, `n_head=4`, `ff=64`,
`n_layers=2`, sequence length 8) is compared against a direct PyTorch
implementation after copying identical state dictionaries. Tests compare
forward logits, next-token loss and prediction probabilities, and check
repeated `q_proj` bindings and internal Group shape projections. A full-size
32-layer graph is compiled for generated-source validation only; it is not
instantiated in routine tests.

`project_workbench(...)` exposes the repeated Group card, repetition count,
method tabs, Group-internal element/edge shapes and optionally generated
source for the Llama template. U3 adapts this to the generic host projection;
U6 integrates it into the existing Zeron renderer and product workflow.

## P9 packaging and runtime hardening

The P9 lifecycle test serializes a real addon-owned MetaBlock and layout,
opens the project while the addon entry point is absent, and verifies that
Core presents an opaque placeholder without changing either file. It then
builds the addon wheel, installs it under a temporary target directory, checks
the registered `pytorch` entry point, imports its registration with Torch and
TorchLens imports guarded, reloads the semantic state, and verifies the same
IDs, values and graph revision. This exercises installation in isolation and
does not alter the developer environment.

`EventQueue` enforces a positive finite capacity and drops events when full
using non-blocking `put_nowait`. Its dropped-event counter is observable. The
addon exposes `default_subscription_rate_hz`: 10 Hz for summary and 2 Hz for
histogram/sample detail; the subscription manager rate-limits before enqueuing.
The P5 and P9 tests cover rate enforcement, queue saturation and invalid
unbounded capacities.

The Graphimo Core and PyTorch package boundary remains import-safe when the
addon is absent. Visible addon controls and project restoration through the
application/service are U2/U3/U6 work; no renderer-only semantic route is
introduced.
