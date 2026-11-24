# Graphimo - Graphical Development Experience Implementation Contract

Status: **normative companion** to `Graphimo_CODEX_IMPLEMENTATION.md`.

This document specifies the graphical development experience of Graphimo with the same level of precision as the main functional/architecture implementation contract. It is not a style guide and it is not optional polish. Graphimo exists to make Python, data-science and machine-learning development faster through direct graphical manipulation; therefore the graphical interaction model is a product contract.

If this document conflicts with the main implementation contract, stop at the conflict and reconcile the two specifications explicitly. Do not let a renderer silently override functional semantics.

The development order is:

1. implement the host-independent functional/application layer;
2. expose it through a standalone Graphimo Workbench used for development and manual testing;
3. implement the production Zeron renderer/adapter against the same application layer;
4. preserve equivalent semantics across both hosts.

The Workbench may be visually plain. It must not contain business logic that later has to be rewritten in Zeron.

---

# 1. Product intent - normative

Graphimo is a **graphical development environment**, not merely a visualization of a Python notebook.

A correct Graphimo implementation lets a user perform ordinary development actions without manually editing generated wiring code:

- discover reusable `@node` definitions;
- instantiate the same node multiple times;
- connect/reconnect outputs to inputs;
- edit supported literal parameters;
- add interactive controls such as sliders and dropdowns;
- observe reactive consequences directly;
- inspect values and rich outputs;
- navigate into Group Blocks and MetaBlocks;
- create and reuse groups/templates;
- observe execution state and errors;
- inspect specialized addon state such as a PyTorch model architecture;
- return to source code when desired without requiring source editing for routine graph operations.

A graph that is technically correct but requires the user to open generated Python every time they connect a block, tune a parameter, inspect an output or navigate nested workflows is **not complete Graphimo**.

Graphimo must remain source-faithful. Graphical convenience does not justify creating a hidden second scientific program.

---

# 2. Renderer separation - mandatory architecture

All semantics described in this document must be represented by a host-independent application layer.

The renderer is responsible for:

- drawing;
- hit testing;
- pointer/keyboard input;
- focus;
- local ephemeral interaction state such as an in-progress drag;
- host-native panels/dialogs where appropriate.

The renderer is **not** responsible for:

- parsing Python;
- generating Graphimo-owned Python source;
- deciding whether a connection is semantically valid;
- deciding what components exist in the current scope;
- mutating Graphimo project state directly;
- compiling MetaBlocks;
- running marimo;
- interpreting PyTorch semantics;
- storing authoritative output values;
- implementing ArtifactStore behavior.

The Workbench and Zeron must both consume the same semantic commands/view models.

A useful conceptual boundary is:

```text
Graphimo functional/application layer
  - project/session
  - source mutation
  - marimo adapter
  - scope model
  - palette resolution
  - addons
  - outputs
  - events
        |
        +---- Workbench renderer
        |
        +---- Zeron renderer
```

Do not fork the product into `graphimo_workbench_logic` and `graphimo_zeron_logic`.

---

# 3. Application-facing UI state - normative minimum

The renderer should receive structured state rather than infer semantics from raw notebook source.

The exact class names may adapt to the existing codebase, but the application layer must be able to provide the equivalent of:

```python
@dataclass(frozen=True)
class GraphimoViewState:
    project_id: str
    active_scope_id: str
    breadcrumbs: tuple[ScopeCrumb, ...]
    blocks: tuple[BlockView, ...]
    edges: tuple[EdgeView, ...]
    palette: ScopePalette
    selection: tuple[str, ...]
    run_state: ProjectRunState
    diagnostics: tuple[DiagnosticView, ...]

@dataclass(frozen=True)
class BlockView:
    id: str
    kind: str
    title: str
    subtitle: str | None
    x: float
    y: float
    width: float
    height: float
    state: str
    inputs: tuple[PortView, ...]
    outputs: tuple[PortView, ...]
    body: BlockBodyView
    badges: tuple[BadgeView, ...]
    can_open: bool
    can_edit_source: bool
    can_disable: bool

@dataclass(frozen=True)
class PortView:
    id: str
    name: str
    direction: str
    declared_type: str | None
    runtime_type: str | None
    expression: str | None
    preview: str | None
    connected: bool
    compatible_hint: str | None
```

These are not mandated public API names. They define the information boundary that must exist.

The renderer must never need to parse a `return` statement to label a port.

---

# 4. UI intents/commands - host-independent contract

User actions should be translated into semantic intents handled by the application layer.

The equivalent operations must exist:

```text
open_project(path)
close_project()
refresh_project()

add_palette_item(item_id, scope_id, position)
delete_elements(ids)
duplicate_elements(ids, offset)
move_elements(ids, delta)
resize_element(id, rect)              # only for resizable kinds

connect(source_port, target_port)
reconnect(edge_id, new_source_or_target)
disconnect(edge_id)

set_literal(instance_id, input_name, value)
set_control_value(control_id, value)
set_control_policy(control_id, policy)

disable_instance(id)
enable_instance(id)

enter_scope(container_id)
leave_scope()
open_scope(scope_id)

create_group(selection, name)
ungroup(group_id)                      # when supported

run_target(id)
run_selection(ids)                    # if supported by execution semantics
stop_running_target(id)               # where marimo supports cancellation semantics

open_source(node_or_instance_id)
open_inspector(id)

undo()
redo()
```

If an operation is unavailable in the current milestone, the renderer must receive that fact from the application layer and disable/hide the action. Do not fake success.

---

# 5. Project shell layout

The Workbench and Zeron renderer may use different host-native chrome, but they must expose the same conceptual regions.

Minimum regions:

1. **Canvas** - primary work surface.
2. **Component Library** - contextual palette for the current scope.
3. **Inspector/Details** - selected block/output/control/addon detail.
4. **Project/Navigation area** - current project and nested scope breadcrumbs.
5. **Execution feedback** - visible run/error/stale status without requiring a terminal.
6. **Logs/diagnostics** - available when deeper debugging is needed, but not the normal path for understanding ordinary errors.

The canvas must remain the dominant surface. Panels should not permanently consume excessive width when closed or unused.

The existing Zeron UI can map these regions to host-native panels; the Workbench may use a simple split layout.

---

# 6. Canvas coordinate and layout behavior

The canvas uses a world-coordinate system independent of screen pixels.

Required behavior:

- pan does not mutate block world positions;
- zoom is centered around the pointer or viewport center according to host capability;
- moving blocks changes their stored layout coordinates only;
- reconnecting or executing a node must not move it;
- scope entry preserves each scope's independent camera position where practical;
- fit-to-graph computes bounds from visible elements in the active scope;
- fit-selection computes bounds from the selected elements;
- layout metadata remains presentation state, not Python execution truth.

Default block positions may be generated automatically for new elements, but auto-layout must never silently reorder an established user layout.

A future explicit auto-layout command is allowed; it is not the default behavior of opening a project.

---

# 7. Pan, zoom and navigation interaction

Required desktop interactions:

- wheel/trackpad zoom where host conventions support it;
- two-finger/pointer pan or middle-button/space-drag pan according to host conventions;
- keyboard shortcut for fit graph;
- keyboard shortcut/action for focus selection;
- reset zoom action;
- zoom range bounded to avoid numerical instability and unreadable extremes;
- controls inside cards must not accidentally drag the canvas when the user manipulates them.

When the cursor is over an interactive control such as a slider, wheel events should follow control/host conventions rather than unexpectedly zoom the entire graph unless the user uses the canvas zoom modifier.

---

# 8. Selection model

Graphimo must support:

- single click selects one element;
- click on empty canvas clears selection;
- modifier-click toggles items in multi-selection;
- drag rectangle selects intersecting/contained elements according to a single documented rule;
- moving one selected block moves the entire selection;
- deleting a multi-selection performs one semantic transaction;
- grouping a multi-selection uses exactly the selected semantic elements;
- selection does not alter execution state.

Edges may be selectable independently for disconnect/delete/inspection.

Ports are interaction targets but are not persistent top-level selection objects unless the renderer needs ephemeral focus.

Selection should survive harmless view refreshes if the selected IDs still exist.

---

# 9. Contextual component library - core rule

