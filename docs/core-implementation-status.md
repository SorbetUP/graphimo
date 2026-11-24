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

## Explicitly blocked feature

`Graphimo_CODEX_IMPLEMENTATION.md` §28 labels the generic Group Blocks backend `HUMAN DECISION REQUIRED`. The needed decision is which supported marimo mechanism executes a nested workflow: `app.run(defs=...)`, a generated callable wrapper, or another minimal marimo-supported path. Group execution is not implemented, and no custom scheduler was added. This does not block independent Core or Workbench work.

## MetaBlock seam — independent subset

- Added generic recursive `GraphScope`, `GraphElement`, `GraphEdge`, and `MetaBlock` records. Addon-owned state remains opaque JSON and round-trips without interpreting element kinds.
- Added `MetaDialectRegistration` and entry-point discovery from `graphimo.metablocks`. Discovery returns metadata entries without importing or invoking optional addons.
- Core source has a conformance test forbidding direct `torch`/`torchlens` imports.
- The stable addon host/handler API remains explicitly unfrozen in Core §31. Core does not instantiate factories, create addon elements, or claim Workbench missing-addon placeholders yet. Those dependent operations remain deferred until the contract is fixed; no handler methods were invented.

## Host integration seam

The current repository has no host implementation of `MarimoPort.get_variable`. The adapter behavior is covered with a fake host port, and the example is executed by marimo itself. A standalone Workbench must implement this operation through its application/controller port before live output inspection is available there. No runtime scheduler was added.
