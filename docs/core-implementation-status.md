# Graphimo core implementation status

This is an implementation log. The three `Graphimo_*_IMPLEMENTATION.md` files remain unchanged and normative.

## Current plan — implementation update 2026-11-25

[`Graphimo_IMPLEMENTATION_UPDATE_2026-11-25.md`](../Graphimo_IMPLEMENTATION_UPDATE_2026-11-25.md)
is now the normative update to the three contracts. It resolves the prior
Group backend, addon host API, and PyTorch parent-value decisions. The ordered
work after the already completed Core/PyTorch backend milestones is U1 local
gate, U2 generic MetaBlock host, U3 PyTorch host integration, U4 Group Blocks,
U5 local service/client, U6 Zeron migration, U7 agent parity, and U8 renderer
UX completion. The existing Zeron plugin is the primary UI; the standalone
Workbench remains a conformance/debug harness.

### U1 — repository hygiene and local validation gate

- Removed tracked addon `build/` and `.egg-info/` outputs and untracked the generated Zeron `dist/` archive while preserving the local archive. Added ignores for build, dist, egg-info, marimo and cache output.
- Added `./scripts/check --fast` and `./scripts/check --full`, with changed-file Ruff format checks, complete Ruff lint, syntax compilation, core/control/artifact/addon checks, full pytest and live marimo tests, isolated root/addon wheel builds, direct dependency consistency checks and generated-artifact hygiene.
- The wheel tests now build from temporary source copies. The optional `.githooks/pre-push` runs the full gate and is enabled with `scripts/install-hooks`.
- Local verification: fast gate **97 passed, 1 deselected**; full gate **181 passed, 1 opt-in network bootstrap skipped**; both wheels build and include expected package files; Ruff and hygiene checks pass.

### U2 — generic MetaBlock addon host API

- Replaced the empty experimental placeholder with `MetaBlockContext`, `MetaBlockDialect`, and validated `MaterializationSpec`. The context exposes project identity/path, an `ArtifactStore`, sidecar load/atomic-save helpers, bounded event and diagnostic methods, a logger, marimo materialization-cell updates, and current scope/navigation metadata. It contains no PyTorch concepts.
- Entry-point discovery remains import-free. A registration and handler are loaded only for an explicit MetaBlock operation; API version and handler protocol are validated then.
- `GraphimoApplication` now exposes create, catalog, project, apply, materialization, invoke, and bounded cursor event methods. Addon state is JSON-only, sidecar writes use temp-file + atomic replace, and a failed apply leaves the previous state untouched. Missing/unreadable addon states continue through the existing opaque projection path without rewriting their bytes.
- Added fake-addon lifecycle tests for catalog/projection, state edits and persistence, materialization-cell insertion/update, invoke/events, artifact access and path validation. Core source stays free of PyTorch imports.
- Verification: **21 MetaBlock/scopes regression tests passed**; `scripts/check --full` passes with **186 passed, 1 opt-in network bootstrap skipped**, both wheels build and pass package-content checks, Ruff lint/format and generated-artifact hygiene pass. The legacy `application.py` controller remains outside formatter checks to avoid a broad unrelated reformat; its new behavior is delegated to formatted `metablock_application.py`.

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

## Remaining unrelated contract conflict

`Graphimo_CODEX_IMPLEMENTATION.md` §5.2 says `active_expression` remains `null` until a separately specified runtime hook exists. §18's expected runtime-inspection response shows `active_expression: "normalized"`. No such hook is defined in the contract. M1's core rule is followed for now: `describe_result` and the adapter return `null`. The runtime output still carries every statically known expression in `expressions`.

This is the remaining unrelated specification discrepancy. The active expression stays `null`; the implementation does not add runtime return-path instrumentation without a defined contract.

## Group Block backend — decision resolved, implementation pending U4

The update selects a cloned marimo `App.run(defs=...)` wrapped as a callable Graphimo component, with declared named outputs and no Graphimo scheduler. Group execution, palette/import/reuse, and nested scope operations remain to be implemented in U4.

## MetaBlock seam — independent subset