The component library is **scope-aware**.

Do not show every installed component everywhere.

The active scope has a `dialect_id`. The application layer resolves the palette for that dialect and current project visibility.

Normal Graphimo Python scope may include:

- project/user `@node` definitions;
- imported nodes that are intentionally exposed;
- Group Blocks;
- MetaBlock entry points from installed addons;
- Control Blocks;
- supported display/annotation organizational elements.

PyTorch-internal components such as `Linear`, `RMSNorm`, `RoPE`, `MatMul`, `Softmax` or `LlamaDecoderLayer` **must not appear in the normal Python palette**.

Entering a PyTorch MetaBlock changes the palette to the PyTorch dialect catalog.

Leaving it restores the parent catalog immediately.

---

# 10. Palette item model

A host-independent palette model should carry at least:

```python
@dataclass(frozen=True)
class PaletteItem:
    id: str
    title: str
    category: str
    kind: str
    description: str | None
    search_terms: tuple[str, ...]
    source_provider: str
    icon_hint: str | None
    enabled: bool
    disabled_reason: str | None
```

`kind` is a renderer/application hint, not a closed universal domain taxonomy.

The renderer sends only the selected item ID and placement intent. The application/addon layer creates the semantic element.

Renderer code must not contain hard-coded lists of PyTorch layers.

---

# 11. Palette search and browsing

Required behavior:

- search is literal/fuzzy enough to find common component names quickly;
- search covers title, description, category and explicit search terms;
- search defaults to the **current scope catalog**, not hidden addon internals;
- categories are collapsible;
- commonly/recently used items may be surfaced later but cannot hide the canonical full catalog;
- keyboard navigation through search results is supported before production release;
- selecting a result by keyboard or click creates the same semantic item;
- unavailable/missing-addon items are not offered as creatable components.

Search should remain fast with hundreds or low thousands of palette entries. Filter/index the catalog rather than performing expensive source discovery on every keystroke.

---

# 12. Templates and high-level components

Templates are first-class palette entries supplied by the core project or an addon.

A template creates a predefined graph/subgraph through application semantics.

For example, `graphimo-pytorch` may expose:

```text
Templates
  Attention
  MLP
  LlamaDecoderLayer
  Llama-style causal LM
```

Selecting `LlamaDecoderLayer` may create a Group Block/subgraph containing lower-level PyTorch elements.

Selecting a full architecture template may create multiple nested groups and connections.

Graphimo Core does not know what Llama, Attention or MLP mean.

Template creation must be one undoable transaction.

Templates must not be implemented as renderer-side macros.

---

# 13. Drag/drop and click-to-insert

The palette should support at least one low-friction insertion path. Preferred production behavior supports both:

- drag palette item onto canvas at a chosen position;
- click/add item, then place it at viewport center or a sensible free location.

During drag:

- show placement preview;
- do not mutate project state until drop succeeds;
- cancel cleanly with Escape or pointer cancellation;
- if the item is invalid in the destination scope, refuse before mutation and explain why.

Insertion creates a real semantic element through the application layer and is immediately reflected in source/project state according to its type.

---

# 14. Normal executable block card anatomy

A normal `@node` instance card has a consistent information hierarchy.

Conceptual anatomy:

```text
[status]  NodeName                         [actions]

input_a  o          body/parameters         o  output A
input_b  o                                  o  output B

compact output/runtime summary
```

Required information:

- node/function display name;
- status;
- input ports in signature order;
- supported literal/default editors;
- output ports in return order;
- declared type and return expression when known;
- bounded runtime preview when available;
- compact error/progress indicator;
- affordance to open source;
- affordance to inspect details.

The exact shape/colors are renderer-specific.

---

# 15. Input port presentation

Input ports are derived from the node signature or dialect-specific semantic contract.

For normal Python nodes show:

- parameter name;
- safe annotation/type if available;
- whether a value is connected;
- editable literal/default value when supported;
- required/optional state where useful.

A connected input should visually distinguish its upstream source from an editable literal.

When connected, the literal editor may remain visible but disabled/secondary according to UX choice; the user must not accidentally believe both values are active.

Removing a connection should restore the previous explicit literal/default when the source contract safely supports it, or show the unresolved input state.

---

# 16. Output port presentation

Normal Python node output display follows the main implementation contract.

Examples:

Before execution:

```text
float (prix * nbr)
Tensor (calc(x))
float (c | d)
```

After execution:

```text
float (34.2 | prix * nbr)
Tensor (shape=(32, 768), dtype=float32 | calc(x))
float (4.2 | c)
```

The connection anchor must remain stable even when the preview text changes length.

Multi-output nodes expose independent connection anchors in return order.

Tuple indices are never shown as user-facing output names.

---

# 17. Edge rendering

Edges represent semantic dependencies/connections.

Required properties:

- source and target ports are visually unambiguous;
- selected edge is distinguishable;
- hovered edge is distinguishable;
- invalid/broken/stale edges can be represented without pretending they are valid;
- crossing edges remain readable at ordinary graph sizes;
- edge geometry updates while blocks move;
- edge geometry is presentation-only and not stored as program truth.

Bezier/orthogonal routing is renderer-specific.

Do not encode semantic type solely by color.

---

# 18. Create connection interaction

Standard flow:

1. user starts drag from an output port;
2. potential compatible input ports receive a visible affordance;
3. incompatible targets are visibly unavailable or produce a clear refusal;
4. dropping on a valid target sends one `connect(...)` intent;
5. source mutation occurs only after the application layer accepts it;
6. marimo/source refresh updates canonical graph state;
7. renderer redraws from canonical state.

Do not draw a permanent edge before the application confirms the semantic mutation.

If application mutation fails, remove the preview edge and display the reason.

---

# 19. Reconnection and disconnection

The user must be able to change an input source without opening code.

Required:

- drag an already-connected input to a new source, or use an equivalent host-native reconnect action;
- disconnect edge through selected edge action/context menu;
- reconnect changes only the target generated call argument for normal `@node` instances;
- other bindings remain byte-equivalent/canonically equivalent;
- undo restores the previous connection.

Do not delete the upstream block when disconnecting an edge.

---

# 20. Compatibility feedback

For normal Python nodes, static compatibility can be conservative.

Graphimo may use safe annotations when available to offer hints, but Python dynamic typing means the renderer must not overclaim compile-time safety.

Required behavior:

- clearly impossible structural connections defined by the active dialect may be refused;
- likely mismatches may warn rather than hard-fail in normal Python scope;
- runtime errors remain visible after execution;
- addons such as PyTorch may enforce stronger shape/type rules inside their own dialect.

Graphimo Core must not pretend to implement a universal Python type checker.

---

# 21. Literal parameter editing

Supported literal parameter values should be editable directly on the block card or inspector.

V1 safe literal categories may include:

- bool;
- int within JSON-safe UI range;
- finite float;
- string;
- `Literal[...]` / select choices where safely inspected;
- explicit `None` where meaningful.

Editing a literal must mutate the canonical Graphimo-owned generated call source through the application layer.

Typing in a text/numeric field should be staged until a valid commit action (Enter, focus loss, explicit Apply according to control behavior) rather than producing invalid Python source on every keystroke.

Invalid input must remain local UI state and must not corrupt the notebook.

---

# 22. Control Blocks - product role

Control Blocks are first-class Graphimo elements for interactive experimentation.

They are not `@node` scientific functions and they are not MetaBlocks.

In normal Python/marimo scope, V1 controls should use marimo reactive UI primitives where an equivalent exists.

Baseline control set:

- Slider;
- Range Slider;
- Number input;
- Select/Dropdown;
- Checkbox/Switch;
- Text input;
- Button/trigger where useful.

Controls produce a normal Python value usable by downstream blocks.

---

# 23. Slider semantics

Conceptual generated source:

```python
# Graphimo control: slider
# Graphimo instance: a81f2c40
g_slider_a81f2c40 = mo.ui.slider(
    start=0.0,
    stop=1.0,
    step=0.01,
    value=0.2,
    debounce=False,
    label="threshold",
)
```

Downstream Graphimo connection:

```python
g_thresholded_82af9d10 = threshold(
    x=g_data_99a1c003,
    threshold=g_slider_a81f2c40.value,
)
```

