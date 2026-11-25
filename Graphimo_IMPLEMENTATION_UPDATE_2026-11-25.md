# Graphimo - Implementation Update 2026-11-25

Status: **normative update** to the following implementation contracts:

1. `Graphimo_CODEX_IMPLEMENTATION.md`
2. `Graphimo_GRAPHICAL_UX_IMPLEMENTATION.md`
3. `Graphimo_PYTORCH_PLUGIN_IMPLEMENTATION.md`

Target repository: `SorbetUP/graphimo`  
Target branch: `feat/graphimo-core-vertical-slice`  
Observed branch head before this update: `cb72700269cac020f00f69308ad02ef8c4594870`.

This document resolves the currently blocking product decisions and changes the implementation order. It is not a feasibility note. Codex must implement these decisions as written.

If an older contract contains a `HUMAN DECISION REQUIRED` that is explicitly resolved here, **this update wins**. If an unrelated conflict remains, the existing precedence rules of the three main contracts still apply.

---

## 0. Decisions resolved by this update

This update resolves the following outstanding decisions:

- the primary development UI/renderer;
- the Zeron-to-Graphimo integration boundary;
- human/agent parity and agent batch automation;
- agent-authored reusable components;
- the generic MetaBlock/addon host contract;
- the external value of the PyTorch MetaBlock;
- the generic Group Block execution backend;
- the local verification gate replacing GitHub CI for now;
- repository hygiene for generated build artifacts.

The following principles are now normative:

1. **The existing Graphimo Zeron plugin remains the primary product UI for now.**
2. **The standalone Workbench remains a development/conformance harness, not a second product UI to polish in parallel.**
3. **The Zeron UI and Zeron agents must call the same host-independent Graphimo application/service operations.**
4. **Agents may execute many Graphimo actions in one batch/script instead of performing one remote round-trip per edit.**
5. **MetaBlock addons extend Graphimo semantics without leaking domain-specific code into Core.**
6. **The PyTorch MetaBlock exports a `PyTorchModelDefinition`.**
7. **Generic Group Blocks execute through marimo `App.run(defs=...)`, wrapped as a callable Graphimo component.**
8. **The official validation gate is local before push. GitHub Actions are not required at this stage.**

---

# 1. Primary UI decision: continue with the existing Zeron plugin

The existing `plugin/` implementation remains the primary UI/renderer for Graphimo development and daily use.

Do **not** replace it with the standalone Workbench and do **not** wait for Zeron Design.

The reasons are practical:

- the plugin already exists and is already usable by the project owner;
- it already has a canvas, component library, notebook selection, logs and Zeron-native interaction patterns;
- it was designed with patterns learned from Orchestrator Builder;
- migrating immediately to another renderer would add risk without improving Graphimo semantics;
- the application layer already makes a future renderer migration possible.

### 1.1 Workbench role after this decision

The standalone Workbench remains in the repository and must continue to pass its tests, but its role is:

- host-independent conformance testing;
- debugging GraphimoApplication without Zeron;
- minimal visual reproduction of application behavior;
- regression testing for marimo integration;
- emergency fallback when debugging Zeron integration.

Do not spend substantial implementation time on Workbench visual polish unless a feature cannot be tested otherwise.

### 1.2 Zeron Design

Zeron Design is not a V1 dependency.

If Zeron Design later exposes a sufficiently complete and stable surface, Graphimo may migrate its renderer. This must not require changes to Graphimo Core, marimo semantics, addons, Group Blocks, ArtifactStore, or agent automation.

The current UI architecture must therefore preserve:

```text
Graphimo semantics/application layer
             |
      renderer adapter
       /           \
existing Zeron    future Zeron Design
plugin UI         or other renderer
```

---

# 2. Zeron integration boundary: persistent local Graphimo service

The current plugin directly contains significant marimo transport behavior in `plugin/marimo_bridge.luau`. The new Graphimo architecture must progressively move product semantics behind a persistent local Python Graphimo service.