- Added generic recursive `GraphScope`, `GraphElement`, `GraphEdge`, and `MetaBlock` records. Addon-owned state remains opaque JSON and round-trips without interpreting element kinds.
- Added `MetaDialectRegistration` and entry-point discovery from `graphimo.metablocks`. Discovery returns metadata entries without importing or invoking optional addons.
- Workbench opening scans generic `.graphimo/metablocks/*.json` sidecars and shows missing/unavailable addons as opaque cards. The sidecar bytes are never rewritten; unreadable files remain preserved with a diagnostic.
- Core source has a conformance test forbidding direct `torch`/`torchlens` imports.
- The host/handler contract is now specified by the implementation update and is the U2 deliverable. Core continues to treat addon state as opaque and does not add PyTorch semantics.

## Host integration seam

The production Zeron adapter still has no implementation of `MarimoPort.get_variable`. The standalone Workbench supports live output inspection through `describe_node_result` executed inside the marimo kernel, so Python values do not cross into the renderer. No runtime scheduler was added.

### W2 — marimo-backed Control Blocks

- Added source generation and strict parsing for Slider, Range Slider, Number, Select, Checkbox, Switch, Text and Button controls. Each is a Graphimo-owned marimo UI cell with stable markers and one typed `.value` output.
- Added control items to the normal-scope application palette, control creation/config validation, value changes through the application/controller, and wiring from a control into a normal `@node` input.
- The Workbench renders interactive control cards. Slider/range/number controls show Live or On release; the on-release policy is the default. Text is staged by default; checkboxes, switches, selects and buttons update immediately.
- The Workbench marimo port uses the pinned marimo 0.25.2 native UI-value endpoint. It retrieves the skew-protection token from the local rendered page; the endpoint then triggers marimo's own reactive execution.
- A live test creates and changes every baseline UI primitive and confirms a slider update recomputes a downstream node. The marimo button uses a fixed generated increment callback; its parser rejects other callback ASTs.

### W3–W5 — persistence, MetaBlocks, and PyTorch Workbench audit

- W3 layout persistence and disable/enable are implemented by M4/M6. Group Block navigation/execution is pending U4 under the resolved marimo `App.run(defs=...)` backend.
- W4 addon entry-point discovery and opaque missing-addon placeholders are implemented. Create/open/edit, scope navigation and materialization are pending U2.
- W5 addon registry, shape/source/runtime projections, templates and standalone numerical execution are available. Connecting them to the shared application, Zeron renderer and parent marimo cell is pending U3/U6; the parent value is fixed as `PyTorchModelDefinition`.

### PyTorch addon P1 — package and semantic schema

- Added `addons/pytorch` as a separate `graphimo-pytorch` distribution with the `graphimo.metablocks` entry point and no PyTorch import during registration/schema import.
- Added frozen state/element/edge/method/group/parameter dataclasses, JSON conversion, finite JSON validation, atomic UTF-8 save, unsupported-schema diagnostics and canonical SHA-256 graph revisions.
- Unknown element-kind strings and their schema fields round-trip without a registry; later validation/compiler stages decide whether they can materialize.
- P1 tests cover state round-trip, repeat validation, deterministic revisions, semantic changes, unknown schema non-mutation, wheel metadata and an import guard forbidding `torch`/`torchlens`.
- The generic host API is specified in the implementation update; its implementation and addon-loading lifecycle are the U2 milestone.

### PyTorch addon P2 — registry, validation and shape inference

- Added an addon-owned registry for every required V1 element kind, with declared input/output ports, parameter types/defaults/ranges/choices and structural metadata. Registry imports remain Torch-free.
- Added `validate_model_graph` / `ValidationReport` to collect duplicate IDs, unknown kinds/ports, missing endpoints, multiple incoming edges, unconnected required inputs, invalid method declarations, generated Python name collisions, bad parameter values/bindings, invalid Group references/repetitions, graph cycles and recursive Group definitions.
- Added frozen `Dim` and `TensorSpec` values plus `infer_model_shapes`. It propagates through methods, declared element inputs/outputs, and nested/repeated Group definitions without executing a model forward.
- Implemented V1 shape rules for all catalog kinds: embedding/linear/norm, activation and dropout, tensor transforms, split/merge heads, concat, broadcast add/multiply, batched matmul, RoPE, causal masks and cross entropy. Static concrete incompatibilities are errors; unresolved symbolic compatibility remains unresolved rather than falsely blocking compilation.
- The P2 suite covers the exact registry catalog, MLP propagation, embedding/attention matmul shapes, dimension failures, invalid ports/cycles/parameters, broadcast and loss shapes, reshape/transpose/flatten/unflatten, RoPE parity, recursive Groups and unresolved external bindings.
- P2 does not create a compiler, PyTorch module instances, or Workbench editing surface; the generic addon host is implemented separately in U2.

