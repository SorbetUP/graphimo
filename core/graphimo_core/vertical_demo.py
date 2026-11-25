from __future__ import annotations

import io
import json
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from .artifacts import ArtifactRef, LocalArtifactStore
from .ledger import Event, WorkspaceGraph
from .pytorch import ModelSpec, load_checkpoint
from .runtime import LocalPythonWorker


def _npz_bytes(**arrays: Any) -> bytes:
    stream = io.BytesIO()
    np.savez_compressed(stream, **arrays)
    return stream.getvalue()


def run_vertical_demo(workspace: str | Path) -> dict[str, Any]:
    """Exercise data branching, multi-head training, durable restore and lineage."""
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
        (original / "README.txt").write_text("synthetic immutable source dataset A\n", encoding="utf-8")
        artifact_a = store.put_directory(original, kind="dataset")

        branches: dict[str, ArtifactRef] = {}
        materialization: dict[str, dict[str, Any]] = {}
        for branch_name in ("B", "C"):
            branch = Path(temporary) / branch_name
            materialization[branch_name] = store.materialize(artifact_a, branch)
            with np.load(branch / "data.npz") as contents:
                values = {key: contents[key] for key in contents.files}
            if branch_name == "B":
                values["x"] = ((values["x"] - values["x"].mean(axis=0)) /
                               (values["x"].std(axis=0) + 1e-6)).astype(np.float32)
            else:
                values["x"] = (values["x"] + rng.normal(0, 0.02, values["x"].shape)).astype(np.float32)
            (branch / "data.npz").write_bytes(_npz_bytes(**values))
            branches[branch_name] = store.put_directory(branch, kind="dataset", parent_id=artifact_a.artifact_id)

    spec = ModelSpec(input_size=4, hidden_size=16, head_sizes={"task_a": 1, "task_b": 1, "task_c": 1})
    ledger.add_node("dataset-A", "dataset", {"artifact_id": artifact_a.artifact_id}, artifact_a.artifact_id)
    for branch_name, ref in branches.items():
        node_id = f"dataset-{branch_name}"
        ledger.add_node(node_id, "dataset", {"artifact_id": ref.artifact_id}, ref.artifact_id)
        ledger.add_edge("dataset-A", node_id, "dataset", "dataset")
    ledger.add_node("model", "model_spec", {"spec": spec.__dict__}, "model-v1")
    ledger.add_node("train", "training", {"epochs": 8, "optimizer": "AdamW"}, "train-v1")
    ledger.add_edge("dataset-B", "train", "dataset", "train_data")
    ledger.add_edge("model", "train", "model_spec", "model_spec")

    train_run = ledger.start_run("train", "train-v1", "local-python", {"train_data": branches["B"].artifact_id})
    worker = LocalPythonWorker(root)
    worker.start()
    first_pid = worker.process.pid if worker.process else None
    invalid_request_rejected = False
    try:
        worker.request({"op": "not-a-real-operation"})
    except RuntimeError:
        invalid_request_rejected = True

    train_source = """
import io, resource, time
import numpy as np
import torch
from graphimo_core.pytorch import ModelSpec, build_model, checkpoint_bytes, train_multihead

spec = ModelSpec(**model_spec)
with np.load(io.BytesIO(graphimo.read_file(dataset_id, 'data.npz'))) as data:
    x = torch.from_numpy(data['x'].astype(np.float32))
    targets = {name: torch.from_numpy(data[name].astype(np.float32)) for name in spec.head_sizes}
model = build_model(spec)
def publish(kind, payload, step, component_path):
    graphimo.event(kind, payload, step=step, component_path=component_path)
started = time.perf_counter()
metrics, optimizer = train_multihead(model, x, targets, epochs=8, emit=publish)
graphimo.event('resource', {'elapsed_seconds': time.perf_counter() - started,
                            'peak_rss_platform_units': int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)})
blob = checkpoint_bytes(model, optimizer, spec,
                        metadata={'train_run_id': run_id, 'metrics': metrics})
checkpoint = graphimo.put_bytes(blob, kind='checkpoint', parent_id=dataset_id)
training_result = {'metrics': metrics, 'checkpoint_id': checkpoint['artifact_id']}
"""
    training = worker.request({
        "op": "execute_node", "source": train_source,
        "inputs": {"dataset_id": branches["B"].artifact_id,
                   "model_spec": spec.__dict__, "run_id": train_run.run_id},
        "outputs": ["training_result"], "run_id": train_run.run_id, "node_id": "train",
    }, on_event=lambda event: ledger.emit(Event(**event)))
    training_result = training["outputs"]["training_result"]
    metrics = training_result["metrics"]
    checkpoint = store.get(training_result["checkpoint_id"])
    ledger.add_node("checkpoint", "checkpoint", {"artifact_id": checkpoint.artifact_id}, checkpoint.artifact_id)
    ledger.add_edge("train", "checkpoint", "checkpoint", "checkpoint")
    ledger.finish_run(train_run.run_id, {"checkpoint": checkpoint.artifact_id})

    # Prove the workspace and artifacts survive closing the original objects.
    reopened_ledger = WorkspaceGraph(root / "workspace.sqlite3")
    restored_run = reopened_ledger.get_run(train_run.run_id)
    assert restored_run is not None and restored_run.status == "succeeded"
    reopened_store = LocalArtifactStore(root / "store")
    assert reopened_store.get(checkpoint.artifact_id).artifact_id == checkpoint.artifact_id
    _, _, saved_checkpoint = load_checkpoint(reopened_store.read_bytes(checkpoint))
    checkpoint_optimizer_saved = saved_checkpoint["optimizer_state"] is not None
    checkpoint_rng_saved = bool(saved_checkpoint.get("rng_state"))

    eval_run = reopened_ledger.start_run("evaluate", "eval-v1", "local-python",
                                         {"checkpoint": checkpoint.artifact_id,
                                          "dataset": branches["B"].artifact_id},
                                         parent_run_id=train_run.run_id)
    eval_one = worker.request({"op": "evaluate_checkpoint", "checkpoint_id": checkpoint.artifact_id,
                               "dataset_id": branches["B"].artifact_id})
    same_worker_pid = worker.process.pid if worker.process else None
    worker.terminate()  # model and process state are intentionally destroyed
    eval_two = worker.request({"op": "evaluate_checkpoint", "checkpoint_id": checkpoint.artifact_id,
                               "dataset_id": branches["B"].artifact_id})
    second_pid = worker.process.pid if worker.process else None
    pseudo = reopened_store.get(eval_two["artifact_id"], parent_id=branches["B"].artifact_id)
    ledger.add_node("evaluate", "evaluation", {"checkpoint": checkpoint.artifact_id}, "eval-v1")
    ledger.add_node("dataset-D", "pseudo_labels", {"artifact_id": pseudo.artifact_id}, pseudo.artifact_id)
    ledger.add_edge("checkpoint", "evaluate", "checkpoint", "checkpoint")
    ledger.add_edge("dataset-B", "evaluate", "dataset", "dataset")
    ledger.add_edge("evaluate", "dataset-D", "pseudo_labels", "dataset")
    ledger.emit(Event(eval_run.run_id, "evaluate", "checkpoint", time.time(),
                      {"restored_artifact_id": checkpoint.artifact_id,
                       "worker_pids": [first_pid, second_pid], "samples": eval_two["samples"]}))
    ledger.finish_run(eval_run.run_id, {"pseudo_labels": pseudo.artifact_id})

    # Fork and train a changed model spec in the restarted worker process.
    fork_spec = ModelSpec(input_size=4, hidden_size=18, head_sizes=dict(spec.head_sizes))
    ledger.add_node("model-fork", "model_spec", {"spec": fork_spec.__dict__, "parent": "model"}, "model-v2")
    ledger.add_edge("checkpoint", "model-fork", "weights", "initial_weights")

    fork_run = ledger.start_run("train-fork", "train-v2", "local-python",
                               {"train_data": branches["C"].artifact_id,
                                "parent_checkpoint": checkpoint.artifact_id},
                               parent_run_id=train_run.run_id)
    fork_source = """
import io
import numpy as np
import torch
from graphimo_core.pytorch import ModelSpec, build_model, checkpoint_bytes, load_checkpoint, migrate_weights, train_multihead

parent, _, _ = load_checkpoint(graphimo.read_bytes(parent_checkpoint_id))
spec = ModelSpec(**fork_spec)
model = build_model(spec)
state = model.state_dict()
migration = migrate_weights(parent.state_dict(), state)
model.load_state_dict(state)
with np.load(io.BytesIO(graphimo.read_file(dataset_id, 'data.npz'))) as data:
    x = torch.from_numpy(data['x'].astype(np.float32))
    targets = {name: torch.from_numpy(data[name].astype(np.float32)) for name in spec.head_sizes}
def publish(kind, payload, step, component_path):
    graphimo.event(kind, payload, step=step, component_path=component_path)
metrics, optimizer = train_multihead(model, x, targets, epochs=3, emit=publish)
blob = checkpoint_bytes(model, optimizer, spec,
                        metadata={'parent_checkpoint': parent_checkpoint_id, 'migration': migration,
                                  'optimizer_state_migrated': False, 'metrics': metrics})
checkpoint = graphimo.put_bytes(blob, kind='checkpoint', parent_id=parent_checkpoint_id)
fork_result = {'metrics': metrics, 'migration': migration,
               'checkpoint_id': checkpoint['artifact_id']}
"""
    forked = worker.request({
        "op": "execute_node", "source": fork_source,
        "inputs": {"dataset_id": branches["C"].artifact_id,
                   "parent_checkpoint_id": checkpoint.artifact_id,
                   "fork_spec": fork_spec.__dict__},
        "outputs": ["fork_result"], "run_id": fork_run.run_id, "node_id": "train-fork",
    }, on_event=lambda event: ledger.emit(Event(**event)))
    fork_result = forked["outputs"]["fork_result"]
    fork_metrics = fork_result["metrics"]
    migration = fork_result["migration"]
    fork_checkpoint = store.get(fork_result["checkpoint_id"])
    ledger.add_node("train-fork", "training", {"epochs": 3, "spec": fork_spec.__dict__}, "train-v2")
    ledger.add_edge("model-fork", "train-fork", "model_spec", "model_spec")
    ledger.add_edge("dataset-C", "train-fork", "dataset", "train_data")
    ledger.finish_run(fork_run.run_id, {"checkpoint": fork_checkpoint.artifact_id})
    worker.close()

    graph_before_reopen = ledger.graph_snapshot()
    ledger = None
    final_ledger = WorkspaceGraph(root / "workspace.sqlite3")
    graph_after_reopen = final_ledger.graph_snapshot()
    assert graph_before_reopen == graph_after_reopen
    assert all(final_ledger.get_run(run_id).status == "succeeded" for run_id in
               (train_run.run_id, eval_run.run_id, fork_run.run_id))
    return {
        "workspace": str(root),
        "artifact_ids": {"A": artifact_a.artifact_id, "B": branches["B"].artifact_id,
                         "C": branches["C"].artifact_id, "checkpoint": checkpoint.artifact_id,
                         "D": pseudo.artifact_id, "fork_checkpoint": fork_checkpoint.artifact_id},
        "branch_materialization": materialization,
        "train_metrics": metrics,
        "fork_metrics": fork_metrics,
        "migration": migration,
        "checkpoint_optimizer_saved": checkpoint_optimizer_saved,
        "checkpoint_rng_saved": checkpoint_rng_saved,
        "graph_survived_close_reopen": True,
        "three_heads_trained": len(spec.head_sizes) == 3,
        "loss_improved": metrics["total"] < metrics["initial_total"],
        "worker_pids": [first_pid, second_pid],
        "worker_reused_after_invalid_request": invalid_request_rejected and first_pid == same_worker_pid,
        "worker_restart_predictions_equal": eval_one["prediction_sha256"] == eval_two["prediction_sha256"],
        "runs": [train_run.run_id, eval_run.run_id, fork_run.run_id],
        "event_count": sum(len(final_ledger.events(run_id)) for run_id in
                            (train_run.run_id, eval_run.run_id, fork_run.run_id)),
        "graph": graph_after_reopen,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run Graphimo's experimental vertical slice")
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    print(json.dumps(run_vertical_demo(args.workspace), indent=2, sort_keys=True))
