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
component.delete  {path}
```

`component.write` accepts a relative `.py` path and bounded UTF-8 source. It
rejects path traversal, hidden/cache paths and symlinks. The candidate is
written to a sibling temporary file, syntax checked, formatted and linted with
`ruff` when available, then imported in a bounded child process to inspect
public `@node` contracts. Only a fully valid candidate replaces the target.
Validation errors return structured diagnostics and leave an existing valid
file untouched. When `ruff` is unavailable, the result includes a warning and
still performs the Python and Graphimo contract checks.

`component.validate` runs the same checks on an existing file without
rewriting it. `component.list` uses static syntax inspection and does not
import project components. `component.delete` moves a file into
`.graphimo/component-trash/` so it can be restored; it does not permanently
remove the source.

Each operation uses the same `GraphimoApplication` route as service clients and
Zeron tools. Multiple writes/edits can be sequenced in `execute_batch`; later
actions may refer to earlier results through the service batch reference
syntax.
