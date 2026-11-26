from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

from graphimo import (
    GraphimoApplication,
    GraphimoHostAPI,
    GroupContract,
    GroupError,
    GroupLibrary,
    GroupResult,
    is_node,
    run_group,
)


def _group_body() -> str:
    return """
@app.cell
def calculate(value, factor):
    result = value * factor
    return result,
"""


def _create(project_path: Path, *, group_id: str = "scale_values"):
    return GroupLibrary(project_path).create(
        {
            "group_id": group_id,
            "module": group_id,
            "name": "Scale Values",
            "inputs": ["value", "factor"],
            "outputs": ["result"],
            "body_source": _group_body(),
        }
    )


def test_group_component_runs_a_cloned_marimo_app_with_named_outputs(tmp_path: Path) -> None:
    definition = _create(tmp_path / "notebook.py")

    first = run_group(definition.path, inputs={"value": 4, "factor": 3}, outputs=("result",))
    second = run_group(definition.path, inputs={"value": 8, "factor": 5}, outputs=("result",))

    assert isinstance(first, GroupResult)
    assert first.result == first["result"] == 12
    assert second.result == second["result"] == 40
    assert first is not second


def test_project_group_is_a_reusable_at_node_component(tmp_path: Path, monkeypatch) -> None:
    definition = _create(tmp_path / "notebook.py")
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module(definition.import_path)
    try:
        component = getattr(module, definition.callable_name)
        assert is_node(component)
        result = component(value=7, factor=6)
        assert result.result == 42
        assert definition.to_catalog_item()["import_source"] == (
            "from groups.scale_values import scale_values"
        )
    finally:
        sys.modules.pop(definition.import_path, None)
        sys.modules.pop("groups", None)


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"value": 2}, "missing="),
        ({"value": 2, "factor": 3, "extra": 4}, "extra="),
    ],
)
def test_group_invocation_requires_all_and_only_contract_inputs(
    tmp_path: Path, inputs: dict[str, Any], message: str
) -> None:
    definition = _create(tmp_path / "notebook.py")

    with pytest.raises(GroupError, match=message):
        run_group(definition.path, inputs=inputs)


def test_group_cannot_request_an_output_outside_its_saved_contract(tmp_path: Path) -> None:
    definition = _create(tmp_path / "notebook.py")

    with pytest.raises(GroupError, match="must match"):
        run_group(
            definition.path,
            inputs={"value": 2, "factor": 3},
            outputs=("other",),
        )


def test_group_rejects_a_declared_output_missing_from_the_marimo_run(tmp_path: Path) -> None:
    definition = GroupLibrary(tmp_path / "notebook.py").create(
        {
            "group_id": "missing_output",
            "module": "missing_output",
            "name": "Missing Output",
            "inputs": [],
            "outputs": ["result"],
            "body_source": """
@app.cell
def calculate():
    other = 1
    return other,
""",
        }
    )

    with pytest.raises(GroupError, match="did not define declared outputs"):
        run_group(definition.path, inputs={})


def test_group_with_no_ports_returns_an_empty_opaque_result(tmp_path: Path) -> None:
    definition = GroupLibrary(tmp_path / "notebook.py").create(
        {
            "group_id": "no_ports",
            "module": "no_ports",
            "name": "No Ports",
            "inputs": [],
            "outputs": [],
        }
    )

    result = run_group(definition.path, inputs={}, outputs=())

    assert isinstance(result, GroupResult)
    assert dict(result) == {}


def test_invalid_contracts_and_managed_input_drift_are_reported(tmp_path: Path) -> None:
    with pytest.raises(GroupError, match="Python identifier"):
        GroupContract("invalid-name", "Broken", "not-valid", (), ())

    definition = _create(tmp_path / "notebook.py")
    source = definition.path.read_text(encoding="utf-8").replace(
        "return value, factor", "return value"
    )
    definition.path.write_text(source, encoding="utf-8")
    listed = GroupLibrary(tmp_path / "notebook.py").list()

    assert listed["items"] == []
    assert listed["diagnostics"][0]["code"] == "group_definition_invalid"
    assert "managed Group input cell" in listed["diagnostics"][0]["message"]


