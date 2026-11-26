from __future__ import annotations

import time
import json
import os
import socket
import subprocess
import sys
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest

from addons.pytorch.graphimo_pytorch.publisher import KernelEventPublisher
from addons.pytorch.graphimo_pytorch.runtime import PyTorchEventBus
from graphimo.client import GraphimoClient, GraphimoServiceError
from graphimo.service.server import GraphimoService
from graphimo.verifier import VerifiedProject, VerifierScenario


def _event_bus(max_events: int = 32) -> PyTorchEventBus:
    return PyTorchEventBus(max_events=max_events)


def _emit(bus: PyTorchEventBus, suffix: str) -> None:
    bus.emit(
        kind="activation.summary",
        info=SimpleNamespace(
            metablock_id="model-kernel",
            graph_revision="revision-1",
            runtime_instance_id="runtime-1",
        ),
        logical_element_id="encoder",
        module_path="encoder.0",
        step=4,
        payload={"suffix": suffix},
    )


def _wait_for_events(client: GraphimoClient, project_id: str, predicate, timeout: float = 3):
    deadline = time.monotonic() + timeout
    cursor = "0"
    received = []
    while time.monotonic() < deadline:
        result = client.events(project_id, cursor=cursor, limit=128, wait_ms=100)
        cursor = result["cursor"]
        received.extend(result["events"])
        if predicate(received):
            return received
    raise AssertionError(f"timed out waiting for events; received {received!r}")


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


