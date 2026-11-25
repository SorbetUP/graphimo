from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from copy import deepcopy

import pytest

from graphimo.client import GraphimoClient, GraphimoServiceError, expr
from graphimo.groups import GroupPort, create_group_file
from graphimo.service.events import EventJournal
from graphimo.service.protocol import MAX_EVENT_BYTES, MAX_RESPONSE_BYTES
from graphimo.service.server import GraphimoService
from graphimo.service.sessions import ProjectSessionManager


class FakeMarimoPort:
    def __init__(self, path):
        self.notebook_path = str(path)
        self.session_id = "session-1"
        self.base_url = "http://127.0.0.1:2745"
        self.snapshot = {
            "notebook": {"path": self.notebook_path, "session_id": self.session_id},
            "cells": [],
            "edges": [],
        }
        self.added = []
        self._next = 0

    def list_active_notebooks(self):
        return [{"session_id": self.session_id, "path": self.notebook_path}]

    def url_for_session(self, session_id):
        return self.base_url if session_id == self.session_id else None

    def inspect(self, session_id):
        assert session_id == self.session_id
        return deepcopy(self.snapshot)

    def add_cell(self, session_id, source):
        assert session_id == self.session_id
        self._next += 1
        cell_id = f"cell-{self._next}"
        self.added.append((cell_id, source))
        self.snapshot["cells"].append(
            {"id": cell_id, "source": source, "defs": [], "refs": [], "status": "idle"}
        )
        return cell_id

    def run_cell(self, session_id, cell_id):
        return {"status": "completed", "cell_id": cell_id}

    def close(self):
        pass


def _project(tmp_path):
    path = tmp_path / "project.py"
    path.write_text("import marimo\napp = marimo.App()\n", encoding="utf-8")
    components = tmp_path / "components"
    components.mkdir()
    create_group_file(
        components / "pipeline.py",
        group_id="pipeline",
        name="Pipeline",
        inputs=[GroupPort("dataset", "dataset")],
        outputs=[GroupPort("model", "model")],
    )
    return path


def test_event_journal_is_bounded_and_reports_cursor_drops():
    journal = EventJournal(capacity=2)
    journal.publish({"n": 0})
    journal.publish({"n": 1})
    journal.publish({"n": 2})

    page = journal.read(0, limit=2)

    assert page == {"cursor": 3, "events": [{"n": 1}, {"n": 2}], "dropped": 1}
    with pytest.raises(ValueError, match="event limit"):
        journal.read(0, limit=257)
    with pytest.raises(ValueError, match="ahead"):
        journal.read(4)


def test_event_journal_bounds_payloads_and_response_bytes():
    journal = EventJournal(capacity=64)
    for index in range(40):
        journal.publish({"index": index, "payload": "x" * (MAX_EVENT_BYTES - 1000)})

    page = journal.read(0, limit=40)

    assert len(json.dumps(page, separators=(",", ":")).encode()) <= MAX_RESPONSE_BYTES
    assert len(page["events"]) < 40
    assert page["events"][0]["index"] == 0


