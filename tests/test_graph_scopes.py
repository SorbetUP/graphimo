from __future__ import annotations

import pytest

from graphimo import GraphEdge, GraphElement, GraphScope, GraphScopeError, GroupBlock, MetaBlock


def test_nested_scopes_keep_group_dialect_and_metablock_extension_dialect():
    root = GraphScope(
        "root",
        "python",
        elements=(
            GraphElement("source", "node", {"name": "load"}),
            GroupBlock(
                "group",
                GraphScope("group-scope", "python", (GraphElement("inner", "node"),)),
            ),
            MetaBlock(
                "model",
                "pytorch.model",
                GraphScope(
                    "model-scope",
                    "pytorch.model",
                    (GraphElement("linear", "addon-defined-linear", {"features": 32}),),
                ),
            ),
        ),
        edges=(GraphEdge("source", "dataset", "group", "dataset"),),
    )

    restored = GraphScope.from_dict(root.to_dict())
    assert restored == root
    assert restored.elements[1].scope.dialect_id == "python"
    assert restored.elements[2].scope.dialect_id == "pytorch.model"


def test_group_blocks_cannot_switch_dialect_and_edges_stay_in_scope():
    with pytest.raises(GraphScopeError, match="inherit"):
        GraphScope(
            "root",
            "python",
            (GroupBlock("group", GraphScope("nested", "pytorch.model")),),
        )
    with pytest.raises(GraphScopeError, match="outside its scope"):
        GraphScope(
            "root",
            "python",
            (GraphElement("node", "node"),),
            (GraphEdge("elsewhere", "out", "node", "in"),),
        )


def test_scope_identity_is_unique_across_recursive_scopes():
    with pytest.raises(GraphScopeError, match="duplicate"):
        GraphScope(
            "root",
            "python",
            (
                GraphElement("same", "node"),
                GroupBlock("group", GraphScope("same", "python")),
            ),
        )


def test_scope_rejects_invalid_json_cycles_and_oversized_serialization():
    with pytest.raises(GraphScopeError, match="JSON-compatible"):
        GraphScope("bad-data", "python", (GraphElement("node", "node", object()),))

    cyclic = GraphScope("cycle", "python")
    object.__setattr__(cyclic, "elements", (GroupBlock("nested-group", cyclic),))
    with pytest.raises(GraphScopeError, match="cycles"):
        cyclic.to_dict()

    huge = GraphScope(
        "large", "python", (GraphElement("node", "node", {"payload": "x" * (8 * 1024 * 1024)}),)
    )
    with pytest.raises(GraphScopeError, match="8 MiB"):
        huge.to_dict()