### PyTorch addon P3 — compiler and generated source

- Added deterministic topology-based compilation from validated semantic state to readable Python source. Source and factory share one graph revision and one materialization key derived from resolved external parameter values and addon compiler version.
- Generated source defines the model and reusable Group classes, constructs standard `nn` layers, routes method graphs through `forward`/`loss`/`predict`, emits repeated Groups as `nn.ModuleList`, includes the required provenance header, and never embeds scheduler, UI, or TorchLens code.
- Added inspectable wrappers for reshape, transpose, split/merge heads, add/multiply, scale, matmul, RoPE, causal masking, concatenate, unflatten and cross entropy. The compiler executes only addon-generated trusted source in a private namespace.
- Added stable `RuntimeBindingMap` records for visual module sites and repeated Group paths. `PyTorchModelDefinition.instantiate()` creates a fresh real module, applies optional device/dtype, attaches private runtime metadata, and registers the model weakly; metadata does not enter `state_dict()`.
- P3 tests execute an MLP, an attention/mask graph and a repeated Group; they verify readable generated source, external-parameter materialization changes, stable binding paths, invalid-graph refusal and weak registry cleanup.
- P3 compiles to the addon-local `PyTorchModelDefinition`. U3 will generate the canonical marimo materialization cell that exports this opaque value to a parent scope.

### PyTorch addon P4 — independent projection/editor subset

- Added `graphimo_pytorch.workbench_projection.project_workbench`, a renderer-independent projection for contextual PyTorch palette entries, MetaBlock summary/ports, method tabs, scope cards, parameter descriptors, edge shapes/diagnostics, Group navigation/repetition, generated source/bindings, and available runtime instances. It never inspects raw `nn.Module` objects.
- Added immutable semantic edit operations for scalar parameter changes, parameter promotion to an external MetaBlock input, and demotion when safe. Promotion/demotion preserves external bindings and only removes an external parameter after its last binding is removed.
- Automated tests cover projection data and edits without invoking a renderer.
- The renderer-independent projection and semantic edit operations are ready. U2/U3 will wire them through the newly specified generic addon host and materialization contract; the parent output is `PyTorchModelDefinition`.

### PyTorch addon P5 — independent runtime inspection subset

- Added V1 JSON-safe `PyTorchEvent`, bounded non-blocking `EventQueue`, and `SubscriptionSpec` validation.
- Added subscription-managed per-module forward hooks, output tensor gradient hooks, repetition-index selection, rate limiting, one-shot no-gradient warnings and cleanup on unsubscribe, explicit release, or model collection. No global PyTorch hooks are used; hook callbacks do not alter outputs or gradients.
- Added bounded tensor summaries, opt-in histogram/sample detail (maximum 4096 sample values), safe tensor-tree summaries, scalar metric emission, and CUDA resource events that make no CUDA calls for CPU models.
- Model creation/release emits runtime lifecycle events. Runtime instances are weakly registered and carry Graphimo provenance separately from `state_dict()`.
- P5 tests exercise activation/gradient events, unsubscribe/release, rate limiting, empty/non-finite tensors, bounded histogram/sample payloads, metrics and CPU resource handling.
- The generic application event route and visible runtime controls remain pending U3/U6. The addon exposes queued events and structured runtime data without a private Workbench or Luau/Zeron bypass.

### PyTorch addon P6 — checkpoints and conservative migration

- Added `save_checkpoint` / `load_checkpoint` using a temporary `torch.save` state-dict file and the native `graphimo.artifacts` API. The checkpoint contains provenance, external parameter values, versions, user JSON metadata, and optional optimizer/scheduler `state_dict`s; it never stores a pickled model object.
- Strict loading checks both graph revision and materialization key before instantiating/loading a model. A structural graph change raises `pytorch_checkpoint_revision_mismatch`; changed resolved external values/compiler materialization raises `pytorch_checkpoint_materialization_mismatch`.
- `strict=False` uses the explicit `migrate_state_dict` helper and attaches a `WeightMigrationReport` to the returned model. It copies only same-name, same-shape tensors and reports missing/extra keys and shape mismatches; it does not resize weights or migrate optimizer state.
- Tests cover artifact round-trip, exact weight restoration, optimizer/scheduler payloads, revision/materialization refusals, exact-copy migration, size mismatch reports and invalid provenance/metadata.