The user sees a `float` output/value connection, not `.value` implementation syntax.

Slider card body renders the real interactive slider.

---

# 24. Live versus commit/debounced control policy

Rapid controls must expose an execution policy.

At minimum:

- **Live** - update during drag where the marimo control supports it;
- **On release** - commit after interaction finishes/debounces.

The chosen policy must be visible/configurable.

The default should avoid pathological recomputation for expensive workflows.

Graphimo must never silently restart a multi-hour training path dozens of times because the user moved a slider by one pixel if that control is configured for commit behavior.

With marimo lazy execution, a control change may make downstream blocks stale rather than run them. The canvas must show that state.

---

# 25. Other controls

Range Slider:

- two handles;
- output is a pair/range value;
- clear min/max labels.

Number:

- exact numeric entry;
- optional step buttons;
- enforce valid finite numeric source before commit.

Select:

- show current option;
- search if option list is long;
- preserve exact underlying scalar type where supported.

Checkbox/Switch:

- boolean output;
- immediate update unless configured otherwise.

Text:

- staged editing by default;
- avoid rerunning descendants on every character for expensive graphs unless explicitly configured live.

Button:

- event/trigger semantics must use marimo's supported reactive primitive;
- button is not a hidden scheduler.

---

# 26. Controls and MetaBlocks

Ordinary Graphimo controls may connect to externally exposed MetaBlock parameters.

The addon decides what the parameter means.

Examples:

```text
Number n_heads  ---> PyTorch Model MetaBlock
Slider dropout  ---> PyTorch Model MetaBlock
Slider lr       ---> Train Group Block
```

A structural model parameter may cause the PyTorch MetaBlock to compile a new model definition.

A runtime training parameter may affect only the external training block.

The Graphimo Core renderer does not encode PyTorch-specific parameter categories.

---

# 27. Reactive feedback loop

The graphical development experience must make reactive consequences visible.

Example:

```text
Slider threshold
      |
      v
Threshold node
      |
      v
Image / metric
```

When the slider changes:

1. UI value changes;
2. marimo/reactive source value changes;
3. dependent nodes become running/stale according to execution mode;
4. status is immediately reflected on cards;
5. updated output preview/rich output is shown when available.

Do not require the user to open the marimo editor to observe this cycle.

---

# 28. Execution states

The canvas must represent at least:

- **never run / idle**;
- **running**;
- **up-to-date/completed**;
- **stale**;
- **error**;
- **disabled**;
- **missing addon/unavailable**;
- **waiting/manual/lazy** where reported by execution semantics.

State must not be indicated only by color.

Use text/icon/badge/shape/animation combinations appropriate to the renderer.

Status changes must not cause significant card geometry jumps.

---

# 29. Running state and progress

A running node should visibly communicate activity.

Where available, show:

- spinner/progress indicator;
- progress fraction;
- current step/epoch text;
- latest bounded metric;
- elapsed time if available cheaply;
- stop/cancel affordance only when supported by the execution path.

The canvas should remain usable while one node is running.

Do not block pan/zoom/inspection because a cell is executing.

---

# 30. Stale/lazy state

Graphimo must embrace marimo stale semantics rather than hide them.

When an upstream value changes but a downstream expensive node has not yet rerun:

- downstream card shows `STALE` or equivalent;
- last successful result may remain visible but is marked old/stale;
- the user can understand that displayed data does not correspond to the latest upstream state;
- running the target should recompute necessary stale ancestors according to marimo semantics.

Never display stale output as if it were current.

---

# 31. Error experience

Ordinary errors must be understandable directly from the graph.

Required:

- error state on affected card;
- short exception/message summary;
- expandable details;
- source context/open-source action;
- traceback available without forcing the user to find a terminal log;
- downstream stale/blocked state represented separately from the original error;
- retry/run action where semantically valid.

Do not show raw multi-page traceback text inside every card.

The inspector/detail surface is the normal location for full diagnostics.

---

# 32. Diagnostics categories

Different failures should be visually distinguishable where possible:

- Python runtime exception;
- invalid Graphimo-generated source contract;
- missing required input;
- invalid connection;
- node output arity mismatch;
- declared/runtime type mismatch warning;
- missing addon;
- addon compilation error;
- ArtifactStore failure;
- marimo session failure;
- unsupported imported construct.

The UI should show structured diagnostic code + readable message when provided by the functional layer.

---

# 33. Compact output previews

Every executable block should have a useful bounded result summary without opening source.

Examples:

Scalar:

```text
float (0.917 | accuracy)
```

Tensor:

```text
Tensor
32 x 3 x 224 x 224
float32 / cuda:0
```

DataFrame:

```text
DataFrame
150000 rows x 23 cols
```

Image:

- thumbnail where safe/available;
- dimensions/type.

Plot:

- compact render/thumbnail if marimo output supports it.

Text:

- bounded snippet.

Large values must never be fully serialized merely to draw the card.

---

# 34. Expanded output inspector

Selecting/opening an output exposes a richer detail surface.

Possible detail modes based on supported output:

- full scalar/text within safe bounds;
- table sample with scrolling/paging;
- image at useful size;
- chart/plot;
- tensor metadata;
- ArtifactRef metadata;
- raw structured preview for unknown safe objects.

The inspector asks the functional layer for richer data. It must not reach directly into the Python namespace from renderer code.

---

# 35. Tables and DataFrames

For tabular output, compact card view may show row/column counts and a tiny sample indicator.

Expanded inspector should support:

- column names;
- bounded rows;
- horizontal/vertical scrolling;
- basic sort/filter only if backed by a safe data-view contract;
- no automatic materialization of millions of rows into renderer memory.

Graphimo V1 does not need to become a full spreadsheet.

---

# 36. Images

Image output should support:

- thumbnail in card or output chip when reasonable;
- aspect-ratio preserving expanded view;
- metadata such as dimensions/mode where known;
- zoom/pan in expanded inspector for large images as a later enhancement;
- no encoding of huge images repeatedly if a stable artifact/reference can be reused.

For image-processing workflows, the slider -> process -> image feedback loop is a key manual acceptance case.

---

# 37. Charts and plots

If marimo already produces a rich plot, Graphimo should preserve/use the supported representation rather than reducing it to a raw `repr`.

Compact card:

- small preview or latest scalar metrics.

Expanded inspector:

- interactive chart where host permits;
- readable axes/legend;
- no hidden rerun merely because inspector opened.

Graphimo-specific live metric charts are described later for addons/training.

---

# 38. Artifact output UX

When a node returns or references an `ArtifactRef`, display:

- kind;
- short ID;
- logical size if known;
- persisted/pinned state where relevant;
- materialize/open actions appropriate to the host;
- lineage/producer link only if available without inventing a second DAG.

Artifact storage internals are not exposed as raw CAS paths by default.

An artifact can be inspected without loading its entire payload into memory.

---

# 39. Source access

The user must always be able to inspect/edit real Python definitions where appropriate.

For a normal `@node`:

- `Open source` navigates to the function definition in the available editor;
- editing the function body changes all future instances because instances are function calls;
- Graphimo refreshes the node contract after source changes;
- instance layout is not lost merely because the function source changed.

Generated instance wiring source is normally not the primary editor surface, but may be inspectable for debugging.

Do not hide the fact that Graphimo is ordinary Python/marimo underneath.

---

# 40. Code pane / generated code for MetaBlocks

A MetaBlock addon may expose readable generated source.

For PyTorch V1:

- visual graph -> generated PyTorch is reliable direction;
- code pane shows generated source corresponding to current graph revision;
- normal editing of generated source does not silently mutate the graph unless round-trip support is explicitly implemented;
- code may be copied/exported;
- unsupported reverse edits must be clearly read-only or diagnostic rather than pretending to round-trip.

The code pane is part of inspection/development experience, not a second authoritative runtime.

---

# 41. Group Block visual semantics

A Group Block is a reusable workflow without changing semantic dialect.

Outer card:

- group name;
- external input/output ports;
- compact state summary;
- open/enter affordance;
- optional count/summary of contained elements.

Inside:

- same dialect palette as parent;
- normal blocks and nested containers;
- independent layout/camera state;
- breadcrumb shows parent -> group.

