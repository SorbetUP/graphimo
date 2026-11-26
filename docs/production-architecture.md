# Graphimo production architecture

This document is the authoritative map of the production paths. Historical
prototypes are listed at the end and marked LEGACY — they are kept only for
their tests and must not receive new code.

## Runtime ownership

- **marimo owns the graph and execution.** Every notebook mutation is a marimo
  cell edit; Graphimo never runs user code outside the kernel.
- **The Graphimo service owns sessions and state.** `graphimo.service.server`
  exposes the authenticated loopback HTTP API; `ProjectSession` serializes
  mutations, journals operation ids (idempotent replay) and batches under one
  lock.
- **The Zeron plugin is the renderer.** All semantic actions go through
  `plugin/service_bridge.luau` → `graphimo.client` → the service. The plugin
  renders what `notebook.inspect` projects; it never re-derives semantics.

## Canonical Python modules

| Concern | Module |
| --- | --- |
| Application actions | `graphimo/application.py` |
| Node contract / ports | `graphimo/node.py`, `graphimo/projection.py` |
| Instances / bindings | `graphimo/instance.py` |
| Components | `graphimo/components.py` |
| Semantic projection | `graphimo/projection.py` |
| Kernel transport + allowlisted snippets | `graphimo/service/marimo_kernel.py`, `graphimo/service/kernel_action_sources.py` |
| Service sessions / batch / journal | `graphimo/service/sessions.py`, `graphimo/service/protocol.py` |
| `$ref`/`$seed` resolution (shared) | `graphimo/refs.py` |
| Artifacts (CAS store, catalogue, runs) | `graphimo/artifacts/` |
| MetaBlocks | `graphimo/metablocks.py` + `addons/*` dialects |
| Verifier | `graphimo/verifier.py` |
| Bounded previews | `graphimo/preview.py` |

## Canonical plugin files

`plugin/init.luau` (actions, inspector, undo), `plugin/canvas.luau` (render,
ports, edges), `plugin/base_block.luau` (block projection),
`plugin/service_bridge.luau` (service transport), `plugin/metablock_projection.luau`,
`plugin/layout.luau`, `plugin/component_library.luau`.

## LEGACY paths (do not extend)

- `adapter/` — old Zeron→marimo adapter; covered by `tests/test_luau_plugin_adapter.py`.
- `core/graphimo_core/` — experimental prototype; covered by `tests/test_core_vertical_slice.py`.
- `addons/pytorch/graphimo_pytorch/vertical_demo.py` — demo over the prototype.
- `plugin/marimo_bridge.luau` — direct marimo transport, kept for logs/sessions/telemetry only.

## Verification

`./scripts/check --fast` for the local gate, `./scripts/check --full` before
push. Live-kernel tests (`tests/test_live_*.py`) run real headless marimo.
The Verifier (`graphimo/verifier.py`) replays seeded scenarios through the same
service API, journals every operation, supports `$ref`/`$seed`, differential
journal comparison (`compare_journals`) and seeded replay fuzzing
(`GraphimoVerifier.fuzz_replay`).
