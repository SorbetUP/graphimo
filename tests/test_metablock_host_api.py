from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from graphimo.application import ApplicationError, GraphimoApplication
from graphimo.instance import build_instance_source
from graphimo.metablocks import MaterializationSpec, MetaBlockAddonHost


class FakeDialect:
    dialect_id = "test.dialect"

    def create_state(self, request, context):
        return {"metablock_id": request["metablock_id"], "name": request["name"], "revision": 0}

    def catalog(self, state, scope, context):
        return {"scope": scope, "items": [{"id": "fake.item"}]}

    def project(self, state, scope, context):
        return {"scope": scope, "name": state["name"], "revision": state["revision"]}

    def apply(self, state, intent, context):
        if intent.get("kind") == "reject":
            raise ValueError("rejected edit")
        updated = {**state, "revision": state["revision"] + 1}
        context.emit_event({"kind": "state_changed", "revision": updated["revision"]})
        return {"state": updated, "diagnostics": [], "projection_invalidated": True}

    def build_materialization(self, state, bindings, context):
        return MaterializationSpec(
            "model = make_model()",
            "model",
            "test.Model",
            state["name"],
            bindings,
            str(state["revision"]),
        )

    def invoke(self, state, action, args, context):
        context.diagnostic("fake_action", action)
        return {"action": action, "args": args}

    def drain_events(self, cursor, limit, context):
        return contextual_events(context, cursor, limit)


def contextual_events(context, cursor, limit):
    # Addons emit into the shared bounded host event queue; a cursor result is
    # returned through the same JSON event envelope as the future service.
    return context.drain_host_events(cursor, limit)


def _opened_app(project, host):
    host.marimo.snapshot["notebook"]["path"] = str(project.notebook)
    host.marimo.sessions[0]["path"] = str(project.notebook)
    app = GraphimoApplication(host.marimo, project.components, layout_path=project.layout)
    app.open_project(project.notebook)
    return app


def test_generic_metablock_handler_lifecycle_is_available_through_application(
    project, host, monkeypatch
):
    app = _opened_app(project, host)
    monkeypatch.setattr(
        "graphimo.metablock_application.load_metablock_registration",
        lambda _dialect: SimpleNamespace(api_version=1, factory=lambda _ctx: FakeDialect()),
    )

    state = app.create_metablock("test.dialect", {"name": "Example"}, "meta-1")
    assert state["revision"] == 0
    assert app.metablock_catalog("meta-1", {"id": "scope-a"})["items"][0]["id"] == "fake.item"
    assert app.project_metablock("meta-1")["name"] == "Example"
    context = app._metablock_context("test.dialect", "meta-1")
    assert context.load_sidecar() == state
    assert context.artifact_store is not None
    assert context.project_id == str(project.notebook)
    nested_context = app._metablock_context(
        "test.dialect",
        "meta-1",
        scope={"id": "scope-a", "navigation": [{"id": "root", "title": "Root"}]},
    )
    assert nested_context.scope_id == "scope-a"
    assert nested_context.navigation == ({"id": "root", "title": "Root"},)

    applied = app.apply_metablock("meta-1", {"kind": "rename"})
    assert applied["state"]["revision"] == 1
    assert applied["projection_invalidated"] is True
    assert (
        json.loads(app._metablock_sidecar_path("meta-1", "test.dialect").read_text())["revision"]
        == 1
    )

    materialization = app.build_metablock_materialization("meta-1", {"data": "source"})
    assert materialization.result_var == "model"
    assert materialization.bindings == {"data": "source"}
    assert app.invoke_metablock("meta-1", "inspect", {"depth": 2}) == {
        "action": "inspect",
        "args": {"depth": 2},
    }
    assert app.drain_metablock_events("meta-1")["events"] == [
        {"kind": "state_changed", "revision": 1}
    ]


def test_rejected_addon_edit_does_not_rewrite_sidecar(project, host, monkeypatch):
    app = _opened_app(project, host)
    monkeypatch.setattr(
        "graphimo.metablock_application.load_metablock_registration",
        lambda _dialect: SimpleNamespace(api_version=1, factory=lambda _ctx: FakeDialect()),
    )
    app.create_metablock("test.dialect", {"name": "Stable"}, "meta-2")
    sidecar = app._metablock_sidecar_path("meta-2", "test.dialect")
    before = sidecar.read_bytes()
    with pytest.raises(ValueError, match="rejected edit"):
        app.apply_metablock("meta-2", {"kind": "reject"})
    assert sidecar.read_bytes() == before


