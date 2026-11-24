from __future__ import annotations

import adapter.graphimo_adapter as adapter
from adapter.graphimo_adapter import dispatch


def assert_error(result, code: str, cell_id=None):
    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert isinstance(result["error"]["message"], str)
    assert result["error"]["cell_id"] == cell_id


def test_inspect_projects_all_marimo_cells_and_current_edges(host):
    result = dispatch({"op": "inspect", "session_id": "fixture"}, host.context)

    assert result["ok"] is True
    assert result["notebook"] == {"path": "fixture.py", "session_id": "fixture"}
    assert [cell["id"] for cell in result["cells"]] == [
        "load", "normalize", "arbitrary", "empty", "markdown"
    ]
    arbitrary = next(cell for cell in result["cells"] if cell["id"] == "arbitrary")
    assert arbitrary["source"] == "x = complicated_custom_code(processed)"
    assert arbitrary["defs"] == ["x"]
    assert "processed" in arbitrary["refs"]
    assert result["edges"] == host.marimo.snapshot["edges"]


def test_inspect_reads_fresh_marimo_graph_instead_of_cached_edges(host):
    first = dispatch({"op": "inspect", "session_id": "fixture"}, host.context)
    host.marimo.snapshot["edges"] = [
        {"source_cell_id": "load", "target_cell_id": "arbitrary", "variable": "data"}
    ]
    second = dispatch({"op": "inspect", "session_id": "fixture"}, host.context)

    assert first["edges"] != second["edges"]
    assert second["edges"] == host.marimo.snapshot["edges"]


def test_inspect_requires_explicit_session_when_more_than_one_is_active(host):
    host.marimo.sessions.append({"session_id": "other", "path": "other.py"})

    result = dispatch({"op": "inspect"}, host.context)

    assert_error(result, "selection_required")
    choices = result.get("sessions", result.get("choices"))
    assert {item["session_id"] for item in choices} == {"fixture", "other"}


def test_inspect_resolves_the_only_active_session(host):
    result = dispatch({"op": "inspect"}, host.context)

    assert result["ok"] is True
    assert result["notebook"]["session_id"] == "fixture"


def test_stale_cell_id_has_common_error_envelope(host):
    result = dispatch(
        {"op": "run", "session_id": "fixture", "cell_id": "deleted-cell"},
        host.context,
    )

    assert_error(result, "cell_not_found", "deleted-cell")
    assert host.marimo.runs == []


def test_run_delegates_to_marimo_and_returns_request_state(host):
    result = dispatch(
        {"op": "run", "session_id": "fixture", "cell_id": "normalize"},
        host.context,
    )

    assert host.marimo.runs == [("fixture", "normalize")]
    assert result["ok"] is True
    assert result["status"] == "queued"
    assert result["request_id"] == "run-1"


def test_open_code_uses_the_injected_zeron_navigation_callback(host):
    result = dispatch(
        {"op": "open_code", "session_id": "fixture", "cell_id": "arbitrary"},
        host.context,
    )

    assert result == {"ok": True, "opened": True}
    assert host.opened == ["arbitrary"]


def test_open_code_unavailable_is_a_clear_error(project, host):
    host.context.open_code = None

    result = dispatch(
        {"op": "open_code", "session_id": "fixture", "cell_id": "arbitrary"},
        host.context,
    )

    assert_error(result, "open_code_unavailable", "arbitrary")


def test_unknown_operation_returns_invalid_request(host):
    result = dispatch({"op": "not-an-operation"}, host.context)

    assert_error(result, "invalid_request")


def test_add_component_inserts_readable_python_with_requested_bindings(project, host):
    result = dispatch(
        {
            "op": "add_component",
            "session_id": "fixture",
            "component_id": "data-processing/normalize.py",
            "callable": "normalize",
            "output_name": "normalized",
            "bindings": {"image": {"variable": "data"}, "mean": {"literal": 0.25}},
        },
        host.context,
    )

    assert result["ok"] is True
    assert result["cell_id"] == "inserted-cell-1"
    assert result["source"] == host.marimo.added[0][1]
    parsed = adapter.parse_instance_source(result["source"])
    assert parsed is not None
    assert parsed.node_name == "normalize"
    assert parsed.result_var.startswith("g_normalize_")
    assert [(binding.input_name, binding.expression) for binding in parsed.bindings] == [
        ("image", "data"), ("mean", "0.25")
    ]
    compile(result["source"], "<inserted-cell>", "exec")


