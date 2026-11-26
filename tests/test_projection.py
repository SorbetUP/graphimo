"""Unit contracts for the semantic projection helpers (no kernel needed)."""

from __future__ import annotations

from graphimo.instance import Binding, InstanceSpec, build_instance_source
from graphimo.projection import (
    contract_from_static,
    expand_binding_edges,
    project_instance,
)


def _instance(cell_bindings, component_path="images/split.py", instance_id="aabbccdd"):
    source, spec = build_instance_source(
        "split", cell_bindings, instance_id=instance_id, component_path=component_path
    )
    assert source
    return spec


def test_expand_binding_edges_emits_one_edge_per_binding() -> None:
    source = _instance({"value": "data"})
    target = _instance(
        {"a": source.result_var + "[0]", "b": source.result_var + "[2]"}, instance_id="11223344"
    )
    edges = [
        {"source_cell_id": "src", "target_cell_id": "dst", "variable": source.result_var},
        {"source_cell_id": "other", "target_cell_id": "dst", "variable": "plain"},
    ]
    instances = {"src": source, "dst": target}
    expanded = expand_binding_edges(edges, instances)
    assert len(expanded) == 3
    by_ref = {edge.get("input_ref"): edge for edge in expanded if edge.get("input_ref")}
    assert by_ref["a"]["output_index"] == 0
    assert by_ref["b"]["output_index"] == 2
    assert by_ref["a"]["variable"] == source.result_var
    # The unmatched edge passes through untouched.
    assert any(
        edge.get("input_ref") is None and edge.get("variable") == "plain" for edge in expanded
    )


def test_expand_binding_edges_annotates_input_ref_for_plain_sources() -> None:
    target = _instance({"data": "dataset"}, instance_id="11223344")
    edges = [{"source_cell_id": "loader", "target_cell_id": "dst", "variable": "dataset"}]
    expanded = expand_binding_edges(edges, {"dst": target})
    assert len(expanded) == 1
    assert expanded[0]["input_ref"] == "data"
    assert "output_index" not in expanded[0]


def test_expand_binding_edges_preserves_stale_status_on_rebuilt_edges() -> None:
    source = _instance({"value": "x"}, instance_id="deadbeef")
    target = _instance({"v": source.result_var + "[1]"}, instance_id="11223344")
    edges = [
        {
            "source_cell_id": "src",
            "target_cell_id": "dst",
            "variable": source.result_var,
            "status": "stale",
        }
    ]
    expanded = expand_binding_edges(edges, {"src": source, "dst": target})
    assert expanded[0]["status"] == "stale"
    assert expanded[0]["output_index"] == 1
    assert expanded[0]["input_ref"] == "v"


def test_project_instance_reports_freshness_states_and_orphans() -> None:
    instance = _instance({"value": "data", "lr": "0.1"})
    contract = {
        "diagnostic": None,
        "inputs": [
            {
                "name": "value",
                "annotation": "int",
                "required": True,
                "has_default": False,
                "default": None,
            },
            {
                "name": "scale",
                "annotation": "int",
                "required": False,
                "has_default": True,
                "default": 2,
            },
        ],
        "outputs": [
            {"index": 0, "name": "out", "declared_type": "int", "expressions": ["value * scale"]}
        ],
    }
    fresh = project_instance(
        instance,
        contract=contract,
        bound={"value": "data", "lr": "0.1"},
        result_vars={instance.result_var},
        cached=True,
        fresh=True,
        has_errors=False,
        dependency_state=None,
        disabled=False,
        component_entry={"source_sha256": "abc", "cell_id": "lib"},
        component_available=True,
        preview_for=lambda index, count: {"runtime_type": "int", "preview": "42"},
    )
    view = fresh["instance"]
    assert view["state"] == "ok"
    assert view["outputs"][0]["available"] is True
    assert view["outputs"][0]["stale"] is False
    assert view["outputs"][0]["runtime_preview"]["preview"] == "42"
    assert view["inputs"][0]["binding"] == {"kind": "variable", "expression": "data"}
    assert view["inputs"][1]["binding"]["kind"] == "default"
    # lr is no longer in the contract: it must stay visible as orphaned.
    assert view["orphaned_bindings"] == [{"name": "lr", "expression": "0.1", "kind": "literal"}]
    assert fresh["component"]["source_sha256"] == "abc"
    assert fresh["component"]["available"] is True

    stale = project_instance(
        instance,
        contract=contract,
        bound={"value": "data"},
        result_vars=set(),
        cached=True,
        fresh=False,
        has_errors=False,
        dependency_state={"state": "blocked", "blocked_by": ["src"]},
        disabled=False,
        component_entry=None,
        component_available=True,
    )
    assert stale["instance"]["state"] == "blocked"
    assert stale["instance"]["outputs"][0]["has_cached_value"] is True
    assert stale["instance"]["outputs"][0]["available"] is False
    assert stale["instance"]["outputs"][0]["stale"] is True

    incomplete = project_instance(
        instance,
        contract=contract,
        bound={},
        result_vars=set(),
        cached=False,
        fresh=False,
        has_errors=False,
        dependency_state=None,
        disabled=False,
        component_entry=None,
        component_available=False,
    )
    assert incomplete["instance"]["state"] == "component_unavailable"
    assert incomplete["instance"]["inputs"][0]["binding"]["kind"] == "unbound"


def test_contract_from_static_normalizes_shape() -> None:
    static = {
        "diagnostic": None,
        "inputs": [
            {"name": "x", "required": True, "annotation": "int", "has_default": False},
            {"name": "y", "required": False, "annotation": None, "has_default": True, "default": 3},
        ],
        "outputs": [{"index": 0, "name": "out", "declared_type": "int", "expressions": ["x + y"]}],
    }
    contract = contract_from_static(static)
    assert contract is not None
    assert contract["inputs"][1]["default"] == 3
    assert contract["outputs"][0]["name"] == "out"
    assert contract_from_static(None) is None


def test_instance_spec_default_component_path_is_none() -> None:
    spec = InstanceSpec("aabbccdd", "normalize", "g_normalize_aabbccdd", (Binding("x", "y"),))
    assert spec.component_path is None
