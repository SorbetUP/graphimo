"""Independent tests for the authenticated local Graphimo service."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from graphimo.client import GraphimoClient, GraphimoServiceError
from graphimo.service.events import EventJournal
from graphimo.service.protocol import MAX_REQUEST_BYTES, execute_batch, parse_json_object
from graphimo.service.server import GraphimoService
from graphimo.service.sessions import ProjectSessionManager


def test_event_journal_bounds_history_and_reports_cursor_drops() -> None:
    journal = EventJournal(capacity=2)
    journal.publish({"kind": "one"})
    journal.publish({"kind": "two", "sequence": 900})
    journal.publish({"kind": "three"})

    result = journal.read("0", limit=1)
    assert result == {
        "cursor": "2",
        "events": [{"kind": "two", "sequence": 2}],
        "dropped": 1,
    }


def test_protocol_rejects_duplicate_json_keys_and_unbounded_events() -> None:
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_json_object(b'{"action":"one","action":"two"}')
    with pytest.raises(ValueError, match="valid JSON"):
        parse_json_object(b'{"value":NaN}')
    journal = EventJournal()
    with pytest.raises(ValueError, match="bounded JSON"):
        journal.publish({"value": object()})


def test_batch_executes_in_order_resolves_refs_and_stops_after_failure() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def execute(action: str, args: dict[str, object]) -> object:
        calls.append((action, args))
        if action == "fail":
            raise ValueError("intent rejected")
        if action == "first":
            return {"name": "demo"}
        return args

    result = execute_batch(
        execute,
        [
            {"id": "first", "action": "first"},
            {"id": "second", "action": "second", "args": {"name": {"$ref": "first.result.name"}}},
            {"id": "bad", "action": "fail"},
            {"id": "later", "action": "second"},
        ],
    )
    assert calls == [("first", {}), ("second", {"name": "demo"}), ("fail", {})]
    assert [item["status"] for item in result["operations"]] == [
        "ok",
        "ok",
        "error",
        "not_executed",
    ]
    assert result["completed"] is False
    assert result["stopped_at"] == "bad"


def test_service_client_projects_batches_and_cursor_events(tmp_path: Path) -> None:
    project_path = tmp_path / "notebook.py"
    project_path.write_text("# Graphimo local service fixture\n", encoding="utf-8")
    manifest_path = tmp_path / "state" / "service.json"
    service = GraphimoService(manifest_path=manifest_path).start()
    try:
        assert manifest_path.stat().st_mode & 0o077 == 0
        client = GraphimoClient.connect_local(manifest_path=manifest_path, auto_start=False)
        assert client.health()["protocol"] == 1
        project = client.open_project(project_path)
        reused = client.open_project(project_path)
        assert reused.project_id == project.project_id
        assert reused.path == str(project_path.resolve())

        scope = project.execute("scope.current")
        assert scope["scope"]["kind"] == "project"
        batch = project.batch(
            [
                {"id": "current", "action": "scope.current"},
                {
                    "id": "layout",
                    "action": "layout.get",
                    "args": {"scope_id": {"$ref": "current.result.scope.scope_id"}},
                },
            ]
        )
        assert batch["completed"] is True
        assert [step["status"] for step in batch["operations"]] == ["ok", "ok"]

        event_result = project.events(cursor="0", wait_ms=0)
        assert any(
            item["kind"] == "application.action.completed" for item in event_result["events"]
        )
        assert int(event_result["cursor"]) >= 1
        assert project.close() is True
        assert project.close() is False
        client.close()
    finally:
        service.close()
    assert not manifest_path.exists()


def test_agent_can_inspect_artifact_references_and_store_statistics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graphimo import artifacts
    from graphimo.artifacts import local as local_artifacts

    artifact_root = tmp_path / "artifact-store"
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(artifact_root))
    local_artifacts._store_at.cache_clear()
    reference = artifacts.put_bytes(b"model-checkpoint", kind="checkpoint")
    service = GraphimoService(manifest_path=tmp_path / "state" / "service.json").start()
    notebook = tmp_path / "artifact-notebook.py"
    notebook.write_text("# Graphimo artifact inspection fixture\n", encoding="utf-8")
    try:
        client = GraphimoClient(service.url, service.token)
        project = client.open_project(notebook)

        assert project.inspect_artifact(reference.id) == {
            "id": reference.id,
            "kind": "checkpoint",
        }
        assert project.artifact_stats() == {
            "stats": {
                "artifacts": 1,
                "blobs": 1,
                "logical_bytes": len(b"model-checkpoint"),
                "physical_bytes": len(b"model-checkpoint"),
                "deduplicated_bytes": 0,
            }
        }
        project.close()
        client.close()
    finally:
        service.close()
        local_artifacts._store_at.cache_clear()


def test_authenticated_kernel_event_bridge_publishes_to_project_spool(tmp_path: Path) -> None:
    notebook = tmp_path / "kernel-notebook.py"
    notebook.write_text("# Graphimo kernel bridge fixture\n", encoding="utf-8")
    service = GraphimoService(manifest_path=tmp_path / "state" / "service.json").start()
    try:
        client = GraphimoClient(service.url, service.token)
        project = client.open_project(notebook)
        event = {
            "schema_version": 1,
            "event_id": "event-1",
            "kind": "activation.summary",
            "timestamp": 1.0,
            "project_id": project.project_id,
            "session_id": "marimo-session-a",
            "metablock_id": "model-a",
            "graph_revision": "revision-a",
            "runtime_instance_id": "runtime-a",
            "logical_element_id": "encoder",
            "module_path": "encoder.0",
            "step": 7,
            "payload": {"mean": 0.25},
        }
        assert client.publish_kernel_events(project, [event]) == {"accepted": 1}
        events = project.events(cursor="0", wait_ms=0)
        published = next(item for item in events["events"] if item["kind"] == "addon.event")
        assert published["source"] == "marimo_kernel"
        assert published["project_id"] == project.project_id
        assert published["metablock_id"] == "model-a"
        assert published["addon_event"] == event

        mismatched = {**event, "project_id": "another-project"}
        with pytest.raises(GraphimoServiceError) as failure:
            client.publish_kernel_events(project, [mismatched])
        assert failure.value.code == "invalid_kernel_events"
        with pytest.raises(TypeError, match="not JSON serializable"):
            client.publish_kernel_events(project, [{**event, "payload": {"model": object()}}])
        project.close()
        client.close()
    finally:
        service.close()


def test_attaching_a_new_marimo_session_cleans_the_previous_kernel(tmp_path: Path) -> None:
    notebook = tmp_path / "switch.py"
    notebook.write_text("# session switching\n", encoding="utf-8")
    session, _ = ProjectSessionManager().open_project(notebook)

    class PreviousKernel:
        url = "http://127.0.0.1:1234"
        session_id = "old-session"
        stopped = False

        def stop_publisher(self):
            self.stopped = True

    previous = PreviousKernel()
    session._kernel_port = previous
    session._subscriptions.add(("model-switch", "runtime-old", "subscription-old"))
    actions = []
    session.application.execute = lambda action, args: actions.append((action, args)) or {}

    session.attach_kernel(
        url="http://127.0.0.1:5678", token="new-marimo-token", session_id="new-session"
    )

    assert previous.stopped is True
    assert session._subscriptions == set()
    assert actions == [
        (
            "metablock.invoke",
            {
                "metablock_id": "model-switch",
                "action": "runtime.unsubscribe_all",
                "args": {},
            },
        )
    ]
    assert session._kernel_port.session_id == "new-session"
    session._kernel_port = None
    session.close()


def test_service_rejects_unauthorized_requests_and_non_loopback_bind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        GraphimoService(host="0.0.0.0", manifest_path=tmp_path / "service.json")
    with pytest.raises(ValueError, match="127.0.0.1"):
        GraphimoService(host="::1", manifest_path=tmp_path / "service.json")

    service = GraphimoService(manifest_path=tmp_path / "service.json").start()
    try:
        request = urllib.request.Request(service.url + "/v1/health", method="GET")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=2)
        assert error.value.code == 401
        body = json.loads(error.value.read().decode("utf-8"))
        assert body["error"]["code"] == "unauthorized"
    finally:
        service.close()


def test_service_rejects_oversized_request_before_reading_body(tmp_path: Path) -> None:
    service = GraphimoService(manifest_path=tmp_path / "service.json").start()
    try:
        host, port = service._server.server_address[:2]
        with socket.create_connection((host, port), timeout=2) as connection:
            connection.sendall(
                (
                    "POST /v1/projects/open HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n"
                    f"Authorization: Bearer {service.token}\r\n"
                    f"Content-Length: {MAX_REQUEST_BYTES + 1}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
            )
            response = b""
            while chunk := connection.recv(4096):
                response += chunk
        assert b"HTTP/1.1 413" in response
        assert b'"code": "request_too_large"' in response
    finally:
        service.close()


def test_client_refuses_non_loopback_urls() -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        GraphimoClient("http://example.com:8000", "secret")


def test_connect_local_starts_and_stops_a_detached_service(tmp_path: Path) -> None:
    manifest_path = tmp_path / "state" / "service.json"
    client = GraphimoClient.connect_local(
        manifest_path=manifest_path,
        auto_start=True,
        timeout=1,
        startup_timeout=8,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    process_id = manifest["pid"]
    try:
        assert client.health()["protocol"] == 1
        assert manifest_path.with_suffix(".json.log").stat().st_mode & 0o077 == 0
    finally:
        client.close()
        os.kill(process_id, signal.SIGINT)
        deadline = time.monotonic() + 5
        while manifest_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if manifest_path.exists():
            os.kill(process_id, signal.SIGTERM)
            manifest_path.unlink(missing_ok=True)
        assert not manifest_path.exists()


def test_client_exposes_service_errors_with_stable_codes(tmp_path: Path) -> None:
    service = GraphimoService(manifest_path=tmp_path / "service.json").start()
    try:
        client = GraphimoClient(service.url, service.token)
        with pytest.raises(GraphimoServiceError) as error:
            client.execute("missing-project", "scope.current")
        assert error.value.code == "project_not_found"
        assert error.value.status == 404
    finally:
        service.close()


def test_stdio_adapter_invokes_the_same_application_api(tmp_path: Path) -> None:
    project_path = tmp_path / "notebook.py"
    project_path.write_text("# service adapter fixture\n", encoding="utf-8")
    state_home = tmp_path / "state"
    manifest_path = state_home / "graphimo" / "service.json"
    service = GraphimoService(manifest_path=manifest_path).start()
    try:
        root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["XDG_STATE_HOME"] = str(state_home)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(root), str(root / "core"), environment.get("PYTHONPATH", "")]
        )
        result = subprocess.run(
            [sys.executable, "-m", "graphimo.service.invoke"],
            input=json.dumps(
                {"path": str(project_path), "operation": "execute", "action": "scope.current"}
            ),
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        response = json.loads(result.stdout)
        assert response["ok"] is True
        assert response["result"]["scope"]["kind"] == "project"
    finally:
        service.close()


def test_service_batch_writes_then_validates_a_reusable_component(tmp_path: Path) -> None:
    project_path = tmp_path / "research.py"
    project_path.write_text("# service component fixture\n", encoding="utf-8")
    service = GraphimoService(manifest_path=tmp_path / "service.json").start()
    client = GraphimoClient(service.url, service.token)
    project = client.open_project(project_path)
    source = (
        "from graphimo import node\n\n"
        "@node\n"
        "def normalize(value: float) -> float:\n"
        "    return value / 2\n"
    )
    try:
        result = project.batch(
            [
                {
                    "id": "write",
                    "action": "component.write",
                    "args": {"path": "normalize.py", "source": source},
                },
                {
                    "id": "validate",
                    "action": "component.validate",
                    "args": {"path": {"$ref": "write.result.path"}},
                },
            ]
        )

        assert result["completed"] is True
        assert [step["status"] for step in result["operations"]] == ["ok", "ok"]
        assert result["operations"][0]["result"]["valid"] is True
        assert result["operations"][1]["result"]["valid"] is True
        assert (tmp_path / "components/normalize.py").is_file()
    finally:
        project.close()
        service.close()


@pytest.mark.full
def test_service_pytorch_metablock_project_apply_materialize_and_events(tmp_path: Path) -> None:
    pytest.importorskip("graphimo_pytorch")
    import torch

    from graphimo_pytorch import train_pytorch_model

    project_path = tmp_path / "research.py"
    project_path.write_text("# service PyTorch integration fixture\n", encoding="utf-8")
    service = GraphimoService(manifest_path=tmp_path / "service.json").start()
    client = GraphimoClient(service.url, service.token)
    project = client.open_project(project_path)
    try:
        with pytest.raises(GraphimoServiceError) as import_error:
            project.execute(
                "metablock.create",
                {
                    "dialect_id": "pytorch.model",
                    "metablock_id": "unsupported_source",
                    "request": {"semantic_state_json": "class Model(torch.nn.Module): pass"},
                },
            )
        assert import_error.value.code == "pytorch_import_unsupported"

        batch = project.batch(
            [
                {
                    "id": "create",
                    "action": "metablock.create",
                    "args": {
                        "dialect_id": "pytorch.model",
                        "metablock_id": "model_service",
                        "request": {"name": "Service Model"},
                    },
                },
                {
                    "id": "add_relu",
                    "action": "metablock.apply",
                    "args": {
                        "metablock_id": {"$ref": "create.result.metablock_id"},
                        "intent": {
                            "type": "add_element",
                            "scope": "forward",
                            "kind": "relu",
                            "id": "relu_service",
                        },
                    },
                },
                {
                    "id": "disconnect_old_head",
                    "action": "metablock.apply",
                    "args": {
                        "metablock_id": {"$ref": "create.result.metablock_id"},
                        "intent": {
                            "type": "disconnect",
                            "scope": "forward",
                            "edge_id": "activation_to_head_output",
                        },
                    },
                },
                {
                    "id": "connect_activation_relu",
                    "action": "metablock.apply",
                    "args": {
                        "metablock_id": {"$ref": "create.result.metablock_id"},
                        "intent": {
                            "type": "connect",
                            "scope": "forward",
                            "source": {"element_id": "activation", "port": "output"},
                            "target": {"element_id": "relu_service", "port": "input"},
                        },
                    },
                },
                {
                    "id": "connect_relu_head",
                    "action": "metablock.apply",
                    "args": {
                        "metablock_id": {"$ref": "create.result.metablock_id"},
                        "intent": {
                            "type": "connect",
                            "scope": "forward",
                            "source": {"element_id": "relu_service", "port": "output"},
                            "target": {"element_id": "head_output", "port": "input"},
                        },
                    },
                },
            ]
        )
        assert batch["completed"] is True
        assert all(item["status"] == "ok" for item in batch["operations"])
        assert batch["operations"][0]["result"]["projection"]["dialect_id"] == "pytorch.model"
        applied = batch["operations"][-1]["result"]
        assert any(item["id"] == "relu_service" for item in applied["projection"]["elements"])
        materialized = project.execute(
            "metablock.materialize",
            {"metablock_id": "model_service", "bindings": {}},
        )
        assert "materialize_definition" in materialized["source"]
        assert materialized["result_name"] == "g_pytorch_model_service"
        namespace: dict[str, object] = {}
        exec(materialized["source"], namespace, namespace)
        definition = namespace[materialized["result_name"]]
        trained, summary, checkpoint = train_pytorch_model(
            definition,
            [(torch.ones(2, 4), torch.zeros(2, 1))],
            loss_fn=lambda output, target: torch.nn.functional.mse_loss(output["output"], target),
            persist_checkpoint=True,
        )
        assert summary["steps"] == 1
        assert checkpoint is not None
        runtime_id = trained.__graphimo_pytorch_runtime__.runtime_instance_id
        subscription = project.execute(
            "metablock.invoke",
            {
                "metablock_id": "model_service",
                "action": "runtime.subscribe",
                "args": {
                    "runtime_instance_id": runtime_id,
                    "logical_element_id": "backbone",
                    "signal": "activation",
                    "rate_hz": 10,
                },
            },
        )
        trained(torch.ones(2, 4))
        events = project.execute(
            "metablock.events",
            {"metablock_id": "model_service", "cursor": "0", "limit": 128},
        )
        assert isinstance(subscription["module_path"], str) and subscription["module_path"]
        assert any(event["kind"] == "activation.summary" for event in events["events"])
        assert any(
            event["kind"] == "metric.scalar" and event["payload"]["name"] == "train/loss"
            for event in events["events"]
        )
        inspection = project.execute(
            "metablock.invoke",
            {
                "metablock_id": "model_service",
                "action": "torchlens.inspect",
                "args": {
                    "runtime_instance_id": runtime_id,
                    "logical_element_id": "backbone",
                    "inputs": [[1.0, 2.0, 3.0, 4.0]],
                },
            },
        )
        assert inspection["logical_element_id"] == "backbone"
        assert inspection["selected_activation"]["available"] is True
        inspected = project.execute(
            "metablock.invoke",
            {
                "metablock_id": "model_service",
                "action": "checkpoint.inspect",
                "args": {"artifact_id": checkpoint.id},
            },
        )
        assert inspected["artifact_id"] == checkpoint.id
        assert inspected["metadata"]["training_steps"] == 1
        project.execute(
            "metablock.apply",
            {
                "metablock_id": "model_service",
                "intent": {
                    "type": "set_model_parameter",
                    "name": "hidden_size",
                    "value": 9,
                },
            },
        )
        migration = project.execute(
            "metablock.invoke",
            {
                "metablock_id": "model_service",
                "action": "checkpoint.preview_migration",
                "args": {"artifact_id": checkpoint.id, "external_values": {}},
            },
        )
        assert migration["graph_revision"] != inspected["graph_revision"]
        assert set(migration["report"]) == {
            "copied",
            "missing_in_source",
            "missing_in_target",
            "shape_mismatch",
        }
        events = project.events(cursor="0", limit=128, wait_ms=0)
        assert any(
            event["kind"] == "metablock.materialization.requested" for event in events["events"]
        )
    finally:
        project.close()
        service.close()
