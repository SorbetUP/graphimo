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


def test_scenario_seed_is_injectable_and_step_data_is_deep_copied() -> None:
    class RecordingProject:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def execute(self, action: str, args: dict[str, Any] | None = None) -> Any:
            self.calls.append((action, args or {}))
            return {"ok": True}

    project = RecordingProject()
    scenario = VerifierScenario("seeded", 4242, invariants=())
    args = {"source": "value = 1", "seed": {"$seed": True}}
    scenario.add("notebook.add_cell", args, step_id="add")
    args["source"] = "mutated-after-add"
    args["seed"] = "mutated"

    journal = scenario.replay(VerifiedProject(project))

    # The recorded scenario was deep-copied at add() time.
    assert scenario.to_dict()["steps"][0]["args"]["source"] == "value = 1"
    # $seed resolves to the durable scenario seed at replay time.
    assert project.calls == [("notebook.add_cell", {"source": "value = 1", "seed": 4242})]
    assert journal["operations"][0]["resolved_args"]["seed"] == 4242


def test_compare_journals_normalizes_runtime_ids_and_detects_differences() -> None:
    from graphimo.verifier import compare_journals

    base = {
        "seed": 7,
        "completed": True,
        "operations": [
            {
                "id": "add",
                "action": "notebook.add_cell",
                "status": "ok",
                "started_at": "2026-01-01T00:00:00.000+00:00",
                "result": {"cell_id": "e" * 64, "artifact": "b" * 64},
            }
        ],
    }
    same = json.loads(json.dumps(base))
    same["operations"][0]["started_at"] = "2026-01-02T00:00:00.000+00:00"
    same["operations"][0]["result"] = {"cell_id": "f" * 64, "artifact": "d" * 64}

    assert compare_journals(base, same)["match"] is True

    drifted = json.loads(json.dumps(base))
    drifted["operations"][0]["status"] = "error"
    comparison = compare_journals(base, drifted)
    assert comparison["match"] is False
    assert any("status" in item for item in comparison["differences"])


def test_expected_error_step_verifies_notebook_state_is_preserved() -> None:
    from graphimo.client import GraphimoServiceError

    class FakeNotebook:
        def __init__(self, cells: list[dict[str, Any]], fail_on: str) -> None:
            self._cells = cells
            self._fail_on = fail_on
            self.calls = 0

        def inspect_notebook(self) -> dict[str, Any]:
            return {"cells": [dict(cell) for cell in self._cells], "edges": []}

        def execute(self, action: str, args: dict[str, Any] | None = None) -> Any:
            self.calls += 1
            if action == self._fail_on:
                raise GraphimoServiceError("unsafe_edit", "rejected")
            return {"ok": True}

    cells = [{"id": "cell-a", "source": "x = 1", "defs": ["x"], "refs": []}]
    scenario = VerifierScenario("guarded", 3, invariants=())
    scenario.add(
        "notebook.delete", {"cell_id": "cell-a"}, step_id="bad", expect_error="unsafe_edit"
    )
    project = FakeNotebook(cells, fail_on="notebook.delete")
    journal = scenario.replay(VerifiedProject(project))
    assert journal["operations"][0]["status"] == "expected_error"
    assert journal["operations"][0]["state_preserved"] is True

    class DriftingNotebook(FakeNotebook):
        def execute(self, action: str, args: dict[str, Any] | None = None) -> Any:
            try:
                return super().execute(action, args)
            finally:
                if action == self._fail_on:
                    self._cells.append(
                        {"id": "ghost", "source": "y = 2", "defs": ["y"], "refs": []}
                    )

    drifting = DriftingNotebook([dict(cell) for cell in cells], fail_on="notebook.delete")
    with pytest.raises(ScenarioRunError, match="notebook changed"):
        scenario.replay(VerifiedProject(drifting))


def test_fuzz_replay_replays_seeded_scenario_on_fresh_copies(tmp_path) -> None:
    from graphimo.verifier import GraphimoVerifier

    notebook = tmp_path / "fuzz-project.py"
    notebook.write_text("# fuzz fixture\n", encoding="utf-8")
    service = GraphimoService(manifest_path=tmp_path / "state" / "service.json").start()
    try:
        with GraphimoVerifier(GraphimoClient(service.url, service.token)) as verifier:
            scenario = VerifierScenario("fuzz", 99, invariants=())
            scenario.add("scope.current", {}, step_id="scope")
            report = verifier.fuzz_replay(
                scenario, notebook, rounds=2, journal_dir=tmp_path / "fuzz"
            )
            assert report["deterministic"] is True
            assert len(report["comparisons"]) == 1
            assert report["comparisons"][0]["match"] is True
    finally:
        service.close()