This is **not MCP**.

The target structure is:

```text
Zeron UI -------------------\
                            \
Zeron agent tools ------------> local Graphimo service
                                 |
                                 v
                         GraphimoApplication
                           /      |       \
                      marimo   addons   ArtifactStore
```

### 2.1 Service responsibilities

Create a host-independent persistent service owned by the `graphimo` Python package. It must:

- create/reuse Graphimo project sessions;
- own `GraphimoApplication` instances;
- expose application intents through a bounded local protocol;
- expose addon/MetaBlock operations through the same application layer;
- expose incremental runtime/addon events;
- preserve marimo as the execution/dependency engine;
- never become a second scheduler;
- never put renderer-specific state into the functional layer.

Suggested module layout:

```text
graphimo/
  service/
    __init__.py
    server.py
    protocol.py
    sessions.py
    events.py
  client.py
```

Exact file splitting may vary, but responsibilities must remain separated.

### 2.2 Transport

V1 transport is local HTTP+JSON on loopback only.

Requirements:

- bind to `127.0.0.1` by default;
- allow port `0` so the OS selects a free port;
- generate an unguessable per-service token;
- do not expose the service on a public interface by default;
- reject oversized requests and malformed JSON;
- return structured errors with stable codes;
- use bounded response sizes;
- support incremental event retrieval using a cursor;
- no MCP server, MCP schema or MCP transport.

The existing Zeron plugin may continue to use `zeron.run("curl", ...)` as its transport mechanism. The important change is that Luau calls Graphimo service operations rather than reimplementing Graphimo semantics.

### 2.3 Event delivery

V1 uses cursor-based bounded long polling rather than requiring WebSocket/SSE integration in Zeron.

Example conceptual endpoint:

```text
GET /v1/events?project=<id>&cursor=<cursor>&limit=256&wait_ms=750
```

Response:

```json
{
  "cursor": "next-cursor",
  "events": [...],
  "dropped": 0
}
```

Requirements:

- event batches are bounded;
- event producers never block on UI consumption;
- PyTorch telemetry remains rate-limited by the addon;
- the UI can repeatedly long-poll while a project page is active;
- closing the page/project stops polling and releases subscriptions where appropriate.

A future Zeron-native streaming primitive may replace the transport without changing the Graphimo event contract.

### 2.4 Migration of the existing plugin

Do not rewrite the current UI from scratch.

Reuse its existing:

- page structure;
- canvas interaction code where still useful;
- component library presentation;
- logging/problem UI;
- Zeron design-system components;
- notebook/open-file flows where compatible.

Progressively replace direct semantic calls in `marimo_bridge.luau` with calls to the local Graphimo service.

`marimo_bridge.luau` may remain temporarily for startup/bootstrap or legacy fallback, but **new Graphimo semantics must not be implemented in Luau**.

---

# 3. Human and agent parity

A core Graphimo design rule is now:

> Every semantic operation available to a human through the UI must be invokable by an agent through the same Graphimo application/service layer.

The inverse does not need to be true: agents may have additional introspection/batch automation operations that have no direct button.

Examples:

```text
Human drags A.out -> B.input
        |
        v
GraphimoApplication.connect(...)

Agent requests connect
        |
        v
GraphimoApplication.connect(...)
```

There must not be a separate “agent editor” that writes different source forms or bypasses Graphimo validation.

### 3.1 Agent-readable information

Agents should be able to query at least:

- current project/session;
- active scope and breadcrumbs;
- blocks/elements;
- ports and declared/runtime types;
- edges/connections;
- node definitions and component source locations;
- return expressions and bounded output previews;
- running/stale/error/disabled state;
- diagnostics;
- available palette entries for the current scope;
- Group Block contracts;
- MetaBlock/addon identity and projected state;
- PyTorch shapes and generated source;
- runtime instances;
- ArtifactStore references and statistics;
- available addon actions;
- telemetry subscriptions and recent bounded events;
- checkpoint/migration reports;
- TorchLens inspection summaries when explicitly requested.

