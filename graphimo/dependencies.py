"""Projection helpers for disabled marimo nodes and blocked descendants."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .instance import parse_disabled_instance_source, parse_instance_source


def annotate_blocked_dependencies(
    cells: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> dict[str, list[str]]:
    """Annotate stale edges and transitive blockers without editing cell source."""
    disabled: dict[str, str] = {}
    unavailable: dict[str, str] = {}
    cell_ids = {item.get("id") for item in cells if isinstance(item, dict)}
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        cell_id = cell.get("id")
        source = cell.get("source", "")
        instance = parse_disabled_instance_source(source)
        existing = cell.get("graphimo_disabled_instance")
        if isinstance(existing, dict) and existing.get("disabled") is True:
            disabled[cell_id] = existing.get("result_var", "")
        elif instance is not None and isinstance(cell_id, str):
            disabled[cell_id] = instance.result_var
            cell["graphimo_disabled_instance"] = {
                "instance_id": instance.instance_id,
                "node": instance.node_name,
                "result_var": instance.result_var,
                "outputs": [],
                "disabled": True,
            }
        elif cell.get("graphimo_component_unavailable") is True and isinstance(cell_id, str):
            active = parse_instance_source(source)
            if active is not None:
                unavailable[cell_id] = active.result_var

    known = {
        (edge.get("source_cell_id"), edge.get("target_cell_id"), edge.get("variable"))
        for edge in edges
        if isinstance(edge, dict)
    }
    for source_id, result_var in {**disabled, **unavailable}.items():
        for cell in cells:
            if not isinstance(cell, dict) or cell.get("id") == source_id:
                continue
            refs = cell.get("refs", [])
            if result_var not in refs:
                continue
            key = (source_id, cell.get("id"), result_var)
            if key not in known:
                edges.append(
                    {
                        "source_cell_id": source_id,
                        "target_cell_id": cell.get("id"),
                        "variable": result_var,
                    }
                )
                known.add(key)

    blockers = {**disabled, **unavailable}
    blocked_by: dict[str, set[str]] = {}
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source_id = edge.get("source_cell_id")
            target_id = edge.get("target_cell_id")
            if target_id not in cell_ids:
                continue
            roots = set()
            if source_id in blockers:
                roots.add(source_id)
            roots.update(blocked_by.get(source_id, set()))
            additions = roots - blocked_by.get(target_id, set())
            if additions:
                blocked_by.setdefault(target_id, set()).update(additions)
                changed = True
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        cell_id = cell.get("id")
        if cell_id in disabled:
            cell["graphimo_dependency_state"] = {
                "state": "disabled",
                "blocked_by": [cell_id],
            }
        elif cell_id in unavailable:
            cell["graphimo_dependency_state"] = {
                "state": "component_unavailable",
                "blocked_by": [cell_id],
            }
        elif blocked_by.get(cell_id):
            cell["graphimo_dependency_state"] = {
                "state": "blocked",
                "blocked_by": sorted(blocked_by[cell_id]),
                "stale": True,
            }
    for edge in edges:
        if isinstance(edge, dict) and (
            edge.get("source_cell_id") in blockers or edge.get("source_cell_id") in blocked_by
        ):
            edge["status"] = "stale"
    return {cell_id: sorted(roots) for cell_id, roots in blocked_by.items()}


def blocked_dependencies_for_graph(graph_cells: Mapping[Any, Any], target_id: str) -> list[str]:
    """Compute disabled upstream roots from marimo's live graph before a run."""
    cells: list[dict[str, Any]] = []
    owners: dict[str, list[str]] = {}
    graph_items = list(graph_cells.items())
    for raw_id, implementation in graph_items:
        cell_id = str(raw_id)
        code = getattr(implementation, "code", "")
        defs = list(getattr(implementation, "defs", ()) or ())
        refs = list(getattr(implementation, "refs", ()) or ())
        cells.append({"id": cell_id, "source": code, "defs": defs, "refs": refs})
        for name in defs:
            owners.setdefault(name, []).append(cell_id)
    edges = []
    for raw_id, implementation in graph_items:
        target = str(raw_id)
        for name in getattr(implementation, "refs", ()) or ():
            for source in owners.get(name, ()):
                if source != target:
                    edges.append(
                        {"source_cell_id": source, "target_cell_id": target, "variable": name}
                    )
    return annotate_blocked_dependencies(cells, edges).get(str(target_id), [])
