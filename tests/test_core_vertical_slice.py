from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import threading
from pathlib import Path

import pytest
from graphimo_core.artifacts import LocalArtifactStore
from graphimo_core.ledger import Event, WorkspaceGraph
from graphimo_core.runtime import LocalPythonWorker


def test_local_store_deduplicates_and_materializes_isolated_branches(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "shared.txt").write_text("shared", encoding="utf-8")
    (source / "value.txt").write_text("original", encoding="utf-8")
    store = LocalArtifactStore(tmp_path / "store")
    original = store.put_directory(source)
    branch_path = tmp_path / "branch"
    report = store.materialize(original, branch_path)
    (branch_path / "value.txt").write_text("changed", encoding="utf-8")

    branch = store.put_directory(branch_path, parent_id=original.artifact_id)
    assert branch.artifact_id != original.artifact_id
    assert store.get(original.artifact_id).artifact_id == original.artifact_id
    assert store.get(branch.artifact_id).parent_id == original.artifact_id
    assert store.parents(branch.artifact_id) == [original.artifact_id]
    assert store.materialize(original, tmp_path / "restored")["files"] == 2
    assert (tmp_path / "restored" / "value.txt").read_text(encoding="utf-8") == "original"
    assert report["method"] in {"copy", "reflink", "mixed"}
    assert (
        branch.manifest["files"]["shared.txt"]["blob"]
        == original.manifest["files"]["shared.txt"]["blob"]
    )


def test_local_store_streams_single_file_artifact(tmp_path: Path) -> None:
    path = tmp_path / "large.bin"
    path.write_bytes(bytes(range(256)) * 8192)
    store = LocalArtifactStore(tmp_path / "store")
    artifact = store.put_file(path, parent_id=None)
    destination = tmp_path / "restored.bin"
    store.materialize(artifact, destination)
    assert destination.read_bytes() == path.read_bytes()
    assert artifact.manifest["size"] == path.stat().st_size


