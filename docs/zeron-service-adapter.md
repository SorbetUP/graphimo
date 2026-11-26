# Zeron service adapter

The Zeron plugin keeps its existing notebook canvas and uses the local
Graphimo service for project semantics. `plugin/service_bridge.luau` only
encodes a bounded request and invokes `python3 -m graphimo.service.invoke`;
the Python service remains responsible for action routing, MetaBlock addons,
Group catalogs, layouts and events. The service bearer token stays in its
owner-only manifest. During a live notebook attach, the service bridge returns
an ephemeral kernel configuration to the plugin, which passes it through the
marimo execute API into kernel memory. Neither service nor marimo credentials
are written into notebook source, sidecars or canvas state.

## Installation requirement

The `python3` executable made available to the Zeron plugin must have the
Graphimo package installed, and `graphimo-pytorch` must also be installed to
create or edit PyTorch MetaBlocks:

```bash
python3 -m pip install -e /path/to/graphimo
python3 -m pip install -e /path/to/graphimo/addons/pytorch
```

The service is started on demand when the plugin's first local project action
uses `GraphimoClient.connect_local()`. The service binds to `127.0.0.1` and
stores its token in the local Graphimo state directory. If the package is
missing from the Python interpreter Zeron launches, the plugin reports a
service adapter error and does not silently perform a different semantic
operation.

## Current canvas integration

- Opening a local project loads its layout, Group catalog, MetaBlock catalog
  and active application scope in one Graphimo service batch.
- The normal Python palette gets its Control Block entries from
  `control.catalog`. Selecting a control calls `control.add`, which returns
  validated widget/value cell source and inserts both as ordinary marimo cells
  through the attached kernel.
- **Reusable Groups** are actionable entries. Opening one enters the matching
  Graphimo application scope, then opens its `groups/<module>.py` file as the
  live marimo notebook in the same canvas. Graphimo continues to use the root
  project path for Group catalog, scope and layout operations, while marimo
  owns the opened Group file's cells and execution.
- Shift-click selects multiple ordinary marimo cells. Right-clicking the
  selection offers **Create Group Block**; the dialog collects a display name,
  Graphimo Core derives the module and infers boundary ports from the inspected
  graph, and a service notebook action replaces the cells only if their source hashes
  still match. The replacement remains a normal marimo cell and the downstream
  marimo dependencies reconnect to the Group's inferred output definitions.
- Canvas undo/redo tracks layout moves and ordinary-cell source/structure
  changes through Graphimo service notebook actions. In a PyTorch MetaBlock, the same
  buttons call `metablock.undo` / `metablock.redo` through Graphimo Core, which
  restores opaque addon state and refuses stale sidecar revisions. See
  [Zeron canvas undo and redo](zeron-undo-redo.md) for transaction coverage and
  current boundaries.
- A breadcrumb above the canvas returns to the project or to an ancestor
  Group. Each scope uses its stable `scope_id` for its saved canvas layout.
  The active project root is kept separately from the currently opened Group
  notebook so service operations continue to address the same project.
- Root and PyTorch MetaBlock canvas layouts use the Graphimo application
  `layout.get` / `layout.set` actions. Root layout reads and writes the legacy
  Graphimo plugin file shape so old notebook positions remain available.
- The **MetaBlocks** section lists saved addon-owned models and creates a
  `pytorch.model` MetaBlock through `metablock.create`. **Import PyTorch state**
  creates a new MetaBlock from a bounded semantic JSON state or PyTorch sidecar
  envelope; arbitrary Python source is explicitly rejected by the addon.
- Selecting a model calls `metablock.project`; the renderer maps its elements,
  ports, edges, shapes and diagnostics into the existing Graphimo canvas.
- The PyTorch sidebar can inspect the addon's read-only generated Python for
  the currently projected graph revision and copy that source. It does not
  edit the semantic graph or present a false Python-to-graph round trip.
- The sidebar shows method/group scopes and the palette returned by the
  PyTorch projection. Add, connect and delete edits use `metablock.apply`.
- Selecting a PyTorch element shows its inferred output shape and
  addon-declared parameters. The same inspector can rename the selected
  element, edit external model parameters, rename the model, and change Group
  repetition. These operations are submitted as `metablock.apply` intents. The
  renderer parses the declared scalar/JSON input type; the addon remains
  authoritative for semantic validation.
- **Insert generated definition** calls `metablock.materialize`, then inserts
  the returned ordinary Python into the active marimo notebook through the
  service notebook action. The inserted Python remains visible and runnable
  in marimo.
- Opening the Terminal panel long-polls bounded Graphimo events and appends
  them to the existing log view. **Read Graphimo events** performs one manual
  cursor read.
- The PyTorch MetaBlock sidebar can manually read its bounded addon event
  stream with `metablock.events`; it retains at most 50 displayed events and
  truncates each rendered payload. It labels these as events received by the
  Graphimo service. Runtime list and inspection requests execute in the
  attached marimo kernel, where the model and runtime registry live. Bounded
  telemetry events from that kernel are pushed into the service journal.
- The MetaBlock checkpoint panel inspects an explicit Artifact ID and offers
  exact-shape migration preview only when graph revisions differ. The preview
  summarizes copied names, missing names and shape mismatches, with a bounded
  number of entries displayed per category. Inspection shows checkpoint
  provenance, library versions, optimizer/scheduler presence and a bounded
  metadata summary. It never loads or migrates a model from the renderer;
  applying a migration remains an explicit Python
  `load_checkpoint(..., strict=False)` operation. Late
  checkpoint responses are ignored after switching MetaBlocks, so they cannot
  overwrite the current model's panel state or clear a newer request's busy
  indicator.

