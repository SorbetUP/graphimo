# Graphimo v0.2 Delivery Contract

Sources: `docs/Graphimo_Cahier_des_charges_Codex.pdf` (v0.2, 18 Nov 2026) and user-provided `/Users/sorbet/.zeron/profiles/local/uploads/058eec05-Graphimo_Structuration_fonctionnelle_v0_5.pdf` (v0.5 draft, 14 pages, 18 Nov 2026). v0.5 is explicitly a draft, not a final coding mandate. The current contract adopts its stated direction while separating acted decisions, strong proposals, and open questions; it does not treat every proposal/roadmap item as authorization to implement. The user asked to continue with v0.5 in mind.

## v0.5 direction and decision status

- **Adopted direction:** Graphimo remains a thin visual/composition layer over Zeron and marimo; Python source remains ordinary and usable without Graphimo; marimo owns cells, DAG, reactivity, execution, outputs, stdout/stderr, and cache. Zeron-owned controls/renderers/devices/agent primitives should be reused where the host supports them.
- **Acted taxonomy in the draft:** Base Block is an executable marimo chunk with a clear input/output contract; Control Block reflects a real marimo control; Display Block is a sink; Annotation is markdown/documentation; Group Block references a subgraph of real executable blocks; Meta Block is a specialized container whose internals compile into one Python unit. Group/Meta terminology differs from earlier v0.2 naming and governs future design pending a final mandate.
- **Strong proposals to validate incrementally:** derive function-backed ports from the callable contract; input order follows signature; output order follows return structure; show typed expression before run and typed value plus expression after run; literal defaults can be edited when unconnected; a connected variable becomes the source of truth; disconnect restores the previous literal/default. For ambiguous code, refuse and offer code/manual mapping rather than guessing.
- **Output proposal:** tuple returns with statically aligned expressions may expose ordered ports; named structures only when established without executing user code; arbitrary/dynamic values default to one object output or a manual declaration.
- **Open questions preserved:** connected-input fallback display; callable class lifecycle; Group Block pin/detach; Meta Block storage/round-trip; webview per card versus shared surface; exact Run-from-here semantics. These require later decisions before implementation.
- **Draft roadmap, not blanket authorization:** taxonomy/Base Block contract first, then manual blocks, canvas actions, Zeron editor, Terminal, Control/Display reflection, Group Blocks, and optional PyTorch Meta Block. Implementation remains decomposed into reviewed leaves.

## Product behavior

- Graphimo remains a Zeron plugin. A notebook is an ordinary, independently usable marimo `.py` file; marimo owns the DAG, dependencies, execution, reactivity, outputs, and cache.
- The canvas is the primary editing surface for component instances, parameters, ports, and outputs. Components are ordinary Python callables discovered from the notebook project's `components/` tree. There is no generic top-level Python-cell component, proprietary workflow engine, scheduler, cache, or type system.
- Graphimo may conservatively edit notebook source through marimo's live session. Ambiguous edits fail without changing source. Agents and users work against the same notebook and live session.
- Graphimo persists only presentation metadata in `.graphimo.json`, scoped to notebook and stable cell ID. Code, edges, values, execution state, and cache remain in the `.py` notebook / marimo.
- New/Open should establish a usable local session without asking the user to provide URL, token, port, or session details. Errors must be recoverable, visible in an actionable notification/panel, and diagnosable in logs. Secrets must not be logged.
- UI must use Zeron navigation, theme, design system, and canvas patterns. Components and Terminal panels can close independently. Avoid a permanent status bar, generic Python cell, prominent body logo, or manual HTTP connection form.
- Render real marimo output according to supported MIME types (including text/error, image, table, and chart where the host permits). Never imply a truncated `repr` is a rich output.

## Observable interfaces and invariants

- Plugin entry/manifest: `plugin/init.luau`, `plugin/zeron-plugin.json`; Zeron owns page rendering, commands, permission prompts, and theme.
- Production marimo transport boundary: `plugin/marimo_bridge.luau`; marimo remains the execution authority. The Python adapter is a contract harness, not a production runtime dependency.
- Sidecar interface: `Layout.load(zeron, notebook_path, done)` returns a cell-ID-to-layout map; `Layout.save(zeron, notebook_path, cells, done)` writes only whitelisted layout fields beside the notebook. Notebook keys must be JSON strings consistently on read/write. Writes are atomic and remain within the notebook directory.
- Success/error bridge envelope remains `{ok=true,...}` or `{ok=false,error={code,message,...}}`.
- Do not stop or mutate processes/files outside Graphimo's declared ownership and project scope. Do not put credentials in logs or sidecar state.

