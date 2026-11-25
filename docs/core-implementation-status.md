# Graphimo core implementation status

This is an implementation log. The three `Graphimo_*_IMPLEMENTATION.md` files remain unchanged and normative.

## Completed milestones

### M1 — transparent node API

- Added `graphimo.node`, `is_node`, `inspect_node`, `preview_value`, and `describe_result`.
- `@node` marks the original function and does not wrap or execute it.
- Signature inputs and statically discovered output expressions are exposed as frozen dataclasses.
- Runtime previews bound strings and container samples, avoid arbitrary serializers, and catch failing `repr` implementations.
- The package is included in the Python distribution.

### M2 — canonical marimo instances

- Added random eight-hex instance IDs and `g_<node>_<id>` result variables.
- Generated cell bodies contain the exact node and instance markers plus a keyword-call assignment.
- Parsing accepts only owned single-assignment cells; reconnecting regenerates only the selected binding.
- Output wiring uses the bare result global for one output and tuple indices for multiple outputs.

### M3 — existing Python adapter integration

- Component discovery marks `@node` functions as executable and uses `graphimo.inspect_node` for outputs. Other callables remain visible as non-executable library information.
- Insertion requires bindings for required inputs and generates cells through `build_instance_source`.
- Connections require owned instances and use `output_expression` / `reconnect_instance_source`.
- Added `inspect_node_instance`; the host `MarimoPort` must expose `get_variable(session_id, name)` for live values. The adapter returns `runtime_value_unavailable` if the host does not provide it.
- Migrated the simple example to one `scale` definition and two independently executable instance cells.

### M4 — host-independent application/controller layer

- Added `GraphimoApplication` with injected marimo and project/library ports.
- Added structured view models for blocks, ports, edges, scoped palettes, navigation, run state and diagnostics.
- Implemented project selection/refresh, node palette discovery, repeated node insertion, connect/reconnect, run, output inspection, delete, selection and layout movement/persistence.
- The Workbench and any later host adapter can invoke the same application methods; layout state remains in `.graphimo.json` and execution state remains in marimo.
- The initial application seam exposed project-file nodes; M5 added host-safe discovery of `@node` definitions already defined in the active marimo notebook.

### M5 — standalone Workbench W1

- Added a standalone browser Workbench with contextual node palette, canvas, inspector, navigation, status and feedback regions.
- Added a local marimo HTTP/code-mode port. The Workbench can open an existing or new `.py` project, discover live notebook `@node` definitions, import library definitions once, add multiple instances, connect/reconnect, run, inspect bounded outputs, edit literals, disconnect, duplicate, delete, pan, zoom and persist layout.
- Browser code renders application view models and sends application intents; it contains no Python parsing, source generation, marimo calls or project state.
- Live marimo tests cover notebook-defined and library-defined node insertion, execution, reconnection and output previews.
- W2 Controls and later W3–W5 functionality remain for their ordered milestones.

### M6 — disabled instances

- Added canonical comment-wrapped disabled bodies and strict enable/disable parsing. Enabling restores the generated active source exactly.
- Disabled cards remain visible with declared outputs marked unavailable; the Workbench exposes enable/disable on the card menu.
- Connections into disabled outputs are projected as stale edges, and their dependent blocks are marked stale. No downstream source is removed or silently rewired.
- The contract still labels the detailed downstream UX decision `HUMAN DECISION REQUIRED`; the implementation provides only the normative stale/unavailable behavior until that decision is made.

### Native ArtifactStore — Core §25–27

- Added `graphimo.artifacts` as an explicit persistence API for files, directories, and byte payloads. Importing it does not initialize a store; the first operation uses `GRAPHIMO_ARTIFACTS_DIR` or `~/.graphimo/artifacts`. Hosts can set an explicit location with `configure_default_store`.
- Added immutable public `ArtifactRef(id, kind)`, `put`, `put_bytes`, `materialize`, `stats`, `pin`, `unpin` and mark/sweep `gc` operations. Persistence remains opt-in; Python outputs are not stored automatically.
- Reused the prototype's file-level CAS and lineage/pin model, replacing large-file `read_bytes()` hashing and verification with 1 MiB streaming. Materialization attempts APFS/Linux reflinks and streams the fallback copy.
- Directory manifests deduplicate each file and preserve empty directories. Symlinks and unsafe relative paths are rejected. No chunking dependency or content-defined chunking was added.
- Storage statistics count the sum of file references across all manifests as logical bytes, the unique blob files as physical bytes, and their non-negative difference as deduplication savings.
- The original `graphimo_core.LocalArtifactStore` remains available for the experimental vertical-slice prototype; product callers use `graphimo.artifacts`.
- The application/controller exposes an explicit ArtifactStore inspection action, and the Workbench inspector displays usage statistics without loading payloads or showing CAS paths.

