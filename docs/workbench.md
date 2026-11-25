# Standalone Graphimo Workbench

The Workbench is a development and conformance harness for the same Graphimo
application operations used by the existing Zeron plugin. It is not a second
product UI; the Zeron plugin remains Graphimo's primary renderer.

## Run it

```bash
python -m pip install -e '.[workbench]'
graphimo-workbench
```

Open the local address printed by the command and enter a `.py` notebook path. The server starts `marimo edit` on a loopback port, initializes the active notebook through marimo's code-mode API, then serves the Workbench in the browser. If the path does not exist, the Workbench creates a minimal ordinary marimo app at that path.

The Workbench and notebook server use the same Python interpreter. That interpreter needs the pinned marimo and `websockets` packages. The local HTTP server defaults to `127.0.0.1:8765`; it does not listen on a public interface by default.

## W1 interactions

- The left panel discovers `@node` definitions in the active notebook and `components/` files.
- Clicking a palette item creates a real instance cell with the canonical Graphimo markers and one `g_<node>_<id>` result global. Required arguments are collected before insertion.
- For a node found in `components/`, the application adds one import cell the first time it is used. Repeated instances reuse that definition cell.
- Drag a card to persist its canvas position. Drag from an output anchor to an input anchor to connect or reconnect. The Workbench redraws from refreshed marimo state after the application accepts the edit.
- Run a selected block through marimo. The inspector shows bounded output previews, declared/runtime types, return expressions, and runtime cell errors.
- Edit supported literals through the inspector; disconnect a bound input; duplicate or delete an instance from its card menu.
- Disable or re-enable an instance from its card menu. The disabled card and output ports remain visible; dependent connections are dashed and dependent blocks are marked stale.
- Inspect native ArtifactStore logical, physical, deduplicated byte counts and blob/artifact counts from the inspector. This action does not read artifact payloads or expose CAS paths.
- Preserve missing MetaBlock addons as opaque unavailable cards. Their JSON sidecars are not rewritten; details appear only after the addon host is available.
- Discover Group files in `components/` from their static contract, add reusable reference instances, connect their named ports, run them through marimo, and inspect bounded named outputs. Use the Group card's open control to enter its Python/marimo scope; the back arrow restores the parent scope and its live notebook session.
- Pan and zoom the canvas with pointer drag, trackpad/wheel and the zoom controls. `F` fits the visible graph.

The browser code performs drawing, hit testing, pointer handling and transient selection only. `GraphimoApplication` validates commands, mutates canonical instance source, reads values through the marimo port, and writes layout coordinates to `.graphimo.json`. Marimo remains the execution and dependency engine.

## Host boundary

`MarimoHttpPort` owns the local marimo transport. It executes bounded inspection code in the active marimo context and returns JSON-safe view data. It never transfers arbitrary notebook values into browser JavaScript. The HTTP adapter is a transport for the same `GraphimoApplication` methods used by application-level tests.

The current Workbench implements W1 node editing, disabled blocks,
ArtifactStore inspection, W2 marimo-backed controls, opaque missing-addon
placeholders, and U4 Group references and nested navigation. Each open scope
uses a separate local marimo session, and Group execution itself uses a cloned
`App.run(defs=...)` with the declared input definitions. Undo/redo, active addon
catalogs and PyTorch editing remain outstanding. The application returns its
supported action list so the renderer can keep unavailable operations hidden.

U5 adds a separate persistent local service and Python client. The Workbench
continues to call the same `GraphimoApplication` directly until the ordered U6
migration; service/client semantics are tested independently of Zeron.

## Verification

`tests/test_marimo_http_workbench.py` launches the pinned marimo server and checks notebook node discovery, repeated instance insertion, connection/reconnection, execution, bounded output inspection, and one-time import of a library node. `tests/test_workbench_server.py` checks that the standalone renderer assets are served. The broader repository suite also checks package resources in the built wheel.