Creating a Group Block from selection must preserve connectivity by converting crossing edges into group external ports according to the functional Group Block contract.

Exact Group execution backend follows the main specification.

---

# 42. Entering/leaving Group Blocks

Required navigation:

- double-click/open action enters group;
- breadcrumb updates;
- Back action leaves to parent;
- viewport restores previous parent position when possible;
- selected internal element does not remain selected after leaving unless selection model explicitly supports path-qualified IDs;
- palette remains same dialect;
- search operates inside current scope.

Nested groups are recursive.

---

# 43. MetaBlock visual semantics

A MetaBlock is a nested Graphimo scope with a different semantic dialect supplied by an addon.

Outer card:

- MetaBlock name/type;
- addon/dialect identity where useful;
- external ports;
- status;
- summary of compiled/materialized state;
- open/enter affordance.

Inside:

- same Graphimo canvas interaction language;
- addon-specific element catalog;
- addon-specific connection/shape rules;
- Group Blocks allowed;
- nested MetaBlocks allowed where supported;
- specialized inspectors allowed through generic host seams.

A MetaBlock is not an iframe or unrelated mini-application.

---

# 44. Scope breadcrumbs

Breadcrumbs are mandatory once nesting exists.

Example:

```text
Project > Train Group > Llama Model > DecoderLayer x32
```

Required:

- each segment navigable where safe;
- current scope visually distinct;
- very long paths collapse gracefully;
- scope identity survives renames via stable IDs;
- breadcrumb navigation is semantic, not just changing visible panels.

The user must always know which scope/dialect is active.

---

# 45. Scope-specific palette behavior

Normal scope palette and MetaBlock palette must be obviously different enough that the user understands the context switch.

When entering PyTorch:

```text
Normal Nodes / Controls / MetaBlock entry
```

are replaced/filtered by PyTorch architecture items according to addon policy.

When leaving:

- PyTorch-only internal items disappear;
- normal nodes return;
- stale palette search state may be cleared to avoid confusing hidden matches.

A Group Block does **not** trigger a dialect change.

---

# 46. Missing addon UX

A project with a MetaBlock whose addon is unavailable must still open.

Outer unavailable card shows:

- original MetaBlock title if stored;
- missing addon identifier;
- preserved external port/state summary when serialized metadata supports it;
- `MISSING ADDON` state;
- no destructive migration.

Entering it may show a read-only placeholder/detail explaining that the addon is required.

Do not delete unknown addon state.

Reinstalling the addon should restore full rendering/behavior from preserved serialized state.

---

# 47. PyTorch MetaBlock palette - UX contract

The dedicated PyTorch plugin spec defines the exact V1 catalog. The GUI must support categories such as:

- Inputs / Outputs;
- Layers;
- Normalization / Activations;
- Tensor transforms;
- Math / Attention operations;
- Losses / Heads;
- Groups / Templates;
- compatible nested MetaBlocks/extensions.

The palette is supplied by `graphimo-pytorch`; the renderer does not hard-code it.

The palette must support both primitives and high-level architecture templates.

---

# 48. PyTorch architecture card semantics

Internal PyTorch elements may show domain-relevant compact information.

Examples:

```text
Linear
4096 -> 4096
bias=False
```

```text
RMSNorm
4096
eps=1e-5
```

```text
Softmax
dim=-1
```

```text
SplitHeads
(e,t,4096) -> (e,32,t,128)
```

When runtime information is available, add compact output shape/stat badges without destroying readability.

The addon controls domain-specific content; Graphimo provides generic card/selection/inspector hosting.

---

# 49. PyTorch shape/type propagation display

Connections inside a PyTorch MetaBlock should show shape/type information when the addon can infer it.

Examples from the Llama-style canvas:

```text
(e, t, 4096)
(e, 32, t, 128)
(e, 32, t, t)
```

Required UX:

- shape available near ports/edges or in inspector;
- incompatible shape connection produces immediate addon diagnostic before/at compile;
- symbolic dimensions remain symbolic rather than invented concrete numbers;
- inferred shape is marked as inferred, runtime shape may be shown separately;
- a runtime discrepancy is visible as diagnostic rather than silently replacing the model contract.

Graphimo Core does not implement tensor shape algebra.

---

# 50. Repeated groups such as `LlamaDecoderLayer x 32`

The PyTorch addon must be able to represent repeated structures compactly.

Collapsed representation:

```text
LlamaDecoderLayer x 32
4096 -> 4096
heads 32 - MLP width 11008
[Open]
```

Opening reveals the logical template/group structure.

For live inspection, the UI must allow selecting a runtime repetition/instance when data differs by layer index.

Possible V1 interaction:

- repeated group inspector contains `Instance: 0..31` selector;
- internal node selection + repetition selector resolves to `layers.N.<module>`;
- compare multiple repetitions may be deferred.

Do not duplicate 32 complete visual copies in the parent scope solely to make runtime mapping easier.

---

# 51. PyTorch parameter editing

Layer parameters should be editable through the card/inspector according to addon schema.

Examples:

- Linear in/out features when structurally editable;
- bias;
- normalization eps;
- number of heads;
- hidden/MLP widths;
- activation options.

Editing a structural parameter:

1. updates addon graph state;
2. re-runs shape validation/propagation;
3. marks compiled model definition stale;
4. updates generated source;
5. does **not** mutate a currently training model in the middle of backward.

Whether weights can migrate between revisions is a later addon feature.

---

# 52. PyTorch external model card/port

The outer PyTorch MetaBlock appears as one Graphimo element with external model/input/output contract defined by the addon specification.

The exact external Python value is fixed in the PyTorch implementation contract, not by the renderer.

UI should expose:

- model/meta title;
- graph revision/materialization state;
- external ports;
- compile errors;
- enter/open button;
- latest training/runtime association when available.

The outer card must not show every internal layer.

---

# 53. External training relationship

Training/inference may occur in a normal Graphimo block or Group Block outside the model MetaBlock.

The UX should make the relationship understandable without implying the MetaBlock owns the training scheduler.

Example:

```text
Dataset ---> Train Group <--- PyTorch Model MetaBlock
                |
                +--> loss/history/checkpoint
```

While training:

- Train block shows run/progress/loss summary;
- PyTorch MetaBlock may show `LIVE`/attached-run badge;
- entering the model reveals live information on selected internal layers;
- stopping/ending training removes active-run state but keeps latest bounded observations according to retention policy.

---

# 54. Live telemetry chart experience

For training metrics such as loss, Graphimo needs a reusable chart presentation.

Required minimum:

- line chart over step/time;
- incremental append without redrawing/retransmitting entire history every event;
- bounded history/downsampling strategy for very long runs;
- current value;
- optional multiple named series;
- run identity;
- stale/completed state.

The chart renderer is generic; metric names/meaning remain addon or node-defined.

A long training run must not cause unbounded UI memory growth.

---

# 55. Layer live-output inspection

When the user selects an inspectable PyTorch element during an active run, the addon may subscribe a lightweight hook.

Compact information can include:

- runtime shape;
- dtype;
- device;
- mean/std;
- min/max;
- NaN/Inf flags;
- gradient norm where requested.

Default behavior must not stream entire tensors.

Selection should start/stop subscriptions cleanly to avoid accumulating hooks.

The user should understand whether displayed information is live, last observed, or from a saved trace.

---

# 56. Tensor deeper inspection

An explicit deeper-inspection action may request:

- histogram;
- bounded sample;
- selected channels/features;
- image projection for image-like tensors;
- persisted Artifact for a full tensor if explicitly requested and safe;
- TorchLens trace.

Do not fetch a multi-gigabyte activation merely because the user clicked a block.

The inspector should present estimated/actual payload size before expensive capture where appropriate.

---

# 57. Gradient inspection

Gradient inspection is opt-in/subscribed.

UI can display:

- gradient norm;
- min/max/mean/std;
- missing/no-gradient state;
- NaN/Inf;
- histogram/deeper capture on request.

Gradients belong to a particular run/step/model revision. Do not display an old gradient as if it belonged to a newly edited architecture.

The addon provides the semantic mapping; renderer displays structured data.

