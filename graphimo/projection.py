"""Semantic projection of live marimo state onto Graphimo's instance model.

The marimo graph (refs/defs) stays the execution truth. These helpers turn a
kernel snapshot into the formal port model renderers and agents consume:
per-input binding kinds, per-output freshness, orphaned bindings and
per-binding edges. They are pure functions over plain data so they can be
unit-tested without a kernel.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .instance import InstanceSpec, binding_kind, call_target
from .node import NodeSpec


def contract_from_node_spec(spec: NodeSpec | None) -> dict[str, Any] | None:
    """Normalize a runtime :class:`NodeSpec` into the shared contract shape."""
    if spec is None:
        return None
    return {
        "diagnostic": spec.diagnostic,
        "inputs": [
            {
                "name": port.name,
                "annotation": port.annotation,
                "required": port.required,
                "has_default": port.has_default,
                "default": port.default
                if port.has_default
                and (port.default is None or type(port.default) in (bool, int, float, str))
                else None,
            }
            for port in spec.inputs
        ],
        "outputs": [
            {
                "index": port.index,
                "name": _output_name(port.expressions, port.index),
                "declared_type": port.declared_type,
                "expressions": list(port.expressions),
            }
            for port in spec.outputs
        ],
    }


def contract_from_static(static: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a static AST component contract into the shared shape."""
    if not isinstance(static, Mapping):
        return None
    inputs = [
        {
            "name": item.get("name"),
            "annotation": item.get("annotation"),
            "required": bool(item.get("required")),
            "has_default": bool(item.get("has_default")),
            "default": item.get("default"),
        }
        for item in static.get("inputs", [])
        if isinstance(item, Mapping)
    ]
    outputs = [
        {
            "index": item.get("index"),
            "name": item.get("name") or f"output_{(item.get('index') or 0) + 1}",
            "declared_type": item.get("declared_type"),
            "expressions": list(item.get("expressions", [])),
        }
        for item in static.get("outputs", [])
        if isinstance(item, Mapping)
    ]
    return {
        "diagnostic": static.get("diagnostic"),
        "inputs": inputs,
        "outputs": outputs,
    }


def _output_name(expressions: Any, index: int) -> str:
    if (
        isinstance(expressions, (list, tuple))
        and len(expressions) == 1
        and isinstance(expressions[0], str)
        and expressions[0].isidentifier()
    ):
        return expressions[0]
    return f"output_{index + 1}"