### 3.2 Agent semantic actions

Agents should be able to invoke at least:

- create/delete/duplicate node instances;
- connect/reconnect/disconnect;
- set literals;
- create/configure Control Blocks;
- run/disable/enable blocks;
- create/open/edit Group Blocks;
- enter/leave scopes;
- create/open/edit MetaBlocks;
- create/update/delete addon elements;
- change addon element parameters;
- promote/demote PyTorch parameters where supported;
- compile/materialize a MetaBlock;
- create/release PyTorch runtime instances through normal Graphimo execution;
- subscribe/unsubscribe activation or gradient telemetry;
- request TorchLens inspection;
- save/load checkpoints;
- inspect ArtifactStore;
- create/update reusable components;
- request validation/gate execution;
- manipulate layout when useful.

Purely cosmetic pan/zoom operations do not need to be agent tools unless an agent is explicitly editing layout.

---

# 4. Agent batching and scripts: avoid one-request-per-step workflows

Graphimo must not force an agent to make a network/tool round trip after every small edit.

Provide three levels of automation.

## 4.1 Atomic action

Useful for interactive agent work:

```text
execute(action, args)
```

This maps one action to the same application operation used by the UI.

## 4.2 Batch plan

Provide one operation that executes many semantic Graphimo actions sequentially in one request.

Conceptual request:

```json
{
  "operations": [
    {
      "id": "dataset",
      "action": "add_palette_item",
      "args": {"item_id": "load_dataset", "bindings": {}}
    },
    {
      "id": "norm",
      "action": "add_palette_item",
      "args": {"item_id": "normalize", "bindings": {"factor": "1.0"}}
    },
    {
      "action": "connect",
      "args": {
        "source_cell_id": {"$ref": "dataset.result.cell_id"},
        "target_cell_id": {"$ref": "norm.result.cell_id"},
        "input_name": "dataset",
        "output_index": 0
      }
    }
  ]
}
```

Requirements:

- operations run in order;
- later operations may reference prior operation results;
- default behavior stops on the first error;
- successful earlier operations remain applied in V1;
- no implicit global rollback transaction in V1;
- response contains a per-operation journal;
- `continue_on_error` may be added as an explicit option but defaults to false;
- every action still goes through the same validation/application code as an atomic human action.

Example response:

```text
1 OK create load_dataset
2 OK create normalize
3 OK connect dataset -> normalize
4 ERROR invalid port 'foo'
5-7 NOT EXECUTED
```

## 4.3 Local Graphimo Python client for complex automation

Provide a small local client library usable from normal Python:

```python
from graphimo.client import GraphimoClient

g = GraphimoClient.connect_local()
project = g.open_project("experiment.py")

data = project.add("load_dataset")
norm = project.add("normalize", factor=1.0)
project.connect(data.output(0), norm.input("dataset"))
project.run(norm)
```

This allows an agent to generate one temporary Python script and execute many operations in one local process instead of performing many Zeron tool calls.

This is ordinary local Python automation over the Graphimo service. It is **not MCP** and must not introduce a second execution scheduler.

The client must use the same service actions as the UI and batch API.

---

# 5. Agent-authored reusable components

Agents are first-class Graphimo developers and may create reusable project components.

A component is still ordinary Python code under `components/` containing one or more `@node` definitions.

Example:

```python
from graphimo import node

@node
def normalize_ct(volume, low: float = -1000.0, high: float = 1000.0):
    clipped = volume.clip(low, high)
    normalized = (clipped - low) / (high - low)
    return normalized
```

### 5.1 Dedicated component operations

Provide semantic operations such as:

```text
create_component(path, source)
update_component(path, source)
validate_component(path)
delete_component(path)
```

These operations do not force the agent to build source through tiny parameter-by-parameter calls. The agent may supply the full file source.