---

# 58. TorchLens inspector UX

TorchLens is an on-demand deep-inspection capability of the PyTorch addon.

The user selects an internal model element and chooses a deep-inspection action.

The inspector may show:

- operation trace within/around selected module;
- saved activation details;
- gradients;
- NaN/Inf localization;
- intervention/ablation/patching tools as supported;
- causal inspection results;
- comparison between runs/checkpoints in future versions.

TorchLens views must clearly distinguish captured/snapshot data from live lightweight telemetry.

Do not run a full TorchLens trace continuously by default.

---

# 59. Inspector panel contract

The inspector is context-sensitive.

Selected normal node:

- definition/signature;
- input bindings;
- output previews;
- runtime/error state;
- source action.

Selected Control:

- control configuration;
- value;
- update policy.

Selected Group:

- external ports;
- metadata;
- open action.

Selected MetaBlock:

- addon/dialect;
- external contract;
- revision/compile state;
- open action.

Selected PyTorch internal element:

- parameters;
- inferred shapes;
- runtime binding;
- live stats;
- TorchLens/deep-inspection actions.

The renderer should not implement separate disconnected inspector systems for every kind; use a common detail host with provider-specific sections.

---

# 60. Context menus

Context menus provide discoverable secondary actions.

Normal block menu may include:

- Run;
- Open source;
- Inspect;
- Duplicate;
- Disable/Enable;
- Delete;
- Group selection;
- Copy/paste actions where supported.

Edge menu:

- Disconnect;
- Inspect connection.

Group/MetaBlock:

- Open;
- Rename where supported;
- Duplicate/reference semantics;
- Delete.

Actions are populated/enabled from application capabilities, not renderer assumptions.

---

# 61. Keyboard shortcuts

Production Graphimo should support efficient keyboard use.

Recommended minimum (host conflicts may adapt keys):

- Delete/Backspace - delete selection;
- Cmd/Ctrl+C - copy selection;
- Cmd/Ctrl+V - paste;
- Cmd/Ctrl+D - duplicate;
- Cmd/Ctrl+Z - undo;
- Cmd/Ctrl+Shift+Z / Ctrl+Y - redo;
- Enter/Open - enter/open selected container or inspector;
- Escape - cancel drag/connection/placement, then clear transient mode;
- F / host equivalent - fit selection/graph;
- search focus shortcut for component library;
- run shortcut where host conventions permit.

Keyboard commands must invoke the same semantic intents as pointer actions.

---

# 62. Undo/redo - required before daily-use readiness

Undo/redo is not limited to canvas pixel movement.

Semantic transactions include:

- insert block;
- delete block;
- move selection;
- reconnect/disconnect;
- literal edit;
- control edit;
- create Group Block;
- template insertion;
- structural MetaBlock edit;
- layout change.

An action that changes both source and layout should undo coherently as one user-visible transaction when practical.

Do not implement renderer-only history that leaves Python source modified after visual undo.

Initial milestones may defer undo implementation, but product V1 daily-use acceptance requires it.

---

# 63. Copy, paste and duplicate

Copy/paste should operate on semantic elements and internal connections between copied elements.

Required eventual behavior:

- duplicated normal block gets a new Graphimo instance ID/result variable;
- copied internal edges reconnect among duplicated elements;
- external incoming/outgoing edges are not silently duplicated unless the operation explicitly supports it;
- duplicated Group/MetaBlock follows its reference/copy semantics;
- pasted layout is offset from source.

Clipboard serialization must not become authoritative project storage.

---

# 64. Delete behavior

Deleting an element must be explicit and predictable.

Normal block deletion:

- remove Graphimo-owned instance cell/source according to application layer;
- remove edges referencing it;
- preserve reusable `@node` definition;
- one undo transaction.

Deleting a Group/MetaBlock:

- warn when it contains meaningful subgraph/state if not recoverable through undo;
- do not delete addon packages/templates themselves.

Do not cascade-delete unrelated upstream/downstream nodes.

---

# 65. Disable/enable visual semantics

Disabled normal executable instance:

- remains visible;
- clearly marked `DISABLED`;
- execution output marked unavailable/stale;
- source is commented as specified by main contract;
- downstream broken/stale connections remain visible rather than silently rewired.

Enable restores canonical source and allows marimo dependency reconstruction.

Disable/enable must be undoable.

Exact downstream execution UX follows the remaining human decision in the main contract.

---

# 66. Running long tasks without freezing UI

The Workbench/Zeron renderer must remain responsive while long marimo/PyTorch work runs.

Required:

- pan/zoom/select continue to work;
- incoming telemetry updates are throttled/batched;
- chart redraw does not block execution transport;
- large payloads are bounded;
- inspector requests do not stall the entire canvas when possible;
- transient network/bridge delay is represented as loading rather than UI freeze.

Do not tie renderer frame rate to Python training step frequency.

---

# 67. Performance budgets - practical V1 targets

These are engineering targets, not strict microbenchmarks, and should be measured in the Workbench.

For an ordinary project with approximately 100 visible blocks:

- pan/zoom should feel interactive;
- moving one block should not trigger Python source parsing for all blocks every pointer frame;
- palette search response should be near-immediate;
- telemetry updates should be coalesced to a sensible UI frequency;
- opening an inspector should not serialize unrelated outputs.

For larger graphs, virtualize/offscreen-cull expensive card content if needed while preserving correct world geometry.

Do not optimize by dropping semantic state.

---

# 68. Rendering virtualization/culling

When graphs become large:

- offscreen heavy card bodies may be simplified/not rendered;
- ports/edges needed for visible connections remain correct;
- selection of offscreen elements through navigator/search remains possible;
- zoomed-out mode may show simplified cards;
- runtime values need not rerender full rich previews at tiny zoom levels.

Culling is renderer optimization only. It must not mutate graph state or execution.

---

# 69. Zoom-level information density

Graphimo should progressively simplify visual detail when zoomed far out.

Example:

High zoom:

- parameters;
- port labels;
- runtime previews;
- controls.

Medium:

- title;
- ports;
- essential shape/status.

Low:

- block silhouette/title/status;
- edges;
- groups.

Do not make ports impossible to find at normal working zoom.

Exact thresholds are renderer-specific.

---

# 70. Minimap / navigator

A minimap is optional for the earliest Workbench but recommended for production large-graph use.

If implemented:

- shows active scope only;
- viewport rectangle;
- click/drag navigates camera;
- groups/MetaBlocks distinguishable;
- no semantic editing in minimap.

A search/navigator can complement or replace it in V1 if host constraints make minimap expensive.

---

# 71. Visual grouping versus semantic Group Block

Do not confuse:

- **visual frame/annotation grouping** - presentation-only organization;
- **Group Block** - reusable semantic workflow/container.

If Graphimo later offers freeform visual frames like Excalidraw, they must not silently change Python execution or import semantics.

Creating a semantic Group Block must be an explicit action.

---

# 72. Notes and annotations

Graphical notes are useful for research workflows.

They may contain:

- text/markdown;
- links;
- lightweight labels;
- visual frames.

Annotations are presentation/project metadata unless explicitly connected to execution through a dedicated block type.

They must not be mistaken for Python comments automatically unless such conversion is explicitly implemented.

This feature can follow core V1 but should fit the same canvas model.

---

# 73. Project persistence for graphical state

Presentation state includes:

- x/y positions;
- card size where resizable;
- collapse state;
- scope camera positions;
- optional panel/view preferences;
- visual annotations;
- selected layout mode.

This state belongs in the Graphimo sidecar/project metadata, not generated Python source, unless a particular semantic element explicitly requires source representation.

Scientific semantics must not exist only in ephemeral renderer memory.

---

# 74. Layout restoration

On reopen:

- stable block IDs restore positions;
- nested scope positions restore independently;
- missing blocks/layout entries are ignored safely;
- new blocks receive reasonable default placement;
- stale sidecar metadata must not corrupt source;
- unavailable addon MetaBlocks retain outer placement.

Layout restore must not require running the notebook.

---

# 75. Rename semantics

Display names and Python identifiers are different concepts.

Normal node definition name follows Python function name.

Instance may optionally have a user-facing label/alias stored as presentation metadata without renaming the generated `g_<function>_<id>` symbol.