def project_instance(
    instance: InstanceSpec,
    *,
    contract: Mapping[str, Any] | None,
    bound: Mapping[str, str],
    result_vars: set[str] | None = None,
    cached: bool,
    fresh: bool,
    has_errors: bool,
    dependency_state: Mapping[str, Any] | None,
    disabled: bool,
    component_entry: Mapping[str, Any] | None,
    component_available: bool,
    preview_for: Callable[[int, int], Any] | None = None,
) -> dict[str, Any]:
    """Project one instance into ``graphimo_instance``/``graphimo_component``.

    ``fresh`` means the cached result is currently valid; a cached but stale
    value is reported with ``has_cached_value`` and ``stale`` so a renderer can
    show it as outdated without ever presenting it as current.
    """
    inputs: list[dict[str, Any]] = []
    orphaned: list[dict[str, Any]] = []
    input_names: set[str] = set()
    contract_inputs = list(contract.get("inputs", [])) if contract else []
    contract_outputs = list(contract.get("outputs", [])) if contract else []
    for port in contract_inputs:
        name = port.get("name")
        if not isinstance(name, str):
            continue
        input_names.add(name)
        expression = bound.get(name)
        if expression is not None:
            binding = {"kind": binding_kind(expression, result_vars), "expression": expression}
        elif port.get("has_default"):
            binding = {"kind": "default", "expression": None, "default": port.get("default")}
        else:
            binding = {"kind": "unbound", "expression": None}
        inputs.append(
            {
                "name": name,
                "annotation": port.get("annotation"),
                "required": bool(port.get("required")),
                "has_default": bool(port.get("has_default")),
                "binding": binding,
            }
        )
    for name, expression in bound.items():
        if name not in input_names:
            orphaned.append(
                {
                    "name": name,
                    "expression": expression,
                    "kind": binding_kind(expression, result_vars),
                }
            )
    output_count = len(contract_outputs)
    output_ports: list[dict[str, Any]] = []
    for port in contract_outputs:
        preview = None
        if cached and preview_for is not None:
            try:
                preview = preview_for(port.get("index", 0), output_count)
            except Exception:
                preview = None
        output_ports.append(
            {
                "index": port.get("index"),
                "name": port.get("name"),
                "declared_type": port.get("declared_type"),
                "expressions": list(port.get("expressions", [])),
                "has_cached_value": cached,
                "available": bool(fresh),
                "stale": bool(cached and not fresh),
                "runtime_preview": preview,
            }
        )
    if disabled:
        state = "disabled"
    elif not component_available:
        state = "component_unavailable"
    elif isinstance(dependency_state, Mapping) and dependency_state.get("state") == "blocked":
        state = "blocked"
    elif has_errors:
        state = "error"
    elif any(item["binding"]["kind"] == "unbound" and item["required"] for item in inputs):
        state = "incomplete"
    elif fresh:
        state = "ok"
    elif cached:
        state = "stale"
    else:
        state = "idle"
    symbol = call_target(instance.node_name, instance.component_path)
    instance_view = {
        "instance_id": instance.instance_id,
        "node": instance.node_name,
        "component_id": instance.component_path,
        "result_var": instance.result_var,
        "runtime_symbol": symbol,
        "disabled": disabled,
        "state": state,
        "bindings": [
            {"name": binding.input_name, "expression": binding.expression}
            for binding in instance.bindings
        ],
        "orphaned_bindings": orphaned,
        "inputs": inputs,
        "outputs": output_ports,
        "contract_diagnostic": (contract or {}).get("diagnostic"),
    }
    component_view = None
    if instance.component_path is not None:
        entry = component_entry or {}
        component_view = {
            "component_id": instance.component_path,
            "callable": instance.node_name,
            "source_sha256": entry.get("source_sha256"),
            "runtime_symbol": symbol,
            "library_cell_id": entry.get("cell_id"),
            "available": component_available,
        }
    return {"instance": instance_view, "component": component_view}


def expand_binding_edges(
    edges: list[Any], instances_by_cell: Mapping[str, InstanceSpec]
) -> list[Any]:
    """Expand marimo variable edges into one Graphimo edge per target binding.

    The target instance's bindings decide the input port regardless of the
    source cell type (P0.2), several bindings may consume the same variable
    (P0.1), and subscript bindings carry their zero-based ``output_index``.
    Edges added for disabled nodes keep their annotations because this runs on
    the final edge list (P0.3).
    """
    expanded: list[Any] = []
    seen: set[tuple[Any, ...]] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        target = instances_by_cell.get(edge.get("target_cell_id"))
        variable = edge.get("variable")
        matches: list[dict[str, Any]] = []
        if target is not None and isinstance(variable, str):
            source = instances_by_cell.get(edge.get("source_cell_id"))
            result_var = source.result_var if source is not None else None
            for binding in target.bindings:
                expression = binding.expression.strip()
                if expression == variable:
                    matches.append(
                        {
                            "input_ref": binding.input_name,
                            "output_index": 0 if result_var == variable else None,
                        }
                    )
                elif (
                    result_var is not None
                    and expression.startswith(result_var + "[")
                    and expression.endswith("]")
                    and expression[len(result_var) + 1 : -1].isdigit()
                ):
                    matches.append(
                        {
                            "input_ref": binding.input_name,
                            "output_index": int(expression[len(result_var) + 1 : -1]),
                        }
                    )
        if not matches:
            expanded.append(edge)
            continue
        for match in matches:
            key = (
                edge.get("source_cell_id"),
                edge.get("target_cell_id"),
                variable,
                match["input_ref"],
                match["output_index"],
            )
            if key in seen:
                continue
            seen.add(key)
            copy = dict(edge)
            copy["input_ref"] = match["input_ref"]
            if match["output_index"] is not None:
                copy["output_index"] = match["output_index"]
            expanded.append(copy)
    return expanded
