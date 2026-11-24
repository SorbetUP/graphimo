# Standalone Graphimo Workbench

The Workbench is the first renderer for the new Graphimo application layer. It is a development surface for Core conformance; it is not the existing Zeron plugin UI.

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
- Pan and zoom the canvas with pointer drag, trackpad/wheel and the zoom controls. `F` fits the visible graph.

The browser code performs drawing, hit testing, pointer handling and transient selection only. `GraphimoApplication` validates commands, mutates canonical instance source, reads values through the marimo port, and writes layout coordinates to `.graphimo.json`. Marimo remains the execution and dependency engine.

## Host boundary

`MarimoHttpPort` owns the local marimo transport. It executes bounded inspection code in the active marimo context and returns JSON-safe view data. It never transfers arbitrary notebook values into browser JavaScript. The HTTP adapter is a transport for the same `GraphimoApplication` methods used by application-level tests.

The current Workbench W1 does not implement controls, undo/redo, disabled blocks, Group Blocks, nested scopes, addon catalogs or PyTorch editing. The application returns its supported action list so the renderer can keep unavailable operations hidden. Group Blocks remain blocked by the explicit human decision in the core contract.

## Verification

`tests/test_marimo_http_workbench.py` launches the pinned marimo server and checks notebook node discovery, repeated instance insertion, connection/reconnection, execution, bounded output inspection, and one-time import of a library node. `tests/test_workbench_server.py` checks that the standalone renderer assets are served. The broader repository suite also checks package resources in the built wheel.