Group/MetaBlock user-facing names may be editable according to their persistence contract.

Renaming a label should not unnecessarily rewrite executable source.

---

# 76. Themes

Graphimo should integrate with host dark/light theme.

Required:

- adequate contrast;
- status not color-only;
- edges/ports visible in both themes;
- charts readable in both themes;
- addon cards inherit theme tokens or generic style contract rather than hard-coded colors that disappear in one mode.

Workbench may initially use simple theme defaults but should support both before production parity.

---

# 77. Accessibility

Minimum production accessibility goals:

- keyboard navigable primary actions;
- visible focus indication;
- status not communicated only by color;
- readable contrast;
- text alternatives/tooltips for icon-only actions;
- reasonable hit target sizes;
- controls expose labels/value;
- zoom does not make essential text permanently inaccessible;
- error messages readable without relying on animation.

Zeron-native accessibility APIs should be used where available.

---

# 78. Tooltips and affordances

Ports/components/actions that are not self-explanatory should provide concise hover/focus explanations.

Examples:

- input type/parameter description;
- output type/expression;
- missing-addon reason;
- disabled action reason;
- structural PyTorch parameter warning;
- stale output explanation.

Tooltips must not be the only place critical error information exists.

---

# 79. Confirmation policy

Avoid modal confirmation for routine reversible actions.

Prefer undo for:

- moving;
- reconnecting;
- duplicating;
- deleting ordinary small selections when undo is reliable.

Use confirmation for destructive/non-reversible actions such as:

- clearing persisted artifacts;
- deleting large nested state if undo unavailable;
- replacing source with a vendor/import operation that cannot be trivially restored.

Do not interrupt rapid graph editing with unnecessary dialogs.

---

# 80. Notifications

Use non-blocking notifications for:

- save success where useful;
- addon discovery/reload;
- background operation result;
- recoverable warnings.

Errors tied to a block should primarily appear on/near that block and inspector, not only as a global toast.

Do not spam notifications for every normal reactive recomputation.

---

# 81. Workbench role - mandatory development host

The Graphimo Workbench is a **development/test renderer**, not a separate product architecture.

Its purpose is to let the user test new Graphimo semantics before Zeron integration.

It must use:

- the same Graphimo application layer;
- the same project/source mutation code;
- the same marimo integration;
- the same addon discovery;
- the same palette resolution;
- the same output/telemetry structures.

It may use simpler visuals.

Do not put behavior in Workbench code that cannot later be reused by Zeron.

---

# 82. Workbench minimum visible shell

The first useful Workbench should have:

- left contextual component library;
- central canvas;
- right inspector/details panel;
- top breadcrumb/project/run area;
- collapsible diagnostics/log area.

Exact layout can adapt, but all core interactions must be directly testable.

The Workbench should start against a selected/local Graphimo project and active marimo session without requiring the user to manually compose internal transport URLs.

---

# 83. Workbench incremental milestones

Workbench UX should grow alongside functional milestones.

### W1 - Nodes

- open project;
- see node definitions;
- add instances;
- move blocks;
- connect/reconnect;
- run/inspect output.

### W2 - Controls

- add slider/select/etc.;
- connect control;
- reactive update;
- stale/running display.

### W3 - Persistence/groups

- save layout;
- disable/enable;
- Group Block navigation when functional backend exists.

### W4 - Addons/MetaBlocks

- discover addon;
- create MetaBlock;
- enter/leave scope;
- contextual palette.

### W5 - PyTorch

- architecture editing;
- shape info;
- generated source;
- external run;
- live stats;
- TorchLens.

Zeron integration should not block these functional/UX validations.

---

# 84. Zeron adapter role

Zeron is the production host/renderer once Graphimo semantics are stable enough.

Zeron adapter responsibilities:

- map Graphimo view state to Zeron/ZGX/Design components;
- map pointer/keyboard actions to Graphimo intents;
- use host-native theme/panels/dialogs;
- handle plugin permissions/lifecycle;
- subscribe to application events;
- preserve the same project semantics as Workbench.

Zeron must not become a second source parser or execution controller.

---

# 85. Workbench/Zeron semantic parity

A project manipulated in Workbench must open in Zeron with the same:

- nodes;
- connections;
- Group/MetaBlock structure;
- layout where renderer-compatible;
- control values/policies;
- addon state;
- source;
- artifact references.

Host-specific panel placement may differ.

A semantic action performed in either host must produce the same underlying project mutation.

---

# 86. Renderer-independent testing

Automated tests should primarily target the application layer.

Examples:

- `add_palette_item` creates correct source/state;
- `connect` mutates one canonical binding;
- scope entry returns correct palette;
- slider value change reaches marimo value semantics;
- missing addon returns placeholder view state;
- PyTorch internal element creation is provided by addon;
- telemetry maps to selected node view data.

Renderer tests then validate that UI gestures dispatch the correct intents and render resulting state.

Do not rely only on screenshot tests to validate semantics.

---

# 87. Renderer snapshot/visual tests

Useful visual tests include:

- normal block card states;
- multi-output ports;
- error card;
- stale card;
- disabled card;
- missing-addon MetaBlock;
- contextual palette in normal vs PyTorch scope;
- slider control;
- nested breadcrumbs;
- PyTorch layer card with shape/runtime stats;
- dark/light theme.

Visual tests are regression aids, not a substitute for semantic tests.

---

# 88. Manual UX acceptance scenario - normal Graphimo

A human tester must be able to perform this without editing generated wiring source:

1. open project;
2. find `scale` node in palette;
3. create two instances;
4. connect both to source;
5. set different literal factors;
6. run/observe distinct results;
7. reconnect one instance;
8. verify other is unchanged;
9. add slider;
10. connect slider to factor of one instance;
11. move slider;
12. observe live/stale consequence;
13. inspect output;
14. disable and re-enable an instance;
15. save/reopen and confirm layout/state.

If ordinary graph operations require opening generated Python, the UX milestone is not complete.

---

# 89. Manual UX acceptance scenario - nesting

When Group Blocks are implemented:

1. select multiple blocks;
2. create Group Block;
3. inspect generated external ports;
4. open group;
5. edit internal connection;
6. leave via breadcrumb;
7. instantiate/reuse group according to functional contract;
8. reopen project and retain nested layout.

Group entry must not change dialect/palette except for project visibility differences.

---

# 90. Manual UX acceptance scenario - PyTorch MetaBlock

When `graphimo-pytorch` is installed:

1. normal palette shows `PyTorch Model` MetaBlock entry but not internal `Linear/RMSNorm/...` items;
2. add PyTorch MetaBlock;
3. enter it;
4. palette switches to PyTorch catalog;
5. create `Input -> Linear -> ReLU -> Linear -> Output`;
6. see inferred shapes;
7. edit a layer parameter and see shapes/generated source update;
8. create/open a nested Group Block;
9. leave MetaBlock and confirm PyTorch internals disappear from normal palette;
10. connect model to external train/inference block;
11. run;
12. observe loss/progress externally;
13. enter model while run is active;
14. select a layer and see live bounded output statistics;
15. request gradient information;
16. trigger on-demand TorchLens inspection;
17. end run and preserve latest state clearly as non-live;
18. save/reopen project.

This scenario is a product acceptance requirement for the first serious PyTorch addon iteration.

---

# 91. Manual UX acceptance scenario - Llama-style architecture

After the tiny model passes, use a Llama-style fixture similar to the provided Torch Canvas reference.

Required visual capabilities:

- embedding;
- repeated `LlamaDecoderLayer x N` group;
- open repeated group/template;
- RMSNorm;
- q/k/v Linear branches;
- split heads;
- RoPE;
- matrix multiply;
- causal mask;
- softmax;
- residual path;
- gated MLP path;
- merge heads;
- final norm/head;
- loss connection;
- readable tensor-shape annotations.

The goal is not pixel-identical Torch Canvas styling. The goal is equivalent structural readability and inspectability inside Graphimo.

---

# 92. State persistence acceptance

After close/reopen, preserve where applicable:

