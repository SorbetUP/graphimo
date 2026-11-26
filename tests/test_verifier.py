"""Tests for Graphimo Verifier assertions over normal project handles."""

from __future__ import annotations

import json
from typing import Any

import pytest

from graphimo.verifier import (
    ScenarioRunError,
    VerificationError,
    VerifiedProject,
    VerifierScenario,
)
from graphimo.client import GraphimoClient
from graphimo.service.server import GraphimoService


class SnapshotProject:
    def __init__(self, snapshot: dict[str, Any]):
        self.snapshot = snapshot

    def inspect_notebook(self) -> dict[str, Any]:
        return self.snapshot


def test_verifier_checks_edges_against_live_definitions_and_references() -> None:
    project = VerifiedProject(
        SnapshotProject(
            {
                "cells": [
                    {"id": "source", "defs": ["value"], "refs": []},
                    {"id": "target", "defs": ["result"], "refs": ["value"]},
                ],
                "edges": [
                    {
                        "source_cell_id": "source",
                        "target_cell_id": "target",
                        "variable": "value",
                    }
                ],
                "truncated": False,
            }
        )
    )

    assert project.assert_graph_consistent() == {
        "cells": 2,
        "edges": 1,
        "consistent": True,
    }


@pytest.mark.parametrize(
    "snapshot, message",
    [
        ({"cells": [], "edges": [], "truncated": True}, "truncated"),
        (
            {
                "cells": [{"id": "target", "defs": [], "refs": ["missing"]}],
                "edges": [
                    {
                        "source_cell_id": "missing-source",
                        "target_cell_id": "target",
                        "variable": "missing",
                    }
                ],
                "truncated": False,
            },
            "missing from the snapshot",
        ),
    ],
)
def test_verifier_refuses_unknown_or_truncated_graphs(
    snapshot: dict[str, Any], message: str
) -> None:
    with pytest.raises(VerificationError, match=message):
        VerifiedProject(SnapshotProject(snapshot)).assert_graph_consistent()


def test_verifier_asserts_bounded_output_preview() -> None:
    project = VerifiedProject(
        SnapshotProject(
            {
                "cells": [
                    {
                        "id": "cell-a",
                        "outputs": [{"name": "answer", "preview": "42", "runtime_type": "int"}],
                    }
                ],
                "edges": [],
            }
        )
    )

    assert project.assert_output("cell-a", "answer", "42") == {
        "cell_id": "cell-a",
        "output_name": "answer",
        "preview": "42",
    }
    with pytest.raises(VerificationError, match="preview mismatch"):
        project.assert_output("cell-a", "answer", "43")


def test_verifier_executes_actions_through_the_graphimo_service(tmp_path) -> None:
    notebook = tmp_path / "verifier-project.py"
    notebook.write_text("# verifier service fixture\n", encoding="utf-8")
    service = GraphimoService(manifest_path=tmp_path / "state" / "service.json").start()
    try:
        from graphimo.verifier import GraphimoVerifier

        with GraphimoVerifier(GraphimoClient(service.url, service.token)) as verifier:
            with verifier.project(notebook) as project:
                current = project.execute("scope.current")
                assert current["scope"]["kind"] == "project"
                batch = project.batch(
                    [
                        {"id": "scope", "action": "scope.current"},
                        {
                            "id": "layout",
                            "action": "layout.get",
                            "args": {"scope_id": {"$ref": "scope.result.scope.scope_id"}},
                        },
                    ]
                )
                assert batch["completed"] is True
                assert [item["status"] for item in batch["operations"]] == ["ok", "ok"]
    finally:
        service.close()


def test_scenario_round_trip_records_seed_resolved_actions_and_replays(tmp_path) -> None:
    class RecordingProject:
        def __init__(self):
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def execute(self, action: str, args: dict[str, Any] | None = None) -> Any:
            self.calls.append((action, args or {}))
            if action == "notebook.add_cell":
                return {"cell_id": "fresh-cell"}
            return {"run": args["cell_id"]}

    project = RecordingProject()
    scenario = VerifierScenario("small_pipeline", 48192, invariants=())
    scenario.add("notebook.add_cell", {"source": "value = 42"}, step_id="add")
    scenario.add("notebook.run", {"cell_id": {"$ref": "add.result.cell_id"}}, step_id="run")
    source_path = scenario.save(tmp_path / "small_pipeline.json")
    loaded = VerifierScenario.load(source_path)
    journal_path = tmp_path / "runs" / "seed-48192.json"

    result = loaded.replay(VerifiedProject(project), journal_path=journal_path)

    assert project.calls == [
        ("notebook.add_cell", {"source": "value = 42"}),
        ("notebook.run", {"cell_id": "fresh-cell"}),
    ]
    assert result["seed"] == 48192
    assert result["completed"] is True
    assert [item["sequence"] for item in result["operations"]] == [1, 2]
    assert result["operations"][1]["resolved_args"] == {"cell_id": "fresh-cell"}
    assert json.loads(journal_path.read_text(encoding="utf-8")) == result


def test_scenario_failure_preserves_replay_journal(tmp_path) -> None:
    class FailingProject:
        def execute(self, _action: str, _args: dict[str, Any] | None = None) -> Any:
            raise RuntimeError("intent failed")

    scenario = VerifierScenario("failure", 8, invariants=()).add("scope.current")
    journal_path = tmp_path / "failure.json"

    with pytest.raises(ScenarioRunError, match="seed=8") as failure:
        scenario.replay(VerifiedProject(FailingProject()), journal_path=journal_path)

    assert failure.value.journal["operations"][0]["status"] == "error"
    assert failure.value.journal["operations"][0]["error"]["message"] == "intent failed"
    assert journal_path.is_file()


def test_verifier_asserts_cursor_spooled_addon_events() -> None:
    class EventProject:
        def events(self, *, cursor, limit, wait_ms):
            assert cursor == "12"
            assert limit == 128
            assert wait_ms >= 0
            return {
                "cursor": "13",
                "events": [
                    {
                        "kind": "addon.event",
                        "sequence": 13,
                        "addon_event": {
                            "kind": "activation.summary",
                            "metablock_id": "model-a",
                        },
                    }
                ],
            }

    result = VerifiedProject(EventProject()).assert_event_received(
        "activation.summary", metablock_id="model-a", cursor="12", wait_seconds=1
    )

    assert result == {
        "event_kind": "activation.summary",
        "metablock_id": "model-a",
        "sequence": 13,
        "cursor": "13",
    }
