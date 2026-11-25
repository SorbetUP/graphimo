from __future__ import annotations

import ast

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
        "load",
        "normalize",
        "arbitrary",
        "empty",
        "markdown",
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
    assert result["cell_id"] == "inserted-cell"
    assert result["source"] == host.marimo.added[-1][1]
    assert result["source"].startswith("# Graphimo node: normalize\n# Graphimo instance: ")
    assert len(host.marimo.added) == 2  # one shared library import plus its first instance
    tree = ast.parse(result["source"])
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign))
    assert [target.id for target in assignment.targets if isinstance(target, ast.Name)] == [
        result["result_var"]
    ]
    assert isinstance(assignment.value, ast.Call)
    called_name = (
        assignment.value.func.id
        if isinstance(assignment.value.func, ast.Name)
        else assignment.value.func.attr
    )
    assert called_name == "normalize"
    names = {node.id for node in ast.walk(assignment.value) if isinstance(node, ast.Name)}
    assert "data" in names
    assert any(
        isinstance(node, ast.Constant) and node.value == 0.25 for node in ast.walk(assignment.value)
    )
    compile(result["source"], "<inserted-cell>", "exec")
    namespace = {"data": 5.0}
    exec(host.marimo.added[0][1], namespace)  # noqa: S102 - exercise generated marimo cell source
    exec(result["source"], namespace)  # noqa: S102 - exercise generated marimo cell source
    assert namespace[result["result_var"]] == 9.5

    second = dispatch(
        {
            "op": "add_component",
            "session_id": "fixture",
            "component_id": "data-processing/normalize.py",
            "callable": "normalize",
            "output_name": "ignored_legacy_output",
            "bindings": {"image": {"variable": "data"}},
        },
        host.context,
    )
    assert second["ok"] is True
    assert len(host.marimo.added) == 3  # the library definition is shared by both instances


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
    assert result["cell_id"] == "inserted-cell"
    expression = next(
        node.value for node in ast.parse(result["source"]).body if isinstance(node, ast.Assign)
    )
    assert isinstance(expression, ast.Call)
    called_name = (
        expression.func.id if isinstance(expression.func, ast.Name) else expression.func.attr
    )
    assert called_name == "encode"
    assert host.marimo.added[-1] == ("fixture", result["source"])
    assert len(host.marimo.added) == 2


def test_add_component_refuses_helpers_and_missing_required_inputs(project, host):
    helper = dispatch(
        {
            "op": "add_component",
            "session_id": "fixture",
            "component_id": "data-processing/normalize.py",
            "callable": "helper",
            "output_name": "result",
            "bindings": {"value": {"variable": "data"}},
        },
        host.context,
    )
    assert_error(helper, "node_required")

    missing = dispatch(
        {
            "op": "add_component",
            "session_id": "fixture",
            "component_id": "data-processing/normalize.py",
            "callable": "normalize",
            "output_name": "result",
            "bindings": {},
        },
        host.context,
    )
    assert_error(missing, "missing_required_binding")
    assert host.marimo.added == []


def test_unknown_component_and_callable_return_defined_errors(project, host):
    missing_component = dispatch(
        {
            "op": "add_component",
            "session_id": "fixture",
            "component_id": "missing.py",
            "callable": "run",
            "output_name": "result",
            "bindings": {},
        },
        host.context,
    )
    assert_error(missing_component, "component_not_found")

    missing_callable = dispatch(
        {
            "op": "add_component",
            "session_id": "fixture",
            "component_id": "data-processing/normalize.py",
            "callable": "absent",
            "output_name": "result",
            "bindings": {},
        },
        host.context,
    )
    assert_error(missing_callable, "callable_not_found")
    assert host.marimo.added == []


def test_connect_replaces_one_simple_positional_reference_in_real_source(host):
    from graphimo import build_instance_source

    target_source, _ = build_instance_source(
        "normalize", {"image": "input_data"}, instance_id="aabbccdd"
    )
    host.marimo.snapshot["cells"][2]["source"] = target_source
    host.marimo.snapshot["cells"][2]["refs"] = ["model", "input_data"]

    result = dispatch(
        {
            "op": "connect",
            "session_id": "fixture",
            "from_cell_id": "normalize",
            "variable": "processed",
            "to_cell_id": "arbitrary",
            "input_name": "image",
        },
        host.context,
    )

    assert result["ok"] is True
    assert result["changed"] is True
    assert "image=processed" in result["source"]
    assert host.marimo.replaced == [("fixture", "arbitrary", result["source"])]


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
                "op": "connect",
                "session_id": "fixture",
                "from_cell_id": "normalize",
                "variable": "processed",
                "to_cell_id": "arbitrary",
                "input_ref": "input_data",
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
    assert normalize["graphimo_node"] is True
    assert normalize["insertable"] is True
    assert normalize["outputs"] == [
        {"index": 0, "declared_type": None, "expressions": ["(image - mean) / std"]}
    ]
    helper = next(item for item in file_item["callables"] if item["name"] == "helper")
    assert helper["graphimo_node"] is False
    assert helper["insertable"] is False

    package_item = items["models/bodypart"]
    assert package_item["kind"] == "package"
    assert {item["name"] for item in package_item["callables"]} == {"encode", "decode"}
    assert "_private" not in {item["name"] for item in package_item["callables"]}