def test_publisher_batches_live_kernel_events_into_service_spool(tmp_path: Path) -> None:
    notebook = tmp_path / "live.py"
    notebook.write_text("# live kernel\n", encoding="utf-8")
    service = GraphimoService(manifest_path=tmp_path / "service.json").start()
    client = GraphimoClient(service.url, service.token)
    project = client.open_project(notebook)
    bus = _event_bus()
    publisher = KernelEventPublisher(
        bus,
        service_url=service.url,
        token=service.token,
        project_id=project.project_id,
        session_id="marimo-session-a",
        interval=0.01,
    ).start()
    try:
        _emit(bus, "one")
        events = _wait_for_events(client, project.project_id, lambda values: bool(values))
        pushed = next(item["addon_event"] for item in events if item["kind"] == "addon.event")
        assert pushed["project_id"] == project.project_id
        assert pushed["session_id"] == "marimo-session-a"
        assert pushed["runtime_instance_id"] == "runtime-1"
        assert pushed["payload"] == {"suffix": "one"}
        assert "model" not in pushed and "module" not in pushed
        deadline = time.monotonic() + 1
        while publisher.stats()["events_sent"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.stats()["events_sent"] >= 1
    finally:
        publisher.close()
        project.close()
        client.close()
        service.close()


def test_publisher_counts_queue_overflow_and_reconnects_after_service_restart(
    tmp_path: Path,
) -> None:
    notebook = tmp_path / "restart.py"
    notebook.write_text("# restartable kernel\n", encoding="utf-8")
    bus = _event_bus(max_events=1)
    _emit(bus, "dropped-1")
    _emit(bus, "dropped-2")
    _emit(bus, "kept")

    first = GraphimoService(manifest_path=tmp_path / "first.json").start()
    first_client = GraphimoClient(first.url, first.token)
    project = first_client.open_project(notebook)
    publisher = KernelEventPublisher(
        bus,
        service_url=first.url,
        token=first.token,
        project_id=project.project_id,
        session_id="marimo-session-b",
        interval=0.01,
    ).start()
    try:
        first_events = _wait_for_events(
            first_client,
            project.project_id,
            lambda values: any(
                item.get("addon_event", {}).get("kind") == "telemetry.dropped" for item in values
            ),
        )
        drop = next(
            item["addon_event"]
            for item in first_events
            if item.get("addon_event", {}).get("kind") == "telemetry.dropped"
        )
        assert drop["count"] == 2
        assert publisher.stats()["events_lost"] == 2
        first.close()
        _emit(bus, "during-outage")  # producer never performs network I/O
        deadline = time.monotonic() + 2
        while publisher.stats()["failures"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.stats()["failures"] > 0

        second = GraphimoService(manifest_path=tmp_path / "second.json").start()
        second_client = GraphimoClient(second.url, second.token)
        second_project = second_client.open_project(notebook)
        assert second_project.project_id == project.project_id
        publisher.reconfigure(
            service_url=second.url,
            token=second.token,
            project_id=second_project.project_id,
            session_id="marimo-session-b",
        )
        recovered = _wait_for_events(
            second_client,
            project.project_id,
            lambda values: any(
                item.get("addon_event", {}).get("payload", {}).get("suffix") == "during-outage"
                for item in values
            ),
        )
        assert any(
            item.get("addon_event", {}).get("payload", {}).get("suffix") == "during-outage"
            for item in recovered
        )
        deadline = time.monotonic() + 1
        while publisher.stats()["reconnects"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.stats()["reconnects"] >= 1
        publisher.close()
        second_project.close()
        second_client.close()
        second.close()
    finally:
        publisher.close()
        first_client.close()
        first.close()


def test_publisher_queue_saturation_counts_drops_without_blocking_event_producers() -> None:
    bus = _event_bus(max_events=32)
    publisher = KernelEventPublisher(
        bus,
        service_url="http://127.0.0.1:1",
        token="offline-token",
        project_id="project-offline",
        session_id="kernel-offline",
        interval=0.01,
        request_timeout=0.05,
        pending_capacity=1,
    ).start()
    try:
        for index in range(12):
            _emit(bus, str(index))
        deadline = time.monotonic() + 2
        while publisher.stats()["events_lost"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.stats()["events_lost"] > 0

        # The producer path remains a bounded in-memory append, even at full
        # publisher capacity and with the service unreachable.
        start = time.monotonic()
        _emit(bus, "training-still-runs")
        assert time.monotonic() - start < 0.1
    finally:
        publisher.close()


@pytest.mark.skipif(importlib.util.find_spec("marimo") is None, reason="marimo is not installed")
def test_real_marimo_kernel_owns_runtime_hooks_and_reconnects_without_blocking_training(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from websockets.sync.client import connect

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    notebook = tmp_path / "kernel-live.py"
    notebook.write_text(
        "import marimo\n__generated_with = '0.25.2'\napp = marimo.App()\n"
        "if __name__ == '__main__': app.run()\n",
        encoding="utf-8",
    )
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    pythonpath = [str(root), str(root / "addons" / "pytorch")]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    log_path = tmp_path / "marimo.log"
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
        first = GraphimoService(manifest_path=tmp_path / "service-one.json").start()
        first_client = GraphimoClient(first.url, first.token)
        project = first_client.open_project(notebook)
        second = None
        second_client = None
        try:
            deadline = time.monotonic() + 25
            session = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.flush()
                    raise AssertionError(log_path.read_text(encoding="utf-8"))
                try:
                    with urlopen(base + "/api/sessions", timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            with connect(
                f"ws://127.0.0.1:{port}/ws?session_id=graphimo-kernel-bridge"
            ) as websocket:
                while time.monotonic() < deadline:
                    message = json.loads(
                        websocket.recv(timeout=max(0.1, deadline - time.monotonic()))
                    )
                    if message.get("op") == "kernel-ready":
                        break
                else:
                    raise AssertionError("marimo kernel did not become ready")
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        log.flush()
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

            project.execute(
                "metablock.create",
                {"dialect_id": "pytorch.model", "metablock_id": "kernel-model"},
            )
            first_client.attach_kernel(project, url=base, token="", session_id=session)
            live_verifier = VerifiedProject(project)

            data_cell = project.execute("notebook.add_cell", {"source": "data = 3"})
            component_source = (
                "from graphimo import node\n\n"
                "@node\n"
                "def normalize(value: int, scale: int = 2) -> int:\n"
                "    return value * scale\n"
            )
            assert (
                project.execute(
                    "component.write", {"path": "normalize.py", "source": component_source}
                )["valid"]
                is True
            )
            component_cell = project.add_component(
                "normalize.py",
                "normalize",
                output_name="normalized",
                bindings={"value": {"variable": "data"}},
            )
            assert "normalize(" in component_cell["source"]
            assert "value=data," in component_cell["source"]
            target_cell = project.add_component(
                "normalize.py",
                "normalize",
                output_name="reconnected",
                bindings={"value": {"literal": 1}, "scale": {"literal": 4}},
            )
            literal_update = project.set_literal(target_cell["instance_id"], "value", 2)
            assert "__import__('json').loads('2')" in literal_update["source"]
            assert "scale=__import__('json').loads('4')," in literal_update["source"]
            control_scenario = VerifierScenario("control_add", 2301)
            control_scenario.add(
                "control.add",
                {
                    "kind": "slider",
                    "identity": "gain",
                    "parameters": {"start": 0.0, "stop": 4.0, "step": 0.5, "value": 1.0},
                    "policy": "live",
                },
                step_id="add_gain",
            )
            control_journal = live_verifier.run_scenario(
                control_scenario,
                journal_path=tmp_path / "scenario-run-control.json",
            )
            gain = control_journal["operations"][0]["result"]
            assert "debounce=False" in gain["control_source"]
            snapshot = project.inspect_notebook()
            library_projection = next(
                cell
                for cell in snapshot["cells"]
                if "# Graphimo node library: normalize.py:normalize" in cell["source"]
            )
            assert not library_projection["errors"], library_projection
            assert any(cell["id"] == data_cell["cell_id"] for cell in snapshot["cells"])
            assert any(cell["id"] == component_cell["cell_id"] for cell in snapshot["cells"])
            assert any(cell["id"] == target_cell["cell_id"] for cell in snapshot["cells"])
            component_projection = next(
                cell for cell in snapshot["cells"] if cell["id"] == component_cell["cell_id"]
            )
            assert component_projection["graphimo_instance"]["node"] == "normalize"
            assert (
                component_projection["graphimo_instance"]["result_var"]
                == component_cell["result_var"]
            )
            assert component_projection["graphimo_instance"]["outputs"][0]["declared_type"] == "int"
            assert any(
                edge["source_cell_id"] == data_cell["cell_id"]
                and edge["target_cell_id"] == component_cell["cell_id"]
                and edge["variable"] == "data"
                for edge in snapshot["edges"]
            )
            connected = project.connect(
                data_cell["cell_id"], "data", target_cell["cell_id"], "value"
            )
            assert connected["changed"] is True
            assert "normalize(" in connected["source"]
            assert "value=data," in connected["source"]
            connected_control = project.connect(
                gain["value_cell_id"],
                gain["value_name"],
                target_cell["cell_id"],
                "scale",
            )
            assert f"scale={gain['value_name']}," in connected_control["source"]
            disconnected = project.disconnect_instance(target_cell["cell_id"], "value")
            assert disconnected["changed"] is True
            with pytest.raises(
                GraphimoServiceError, match="Required node input"
            ) as missing_binding:
                project.run(target_cell["cell_id"])
            assert getattr(missing_binding.value, "code", None) == "missing_required_binding"
            connected = project.connect(
                data_cell["cell_id"], "data", target_cell["cell_id"], "value"
            )
            assert "value=data," in connected["source"]
            reconnected = project.inspect_notebook()
            reconnected_target = next(
                cell for cell in reconnected["cells"] if cell["id"] == target_cell["cell_id"]
            )
            assert "data" in reconnected_target["refs"]
            assert "__import__('json').loads('1')" not in reconnected_target["source"]
            chained_cell = project.add_component(
                "normalize.py",
                "normalize",
                output_name="chained",
                bindings={"value": {"literal": 0}},
            )
            chained = project.connect(
                target_cell["cell_id"],
                target_cell["result_var"],
                chained_cell["cell_id"],
                "value",
                output_index=0,
            )
            assert target_cell["result_var"] in chained["source"]
            unsafe_target = project.execute(
                "notebook.add_cell", {"source": "unsafe_result = int(data)"}
            )
            with pytest.raises(GraphimoServiceError, match="not a Graphimo-owned node instance"):
                project.connect(data_cell["cell_id"], "data", unsafe_target["cell_id"], "data")
            assert project.run(chained_cell["cell_id"])["status"] == "queued"
            deadline = time.monotonic() + 4
            result_preview = None
            target_projection = None
            while time.monotonic() < deadline and result_preview is None:
                after_run = project.inspect_notebook()
                target_projection = next(
                    cell for cell in after_run["cells"] if cell["id"] == target_cell["cell_id"]
                )
                result_preview = next(
                    (
                        item
                        for item in target_projection["outputs"]
                        if item["name"] == target_cell["result_var"]
                    ),
                    None,
                )
                if result_preview is None:
                    time.sleep(0.1)
            assert result_preview is not None, json.dumps(
                [
                    {
                        "id": cell.get("id"),
                        "source": cell.get("source", "")[:300],
                        "errors": cell.get("errors"),
                        "status": cell.get("status"),
                        "output": cell.get("output"),
                    }
                    for cell in after_run["cells"]
                ],
                ensure_ascii=False,
            )
            assert result_preview["runtime_type"] == "float"
            assert result_preview["preview"] == "3.0"
            assert target_projection["graphimo_instance"]["outputs"][0]["declared_type"] == "int"
            assert live_verifier.assert_graph_consistent()["consistent"] is True
            assert (
                live_verifier.assert_output(
                    target_cell["cell_id"], target_cell["result_var"], "3.0"
                )["preview"]
                == "3.0"
            )
            assert live_verifier.assert_no_errors()["errors"] == 0

            scenario = VerifierScenario(
                "live_marimo_scalar", 48192, invariants=("graph_consistent", "no_errors")
            )
            scenario.add(
                "notebook.add_cell",
                {"source": "verifier_answer = 6 * 7"},
                step_id="add_answer",
            ).add(
                "notebook.run",
                {"cell_id": {"$ref": "add_answer.result.cell_id"}},
                step_id="run_answer",
                assertions=[
                    {
                        "kind": "output_preview",
                        "cell_id": {"$ref": "add_answer.result.cell_id"},
                        "output_name": "verifier_answer",
                        "equals": "42",
                        "wait_seconds": 4,
                    }
                ],
            )
            scenario_file = tmp_path / "scenarios" / "live-marimo.json"
            scenario.save(scenario_file)
            loaded_scenario = VerifierScenario.load(scenario_file)
            scenario_journal_path = tmp_path / "scenario-run.json"
            scenario_journal = loaded_scenario.replay(
                live_verifier, journal_path=scenario_journal_path
            )
            assert scenario_journal["completed"] is True
            assert [step["status"] for step in scenario_journal["operations"]] == ["ok", "ok"]
            assert scenario_journal["operations"][1]["assertions"][0]["result"]["preview"] == "42"
            assert json.loads(scenario_journal_path.read_text(encoding="utf-8"))["seed"] == 48192
            chained_projection = next(
                cell for cell in after_run["cells"] if cell["id"] == chained_cell["cell_id"]
            )
            chained_preview = next(
                item
                for item in chained_projection["outputs"]
                if item["name"] == chained_cell["result_var"]
            )
            assert chained_preview["preview"] == "6.0"
            updated_component_source = component_source.replace(
                "value * scale", "value * (scale + 1)"
            )
            updated_write = project.execute(
                "component.write",
                {"path": "normalize.py", "source": updated_component_source},
            )
            assert updated_write["valid"]
            updated_instance = project.add_component(
                "normalize.py",
                "normalize",
                output_name="updated",
                bindings={"value": {"literal": 2}},
            )
            assert updated_instance["library_cell_id"] == component_cell["library_cell_id"]
            updated_snapshot = project.inspect_notebook()
            updated_library = next(
                cell
                for cell in updated_snapshot["cells"]
                if cell["id"] == component_cell["library_cell_id"]
            )
            assert updated_write["sha256"] in updated_library["source"]
            from graphimo.instance import build_instance_source

            disabled_source, disabled_spec = build_instance_source(
                "disabled_source", {}, instance_id="deadbeef"
            )
            project.execute(
                "notebook.add_cell",
                {"source": "def disabled_source():\n    return 4"},
            )
            disabled_cell = project.execute("notebook.add_cell", {"source": disabled_source})
            blocked_cell = project.execute(
                "notebook.add_cell",
                {"source": f"blocked_value = {disabled_spec.result_var}"},
            )
            dependency_scenario = VerifierScenario(
                "disable_blocked_reenable", 73021, invariants=("graph_consistent",)
            )
            dependency_scenario.add(
                "notebook.disable_instance",
                {"cell_id": disabled_cell["cell_id"]},
                step_id="disable_source",
                assertions=[
                    {
                        "kind": "blocked_by",
                        "cell_id": blocked_cell["cell_id"],
                        "dependency_id": disabled_cell["cell_id"],
                    }
                ],
            ).add(
                "notebook.run",
                {"cell_id": blocked_cell["cell_id"]},
                step_id="run_blocked",
                expect_error="blocked_by_disabled_dependency",
            ).add(
                "notebook.enable_instance",
                {"cell_id": disabled_cell["cell_id"]},
                step_id="enable_source",
                assertions=[
                    {
                        "kind": "source_equals",
                        "cell_id": disabled_cell["cell_id"],
                        "equals": disabled_source,
                    }
                ],
            ).add(
                "notebook.run",
                {"cell_id": blocked_cell["cell_id"]},
                step_id="run_unblocked",
            )
            dependency_journal = dependency_scenario.replay(
                live_verifier,
                journal_path=tmp_path / "scenario-run-disabled.json",
            )
            assert [item["status"] for item in dependency_journal["operations"]] == [
                "ok",
                "expected_error",
                "ok",
                "ok",
            ]
            assert dependency_journal["operations"][0]["assertions"][0]["result"]["blocked_by"] == [
                disabled_cell["cell_id"]
            ]
            unblocked_snapshot = project.inspect_notebook()
            assert not unblocked_snapshot["blocked_by"].get(blocked_cell["cell_id"])
            assert any(
                edge["source_cell_id"] == disabled_cell["cell_id"]
                and edge["target_cell_id"] == blocked_cell["cell_id"]
                for edge in unblocked_snapshot["edges"]
            )

            history_scenario = VerifierScenario(
                "guarded_history_edit", 919, invariants=("graph_consistent",)
            )
            history_scenario.add(
                "notebook.restore_cells",
                {"records": [{"source": "history_value = 10"}]},
                step_id="restore_history",
            ).add(
                "notebook.edit_guarded",
                {
                    "cell_id": {"$ref": "restore_history.result.cell_ids.0"},
                    "expected_source": "history_value = 10",
                    "source": "history_value = 11",
                },
                step_id="edit_history",
                assertions=[
                    {
                        "kind": "source_equals",
                        "cell_id": {"$ref": "restore_history.result.cell_ids.0"},
                        "equals": "history_value = 11",
                    }
                ],
            )
            history_journal = live_verifier.run_scenario(
                history_scenario, journal_path=tmp_path / "scenario-run-history.json"
            )
            restored_id = history_journal["operations"][0]["result"]["cell_ids"][0]
            assert (
                history_journal["operations"][1]["assertions"][0]["result"]["cell_id"]
                == restored_id
            )
            with pytest.raises(GraphimoServiceError, match="source changed since"):
                project.execute(
                    "notebook.edit_guarded",
                    {
                        "cell_id": restored_id,
                        "expected_source": "history_value = 10",
                        "source": "history_value = 12",
                    },
                )

            group_input = project.execute("notebook.add_cell", {"source": "group_input_value = 4"})
            group_output = project.execute(
                "notebook.add_cell", {"source": "group_output_value = group_input_value * 2"}
            )
            group_snapshot = project.inspect_notebook()
            group_scenario = VerifierScenario("create_and_replace_group", 1947)
            group_scenario.add(
                "group.create_from_selection",
                {
                    "name": "Service Group",
                    "module": "service_group",
                    "cells": group_snapshot["cells"],
                    "selected_ids": [group_input["cell_id"], group_output["cell_id"]],
                },
                step_id="create_group",
            ).add(
                "notebook.replace_group_selection",
                {
                    "selected_ids": {"$ref": "create_group.result.selected_ids"},
                    "source_hashes": {"$ref": "create_group.result.source_hashes"},
                    "parent_source": {"$ref": "create_group.result.parent_source"},
                },
                step_id="replace_selection",
            )
            group_journal = live_verifier.run_scenario(
                group_scenario, journal_path=tmp_path / "scenario-run-group.json"
            )
            group = group_journal["operations"][0]["result"]
            replacement = group_journal["operations"][1]["result"]
            assert replacement["deleted_cell_ids"] == group["selected_ids"]
            assert replacement["cell_id"] not in {
                group_input["cell_id"],
                group_output["cell_id"],
            }

            config = {
                "service_url": first.url,
                "service_token": first.token,
                "project_id": project.project_id,
                "session_id": session,
            }
            code = """
import builtins, json, torch
from graphimo_pytorch.model_definition import RuntimeBindingMap, RuntimeModelInfo, RuntimeSite
from graphimo_pytorch.runtime import RUNTIME_REGISTRY
from graphimo_pytorch.publisher import configure_kernel_publisher
config = CONFIG
configure_kernel_publisher(service_url=config['service_url'], token=config['service_token'], project_id=config['project_id'], session_id=config['session_id'])
torch.manual_seed(12007)
model = torch.nn.Sequential(torch.nn.Linear(1, 1))
info = RuntimeModelInfo('kernel-runtime', 'kernel-model', 'revision-kernel', 'materialization-kernel', RuntimeBindingMap((RuntimeSite('linear', '0'),)))
RUNTIME_REGISTRY.register(model, info)
builtins._graphimo_test_model = model
print('GRAPHIMO_RESULT:' + json.dumps({'ok': True, 'runtime_instance_id': 'kernel-runtime'}))
""".replace("CONFIG", repr(config))
            first_result = json.loads(_kernel_execute(base, session, code))
            assert first_result["ok"] is True
            runtime_id = first_result["runtime_instance_id"]
            probe_scenario = VerifierScenario("pytorch_runtime_probe", 12007)
            probe_scenario.add(
                "metablock.invoke",
                {"metablock_id": "kernel-model", "action": "runtime.list", "args": {}},
                step_id="list_runtime",
            ).add(
                "metablock.invoke",
                {
                    "metablock_id": "kernel-model",
                    "action": "runtime.subscribe",
                    "args": {
                        "runtime_instance_id": {
                            "$ref": "list_runtime.result.items.0.runtime_instance_id"
                        },
                        "logical_element_id": "linear",
                        "signal": "activation",
                        "rate_hz": 1000,
                    },
                },
                step_id="subscribe_activation",
            ).add(
                "metablock.invoke",
                {
                    "metablock_id": "kernel-model",
                    "action": "runtime.subscribe",
                    "args": {
                        "runtime_instance_id": {
                            "$ref": "list_runtime.result.items.0.runtime_instance_id"
                        },
                        "logical_element_id": "linear",
                        "signal": "gradient",
                        "rate_hz": 1000,
                    },
                },
                step_id="subscribe_gradient",
            )
            probe_journal = live_verifier.run_scenario(
                probe_scenario, journal_path=tmp_path / "scenario-run-pytorch-probe.json"
            )
            listed = probe_journal["operations"][0]["result"]
            assert listed["items"][0]["runtime_instance_id"] == runtime_id
            subscription = probe_journal["operations"][1]["result"]
            gradient = probe_journal["operations"][2]["result"]
            assert subscription["subscription_id"] and gradient["subscription_id"]
            telemetry_cursor = project.events(cursor="0", wait_ms=0)["cursor"]
            run_code = "import builtins, json, torch\nvalue = builtins._graphimo_test_model(torch.ones(2, 1, requires_grad=True)); value.sum().backward()\nprint('GRAPHIMO_RESULT:' + json.dumps({'ok': True}))"
            assert json.loads(_kernel_execute(base, session, run_code))["ok"] is True
            telemetry_scenario = VerifierScenario("pytorch_telemetry", 12007, invariants=())
            telemetry_scenario.add(
                "metablock.invoke",
                {"metablock_id": "kernel-model", "action": "runtime.list", "args": {}},
                step_id="verify_telemetry",
                assertions=[
                    {
                        "kind": "event_received",
                        "event_kind": "activation.summary",
                        "metablock_id": "kernel-model",
                        "cursor": telemetry_cursor,
                        "wait_seconds": 5,
                    },
                    {
                        "kind": "event_received",
                        "event_kind": "gradient.summary",
                        "metablock_id": "kernel-model",
                        "cursor": telemetry_cursor,
                        "wait_seconds": 5,
                    },
                ],
            )
            telemetry_journal = live_verifier.run_scenario(
                telemetry_scenario, journal_path=tmp_path / "scenario-run-pytorch-telemetry.json"
            )
            assert [
                item["result"]["event_kind"]
                for item in telemetry_journal["operations"][0]["assertions"]
            ] == ["activation.summary", "gradient.summary"]

            checkpoint_code = """
import builtins, json
from graphimo_pytorch.checkpoint import save_checkpoint
reference = save_checkpoint(builtins._graphimo_test_model, metadata={"seed": 12007})
print('GRAPHIMO_RESULT:' + json.dumps({"artifact_id": reference.id}))
"""
            checkpoint_id = json.loads(_kernel_execute(base, session, checkpoint_code))[
                "artifact_id"
            ]
            checkpoint_scenario = VerifierScenario("pytorch_checkpoint", 12007, invariants=())
            checkpoint_scenario.add(
                "metablock.invoke",
                {
                    "metablock_id": "kernel-model",
                    "action": "checkpoint.inspect",
                    "args": {"artifact_id": checkpoint_id},
                },
                step_id="inspect_checkpoint",
            ).add(
                "artifact.inspect",
                {"artifact_id": {"$ref": "inspect_checkpoint.result.artifact_id"}},
                step_id="inspect_checkpoint_artifact",
            )
            checkpoint_journal = live_verifier.run_scenario(
                checkpoint_scenario, journal_path=tmp_path / "scenario-run-pytorch-checkpoint.json"
            )
            checkpoint_info = checkpoint_journal["operations"][0]["result"]
            assert checkpoint_info["metadata"] == {"seed": 12007}
            assert checkpoint_info["artifact_id"] == checkpoint_id
            assert checkpoint_journal["operations"][1]["result"]["kind"] == "pytorch-checkpoint"

            per_metablock = first_client.execute(
                project,
                "metablock.events",
                {"metablock_id": "kernel-model", "cursor": "0", "limit": 128},
            )
            assert {item["kind"] for item in per_metablock["events"]} >= {
                "activation.summary",
                "gradient.summary",
            }

            before_service_restart = project.inspect_notebook()
            first.close()
            stopped = _kernel_execute(
                base,
                session,
                "import json\nfrom graphimo_pytorch.publisher import kernel_publisher_stats\nprint('GRAPHIMO_RESULT:' + json.dumps(kernel_publisher_stats()))",
            )
            assert json.loads(stopped)["running"] is False
            after_stop = "import builtins, json, torch\nvalue = builtins._graphimo_test_model(torch.ones(2, 1, requires_grad=True)); value.sum().backward()\nprint('GRAPHIMO_RESULT:' + json.dumps({'ok': True}))"
            continued = _kernel_execute(
                base,
                session,
                after_stop,
            )
            assert json.loads(continued)["ok"] is True

            second = GraphimoService(manifest_path=tmp_path / "service-two.json").start()
            second_client = GraphimoClient(second.url, second.token)
            reopened = second_client.open_project(notebook)
            assert reopened.project_id == project.project_id
            second_client.attach_kernel(reopened, url=base, token="", session_id=session)
            reopened_verifier = VerifiedProject(reopened)
            after_service_restart = reopened_verifier.inspect_notebook()
            assert reopened_verifier.assert_graph_consistent()["consistent"] is True
            before_cells = {cell["id"]: cell["source"] for cell in before_service_restart["cells"]}
            after_cells = {cell["id"]: cell["source"] for cell in after_service_restart["cells"]}
            assert after_cells == before_cells
            before_edges = {
                (edge["source_cell_id"], edge["target_cell_id"], edge["variable"])
                for edge in before_service_restart["edges"]
            }
            after_edges = {
                (edge["source_cell_id"], edge["target_cell_id"], edge["variable"])
                for edge in after_service_restart["edges"]
            }
            assert after_edges == before_edges
            reconf = {
                "service_url": second.url,
                "service_token": second.token,
                "project_id": reopened.project_id,
                "session_id": session,
            }
            configure_again = """
import builtins, json, torch
from graphimo_pytorch.publisher import configure_kernel_publisher
config = CONFIG
status = configure_kernel_publisher(service_url=config['service_url'], token=config['service_token'], project_id=config['project_id'], session_id=config['session_id'])
value = builtins._graphimo_test_model(torch.ones(2, 1, requires_grad=True)); value.sum().backward()
print('GRAPHIMO_RESULT:' + json.dumps({'ok': True, 'running': status['running']}))
""".replace("CONFIG", repr(reconf))
            assert json.loads(_kernel_execute(base, session, configure_again))["running"] is True
            recovered = _wait_for_events(
                second_client,
                reopened.project_id,
                lambda values: any(item.get("source") == "marimo_kernel" for item in values),
            )
            assert any(item.get("source") == "marimo_kernel" for item in recovered)
            reopened.close()
            second_client.close()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            first_client.close()
            first.close()
            if second_client is not None:
                second_client.close()
            if second is not None:
                second.close()