- block positions;
- nested scope layouts;
- Group/MetaBlock identities;
- control configuration/value according to source/marimo semantics;
- selection is not required to persist;
- camera state may persist;
- addon state;
- missing-addon opaque state;
- generated source/project semantics.

Reopen must not require executing all expensive nodes just to reconstruct the canvas.

---

# 93. Crash/recovery UX

If marimo/runtime disconnects:

- canvas/project remains visible where cached project state permits;
- show disconnected/recovering state;
- disable actions that require runtime;
- offer reconnect/restart action;
- do not discard unsaved layout/source edits silently.

If addon compilation crashes:

- contain error to affected MetaBlock;
- preserve graph state;
- allow user to edit/fix structure.

---

# 94. Addon install/uninstall UX

When a new MetaBlock addon becomes available:

- palette refreshes;
- no application restart should be required if runtime supports safe discovery refresh; otherwise show explicit restart requirement;
- addon version visible in diagnostics/inspector where useful.

When removed:

- existing MetaBlocks become unavailable placeholders;
- project opens;
- state preserved;
- creatable palette entry removed.

Do not silently migrate/delete unknown addon state.

---

# 95. Version mismatch UX

If a project MetaBlock was saved with an incompatible addon/API version:

- show structured `VERSION MISMATCH` state;
- preserve serialized state;
- do not attempt destructive automatic conversion unless addon explicitly provides migration;
- show required/current versions;
- allow export/backup of state.

Core remains usable.

---

# 96. Source conflict UX

If user edits Graphimo-owned generated instance source into a shape no longer recognized:

- do not silently overwrite it;
- mark element as source-conflicted/unmanaged;
- explain why graphical editing is unavailable;
- offer open source;
- where safe, offer explicit `Regenerate from Graphimo state` only with confirmation because it is destructive.

Normal user function body edits are expected and are not source conflicts.

---

# 97. Unknown/manual Python cells

Graphimo may display ordinary marimo cells that are not Graphimo-managed depending on project mode.

They must be visually distinct from `@node` instances.

Do not pretend Graphimo can safely reconnect arbitrary Python code using the canonical instance editor.

Possible treatment:

- generic read-only/raw cell card;
- open in marimo editor;
- show refs/defs if available;
- no Graphimo-owned parameter editor unless source conforms to a supported contract.

This preserves interoperability without weakening source safety.

---

# 98. Component discovery refresh

When node/component source files change:

- refresh palette metadata;
- preserve existing instance identity;
- show changed/stale contract if signature/output ports changed;
- do not auto-delete invalid connections;
- surface a clear diagnostic and let user repair wiring.

Palette discovery should not execute arbitrary node bodies.

---

# 99. Signature change UX

If a node definition changes from:

```python
def train(x, lr)
```

to:

```python
def train(x, lr, epochs)
```

existing instances should show unresolved new required input rather than disappearing.

If an input is removed, stale generated argument/source needs explicit migration according to source contract.

Graphimo should explain the mismatch in the card/inspector.

Do not silently guess values for new scientific parameters.

---

# 100. Output contract change UX

If a function's return arity/type contract changes:

- affected output ports update after source inspection;
- existing downstream edges that reference removed outputs become broken/diagnostic;
- do not silently remap `output 1` to another expression;
- user can inspect old vs new contract if needed through diagnostic details.

This is especially important because output identity is positional for wiring.

---

# 101. Search/navigation in large projects

Production Graphimo should provide a way to locate blocks by:

- title/node name;
- user label;
- type/kind;
- error/running state;
- possibly artifact/run association later.

Selecting a search result focuses/zooms the corresponding scope/block, entering nested scopes as needed.

Do not require manual panning through a large research graph.

---

# 102. Run visibility and history boundary

Graphimo V1 needs current run state and latest bounded metrics.

A full experiment-history product is not required by this UX contract unless specified elsewhere.

If run history is exposed:

- distinguish current vs previous run;
- never show old activation/loss as live;
- associate metrics with run/model revision;
- large history should be stored/persisted through appropriate backend, not renderer memory.

---

# 103. Data privacy / large-value safety in UI

Do not automatically render arbitrary huge/sensitive values.

Preview functions are bounded.

For files/images/tables/tensors:

- show metadata/sample;
- explicit action to inspect deeper;
- avoid logging full payloads;
- ArtifactStore paths/internal hashes need not be exposed unless requested.

The renderer must treat addon/runtime payloads as untrusted structured data and bound sizes.

---

# 104. Interaction conflict rules

The UI must prevent common accidental gestures:

- dragging a slider does not drag its card;
- selecting text in a field does not pan canvas;
- dragging from a port starts connection, not card move;
- dragging card header/body region moves card;
- clicking an output preview opens inspector rather than starting connection unless user targets port anchor;
- double-click on Group/MetaBlock opens it; double-click on editable text should edit text instead where relevant.

Hit regions need automated/manual tests because these conflicts are common in canvas applications.

---

# 105. Pointer feedback during drag

During block drag:

- selected blocks move smoothly;
- edges update visually;
- final layout mutation may be committed at pointer release to avoid excessive persistence writes;
- snap/grid is optional and must be user-friendly if implemented.

During connection drag:

- preview edge follows pointer;
- compatible targets highlight;
- escape cancels;
- drop on empty canvas cancels or optionally opens quick-insert later; V1 may simply cancel.

---

# 106. Quick insert on edge - deferred enhancement

A future high-value interaction is dropping an edge on empty canvas to open a filtered palette of compatible components, then inserting/reconnecting automatically.

This is not required for first Workbench but should fit the application intent model without changing source semantics.

Do not implement it before ordinary palette insertion/connect is stable.

---

# 107. Inline parameter controls vs inspector

Simple frequent parameters may appear directly on card.

Complex/rare parameters belong in inspector.

Guideline:

- bool/select/small numeric value: inline where space allows;
- long text/list/object/config: inspector;
- addon structural settings: inspector or specialized compact editor;
- avoid cards becoming full forms.

The application exposes the same editable parameter model regardless of where renderer places it.

---

# 108. Card sizing

Cards should have stable sensible default widths.

Height adapts to:

- port count;
- inline controls;
- compact output summary;
- error/progress rows.

Long labels wrap/ellipsis with tooltip/inspector access.

Users may resize specific display/inspector-oriented cards if supported; normal executable cards need not be freely resizable initially.

Card size changes are layout metadata only.

---

# 109. Port ordering

Normal Python input ports follow function signature order.

Normal output ports follow return tuple position.

Addon dialects define deterministic ordering.

Do not reorder ports alphabetically in renderer because it would break the mental correspondence with source/model structure.

---

# 110. Edge labels

Edge labels are optional by default to reduce clutter.

Useful information can appear on hover/selection:

- source expression/type;
- target parameter;
- PyTorch tensor shape;
- connection diagnostic.

Inside PyTorch MetaBlocks, shape labels may be persistently visible at useful zoom levels because they are central to architecture readability.

---

# 111. Visual style consistency across dialects

MetaBlock dialects may use domain-specific colors/badges/card content, but should preserve core Graphimo interaction cues:

- same selection outline semantics;
- same input/output anchor behavior;
- same group/open affordance;
- same execution/error state language where applicable;
- same breadcrumb navigation;
- same context menu conventions.

A PyTorch MetaBlock should feel like Graphimo with specialized components, not a foreign embedded application.

---

# 112. Addon-provided visual descriptors

The generic addon seam may provide structured visual descriptors for elements, for example:

```text
title
subtitle
category
icon/style hint
parameter rows
port descriptors
badges
inspector sections/actions
```

Do not freeze a tiny closed list of widget APIs that prevents future addon capabilities.

The renderer hosts addon-provided semantic view data using Graphimo canvas primitives/contracts; domain-specific interpretation stays in addon.

Exact stable addon UI API follows the dedicated addon implementation work.

---

# 113. Generic live-event routing into UI

The UI/application layer needs a domain-agnostic way to associate live observations with targets.

Conceptually an event includes:

```text
run_id
target element/scope id
kind
timestamp/step
bounded payload
```

The application/addon converts event payload into updated view state/inspector data.

Renderer does not parse raw PyTorch hooks or TorchLens objects.

A fast event stream must be throttled/coalesced before expensive rendering.

---

# 114. Telemetry retention in UI