### 5.2 Validation pipeline after write

After create/update, Graphimo runs a bounded validation pipeline:

1. ensure path remains under the project `components/` root;
2. UTF-8 write to a temporary file;
3. Python parse/compile check;
4. `ruff format` on the file when available/configured;
5. `ruff check` on the file;
6. import/discovery check in an isolated/subprocess-safe context where necessary;
7. verify exported `@node` contracts with Graphimo introspection;
8. atomically replace the target file only after validation succeeds;
9. refresh component discovery/palette.

If validation fails, do not replace a previously valid component file.

The response must include diagnostics suitable for both humans and agents.

### 5.3 Direct file access

General Zeron file capabilities may still exist, but when a Graphimo component operation exists, agents should prefer it because it provides validation, atomic replacement and palette refresh.

---

# 6. Generic MetaBlock/addon host API - decision resolved

Core Section 31 is no longer “unfrozen”. Implement a small generic addon host contract.

The goal is not to define PyTorch in Core. The goal is to allow an addon to own a specialized semantic graph while Graphimo owns project/session/rendering orchestration.

## 6.1 Registration

Keep entry-point discovery through:

```text
graphimo.metablocks
```

A registration identifies at least:

```text
dialect_id
addon_version
api_version
factory
```

Loading an addon must not happen merely to list installed entry points. Import/runtime-heavy addon modules should remain lazy where possible.

## 6.2 Host services exposed to addons

Replace the empty `ExperimentalGraphimoAPI` with a bounded host API. It should expose services, not UI widgets and not domain concepts.

Minimum host services:

```text
project identity/path
sidecar persistence helpers
ArtifactStore access
application event emission
bounded logging/diagnostics
marimo materialization-cell update helper
current scope/navigation metadata
```

Do not expose PyTorch-specific helpers.

## 6.3 Addon dialect handler

A loaded MetaBlock dialect must implement the following semantic capabilities. Exact Python class names may follow repository conventions, but the responsibilities are normative.

```python
class MetaBlockDialect(Protocol):
    dialect_id: str

    def create_state(self, request, context): ...
    def catalog(self, state, scope, context): ...
    def project(self, state, scope, context): ...
    def apply(self, state, intent, context): ...
    def build_materialization(self, state, bindings, context): ...
    def invoke(self, state, action, args, context): ...
    def drain_events(self, cursor, limit, context): ...
```

### `create_state`

Creates a new addon-owned MetaBlock state from an explicit request/template.

### `catalog`

Returns the contextual component catalog for the current internal addon scope.

For PyTorch this can expose `Linear`, `RMSNorm`, `RoPE`, templates, etc. These entries remain invisible in normal Graphimo scope.

### `project`

Converts addon-owned state into generic renderer-safe projection data:

- cards/elements;
- ports;
- edges;
- groups/nested scopes;
- parameter descriptors;
- diagnostics;
- shapes/type labels;
- inspector sections/actions;
- generated-source previews where relevant.

Core does not interpret the addon element kinds.

### `apply`

Applies a semantic edit intent and returns updated addon state plus diagnostics/projection invalidation information.

Examples of generic intent families:

```text
create_element
update_element
delete_element
duplicate_element
connect
disconnect
create_group
update_group
set_parameter
```

An addon may reject unsupported intents.

### `build_materialization`

This is the important parent-Graphimo boundary.

The Graphimo application/service and addon run in a different process from the active marimo kernel. Therefore the addon does **not** return a live Python object to the renderer/service.

Instead it returns a canonical materialization-cell specification: source/import information that Graphimo inserts/updates in the marimo notebook so the addon value is created **inside the marimo kernel**.

The result must include at least:

```text
canonical generated source
result variable name
declared external output type/label
bindings to parent inputs
state/revision dependency
```

Graphimo owns the outer cell markers and lifecycle; the addon owns the domain expression/source needed to materialize its value.

