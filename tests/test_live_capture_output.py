"""Live E2E: capture a kernel output into the ArtifactStore through the service."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

pytest.importorskip("marimo")

from graphimo.client import GraphimoClient
from graphimo.service.server import GraphimoService

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
        raw = response.read().decode()
        output = []
        event = None
        for line in raw.splitlines():
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
            elif line.startswith("data:") and event == "stdout":
                output.append(json.loads(line.partition(":")[2].strip()).get("data", ""))
    text = "".join(output)
    marker = "GRAPHIMO_RESULT:"
    assert marker in text, f"stdout: {text}\nraw: {raw[:2000]}"
    return text.split(marker, 1)[1].splitlines()[0]


@pytest.fixture
def live_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from websockets.sync.client import connect

    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    notebook = tmp_path / "capture.py"
    notebook.write_text(
        "import marimo\n__generated_with = '0.25.2'\napp = marimo.App()\n"
        "if __name__ == '__main__': app.run()\n",
        encoding="utf-8",
    )
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
        with connect(f"ws://127.0.0.1:{port}/ws?session_id=capture") as websocket:
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


def test_live_capture_output_and_catalogue(live_kernel) -> None:
    project, base, session = live_kernel

    # A real notebook cell defines the variable in the kernel globals.
    cell = project.execute("notebook.add_cell", {"source": "values = list(range(1000))"})
    project.execute("notebook.run", {"cell_id": cell["cell_id"]})
    deadline = time.monotonic() + 20
    setup = None
    last_error = ""
    while time.monotonic() < deadline:
        try:
            setup = json.loads(
                _kernel_execute(
                    base,
                    session,
                    "import json\nprint('GRAPHIMO_RESULT:' + json.dumps({'n': len(values)}))",
                )
            )
            break
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.3)
    assert setup is not None and setup["n"] == 1000, last_error[-800:]

    captured = project.capture_output("values", format="json", pin=True, label="dataset-v1")
    assert captured["artifact_id"]
    assert captured["kind"] == "snapshot.json"
    assert captured["pinned"] is True

    listing = project.list_artifacts(kind="snapshot.json")
    assert len(listing["items"]) == 1
    entry = listing["items"][0]
    assert entry["artifact_id"] == captured["artifact_id"]
    assert entry["metadata"]["result_var"] == "values"
    assert entry["metadata"]["label"] == "dataset-v1"
    assert entry["pins"] == ["snapshot:values"]

    inspected = project.inspect_artifact(captured["artifact_id"])
    assert inspected["metadata"]["format"] == "json"
    assert inspected["size_bytes"] > 1000

    # A missing variable is a clean bounded error, not a crash.
    from graphimo.client import GraphimoServiceError

    with pytest.raises(GraphimoServiceError) as exc_info:
        project.capture_output("does_not_exist", format="json")
    assert exc_info.value.code == "capture_failed"

    # GC keeps the pinned snapshot and removes nothing else unexpected.
    stats_before = project.artifact_stats()["stats"]
    project.gc_artifacts()
    stats_after = project.artifact_stats()["stats"]
    assert stats_after["artifacts"] == stats_before["artifacts"]
