"""LEGACY vertical demo over the experimental graphimo_core prototype.

Kept for ``tests/test_core_vertical_slice.py``; the production PyTorch path is
the MetaBlock dialect + ``graphimo.service``. See ``docs/production-architecture.md``.
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
from graphimo_core.artifacts import LocalArtifactStore
from graphimo_core.ledger import Event, WorkspaceGraph
from graphimo_core.runtime import LocalPythonWorker

from graphimo_pytorch.prototype import ModelSpec, load_checkpoint


def _npz_bytes(**arrays: Any) -> bytes:
    stream = io.BytesIO()
    np.savez_compressed(stream, **arrays)
    return stream.getvalue()


def _version(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def run_vertical_demo(workspace: str | Path) -> dict[str, Any]:
    """Exercise generic data nodes, multi-head training, restore and lineage."""
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    store = LocalArtifactStore(root / "store")
    ledger = WorkspaceGraph(root / "workspace.sqlite3")

    rng = np.random.default_rng(7)
    x = rng.normal(size=(96, 4)).astype(np.float32)
    y = {
        "task_a": (x[:, :1] * 0.8 - x[:, 1:2] * 0.25).astype(np.float32),
        "task_b": (x[:, 2:3] * 0.5 + x[:, 3:4] * 0.2).astype(np.float32),
        "task_c": (x.sum(axis=1, keepdims=True) * 0.15).astype(np.float32),
    }
    with tempfile.TemporaryDirectory(prefix="graphimo-data-") as temporary:
        original = Path(temporary) / "A"
        original.mkdir()
        (original / "data.npz").write_bytes(_npz_bytes(x=x, **y))
        (original / "README.txt").write_text(
            "synthetic immutable source dataset A\n", encoding="utf-8"
        )
        artifact_a = store.put_directory(original, kind="dataset")

    ledger.add_node(
        "dataset-A", "dataset", {"artifact_id": artifact_a.artifact_id}, artifact_a.artifact_id
    )
    worker = LocalPythonWorker(root)
    worker.start()
    first_pid = worker.process.pid if worker.process else None
    invalid_request_rejected = False
    try:
        worker.request({"op": "not-a-real-operation"})
    except RuntimeError:
        invalid_request_rejected = True

    # Both branches are ordinary Graphimo Python nodes. They receive only an artifact ID,
    # materialize A into an isolated directory and publish a new immutable artifact.
    transform_source = """
import io
import tempfile
from pathlib import Path
import numpy as np

with tempfile.TemporaryDirectory(prefix="graphimo-transform-") as temporary:
    destination = Path(temporary) / "branch"
    graphimo.materialize(dataset_id, destination)
    with np.load(destination / "data.npz") as archive:
        values = {name: archive[name] for name in archive.files}
    if branch_name == "B":
        values["x"] = ((values["x"] - values["x"].mean(axis=0)) /
                       (values["x"].std(axis=0) + 1e-6)).astype(np.float32)
    else:
        values["x"] = (values["x"] + np.random.default_rng(noise_seed).normal(
            0, 0.02, values["x"].shape)).astype(np.float32)
    np.savez_compressed(destination / "data.npz", **values)
    branch_artifact = graphimo.put_directory(destination, kind="dataset", parent_id=dataset_id)
branch_result = {"artifact_id": branch_artifact["artifact_id"], "branch": branch_name}
"""
    transform_version = _version(transform_source)
    branches: dict[str, str] = {}
    transform_runs: list[str] = []
    materialization: dict[str, dict[str, Any]] = {}
    for branch_name, seed in (("B", 8), ("C", 9)):
        node_id = f"dataset-{branch_name}"
        ledger.add_node(
            node_id,
            "python",
            {"source": transform_source, "branch": branch_name},
            transform_version,
        )
        ledger.add_edge("dataset-A", node_id, "dataset", "dataset_id")
        run = ledger.start_run(
            node_id, transform_version, "local-python", {"dataset_id": artifact_a.artifact_id}
        )
        result = worker.request(
            {
                "op": "execute_node",
                "source": transform_source,
                "inputs": {
                    "dataset_id": artifact_a.artifact_id,
                    "branch_name": branch_name,
                    "noise_seed": seed,
                },
                "outputs": ["branch_result"],
                "run_id": run.run_id,
                "node_id": node_id,
            },
            on_event=lambda event: ledger.emit(Event(**event)),
        )
        branch_ref = store.get(
            result["outputs"]["branch_result"]["artifact_id"], parent_id=artifact_a.artifact_id
        )
        branches[branch_name] = branch_ref.artifact_id
        materialization[branch_name] = branch_ref.manifest
        ledger.finish_run(run.run_id, {"dataset": branch_ref.artifact_id})
        transform_runs.append(run.run_id)

    spec = ModelSpec(
        input_size=4, hidden_size=16, head_sizes={"task_a": 1, "task_b": 1, "task_c": 1}
    )
    ledger.add_node("model", "model_spec", {"spec": spec.__dict__}, "model-v1")

    train_source = """