### `invoke`

Runs an explicit addon action that is not a graph edit.

Examples for PyTorch:

```text
subscribe activation
unsubscribe
TorchLens inspect
save checkpoint
load checkpoint
migration report
```

Core only sees action names/arguments/results/events.

### `drain_events`

Returns bounded addon events using the generic Graphimo event envelope/cursor mechanism.

## 6.4 Persistence

Core continues preserving addon sidecar bytes when the addon is absent.

When the addon is installed:

- Core loads the sidecar JSON as opaque data;
- the addon parses/validates its own schema;
- addon mutations return a new serializable state;
- Core writes sidecar updates atomically;
- layout remains separate from semantic addon state.

Uninstalling an addon must never destroy or rewrite its semantic state.

## 6.5 No PyTorch leakage

The following remain forbidden in Graphimo Core:

```text
torch
nn.Module
Tensor
optimizer
gradient
activation
TorchLens
Linear
RMSNorm
PyTorch checkpoint semantics
```

The generic API must be sufficient without these concepts.

---

# 7. PyTorch MetaBlock external value - decision resolved

Core Section 32 is resolved.

The external parent-scope value of a PyTorch model MetaBlock is:

```python
PyTorchModelDefinition
```

This is addon-owned. Graphimo Core treats it as an opaque Python value produced by the MetaBlock materialization cell.

The PyTorch addon already contains the correct conceptual object with:

```text
metablock_id
graph_revision
materialization_key
model_name
generated_source
external_parameters
runtime bindings
instantiate()
```

### 7.1 Parent cell

The addon must generate a materialization cell that recreates/loads the `PyTorchModelDefinition` inside the active marimo kernel from the persisted MetaBlock state and connected external parameters.

Conceptually:

```python
# Graphimo metablock: pytorch.model
# Graphimo instance: 4f2a7c91

g_pytorch_4f2a7c91 = graphimo_pytorch.materialize_definition(
    state_path=".graphimo/metablocks/4f2a7c91.pytorch.json",
    external_values={
        "d_model": g_width_slider_a192.value,
    },
)
```

Exact helper naming may differ, but semantics must match.

A downstream normal Graphimo node receives this object normally:

```python
@node
def train(model_definition, dataset):
    model = model_definition.instantiate(device="cuda")
    ...
```

### 7.2 Why this value is normative

It preserves:

- model definition separate from model instantiation;
- generated source;
- graph revision/provenance;
- visual-to-runtime bindings;
- external parameter materialization identity;
- runtime hooks/TorchLens mapping after instantiation.

Do not replace it with an already-instantiated `nn.Module` or a raw class/factory.

---

# 8. Generic Group Block backend - decision resolved

Core Section 28 is resolved.

A generic Graphimo Group Block is a reusable **marimo sub-workflow** stored as an ordinary Python/marimo file and called from the parent graph as one Graphimo instance.

The execution backend is marimo `App.run(defs=...)`.

Pinned project runtime remains `marimo==0.25.2` until explicitly upgraded.

## 8.1 Group file

A Group Block has:

```text
group Python file containing a marimo.App
Graphimo-managed input contract
Graphimo-managed output contract
layout metadata for its internal scope
```

The internal blocks remain normal Graphimo/marimo cells. Opening the Group Block in Graphimo shows this internal scope and allows normal editing.

## 8.2 Inputs

Graphimo maintains a dedicated group input-definition cell whose definitions correspond exactly to the Group Block's external input ports.

Example conceptual group file:

```python
import marimo

app = marimo.App()

@app.cell
def __graphimo_inputs():
    dataset = None
    config = None
    return dataset, config

# normal Graphimo-managed cells follow
```

When executing the Group, Graphimo overrides these definitions through:

```python
outputs, defs = group_app.run(
    defs={
        "dataset": dataset_value,
        "config": config_value,
    }
)
```

All definitions produced by the overridden input cell must be supplied together as required by marimo semantics.

