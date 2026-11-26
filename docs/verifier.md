# Graphimo Verifier

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
  and the target cell references that variable. Preserved outputs from disabled
  Graphimo instances remain valid stale edges.
- `assert_output(cell_id, output_name, expected_preview)` compares a named
  output's bounded, JSON-safe preview exactly. It validates the representation
  visible through inspection, not the identity or full value of a live Python
  object.
- `assert_no_errors()`, `assert_blocked_by()` and `assert_source_equals()` check
  live cell errors, disabled-ancestor state and byte-identical cell source.
- `execute()` and `batch()` delegate unchanged to the project client, allowing
  test scenarios to use precisely the same named actions and ordered batch
  semantics as the UI and agents. Store the sequence and expected assertions in
  ordinary tests or scenario files for deterministic replay.

## Deterministic scenarios and journals

`VerifierScenario` stores an ordered sequence of ordinary Graphimo actions, a
user-selected seed, per-step invariants and assertions. Scenario definitions
are JSON, can be committed with tests, and replay through
`GraphimoProject.execute()` rather than introducing another scheduler. `$ref`
expressions can use earlier step results, matching the service batch reference
syntax. The runner checks the configured invariants after each action, records
resolved arguments/results/errors and atomically updates a local run journal
after every step. A failed step raises `ScenarioRunError`; its `.journal`
contains the seed and the exact operation where replay stopped.

```python
from graphimo.verifier import GraphimoVerifier, VerifierScenario
from pathlib import Path

scenario = VerifierScenario("scalar_smoke", seed=48192)
scenario.add("notebook.add_cell", {"source": "answer = 6 * 7"}, step_id="add_answer")
scenario.add(
    "notebook.run",
    {"cell_id": {"$ref": "add_answer.result.cell_id"}},
    step_id="run_answer",
    assertions=[
        {
            "kind": "output_preview",
            "cell_id": {"$ref": "add_answer.result.cell_id"},
            "output_name": "answer",
            "equals": "42",
            "wait_seconds": 4,
        }
    ],
)

with GraphimoVerifier.connect_local() as verifier:
    with verifier.project(Path("/path/to/notebook.py")) as project:
        project.run_scenario(scenario, journal_path="artifacts/scalar-smoke-run.json")
```

The seed is recorded in the scenario and journal. It does not silently reseed a
kernel or libraries; scenario source/setup must consume the seed explicitly.
Current generic checks are `graph_consistent` and `no_errors`; step assertions
include `blocked_by`, `source_equals` and `output_preview` (with a bounded wait
for reactive output). Journal files can contain the action arguments and
results; choose a local path appropriate for the project data.

The current graph snapshot intentionally omits arbitrary object serialization.
For stronger value checks, make the notebook expose a scalar/string summary or
persist a value as an Artifact and inspect that artifact through the existing
ArtifactStore API. V1 does not claim byte-for-byte determinism for model
training, external data, or operations whose source code is nondeterministic.

## Real-kernel coverage

```python
with verifier.project(notebook_path) as project:
    inserted = project.execute("notebook.add_cell", {"source": "answer = 6 * 7"})
    project.execute("notebook.run", {"cell_id": inserted["cell_id"]})
    project.assert_graph_consistent()
    project.assert_output(inserted["cell_id"], "answer", "42")
```

The live marimo integration suite now runs Verifier scenarios against an actual
marimo 0.25.2 process. It exercises component output inspection, scalar output
assertion, Control Block creation, disable → blocked run refusal → re-enable,
protected history edits, Group creation/replacement and service restart with
the same live cell IDs, source and edges. A seeded PyTorch scenario subscribes
to activation and gradient events, runs forward/backward in the kernel, checks
that both events reach the service, then saves and inspects a checkpoint
artifact. It is an API/kernel integration test; it does not launch or automate
the Zeron visual canvas.

This is a Verifier foundation, not yet the full stateful fuzzer. Automatic
action generation, shrinking, differential agent/UI runs, broad PyTorch
scenario generation and soak testing remain later increments. The existing
Graphimo Workbench stays the UI surface; this tranche adds no second verifier
application or canvas.

For currently supported actions and marimo boundaries, see
[`service.md`](service.md), [`node-instance-contract.md`](node-instance-contract.md)
and [`zeron-service-adapter.md`](zeron-service-adapter.md).
