# Agent-authored components

Project components are ordinary Python files under the sibling `components/`
directory. Public reusable functions use Graphimo Core's `@node` marker:

```python
from graphimo import node

@node
def normalize(volume, low: float = -1000.0, high: float = 1000.0):
    clipped = volume.clip(low, high)
    return (clipped - low) / (high - low)
```

The shared application exposes these actions:

```text
component.list
component.write   {path, source}
component.validate {path}
component.instance_source {path, callable, output_name, bindings}
component.add      {path, callable, output_name, bindings}
component.delete  {path}
```

`component.write` accepts a relative `.py` path and bounded UTF-8 source. It
rejects path traversal, hidden/cache paths and symlinks. Validation is fully
static: it parses the candidate AST and never imports the component or any of
its dependencies. The AST policy rejects executable module-level statements. A component module may
contain imports, literal constants, helper/function definitions and public
`@node` functions; top-level calls, loops, context managers, exception blocks,
classes, executable defaults/annotations/decorators and variadic parameters are
rejected. Public `@node` signatures, safe literal defaults, return shapes and
output expressions are inferred from syntax. The candidate is written to a
sibling temporary file, syntax checked, formatted and linted with `ruff` when
available, then atomically replaces the target only if its static Graphimo
contract is valid. Validation errors return structured diagnostics and leave
an existing valid file untouched. When `ruff` is unavailable, the result
includes a warning and still performs Python and Graphimo AST checks.

This protects validation from import-time side effects; it is not a sandbox for
component execution. Imports and function bodies run naturally when marimo
executes the inserted component library cell. Components should therefore
still come from trusted project sources.

`component.validate` runs the same checks on an existing file without
rewriting it. `component.list` uses static syntax inspection and does not
import project components. `component.delete` moves a file into
`.graphimo/component-trash/` so it can be restored; it does not permanently
remove the source.

`component.instance_source` generates readable ordinary Python from a
validated `@node` contract. For keyword-bindable nodes, the library import and
canonical instance are separate cells: the import cell defines the reusable
node callable, while the instance source has the stable Graphimo markers and
its `g_<node>_<id>` output symbol. `component.add` inserts both into an
attached marimo session, schedules the import cell through marimo, and returns
the instance cell ID and output symbol. Reusing a component updates the
existing marked import cell when its validated source hash changes. Bindings
are live Python names or bounded JSON literals.

Python positional-only parameters cannot be represented by the canonical
keyword-binding instance format. To preserve valid Python calls, such
components currently use a direct imported call and are marked
`canonical_instance: false`; the safe Graphimo-only connect/disable actions do
not edit those cells. When an omitted earlier positional-only default would
make the call ambiguous, the builder refuses to generate it.

Each operation uses the same `GraphimoApplication` route as service clients and
Zeron tools. Multiple writes/edits can be sequenced in `execute_batch`; later
actions may refer to earlier results through the service batch reference
syntax.
