from __future__ import annotations

import ast
import runpy
import textwrap

import pytest

from graphimo.application import GraphimoApplication
from graphimo.controls import control_catalog, create_control_source
from graphimo.metablocks import GraphimoHostAPI


@pytest.mark.parametrize(
    ("kind", "method"),
    [
        ("slider", "slider"),
        ("range_slider", "range_slider"),
        ("number", "number"),
        ("dropdown", "dropdown"),
        ("checkbox", "checkbox"),
        ("text", "text"),
        ("run_button", "run_button"),
    ],
)
def test_control_source_is_two_valid_reactive_marimo_cells(kind: str, method: str) -> None:
    created = create_control_source({"kind": kind, "identity": f"{kind}_test_1"})

    ast.parse(created["control_source"])
    ast.parse(created["value_source"])
    assert f".ui.{method}(" in created["control_source"]
    assert f"{created['control_name']}.value" in created["value_source"]
    assert created["value_name"] in created["value_source"]


def test_control_catalog_lists_only_python_controls() -> None:
    catalog = control_catalog()

    assert {item["kind"] for item in catalog["items"]} == {
        "slider",
        "range_slider",
        "number",
        "dropdown",
        "checkbox",
        "text",
        "run_button",
    }
    assert all(item["dialect_id"] == "python" for item in catalog["items"])


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("slider", 0.2),
        ("range_slider", [0.25, 0.75]),
        ("number", 0.0),
        ("dropdown", "option_a"),
        ("checkbox", False),
        ("text", ""),
        ("run_button", False),
    ],
)
def test_generated_control_cells_run_in_pinned_marimo(
    tmp_path, kind: str, expected: object
) -> None:
    marimo = pytest.importorskip("marimo")
    created = create_control_source({"kind": kind, "identity": f"{kind}_live"})
    widget_cell = textwrap.indent(created["control_source"], "    ")
    value_cell = textwrap.indent(created["value_source"], "    ")
    notebook_source = f"""import marimo
app = marimo.App()

@app.cell
def _():
{widget_cell}
    return ({created["control_name"]},)

@app.cell
def _({created["control_name"]}):
{value_cell}
    return ({created["value_name"]},)
"""
    notebook_path = tmp_path / f"{kind}.py"
    notebook_path.write_text(notebook_source, encoding="utf-8")
    namespace = runpy.run_path(str(notebook_path))

    values, _outputs = namespace["app"].run()

    assert values[-1] == expected
    assert marimo.__version__ == "0.25.2"


@pytest.mark.parametrize(
    ("request_data", "message"),
    [
        ({"kind": "not_a_control", "identity": "control"}, "unsupported Graphimo control"),
        ({"kind": "slider", "identity": "invalid-name"}, "safe Python identifier"),
        (
            {"kind": "slider", "identity": "s", "parameters": {"start": 1, "stop": 0}},
            "start < stop",
        ),
        (
            {
                "kind": "range_slider",
                "identity": "r",
                "parameters": {"value": [0.8, 0.2]},
            },
            "ordered pair",
        ),
        (
            {"kind": "dropdown", "identity": "d", "parameters": {"value": "missing"}},
            "one of its choices",
        ),
        (
            {"kind": "checkbox", "identity": "c", "parameters": {"value": 1}},
            "must be boolean",
        ),
    ],
)
def test_invalid_control_contracts_are_rejected(
    request_data: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        create_control_source(request_data)


def test_graphimo_application_compiles_control_source(tmp_path) -> None:
    application = GraphimoApplication(GraphimoHostAPI("controls", tmp_path / "notebook.py"))

    catalog = application.execute("control.catalog", {})
    created = application.execute(
        "control.create",
        {"kind": "slider", "identity": "threshold", "parameters": {"label": "cutoff"}},
    )

    assert any(item["kind"] == "slider" for item in catalog["items"])
    assert created["label"] == "cutoff"
    assert created["value_name"] == "g_threshold_value"
    assert "label='cutoff'" in created["control_source"]
