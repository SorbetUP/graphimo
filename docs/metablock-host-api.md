# MetaBlock host API

Graphimo Core now loads semantic MetaBlock implementations through the
`graphimo.metablocks` Python entry-point group. Discovering installed addons
reads entry-point metadata only; an addon's module is imported only when a
specific dialect is opened or used.

An entry point exposes a `MetaDialectRegistration` with a dialect ID, addon
version, API version and factory. API version 1 creates a handler implementing
`create_state`, `catalog`, `project`, `apply`, `build_materialization`,
`invoke` and `drain_events`. This interface is domain-neutral. Core does not
inspect addon element kinds or interpret addon state.

`GraphimoHostAPI` gives a handler the current project identity/path, sidecar
read/write helpers, ArtifactStore access, application event and diagnostic
callbacks, a marimo materialization-cell updater and current-scope metadata.
It exposes no renderer widgets and contains no PyTorch behavior.

Each MetaBlock is persisted as opaque UTF-8 JSON under
`.graphimo/metablocks/<metablock_id>.<dialect-prefix>.json` (for example,
`model_7ab2c1f0.pytorch.json`). Writes use a temporary file and
atomic replacement. The 8 MiB sidecar limit and constrained ID syntax prevent
unbounded input and path traversal. State revisions are SHA-256 hashes of
canonical JSON. Core does not rewrite sidecars when their addon is missing;
the application returns an unavailable placeholder with the original state
and revision.

`GraphimoApplication.execute()` is the shared action seam for host adapters.
It routes list/create/catalog/project/apply/materialize/invoke/events operations
to the same manager, bounds action arguments and JSON results, and leaves
canvas state and notebook scheduling to their respective host systems. The
`metablock.list` action reads sidecar metadata without loading addon modules;
invalid sidecars appear as bounded warning diagnostics.

Canvas positions are stored separately from addon-owned semantic state in the
project sibling file `.graphimo.json`. `GraphimoHostAPI.read_layout(scope_id)`
and `write_layout(scope_id, layout)` store finite `x`, `y`, `width` and
`height` values keyed by element ID. Writes preserve other scopes and unknown
presentation metadata and use atomic replacement. The shared application
exposes those operations as `layout.get` and `layout.set` actions. This keeps
renderer layout out of PyTorch graph revisions and addon sidecars.

The tests use a fake entry-point addon to verify lazy discovery,
state creation/editing, atomic failure behavior, project serialization,
missing-addon preservation, materialization revision checks, bounded events,
scoped layout persistence and every addon action through the shared application
seam. The separately installable PyTorch wheel registers through this API and
implements the semantic graph/compiler, `PyTorchModelDefinition` materialization,
runtime subscriptions, TorchLens actions and checkpoint lifecycle. Its runtime
registry and telemetry are process-local; routing live marimo events through a
persistent service and migrating the existing Zeron UI remain later integration
milestones. See [`pytorch-addon.md`](pytorch-addon.md) for the addon contract and
current process-boundary limitations.
