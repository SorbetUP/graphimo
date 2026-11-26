"""Live marimo-kernel coverage for the formal @node port projection.

These tests start a real marimo 0.25.2 kernel and drive it through the same
Graphimo service the Zeron plugin and agents use, so the projection, the
multi-output wiring, the component collision handling and the canonical
refresh are all verified against the execution engine that owns them.
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
from urllib.request import urlopen

import pytest

from graphimo.client import GraphimoClient, GraphimoServiceError
from graphimo.service.server import GraphimoService

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("marimo") is None, reason="marimo is not installed"
)

ROOT = Path(__file__).resolve().parents[1]

NORMALIZE_SOURCE = (
    "from graphimo import node\n\n"
    "@node\n"
    "def normalize(value: int, scale: int = 2) -> int:\n"
    "    return value * scale\n"
)
NORMALIZE_SOURCE_V2 = NORMALIZE_SOURCE.replace(
    "return value * scale", "return value * (scale + 10)"
)
SPLIT_SOURCE = (
    "from graphimo import node\n\n"
    "@node\n"
    "def split(value: int) -> tuple[int, int, int]:\n"
    "    return value, value + 1, value * 2\n"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_output(project, cell_id: str, name: str, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    snapshot = None
    while time.monotonic() < deadline:
        snapshot = project.inspect_notebook()
        cell = next((item for item in snapshot["cells"] if item["id"] == cell_id), None)
        if cell is not None:
            output = next((item for item in cell.get("outputs", []) if item["name"] == name), None)
            if output is not None:
                return output, snapshot
        time.sleep(0.1)
    raise AssertionError(
        "timed out waiting for output "
        + json.dumps(
            [
                {
                    "id": cell.get("id"),
                    "source": cell.get("source", "")[:200],
                    "errors": cell.get("errors"),
                    "status": cell.get("status"),
                    "outputs": cell.get("outputs"),
                }
                for cell in (snapshot or {}).get("cells", [])
            ],
            ensure_ascii=False,
        )
    )


@pytest.fixture
def live_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from websockets.sync.client import connect

    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    notebook = tmp_path / "ports-live.py"
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
        with connect(f"ws://127.0.0.1:{port}/ws?session_id=graphimo-ports-live") as websocket:
            while time.monotonic() < deadline:
                message = json.loads(websocket.recv(timeout=max(0.1, deadline - time.monotonic())))
                if message.get("op") == "kernel-ready":
                    break
            else:
                raise AssertionError("marimo kernel did not become ready")
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log_path.flush()
                    raise AssertionError(log_path.read_text(encoding="utf-8"))
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
        yield project
        project.close()
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        service.close()


def test_live_kernel_projects_formal_ports_collisions_and_refresh(live_kernel) -> None:
    project = live_kernel

    data_cell = project.execute("notebook.add_cell", {"source": "data = 21"})
    assert project.run(data_cell["cell_id"])["status"] == "queued"
    data_output, _ = _wait_for_output(project, data_cell["cell_id"], "data")
    assert data_output["preview"] == "21"

    # Two components exporting the same callable name must not collide in the
    # marimo globals: each library cell binds a path-derived symbol.
    for path in ("images.py", "tables.py"):
        assert (
            project.execute("component.write", {"path": path, "source": NORMALIZE_SOURCE})["valid"]
            is True
        )
    from_images = project.add_component(
        "images.py",
        "normalize",
        output_name="from_images",
        bindings={"value": {"variable": "data"}},
    )
    from_tables = project.add_component(
        "tables.py", "normalize", output_name="from_tables", bindings={"value": {"literal": 1}}
    )
    assert from_images["runtime_symbol"] != from_tables["runtime_symbol"]
    assert from_images["library_cell_id"] != from_tables["library_cell_id"]

    snapshot = project.inspect_notebook()
    cells = {cell["id"]: cell for cell in snapshot["cells"]}
    assert "graphimo_instance" not in cells[data_cell["cell_id"]]
    for built, expected_path in ((from_images, "images.py"), (from_tables, "tables.py")):
        cell = cells[built["cell_id"]]
        # Canonical component identity is projected by the real service inspect.
        assert cell["graphimo_component"]["component_id"] == expected_path
        assert cell["graphimo_component"]["callable"] == "normalize"
        assert cell["graphimo_component"]["source_sha256"] is not None
        instance = cell["graphimo_instance"]
        assert instance["component_id"] == expected_path
        assert instance["runtime_symbol"] == built["runtime_symbol"]
        inputs = {item["name"]: item for item in instance["inputs"]}
        assert inputs["value"]["required"] is True
        assert inputs["scale"]["has_default"] is True
        # The optional input stays a formal port even while on its default.
        assert inputs["scale"]["binding"]["kind"] == "default"
        assert instance["outputs"][0]["index"] == 0
    assert cells[from_images["cell_id"]]["graphimo_instance"]["inputs"][0]["binding"] == {
        "kind": "variable",
        "expression": "data",
    }
    assert cells[from_tables["cell_id"]]["graphimo_instance"]["inputs"][0]["binding"]["kind"] == (
        "literal"
    )

    # No implicit bindings: adding a component with an unbound required input
    # never invents `value=value`-style kwargs, and run refuses cleanly.
    sink = project.add_component("images.py", "normalize", output_name="scaled", bindings={})
    assert "value=" not in sink["source"]
    with pytest.raises(GraphimoServiceError, match="Required node input") as missing:
        project.run(sink["cell_id"])
    assert getattr(missing.value, "code", None) == "missing_required_binding"

    # Multi-output node: each output is its own connectable port.
    assert (
        project.execute("component.write", {"path": "split.py", "source": SPLIT_SOURCE})["valid"]
        is True
    )
    split_cell = project.add_component(
        "split.py", "split", output_name="parts", bindings={"value": {"variable": "data"}}
    )
    assert project.run(split_cell["cell_id"])["status"] == "queued"
    split_output, _ = _wait_for_output(project, split_cell["cell_id"], split_cell["result_var"])
    assert split_output["runtime_type"] == "tuple"
    split_instance = next(
        cell for cell in project.inspect_notebook()["cells"] if cell["id"] == split_cell["cell_id"]
    )["graphimo_instance"]
    assert [output["index"] for output in split_instance["outputs"]] == [0, 1, 2]

    connected = project.connect(
        split_cell["cell_id"], split_cell["result_var"], sink["cell_id"], "value", output_index=2
    )
    assert f"value={split_cell['result_var']}[2]," in connected["source"]
    with pytest.raises(GraphimoServiceError, match="output_index_required"):
        project.connect(split_cell["cell_id"], split_cell["result_var"], sink["cell_id"], "scale")
    snapshot = project.inspect_notebook()
    edge = next(
        item
        for item in snapshot["edges"]
        if item["source_cell_id"] == split_cell["cell_id"]
        and item["target_cell_id"] == sink["cell_id"]
    )
    assert edge["output_index"] == 2
    assert edge["input_ref"] == "value"

    assert project.run(sink["cell_id"])["status"] == "queued"
    sink_output, _ = _wait_for_output(project, sink["cell_id"], sink["result_var"])
    # split(21)[2] == 42, default scale 2 -> 84
    assert sink_output["preview"] == "84"

    # An optional input can be connected later; single-output sources need no
    # explicit output_index.
    reconnected = project.connect(data_cell["cell_id"], "data", sink["cell_id"], "scale")
    assert "scale=data," in reconnected["source"]
    assert project.run(sink["cell_id"])["status"] == "queued"
    sink_output, _ = _wait_for_output(project, sink["cell_id"], sink["result_var"])
    assert sink_output["preview"] == str(42 * 21)

    # Editing the component file and refreshing must reload the callable used
    # by existing instances, without adding a new instance.
    library_cell_id = from_images["library_cell_id"]
    before = next(
        cell for cell in project.inspect_notebook()["cells"] if cell["id"] == library_cell_id
    )
    updated = project.execute(
        "component.write", {"path": "images.py", "source": NORMALIZE_SOURCE_V2}
    )
    assert updated["valid"] is True
    refresh = project.execute("component.refresh_instances", {})
    assert refresh["ok"] is True
    assert library_cell_id in refresh["changed_cells"]
    after = next(
        cell for cell in project.inspect_notebook()["cells"] if cell["id"] == library_cell_id
    )
    assert after["source"] != before["source"]
    assert updated["sha256"] in after["source"]

    assert project.run(sink["cell_id"])["status"] == "queued"
    sink_output, snapshot = _wait_for_output(project, sink["cell_id"], sink["result_var"])
    # New implementation: value * (scale + 10) with value=42, scale=21 -> 1302
    assert sink_output["preview"] == "1302"

    # The whole notebook stays healthy: no marimo multiple-definition or
    # execution errors anywhere after all of the above.
    assert all(not cell["errors"] for cell in snapshot["cells"]), [
        {"id": cell["id"], "errors": cell["errors"]} for cell in snapshot["cells"] if cell["errors"]
    ]