## Contract conflict to resolve

`Graphimo_CODEX_IMPLEMENTATION.md` §5.2 says `active_expression` remains `null` until a separately specified runtime hook exists. §18's expected runtime-inspection response shows `active_expression: "normalized"`. No such hook is defined in the contract. M1's core rule is followed for now: `describe_result` and the adapter return `null`. The runtime output still carries every statically known expression in `expressions`.

`Graphimo_CODEX_IMPLEMENTATION.md` §32 says **HUMAN DECISION REQUIRED** whether the PyTorch MetaBlock's parent port carries an instantiated module, class/factory, or explicit handle. `Graphimo_PYTORCH_PLUGIN_IMPLEMENTATION.md` §7 says this is fixed as a `PyTorchModelDefinition` handle and rejects the other two forms. These are incompatible normative instructions. The external model value, its parent marimo cell and dependent runtime materialization are not implemented until the decision is resolved; independent schema/package work can proceed.

## Explicitly blocked feature

`Graphimo_CODEX_IMPLEMENTATION.md` §28 labels the generic Group Blocks backend `HUMAN DECISION REQUIRED`. The needed decision is which supported marimo mechanism executes a nested workflow: `app.run(defs=...)`, a generated callable wrapper, or another minimal marimo-supported path. Group execution is not implemented, and no custom scheduler was added. This does not block independent Core or Workbench work.

## MetaBlock seam — independent subset

- Added generic recursive `GraphScope`, `GraphElement`, `GraphEdge`, and `MetaBlock` records. Addon-owned state remains opaque JSON and round-trips without interpreting element kinds.
- Added `MetaDialectRegistration` and entry-point discovery from `graphimo.metablocks`. Discovery returns metadata entries without importing or invoking optional addons.
- Workbench opening scans generic `.graphimo/metablocks/*.json` sidecars and shows missing/unavailable addons as opaque cards. The sidecar bytes are never rewritten; unreadable files remain preserved with a diagnostic. Entering/editing still requires the unfrozen addon host interface.
- Core source has a conformance test forbidding direct `torch`/`torchlens` imports.
- The stable addon host/handler API remains explicitly unfrozen in Core §31. Core does not instantiate factories, create addon elements, or claim Workbench missing-addon placeholders yet. Those dependent operations remain deferred until the contract is fixed; no handler methods were invented.

## Host integration seam

The production Zeron adapter still has no implementation of `MarimoPort.get_variable`. The standalone Workbench supports live output inspection through `describe_node_result` executed inside the marimo kernel, so Python values do not cross into the renderer. No runtime scheduler was added.

### W2 — marimo-backed Control Blocks

- Added source generation and strict parsing for Slider, Range Slider, Number, Select, Checkbox, Switch, Text and Button controls. Each is a Graphimo-owned marimo UI cell with stable markers and one typed `.value` output.
- Added control items to the normal-scope application palette, control creation/config validation, value changes through the application/controller, and wiring from a control into a normal `@node` input.
- The Workbench renders interactive control cards. Slider/range/number controls show Live or On release; the on-release policy is the default. Text is staged by default; checkboxes, switches, selects and buttons update immediately.
- The Workbench marimo port uses the pinned marimo 0.25.2 native UI-value endpoint. It retrieves the skew-protection token from the local rendered page; the endpoint then triggers marimo's own reactive execution.
- A live test creates and changes every baseline UI primitive and confirms a slider update recomputes a downstream node. The marimo button uses a fixed generated increment callback; its parser rejects other callback ASTs.

### PyTorch addon P1 — package and semantic schema

- Added `addons/pytorch` as a separate `graphimo-pytorch` distribution with the `graphimo.metablocks` entry point and no PyTorch import during registration/schema import.
- Added frozen state/element/edge/method/group/parameter dataclasses, JSON conversion, finite JSON validation, atomic UTF-8 save, unsupported-schema diagnostics and canonical SHA-256 graph revisions.
- Unknown element-kind strings and their schema fields round-trip without a registry; later validation/compiler stages decide whether they can materialize.
- P1 tests cover state round-trip, repeat validation, deterministic revisions, semantic changes, unknown schema non-mutation, wheel metadata and an import guard forbidding `torch`/`torchlens`.
- Core §31 explicitly leaves the stable addon handler API unfrozen. The P1 registration factory is therefore a private marker only; it does not advertise a usable PyTorch canvas/editor yet.

### PyTorch addon P2 — registry, validation and shape inference

