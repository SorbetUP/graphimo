"""Live E2E: checkpoint save -> drift -> restore through the Graphimo service.

The restore goes through the same ``metablock.invoke`` path the Zeron plugin
and agents use, executes inside the real marimo kernel, and returns only
JSON-safe metadata (never the nn.Module).
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from graphimo.client import GraphimoClient
from graphimo.service.server import GraphimoService

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("marimo") is None or importlib.util.find_spec("torch") is None,
    reason="marimo and torch are required for the live PyTorch restore path",
)

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _kernel_execute(url: str, session: str, code: str) -> str:
    request = Request(
        url + "/api/kernel/execute",
        data=json.dumps({"code": code}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Marimo-Session-Id": session,
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        output = []
        diagnostics = []
        event = None
        for line in response.read().decode().splitlines():
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
            elif line.startswith("data:") and event == "stdout":
                output.append(json.loads(line.partition(":")[2].strip()).get("data", ""))
            elif line.startswith("data:") and event in {"stderr", "error"}:
                diagnostics.append(json.loads(line.partition(":")[2].strip()).get("data", ""))
    text = "".join(output)
    marker = "GRAPHIMO_RESULT:"
    assert marker in text, f"stdout: {text}\nstderr/errors: {' '.join(diagnostics)}"
    return text.split(marker, 1)[1].splitlines()[0]


@pytest.fixture
def live_torch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from websockets.sync.client import connect

    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    notebook = tmp_path / "torch-restore.py"
    notebook.write_text(
        "import marimo\n__generated_with = '0.25.2'\napp = marimo.App()\n"
        "if __name__ == '__main__': app.run()\n",
        encoding="utf-8",
    )
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    pythonpath = [str(ROOT), str(ROOT / "addons" / "pytorch")]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    log_path = tmp_path / "marimo.log"
    process = None
    service = GraphimoService(manifest_path=tmp_path / "service.json").start()
    try:
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "marimo",
                    "edit",
                    str(notebook),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--headless",
                    "--no-token",
                    "--no-sandbox",
                    "--skip-update-check",
                ],
                cwd=tmp_path,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 25
        session = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log_path.flush()
                raise AssertionError(log_path.read_text(encoding="utf-8"))
            try:
                with urlopen(base + "/api/sessions", timeout=1):
                    break
            except OSError:
                time.sleep(0.1)
        with connect(f"ws://127.0.0.1:{port}/ws?session_id=torch-restore") as websocket:
            while time.monotonic() < deadline:
                message = json.loads(websocket.recv(timeout=max(0.1, deadline - time.monotonic())))
                if message.get("op") == "kernel-ready":
                    break
            else:
                raise AssertionError("marimo kernel did not become ready")
            while time.monotonic() < deadline:
                try:
                    with urlopen(base + "/api/sessions", timeout=1) as response:
                        sessions = json.loads(response.read().decode())
                    if sessions:
                        session = next(iter(sessions))
                        break
                except Exception:
                    time.sleep(0.1)
            assert session, log_path.read_text(encoding="utf-8")

        client = GraphimoClient(service.url, service.token)
        project = client.open_project(notebook)
        client.attach_kernel(project, url=base, token="", session_id=session)
        yield project, base, session, tmp_path
        project.close()
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        service.close()


def _run_cell(project, source: str) -> dict[str, str]:
    """Add and run one notebook cell through the service; return output previews."""
    cell = project.execute("notebook.add_cell", {"source": source})
    cell_id = cell["cell_id"]
    project.execute("notebook.run", {"cell_id": cell_id})
    snapshot = project.execute("notebook.inspect", {})
    for item in snapshot["cells"]:
        if item["id"] == cell_id:
            if item.get("errors"):
                raise AssertionError(f"cell failed: {item['errors']}")
            return {out["name"]: out.get("preview", "") for out in item.get("outputs", [])}
    raise AssertionError("cell missing from inspect")


def _number(outputs: dict[str, str], name: str) -> float:
    return float(outputs[name])


def _text(outputs: dict[str, str], name: str) -> str:
    value = outputs[name]
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1]
    return value


def test_live_checkpoint_restore_via_service(live_torch) -> None:
    project, base, session, tmp_path = live_torch

    project.execute(
        "metablock.create", {"dialect_id": "pytorch.model", "metablock_id": "restore_model"}
    )
    projection = project.execute(
        "metablock.project", {"metablock_id": "restore_model", "scope": "root"}
    )
    # Materialize through the service: the definition cell is inserted AND run
    # in the live kernel by the service itself (atomic materialization).
    materialized = project.execute(
        "metablock.materialize", {"metablock_id": "restore_model", "bindings": {}}
    )
    assert materialized["materialization_state"] == "current"
    assert isinstance(materialized["cell_id"], str) and materialized["cell_id"]
    assert materialized["revision"] == projection["revision"]

    # Training cell: ordinary notebook cell using the materialized definition,
    # exactly like a user pipeline. Instantiates a live runtime, trains one
    # step with optimizer+scheduler, and saves a checkpoint.
    train = _run_cell(
        project,
        "import torch\n"
        "torch.manual_seed(7)\n"
        "restore_model = g_pytorch_restore_model.instantiate()\n"
        "restore_optimizer = torch.optim.SGD(restore_model.parameters(), lr=0.1, momentum=0.9)\n"
        "restore_scheduler = torch.optim.lr_scheduler.StepLR(restore_optimizer, step_size=1)\n"
        "next(restore_model.parameters()).sum().backward()\n"
        "restore_optimizer.step()\n"
        "restore_scheduler.step()\n"
        "restore_model.__graphimo_pytorch_training_state__ = {'optimizer': restore_optimizer, 'scheduler': restore_scheduler}\n"
        "from graphimo_pytorch.checkpoint import save_checkpoint\n"
        "restore_reference = save_checkpoint(restore_model, optimizer=restore_optimizer, scheduler=restore_scheduler, metadata={'training_steps': 1})\n"
        "restore_runtime = restore_model.__graphimo_pytorch_runtime__.runtime_instance_id\n"
        "restore_artifact = restore_reference.id\n"
        "restore_post_train = float(next(restore_model.parameters()).sum())\n"
        "restore_in_features = next(m for m in restore_model.modules() if isinstance(m, torch.nn.Linear)).in_features\n",
    )
    runtime_id = _text(train, "restore_runtime")
    artifact_id = _text(train, "restore_artifact")
    post_train = _number(train, "restore_post_train")
    in_features = int(_number(train, "restore_in_features"))

    # Drift the weights in the kernel so the restore is observable.
    drift = _run_cell(
        project,
        "with torch.no_grad():\n"
        "    for p in restore_model.parameters(): p.add_(3.0)\n"
        "restore_drifted = float(next(restore_model.parameters()).sum())\n",
    )
    drifted = _number(drift, "restore_drifted")

    # Restore through the service into the live runtime, including the
    # optimizer/scheduler training state.
    restored = project.execute(
        "metablock.invoke",
        {
            "metablock_id": "restore_model",
            "action": "checkpoint.restore",
            "args": {
                "artifact_id": artifact_id,
                "runtime_instance_id": runtime_id,
                "restore_training_state": True,
            },
        },
    )
    assert restored["runtime_instance_id"] == runtime_id
    assert restored["artifact_id"] == artifact_id
    assert restored["revision_match"] is True
    assert restored["migrated"] is False
    assert restored["optimizer_state_restored"] is True
    assert restored["scheduler_state_restored"] is True
    assert restored["training_steps"] == 1
    json.dumps(restored, allow_nan=False)
    assert "model" not in restored

    after = _run_cell(
        project,
        "restore_after = float(next(restore_model.parameters()).sum())\n"
        "restore_lr = restore_optimizer.param_groups[0]['lr']\n"
        "restore_epoch = restore_scheduler.state_dict().get('last_epoch')\n",
    )
    # Weights went back to the exact checkpointed values (drift removed).
    assert _number(after, "restore_after") != drifted
    assert _number(after, "restore_after") == pytest.approx(post_train)
    # StepLR(gamma=0.1) after one step: lr 0.1 -> 0.01.
    assert _number(after, "restore_lr") == pytest.approx(0.01)
    assert _number(after, "restore_epoch") == 1

    # A fresh runtime can also be restored from the same checkpoint (new id).
    fresh = project.execute(
        "metablock.invoke",
        {
            "metablock_id": "restore_model",
            "action": "checkpoint.restore",
            "args": {"artifact_id": artifact_id},
        },
    )
    assert fresh["runtime_instance_id"] != runtime_id
    assert fresh["revision_match"] is True
    listing = project.execute(
        "metablock.invoke",
        {"metablock_id": "restore_model", "action": "runtime.list", "args": {}},
    )
    listed_ids = {item["runtime_instance_id"] for item in listing["items"]}
    assert {runtime_id, fresh["runtime_instance_id"]} <= listed_ids

    # TorchLens inspection works on the restored runtime through the service.
    inspection = project.execute(
        "metablock.invoke",
        {
            "metablock_id": "restore_model",
            "action": "torchlens.inspect",
            "args": {
                "runtime_instance_id": fresh["runtime_instance_id"],
                "logical_element_id": "backbone",
                "inputs": {"$inputs": [[[1.0] * in_features]]},
            },
        },
    )
    assert inspection["runtime_instance_id"] == fresh["runtime_instance_id"]

    # Revision conflict is explicit: an older expected revision is refused.
    from graphimo.client import GraphimoServiceError

    with pytest.raises(GraphimoServiceError) as exc_info:
        project.execute(
            "metablock.apply",
            {
                "metablock_id": "restore_model",
                "intent": {"type": "set_parameter", "name": "name", "value": "Renamed"},
                "expected_revision": "stale-revision",
            },
        )
    assert exc_info.value.code == "revision_conflict"