## Acceptance criteria

### Repository and bridge

1. A saved sidecar is valid JSON and reloads layout using the same notebook key as the loader; no `Path` serialization exception.
2. Existing contract and layout tests pass; where dependencies are available, production Luau-generated requests pass against pinned live marimo, including a real Run and sidecar save.
3. `.py` examples remain valid/openable by pinned marimo; component insertion/connection stays ordinary readable Python and source changes are safe/refused when ambiguous.
4. Real outputs preserve MIME identity and meaningful data; errors are surfaced and logged without secrets.
5. Documentation describes install, New/Open, components, restrictions, pinned versions, and any unverified host limitations.

### Zeron host and release

6. From the installed/signed plugin, open sidebar page, New/Open without manual connection fields, establish a live session, run a pipeline, and inspect output/log/error behavior.
7. Verify canvas pan/zoom/fit, three components drag/connect, editable parameters, panel independence, dark/light theme, resize, field click vs card drag, and layout restore with unchanged source.
8. Verify missing marimo, denied permissions, port collision, stop/restart, sleep/resume, and Zeron restart are explicit and recoverable; no foreign process is terminated.
9. Verify two notebooks and repeated component instances are isolated; component file changes refresh correctly; agent tools require an explicit target when ambiguous; mutations stay inside the project.
10. Verify the exact artifact/version under test is the one installed and confirm the signed plugin in Zeron.

## Completed repository slices and active planning

- Completed and independently validated repository slices: sidecar notebook-key serialization, Python package discovery/Conda install, searchable Components library, and safe callable parameter metadata across Python 3.11/3.13.
- The previous v0.2-oriented optional-parameter editor contract (L-05) is superseded and must not be implemented until reconciled with v0.5's functional block taxonomy and explicit draft/open-question status.
- The next leaf is a planner-approved read-only Base Block contract projection (L-06), specified below. It must not silently decide the draft's open questions or claim full v0.5 acceptance.
- Full v0.2 host/release acceptance and v0.5 implementation remain incomplete; host checks require an identified supported way to install/load this checkout in Zeron.

## Function-backed Base Block contract projection leaf

- Extend component discovery in `adapter/graphimo_adapter.py` and the production `plugin/marimo_bridge.luau` script with `base_block = { inputs, outputs, output_mode }`. Inputs are the already-safe parameter metadata list, unchanged and in signature order. `output_mode` is one of `none|single|tuple|object`; outputs are ordered `{index, name, expression?, annotation?}` entries.
- Never execute a component function to infer its contract. Use `inspect.signature`, safe annotation metadata, and `ast.parse(inspect.getsource(fn))` where source is available.
- No value-return statements or a `-> None` return annotation => `none` with empty outputs. A supported scalar return annotation (`bool`, `int`, finite `float`, `str`, or safely formatted scalar `Literal[...]`) yields one output. Without such an annotation, only an exact finite JSON-scalar AST constant proves a scalar; otherwise use one object output. For a single output, a bare returned name becomes the output label, while a compound expression uses `output_N`. Include a safe return annotation when available and `ast.unparse` expression text when static and unique.
- Split a tuple only when a fixed `tuple[T1,...,Tn]` or `typing.Tuple[...]` return annotation matches tuple-literal expressions at every value-return path with the same arity. Preserve annotation/return order. For each position, keep expression/name only when identical across branches; otherwise label `output_N` and omit expression.
- Mismatched, dynamic, unavailable-source, unsupported structures, named tuple/dataclass/dict, or ambiguous paths fall back to one `object` output without invented field decomposition.
- Generated component source gains an exact third marker `# Graphimo callable: <name>` after the existing component and source-hash markers. Python inspect adds `graphimo_component = {component_id, callable, source_sha256}` only when all markers parse and a generated single assignment/call agrees. Legacy two-marker cells remain generic. Never parse arbitrary source in Luau.
- Add pure `BaseBlock.project(components, cells)` in `plugin/base_block.luau`: return projected blocks in cell order, join exact component ID/callable/hash markers, preserve signature input order and declared output order, return nil/no projection for missing, stale, or mismatched markers, and keep duplicate instances separate.
- Tests cover the PDF's `obtenir_infos` tuple example, scalar/no-value-return/object fallback, tuple mismatch, same-arity divergent branches, JSON safety/live marimo linkage, and Luau projection ordering, marker/hash refusal, and multiple instances.
- Explicit non-goals: UI/port geometry, connection/fan-out/index semantics, code mutation/unpacking, Control/Display/Annotation/Group/Meta blocks, manual-block editor, runtime value labels, and the v0.5 §19 open questions. This leaf supplies metadata/projection only and does not claim complete Base Block interaction.

