# PyTorch MetaBlock addon

`graphimo-pytorch` lives at `addons/pytorch` and builds as its own Python
distribution. It registers in `graphimo.metablocks`; Graphimo Core does not
import Torch or TorchLens. Package metadata follows the addon contract:
Python 3.11+ (the current Graphimo Core floor), `torch>=2.6`, and
`torchlens==2.34.1`, with no CUDA wheel pinned.

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

The first schema milestone does not claim an interactive PyTorch editor or
model materializer. Core §31 says the stable host handler API is not frozen;
the factory remains a private registration marker until that interface is
specified. P1 does not add handler methods to Core.

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
The definition remains inside the addon until the Core/addon parent-port
contract conflict described above is resolved.

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
are covered by pure tests. The Workbench cannot yet render these views or
invoke the addon through the stable host API: Core §31 explicitly says that
API is not frozen and forbids inventing public handler methods. Parent-port
materialization also waits for the Core §32 human choice among an instantiated
module, class/factory, or `PyTorchModelDefinition` handle (the addon spec fixes
the last option, so those requirements conflict).

## Open normative conflict

Core §32 marks the exported PyTorch model value as a human decision among an
instantiated module, class/factory or explicit handle. This addon contract
§7 fixes the value as `PyTorchModelDefinition`. Those instructions conflict.
No parent model port or materializer dependent on that choice is implemented.

For implementation order, use P1 through P8 in
`Graphimo_PYTORCH_PLUGIN_IMPLEMENTATION.md` after resolving only the
decisions each stage depends on. The addon remains independent from the
Graphimo Core scheduler and contains all Torch-specific semantics.