def test_artifact_store_rejects_symlinks_and_manifest_traversal(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (source / "link").symlink_to(outside)
    store = LocalArtifactStore(tmp_path / "store")
    with pytest.raises(ValueError, match="symlinks"):
        store.put_directory(source)

    ref = store.put_bytes(b"bytes")
    (store.manifests / f"{ref.artifact_id}.json").write_text(
        json.dumps({"format": 1, "kind": "bad", "files": {"../outside": {"blob": "x", "size": 1}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="checksum"):
        store.get(ref.artifact_id)

    unsafe = {
        "format": 1,
        "kind": "dataset",
        "files": {"../outside": {"blob": "0" * 64, "size": 1}},
    }
    encoded = json.dumps(unsafe, sort_keys=True, separators=(",", ":")).encode()
    unsafe_id = hashlib.sha256(encoded).hexdigest()
    (store.manifests / f"{unsafe_id}.json").write_bytes(encoded)
    with pytest.raises(ValueError, match="path"):
        store.get(unsafe_id)


def test_artifact_gc_respects_pins_run_references_and_parent_lineage(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "store")
    source = store.put_bytes(b"source")
    derived = store.put_bytes(b"derived", parent_id=source.artifact_id)
    orphan = store.put_bytes(b"unused")
    store.pin(derived.artifact_id, "saved-model")

    report = store.gc()
    assert report["artifacts_removed"] == 1
    assert store.get(source.artifact_id).artifact_id == source.artifact_id
    assert store.get(derived.artifact_id).parent_id == source.artifact_id
    with pytest.raises(FileNotFoundError):
        store.get(orphan.artifact_id)

    store.unpin(derived.artifact_id, "saved-model")
    retained = store.gc({source.artifact_id})
    assert retained["artifacts_removed"] == 1
    assert store.get(source.artifact_id).artifact_id == source.artifact_id
    removed = store.gc()
    assert removed["artifacts_removed"] == 1
    assert removed["blobs_removed"] == 1


def test_workspace_graph_runs_and_events_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "workspace.sqlite3"
    ledger = WorkspaceGraph(path)
    input_artifact = "a" * 64
    output_artifact = "b" * 64
    ledger.add_node("a", "dataset", {"artifact_id": input_artifact}, "hash-a")
    ledger.add_node("b", "training", {}, "hash-b")
    ledger.add_edge("a", "b", "data", "input")
    run = ledger.start_run("b", "hash-b", "local", {"input": input_artifact})
    ledger.emit(Event(run.run_id, "b", "metric", 1.0, {"name": "loss", "value": 0.5}, 1))
    ledger.finish_run(run.run_id, {"output": output_artifact})
    del ledger

    reopened = WorkspaceGraph(path)
    assert reopened.graph_snapshot()["edges"] == [
        {"source_id": "a", "target_id": "b", "output_name": "data", "input_name": "input"}
    ]
    assert reopened.get_run(run.run_id).status == "succeeded"
    assert reopened.events(run.run_id)[0].payload["value"] == 0.5
    assert reopened.artifact_references() == {input_artifact, output_artifact}


def test_workspace_graph_rejects_cycles(tmp_path: Path) -> None:
    ledger = WorkspaceGraph(tmp_path / "workspace.sqlite3")
    ledger.add_node("a", "dataset", {}, "a")
    ledger.add_node("b", "transform", {}, "b")
    ledger.add_node("c", "model", {}, "c")
    ledger.add_edge("a", "b", "out", "in")
    ledger.add_edge("b", "c", "out", "in")
    with pytest.raises(ValueError, match="acyclic"):
        ledger.add_edge("c", "a", "out", "in")


def test_workspace_graph_requires_nodes_and_preserves_immutable_revisions(tmp_path: Path) -> None:
    ledger = WorkspaceGraph(tmp_path / "workspace.sqlite3")
    with pytest.raises(ValueError, match="unknown node"):
        ledger.start_run("missing", "v1", "local")

    first = ledger.add_node("transform", "python", {"source": "value = 1"}, "v1")
    run = ledger.start_run("transform", "v1", "local")
    second = ledger.add_node("transform", "python", {"source": "value = 2"}, "v2")
    ledger.finish_run(run.run_id)

    revisions = ledger.node_revisions("transform")
    assert first != second
    assert len(revisions) == 2
    assert {revision["definition"]["source"] for revision in revisions} == {
        "value = 1",
        "value = 2",
    }
    assert ledger.get_run(run.run_id).revision_id == first
    with (
        sqlite3.connect(tmp_path / "workspace.sqlite3") as db,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        db.execute("UPDATE node_revisions SET definition='{}' WHERE revision_id=?", (first,))


def test_telemetry_series_include_component_path_and_have_no_lifetime_cap() -> None:
    from graphimo_core.telemetry import TelemetryThrottle

    throttle = TelemetryThrottle(max_events_per_second=1000)
    events = []
    for index in range(600):
        throttle.emit(
            {
                "run_id": "run",
                "node_id": "train",
                "kind": "extension.activation",
                "component_path": f"layer.{index}",
                "payload": {"name": "activation", "value": index},
            },
            events.append,
        )
    assert len(events) == 600
    assert throttle.report()["dropped"] == 0
    assert throttle.report()["series"] == 600


def test_checkpoint_saves_and_restores_rng_state() -> None:
    pytest.importorskip("graphimo_pytorch.prototype")
    pytest.importorskip("torch")
    import numpy as np
    import torch
    from graphimo_pytorch.prototype import (
        ModelSpec,
        build_model,
        checkpoint_bytes,
        load_checkpoint,
        restore_rng,
    )

    random.seed(17)
    np.random.seed(17)
    torch.manual_seed(17)
    spec = ModelSpec(2, 4, {"task": 1})
    model = build_model(spec)
    optimizer = torch.optim.AdamW(model.parameters())
    blob = checkpoint_bytes(model, optimizer, spec)
    expected = (random.random(), float(np.random.rand()), float(torch.rand(())))

    random.seed(99)
    np.random.seed(99)
    torch.manual_seed(99)
    _model, _spec, checkpoint = load_checkpoint(blob)
    assert checkpoint["optimizer_state"] is not None
    assert set(checkpoint["rng_state"]) >= {"python", "numpy", "torch_cpu", "torch_cuda"}
    restore_rng(checkpoint)
    actual = (random.random(), float(np.random.rand()), float(torch.rand(())))
    assert actual == expected


def test_model_spec_compiles_to_fx_and_infers_each_head_shape() -> None:
    pytest.importorskip("graphimo_pytorch.prototype")
    pytest.importorskip("torch")
    import torch
    from graphimo_pytorch.prototype import ModelSpec, compile_fx

    spec = ModelSpec(4, 8, {"class_a": 2, "class_b": 3, "class_c": 1})
    graph_module, shapes = compile_fx(spec, batch_size=5)
    outputs = graph_module(torch.zeros(5, 4))
    assert set(outputs) == set(spec.head_sizes)
    assert shapes == {"class_a": [5, 2], "class_b": [5, 3], "class_c": [5, 1]}


def test_persistent_worker_executes_python_and_streams_throttled_events(tmp_path: Path) -> None:
    ledger = WorkspaceGraph(tmp_path / "workspace.sqlite3")
    ledger.add_node("normalize", "python", {}, "normalize-v1")
    run = ledger.start_run("normalize", "normalize-v1", "local-python")
    worker = LocalPythonWorker(tmp_path)
    events = []
    source = """
for step in range(10):
    graphimo.progress(step / 10, 'working', step=step)
    graphimo.metric('loss', float(step), step=step)
print('node output')
artifact = graphimo.put_bytes(b'result', kind='node-result')
answer = {'sum': sum(values), 'artifact_id': artifact['artifact_id']}
"""

    def save_event(event):
        events.append(event)
        ledger.emit(Event(**event))

    try:
        result = worker.request(
            {
                "op": "execute_node",
                "source": source,
                "inputs": {"values": [1, 2, 3]},
                "outputs": ["answer"],
                "run_id": run.run_id,
                "node_id": "normalize",
                "telemetry_hz": 1,
            },
            on_event=save_event,
        )
        process_id = worker.process.pid
        assert result["outputs"]["answer"]["sum"] == 6
        assert result["telemetry"]["emitted"] == len(events)
        assert result["telemetry"]["dropped"] >= 18
        assert {event.kind for event in ledger.events(run.run_id)} >= {"progress", "metric", "log"}
        assert any(event["kind"] == "metric" and event["payload"]["value"] == 9 for event in events)
        assert (
            tmp_path / "store" / "manifests" / f"{result['outputs']['answer']['artifact_id']}.json"
        ).is_file()

        with pytest.raises(RuntimeError, match="invalid Python source"):
            worker.request({"op": "execute_node", "source": "if:", "outputs": []})
        assert worker.process.pid == process_id
    finally:
        worker.close()


def test_generic_node_materializes_and_publishes_directory_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "dataset.txt").write_text("immutable input", encoding="utf-8")
    store = LocalArtifactStore(tmp_path / "store")
    original = store.put_directory(source)
    worker = LocalPythonWorker(tmp_path)
    code = """
from pathlib import Path
import tempfile
with graphimo.open_artifact(dataset_id) as input_dir:
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / "result"
        graphimo.materialize(dataset_id, output)
        (output / "derived.txt").write_text((input_dir / "dataset.txt").read_text() + " + derived")
        artifact = graphimo.put_directory(output, parent_id=dataset_id)
result = {"artifact_id": artifact["artifact_id"]}
"""
    try:
        result = worker.request(
            {
                "op": "execute_node",
                "source": code,
                "inputs": {"dataset_id": original.artifact_id},
                "outputs": ["result"],
            }
        )
    finally:
        worker.close()
    derived = store.get(result["outputs"]["result"]["artifact_id"])
    assert derived.parent_id == original.artifact_id
    assert store.read_file(derived, "derived.txt") == b"immutable input + derived"


def test_worker_accepts_live_probe_control_during_training(tmp_path: Path) -> None:
    pytest.importorskip("graphimo_pytorch.prototype")
    pytest.importorskip("torch")
    worker = LocalPythonWorker(tmp_path)
    events = []
    ready = threading.Event()
    failure: list[Exception] = []
    source = """
import time
import torch
from graphimo_pytorch.prototype import ModelSpec, build_model, train_multihead
spec = ModelSpec(4, 8, {"task": 1})
model = build_model(spec)
x = torch.ones(32, 4)
targets = {"task": torch.zeros(32, 1)}
graphimo.progress(0.0, "ready")
time.sleep(0.25)
def publish(kind, payload, step, component_path, durable):
    graphimo.event(kind, payload, step=step, component_path=component_path, durable=durable)
train_multihead(model, x, targets, epochs=3, emit=publish, poll_controls=graphimo.poll_controls)
answer = "done"
"""

    def run():
        try:
            result = worker.request(
                {"op": "execute_node", "source": source, "outputs": ["answer"]},
                on_event=lambda event: (events.append(event), ready.set()),
            )
            assert result["outputs"]["answer"] == "done"
        except Exception as exc:  # noqa: BLE001 - forward thread failures to the test thread
            failure.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert ready.wait(5)
        ack = worker.control({"op": "subscribe_probe", "module_path": "backbone.0"})
        assert ack["accepted"]
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not failure
        probes = [event for event in events if event["kind"] == "model_probe"]
        assert {event["payload"]["phase"] for event in probes} >= {"activation", "gradient"}
        assert all(
            event["component_path"] == "backbone.0" and not event["durable"] for event in probes
        )
    finally:
        worker.close()


def test_training_checkpoint_resumes_same_optimizer_trajectory_across_worker_restart(
    tmp_path: Path,
) -> None:
    pytest.importorskip("graphimo_pytorch.prototype")
    pytest.importorskip("torch")
    import numpy as np
    import torch
    from graphimo_pytorch.prototype import (
        ModelSpec,
        build_model,
        load_checkpoint,
        train_multihead,
    )

    spec = ModelSpec(3, 6, {"task": 2})
    x = torch.arange(48, dtype=torch.float32).reshape(16, 3) / 20
    targets = {"task": torch.flip(x[:, :2], dims=[0])}

    torch.manual_seed(121)
    uninterrupted_model = build_model(spec)
    uninterrupted_optimizer = torch.optim.AdamW(uninterrupted_model.parameters(), lr=0.01)
    train_multihead(uninterrupted_model, x, targets, epochs=8, optimizer=uninterrupted_optimizer)
    expected = {
        key: value.detach().clone() for key, value in uninterrupted_model.state_dict().items()
    }

    artifact_source = tmp_path / "dataset"
    artifact_source.mkdir()
    np.savez_compressed(artifact_source / "data.npz", x=x.numpy(), task=targets["task"].numpy())
    store = LocalArtifactStore(tmp_path / "store")
    dataset = store.put_directory(artifact_source)
    worker = LocalPythonWorker(tmp_path)
    partial_source = """
import numpy as np
import torch
from graphimo_pytorch.prototype import ModelSpec, build_model, checkpoint_bytes, train_multihead
torch.manual_seed(seed)
spec = ModelSpec(**model_spec)
with graphimo.open_artifact(dataset_id) as directory:
    with np.load(directory / "data.npz") as data:
        x = torch.from_numpy(data["x"].copy())
        targets = {"task": torch.from_numpy(data["task"].copy())}
model = build_model(spec)
optimizer = torch.optim.AdamW(model.parameters(), lr=0.01)
train_multihead(model, x, targets, epochs=4, optimizer=optimizer)
checkpoint = graphimo.put_bytes(checkpoint_bytes(model, optimizer, spec,
    metadata={"next_epoch": 4}), kind="checkpoint", parent_id=dataset_id)
partial_result = {"checkpoint_id": checkpoint["artifact_id"]}
"""
    first = worker.request(
        {
            "op": "execute_node",
            "source": partial_source,
            "inputs": {"dataset_id": dataset.artifact_id, "model_spec": spec.__dict__, "seed": 121},
            "outputs": ["partial_result"],
        }
    )
    checkpoint_id = first["outputs"]["partial_result"]["checkpoint_id"]
    worker.terminate()

    resume_source = """
import numpy as np
import torch
from graphimo_pytorch.prototype import checkpoint_bytes, load_training_checkpoint, train_multihead
model, spec, optimizer, checkpoint = load_training_checkpoint(graphimo.read_bytes(checkpoint_id))
with graphimo.open_artifact(dataset_id) as directory:
    with np.load(directory / "data.npz") as data:
        x = torch.from_numpy(data["x"].copy())
        targets = {"task": torch.from_numpy(data["task"].copy())}
train_multihead(model, x, targets, epochs=4, optimizer=optimizer, start_epoch=4)
resumed_checkpoint = graphimo.put_bytes(checkpoint_bytes(model, optimizer, spec), kind="checkpoint")
resume_result = {"checkpoint_id": resumed_checkpoint["artifact_id"]}
"""
    resumed = worker.request(
        {
            "op": "execute_node",
            "source": resume_source,
            "inputs": {"dataset_id": dataset.artifact_id, "checkpoint_id": checkpoint_id},
            "outputs": ["resume_result"],
        }
    )
    model_after, _, _ = load_checkpoint(
        store.read_bytes(store.get(resumed["outputs"]["resume_result"]["checkpoint_id"]))
    )
    worker.close()
    assert all(torch.equal(expected[key], model_after.state_dict()[key]) for key in expected)


def test_worker_timeout_and_external_cancel_restart_the_process(tmp_path: Path) -> None:
    worker = LocalPythonWorker(tmp_path)
    request = {
        "op": "execute_node",
        "source": "import time\ntime.sleep(2)\nanswer = 1",
        "outputs": ["answer"],
    }
    try:
        with pytest.raises(TimeoutError, match="worker stopped"):
            worker.request(request, timeout_seconds=0.05)
        assert worker.process is None

        event_seen = threading.Event()
        failure: list[Exception] = []

        def long_request():
            try:
                worker.request(
                    {
                        "op": "execute_node",
                        "source": "import time\nprint('started')\ngraphimo.progress(0.1)\ntime.sleep(10)\nanswer = 1",
                        "outputs": ["answer"],
                    },
                    on_event=lambda event: event_seen.set() if event["kind"] == "log" else None,
                )
            except Exception as exc:  # noqa: BLE001 - forward thread failures to the test thread
                failure.append(exc)

        thread = threading.Thread(target=long_request)
        thread.start()
        assert event_seen.wait(5)
        worker.cancel()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert failure and "exited without a response" in str(failure[0])

        restarted = worker.request(
            {"op": "execute_node", "source": "answer = 2", "outputs": ["answer"]}
        )
        assert restarted["outputs"]["answer"] == 2
    finally:
        worker.close()


def test_vertical_slice_trains_checkpoints_restarts_and_forks(tmp_path: Path) -> None:
    pytest.importorskip("graphimo_pytorch.prototype")
    pytest.importorskip("torch")
    import importlib.util

    from graphimo_pytorch.vertical_demo import run_vertical_demo

    report = run_vertical_demo(tmp_path / "workspace")

    assert report["graph_survived_close_reopen"]
    assert report["checkpoint_optimizer_saved"]
    assert report["checkpoint_rng_saved"]
    assert report["worker_restart_predictions_equal"]
    assert report["worker_reused_after_invalid_request"]
    assert report["worker_pids"][0] != report["worker_pids"][1]
    assert report["three_heads_trained"]
    assert report["loss_improved"]
    has_torchlens = importlib.util.find_spec("torchlens") is not None
    assert report["torchlens_inspection"]["available"] is has_torchlens
    if has_torchlens:
        assert report["torchlens_inspection"]["validated"]
    assert report["migration"]["loaded"]
    assert report["migration"]["incompatible"]
    assert report["event_count"] >= 12
    assert report["artifact_ids"]["D"]

    ledger = WorkspaceGraph(tmp_path / "workspace" / "workspace.sqlite3")
    events = ledger.events(report["runs"][0])
    assert {event.kind for event in events} >= {"metric", "progress", "resource"}
    assert all(ledger.get_run(run_id).status == "succeeded" for run_id in report["runs"])
