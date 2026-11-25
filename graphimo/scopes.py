"""Minimal recursive Graphimo semantic-scope data model.

Addon-owned state is treated as opaque JSON by Core. This lets an unavailable
addon scope round-trip without Core learning the addon's element semantics.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GraphElement:
    id: str
    kind: str
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdge:
    id: str
    source_element_id: str
    source_port_id: str
    target_element_id: str
    target_port_id: str
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphScope:
    id: str
    dialect_id: str
    elements: list[GraphElement | GroupBlock | MetaBlock] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dialect_id": self.dialect_id,
            "elements": [_element_to_dict(item) for item in self.elements],
            "edges": [
                {
                    "id": edge.id,
                    "source_element_id": edge.source_element_id,
                    "source_port_id": edge.source_port_id,
                    "target_element_id": edge.target_element_id,
                    "target_port_id": edge.target_port_id,
                    "state": deepcopy(edge.state),
                }
                for edge in self.edges
            ],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> GraphScope:
        if not isinstance(value, dict):
            raise ValueError("graph scope must be an object")
        scope_id = value.get("id")
        dialect_id = value.get("dialect_id")
        elements = value.get("elements", [])
        edges = value.get("edges", [])
        if (
            not isinstance(scope_id, str)
            or not scope_id
            or not isinstance(dialect_id, str)
            or not dialect_id
        ):
            raise ValueError("graph scope requires non-empty id and dialect_id")
        if not isinstance(elements, list) or not isinstance(edges, list):
            raise ValueError("graph scope elements and edges must be arrays")
        return cls(
            scope_id,
            dialect_id,
            [_element_from_dict(item) for item in elements],
            [_edge_from_dict(item) for item in edges],
        )


@dataclass
class MetaBlock:
    id: str
    plugin_id: str
    scope: GraphScope
    state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_type": "metablock",
            "id": self.id,
            "plugin_id": self.plugin_id,
            "scope": self.scope.to_dict(),
            "state": deepcopy(self.state),
        }


@dataclass
class GroupBlock:
    """Reusable nested scope that preserves its parent's semantic dialect."""

    id: str
    group_path: str
    scope: GraphScope
    state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_type": "group",
            "id": self.id,
            "group_path": self.group_path,
            "scope": self.scope.to_dict(),
            "state": deepcopy(self.state),
        }


def _element_to_dict(element: GraphElement | GroupBlock | MetaBlock) -> dict[str, Any]:
    if isinstance(element, MetaBlock):
        return element.to_dict()
    if isinstance(element, GroupBlock):
        return element.to_dict()
    if not isinstance(element, GraphElement):
        raise TypeError(f"unsupported graph element: {type(element).__name__}")
    return {
        "element_type": "element",
        "id": element.id,
        "kind": element.kind,
        "state": deepcopy(element.state),
    }


def _element_from_dict(value: Any) -> GraphElement | MetaBlock:
    if not isinstance(value, dict):
        raise ValueError("graph elements must be objects")
    state = value.get("state", {})
    if not isinstance(state, dict):
        raise ValueError("graph element state must be an object")
    if value.get("element_type") == "metablock":
        element_id = value.get("id")
        plugin_id = value.get("plugin_id")
        scope = GraphScope.from_dict(value.get("scope"))
        if (
            not isinstance(element_id, str)
            or not element_id
            or not isinstance(plugin_id, str)
            or not plugin_id
        ):
            raise ValueError("MetaBlock requires non-empty id and plugin_id")
        return MetaBlock(element_id, plugin_id, scope, deepcopy(state))
    if value.get("element_type") == "group":
        element_id = value.get("id")
        group_path = value.get("group_path")
        scope = GraphScope.from_dict(value.get("scope"))
        if (
            not isinstance(element_id, str)
            or not element_id
            or not isinstance(group_path, str)
            or not group_path
        ):
            raise ValueError("Group Block requires non-empty id and group_path")
        return GroupBlock(element_id, group_path, scope, deepcopy(state))
    element_id = value.get("id")
    kind = value.get("kind")
    if not isinstance(element_id, str) or not element_id or not isinstance(kind, str) or not kind:
        raise ValueError("graph element requires non-empty id and kind")
    return GraphElement(element_id, kind, deepcopy(state))


def _edge_from_dict(value: Any) -> GraphEdge:
    if not isinstance(value, dict):
        raise ValueError("graph edges must be objects")
    fields = (
        "id",
        "source_element_id",
        "source_port_id",
        "target_element_id",
        "target_port_id",
    )
    if any(not isinstance(value.get(field), str) or not value[field] for field in fields):
        raise ValueError("graph edge requires non-empty element and port ids")
    state = value.get("state", {})
    if not isinstance(state, dict):
        raise ValueError("graph edge state must be an object")
    return GraphEdge(*(value[field] for field in fields), deepcopy(state))