## Base Block card I/O summary leaf

- On generated component cards with an exact `BaseBlock.project` match, show a read-only I/O summary. Inputs display names in signature order, with safe annotations when available. Outputs display descriptors in declared order, formatted as `type (expression-or-name)` when an annotation exists and otherwise by name.
- Add pure `BaseBlock.summary(projected_block)` returning `{inputs = {string, ...}, outputs = {string, ...}}` in descriptor order; nil input returns nil. Format an annotated input as `annotation (name)`, otherwise `name`. Format an annotated output as `annotation (expression-or-name)`, otherwise `name`. Text formatting must be deterministic and must not mutate projection metadata.
- In `plugin/init.luau`, render the summary after the existing bottom port-label row inside the corresponding card. Keep card identity, status, output preview, generic refs/defs ports, edge rendering, and interactions unchanged. Cells with missing/legacy/stale/unmatched markers have no Base Block summary and retain the existing generic card.
- Extend `Canvas.height(cell, summary)` to budget conservatively for wrapped summary text only on matched cards; generic/legacy card dimensions remain byte-for-byte equivalent to current height calculations. For each nonempty rendered `In: ...` / `Out: ...` row, reserve 20 layout units for every 24 UTF-8 bytes (minimum one line); byte counting intentionally overestimates multibyte text. Pass the same per-cell summary into card style height, fit bounds, and node hit testing. Keep `Canvas.port` and edge endpoints unchanged because the summary follows the port-label row.
- This leaf does not create interactive ports, connection/fan-out behavior, controls, parameter editing, defaults-on-connect behavior, runtime value/type labels, source edits, or a run-from-here policy. No §19 open question is resolved.
- Validation: Luau CLI test covers input/output order, annotation/expression labels, nil/generic fallback, summary height allocation including long wrapped labels, and unchanged generic height/port anchors; compile the production UI and run the full repo suite. Actual Zeron rendering remains host validation.

## Packaging contract discovered during Conda validation

- The documented developer install (`python -m pip install -e '.[notebook,dev]'`) must work from a clean Python 3.11 environment.
- The Python distribution contains the `adapter` package only. `plugin/` is Luau loaded by Zeron, while `components/` and `examples/` are project content, not top-level Python packages. Do not make them Python packages solely to satisfy automatic discovery.
- Validation must build/install the declared project extras and import `adapter.graphimo_adapter`; pinned marimo and dev tools must resolve from the same environment.

## Component library UI leaf

- Replace the current `Component` and `Callable` dropdowns plus `Add block` row with a collapsible **Components** library panel in `plugin/init.luau`. The canvas remains the primary work surface. The Components panel starts open; Terminal/logs remains independently collapsible and starts closed.
- Search filters discovered components by component ID, category, and callable name/signature/description, case-insensitively using literal substring matching. Group results by the existing `category` metadata returned by `Bridge.list_components`; root-level items use a stable “Components” category. Preserve component/callable metadata and sorted order.
- Selecting a component expands its callables. Choosing a callable invokes the existing component insertion path and creates an ordinary readable Python cell. The library must not add a generic Python-cell item or introduce execution behavior.
- Pure UI projection interface: `Library.project(components, query)` returns ordered `{ category: string, components: { item, ... } }` groups. It does not mutate inputs; empty query returns all groups, and a no-match query returns an empty array.
- Out of scope for this leaf: bridge/runtime changes, parameter widgets, new ports, canvas geometry/drag changes, rich outputs, and host permission/lifecycle redesign.
- Validation: Luau CLI unit tests exercise grouping, empty query, searches over ID/category/callable metadata, stable order, and empty results; full pytest/Ruff plus existing live bridge check. Zeron host acceptance (panel open/collapse, selecting a callable and observing a new notebook cell, independent Terminal state) remains a separate required runtime check.

## Explicit non-goals for this leaf

- Do not redesign the full plugin UI, component widgets, or runtime bridge in this leaf.
- Do not add a Graphimo execution engine or change marimo's authority.
- Do not claim full v0.2 acceptance based on the layout fix or unit tests.

## Safe callable parameter metadata leaf