## 8.3 Instance isolation

Each Group Block instance must use an independent app instance when required by marimo's state model.

Use `app.clone()` before `run()` for an independently executing Group instance unless the pinned marimo API guarantees equivalent isolation for the implementation path.

Do not share mutable notebook runtime state accidentally between two parent Group instances.

## 8.4 Outputs

The Group contract explicitly declares which marimo definitions are exported as Graphimo output ports.

The Group runner returns one opaque Python result object representing all declared outputs, consistent with normal Graphimo instance semantics.

Provide a small Core `GroupResult` mapping/object supporting stable named access in generated code, for example:

```python
g_pipeline_93ab21ef = graphimo.groups.run_group(
    "components/bodypart_pipeline.py",
    inputs={
        "dataset": g_dataset_a1.dataset,
        "config": g_config_b2.value,
    },
    outputs=("model", "metrics"),
)
```

Downstream generated wiring may use:

```python
g_pipeline_93ab21ef.model
g_pipeline_93ab21ef.metrics
```

The user does not edit these generated names manually.

## 8.5 No Graphimo scheduler

`run_group` is a wrapper around marimo app execution. It must not implement its own DAG traversal/scheduler/cache.

marimo owns execution inside the Group just as it owns execution in the parent notebook.

## 8.6 Group import/reuse

Group Blocks are reusable project/library components.

Default behavior is reference/import, not source duplication.

A future explicit “vendor/copy into project” action may copy a Group implementation, but V1 reference semantics remain the default.

---

# 9. Agent-facing Zeron tool surface

The existing six tools are insufficient for the new Graphimo architecture.

Do not create one Zeron tool for every tiny internal method. Expose a compact orchestration surface backed by the Graphimo service.

Minimum recommended tools:

```text
graphimo_inspect
graphimo_execute
graphimo_execute_batch
graphimo_component_write
graphimo_validate
graphimo_events
```

Names may follow existing plugin naming conventions.

### `graphimo_inspect`

Reads project/scope/block/addon/artifact/runtime information.

### `graphimo_execute`

Executes one semantic Graphimo application/addon action.

### `graphimo_execute_batch`

Executes a full multi-step plan and returns the journal/results.

### `graphimo_component_write`

Creates or updates reusable Python component source with validation.

### `graphimo_validate`

Runs an appropriate local validation subset or reports current diagnostics.

### `graphimo_events`

Reads bounded incremental events when an agent needs live/runtime information.

The exact Zeron tool schemas may be adapted to the plugin SDK, but **all tools route to the same Graphimo service/application operations**.

Agents may additionally generate/run local Python scripts using `graphimo.client` for complex automation.

---

# 10. Local verification gate replaces GitHub CI for now

GitHub Actions are not required at this stage because the project owner wants to conserve hosted CI credits.

The repository must instead have a reproducible local gate.

Create:

```text
scripts/check
scripts/check.py
```

or an equivalent cross-platform entry point. `scripts/check` may be a thin shell wrapper around Python.

## 10.1 Fast gate

Command:

```bash
./scripts/check --fast
```

Must include at least:

- Python syntax/import sanity;
- `ruff format --check` for affected Python where practical;
- `ruff check`;
- Core node/instance/application tests;
- Control tests;
- ArtifactStore tests;
- fast addon schema/validation/shape/compiler tests that do not require expensive optional integration.

Target: frequent local use during development.

## 10.2 Full pre-push gate

Command:

```bash
./scripts/check --full
```

Must include at least:

- everything in `--fast`;
- complete pytest suite;
- live marimo Workbench tests;
- package-distribution tests;
- root Graphimo wheel build;
- `graphimo-pytorch` wheel build;
- isolated addon install/discovery/reinstall test;
- Core/addon import-boundary test;
- PyTorch CPU compiler/runtime tests;
- checkpoint/migration tests;
- exact pinned TorchLens integration tests;
- Llama reduced-fixture numerical equivalence tests;
- repository generated-artifact hygiene check;
- `pip check` or equivalent dependency consistency check in the prepared development environment.

