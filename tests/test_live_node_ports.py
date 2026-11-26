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


def test_live_kernel_edge_projection_mutation_guards_and_migration(live_kernel) -> None:
    project = live_kernel
    root = Path(project.path).resolve().parent

    data_cell = project.execute("notebook.add_cell", {"source": "data = 21"})
    assert project.run(data_cell["cell_id"])["status"] == "queued"
    _wait_for_output(project, data_cell["cell_id"], "data")

    for path, source in (("split.py", SPLIT_SOURCE), ("norm.py", NORMALIZE_SOURCE)):
        assert project.execute("component.write", {"path": path, "source": source})["valid"] is True

    split_cell = project.add_component(
        "split.py", "split", output_name="parts", bindings={"value": {"variable": "data"}}
    )
    assert project.run(split_cell["cell_id"])["status"] == "queued"
    _wait_for_output(project, split_cell["cell_id"], split_cell["result_var"])

    sink = project.add_component("norm.py", "normalize", output_name="scaled", bindings={})

    # P0.4: a literal can be assigned to an input that was never bound.
    literal = project.set_literal(sink["instance_id"], "value", 10)
    assert "value=__import__('json').loads('10')," in literal["source"]
    assert project.run(sink["cell_id"])["status"] == "queued"
    sink_output, _ = _wait_for_output(project, sink["cell_id"], sink["result_var"])
    assert sink_output["preview"] == "20"  # 10 * default scale 2

    # P0.1/P0.2: several bindings may consume the same source variable, and a
    # plain marimo cell still resolves the formal input port of the target.
    first = project.connect(
        split_cell["cell_id"], split_cell["result_var"], sink["cell_id"], "value", output_index=0
    )
    assert f"value={split_cell['result_var']}[0]," in first["source"]
    second = project.connect(
        split_cell["cell_id"], split_cell["result_var"], sink["cell_id"], "scale", output_index=2
    )
    assert f"scale={split_cell['result_var']}[2]," in second["source"]
    plain = project.add_component("norm.py", "normalize", output_name="plain_sink", bindings={})
    project.connect(data_cell["cell_id"], "data", plain["cell_id"], "value")

    snapshot = project.inspect_notebook()
    edges = [
        edge
        for edge in snapshot["edges"]
        if edge["source_cell_id"] == split_cell["cell_id"]
        and edge["target_cell_id"] == sink["cell_id"]
    ]
    assert len(edges) == 2, edges
    assert {(edge.get("input_ref"), edge.get("output_index")) for edge in edges} == {
        ("value", 0),
        ("scale", 2),
    }
    plain_edge = next(
        edge
        for edge in snapshot["edges"]
        if edge["source_cell_id"] == data_cell["cell_id"]
        and edge["target_cell_id"] == plain["cell_id"]
    )
    assert plain_edge.get("input_ref") == "value"
    assert "output_index" not in plain_edge

    assert project.run(sink["cell_id"])["status"] == "queued"
    sink_output, _ = _wait_for_output(project, sink["cell_id"], sink["result_var"])
    # split(21) = (21, 22, 42): value=21, scale=42 -> 882
    assert sink_output["preview"] == "882"

    # P0.7: fresh results are available; nothing stale is ever "available".
    snapshot = project.inspect_notebook()
    sink_view = next(cell for cell in snapshot["cells"] if cell["id"] == sink["cell_id"])[
        "graphimo_instance"
    ]
    assert sink_view["state"] == "ok"
    assert sink_view["outputs"][0]["available"] is True
    assert sink_view["outputs"][0]["stale"] is False

    # P0.3: disabling the source keeps the port identity on the stale edges.
    project.disable_instance(split_cell["cell_id"])
    snapshot = project.inspect_notebook()
    split_view = next(cell for cell in snapshot["cells"] if cell["id"] == split_cell["cell_id"])[
        "graphimo_instance"
    ]
    assert split_view["disabled"] is True
    assert split_view["state"] == "disabled"
    assert split_view["outputs"][0]["available"] is False
    stale_edges = [
        edge
        for edge in snapshot["edges"]
        if edge["source_cell_id"] == split_cell["cell_id"]
        and edge["target_cell_id"] == sink["cell_id"]
    ]
    assert len(stale_edges) == 2
    assert all(edge.get("status") == "stale" for edge in stale_edges)
    assert {(edge.get("input_ref"), edge.get("output_index")) for edge in stale_edges} == {
        ("value", 0),
        ("scale", 2),
    }
    sink_view = next(cell for cell in snapshot["cells"] if cell["id"] == sink["cell_id"])[
        "graphimo_instance"
    ]
    assert sink_view["state"] == "blocked"
    assert sink_view["outputs"][0]["available"] is False

    # P0.9: connecting back would create a cycle and must be refused without
    # changing the target source.
    project.enable_instance(split_cell["cell_id"])
    before = next(
        cell for cell in project.inspect_notebook()["cells"] if cell["id"] == split_cell["cell_id"]
    )["source"]
    with pytest.raises(GraphimoServiceError, match="cycle_detected"):
        project.connect(
            sink["cell_id"], sink["result_var"], split_cell["cell_id"], "value", output_index=0
        )
    after = next(
        cell for cell in project.inspect_notebook()["cells"] if cell["id"] == split_cell["cell_id"]
    )["source"]
    assert after == before

    # P1.1: formal ports come from the static contract even before any run.
    assert (
        project.execute(
            "component.write",
            {"path": "fresh.py", "source": SPLIT_SOURCE.replace("split", "fresh")},
        )["valid"]
        is True
    )
    built = project.execute(
        "component.instance_source",
        {
            "path": "fresh.py",
            "callable": "fresh",
            "output_name": "fresh_out",
            "bindings": {"value": {"variable": "data"}},
        },
    )
    unrun_cell = project.execute("notebook.add_cell", {"source": built["source"]})
    snapshot = project.inspect_notebook()
    unrun = next(cell for cell in snapshot["cells"] if cell["id"] == unrun_cell["cell_id"])
    assert unrun["graphimo_instance"]["inputs"][0]["name"] == "value"
    assert [output["index"] for output in unrun["graphimo_instance"]["outputs"]] == [0, 1, 2]
    assert unrun["graphimo_instance"]["state"] == "idle"

    # P0.5: a pre-69658b4 notebook (plain symbol, no component marker) is
    # migrated explicitly by the refresh and keeps running afterwards.
    legacy_source = NORMALIZE_SOURCE.replace("def normalize", "def legacy_norm")
    assert (
        project.execute("component.write", {"path": "legacy.py", "source": legacy_source})["valid"]
        is True
    )
    legacy_library = "\n".join(
        [
            "# Graphimo node library: legacy.py:legacy_norm",
            "import sys as _graphimo_sys",
            f"_graphimo_root = {str(root)!r}",
            "if _graphimo_root not in _graphimo_sys.path:",
            "    _graphimo_sys.path.insert(0, _graphimo_root)",
            "import importlib as _graphimo_importlib",
            "_graphimo_module = _graphimo_importlib.reload(_graphimo_importlib.import_module('components.legacy'))",
            "legacy_norm = _graphimo_module.legacy_norm",
            "",
        ]
    )
    legacy_instance = (
        "# Graphimo node: legacy_norm\n"
        "# Graphimo instance: 1e9a0001\n"
        "g_legacy_norm_1e9a0001 = legacy_norm(\n"
        "    value=data,\n"
        ")"
    )
    lib_cell = project.execute("notebook.add_cell", {"source": legacy_library})
    inst_cell = project.execute("notebook.add_cell", {"source": legacy_instance})
    assert project.run(lib_cell["cell_id"])["status"] == "queued"
    assert project.run(inst_cell["cell_id"])["status"] == "queued"
    legacy_output, _ = _wait_for_output(project, inst_cell["cell_id"], "g_legacy_norm_1e9a0001")
    assert legacy_output["preview"] == "42"  # 21 * 2

    refresh = project.execute("component.refresh_instances", {})
    assert refresh["ok"] is True
    assert lib_cell["cell_id"] in refresh["changed_cells"]
    assert inst_cell["cell_id"] in refresh["migrated_instances"]
    assert refresh["conflicts"] == []
    snapshot = project.inspect_notebook()
    migrated = next(cell for cell in snapshot["cells"] if cell["id"] == inst_cell["cell_id"])
    assert migrated["graphimo_instance"]["component_id"] == "legacy.py"
    assert migrated["graphimo_component"]["callable"] == "legacy_norm"
    assert "# Graphimo component: legacy.py" in migrated["source"]
    assert migrated["graphimo_instance"]["runtime_symbol"] in migrated["source"]
    assert project.run(inst_cell["cell_id"])["status"] == "queued"
    legacy_output, _ = _wait_for_output(project, inst_cell["cell_id"], "g_legacy_norm_1e9a0001")
    assert legacy_output["preview"] == "42"

    # P0.6: a deleted component is explicitly unavailable; its instances are
    # blocked, keep their source, and run refuses with component_unavailable.
    assert (
        project.execute("component.write", {"path": "doomed.py", "source": NORMALIZE_SOURCE})[
            "valid"
        ]
        is True
    )
    doomed = project.add_component(
        "doomed.py", "normalize", output_name="doomed_out", bindings={"value": {"literal": 5}}
    )
    assert project.run(doomed["cell_id"])["status"] == "queued"
    _wait_for_output(project, doomed["cell_id"], doomed["result_var"])
    assert project.execute("component.delete", {"path": "doomed.py"})["deleted"] is True
    project.execute("component.refresh_instances", {})
    snapshot = project.inspect_notebook()
    doomed_view = next(cell for cell in snapshot["cells"] if cell["id"] == doomed["cell_id"])
    assert doomed_view["graphimo_instance"]["state"] == "component_unavailable"
    assert doomed_view["graphimo_component"]["available"] is False
    assert "# Graphimo component: doomed.py" in doomed_view["source"]
    with pytest.raises(GraphimoServiceError, match="no longer exists") as unavailable:
        project.run(doomed["cell_id"])
    assert getattr(unavailable.value, "code", None) == "component_unavailable"
