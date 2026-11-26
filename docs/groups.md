# Graphimo Group Blocks

A Group Block is a reusable marimo sub-workflow stored as a Python file in the
project's `groups/` package. The group remains in the normal Python dialect.
Graphimo imports and calls the group; it does not copy the group source into
each parent block.

The generic Core API is in `graphimo.groups`. Importing Graphimo does not
import marimo. Group execution requires the project-pinned `marimo==0.25.2`.

## Group contract

Each group file contains a literal `GRAPHIMO_GROUP` contract, a
Graphimo-managed input cell, ordinary marimo workflow cells, and a callable
`@node` wrapper. For example:

```python
GRAPHIMO_GROUP = {
    "format": 1,
    "group_id": "scale_values",
    "name": "Scale Values",
    "module": "scale_values",
    "dialect_id": "python",
    "inputs": ["value", "factor"],
    "outputs": ["result"],
}

app = marimo.App()

@app.cell
def __graphimo_inputs():
    value = None
    factor = None
    return value, factor

@app.cell
def calculate(value, factor):
    result = value * factor
    return result,
```

Create a file through the shared application layer:

```python
created = application.execute(
    "group.create",
    {
        "request": {
            "group_id": "scale_values",
            "module": "scale_values",
            "name": "Scale Values",
            "inputs": ["value", "factor"],
            "outputs": ["result"],
            "body_source": body_source,
        }
    },
)
```

The returned catalog item includes the stable group ID, relative file path,
callable name, input/output contract, source hash and import statement. The
group is a normal reusable callable:

```python
from groups.scale_values import scale_values

scaled = scale_values(value=volume, factor=2.0)
print(scaled.result)
```

`GroupResult` is one opaque return value for the parent Graphimo instance. It
supports named attribute access and mapping access, for example
`scaled.result` or `scaled["result"]`.

## Execution semantics

`run_group(path, inputs=..., outputs=...)` reads the saved contract and checks
that every declared input is supplied exactly once. The input-definition cell
defines all group inputs together; marimo receives them through
`App.run(defs=...)` and therefore skips the original input cell for that
invocation. The declared outputs are selected from marimo's returned definition
mapping. Missing outputs, extra inputs, and contract mismatches produce a
`GroupError`.

Every invocation clones the group's marimo app and calls `run()` on that
clone. Repeated group instances therefore receive independent app/runtime
instances. marimo owns dependency analysis and execution inside the group;
Graphimo adds no scheduler or DAG evaluator.

The generated component wrapper is decorated with Graphimo Core's `@node`
marker so standard node introspection and palette code can recognize it.
Reusable groups use reference/import semantics. A later copy/vendor action may
duplicate a group explicitly, but it is not the default.

## Catalog, contracts and navigation

The host-independent application actions are:

```text
group.list
group.create
group.inspect
group.set_contract
scope.enter
scope.current
scope.leave
layout.get
layout.set
```

`group.list` reads literal contracts without importing group modules, so a
palette can list entries without executing workflow code. Invalid files are
returned as per-file diagnostics. `group.set_contract` updates only the managed
contract/input cell/callable sections and preserves the workflow cells. The
group ID remains stable when the display name changes.

`scope.enter` takes a `container_id` equal to a group ID and returns the active
scope plus the breadcrumb path. Nested navigation uses the same Python dialect.
The returned stable `scope_id` can be used with `layout.get` and `layout.set`
to keep each group canvas layout separate from its semantic Python file.

The project group directory is a reference-based library:

```text
notebook.py
groups/
  __init__.py
  scale_values.py
```

Editing the cells in a group file changes that shared group for every caller.
The `group.set_contract` operation preserves those cells while changing the
managed input/output declarations. It does not attempt to infer or rewrite
arbitrary Python body code when a port is removed.