- Keep the Python adapter and production Luau-generated marimo discovery script in parity. Preserve existing parameter keys and signature; add `has_default` and safe display-only metadata.
- Every parameter retains `name`, `required`, and `positional`. `has_default` is true exactly when `inspect.Parameter.default` is not `inspect.Parameter.empty`.
- Include `default` only for JSON scalar values (`None`, bool, int, finite float, str). When the default is non-scalar or a non-finite float, omit `default` while retaining `has_default: true`.
- Include `annotation` only when safely representable without evaluating annotations: a forward-reference string as-is, `None` as `"None"`, a plain type object's `__name__` (only when its metaclass is exactly `type`), or deterministic Python-syntax `Literal[...]` text when every Literal value is a JSON scalar. Builtin `repr` may be used only after an exact scalar type guard. Unsupported annotation objects are omitted. Do not call `typing.get_type_hints`, evaluate forward references, import new modules, or call arbitrary annotation repr/str methods.
- Include `choices` only for `typing.Literal` whose values are JSON scalars; preserve their order. Identify Literal aliases using the public stdlib `typing` API and a version-tolerant exact alias-type guard, not private runtime class names. If any Literal value is unsupported, omit both `annotation` and `choices`. Enum choices and arbitrary class metadata are out of scope.
- Preserve the `signature` key and parameter structure, but do not call `str(inspect.signature(...))` on values with unsupported annotations/defaults: signature rendering must use sanitized parameters, keeping ordinary supported annotations and scalar defaults readable, omitting an unsupported annotation segment and rendering a non-scalar/non-finite default as `...`. Include a sentinel annotation/default whose `__repr__` and `__str__` raise, and prove discovery still succeeds.
- Validate adapter parity and the production Luau bridge against live marimo on Python 3.11 and a representative newer Python (3.13 where available), using a fixture callable that covers required/unannotated parameters, safe scalar defaults, a non-scalar default, a forward-ref annotation, built-in type annotations, Literal choices, and unsupported values. Ensure generated output survives JSON encoding/decoding.
- No UI editors, notebook source mutations, or execution semantics change in this leaf. Visible parameter controls remain a separate planned task after metadata review.

## Source-backed optional parameter editor leaf

- Add a bounded first slice of PDF §10: parameter controls live inside an inserted Graphimo component card, with the notebook source as the only value store. Do not implement a global form or sidecar parameter values.
- Insertion materializes supported optional scalar defaults as explicit Python keyword literals so the card can reflect source truth. Required parameters remain variable-backed input ports using the existing call-generation convention. No editor is shown for missing defaults, `None` without useful `Literal` choices, Enum/Path/arbitrary objects, or other unsupported types. An annotated primitive editor is shown only when the default exists and its exact runtime type matches the annotation; `Literal` requires a default matching one choice by exact type and value. Unannotated defaults get an editor only when their exact runtime default type is supported.
- Supported editor kinds are exact bool (checkbox), exact int (integer text input), finite float (decimal text input), str (text input), and scalar `typing.Literal` (select). Unannotated defaults may be inferred only from exact bool/int/finite-float/str types, checking bool before int. `Literal` select values preserve exact scalar types and `None` using an explicit Luau null token. Integer values and choices must be within the exact JSON-safe range `[-9007199254740991, 9007199254740991]`; values outside it get no editor and are rejected.
- Editing is staged locally. Text inputs apply on Enter or explicit Apply; checkbox/select apply on change. Typing without commit does not change notebook source. `Bridge.set_component_parameter` accepts a JSON scalar (or explicit null token), validates exact type/range and Literal membership, then safely replaces only the target generated keyword literal.
- Setter source edits are allowed only for the exact generated Graphimo cell shape: the two existing Graphimo marker lines followed by one assignment/call matching the marked component and callable. It may insert a missing optional keyword only when discovered component metadata proves that parameter has a supported default; required/missing inputs remain ports and are never inserted as literals. Reject stale source hashes, duplicate or unknown parameter names, unsupported literal/value types, missing or ambiguous markers/calls, extra statements, or changed/non-generated structure without mutating source. Preserve the markers and call structure. Success refreshes the live graph; marimo remains responsible for reactive recomputation. Never run the cell automatically.
- Inspection attaches a `graphimo_component` projection only for an unambiguous generated cell and reports its component ID, callable, source hash, and supported parameter control metadata/current values. Do not parse or mutate arbitrary Python source in Luau.
- Tests cover source literal serialization and validation, exact typing (`bool` differs from `int`), None, Literal choice validation, safe source replacement for each supported kind, stale/unknown/ambiguous/extra-statement refusal with zero mutation, live bridge edit followed by observing recomputed output, and preservation of graph references. Zeron card interaction/drag hit testing and the exact installed artifact remain host acceptance checks.
- Explicit non-goals: editors for required inputs, object/Enum/Path values, editing arbitrary existing Python calls, bulk forms, custom execution/scheduling, auto-run, changes to marimo's execution authority, and claiming full PDF §10 from this slice.

