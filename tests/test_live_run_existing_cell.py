"""Live regression: notebook.run must work on a file cell that has never run.

A freshly loaded marimo kernel has ctx.cells populated but ctx.graph empty
until the first execution; graph.ancestors() then raises KeyError. The run
action must guard against that instead of failing with kernel_action_failed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

pytest.importorskip("marimo")

from graphimo.client import GraphimoClient
from graphimo.service.server import GraphimoService

ROOT = Path(__file__).resolve().parents[1]

NOTEBOOK = """import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.cell
def load():
    data = [1.0, 2.0, 3.0]
    data
    return (data,)


@app.cell
def scale(data):
    out = [x * 10 for x in data]
    out
    return (out,)


if __name__ == "__main__":
    app.run()
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from websockets.sync.client import connect

    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    notebook = tmp_path / "pipeline.py"
    notebook.write_text(NOTEBOOK, encoding="utf-8")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    pythonpath = [str(ROOT)]
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
        with connect(f"ws://127.0.0.1:{port}/ws?session_id=run-existing") as websocket:
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
        yield project, base, session
        project.close()
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        service.close()


def test_live_run_existing_file_cell(live_kernel) -> None:
    project, _, _ = live_kernel
    snapshot = project.execute("notebook.inspect", {})
    cells = snapshot["cells"]
    assert len(cells) == 2
    assert all(cell["status"] == "stale" for cell in cells)

    first, second = cells[0]["id"], cells[1]["id"]
    run_first = project.execute("notebook.run", {"cell_id": first})
    assert run_first["ok"] is True, run_first
    run_second = project.execute("notebook.run", {"cell_id": second})
    assert run_second["ok"] is True, run_second

    after = project.execute("notebook.inspect", {})
    statuses = {cell["id"]: cell["status"] for cell in after["cells"]}
    assert statuses[first] == "idle"
    assert statuses[second] == "idle"
    outputs = {cell["id"]: cell["outputs"] for cell in after["cells"]}
    assert any(out["name"] == "out" for out in outputs[second])
    assert after["edges"], "expected dependency edges after running both cells"

    from graphimo.client import GraphimoServiceError

    with pytest.raises(GraphimoServiceError) as exc_info:
        project.execute("notebook.run", {"cell_id": "no-such-cell"})
    assert exc_info.value.code == "cell_not_found"
