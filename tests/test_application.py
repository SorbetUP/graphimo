import pytest

from graphimo.application import ApplicationError, GraphimoApplication
from graphimo.instance import build_instance_source, parse_instance_source
from graphimo.instance import disable_instance_source


def _write_nodes(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "nodes.py").write_text(
        "from graphimo import node\n"
        "@node\n"
        "def scale(x: float, factor: float = 1.0) -> float:\n"
        "    result = x * factor\n"
        "    return result\n",
        encoding="utf-8",
    )


def _application(project, host):
    app = GraphimoApplication(host.marimo, project.components, layout_path=project.layout)
    host.marimo.snapshot["notebook"]["path"] = str(project.notebook)
    host.marimo.sessions[0]["path"] = str(project.notebook)
    return app


def test_application_opens_project_and_projects_typed_view_state(project, host):
    _write_nodes(project.components)
    source, instance = build_instance_source("scale", {"x": "2.0", "factor": "3.0"}, instance_id="4f2a7c91")
    host.marimo.snapshot["cells"] = [
        {"id": "scale-cell", "name": None, "source": source, "defs": [instance.result_var], "refs": [], "status": "success"}
    ]
    host.marimo.snapshot["edges"] = []
    host.marimo.values[instance.result_var] = 6.0
    app = _application(project, host)

    state = app.open_project(project.notebook)

    assert state.project_id == str(project.notebook)
    assert state.active_scope_id == "root"
    assert "scale" in [item.title for item in state.palette.items]
    assert state.palette.dialect_id == "python"
    block = state.blocks[0]
    assert block.kind == "node_instance"
    assert block.title == "scale"
    assert block.outputs[0].declared_type == "float"
    assert block.outputs[0].runtime_type == "float"
    assert block.outputs[0].preview == "6.0"
    assert block.outputs[0].expression == "result"
    assert block.inputs[0].connected is False
    assert block.inputs[0].expression == "2.0"
    assert "connect" in state.available_actions
    assert "disable_instance" in state.available_actions


def test_application_inspects_native_artifact_store_without_loading_payload(project, host):
    from graphimo import artifacts

    store = artifacts.configure_default_store(project.root / "artifacts")
    ref = artifacts.put_bytes(b"persisted", kind="dataset")
    app = _application(project, host)

    usage = app.inspect_artifact_store()

    assert usage.logical_bytes == len(b"persisted")
    assert usage.physical_stored_bytes == len(b"persisted")
    assert usage.artifacts == 1
    assert store.get(ref.id) == ref


def test_application_adds_repeated_instances_as_separate_marimo_cells(project, host):
    _write_nodes(project.components)
    app = _application(project, host)
    app.open_project(project.notebook)

    first = app.add_node("nodes.py", "scale", {"x": "data", "factor": "2.0"}, position=(100, 120))
    second = app.add_node("nodes.py", "scale", {"x": "data", "factor": "3.0"}, position=(440, 120))

    assert first["cell_id"] != second["cell_id"]
    assert first["result_var"] != second["result_var"]
    assert len(host.marimo.added) == 2
    assert all(parse_instance_source(source) is not None for _, source in host.marimo.added)
    layout = project.layout.read_text(encoding="utf-8")
    assert '"x": 100' in layout and '"x": 440' in layout


def test_application_connect_mutates_only_target_instance(project, host):
    _write_nodes(project.components)
    first_source, first_instance = build_instance_source(
        "scale", {"x": "data", "factor": "2.0"}, instance_id="11111111"
    )
    second_source, second_instance = build_instance_source(
        "scale", {"x": "other", "factor": "3.0"}, instance_id="22222222"
    )
    host.marimo.snapshot["cells"] = [
        {"id": "a", "source": first_source, "defs": [first_instance.result_var], "refs": ["data"]},
        {"id": "b", "source": second_source, "defs": [second_instance.result_var], "refs": ["other"]},
    ]
    app = _application(project, host)
    app.open_project(project.notebook)

    result = app.connect("a", "b", input_name="x")

    assert f"x={first_instance.result_var}" in result["source"]
    assert host.marimo.replaced == [(app.session_id, "b", result["source"])]
    assert host.marimo.snapshot["cells"][0]["source"] == first_source


def test_application_edits_literals_duplicates_and_disconnects(project, host):
    _write_nodes(project.components)
    source, instance = build_instance_source(
        "scale", {"x": "data", "factor": "2.0"}, instance_id="33333333"
    )
    host.marimo.snapshot["cells"] = [
        {"id": "node-a", "source": source, "defs": [instance.result_var], "refs": ["data"]}
    ]
    app = _application(project, host)
    app.open_project(project.notebook)

    edited = app.set_literal("node-a", "factor", [2, 3])
    assert "factor=[2, 3]" in edited["source"]
    copied = app.duplicate_elements(["node-a"])
    assert copied[0]["instance_id"] != instance.instance_id
    assert copied[0]["result_var"] != instance.result_var
    disconnected = app.disconnect("node-a", input_name="x")
    assert "x=" not in disconnected["source"]


def test_disabling_instance_keeps_it_visible_and_marks_downstream_stale(project, host):
    _write_nodes(project.components)
    upstream, source_instance = build_instance_source(
        "scale", {"x": "data", "factor": "2.0"}, instance_id="44444444"
    )
    downstream, target_instance = build_instance_source(
        "scale", {"x": source_instance.result_var, "factor": "3.0"}, instance_id="55555555"
    )
    host.marimo.snapshot["cells"] = [
        {"id": "source", "source": disable_instance_source(upstream), "defs": [], "refs": [], "status": "idle"},
        {"id": "target", "source": downstream, "defs": [target_instance.result_var], "refs": [], "status": "success"},
    ]
    host.marimo.snapshot["edges"] = []
    app = _application(project, host)
    app.open_project(project.notebook)

    state = app.refresh_project()

    source_block = next(block for block in state.blocks if block.id == "source")
    target_block = next(block for block in state.blocks if block.id == "target")
    assert source_block.state == "disabled"
    assert source_block.can_enable and not source_block.can_disable
    assert source_block.outputs[0].preview == "unavailable · disabled"
    assert target_block.state == "stale"
    assert any(edge.source_block_id == "source" and edge.target_block_id == "target" and edge.state == "stale" for edge in state.edges)

    with pytest.raises(ApplicationError, match="Enable"):
        app.run_target("source")


def test_application_reports_unavailable_or_invalid_commands(project, host):
    app = _application(project, host)
    try:
        app.refresh_project()
    except ApplicationError as exc:
        assert exc.code == "project_not_open"
    else:
        raise AssertionError("expected project_not_open")

    _write_nodes(project.components)
    app.open_project(project.notebook)
    try:
        app.add_node("nodes.py", "scale", {}, position=(float("nan"), 10))
    except ApplicationError as exc:
        assert exc.code == "invalid_position"
    else:
        raise AssertionError("expected invalid_position")