- Added an addon-owned registry for every required V1 element kind, with declared input/output ports, parameter types/defaults/ranges/choices and structural metadata. Registry imports remain Torch-free.
- Added `validate_model_graph` / `ValidationReport` to collect duplicate IDs, unknown kinds/ports, missing endpoints, multiple incoming edges, unconnected required inputs, invalid method declarations, generated Python name collisions, bad parameter values/bindings, invalid Group references/repetitions, graph cycles and recursive Group definitions.
- Added frozen `Dim` and `TensorSpec` values plus `infer_model_shapes`. It propagates through methods, declared element inputs/outputs, and nested/repeated Group definitions without executing a model forward.
- Implemented V1 shape rules for all catalog kinds: embedding/linear/norm, activation and dropout, tensor transforms, split/merge heads, concat, broadcast add/multiply, batched matmul, RoPE, causal masks and cross entropy. Static concrete incompatibilities are errors; unresolved symbolic compatibility remains unresolved rather than falsely blocking compilation.
- The P2 suite covers the exact registry catalog, MLP propagation, embedding/attention matmul shapes, dimension failures, invalid ports/cycles/parameters, broadcast and loss shapes, reshape/transpose/flatten/unflatten, RoPE parity, recursive Groups and unresolved external bindings.
- P2 does not create a compiler, PyTorch module instances, Workbench editing surface, or addon host handler methods; those belong to later milestones and the Core handler contract remains unfrozen.

### PyTorch addon P3 — compiler and generated source

- Added deterministic topology-based compilation from validated semantic state to readable Python source. Source and factory share one graph revision and one materialization key derived from resolved external parameter values and addon compiler version.
- Generated source defines the model and reusable Group classes, constructs standard `nn` layers, routes method graphs through `forward`/`loss`/`predict`, emits repeated Groups as `nn.ModuleList`, includes the required provenance header, and never embeds scheduler, UI, or TorchLens code.
- Added inspectable wrappers for reshape, transpose, split/merge heads, add/multiply, scale, matmul, RoPE, causal masking, concatenate, unflatten and cross entropy. The compiler executes only addon-generated trusted source in a private namespace.
- Added stable `RuntimeBindingMap` records for visual module sites and repeated Group paths. `PyTorchModelDefinition.instantiate()` creates a fresh real module, applies optional device/dtype, attaches private runtime metadata, and registers the model weakly; metadata does not enter `state_dict()`.
- P3 tests execute an MLP, an attention/mask graph and a repeated Group; they verify readable generated source, external-parameter materialization changes, stable binding paths, invalid-graph refusal and weak registry cleanup.
- P3 compiles to the addon-local definition handle. It does not expose that handle as a parent Graphimo port because Core §32 and addon §7 still conflict on that boundary.

### PyTorch addon P4 — independent projection/editor subset

- Added `graphimo_pytorch.workbench_projection.project_workbench`, a renderer-independent projection for contextual PyTorch palette entries, MetaBlock summary/ports, method tabs, scope cards, parameter descriptors, edge shapes/diagnostics, Group navigation/repetition, generated source/bindings, and available runtime instances. It never inspects raw `nn.Module` objects.
- Added immutable semantic edit operations for scalar parameter changes, parameter promotion to an external MetaBlock input, and demotion when safe. Promotion/demotion preserves external bindings and only removes an external parameter after its last binding is removed.
- Automated tests cover projection data and edits without invoking a renderer.
- The actual Workbench presentation/intent wiring and marimo parent materialization remain deferred. Core §31 says the stable addon handler API is not frozen and forbids inventing public handler methods. Core §32 marks the parent model value as **HUMAN DECISION REQUIRED**; the addon contract §7 selects `PyTorchModelDefinition`, so the two documents conflict. The exact decision is whether the parent cell exposes an instantiated `nn.Module`, a class/factory, or the explicit `PyTorchModelDefinition` handle. No UI-only bypass was added.

### PyTorch addon P5 — independent runtime inspection subset

- Added V1 JSON-safe `PyTorchEvent`, bounded non-blocking `EventQueue`, and `SubscriptionSpec` validation.
- Added subscription-managed per-module forward hooks, output tensor gradient hooks, repetition-index selection, rate limiting, one-shot no-gradient warnings and cleanup on unsubscribe, explicit release, or model collection. No global PyTorch hooks are used; hook callbacks do not alter outputs or gradients.
- Added bounded tensor summaries, opt-in histogram/sample detail (maximum 4096 sample values), safe tensor-tree summaries, scalar metric emission, and CUDA resource events that make no CUDA calls for CPU models.
- Model creation/release emits runtime lifecycle events. Runtime instances are weakly registered and carry Graphimo provenance separately from `state_dict()`.
- P5 tests exercise activation/gradient events, unsubscribe/release, rate limiting, empty/non-finite tensors, bounded histogram/sample payloads, metrics and CPU resource handling.
- The generic Graphimo application event-routing adapter and visible Workbench live selector remain deferred with Core §31's unfrozen handler API. The addon exposes queued events and structured runtime data without creating a private Workbench or Luau/Zeron bypass.