import resource
import time
import numpy as np
import torch
from graphimo_pytorch.prototype import ModelSpec, build_model, checkpoint_bytes, train_multihead

spec = ModelSpec(**model_spec)
with graphimo.open_artifact(dataset_id) as dataset:
    with np.load(dataset / "data.npz") as data:
        x = torch.from_numpy(data["x"].astype(np.float32))
        targets = {name: torch.from_numpy(data[name].astype(np.float32)) for name in spec.head_sizes}
model = build_model(spec)
def publish(kind, payload, step, component_path, durable):
    graphimo.event(kind, payload, step=step, component_path=component_path, durable=durable)
started = time.perf_counter()
metrics, optimizer = train_multihead(model, x, targets, epochs=8, emit=publish,
                                     poll_controls=graphimo.poll_controls)
graphimo.event("resource", {"elapsed_seconds": time.perf_counter() - started,
                            "peak_rss_platform_units": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)})
inspection = {"available": False}
try:
    from graphimo_pytorch.prototype import inspect_model
    inspection = {"available": True, **inspect_model(model.eval(), x, "backbone.1")}
except ModuleNotFoundError:
    pass
except Exception as exc:
    inspection = {"available": True, "error": str(exc)[:500]}
blob = checkpoint_bytes(model, optimizer, spec,
                        metadata={"train_run_id": run_id, "metrics": metrics})
checkpoint = graphimo.put_bytes(blob, kind="checkpoint", parent_id=dataset_id)
training_result = {"metrics": metrics, "checkpoint_id": checkpoint["artifact_id"],
                   "torchlens_inspection": inspection}
"""
    train_version = _version(train_source)
    ledger.add_node(
        "train",
        "training",
        {"source": train_source, "epochs": 8, "optimizer": "AdamW"},
        train_version,
    )
    ledger.add_edge("dataset-B", "train", "dataset", "dataset_id")
    ledger.add_edge("model", "train", "model_spec", "model_spec")
    train_run = ledger.start_run(
        "train", train_version, "local-python", {"train_data": branches["B"]}
    )
    training = worker.request(
        {
            "op": "execute_node",
            "source": train_source,
            "inputs": {
                "dataset_id": branches["B"],
                "model_spec": spec.__dict__,
                "run_id": train_run.run_id,
            },
            "outputs": ["training_result"],
            "run_id": train_run.run_id,
            "node_id": "train",
        },
        on_event=lambda event: ledger.emit(Event(**event)),
    )
    training_result = training["outputs"]["training_result"]
    metrics = training_result["metrics"]
    torchlens_inspection = training_result["torchlens_inspection"]
    checkpoint = store.get(training_result["checkpoint_id"])
    ledger.add_node(
        "checkpoint", "checkpoint", {"artifact_id": checkpoint.artifact_id}, checkpoint.artifact_id
    )
    ledger.add_edge("train", "checkpoint", "checkpoint", "checkpoint")
    ledger.finish_run(train_run.run_id, {"checkpoint": checkpoint.artifact_id})

    reopened_ledger = WorkspaceGraph(root / "workspace.sqlite3")
    restored_run = reopened_ledger.get_run(train_run.run_id)
    assert restored_run is not None and restored_run.status == "succeeded"
    reopened_store = LocalArtifactStore(root / "store")
    assert reopened_store.get(checkpoint.artifact_id).artifact_id == checkpoint.artifact_id
    _, _, saved_checkpoint = load_checkpoint(reopened_store.read_bytes(checkpoint))
    checkpoint_optimizer_saved = saved_checkpoint["optimizer_state"] is not None
    checkpoint_rng_saved = bool(saved_checkpoint.get("rng_state"))

    eval_source = """
