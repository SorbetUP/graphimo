from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from graphimo.metablocks import MetaDialectRegistration, discover_metablock_addons
from graphimo.scopes import GraphEdge, GraphElement, GraphScope, MetaBlock
from graphimo.controls import build_control_source, control_output_type, parse_control_source, validate_control_value


def test_recursive_scope_roundtrip_preserves_opaque_addon_state() -> None:
    original_payload = {"schema_version": 1, "unknown": {"future": [1, "raw"]}}
    nested = MetaBlock(
        id="model-a",
        plugin_id="pytorch",
        scope=GraphScope(
            id="model-scope",
            dialect_id="pytorch.model",
            elements=[GraphElement("layer-a", "addon-owned-kind", {"width": 128})],
        ),
        state=original_payload,
    )
    root = GraphScope(
        id="root",
        dialect_id="python",
        elements=[nested],
        edges=[GraphEdge("edge-a", "source", "out", "model-a", "input", {"label": "kept"})],
    )

    restored = GraphScope.from_dict(root.to_dict())
    restored_meta = restored.elements[0]
    assert isinstance(restored_meta, MetaBlock)
    assert restored_meta.plugin_id == "pytorch"
    assert restored_meta.scope.dialect_id == "pytorch.model"
    assert restored_meta.state == original_payload
    assert restored.to_dict() == root.to_dict()


def test_scope_rejects_malformed_edges_without_rewriting_unknown_state() -> None:
    with pytest.raises(ValueError, match="edge requires"):
        GraphScope.from_dict(
            {"id": "root", "dialect_id": "python", "elements": [], "edges": [{"id": "bad"}]}
        )


def test_registration_validates_only_the_declared_registration_contract() -> None:
    def factory(_api: object) -> object:
        return object()

    registration = MetaDialectRegistration("example.dialect", "1.2.3", 1, factory)
    assert registration.factory is factory
    with pytest.raises(ValueError, match="api_version"):
        MetaDialectRegistration("example.dialect", "1.2.3", 0, factory)
    with pytest.raises(TypeError, match="callable"):
        MetaDialectRegistration("example.dialect", "1.2.3", 1, object())  # type: ignore[arg-type]


def test_discovery_uses_graphimo_metablocks_entry_point_group_without_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = EntryPoint(name="example", value="example_addon:registration", group="graphimo.metablocks")
    monkeypatch.setattr("graphimo.metablocks.entry_points", lambda *, group: [entry] if group == "graphimo.metablocks" else [])
    result = discover_metablock_addons()
    assert result == (entry,)


def test_core_package_has_no_torch_or_torchlens_imports() -> None:
    core_root = Path(__file__).resolve().parents[1] / "graphimo"
    source_files = core_root.rglob("*.py")
    forbidden = ("import torch", "from torch", "import torchlens", "from torchlens")
    for path in source_files:
        source = path.read_text(encoding="utf-8")
        assert not any(line.lstrip().startswith(forbidden) for line in source.splitlines()), path


@pytest.mark.parametrize(
    ("control_type", "parameters", "output_type"),
    [
        ("slider", {"start": 0.0, "stop": 1.0, "step": 0.01, "value": 0.25, "debounce": True}, "float"),
        ("range_slider", {"start": 0, "stop": 10, "step": 1, "value": [2, 8]}, "tuple[float, float]"),
        ("number", {"value": 3, "step": 1}, "int"),
        ("select", {"options": ["small", "large"], "value": "large"}, "str"),
        ("checkbox", {"value": True}, "bool"),
        ("switch", {"value": False}, "bool"),
        ("text", {"value": "hello"}, "str"),
        ("button", {}, "event"),
    ],
)
def test_control_sources_are_readable_owned_marimo_calls(control_type, parameters, output_type) -> None:
    source, spec = build_control_source(control_type, parameters, instance_id="a81f2c40")
    parsed = parse_control_source(source)
    assert parsed == spec
    assert control_output_type(spec) == output_type
    assert f"# Graphimo control: {control_type}" in source
    assert f"{spec.result_var}.value" == spec.output_expression


def test_control_value_validation_preserves_select_types_and_bounds() -> None:
    _, slider = build_control_source("slider", {"start": 0, "stop": 1, "value": 0.5})
    assert validate_control_value(slider, 0.8) == 0.8
    with pytest.raises(ValueError, match="above its maximum"):
        validate_control_value(slider, 2)
    _, select = build_control_source("select", {"options": [1, "1"], "value": 1})
    assert validate_control_value(select, 1) == 1
    assert validate_control_value(select, "1") == "1"
    with pytest.raises(ValueError, match="exactly match"):
        validate_control_value(select, True)
