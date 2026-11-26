"""Tests for Graphimo Verifier assertions over normal project handles."""

from __future__ import annotations

from typing import Any

import pytest

from graphimo.verifier import VerificationError, VerifiedProject
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