import hashlib
import tempfile
import time
from pathlib import Path
import numpy as np
import torch
from graphimo_pytorch.prototype import load_checkpoint

model, _spec, checkpoint_data = load_checkpoint(graphimo.read_bytes(checkpoint_id))
with graphimo.open_artifact(dataset_id) as dataset:
    with np.load(dataset / "data.npz") as data:
        x = data["x"].astype(np.float32)
    started = time.perf_counter()
    predictions = model(torch.from_numpy(x))
    elapsed = time.perf_counter() - started
    pseudo = predictions[sorted(predictions)[0]].detach().cpu().numpy()
    with tempfile.TemporaryDirectory(prefix="graphimo-pseudo-") as temporary:
        output = Path(temporary) / "pseudo"
        output.mkdir()
        np.savez_compressed(output / "pseudo_labels.npz", x=x, pseudo=pseudo)
        artifact = graphimo.put_directory(output, kind="pseudo_labels", parent_id=dataset_id)
evaluation_result = {"artifact_id": artifact["artifact_id"], "elapsed_seconds": elapsed,
                     "samples": int(len(x)),
                     "prediction_sha256": hashlib.sha256(pseudo.tobytes()).hexdigest(),
                     "checkpoint_metadata": checkpoint_data["metadata"]}
"""
    eval_version = _version(eval_source)
    ledger.add_node("evaluate", "python", {"source": eval_source}, eval_version)
    ledger.add_edge("checkpoint", "evaluate", "checkpoint", "checkpoint_id")
    ledger.add_edge("dataset-B", "evaluate", "dataset", "dataset_id")
    eval_run = reopened_ledger.start_run(
        "evaluate",
        eval_version,
        "local-python",
        {"checkpoint": checkpoint.artifact_id, "dataset": branches["B"]},
        parent_run_id=train_run.run_id,
    )
    eval_request = {
        "op": "execute_node",
        "source": eval_source,
        "inputs": {"checkpoint_id": checkpoint.artifact_id, "dataset_id": branches["B"]},
        "outputs": ["evaluation_result"],
        "run_id": eval_run.run_id,
        "node_id": "evaluate",
    }
    eval_one = worker.request(
        eval_request, on_event=lambda event: reopened_ledger.emit(Event(**event))
    )
    same_worker_pid = worker.process.pid if worker.process else None
    worker.terminate()
    eval_two = worker.request(
        eval_request, on_event=lambda event: reopened_ledger.emit(Event(**event))
    )
    second_pid = worker.process.pid if worker.process else None
    evaluation = eval_two["outputs"]["evaluation_result"]
    pseudo = reopened_store.get(evaluation["artifact_id"], parent_id=branches["B"])
    reopened_ledger.add_node(
        "dataset-D", "pseudo_labels", {"artifact_id": pseudo.artifact_id}, pseudo.artifact_id
    )
    reopened_ledger.add_edge("evaluate", "dataset-D", "pseudo_labels", "dataset")
    reopened_ledger.emit(
        Event(
            eval_run.run_id,
            "evaluate",
            "checkpoint",
            time.time(),
            {
                "restored_artifact_id": checkpoint.artifact_id,
                "worker_pids": [first_pid, second_pid],
                "samples": evaluation["samples"],
            },
        )
    )
    reopened_ledger.finish_run(eval_run.run_id, {"pseudo_labels": pseudo.artifact_id})

    fork_spec = ModelSpec(input_size=4, hidden_size=18, head_sizes=dict(spec.head_sizes))
    fork_source = """
import numpy as np
import torch
from graphimo_pytorch.prototype import (ModelSpec, build_model, checkpoint_bytes, load_checkpoint,
                                   migrate_weights, train_multihead)

parent, _, _ = load_checkpoint(graphimo.read_bytes(parent_checkpoint_id))
spec = ModelSpec(**fork_spec)
model = build_model(spec)
state = model.state_dict()
migration = migrate_weights(parent.state_dict(), state)
model.load_state_dict(state)
with graphimo.open_artifact(dataset_id) as dataset:
    with np.load(dataset / "data.npz") as data:
        x = torch.from_numpy(data["x"].astype(np.float32))
        targets = {name: torch.from_numpy(data[name].astype(np.float32)) for name in spec.head_sizes}
