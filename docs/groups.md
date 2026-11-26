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

## Create a Group Block from live cells

The `group.create_from_selection` application action accepts a snapshot of
live marimo cells and their selected IDs. Graphimo Core preserves the selected
cell source in the group's marimo app and infers the Group contract from graph
edges crossing the selection boundary:

- dependencies from unselected parent cells become Group inputs;
- definitions consumed by unselected child cells become Group outputs;
- dependencies wholly inside the selection remain internal to the Group.

The result contains the new Group catalog item, generated parent-call source,
ordered selected IDs and SHA-256 hashes of the selected cell sources. A host
must check those hashes against the current marimo cells before replacing
anything. The Zeron bridge performs that check and replaces the selection in
one marimo code-mode request; if marimo mutation fails, it attempts to restore
the original source for every cell already removed. This prevents an agent or
user action based on an outdated graph snapshot from deleting newer edits.

In the Graphimo canvas, Shift-click to select multiple cells, then right-click
and choose **Create Group Block**. The agent tool
`graphimo_group_from_selection` performs the same Core operation and
hash-checked marimo replacement from explicit cell IDs. Both surfaces use the
same `GraphimoApplication` action; the renderer/bridge only supplies the live
marimo snapshot and applies the safe replacement.

The selection action accepts 2–128 unique live cells and a snapshot of no more
than 2048 cells. Selected source is bounded before it is written as a Group
file. Duplicate definitions, malformed dependency metadata, invalid Python,
and stale/missing cell IDs are rejected before the group is created.

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
group.delete
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
group ID remains stable when the display name changes. `group.delete` requires
the expected source SHA-256, so a stale cleanup request cannot remove a Group
that was edited after inspection.

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