Define three conceptual retention classes:

- **ephemeral live** - latest activation stats, hover data;
- **bounded series** - loss curves/metrics;
- **persisted artifact/trace** - explicitly saved deep inspection.

Renderer memory is not a persistent telemetry database.

After run end, latest values may remain visible but are marked completed/last-run.

---

# 115. Chart downsampling

Long metric series should not render millions of points.

The telemetry/application layer may maintain/downsample a bounded representation while preserving recent/high-level trend.

Possible strategies include reservoir/bucket/LTTB-like later; exact algorithm is implementation choice if visual fidelity is acceptable.

Do not block training on chart rendering.

---

# 116. Progress and latest metric on collapsed groups

A Group Block/MetaBlock outer card may summarize internal/associated execution:

- running descendant count;
- error badge;
- latest loss/progress for attached PyTorch training when provided by addon;
- stale state.

Summary must not imply the container executes independently if execution is actually in descendants/external block.

---

# 117. Error propagation into containers

If an internal block errors:

- parent Group/MetaBlock outer card receives an error indicator;
- entering the container focuses/highlights failing element where practical;
- original diagnostic remains attached to actual failing element;
- parent summary does not replace detailed error source.

Nested error propagation should remain understandable across breadcrumbs.

---

# 118. Scope navigation to diagnostics

Clicking a diagnostic associated with a nested element should:

1. enter required scope path;
2. focus/select element;
3. open inspector/details;
4. preserve breadcrumb path.

This is important once nested PyTorch architectures become deep.

---

# 119. Save semantics and dirty state

The UI should distinguish unsaved Graphimo presentation/project changes from runtime execution state.

If source edits are written immediately through marimo, layout sidecar may still be dirty until persisted.

Expose save/dirty indication according to actual persistence model.

Do not imply that saving layout saves an in-memory Python object/checkpoint.

---

# 120. Project close behavior

On close:

- flush/prompt for unsaved source/layout changes;
- do not silently terminate unrelated runtime processes;
- if a Graphimo-owned training/run is active, follow explicit stop/detach behavior defined by runtime integration;
- Workbench should release listeners/hooks cleanly.

Exact long-running background behavior may evolve; never silently lose edits.

---

# 121. Workbench implementation files - recommended structure

Codex should prefer a separation comparable to:

```text
graphimo/
  application/
    app.py
    view_state.py
    commands.py
    events.py
    palette.py
    scopes.py
  ... functional packages ...

workbench/
  ... renderer-specific code only ...
```

Exact technology/file extensions depend on chosen Workbench renderer, but Python/source mutation must stay out of renderer-specific files.

The main repo should be able to test `graphimo.application` without launching a GUI.

---

# 122. Zeron renderer files - expected responsibility

Existing/future Zeron files such as:

```text
plugin/init.luau
plugin/canvas.luau
plugin/component_library.luau
plugin/layout.luau
plugin/... renderer helpers ...
```

should:

- render application state;
- dispatch intents;
- manage host-specific panels/theme/input;
- not duplicate Python AST logic;
- not hard-code PyTorch catalogs;
- not own marimo execution semantics.

The Zeron bridge may transport commands/events but not reinterpret their scientific meaning.

---

# 123. UX automated test categories

Required test families should eventually include:

1. palette resolution by scope/dialect;
2. palette insertion intent;
3. block/edge view-state projection;
4. status projection;
5. output preview projection;
6. control value/policy mutation;
7. Group/MetaBlock navigation;
8. missing-addon placeholder;
9. layout persistence;
10. undo/redo transactions;
11. telemetry coalescing/view updates;
12. PyTorch layer descriptor projection;
13. Workbench gesture -> intent mapping;
14. Zeron action -> same intent mapping.

Tests should assert semantic state, not brittle pixel positions unless explicitly visual.

---

# 124. Manual regression checklist - every major milestone

After a meaningful Graphimo UI milestone, manually verify:

- pan/zoom;
- select/multi-select;
- move;
- insert component;
- connect/reconnect/disconnect;
- literal edit;
- run/stale/error state;
- output inspector;
- save/reopen layout;
- keyboard Escape cancellation;
- no accidental card drag while using controls;
- no source corruption after reconnect;
- no UI freeze during a modest long-running node.

For nested milestones add Group/MetaBlock navigation and contextual palette checks.

---

# 125. Visual regression checklist

Before shipping a renderer change, inspect at least:

- dark theme;
- light theme;
- 80%, 100%, 150% OS/UI scale where testable;
- long node names;
- 0, 1, many ports;
- large output preview text;
- error traceback summary;
- active slider;
- multiple selected blocks;
- dense edges;
- nested breadcrumb;
- missing addon card;
- PyTorch shape labels.

No clipped text, invisible ports, or overlapping interaction targets.

---

# 126. Production usability criteria

Graphimo is not ready for serious daily use until a user can build and iterate a nontrivial workflow without feeling that the canvas is merely a thin visualization layer.

Required qualitative outcomes:

- common operations require fewer steps than manual notebook rewiring;
- graph state is legible at a glance;
- interactive parameter experimentation feels immediate;
- errors lead directly to relevant block/source;
- nested workflows remain navigable;
- addon-specific work remains integrated into Graphimo interaction language;
- user rarely needs to inspect generated wiring code;
- source remains readable/exportable when they choose to inspect it.

---

# 127. Explicit UX non-goals for first implementation

Do not make the first Workbench wait for:

- perfect visual branding;
- collaborative multiplayer editing;
- mobile/touch-first UX;
- arbitrary HTML plugin canvases;
- complete IDE replacement;
- full spreadsheet editing;
- full experiment-tracking dashboard;
- perfect arbitrary Python visualization;
- infinite custom theme system;
- automatic AI graph design.

The first Workbench must be functionally complete enough to exercise the product semantics, not visually final.

---

# 128. Decisions that remain host/style choices

The following may differ between Workbench and Zeron without changing Graphimo semantics:

- exact color palette;
- corner radius;
- typography;
- panel docking implementation;
- bezier vs orthogonal edge style;
- exact keyboard shortcuts when host conflicts exist;
- icon set;
- minimap appearance;
- animation timing;
- exact compact-card visual density.

Do not encode these choices into functional source models.

---

# 129. Decisions that are NOT cosmetic and must remain invariant

Across all renderers:

- which components are visible in a scope;
- what a connection means;
- source mutation caused by graph editing;
- control values and execution policy;
- Group vs MetaBlock semantics;
- addon missing/version behavior;
- output type/expression/value semantics;
- execution states;
- nested navigation identity;
- telemetry association;
- artifact identity;
- undo transaction meaning.

These are application semantics.

---

# 130. Definition of Graphical UX V1 completion

The Graphical UX V1 is complete when all of the following are true:

- standalone Workbench can exercise the complete normal Graphimo node/control workflow;
- Group/MetaBlock navigation works once their functional contracts exist;
- palette is contextual and addon-driven;
- ordinary graph editing never requires manual generated wiring edits;
- interactive controls visibly drive reactive results;
- output previews and richer inspectors exist;
- running/stale/error/disabled/missing-addon states are legible;
- layout persists;
- Workbench remains responsive during long work;
- PyTorch addon can provide its full specialized architecture canvas without PyTorch code in Core;
- external PyTorch training can send live information back to internal layers;
- on-demand TorchLens inspection is reachable through the model UI;
- Zeron renderer can consume the same application semantics without duplicating core logic;
- manual acceptance scenarios in this contract pass.

A visually attractive canvas that lacks these semantics is incomplete.
A semantically correct engine with no usable graphical workflow is also incomplete.

---

# 131. Codex implementation posture for this document

This contract is normative in the same sense as `Graphimo_CODEX_IMPLEMENTATION.md`.

Codex must:

- implement specified interaction semantics;
- keep renderer and functional logic separated;
- add semantic tests before relying on manual UI testing;
- use the Workbench to make intermediate features human-testable;
- avoid inventing PyTorch-specific logic in Core;
- avoid shrinking addon capabilities merely to simplify renderer APIs;
- stop at explicit unresolved product decisions rather than silently choosing a different product behavior.

Tests verify conformance. They do not decide whether the architecture is valid.