def publish(kind, payload, step, component_path, durable):
    graphimo.event(kind, payload, step=step, component_path=component_path, durable=durable)
metrics, optimizer = train_multihead(model, x, targets, epochs=3, emit=publish)
blob = checkpoint_bytes(model, optimizer, spec,
                        metadata={"parent_checkpoint": parent_checkpoint_id, "migration": migration,
                                  "optimizer_state_migrated": False, "metrics": metrics})
checkpoint = graphimo.put_bytes(blob, kind="checkpoint", parent_id=parent_checkpoint_id)
fork_result = {"metrics": metrics, "migration": migration,
               "checkpoint_id": checkpoint["artifact_id"]}
"""
    fork_version = _version(fork_source)
    reopened_ledger.add_node(
        "model-fork", "model_spec", {"spec": fork_spec.__dict__, "parent": "model"}, "model-v2"
    )
    reopened_ledger.add_node(
        "train-fork", "training", {"source": fork_source, "epochs": 3}, fork_version
    )
    reopened_ledger.add_edge("checkpoint", "model-fork", "weights", "initial_weights")
    reopened_ledger.add_edge("model-fork", "train-fork", "model_spec", "model_spec")
    reopened_ledger.add_edge("dataset-C", "train-fork", "dataset", "dataset_id")
    fork_run = reopened_ledger.start_run(
        "train-fork",
        fork_version,
        "local-python",
        {"train_data": branches["C"], "parent_checkpoint": checkpoint.artifact_id},
        parent_run_id=train_run.run_id,
    )
    forked = worker.request(
        {
            "op": "execute_node",
            "source": fork_source,
            "inputs": {
                "dataset_id": branches["C"],
                "parent_checkpoint_id": checkpoint.artifact_id,
                "fork_spec": fork_spec.__dict__,
            },
            "outputs": ["fork_result"],
            "run_id": fork_run.run_id,
            "node_id": "train-fork",
        },
        on_event=lambda event: reopened_ledger.emit(Event(**event)),
    )
    fork_result = forked["outputs"]["fork_result"]
    fork_checkpoint = reopened_store.get(fork_result["checkpoint_id"])
    reopened_ledger.finish_run(fork_run.run_id, {"checkpoint": fork_checkpoint.artifact_id})
    worker.close()

    graph_before_reopen = reopened_ledger.graph_snapshot()
    final_ledger = WorkspaceGraph(root / "workspace.sqlite3")
    graph_after_reopen = final_ledger.graph_snapshot()
    run_ids = [train_run.run_id, eval_run.run_id, fork_run.run_id, *transform_runs]
    assert graph_before_reopen == graph_after_reopen
    assert all(final_ledger.get_run(run_id).status == "succeeded" for run_id in run_ids)
    return {
        "workspace": str(root),
        "artifact_ids": {
            "A": artifact_a.artifact_id,
            "B": branches["B"],
            "C": branches["C"],
            "checkpoint": checkpoint.artifact_id,
            "D": pseudo.artifact_id,
            "fork_checkpoint": fork_checkpoint.artifact_id,
        },
        "branch_materialization": materialization,
        "train_metrics": metrics,
        "fork_metrics": fork_result["metrics"],
        "migration": fork_result["migration"],
        "torchlens_inspection": torchlens_inspection,
        "checkpoint_optimizer_saved": checkpoint_optimizer_saved,
        "checkpoint_rng_saved": checkpoint_rng_saved,
        "graph_survived_close_reopen": True,
        "three_heads_trained": len(spec.head_sizes) == 3,
        "loss_improved": metrics["total"] < metrics["initial_total"],
        "worker_pids": [first_pid, second_pid],
        "worker_reused_after_invalid_request": invalid_request_rejected
        and first_pid == same_worker_pid,
        "worker_restart_predictions_equal": (
            eval_one["outputs"]["evaluation_result"]["prediction_sha256"]
            == evaluation["prediction_sha256"]
        ),
        "runs": run_ids,
        "event_count": sum(len(final_ledger.events(run_id)) for run_id in run_ids),
        "graph": graph_after_reopen,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run Graphimo's experimental vertical slice")
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    print(json.dumps(run_vertical_demo(args.workspace), indent=2, sort_keys=True))
