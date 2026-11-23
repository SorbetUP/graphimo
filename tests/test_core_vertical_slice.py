from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import threading
from pathlib import Path

import pytest

from graphimo_core.artifacts import LocalArtifactStore
from graphimo_core.ledger import Event, WorkspaceGraph
from graphimo_core.pytorch import ModelSpec, build_model, checkpoint_bytes, compile_fx, load_checkpoint, restore_rng
from graphimo_core.runtime import LocalPythonWorker
from graphimo_core.vertical_demo import run_vertical_demo


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
    assert branch.manifest["files"]["shared.txt"]["blob"] == original.manifest["files"]["shared.txt"]["blob"]


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

    unsafe = {"format": 1, "kind": "dataset", "files": {"../outside": {"blob": "0" * 64, "size": 1}}}
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


def test_checkpoint_saves_and_restores_rng_state() -> None:
    import numpy as np
    import torch

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
    import torch

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
        result = worker.request({"op": "execute_node", "source": source, "inputs": {"values": [1, 2, 3]},
                                "outputs": ["answer"], "run_id": run.run_id, "node_id": "normalize",
                                "telemetry_hz": 1}, on_event=save_event)
        process_id = worker.process.pid
        assert result["outputs"]["answer"]["sum"] == 6
        assert result["telemetry"]["emitted"] == len(events)
        assert result["telemetry"]["dropped"] >= 18
        assert {event.kind for event in ledger.events(run.run_id)} >= {"progress", "metric", "log"}
        assert any(event["kind"] == "metric" and event["payload"]["value"] == 9 for event in events)
        assert (tmp_path / "store" / "manifests" / f"{result['outputs']['answer']['artifact_id']}.json").is_file()

        with pytest.raises(RuntimeError, match="invalid Python source"):
            worker.request({"op": "execute_node", "source": "if:", "outputs": []})
        assert worker.process.pid == process_id
    finally:
        worker.close()


def test_worker_timeout_and_external_cancel_restart_the_process(tmp_path: Path) -> None:
    worker = LocalPythonWorker(tmp_path)
    request = {"op": "execute_node", "source": "import time\ntime.sleep(2)\nanswer = 1",
               "outputs": ["answer"]}
    try:
        with pytest.raises(TimeoutError, match="worker stopped"):
            worker.request(request, timeout_seconds=0.05)
        assert worker.process is None

        event_seen = threading.Event()
        failure: list[Exception] = []

        def long_request():
            try:
                worker.request({"op": "execute_node",
                                "source": "import time\nprint('started')\ngraphimo.progress(0.1)\ntime.sleep(10)\nanswer = 1",
                                "outputs": ["answer"]},
                               on_event=lambda event: event_seen.set() if event["kind"] == "log" else None)
            except Exception as exc:
                failure.append(exc)

        thread = threading.Thread(target=long_request)
        thread.start()
        assert event_seen.wait(5)
        worker.cancel()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert failure and "exited without a response" in str(failure[0])

        restarted = worker.request({"op": "execute_node", "source": "answer = 2", "outputs": ["answer"]})
        assert restarted["outputs"]["answer"] == 2
    finally:
        worker.close()


def test_vertical_slice_trains_checkpoints_restarts_and_forks(tmp_path: Path) -> None:
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
    assert {event.kind for event in events} >= {"metric", "progress", "model_probe", "resource"}
    assert all(ledger.get_run(run_id).status == "succeeded" for run_id in report["runs"])