def test_add_component_can_select_one_callable_from_a_multi_export_package(project, host):
    result = dispatch(
        {
            "op": "add_component",
            "session_id": "fixture",
            "component_id": "models/bodypart",
            "callable": "encode",
            "output_name": "encoded",
            "bindings": {"value": {"variable": "data"}},
        },
        host.context,
    )

    assert result["ok"] is True
    assert result["cell_id"] == "inserted-cell-1"
    parsed = adapter.parse_instance_source(result["source"])
    assert parsed is not None and parsed.node_name == "encode"
    assert host.marimo.added == [("fixture", result["source"])]


def test_unknown_component_and_callable_return_defined_errors(project, host):
    missing_component = dispatch(
        {
            "op": "add_component", "session_id": "fixture",
            "component_id": "missing.py", "callable": "run",
            "output_name": "result", "bindings": {},
        },
        host.context,
    )
    assert_error(missing_component, "component_not_found")

    missing_callable = dispatch(
        {
            "op": "add_component", "session_id": "fixture",
            "component_id": "data-processing/normalize.py", "callable": "absent",
            "output_name": "result", "bindings": {},
        },
        host.context,
    )
    assert_error(missing_callable, "callable_not_found")
    assert host.marimo.added == []


def test_connect_rejects_arbitrary_python_as_graphimo_instance(host):
    host.marimo.snapshot["cells"][2]["source"] = "prediction = model(input_data)"
    host.marimo.snapshot["cells"][2]["refs"] = ["model", "input_data"]

    result = dispatch(
        {
            "op": "connect", "session_id": "fixture", "from_cell_id": "normalize",
            "variable": "processed", "to_cell_id": "arbitrary", "input_ref": "input_data",
        },
        host.context,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "unsafe_edit"
    assert host.marimo.replaced == []


def test_connect_refuses_ambiguous_or_unsupported_source_without_editing(host):
    unsupported = [
        "prediction = model(input_data, fallback=input_data)",
        "prediction = model(input_data)\nother = 1",
        "prediction = model(other)",
        "this is not python (",
    ]
    for source in unsupported:
        host.marimo.snapshot["cells"][2]["source"] = source
        result = dispatch(
            {
                "op": "connect", "session_id": "fixture", "from_cell_id": "normalize",
                "variable": "processed", "to_cell_id": "arbitrary", "input_ref": "input_data",
            },
            host.context,
        )
        assert_error(result, "unsafe_edit", "arbitrary")

    assert host.marimo.replaced == []


def test_list_components_finds_plain_file_and_package_public_exports(project, host):
    result = dispatch({"op": "list_components"}, host.context)

    assert result["ok"] is True
    items = {item["id"]: item for item in result["components"]}
    file_item = items["data-processing/normalize.py"]
    assert file_item["path"] == "data-processing/normalize.py"
    assert file_item["category"] == ["data-processing"]
    assert file_item["kind"] == "file"
    normalize = next(item for item in file_item["callables"] if item["name"] == "normalize")
    assert normalize["signature"] == "(image, mean=0.5, std=0.5)"
    assert normalize["description"] == "Scale an image."

    package_item = items["models/bodypart"]
    assert package_item["kind"] == "package"
    assert {item["name"] for item in package_item["callables"]} == {"encode", "decode"}
    assert "_private" not in {item["name"] for item in package_item["callables"]}


def test_missing_component_root_returns_error_instead_of_empty_success(project, host, tmp_path):
    result = dispatch({"op": "list_components", "root": str(tmp_path / "absent")}, host.context)

    assert_error(result, "component_root_not_found")
