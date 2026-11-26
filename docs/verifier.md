# Graphimo Verifier V1

The Verifier is a small assertion layer over Graphimo's normal authenticated
service client. It does not own a graph model, edit notebook files itself,
schedule Python, or introduce a second execution path. Actions still go
through `GraphimoClient` → `GraphimoApplication` → the attached marimo kernel.

```python
from graphimo.verifier import GraphimoVerifier

with GraphimoVerifier.connect_local() as verifier:
    with verifier.project("/path/to/notebook.py") as project:
        # Use the same application actions as the Zeron adapter and agents.
        project.execute("notebook.run", {"cell_id": "cell-id"})
        report = project.assert_graph_consistent()
        print(report)
```

The service must have an attached live marimo kernel for notebook operations.
As in the regular Graphimo client, `notebook.inspect` returns a bounded
snapshot; consistency assertions fail closed if it is truncated.

## V1 assertions

- `assert_graph_consistent()` checks unique cell IDs and verifies each edge has
  both endpoints in the snapshot, its variable is defined by the source cell,
  and the target cell references that variable.
- `assert_output(cell_id, output_name, expected_preview)` compares a named
  output's bounded, JSON-safe preview exactly. It validates the representation
  visible through inspection, not the identity or full value of a live Python
  object.
- `execute()` and `batch()` delegate unchanged to the project client, allowing
  test scenarios to use precisely the same named actions and ordered batch
  semantics as the UI and agents. Store the sequence and expected assertions in
  ordinary tests or scenario files for deterministic replay.

The current graph snapshot intentionally omits arbitrary object serialization.
For stronger value checks, make the notebook expose a scalar/string summary or
persist a value as an Artifact and inspect that artifact through the existing
ArtifactStore API. V1 does not claim byte-for-byte determinism for model
training, external data, or operations whose source code is nondeterministic.

## Example in a test

```python
with verifier.project(notebook_path) as project:
    inserted = project.execute("notebook.add_cell", {"source": "answer = 6 * 7"})
    project.execute("notebook.run", {"cell_id": inserted["cell_id"]})
    project.assert_graph_consistent()
    project.assert_output(inserted["cell_id"], "answer", "42")
```

For currently supported actions and marimo boundaries, see
[`service.md`](service.md), [`node-instance-contract.md`](node-instance-contract.md)
and [`zeron-service-adapter.md`](zeron-service-adapter.md).