def test_loopback_service_client_batch_references_and_events(tmp_path):
    notebook = _project(tmp_path)
    manager = ProjectSessionManager(marimo_factory=FakeMarimoPort)
    with GraphimoService("127.0.0.1", 0, sessions=manager) as service:
        client = GraphimoClient(service.base_url, service.token)
        project = client.open_project(notebook)
        reopened = client.open_project(notebook)
        assert reopened.id == project.id
        assert project.editor_url == "http://127.0.0.1:2745"
        assert reopened.state["palette"]["items"]

        block = project.add("Pipeline", dataset=expr("input_data"))
        assert block.group_id == "pipeline"
        assert block.result_var in manager.get(project.id).marimo.added[-1][1]

        result = project.batch(
            [
                {
                    "id": "first",
                    "action": "add_palette_item",
                    "args": {"item_id": "group:pipeline.py", "bindings": {"dataset": "input_data"}},
                },
                {
                    "id": "second",
                    "action": "add_palette_item",
                    "args": {
                        "item_id": "group:pipeline.py",
                        "bindings": {"dataset": {"$ref": "first.result.result_var"}},
                    },
                },
            ]
        )
        assert [item["status"] for item in result["operations"]] == ["success", "success"]
        second_source = result["operations"][1]["result"]["source"]
        first_var = result["operations"][0]["result"]["result_var"]
        assert f"'dataset': {first_var}" in second_source

        failed = project.batch(
            [
                {"id": "bad", "action": "not_an_application_action", "args": {}},
                {"id": "skipped", "action": "refresh_project", "args": {}},
            ]
        )
        assert [item["status"] for item in failed["operations"]] == ["error", "not_executed"]

        page = project.events(cursor=0, limit=100, wait_ms=0)
        assert page["cursor"] > 0
        assert any(item["type"] == "project.opened" for item in page["events"])
        assert any(item["type"] == "application.operation.completed" for item in page["events"])


def test_service_can_create_a_new_marimo_project_file(tmp_path):
    manager = ProjectSessionManager(marimo_factory=FakeMarimoPort)
    with GraphimoService("127.0.0.1", 0, sessions=manager) as service:
        client = GraphimoClient(service.base_url, service.token)
        notebook = tmp_path / "nested" / "new_project.py"
        project = client.create_project(notebook)

        assert notebook.is_file()
        assert "marimo.App()" in notebook.read_text(encoding="utf-8")
        assert project.path == str(notebook.resolve())
        with pytest.raises(GraphimoServiceError) as error:
            client.create_project(notebook)
        assert error.value.code == "project_already_exists"


def test_long_poll_can_observe_operation_start_while_marimo_action_is_running(tmp_path):
    notebook = _project(tmp_path)
    manager = ProjectSessionManager(marimo_factory=FakeMarimoPort)
    with GraphimoService("127.0.0.1", 0, sessions=manager) as service:
        client = GraphimoClient(service.base_url, service.token)
        project = client.open_project(notebook)
        session = manager.get(project.id)
        entered = threading.Event()
        release = threading.Event()

        def slow_run(cell_id):
            assert cell_id == "slow"
            entered.set()
            release.wait(3)
            return {"status": "completed"}

        session.application.run_target = slow_run
        operation = threading.Thread(
            target=lambda: manager.execute(project.id, "run_target", {"cell_id": "slow"}),
            daemon=True,
        )
        operation.start()
        assert entered.wait(2)
        page = client.events(project.id, cursor=1, wait_ms=1500)
        assert any(item["type"] == "application.operation.started" for item in page["events"])
        assert operation.is_alive()
        release.set()
        operation.join(timeout=3)
        assert not operation.is_alive()


def test_session_manager_pumps_addon_events_into_project_cursor_stream(tmp_path):
    notebook = _project(tmp_path)
    manager = ProjectSessionManager(marimo_factory=FakeMarimoPort)
    opened = manager.open_project(notebook)
    session = manager.get(opened["project_id"])
    sidecars = notebook.parent / ".graphimo" / "metablocks"
    sidecars.mkdir(parents=True)
    (sidecars / "meta-1.example.json").write_text(
        json.dumps({"metablock_id": "meta-1"}), encoding="utf-8"
    )
    calls = []

    def drain(metablock_id, *, cursor, limit):
        calls.append((metablock_id, cursor, limit))
        if cursor == 0:
            return {"cursor": 1, "events": [{"type": "activation", "value": 0.5}]}
        return {"cursor": cursor, "events": []}

    session.application.drain_metablock_events = drain
    manager.pump_events(session)
    manager.pump_events(session)

    page = session.journal.read(1)
    assert calls == [("meta-1", 0, 256), ("meta-1", 1, 256)]
    assert page["events"] == [
        {
            "type": "addon.event",
            "project_id": session.id,
            "metablock_id": "meta-1",
            "event": {"type": "activation", "value": 0.5},
        }
    ]


