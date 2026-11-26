"""Independent tests for the authenticated local Graphimo service."""

from __future__ import annotations

import json
import os
import signal
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from graphimo.client import GraphimoClient, GraphimoServiceError
from graphimo.service.events import EventJournal
from graphimo.service.protocol import execute_batch, parse_json_object
from graphimo.service.server import GraphimoService


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
                    "Content-Length: 262145\r\n"
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
