# Zeron service adapter

The Zeron plugin keeps its existing notebook canvas and uses the local
Graphimo service for project semantics. `plugin/service_bridge.luau` only
encodes a bounded request and invokes `python3 -m graphimo.service.invoke`;
the Python service remains responsible for action routing, MetaBlock addons,
Group catalogs, layouts and events. The service bearer token stays in its
owner-only manifest and is never passed through Luau or a canvas state value.

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
- Root and PyTorch MetaBlock canvas layouts use the Graphimo application
  `layout.get` / `layout.set` actions. Root layout reads and writes the legacy
  Graphimo plugin file shape so old notebook positions remain available.
- The **MetaBlocks** section lists saved addon-owned models and creates a
  `pytorch.model` MetaBlock through `metablock.create`.
- Selecting a model calls `metablock.project`; the renderer maps its elements,
  ports, edges, shapes and diagnostics into the existing Graphimo canvas.
- The sidebar shows method/group scopes and the palette returned by the
  PyTorch projection. Add, connect and delete edits use `metablock.apply`.
- **Insert generated definition** calls `metablock.materialize`, then inserts
  the returned ordinary Python into the active marimo notebook through the
  existing notebook bridge. The inserted Python remains visible and runnable
  in marimo.
- Opening the Terminal panel long-polls bounded Graphimo events and appends
  them to the existing log view. **Read Graphimo events** performs one manual
  cursor read.

## Runtime ownership

Marimo remains the owner of ordinary Python cells, dependency analysis,
reactive execution and cell output. The current Zeron adapter still uses the
marimo bridge for opening notebooks, inspecting ordinary cells, inserting
ordinary components, connecting ordinary cell source, running cells and
deleting cells. Those operations are not reimplemented by the service because
the Core application does not schedule or replace a live marimo session.

The service adapter does not create a second canvas. PyTorch MetaBlocks are
rendered in the same canvas and use the same pan, zoom, card and edge renderer.
The standalone Workbench remains the conformance surface for host-independent
application behavior; browser-specific Zeron rendering still requires a live
plugin check in Zeron.

## Agent tools

The manifest exposes a compact Graphimo service tool set in addition to the
legacy marimo tools:

| Tool | Service operation |
| --- | --- |
| `graphimo_inspect` | Batch reads the active scope, Group catalog, component catalog and MetaBlock catalog; includes the live marimo snapshot when a session is selected. |
| `graphimo_execute` | Routes one named application action through the local service. |
| `graphimo_execute_batch` | Routes an ordered action plan through the service batch journal, including result references. |
| `graphimo_component_write` | Calls `component.write` to atomically validate and create/update a reusable `@node` source file. |
| `graphimo_validate` | Calls `component.validate` and returns contract diagnostics. |
| `graphimo_events` | Reads bounded application/addon events by cursor. |

Each tool accepts an optional local notebook `path`; otherwise it uses the
active Graphimo project. The source-writing tool accepts at most 2 MiB of
source, while the service request limit is 5 MiB to allow JSON encoding
overhead. See [Agent-authored components](components.md) for file validation,
atomic replacement and recoverable delete semantics. The old marimo tools
remain available for existing notebook workflows during the service migration.