def test_metablock_materialization_updates_existing_or_adds_cell(project, host, monkeypatch):
    app = _opened_app(project, host)
    monkeypatch.setattr(
        "graphimo.metablock_application.load_metablock_registration",
        lambda _dialect: SimpleNamespace(api_version=1, factory=lambda _ctx: FakeDialect()),
    )
    app.create_metablock("test.dialect", {"name": "Cells"}, "meta-3")
    app._metablock_context("test.dialect", "meta-3").update_materialization_cell(
        "new-cell", "value = 1", "value"
    )
    assert "# Graphimo metablock: test.dialect" in host.marimo.added[-1][1]
    assert "# Graphimo instance: meta-3" in host.marimo.added[-1][1]
    host.marimo.snapshot["cells"].append(
        {
            "id": "existing-cell",
            "source": "# Graphimo metablock: test.dialect\n# Graphimo instance: meta-3\nold = 1\n",
        }
    )
    app._metablock_context("test.dialect", "meta-3").update_materialization_cell(
        "existing-cell", "value = 2", "value"
    )
    assert host.marimo.replaced[-1][1] == "existing-cell"
    assert "value = 2" in host.marimo.replaced[-1][2]


def test_metablock_materialization_projects_as_canvas_block_and_connects_to_node(
    project, host, monkeypatch
):
    app = _opened_app(project, host)
    monkeypatch.setattr(
        "graphimo.metablock_application.load_metablock_registration",
        lambda _dialect: SimpleNamespace(
            api_version=1,
            dialect_id="test.dialect",
            factory=lambda _ctx: FakeDialect(),
        ),
    )
    app.create_metablock("test.dialect", {"name": "Model scope"}, "meta-canvas")
    materialization = app.build_metablock_materialization("meta-canvas", {})
    source = (
        "# Graphimo metablock: test.dialect\n"
        "# Graphimo instance: meta-canvas\n"
        f"{materialization.source}\n"
    )
    host.marimo.snapshot["cells"].append({"id": "meta-cell", "source": source, "status": "idle"})

    projected = app.refresh_project()
    meta_block = next(block for block in projected.blocks if block.id == "meta-cell")
    assert meta_block.kind == "metablock_instance"
    assert meta_block.body["projection"]["name"] == "Model scope"
    assert meta_block.outputs[0].expression == "model"

    target_source, _ = build_instance_source(
        "normalize", {"image": "other"}, instance_id="f0f0f0f0"
    )
    host.marimo.snapshot["cells"].append(
        {"id": "consumer", "source": target_source, "status": "idle"}
    )
    app.connect("meta-cell", "consumer", input_name="image")
    assert "image=model" in host.marimo.replaced[-1][2]


def test_metablock_palette_discovery_does_not_import_addon(project, host, monkeypatch):
    app = _opened_app(project, host)
    loads = []
    entry = SimpleNamespace(
        name="example-addon",
        dist=SimpleNamespace(name="example-distribution"),
        load=lambda: loads.append("loaded"),
    )
    monkeypatch.setattr("graphimo.application.discover_metablock_addons", lambda: (entry,))

    item = next(item for item in app._palette().items if item.kind == "metablock")

    assert item.id == "metablock:example-addon"
    assert item.title == "example-distribution"
    assert loads == []


def test_metablock_context_rejects_unsafe_sidecar_ids(project, host):
    app = _opened_app(project, host)
    with pytest.raises(ApplicationError, match="path separators"):
        app._metablock_sidecar_path("../escape", "test.dialect")


def test_meta_event_buffer_uses_monotonic_bounded_cursors():
    host = MetaBlockAddonHost()
    for sequence in range(205):
        host.emit_event({"sequence": sequence})

    first_page = host.drain_local_events(0, 4)
    assert first_page["cursor"] == 9
    assert first_page["dropped"] == 5
    assert [event["sequence"] for event in first_page["events"]] == [5, 6, 7, 8]
    second_page = host.drain_local_events(first_page["cursor"], 3)
    assert [event["sequence"] for event in second_page["events"]] == [9, 10, 11]
