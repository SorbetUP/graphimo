---
name: zeron-plugin-builder
description: Create, update, review, or package Zeron v3 plugins in Luau. Use for plugin manifests, UI pages, agent tools, services, permissions, signing, or plugin-specific debugging.
---

# Zeron Plugin Builder

Build Zeron plugins quickly from the installed SDK while keeping the manifest, code, permissions, and signed package consistent.

## Start with the actual host version

1. Read the repository's `docs/zeron-plugin-api.md` when available.
2. Inspect the plugin's `zeron-plugin.json`, entry file, SDK copy (`zeron.luau`), and any existing tests or examples. For a new plugin, use `zeron plugin new` with the target Zeron version and treat its generated files as the authority.
3. Resolve uncertainty from source signatures/comments and that version's schema before relying on DeepWiki. DeepWiki explains Zeron's internals; it is not a complete plugin API catalogue. Keep `ui.*`, `@ds/*`, private RPCs, and external service APIs separate from the public `zeron.*` SDK.
4. When adapting Graphimo or Orchestrator Builder, distinguish reusable Zeron APIs from their private bridge, workflow, and domain contracts. Do not copy Graphimo's private marimo transport as a general plugin API.

## Implement the smallest complete slice

- Write down the input, output, side effects, failure behavior, and data limits before coding.
- Keep the manifest ID identical to `require("./zeron")(id)`. Keep every entry/resource path valid and included in the package.
- Request the smallest permission set for the planned calls. For local commands, declare both the `commands` permission and the exact executable names. Pass arguments as an array to `zeron.run`; never build shell command strings.
- For agent tools, declare a strict JSON schema and register the same tool name with `zeron.tool`. Validate arguments again at runtime, bound output, and return a stable result shape or a useful error.
- Keep domain validation and transport separate from UI code. Use host `ui` and `@ds/*` primitives only after confirming them in the generated plugin or matching examples.
- For interactive canvases and diagrams, consult the “Pavé tactile et interactions de canvas” section in the API guide. Verify the target runtime's `on_press`, `on_pointer_move`, `on_release`, `on_wheel`, and `on_pinch` event fields; implement selection/drag, background pan, two-finger scroll pan, modifier-wheel zoom, and pinch zoom only where the component supports them. Keep zoom anchored under the gesture, clamp scale, and clean up gesture state on release/unmount. These callbacks are runtime UI behavior, not stable `zeron.*` APIs.
- Handle both branches of asynchronous calls with `:after(success, failure)`. `zeron.on` returns both an unsubscribe function and listener-registration promise; await registration before relying on events and unsubscribe when the owner is done.
- Treat agent text, files, GitHub responses, imports, and device messages as untrusted. Bound lengths and collections, validate IDs/paths/URLs, avoid logging secrets, and use SQLite parameters rather than SQL interpolation.
- Treat timeouts on side-effecting calls as ambiguous. Check persisted or remote state before offering a retry; use IDs/nonces for correlation and idempotency where the operation supports them.
- Put work that must run without an open window in a service when the target runtime supports it. Give listeners, timers, and background work an explicit stop path.

## Verify before signing

Check that the manifest and implementation agree: ID, entry, assets, tools, settings, permissions, allowed commands, and optional service. Check normal, invalid-input, missing-context, denied-permission, offline, and timeout paths at a level appropriate to the change. Use existing repository validation commands; do not invent host APIs to make a check pass.

If the user asked for an end-to-end check, install or open the plugin in Zeron, exercise its UI and tool path, and inspect the consent prompt. Sign only after the last source, SDK, manifest, and resource edit with `zeron plugin sign <id>` while Zeron is running. Then install/check the signed artifact. Do not publish, install to a shared environment, or grant permissions beyond the user's request.

## References

- [Plugin API, permissions, workflows, limits, and Graphimo/Builder examples](../../../docs/zeron-plugin-api.md)
- Graphimo source: `plugin/zeron.luau`, `plugin/zeron-plugin.json`, `plugin/init.luau`, `plugin/marimo_bridge.luau`.
- Orchestrator Builder source and its separate detailed guide: https://github.com/Master0fFate/zeron-orchestration-builder/blob/initial-release/docs/creer-un-plugin-zeron.md

When the reference conflicts with the generated SDK or installed runtime, use the installed version and update the reference with the verified difference.