## Runtime ownership

Marimo remains the owner of ordinary Python cells, dependency analysis,
reactive execution and cell output. The canvas and agent use named Graphimo
application/service actions for live graph inspection, component discovery and
insertion, controls, edits/restoration, Group replacement, connections and
runs. The service forwards bounded intents to the attached marimo kernel; it
does not schedule or replace a live marimo session. The private Zeron bridge
is retained for host/runtime duties: session discovery, opening the editor,
reading logs and diagnostics, and configuring the ephemeral telemetry
publisher. It does not perform functional graph or cell mutations.

The service adapter does not create a second canvas. PyTorch MetaBlocks are
rendered in the same canvas and use the same pan, zoom, card and edge renderer.
The standalone Workbench remains the conformance surface for host-independent
application behavior; browser-specific Zeron rendering still requires a live
plugin check in Zeron.

## Live kernel/runtime bridge

When `graphimo_inspect` inspects a live local notebook, Zeron sends its local
marimo URL, optional marimo token and session ID to `graphimo.service.invoke`.
That process attaches a `MarimoKernelPort` to the in-memory project session and
returns the service URL/token/project/session identity to Zeron. Zeron then
asks the kernel to run every `graphimo.kernel_hooks` entry point's `configure(...)`
method through the transient marimo execute endpoint — the bridge never imports
an addon by name. Credentials remain in process memory.

Runtime operations (`runtime.list`, subscribe, unsubscribe, release and
TorchLens inspection) go from Zeron through `GraphimoClient`,
`ProjectSession`, `GraphimoApplication` and the addon to the named kernel port.
The port accepts a strict action allowlist and JSON arguments; it executes the
same `metablock.invoke` application action in the kernel, where the actual
`RuntimeRegistry` and model live. `torch.nn.Module` instances never cross the
HTTP boundary. Request arguments are capped at 1 MiB and results at 4 MiB.

Continuous events travel kernel → service instead. A daemon publisher drains
the addon's existing bounded event bus, batches at most 64 JSON events/256 KiB
and posts them to authenticated loopback `POST /v1/events/publish`. The service
places them into its cursor-based `EventJournal`; `metablock.events` reads the
MetaBlock-filtered view of that same spool. The service records project,
session, MetaBlock, revision, runtime, logical element, module path and step.
No code is accepted by the event endpoint.

The publisher uses bounded pending storage, timeout and backoff. If the service
stops, forward/backward and training continue; events retry until the bounded
queue fills, then losses are counted and a `telemetry.dropped` event is sent
when connectivity returns. A later attach reconfigures the existing publisher
with the new URL/token and preserves queued events. Closing a project asks the
kernel to remove tracked subscriptions and stop the publisher. Attaching a
different marimo session to the same project first cleans the prior kernel's
subscriptions and publisher. Cleanup is best-effort if the old kernel is
already gone.

## Agent tools

The manifest exposes a compact Graphimo service tool set and retains the
original short tool names as aliases for common notebook operations:

| Tool | Service operation |
| --- | --- |
| `graphimo_inspect` | Batch reads the active scope, Group catalog, component catalog and MetaBlock catalog; includes the live marimo snapshot when a session is selected. |
| `graphimo_execute` | Routes one named application action through the local service. |
| `graphimo_group_from_selection` | Inspects the active marimo graph, creates a Group through `group.create_from_selection`, then replaces the explicit selected cell IDs after checking their live source hashes. |
| `graphimo_execute_batch` | Routes an ordered action plan through the service batch journal, including result references. |
| `graphimo_component_write` | Calls `component.write` to atomically validate and create/update a reusable `@node` source file. |
| `graphimo_validate` | Calls `component.validate` and returns contract diagnostics. |
| `graphimo_events` | Reads bounded application/addon events by cursor. |

`graphimo_inspect` includes the current Control Block catalog. Agents can call
`graphimo_execute` with `control.create` to obtain the same validated source
bundle as the UI. `graphimo_inspect` also attaches the selected local marimo
kernel to the service for subsequent `notebook.*` actions and configures the
kernel event publisher. The short `add_component`, `connect` and `run` tools
delegate to the same `component.add`, `notebook.connect` and `notebook.run`
application actions used by the canvas. Marimo still owns and performs the
cell edits and reactive execution.
Agents can call `graphimo_group_from_selection` with an explicit selection,
name and Python module identifier to use the same Group inference and safe
replacement flow as the canvas context menu.

Each tool accepts an optional local notebook `path`; otherwise it uses the
active Graphimo project. The source-writing tool accepts at most 2 MiB of
source, while the service request limit is 5 MiB to allow JSON encoding
overhead. See [Agent-authored components](components.md) for file validation,
atomic replacement and recoverable delete semantics. The old marimo tools
remain available for existing notebook workflows during the service migration.

The full service test exercises an agent-style PyTorch workflow through the
same client boundary: a batch creates and edits a connected MetaBlock, the
service materializes its definition, the ordinary addon training helper runs
one external training step, activation events and a TorchLens inspection are
read through service actions, and a saved checkpoint is inspected and given a
shape-migration preview after the graph revision changes. A separate live
marimo integration test exercises service-to-kernel subscriptions,
kernel-to-service activation/gradient events, training during service outage,
and publisher reconnection.