If a test depends on an explicitly required development extra, `--full` must fail with a clear missing-dependency message rather than silently skipping it.

Tests that are intentionally platform-specific may report an explicit supported skip, but the script must summarize skips at the end.

## 10.3 Optional pre-push hook

Provide an optional repository hook installer:

```text
scripts/install-hooks
.githooks/pre-push
```

The hook runs:

```bash
./scripts/check --full
```

The project owner may bypass the hook intentionally when needed, but the full local gate remains the normal pre-push policy.

## 10.4 No hosted CI requirement

Do not add GitHub Actions merely to satisfy a generic “CI required” convention. Local gate success is the current official verification signal.

A hosted CI workflow may be added later when credits/cost policy changes.

---

# 11. Repository hygiene - immediate cleanup

The current branch contains generated package artifacts under paths such as:

```text
addons/pytorch/build/
addons/pytorch/graphimo_pytorch.egg-info/
```

These must not remain tracked source.

Update `.gitignore` with at least:

```gitignore
build/
dist/
*.egg-info/
```

Also ignore transient marimo/cache artifacts produced by the local development flow when they are not project state.

Remove already tracked generated build/egg-info files from Git while preserving real source under:

```text
addons/pytorch/graphimo_pytorch/
```

Build/package tests must build into temporary directories and must not dirty the working tree.

Update README/status docs so they no longer describe the PyTorch addon as only P1 when P1-P9 backend work is present.

---

# 12. Implementation order after this update

Codex should continue from the current branch in this order.

## U1 - repository hygiene and local gate

1. clean generated build artifacts;
2. update `.gitignore`;
3. implement `scripts/check --fast/--full`;
4. run full local gate;
5. commit.

## U2 - freeze and implement generic MetaBlock host API

1. replace empty experimental host protocol with the generic bounded API defined here;
2. implement addon loading/handler lifecycle;
3. implement sidecar state load/apply/save path;
4. expose addon catalog/project/apply/materialization/invoke/events through `GraphimoApplication`;
5. preserve missing-addon behavior;
6. update tests;
7. commit.

## U3 - connect `graphimo-pytorch` to the host API

1. adapt existing PyTorch projection to generic addon projections;
2. adapt existing immutable edit operations to `apply` intents;
3. implement canonical materialization cell producing `PyTorchModelDefinition` inside marimo;
4. wire runtime events/subscriptions/TorchLens through generic addon action/event paths;
5. do not move PyTorch code into Core;
6. commit.

## U4 - implement generic Group Blocks

1. add Group contract representation;
2. add Group input/output managed cells;
3. add `graphimo.groups.run_group` based on cloned marimo `App.run(defs=...)`;
4. implement one opaque result variable with named output access;
5. add Group palette/import/reuse;
6. add nested scope navigation in application layer;
7. test repeated Group instances and isolation;
8. commit.

## U5 - local Graphimo service

1. create persistent local service;
2. expose GraphimoApplication intents;
3. expose batch execution;
4. expose cursor-based events;
5. create `graphimo.client`;
6. keep loopback/token bounds;
7. test service independent of Zeron;
8. commit.

## U6 - migrate existing Zeron plugin to the service

1. keep current UI/canvas/components presentation where useful;
2. replace semantic marimo bridge operations with service intents;
3. preserve user-visible behavior during migration;
4. add contextual MetaBlock palettes/scopes;
5. render PyTorch projections in the existing Graphimo canvas;
6. expose live event inspection;
7. do not create a separate PyTorch canvas application;
8. commit in small migration steps.

## U7 - agent parity and automation