### PyTorch addon P7 — on-demand TorchLens inspection and interventions

- Added the addon-local `TorchLensRequest`, `TorchLensIntervention`, and bounded `TorchLensResult` API. It resolves the selected logical element and repeated Group instance through `RuntimeBindingMap`; it never guesses a module path from visual labels.
- `inspect_with_torchlens(request, inputs, loss_fn=...)` runs only on caller-supplied inputs. It captures the selected module activation and bounded operation labels, and captures gradients only when requested with an explicit scalar loss callback. No implicit training-batch capture is armed.
- Zero ablation and scalar scaling use TorchLens 2.34.1's capture-time intervention selector. Results label these as experimental, report the modified activation, and leave model weights unchanged. Inspection restores pre-existing parameter gradients and module buffers after the explicit replay.
- Full traces remain transient by default. `save_torchlens_inspection(result, store)` is the explicit export action; it stores a TorchLens trace plus a JSON provenance report as `pytorch-torchlens-trace`, including MetaBlock/revision/materialization/runtime/element/path and Torch/PyTorch versions.
- Tested the actual addon against the exact TorchLens 2.34.1 wheel with CPU activation, loss-gradient, zero/scale intervention, unchanged weight, and ArtifactStore round-trip tests. The pinned package remains lazy-loaded; importing the addon does not import TorchLens.
- Workbench inspector controls and the generic host event/action route are pending U3/U6. Deep inspection remains addon-owned and does not create a separate PyTorch canvas or Zeron-only logic.

### Core/addon boundary correction

- Removed the historical optional TorchLens adapter from `graphimo_core.pytorch`, its vertical-slice demo path, and the Core distribution's `inspection` extra. TorchLens deep inspection is now owned by the separately installed PyTorch addon only.
- Replaced the old Core integration test with a conformance test that checks the Core package source and dependency metadata for TorchLens leakage. The addon P7 integration test exercises the pinned API instead.

### PyTorch addon P8 — editable attention and Llama templates

- Added `mlp_template`, `self_attention_template`, `llama_decoder_layer_template`, and `llama_causal_lm_template`. Each returns ordinary editable schema records; the default 32-layer configuration is compiled to a factory/source only and is never instantiated during insertion or projection.
- The Llama graph uses one reusable `LlamaDecoderLayer` Group repeated by `n_layers`, keeps the documented semantic module names, exposes `forward`, next-token `loss`, and no-gradient `predict`, and produces static parameter/memory estimates.
- Added an internal `method_call` semantic record so `predict -> forward -> softmax` remains explicit and reconstructible. It is supported by validation, shape propagation and generated Python but intentionally excluded from the exact user-insertable V1 element catalog and palette.
- Extended Group shape projection so the Workbench view can inspect inner element and edge shapes within a repeated Group. The projected view also exposes the collapsed repetition count, method tabs and generated source.
- The scaled `vocab=128, d=32, heads=4, ff=64, layers=2, sequence=8` graph is numerically compared to a direct PyTorch reference with copied state dictionaries for logits, next-token loss and prediction probabilities. Tests also verify repeated binding paths and internal Group shape labels. The full-size fixture is compiled but not instantiated.
- The addon APIs and Workbench view projection are tested. The end-to-end visual insertion/edit/run scenario is now specified and is pending the ordered U2/U3/U6 host integration; no separate PyTorch canvas was added.

### PyTorch addon P9 — isolation, reinstall, and bounded delivery

- Added an integration lifecycle test that saves a real PyTorch MetaBlock sidecar and layout, opens it with the addon absent, verifies the opaque placeholder and byte-for-byte preservation, builds/installs the exact addon wheel into an isolated target, discovers its entry point, imports registration without Torch/TorchLens, then reloads the same state and graph revision.
- The isolated wheel check verifies the template and TorchLens inspection modules are packaged. It does not modify the active Python environment or install addon dependencies.
- `EventQueue` now rejects non-positive/non-integral capacities so a caller cannot accidentally disable its bound. Saturation is verified to drop new UI events without waiting; defaults for summary versus histogram/sample subscriptions are exposed at 10 Hz and 2 Hz respectively. Existing P5 tests verify subscription rate limiting.
- The full P9 package/isolation and rate tests are independent of any frozen addon host handler. The P9 visual Workbench reopen/edit scenario remains unavailable for the previously documented Core §31/§32 reasons.
