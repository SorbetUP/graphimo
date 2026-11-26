from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from graphimo.application import GraphimoApplication
from graphimo.metablocks import (
    GraphimoHostAPI,
    MetaBlockAddonManager,
    MetaDialectRegistration,
)
from graphimo.workbench import GraphimoWorkbenchController, create_server


class WorkbenchDialect:
    dialect_id = "test.workbench"

    def __init__(self, _host: GraphimoHostAPI):
        pass

    def create_state(self, request: dict[str, Any], _context: GraphimoHostAPI) -> dict[str, Any]:
        return {"name": request.get("name", "Workbench test")}

    def catalog(self, state: Any, scope: str, _context: GraphimoHostAPI) -> Any:
        return {"scope": scope, "items": []}

    def project(self, state: Any, scope: str, _context: GraphimoHostAPI) -> Any:
        return {"name": state["name"], "scope": scope}

    def apply(self, state: Any, intent: dict[str, Any], _context: GraphimoHostAPI) -> Any:
        return {**state, "value": intent.get("value")}

    def build_materialization(
        self, state: Any, bindings: dict[str, Any], _context: GraphimoHostAPI
    ):
        return {
            "source": "test_value = 1\n",
            "result_name": "test_value",
            "output_type": "int",
            "bindings": bindings,
            "revision": "",
        }

    def invoke(
        self, state: Any, action: str, args: dict[str, Any], _context: GraphimoHostAPI
    ) -> Any:
        return {"action": action, "args": args}

    def drain_events(self, cursor: str | None, limit: int, _context: GraphimoHostAPI) -> Any:
        return {"cursor": cursor or "0", "events": []}


class WorkbenchEntryPoint:
    name = "test.workbench"
    value = "workbench:registration"
    dist = SimpleNamespace(name="graphimo-workbench-test", version="1.0")

    def load(self):
        return MetaDialectRegistration("test.workbench", "1.0", 1, WorkbenchDialect)


def build_test_controller(tmp_path: Path) -> GraphimoWorkbenchController:
    project = tmp_path / "project.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    host = GraphimoHostAPI("project", project)
    manager = MetaBlockAddonManager(host, entry_points=lambda: [WorkbenchEntryPoint()])
    return GraphimoWorkbenchController(GraphimoApplication(host, manager), project)


def test_controller_forwards_actions_to_shared_application(tmp_path: Path) -> None:
    controller = build_test_controller(tmp_path)

    created = controller.action(
        {
            "action": "metablock.create",
            "args": {
                "dialect_id": "test.workbench",
                "metablock_id": "test-block",
                "request": {"name": "Local model"},
            },
        }
    )

    assert created["ok"] is True
    assert created["result"]["projection"]["name"] == "Local model"
    assert controller.project()["metablocks"]["items"][0]["metablock_id"] == "test-block"


def test_workbench_http_serves_shell_and_routes_application_actions(tmp_path: Path) -> None:
    controller = build_test_controller(tmp_path)
    server = create_server(controller, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            assert response.status == 200
            assert b"Graphimo Workbench" in response.read()
        request = urllib.request.Request(
            f"{url}/api/action",
            data=json.dumps(
                {
                    "action": "metablock.create",
                    "args": {"dialect_id": "test.workbench", "metablock_id": "http-block"},
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            value = json.loads(response.read())
        assert value["ok"] is True
        assert value["result"]["metablock_id"] == "http-block"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_workbench_rejects_non_loopback_bind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        create_server(build_test_controller(tmp_path), host="0.0.0.0")