@pytest.mark.skipif(
    importlib.util.find_spec("marimo") is None or importlib.util.find_spec("websockets") is None,
    reason="live Graphimo service conformance requires marimo and websockets",
)
def test_service_runs_and_inspects_real_marimo_node_without_zeron(tmp_path):
    notebook = tmp_path / "live_project.py"
    notebook.write_text(
        "import marimo\napp = marimo.App()\nif __name__ == '__main__': app.run()\n",
        encoding="utf-8",
    )
    components = tmp_path / "components"
    components.mkdir()
    (components / "scale.py").write_text(
        "from graphimo import node\n"
        "@node\n"
        "def scale(x: float) -> float:\n"
        "    result = x * 3\n"
        "    return result\n",
        encoding="utf-8",
    )
    with GraphimoService("127.0.0.1", 0) as service:
        client = GraphimoClient(service.base_url, service.token)
        project = client.open_project(notebook)
        node = project.add("scale", x=4.0)
        node.run()
        inspection = node.inspect()
        assert inspection["outputs"][0]["preview"] == "12.0"


def test_service_requires_token_and_rejects_non_loopback_bind(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        GraphimoService("0.0.0.0", 0)
    with GraphimoService(
        "127.0.0.1", 0, sessions=ProjectSessionManager(marimo_factory=FakeMarimoPort)
    ) as service:
        request = urllib.request.Request(
            service.base_url + "/v1/projects/open",
            data=b'{"path":"x"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=2)
        assert error.value.code == 401
        payload = json.loads(error.value.read())
        assert payload["error"]["code"] == "unauthorized"


def test_client_discovers_running_service_from_private_runtime_file(tmp_path):
    discovery = tmp_path / "service.json"
    with GraphimoService(
        "127.0.0.1",
        0,
        sessions=ProjectSessionManager(marimo_factory=FakeMarimoPort),
        discovery_file=discovery,
    ) as service:
        client = GraphimoClient.connect_local(discovery_file=discovery)
        assert client.base_url == service.base_url
        assert client.token == service.token
        assert discovery.exists()
    assert not discovery.exists()


def test_client_can_start_and_stop_persistent_local_service(tmp_path):
    discovery = tmp_path / "runtime" / "service.json"
    client = GraphimoClient.connect_local(discovery_file=discovery, startup_timeout=10)
    assert client.base_url.startswith("http://127.0.0.1:")
    assert discovery.exists()

    assert client.stop_service()["stopping"] is True
    deadline = time.monotonic() + 5
    while discovery.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not discovery.exists()


def test_service_rejects_malformed_and_oversized_json(tmp_path):
    with GraphimoService(
        "127.0.0.1", 0, sessions=ProjectSessionManager(marimo_factory=FakeMarimoPort)
    ) as service:
        headers = {"Authorization": f"Bearer {service.token}", "Content-Type": "application/json"}
        malformed = urllib.request.Request(
            service.base_url + "/v1/projects/open", data=b"{", headers=headers, method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as bad_json:
            urllib.request.urlopen(malformed, timeout=2)
        assert bad_json.value.code == 400
        sock = socket.create_connection(("127.0.0.1", service.port), timeout=2)
        try:
            request_head = (
                f"POST /v1/projects/open HTTP/1.1\r\nHost: 127.0.0.1:{service.port}\r\n"
                f"Authorization: Bearer {service.token}\r\nContent-Type: application/json\r\n"
                "Content-Length: 1000001\r\nConnection: close\r\n\r\n"
            ).encode()
            sock.sendall(request_head)
            response = sock.recv(1024)
        finally:
            sock.close()
        assert b"413" in response.split(b"\r\n", 1)[0]
