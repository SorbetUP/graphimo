from __future__ import annotations

from graphimo.application import ApplicationError, GraphimoApplication
from graphimo.groups import GroupPort, create_group_file, parse_group_instance_source


def _write_group(components, filename="pipeline.py"):
    path = components / filename
    create_group_file(
        path,
        group_id=filename.removesuffix(".py"),
        name="Pipeline",
        inputs=[GroupPort("dataset", "dataset", "list")],
        outputs=[GroupPort("model", "model", "str")],
    )
    return path


def test_group_appears_in_palette_and_adds_a_referenced_marimo_instance(project, host):
    group_path = _write_group(project.components)
    app = GraphimoApplication(host.marimo, project.components, layout_path=project.layout)
    host.marimo.snapshot["notebook"]["path"] = str(project.notebook)
    host.marimo.sessions[0]["path"] = str(project.notebook)
    app.open_project(project.notebook)

    item = next(item for item in app.refresh_project().palette.items if item.kind == "group")
    assert item.id == "group:pipeline.py"
    assert item.input_schema[0]["name"] == "dataset"

    created = app.add_palette_item(
        item.id, bindings={"dataset": "dataset_a.data"}, position=(120, 140)
    )
    group_source = host.marimo.added[-1][1]
    instance = parse_group_instance_source(group_source)
    assert instance is not None
    assert instance.path == str(group_path.resolve())
    assert "graphimo.groups.run_group" in group_source
    assert host.marimo.added[0][1] == "# Graphimo group runtime\nimport graphimo"
    assert created["result_var"] == instance.result_var
    assert '"x": 120' in project.layout.read_text(encoding="utf-8")


def test_enter_and_leave_group_use_application_scope_stack(project, host):
    group_path = _write_group(project.components)
    app = GraphimoApplication(host.marimo, project.components, layout_path=project.layout)
    host.marimo.snapshot["notebook"]["path"] = str(project.notebook)
    host.marimo.sessions[0]["path"] = str(project.notebook)
    app.open_project(project.notebook)
    host.marimo.sessions.append({"session_id": "group-session", "path": str(group_path)})
    original_inspect = host.marimo.inspect

    def inspect(session_id):
        if session_id == "group-session":
            snapshot = original_inspect("fixture")
            snapshot["notebook"]["path"] = str(group_path)
            snapshot["notebook"]["session_id"] = session_id
            return snapshot
        return original_inspect(session_id)

    host.marimo.inspect = inspect
    entered = app.enter_group("pipeline.py")
    assert entered.active_scope_id == "pipeline"
    assert [crumb.title for crumb in entered.breadcrumbs] == [project.notebook.name, "Pipeline"]
    left = app.leave_scope()
    assert left.active_scope_id == "root"
    assert app.session_id == "fixture"


def test_enter_group_requires_an_active_marimo_session(project, host):
    _write_group(project.components)
    app = GraphimoApplication(host.marimo, project.components, layout_path=project.layout)
    host.marimo.snapshot["notebook"]["path"] = str(project.notebook)
    host.marimo.sessions[0]["path"] = str(project.notebook)
    app.open_project(project.notebook)

    try:
        app.enter_group("pipeline.py")
    except ApplicationError as exc:
        assert exc.code == "group_session_required"
    else:
        raise AssertionError("expected an active marimo Group session to be required")


def test_groups_connect_by_named_output_and_can_disconnect_required_inputs(project, host):
    group_path = _write_group(project.components)
    app = GraphimoApplication(host.marimo, project.components, layout_path=project.layout)
    host.marimo.snapshot["notebook"]["path"] = str(project.notebook)
    host.marimo.sessions[0]["path"] = str(project.notebook)
    app.open_project(project.notebook)
    first = app.add_group_instance("pipeline.py", {"dataset": "source.data"})
    second = app.add_group_instance("pipeline.py", {"dataset": "other.data"})
    host.marimo.snapshot["cells"] = [
        {"id": first["cell_id"], "source": first["source"], "defs": [first["result_var"]]},
        {"id": second["cell_id"], "source": second["source"], "defs": [second["result_var"]]},
    ]

    connected = app.connect(first["cell_id"], second["cell_id"], input_name="dataset")

    assert f"'dataset': {first['result_var']}.model" in connected["source"]
    disconnected = app.disconnect(second["cell_id"], input_name="dataset")
    parsed = parse_group_instance_source(disconnected["source"])
    assert parsed is not None and parsed.bindings == ()
    assert group_path.exists()


def test_palette_catalogs_group_files_without_importing_them(project, host):
    group_path = _write_group(project.components)
    with group_path.open("a", encoding="utf-8") as stream:
        stream.write("\nraise RuntimeError('catalog must not execute Group files')\n")
    app = GraphimoApplication(host.marimo, project.components, layout_path=project.layout)
    host.marimo.snapshot["notebook"]["path"] = str(project.notebook)
    host.marimo.sessions[0]["path"] = str(project.notebook)
    app.open_project(project.notebook)

    items = app.refresh_project().palette.items

    assert any(item.kind == "group" and item.title == "Pipeline" for item in items)


def test_group_view_projects_declared_ports_and_named_runtime_outputs(project, host):
    _write_group(project.components)
    app = GraphimoApplication(host.marimo, project.components, layout_path=project.layout)
    host.marimo.snapshot["notebook"]["path"] = str(project.notebook)
    host.marimo.sessions[0]["path"] = str(project.notebook)
    app.open_project(project.notebook)
    created = app.add_group_instance("pipeline.py", {"dataset": "dataset_a.data"})
    host.marimo.snapshot["cells"] = [
        {"id": created["cell_id"], "source": created["source"], "status": "success"}
    ]
    host.marimo.values[created["result_var"]] = {"model": "trained-model"}

    state = app.refresh_project()
    inspected = app.open_inspector(created["cell_id"])
    block = state.blocks[0]

    assert block.kind == "group_instance"
    assert block.inputs[0].name == "dataset"
    assert block.outputs[0].name == "model"
    assert block.outputs[0].preview == "trained-model"
    assert inspected["outputs"][0]["expression"] == f"{created['result_var']}.model"
