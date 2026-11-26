"""Mutation-reliability contracts: idempotent operation ids, batch isolation,
and compensated composed operations."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from graphimo.application import GraphimoApplication
from graphimo.client import GraphimoClient
from graphimo.metablocks import GraphimoHostAPI
from graphimo.service.server import GraphimoService
from graphimo.service.sessions import ProjectSessionManager

NODE_SOURCE = (
    "from graphimo import node\n\n"
    "@node\n"
    "def normalize(value: float) -> float:\n"
    "    return value / 2\n"
)


def _open(tmp_path: Path):
    project_path = tmp_path / "notebook.py"
    project_path.write_text("# mutation fixture\n", encoding="utf-8")
    service = GraphimoService(manifest_path=tmp_path / "service.json").start()
    client = GraphimoClient(service.url, service.token)
    project = client.open_project(project_path)
    return service, client, project, project_path


def test_operation_id_makes_mutations_idempotent(tmp_path: Path) -> None:
    service, client, project, project_path = _open(tmp_path)
    try:
        args = {"path": "normalize.py", "source": NODE_SOURCE, "operation_id": "write-once"}
        first = project.execute("component.write", args)
        assert first["valid"] is True
        assert "replayed" not in first

        second = project.execute("component.write", dict(args))
        assert second["replayed"] is True
        assert second["operation_id"] == "write-once"
        assert second["result"]["sha256"] == first["sha256"]

        # Read-only actions never enter the journal.
        listing = project.execute("component.list", {"operation_id": "list-1"})
        assert "replayed" not in listing
        again = project.execute("component.list", {"operation_id": "list-1"})
        assert "replayed" not in again

        with pytest.raises(Exception, match="operation_id"):
            project.execute(
                "component.write",
                {"path": "x.py", "source": NODE_SOURCE, "operation_id": "bad id!"},
            )

        # A different operation id re-executes (and here hits the same file).
        third = project.execute(
            "component.write",
            {"path": "normalize.py", "source": NODE_SOURCE, "operation_id": "write-twice"},
        )
        assert "replayed" not in third
        assert (project_path.parent / "components/normalize.py").is_file()
    finally:
        project.close()
        client.close()
        service.close()


def test_batch_holds_the_action_lock_for_the_whole_sequence(tmp_path: Path) -> None:
    project_path = tmp_path / "locked.py"
    project_path.write_text("# lock fixture\n", encoding="utf-8")
    manager = ProjectSessionManager()
    session, _ = manager.open_project(project_path)
    order: list[str] = []
    blocker = threading.Event()
    original = session.application.execute

    def slow_execute(action: str, args: dict) -> object:
        if action == "slow":
            order.append("slow-start")
            assert blocker.wait(timeout=5)
            order.append("slow-end")
            return {"done": True}
        order.append(f"{action}-start")
        result = original(action, args)
        order.append(f"{action}-end")
        return result

    session.application.execute = slow_execute  # type: ignore[method-assign]
    try:
        batch_result: dict[str, object] = {}

        def run_batch() -> None:
            batch_result["result"] = session.execute_batch(
                [
                    {"id": "one", "action": "slow"},
                    {"id": "two", "action": "scope.current"},
                ]
            )

        def run_single() -> None:
            session.execute("scope.current", {})

        batch_thread = threading.Thread(target=run_batch)
        batch_thread.start()
        time.sleep(0.3)
        single_thread = threading.Thread(target=run_single)
        single_thread.start()
        time.sleep(0.3)
        # The single action is blocked on the action lock held by the batch.
        assert order == ["slow-start"]
        blocker.set()
        batch_thread.join(timeout=5)
        single_thread.join(timeout=5)
        assert order == [
            "slow-start",
            "slow-end",
            "scope.current-start",
            "scope.current-end",
            "scope.current-start",
            "scope.current-end",
        ]
        assert batch_result["result"]["completed"] is True  # type: ignore[index]
    finally:
        manager.close()


def test_composed_component_add_rolls_back_created_cells_on_failure(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# rollback fixture\n", encoding="utf-8")
    calls: list[tuple[str, dict]] = []
    deleted: list[str] = []

    def invoke_notebook(action: str, args: dict):
        calls.append((action, args))
        if action == "inspect":
            return {"ok": True, "cells": []}
        if action == "add_cell":
            if "# Graphimo node:" in args.get("source", ""):
                raise ValueError("kernel exploded on the instance cell")
            return {"ok": True, "cell_id": f"cell-{len(calls)}", "source": args["source"]}
        if action == "run":
            return {"ok": True, "status": "queued"}
        if action == "delete":
            deleted.append(args["cell_id"])
            return {"ok": True, "deleted": True}
        raise AssertionError(f"unexpected action {action}")

    host = GraphimoHostAPI("project", project, invoke_notebook=invoke_notebook)
    application = GraphimoApplication(host)
    application.execute("component.write", {"path": "normalize.py", "source": NODE_SOURCE})

    with pytest.raises(ValueError, match="rollback"):
        application.execute(
            "component.add",
            {"path": "normalize.py", "callable": "normalize", "output_name": "out", "bindings": {}},
        )
    # The library cell created before the failure must have been deleted.
    assert deleted == ["cell-2"], deleted
    deletes = [args for action, args in calls if action == "delete"]
    assert deletes == [{"cell_id": "cell-2"}]


def test_composed_control_add_rolls_back_on_failure(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# rollback fixture\n", encoding="utf-8")
    deleted: list[str] = []

    def invoke_notebook(action: str, args: dict):
        if action == "inspect":
            return {"ok": True, "cells": []}
        if action == "add_cell":
            return {
                "ok": True,
                "cell_id": f"c-{action}-{len(deleted)}-{len(str(args))}",
                "source": args.get("source", ""),
            }
        if action == "run":
            raise ValueError("kernel refused to run the control cell")
        if action == "delete":
            deleted.append(args["cell_id"])
            return {"ok": True, "deleted": True}
        raise AssertionError(f"unexpected action {action}")

    host = GraphimoHostAPI("project", project, invoke_notebook=invoke_notebook)
    application = GraphimoApplication(host)
    with pytest.raises(ValueError, match="rollback"):
        application.execute(
            "control.add",
            {
                "kind": "slider",
                "identity": "gain",
                "parameters": {"start": 0.0, "stop": 1.0, "step": 0.1, "value": 0.5},
            },
        )
    assert len(deleted) == 2, deleted


def test_operation_id_replay_rejects_different_action_or_args(tmp_path: Path) -> None:
    from graphimo.client import GraphimoServiceError

    service, client, project, _project_path = _open(tmp_path)
    try:
        project.execute(
            "component.write",
            {"path": "normalize.py", "source": NODE_SOURCE, "operation_id": "op-1"},
        )
        # Same id, different args -> explicit conflict, never a silent replay.
        with pytest.raises(GraphimoServiceError) as exc_info:
            project.execute(
                "component.write",
                {"path": "other.py", "source": NODE_SOURCE, "operation_id": "op-1"},
            )
        assert exc_info.value.code == "operation_id_conflict"
        # Same id, different mutating action -> conflict as well.
        with pytest.raises(GraphimoServiceError) as exc_info2:
            project.execute(
                "layout.set", {"scope_id": "root", "positions": {}, "operation_id": "op-1"}
            )
        assert exc_info2.value.code == "operation_id_conflict"
        # Identical action+args still replays.
        replayed = project.execute(
            "component.write",
            {"path": "normalize.py", "source": NODE_SOURCE, "operation_id": "op-1"},
        )
        assert replayed["replayed"] is True
    finally:
        project.close()
        client.close()
        service.close()
