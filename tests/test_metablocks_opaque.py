from __future__ import annotations

from graphimo.application import GraphimoApplication


def test_missing_addon_sidecar_is_rendered_as_opaque_and_never_rewritten(
    project, host, monkeypatch
) -> None:
    monkeypatch.setattr("graphimo.application.discover_metablock_addons", lambda: ())
    state_path = project.root / ".graphimo" / "metablocks" / "model-opaque.demo.json"
    state_path.parent.mkdir(parents=True)
    raw = '{ "schema_version": 912, "metablock_id": "model-opaque", "name": "Future Model", "new": [4, 5] }\n'
    state_path.write_text(raw, encoding="utf-8")
    app = GraphimoApplication(host.marimo, project.components, layout_path=project.layout)
    host.marimo.snapshot["notebook"]["path"] = str(project.notebook)
    host.marimo.sessions[0]["path"] = str(project.notebook)

    view = app.open_project(project.notebook)
    placeholder = next(block for block in view.blocks if block.id == "metablock:model-opaque")

    assert placeholder.kind == "metablock_opaque"
    assert placeholder.title == "Future Model"
    assert placeholder.state == "unavailable"
    assert placeholder.can_open is False
    assert placeholder.body["opaque"] is True
    assert placeholder.body["plugin_id"] == "demo"
    assert placeholder.body["addon_installed"] is False
    assert "not installed" in placeholder.body["unavailable_reason"]
    assert state_path.read_text(encoding="utf-8") == raw
    selected = app.select([placeholder.id])
    assert selected.selection == (placeholder.id,)
    assert state_path.read_text(encoding="utf-8") == raw


def test_unreadable_addon_sidecar_stays_opaque_with_diagnostic(project, host, monkeypatch) -> None:
    monkeypatch.setattr("graphimo.application.discover_metablock_addons", lambda: ())
    state_path = project.root / ".graphimo" / "metablocks" / "broken.demo.json"
    state_path.parent.mkdir(parents=True)
    raw = "{not valid JSON"
    state_path.write_text(raw, encoding="utf-8")
    app = GraphimoApplication(host.marimo, project.components, layout_path=project.layout)
    host.marimo.snapshot["notebook"]["path"] = str(project.notebook)
    host.marimo.sessions[0]["path"] = str(project.notebook)

    view = app.open_project(project.notebook)

    assert any(item.code == "metablock_state_unreadable" for item in view.diagnostics)
    opaque = next(block for block in view.blocks if block.id == "metablock:broken")
    assert opaque.state == "unavailable"
    assert state_path.read_text(encoding="utf-8") == raw
