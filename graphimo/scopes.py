"""Generic nested graph scopes shared by normal nodes, groups and addons."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, TypeAlias

_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_SCOPE_BYTES = 8 * 1024 * 1024
_MAX_SCOPE_DEPTH = 64


class GraphScopeError(ValueError):
    """Raised when a nested Graphimo scope is malformed or unsafe to persist."""


@dataclass(frozen=True)
class GraphElement:
    """A generic element whose semantics are defined by its scope dialect."""

    id: str
    kind: str
    data: Any = None


@dataclass(frozen=True)
class GraphEdge:
    source_id: str
    source_port: str
    target_id: str
    target_port: str


@dataclass(frozen=True)
class GroupBlock:
    id: str
    scope: GraphScope


@dataclass(frozen=True)
class MetaBlock:
    id: str
    plugin_id: str
    scope: GraphScope


ScopeElement: TypeAlias = GraphElement | GroupBlock | MetaBlock


@dataclass(frozen=True)
class GraphScope:
    id: str
    dialect_id: str
    elements: tuple[ScopeElement, ...] = ()
    edges: tuple[GraphEdge, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "elements", tuple(self.elements))
        object.__setattr__(self, "edges", tuple(self.edges))
        validate_scope(self)

    def to_dict(self) -> dict[str, Any]:
        validate_scope(self)
        value = _scope_to_dict(self)
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, sort_keys=True, allow_nan=False
            ).encode()
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
            raise GraphScopeError(f"graph scope is not serializable: {exc}") from exc
        if len(encoded) > _MAX_SCOPE_BYTES:
            raise GraphScopeError("serialized graph scope exceeds 8 MiB")
        return value

    @classmethod
    def from_dict(cls, value: Any) -> GraphScope:
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
            raise GraphScopeError(f"serialized graph scope is not valid JSON: {exc}") from exc
        if len(encoded) > _MAX_SCOPE_BYTES:
            raise GraphScopeError("serialized graph scope exceeds 8 MiB")
        return _scope_from_dict(value)


def _validate_id(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise GraphScopeError(f"{label} must contain 1-128 letters, digits, '_' or '-'")


def validate_scope(scope: GraphScope) -> None:
    """Validate uniqueness, nesting, dialect inheritance, edges and opaque JSON."""
    seen_ids: set[str] = set()
    visiting: set[int] = set()

    def unique_id(value: str, label: str) -> None:
        _validate_id(value, label)
        if value in seen_ids:
            raise GraphScopeError(f"duplicate graph scope identity: {value}")
        seen_ids.add(value)

    def visit(current: GraphScope, parent_dialect: str | None, depth: int) -> None:
        if depth > _MAX_SCOPE_DEPTH:
            raise GraphScopeError(f"graph scope nesting exceeds {_MAX_SCOPE_DEPTH} levels")
        identity = id(current)
        if identity in visiting:
            raise GraphScopeError("graph scopes cannot contain object cycles")
        visiting.add(identity)
        unique_id(current.id, "scope id")
        if not isinstance(current.dialect_id, str) or not current.dialect_id:
            raise GraphScopeError("scope dialect_id must be a non-empty string")
        if parent_dialect is not None and current.dialect_id != parent_dialect:
            raise GraphScopeError("Group Blocks must inherit their parent scope dialect")
        element_ids: set[str] = set()
        for element in current.elements:
            if isinstance(element, GraphElement):
                unique_id(element.id, "element id")
                if not isinstance(element.kind, str) or not element.kind:
                    raise GraphScopeError("element kind must be a non-empty string")
                try:
                    json.dumps(element.data, ensure_ascii=False, allow_nan=False)
                except (TypeError, ValueError, UnicodeEncodeError) as exc:
                    raise GraphScopeError(f"element data must be JSON-compatible: {exc}") from exc
            elif isinstance(element, GroupBlock):
                unique_id(element.id, "group id")
                if not isinstance(element.scope, GraphScope):
                    raise GraphScopeError("GroupBlock scope must be a GraphScope")
                visit(element.scope, current.dialect_id, depth + 1)
            elif isinstance(element, MetaBlock):
                unique_id(element.id, "MetaBlock id")
                if not isinstance(element.plugin_id, str) or not element.plugin_id:
                    raise GraphScopeError("MetaBlock plugin_id must be a non-empty string")
                if not isinstance(element.scope, GraphScope):
                    raise GraphScopeError("MetaBlock scope must be a GraphScope")
                visit(element.scope, None, depth + 1)
            else:
                raise GraphScopeError(f"unsupported graph element type: {type(element).__name__}")
            element_ids.add(element.id)
        for edge in current.edges:
            if not isinstance(edge, GraphEdge):
                raise GraphScopeError("scope edges must be GraphEdge values")
            if edge.source_id not in element_ids or edge.target_id not in element_ids:
                raise GraphScopeError("edge references an element outside its scope")
            if not isinstance(edge.source_port, str) or not edge.source_port:
                raise GraphScopeError("edge source_port must be a non-empty string")
            if not isinstance(edge.target_port, str) or not edge.target_port:
                raise GraphScopeError("edge target_port must be a non-empty string")
        visiting.remove(identity)

    if not isinstance(scope, GraphScope):
        raise GraphScopeError("value must be a GraphScope")
    visit(scope, None, 0)


def _scope_to_dict(scope: GraphScope) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for element in scope.elements:
        if isinstance(element, GraphElement):
            elements.append(
                {"type": "element", "id": element.id, "kind": element.kind, "data": element.data}
            )
        elif isinstance(element, GroupBlock):
            elements.append(
                {"type": "group", "id": element.id, "scope": _scope_to_dict(element.scope)}
            )
        else:
            elements.append(
                {
                    "type": "metablock",
                    "id": element.id,
                    "plugin_id": element.plugin_id,
                    "scope": _scope_to_dict(element.scope),
                }
            )
    return {
        "format": 1,
        "id": scope.id,
        "dialect_id": scope.dialect_id,
        "elements": elements,
        "edges": [
            {
                "source_id": edge.source_id,
                "source_port": edge.source_port,
                "target_id": edge.target_id,
                "target_port": edge.target_port,
            }
            for edge in scope.edges
        ],
    }


def _scope_from_dict(value: Any) -> GraphScope:
    if not isinstance(value, dict) or value.get("format") != 1:
        raise GraphScopeError("unsupported graph scope serialization format")
    raw_elements = value.get("elements")
    raw_edges = value.get("edges")
    if not isinstance(raw_elements, list) or not isinstance(raw_edges, list):
        raise GraphScopeError("serialized scope elements and edges must be lists")
    elements: list[ScopeElement] = []
    try:
        for item in raw_elements:
            if not isinstance(item, dict):
                raise GraphScopeError("serialized graph elements must be objects")
            if item.get("type") == "element":
                elements.append(GraphElement(item["id"], item["kind"], item.get("data")))
            elif item.get("type") == "group":
                elements.append(GroupBlock(item["id"], _scope_from_dict(item["scope"])))
            elif item.get("type") == "metablock":
                elements.append(
                    MetaBlock(item["id"], item["plugin_id"], _scope_from_dict(item["scope"]))
                )
            else:
                raise GraphScopeError("serialized graph element has an unknown type")
        edges = tuple(
            GraphEdge(
                item["source_id"], item["source_port"], item["target_id"], item["target_port"]
            )
            for item in raw_edges
            if isinstance(item, dict)
        )
        if len(edges) != len(raw_edges):
            raise GraphScopeError("serialized graph edges must be objects")
        return GraphScope(value["id"], value["dialect_id"], tuple(elements), edges)
    except (KeyError, TypeError) as exc:
        raise GraphScopeError(f"serialized graph scope is incomplete: {exc}") from exc
