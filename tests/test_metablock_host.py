from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from graphimo.application import GraphimoApplication
from graphimo.metablocks import (
    AddonUnavailableError,
    GraphimoHostAPI,
    MetaBlockAddonManager,
    MetaBlockError,
    MetaBlockStateError,
    MetaDialectRegistration,
)


class FakeDialect:
    dialect_id = "test.counter"

    def __init__(self, host: GraphimoHostAPI):
        self.host = host

    def create_state(self, request: dict[str, Any], context: GraphimoHostAPI) -> dict[str, Any]:
        assert context.current_metablock_id is not None
        return {"name": request.get("name", "Counter"), "value": 0}

    def catalog(self, state: Any, scope: str, context: GraphimoHostAPI) -> dict[str, Any]:
        return {"scope": scope, "items": ["increment"]}

    def project(self, state: Any, scope: str, context: GraphimoHostAPI) -> dict[str, Any]:
        return {"scope": scope, "name": state["name"], "value": state["value"]}

    def apply(self, state: Any, intent: dict[str, Any], context: GraphimoHostAPI) -> dict[str, Any]:
        if intent["type"] != "increment":
            raise ValueError("unknown intent")
        return {**state, "value": state["value"] + 1}

    def build_materialization(
        self, state: Any, bindings: dict[str, Any], context: GraphimoHostAPI
    ) -> dict[str, Any]:
        return {
            "source": "counter = object()",
            "result_name": "counter",
            "output_type": "Counter",
            "bindings": bindings,
            "revision": hashlib.sha256(
                (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()
            ).hexdigest(),
        }

    def invoke(
        self, state: Any, action: str, args: dict[str, Any], context: GraphimoHostAPI
    ) -> dict[str, Any]:
        assert context.current_metablock_id == "block"
        return {"action": action, "args": args}

    def drain_events(
        self, cursor: str | None, limit: int, context: GraphimoHostAPI
    ) -> dict[str, Any]:
        assert context.current_metablock_id == "block"
        return {"cursor": "next", "events": [{"sequence": 1}][:limit]}


class FakeEntryPoint:
    name = "test.counter"
    value = "fake:registration"
    dist = SimpleNamespace(name="graphimo-test-addon", version="1.0.0")

    def __init__(self):
        self.loaded = False

    def load(self):
        self.loaded = True
        return MetaDialectRegistration(
            dialect_id=self.name,
            addon_version="1.0.0",
            api_version=1,
            factory=FakeDialect,
        )


def setup_manager(tmp_path: Path, *, installed: bool = True):
    entry = FakeEntryPoint()
    host = GraphimoHostAPI(
        project_id="project-1",
        project_path=tmp_path / "notebook.py",
    )
    manager = MetaBlockAddonManager(host, entry_points=lambda: [entry] if installed else [])
    return host, manager, entry


def test_discovery_is_lazy_and_lists_only_entry_point_metadata(tmp_path: Path) -> None:
    _host, manager, entry = setup_manager(tmp_path)

    descriptors = manager.installed()

    assert descriptors[0].name == "test.counter"
    assert descriptors[0].distribution == "graphimo-test-addon"
    assert entry.loaded is False


def test_loader_rejects_addons_outside_the_frozen_v1_contract(tmp_path: Path) -> None:
    class IncompleteDialect:
        dialect_id = "test.counter"

    entry = FakeEntryPoint()
    entry.load = lambda: MetaDialectRegistration(
        dialect_id="test.counter",
        addon_version="1.0.0",
        api_version=1,
        factory=lambda _host: IncompleteDialect(),
    )
    host = GraphimoHostAPI("project-1", tmp_path / "notebook.py")
    manager = MetaBlockAddonManager(host, entry_points=lambda: [entry])

    with pytest.raises(MetaBlockError, match="missing required methods"):
        manager.create("test.counter", "block", {})


def test_create_apply_and_catalog_use_addon_owned_semantics(tmp_path: Path) -> None:
    _host, manager, _entry = setup_manager(tmp_path)

    created = manager.create("test.counter", "block", {"name": "Samples"})
    catalog = manager.catalog("block", "root/model")
    updated = manager.apply("block", {"type": "increment"})

    assert created["available"] is True
    assert created["projection"]["name"] == "Samples"
    assert catalog == {"scope": "root/model", "items": ["increment"]}
    assert updated["projection"]["value"] == 1
    assert manager.load("block").revision != created["revision"]


def test_missing_addon_projects_opaque_state_without_rewriting_sidecar(tmp_path: Path) -> None:
    host, manager, _entry = setup_manager(tmp_path)
    manager.create("test.counter", "block", {"name": "Preserved"})
    sidecar = host.sidecar_path("block")
    before = sidecar.read_bytes()
    manager_without_addon = MetaBlockAddonManager(host, entry_points=list)

    projected = manager_without_addon.project("block")

    assert projected["available"] is False
    assert projected["state"] == {"name": "Preserved", "value": 0}
    assert projected["diagnostics"][0]["code"] == "metablock_addon_unavailable"
    assert sidecar.read_bytes() == before
    with pytest.raises(AddonUnavailableError):
        manager_without_addon.apply("block", {"type": "increment"})

    restored = MetaBlockAddonManager(host, entry_points=lambda: [FakeEntryPoint()])
    available_again = restored.project("block")
    assert available_again["available"] is True
    assert available_again["revision"] == projected["revision"]
    assert available_again["projection"]["name"] == "Preserved"


def test_materialization_is_sent_to_host_with_state_revision(tmp_path: Path) -> None:
    host, manager, _entry = setup_manager(tmp_path)
    updated_specs = []
    host.update_materialization_cell = updated_specs.append
    manager.create("test.counter", "block", {})

    result = manager.materialize("block", {"seed": 4})

    assert result["result_name"] == "counter"
    assert updated_specs[0]["metablock_id"] == "block"
    assert updated_specs[0]["revision"] == manager.load("block").revision


def test_materialization_rejects_stale_revision(tmp_path: Path) -> None:
    host, manager, _entry = setup_manager(tmp_path)
    host.update_materialization_cell = lambda _spec: None
    manager.create("test.counter", "block", {})
    dialect = manager._load("test.counter")
    dialect.build_materialization = lambda *_args: {
        "source": "x = 1",
        "result_name": "x",
        "output_type": "int",
        "bindings": {},
        "revision": "stale",
    }

    with pytest.raises(MetaBlockError, match="revision"):
        manager.materialize("block", {})


def test_event_drain_is_bounded(tmp_path: Path) -> None:
    _host, manager, _entry = setup_manager(tmp_path)
    manager.create("test.counter", "block", {})

    assert len(manager.drain_events("block", limit=1)["events"]) == 1
    assert manager.invoke("block", "test", {}) == {"action": "test", "args": {}}
    with pytest.raises(ValueError, match="between 1 and 512"):
        manager.drain_events("block", limit=513)


def test_sidecar_rejects_traversal_and_invalid_json_state(tmp_path: Path) -> None:
    host, _manager, _entry = setup_manager(tmp_path)

    with pytest.raises(ValueError, match="metablock_id"):
        host.sidecar_path("../outside")
    with pytest.raises(ValueError, match="metablock_id"):
        host.remove_sidecar("../outside")
    with pytest.raises(MetaBlockStateError, match="UTF-8 JSON"):
        host.write_sidecar("block", {"state": object()})


def test_sidecar_write_is_utf8_and_atomically_loadable(tmp_path: Path) -> None:
    host, manager, _entry = setup_manager(tmp_path)

    manager.create("test.counter", "block", {"name": "Échantillon"})

    data = host.sidecar_path("block").read_bytes()
    assert host.sidecar_path("block").name == "block.test.json"
    assert b"\xc3\x89chantillon" in data
    assert manager.load("block").state["name"] == "Échantillon"


def test_application_routes_meta_actions_through_the_shared_manager(tmp_path: Path) -> None:
    host, manager, _entry = setup_manager(tmp_path)
    application = GraphimoApplication(host, manager)

    created = application.execute(
        "metablock.create",
        {"dialect_id": "test.counter", "metablock_id": "block", "request": {}},
    )
    updated = application.execute(
        "metablock.apply", {"metablock_id": "block", "intent": {"type": "increment"}}
    )

    assert created["available"] is True
    assert updated["projection"]["value"] == 1


def test_application_routes_all_addon_lifecycle_actions_through_one_manager(
    tmp_path: Path,
) -> None:
    host, manager, _entry = setup_manager(tmp_path)
    application = GraphimoApplication(host, manager)
    materialization_updates: list[dict[str, Any]] = []
    host.update_materialization_cell = materialization_updates.append

    created = application.execute(
        "metablock.create",
        {"dialect_id": "test.counter", "metablock_id": "block", "request": {}},
    )
    catalog = application.execute(
        "metablock.catalog", {"metablock_id": "block", "scope": "root/model"}
    )
    projected = application.execute("metablock.project", {"metablock_id": "block"})
    edited = application.execute(
        "metablock.apply", {"metablock_id": "block", "intent": {"type": "increment"}}
    )
    materialized = application.execute(
        "metablock.materialize",
        {"metablock_id": "block", "bindings": {"seed": 9}},
    )
    invoked = application.execute(
        "metablock.invoke",
        {"metablock_id": "block", "action": "probe", "args": {"depth": 2}},
    )
    events = application.execute(
        "metablock.events", {"metablock_id": "block", "cursor": "0", "limit": 8}
    )

    assert created["available"] is True
    assert catalog == {"scope": "root/model", "items": ["increment"]}
    assert projected["projection"]["value"] == 0
    assert edited["projection"]["value"] == 1
    assert materialized["bindings"] == {"seed": 9}
    assert materialization_updates[0]["metablock_id"] == "block"
    assert materialization_updates[0]["revision"] == edited["revision"]
    assert invoked == {"action": "probe", "args": {"depth": 2}}
    assert events == {"cursor": "next", "events": [{"sequence": 1}]}


def test_application_rejects_unknown_actions_and_oversized_arguments(tmp_path: Path) -> None:
    host, _manager, _entry = setup_manager(tmp_path)
    application = GraphimoApplication(host)

    with pytest.raises(ValueError, match="unknown Graphimo action"):
        application.execute("metablock.magic", {})
    with pytest.raises(ValueError, match="256 KiB"):
        application.execute(
            "metablock.project", {"metablock_id": "block", "padding": "x" * (256 * 1024)}
        )


def test_failed_projection_does_not_persist_a_partial_addon_edit(tmp_path: Path) -> None:
    _host, manager, _entry = setup_manager(tmp_path)
    manager.create("test.counter", "block", {})
    handler = manager._load("test.counter")
    handler.project = lambda *_args: (_ for _ in ()).throw(ValueError("projection failed"))

    with pytest.raises(ValueError, match="projection failed"):
        manager.apply("block", {"type": "increment"})

    assert manager.load("block").state["value"] == 0


def test_layout_persists_per_scope_and_preserves_other_document_metadata(tmp_path: Path) -> None:
    host, _manager, _entry = setup_manager(tmp_path)
    host.presentation_path.parent.mkdir(parents=True, exist_ok=True)
    host.presentation_path.write_text(
        json.dumps(
            {
                "format": 1,
                "layout": {"nested": {"old": {"x": 1, "y": 2, "width": 3, "height": 4}}},
                "camera": {"zoom": 1.2},
            }
        ),
        encoding="utf-8",
    )

    host.write_layout("root", {"element_1": {"x": 11, "y": 12, "width": 240, "height": 96}})

    assert host.read_layout("root") == {
        "element_1": {"x": 11.0, "y": 12.0, "width": 240.0, "height": 96.0}
    }
    document = json.loads(host.presentation_path.read_text(encoding="utf-8"))
    assert document["layout"]["nested"]["old"]["width"] == 3
    assert document["camera"] == {"zoom": 1.2}


@pytest.mark.parametrize(
    ("scope_id", "layout", "message"),
    [
        ("../outside", {}, "scope ID"),
        ("root", {"../outside": {"x": 0, "y": 0, "width": 1, "height": 1}}, "element ID"),
        ("root", {"block": {"x": float("nan"), "y": 0, "width": 1, "height": 1}}, "finite"),
        ("root", {"block": {"x": 0, "y": 0, "width": 0, "height": 1}}, "positive"),
    ],
)
def test_layout_rejects_invalid_scope_and_geometry(
    tmp_path: Path, scope_id: str, layout: dict[str, Any], message: str
) -> None:
    host, _manager, _entry = setup_manager(tmp_path)

    with pytest.raises((ValueError, MetaBlockStateError), match=message):
        host.write_layout(scope_id, layout)


def test_application_routes_layout_and_list_actions(tmp_path: Path) -> None:
    host, manager, _entry = setup_manager(tmp_path)
    application = GraphimoApplication(host, manager)
    manager.create("test.counter", "block", {})

    saved = application.execute(
        "layout.set",
        {"scope_id": "root", "layout": {"block": {"x": 10, "y": 20, "width": 200, "height": 90}}},
    )
    layout = application.execute("layout.get", {"scope_id": "root"})
    listed = application.execute("metablock.list", {})

    assert saved == {"saved": True}
    assert layout["layout"]["block"]["x"] == 10
    assert listed["items"][0]["metablock_id"] == "block"