def test_graphimo_application_catalog_creates_and_navigates_nested_groups(
    tmp_path: Path,
) -> None:
    host = GraphimoHostAPI("group-project", tmp_path / "notebook.py")
    application = GraphimoApplication(host)
    first = application.execute(
        "group.create",
        {
            "request": {
                "group_id": "data_prep",
                "module": "data_prep",
                "name": "Data Preparation",
                "inputs": ["dataset"],
                "outputs": ["cleaned"],
            }
        },
    )
    second = application.execute(
        "group.create",
        {
            "group_id": "training",
            "module": "training",
            "name": "Training",
            "inputs": ["dataset"],
            "outputs": ["model", "metrics"],
        },
    )

    catalog = application.execute("group.list", {})
    inspected = application.execute("group.inspect", {"group_id": first["group_id"]})
    first_scope = application.execute("scope.enter", {"container_id": first["group_id"]})
    nested_scope = application.execute("scope.enter", {"container_id": second["group_id"]})
    saved_layout = application.execute(
        "layout.set",
        {
            "scope_id": nested_scope["scope"]["scope_id"],
            "layout": {"train": {"x": 10, "y": 20, "width": 240, "height": 120}},
        },
    )
    restored_scope = application.execute("scope.leave", {})
    root_scope = application.execute("scope.leave", {})

    assert {item["group_id"] for item in catalog["items"]} == {
        "data_prep",
        "training",
    }
    assert inspected["inputs"] == ["dataset"]
    assert inspected["outputs"] == ["cleaned"]
    assert first_scope["scope"]["dialect_id"] == "python"
    assert len(nested_scope["breadcrumbs"]) == 3
    assert nested_scope["scope"]["dialect_id"] == first_scope["scope"]["dialect_id"]
    assert saved_layout == {"saved": True}
    assert restored_scope["scope"]["container_id"] == first["group_id"]
    assert root_scope["scope"]["scope_id"] == "root"
    assert host.read_layout(nested_scope["scope"]["scope_id"])["train"]["x"] == 10

    with pytest.raises(ValueError, match="root"):
        application.execute("scope.leave", {})


def test_group_scope_identity_is_stable_across_name_changes(tmp_path: Path) -> None:
    host = GraphimoHostAPI("group-project", tmp_path / "notebook.py")
    application = GraphimoApplication(host)
    created = application.execute(
        "group.create",
        {
            "request": {
                "group_id": "stable_group",
                "module": "stable_group",
                "name": "Original Name",
                "inputs": [],
                "outputs": [],
            }
        },
    )
    entered = application.execute("scope.enter", {"container_id": created["group_id"]})
    scope_id = entered["scope"]["scope_id"]

    updated = application.execute(
        "group.set_contract",
        {
            "group_id": created["group_id"],
            "contract": {"name": "Renamed Group"},
        },
    )
    application.execute("scope.leave", {})
    reopened = application.execute("scope.enter", {"container_id": created["group_id"]})

    assert updated["group_id"] == created["group_id"]
    assert reopened["scope"]["name"] == "Renamed Group"
    assert reopened["scope"]["scope_id"] == scope_id


def test_group_contract_updates_preserve_workflow_cells(tmp_path: Path) -> None:
    definition = _create(tmp_path / "notebook.py")
    library = GroupLibrary(tmp_path / "notebook.py")
    updated = library.update_contract(
        definition.contract.group_id,
        {"name": "Multiply Values", "outputs": ["result"]},
    )

    assert updated.contract.name == "Multiply Values"
    assert "@app.cell\ndef calculate(value, factor):" in updated.path.read_text(encoding="utf-8")
    assert run_group(updated.path, inputs={"value": 3, "factor": 5}).result == 15


def test_application_creates_group_from_selection_and_preserves_crossing_ports(
    tmp_path: Path,
) -> None:
    application = GraphimoApplication(GraphimoHostAPI("selected-group", tmp_path / "main.py"))
    cells = [
        {"id": "input", "source": "raw = 2", "defs": ["raw"], "parents": []},
        {
            "id": "clean",
            "source": "cleaned = raw + 1",
            "defs": ["cleaned"],
            "parents": [{"cell_id": "input", "variables": ["raw"]}],
        },
        {
            "id": "scale",
            "source": "result = cleaned * 3",
            "defs": ["result"],
            "parents": [{"cell_id": "clean", "variables": ["cleaned"]}],
        },
        {
            "id": "sink",
            "source": "final = result + 4",
            "defs": ["final"],
            "parents": [{"cell_id": "scale", "variables": ["result"]}],
        },
    ]

    created = application.execute(
        "group.create_from_selection",
        {
            "name": "Clean and Scale",
            "module": "clean_and_scale",
            "cells": cells,
            "selected_ids": ["clean", "scale"],
        },
    )

    assert created["inputs"] == ["raw"]
    assert created["outputs"] == ["result"]
    assert created["selected_ids"] == ["clean", "scale"]
    assert "raw=raw" in created["parent_source"]
    assert "result = __graphimo_group_result_" in created["parent_source"]
    group = GroupLibrary(tmp_path / "main.py").get(created["group"]["group_id"])
    assert group.contract.inputs == ("raw",)
    assert group.contract.outputs == ("result",)
    assert run_group(group.path, inputs={"raw": 2}).result == 9


def test_group_selection_rejects_stale_cells_without_creating_a_file(tmp_path: Path) -> None:
    application = GraphimoApplication(GraphimoHostAPI("stale-group", tmp_path / "main.py"))

    with pytest.raises(ValueError, match="stale"):
        application.execute(
            "group.create_from_selection",
            {
                "name": "Stale",
                "module": "stale_group",
                "cells": [{"id": "one", "source": "x = 1", "defs": ["x"], "parents": []}],
                "selected_ids": ["one", "two"],
            },
        )

    assert not (tmp_path / "groups" / "stale_group.py").exists()