## v0.5 continuation research: callable connections and Control Blocks

- Existing canvas anchors, edges, and drag hit tests are based on live marimo `refs`/`defs`; keep that graph as the execution truth. Static Base Block descriptors currently remain descriptive metadata. `Canvas.port` is shared by drawing and hit testing, so changing its input shape affects geometry and interaction together.
- `Bridge.connect` already validates against live refs/defs and edits generated Python source through AST with compile-before-replace. There is no disconnect UI/API path. Production Luau bridge and `adapter._connect` currently accept different call shapes; define parity/refusal behavior before extending either.
- Before making Base Block descriptors interactive, resolve: connected default behavior and disconnect restoration; whether optional defaults are pins; whether required signature metadata differs from live refs; tuple-result pin mapping; and source behavior for keyword or qualified calls. No implementation contract exists for these choices yet.
- Control Block feasibility audit (marimo 0.25.2): real widgets are created by `marimo.ui` factories and live objects can be reached through `ctx.globals`. A runtime projection can potentially identify a bounded allowlist and map widget instances to bound names/cells. The available context/registry/class details are private marimo APIs; `Bridge.inspect` currently returns neither widget objects nor bindings. Treat this as version-sensitive and require live tests against the pinned marimo version.
- A Control Block metadata projection alone would not provide interactive controls in Zeron. Host-side widget rendering/embedding remains unspecified. Do not substitute source-code heuristics or claim an interactive Control Block until the rendering host contract and supported widget boundary are agreed.
- Existing tested behavior is unaffected by these findings. They define P-08 research and are not approval to alter connections, defaults, runtime source, or Zeron widget rendering.

## Read-only Control Block runtime inventory leaf

- Scope is a bridge-side inventory of actual marimo widgets, not an interactive Zeron Control Block. It is independent of unresolved Base Block pin/default/disconnect decisions in Q-006/Q-007.
- The production `Bridge.inspect` response adds an optional top-level `controls` array only when the supported runtime API is available. Each item has `{cell_id: string, name: string, kind: "slider"|"dropdown", label: string, value?: JSONScalar, choices?: [{label: string, value: JSONScalar}, ...]}`. Preserve notebook cell order, then sort bound names lexicographically per cell. Emit one item per defined name whose global is an exact supported widget instance. Associate only names in that cell's marimo `defs`; do not scan source text or unrelated globals.
- Supported runtime boundary is exactly pinned marimo `0.25.2`. Use exact `UIElement` from `marimo._plugins.ui._core.ui_element` and exact concrete `slider`/`dropdown` classes from `marimo._plugins.ui._impl.input`, plus `ctx.globals`; this private API dependency is accepted for this read-only leaf only. On version mismatch or missing/incompatible imports/context/globals, omit `controls` and return the ordinary inspect result successfully. On a supported API with no supported widgets, return `controls: []`.
- Map exact runtime `slider` to kind `slider` and exact runtime `dropdown` to kind `dropdown`; other `UIElement` subclasses, subclasses/custom widgets, and non-widgets are omitted. A widget's label is included only when it is an exact non-empty `str`; otherwise use its bound variable name. Never call arbitrary `str`/`repr` or enumerate object attributes dynamically.
- JSON scalar values are exactly `None`, exact `bool`, exact `int` in `[-9007199254740991, 9007199254740991]`, finite exact `float`, and exact `str`. Include `value` only when safe. For dropdowns, include `choices` only when every option key is an exact string and every option value is a safe JSON scalar; preserve option order and encode each as `{label: key, value: option_value}`. Otherwise omit `choices` only. Sliders have no `choices` field. Unsafe values/choices, or a failure reading one widget, omit only the affected field/item and do not fail inspection. No widget values are persisted or sent back to marimo.
- Do not render, mutate, or trigger controls; do not edit notebook source; do not infer widget types from source; do not claim interactive Control Block support. Webview/embed rendering and interactive event/update behavior remain a separate decision and host validation.
- Acceptance: an opt-in live-marimo test creates a slider and dropdown in known cells and verifies exact `cell_id`/binding/kind/label/value/ordered choices through production bridge inspect; tests a checkbox/custom widget is omitted and an unsafe dropdown value/choices are omitted without `repr` execution; verifies unsupported version/API returns ordinary inspect with `controls` absent; confirms JSON encoding with `allow_nan=False`. Adapter parity and Zeron UI rendering are out of this leaf; H-01 remains open.