1. expand Zeron tools to inspect/execute/batch/component-write/events;
2. ensure tools call the same service methods as the UI;
3. implement batch result references and per-step journal;
4. implement component write/validation pipeline;
5. expose local Python `GraphimoClient` examples for agents;
6. test an agent-equivalent workflow that creates components, constructs a graph, creates a PyTorch MetaBlock, connects it, runs it and inspects results;
7. commit.

## U8 - UX completion against the graphical contract

After semantic integration is functional, continue the existing `Graphimo_GRAPHICAL_UX_IMPLEMENTATION.md` requirements in the Zeron plugin renderer:

- contextual palettes;
- nested scope navigation;
- Group/MetaBlock breadcrumbs;
- rich outputs;
- controls;
- stale/error/running states;
- PyTorch shape labels;
- generated source inspector;
- activations/gradients/TorchLens controls;
- undo/redo where specified;
- layout/navigation polish.

The Workbench only needs enough updates to remain a useful conformance/debug harness.

---

# 13. Acceptance scenarios added by this update

## 13.1 Human Zeron workflow

The user must be able to:

1. open Graphimo in Zeron;
2. open/create a marimo project;
3. create ordinary `@node` instances;
4. connect them;
5. add a slider and see reactive results;
6. create/open a Group Block;
7. enter the Group and edit its internal Graphimo workflow;
8. create a PyTorch MetaBlock;
9. enter it and see the PyTorch-only component catalog;
10. create/edit a model or insert the Llama template;
11. return to the parent graph;
12. connect the PyTorch `PyTorchModelDefinition` output to an external training node;
13. run training through normal marimo execution;
14. inspect activation/gradient telemetry on internal model elements;
15. request explicit TorchLens inspection;
16. save/load checkpoint artifacts.

## 13.2 Agent-equivalent workflow

An agent must be able to perform the same semantic workflow without directly editing Graphimo internal state:

1. inspect project and palette;
2. create/update a reusable `@node` component source;
3. pass validation;
4. add two or more block instances in one batch/script;
5. connect them;
6. create a control;
7. create a PyTorch MetaBlock/template;
8. edit at least one PyTorch parameter;
9. connect the MetaBlock output to a training node;
10. run the relevant target;
11. read bounded outputs/events;
12. subscribe to one internal activation;
13. request a TorchLens inspection;
14. store a checkpoint;
15. inspect resulting ArtifactRef/provenance.

The agent may use one batch request or one local Graphimo Python script for the multi-step construction. It must not be forced into one network/tool round trip per edit.

## 13.3 Local gate workflow

Before push:

```bash
./scripts/check --full
```

must succeed on the developer machine.

The check must not leave tracked/untracked build artifacts that make the repository dirty, except explicitly documented cache directories ignored by Git.

---

# 14. Explicit non-goals of this update

Do not implement the following merely because this update mentions adjacent concepts:

- MCP server/protocol;
- GitHub Actions as the main validation gate;
- Zeron Design migration;
- arbitrary Python-to-PyTorch-graph reconstruction;
- a second Graphimo scheduler;
- a second PyTorch-only canvas application;
- global transactional rollback for agent batch scripts;
- automatic arbitrary shell execution inside the Graphimo semantic API;
- a generic distributed runtime;
- forcing every user/agent action through one tiny Zeron tool call.

---

# 15. Final implementation posture

After this update, Codex should no longer treat Core §28, Core §31 or Core §32 as unresolved blockers.

The decisions are:

```text
Core §28 Group backend
    -> marimo App.run(defs=...) through a callable Group wrapper

Core §31 MetaBlock host API
    -> generic catalog/project/apply/materialization/invoke/events contract

Core §32 PyTorch external model value
    -> addon-owned PyTorchModelDefinition
```

The existing Zeron plugin is now the primary renderer to improve. The Workbench remains a test harness. Zeron agents and humans share the same Graphimo service/application semantics, while agents additionally gain batch/script automation and richer introspection.

Local `scripts/check --full` is the required pre-push validation gate until the project owner explicitly enables hosted CI again.
