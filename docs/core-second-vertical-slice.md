# Graphimo core: second vertical slice

This report records the follow-up validation on top of the first core slice. It
focuses on three risks from the architecture review: ordinary data nodes,
cooperative control and live model probes during a run, and optimizer/RNG
continuation after the worker process is destroyed. It does not implement a UI.

## Results

| Requirement | Result | Evidence |
| --- | --- | --- |
| A → B and A → C are produced by ordinary Python nodes | Verified | `run_vertical_demo` uses `execute_node`, `graphimo.materialize`, and `graphimo.put_directory` for both branches. |
| Nodes can work with datasets without loading each whole artifact into Python bytes | Verified for filesystem APIs | `open_artifact` and `materialize` expose a local isolated path; directory/file import and checksum verification stream in 1 MiB chunks. |
| Evaluation is a normal Python node | Verified | The demo's evaluation source is stored as a node revision and runs through `execute_node`; the worker has no `evaluate_checkpoint` operation. |
| Subscribe to a model component during an active run | Verified, cooperative | The worker accepts a control frame while an execution thread is active. `train_multihead` polls commands between epochs and attaches/removes hooks on the selected named module. |
| Receive activation and gradient summaries for the selected component | Verified | Live events contain phase, shape, mean, and standard deviation plus the exact `component_path`. The test checks activation and gradient events. |
| Keep probe telemetry out of durable history by default | Verified | Probe events are delivered to the live event callback with `durable=false`; `WorkspaceGraph.emit` ignores those events. Progress, metrics, logs, and resources remain durable by default. |
| Avoid a fixed total-event lifetime cap | Verified | Throttling is per `(run, node, kind, component_path, metric name)` series. It coalesces to the latest pending value; it does not stop after 500 events. A 600-series test verifies this. |
| Preserve node code/config that produced each run | Verified | `add_node` appends a content-derived immutable revision. Every run stores the selected `revision_id`; later edits update the current node pointer without replacing prior revisions. `start_run` rejects missing nodes or mismatched code versions. |
| Resume model + optimizer + RNG after process restart | Verified for deterministic CPU training | The test compares 8 uninterrupted AdamW epochs with 4 epochs, worker termination, checkpoint restore, and 4 resumed epochs. Parameters are exactly equal for the deterministic fixture. |
| Protect the core checks in GitHub Actions | Added | `.github/workflows/core.yml` installs the prototype dependencies, runs Ruff, and tests the core suite on Python 3.11. The workflow has not yet run on GitHub from this workspace. |

## Data node API

Python node source receives input values as named variables and a `graphimo`
API object. Artifact IDs, not open file handles or Python object pointers, cross
the node boundary.

```python
with graphimo.open_artifact(dataset_id) as dataset_dir:
    # A file artifact materializes as a file; a directory artifact as a directory.
    source = dataset_dir / "images"
    # Transform data with ordinary Python libraries here.
    result = transform(source)
    output_dir = dataset_dir.parent / "derived"
    write_result(result, output_dir)

artifact = graphimo.put_directory(
    output_dir, kind="dataset", parent_id=dataset_id
)
result_id = artifact["artifact_id"]
```

Available artifact methods:

- `graphimo.materialize(artifact_id, destination)` copies/reflinks to an explicit path.
- `graphimo.open_artifact(artifact_id)` is a context manager that materializes to a temporary path and removes it when the context exits.
- `graphimo.put_directory(path, kind="dataset", parent_id=...)` stores a directory manifest and deduplicated file blobs.
- `graphimo.put_file(path, kind="node_output", parent_id=...)` streams one regular file into the content store.
- `graphimo.put_bytes(bytes, ...)`, `read_bytes(id)`, and `read_file(id, relative_path)` remain available for small payloads. The `read_*` methods return the entire payload in memory; use `open_artifact`/`materialize` for large inputs.

File-level CAS deduplication remains the storage model. Chunked directory
formats such as Zarr naturally provide smaller deduplication units because each
chunk is a separate file. A single huge `.npz` is still one blob when changed;
this slice does not add a custom chunk store.

`open_artifact` uses a temporary directory, not a RAM cache. It avoids loading a
whole dataset into bytes, but repeated opens still materialize from the local
artifact store. An in-memory/VRAM object cache remains future work.

## Worker control and live probes

The worker now has two concurrent paths while a node runs:

```text
LocalPythonWorker.request(execute_node)
       │
       ├── execution thread: node Python + stdout/events + result
       └── control reader: subscribe_probe / unsubscribe_probe + acknowledgement
```

The public control calls are:

```python
worker.control({"op": "subscribe_probe", "module_path": "backbone.0"})
worker.control({"op": "unsubscribe_probe", "module_path": "backbone.0"})
```

Controls are accepted only while a node is active. A node must cooperate by
calling `graphimo.poll_controls()` at safe points. `train_multihead` polls at
the start of each epoch, so a subscription applies by the next epoch boundary.
The selected path must match a name from `model.named_modules()`. Forward and
full-backward hooks emit ephemeral activation/gradient summaries. The callbacks
run in the training process; the control reader never inspects or mutates a
model concurrently.

This proves live selection and telemetry during a supported cooperative
training node. It does not interrupt arbitrary Python/CUDA code, pause a node,
checkpoint it on demand, or provide arbitrary inspection of every framework.
Long-running custom nodes need to poll at safe points. Probe summaries currently
include shape, mean, and standard deviation; configurable histograms, NaN
diagnostics, head-level attribution, and a general subscription API for
non-PyTorch frameworks remain unimplemented.

`TelemetryThrottle` coalesces updates per run, node, event kind, component path,
and metric name. It is a live-rate limiter; durable SQLite events and ephemeral
probe events are separate paths. The core does not persist every activation or
gradient sample.

## Training checkpoint continuation

`checkpoint_bytes` includes model state, optimizer state, Python/NumPy/PyTorch
RNG states, and caller metadata. `load_training_checkpoint` reconstructs the
model and AdamW optimizer, loads optimizer state, switches the model back to
training mode, and restores RNG state. The node then passes the restored
optimizer and the saved next epoch to `train_multihead`.

The exact-equivalence test uses CPU tensors, deterministic inputs, and the same
PyTorch process configuration. CUDA kernels and nondeterministic operators may
not reproduce bit-for-bit; a production resume contract should record backend,
device, library versions, data sampler state, and an explicit tolerance.

## Provenance and telemetry semantics

The current `nodes` table is a pointer to the latest definition. Each
`node_revisions` entry is immutable and stores `kind`, serialized definition,
code version, and a content-derived revision ID. `runs.revision_id` selects the
exact revision that ran. Database triggers reject runs without a matching
revision and updates/deletes of revisions. Events must refer to the node named
by their run.

Durable event kinds can still make the event database grow over long runs.
This slice removes the misleading 500-event lifetime cutoff but does not yet
implement retention/compaction policies for durable logs and metrics. Live
probe events are marked non-durable and are delivered only to the active event
consumer.

## Verification

Local validation is recorded in the branch's test output. The relevant commands
are:

```bash
python -m pytest -q tests/test_core_vertical_slice.py tests/test_optional_torchlens.py
ruff check core/graphimo_core tests/test_core_vertical_slice.py tests/test_optional_torchlens.py
ruff format --check core/graphimo_core tests/test_core_vertical_slice.py
```

The tests include the original artifact/worker/model contracts, generic
directory transformations through the worker, control during active training,
exact optimizer continuation after worker restart, immutable node revisions,
and component-aware telemetry beyond 500 events. TorchLens integration remains
optional and is skipped when the package is absent.