def test_missing_component_root_returns_error_instead_of_empty_success(project, host, tmp_path):
    result = dispatch({"op": "list_components", "root": str(tmp_path / "absent")}, host.context)

    assert_error(result, "component_root_not_found")


def test_inspect_node_instance_reads_live_result_through_marimo_port(host):
    from graphimo import build_instance_source

    source, instance = build_instance_source(
        "normalize",
        {"image": "data", "mean": "0.0", "std": "1.0"},
        instance_id="aabbccdd",
    )
    host.marimo.snapshot["cells"][2]["source"] = source
    host.marimo.values[instance.result_var] = 4.2
    result = dispatch(
        {"op": "inspect_node_instance", "session_id": "fixture", "cell_id": "arbitrary"},
        host.context,
    )
    assert result["ok"] is True
    assert result["instance_id"] == "aabbccdd"
    assert result["node"] == "normalize"
    assert result["outputs"][0]["runtime_type"] == "float"
    assert result["outputs"][0]["preview"] == "4.2"
    assert result["outputs"][0]["expressions"] == ["(image - mean) / std"]


def test_connect_uses_declared_multi_output_expression(project, host):
    from graphimo import build_instance_source

    source_path = project.components / "pair.py"
    source_path.write_text(
        "from graphimo import node as _node\n\n"
        "@_node\n"
        "def split(value) -> tuple[int, int]:\n"
        "    return value, value\n",
        encoding="utf-8",
    )
    source, source_instance = build_instance_source(
        "split", {"value": "data"}, instance_id="bbaa9988"
    )
    target, _ = build_instance_source("normalize", {"image": "old_value"}, instance_id="aabbccdd")
    host.marimo.snapshot["cells"][0]["source"] = source
    host.marimo.snapshot["cells"][0]["defs"] = [source_instance.result_var]
    host.marimo.snapshot["cells"][2]["source"] = target
    result = dispatch(
        {
            "op": "connect",
            "session_id": "fixture",
            "from_cell_id": "load",
            "to_cell_id": "arbitrary",
            "variable": source_instance.result_var,
            "output_index": 1,
            "input_name": "image",
        },
        host.context,
    )
    assert result["ok"] is True
    assert "image=g_split_bbaa9988[1]" in result["source"]


def test_disabled_instance_keeps_stale_ports_and_connections(project, host):
    from graphimo import build_instance_source

    active, instance = build_instance_source(
        "normalize",
        {"image": "data", "mean": "0.0", "std": "1.0"},
        instance_id="aabbccdd",
    )
    host.marimo.snapshot["cells"][1]["source"] = active
    host.marimo.snapshot["cells"][1]["defs"] = [instance.result_var]
    host.marimo.snapshot["cells"][2]["refs"] = [instance.result_var]
    host.marimo.snapshot["edges"] = [
        {
            "source_cell_id": "normalize",
            "target_cell_id": "arbitrary",
            "variable": instance.result_var,
        }
    ]
    disabled = dispatch(
        {
            "op": "set_instance_enabled",
            "session_id": "fixture",
            "cell_id": "normalize",
            "enabled": False,
        },
        host.context,
    )
    assert disabled["ok"] is True
    assert "# graphimo:disabled-begin" in disabled["source"]

    snapshot = dispatch({"op": "inspect", "session_id": "fixture"}, host.context)
    cell = next(item for item in snapshot["cells"] if item["id"] == "normalize")
    assert cell["graphimo_disabled_instance"]["disabled"] is True
    assert cell["graphimo_disabled_instance"]["outputs"] == [
        {
            "index": 0,
            "declared_type": None,
            "expressions": ["(image - mean) / std"],
            "available": False,
            "stale": True,
        }
    ]
    assert snapshot["edges"][0]["status"] == "stale"

    enabled = dispatch(
        {
            "op": "set_instance_enabled",
            "session_id": "fixture",
            "cell_id": "normalize",
            "enabled": True,
        },
        host.context,
    )
    assert enabled["ok"] is True
    assert enabled["source"] == active
    reopened = dispatch({"op": "inspect", "session_id": "fixture"}, host.context)
    assert "graphimo_disabled_instance" not in next(
        item for item in reopened["cells"] if item["id"] == "normalize"
    )
