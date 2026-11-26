"""Host-independent PyTorch view models for Graphimo renderers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .compiler import PyTorchCompilationError, compile_model
from .registry import ELEMENT_REGISTRY
from .schema import GroupDefinition, MethodGraph, PyTorchMetaBlockState
from .shapes import infer_model_shapes


def project_model(
    state: PyTorchMetaBlockState,
    scope: str,
    *,
    external_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project semantic PyTorch state into bounded renderer data.

    The projection contains no ``nn.Module`` instances. A renderer can draw and
    edit the graph using only JSON-compatible descriptors and application
    intents.
    """
    values = dict(external_values or {})
    method, group = _resolve_scope(state, scope)
    graph: MethodGraph | GroupDefinition = (
        method or group or next(item for item in state.entrypoints if item.name == "forward")
    )
    inference = infer_model_shapes(state, values)
    shape_prefixes = _shape_prefixes(state, method, group)
    elements = []
    for element in graph.elements:
        descriptor = ELEMENT_REGISTRY.get(element.kind)
        parameter_specs = []
        if descriptor:
            parameter_specs = [
                {
                    "name": spec.name,
                    "type": spec.type,
                    "default": list(spec.default)
                    if isinstance(spec.default, tuple)
                    else spec.default,
                    "minimum": spec.minimum,
                    "maximum": spec.maximum,
                    "choices": list(spec.choices) if spec.choices is not None else None,
                    "structural": spec.structural,
                    "value": element.params.get(spec.name, spec.default),
                    "binding": (
                        element.parameter_bindings[spec.name].external_parameter_id
                        if spec.name in element.parameter_bindings
                        else None
                    ),
                }
                for spec in descriptor.parameters
            ]
        elements.append(
            {
                "id": element.id,
                "kind": element.kind,
                "name": element.name,
                "params": element.params,
                "inputs": [
                    {"name": port.name, "required": port.required, "variadic": port.variadic}
                    for port in descriptor.input_ports
                ]
                if descriptor
                else [],
                "outputs": [
                    {"name": port.name, "required": port.required, "variadic": port.variadic}
                    for port in descriptor.output_ports
                ]
                if descriptor
                else [],
                "parameters": parameter_specs,
                "output_shape": _first_shape(
                    inference.port_shapes,
                    shape_prefixes,
                    element.id,
                    "output",
                ),
                "shape_variants": _shape_variants(
                    inference.port_shapes, shape_prefixes, element.id, "output"
                ),
            }
        )
    group_definitions = {item.id: item for item in state.groups}
    for instance in graph.group_instances:
        definition = group_definitions.get(instance.group_id)
        elements.append(
            {
                "id": instance.id,
                "kind": "group_instance",
                "name": instance.name,
                "group_id": instance.group_id,
                "repeat": instance.repeat,
                "params": instance.params,
                "inputs": [
                    {"name": item.name, "required": True, "variadic": False}
                    for item in (definition.inputs if definition else ())
                ],
                "outputs": [
                    {"name": item.name, "required": True, "variadic": False}
                    for item in (definition.outputs if definition else ())
                ],
                "parameters": [
                    {
                        "name": "repeat",
                        "type": "int",
                        "default": 1,
                        "minimum": 1,
                        "maximum": None,
                        "choices": None,
                        "structural": True,
                        "value": instance.repeat,
                        "binding": None,
                    }
                ],
                "output_shape": _first_group_output_shape(
                    inference.port_shapes, shape_prefixes, instance.id, definition
                ),
            }
        )
    edges = [
        {
            "id": edge.id,
            "source": {"element_id": edge.source.element_id, "port": edge.source.port},
            "target": {"element_id": edge.target.element_id, "port": edge.target.port},
            "shape": _first_edge_shape(inference.edge_shapes, shape_prefixes, edge.id),
        }
        for edge in graph.edges
    ]
    diagnostics = [_diagnostic_payload(item) for item in inference.diagnostics]
    generated_source = None
    if not any(item["severity"] == "error" for item in diagnostics):
        try:
            generated_source = compile_model(state, values).generated_source
        except PyTorchCompilationError as exc:
            diagnostics.extend(_diagnostic_payload(item) for item in exc.diagnostics)
    palette = _palette()
    return {
        "scope": scope,
        "name": state.name,
        "dialect_id": "pytorch.model",
        "outer": {
            "kind": "metablock",
            "name": state.name,
            "inputs": [
                asdict(item)
                for item in (
                    method or next(item for item in state.entrypoints if item.name == "forward")
                ).inputs
            ],
            "outputs": [
                {"name": item.name, "endpoint": asdict(item.endpoint)}
                for item in (
                    method or next(item for item in state.entrypoints if item.name == "forward")
                ).outputs
            ],
        },
        "method_tabs": [
            {
                "name": item.name,
                "inputs": [asdict(port) for port in item.inputs],
                "outputs": [
                    {"name": output.name, "endpoint": asdict(output.endpoint)}
                    for output in item.outputs
                ],
            }
            for item in state.entrypoints
        ],
        "active_method": method.name if method else None,
        "groups": [
            {
                "id": item.id,
                "name": item.name,
                "inputs": [
                    {"name": port.name, "endpoint": asdict(port.endpoint)} for port in item.inputs
                ],
                "outputs": [
                    {"name": port.name, "endpoint": asdict(port.endpoint)} for port in item.outputs
                ],
                "element_count": len(item.elements),
            }
            for item in state.groups
        ],
        "group_templates": [
            {
                "id": item.id,
                "name": item.name,
                "inputs": len(item.inputs),
                "outputs": len(item.outputs),
            }
            for item in state.groups
        ],
        "group_instances": [asdict(item) for item in graph.group_instances],
        "elements": elements,
        "edges": edges,
        "palette": palette,
        "external_parameters": [
            {
                "id": item.id,
                "name": item.name,
                "type": item.type,
                "default": item.default,
                "structural": item.structural,
            }
            for item in state.external_parameters
        ],
        "diagnostics": diagnostics,
        "generated_source": generated_source,
        "runtime_selector": {
            "available": False,
            "items": [],
            "reason": "runtime inspection is a later addon milestone",
        },
        "live_summaries": {"activations": [], "gradients": [], "metrics": []},
        "torchlens": {"available": False, "inspections": []},
        "checkpoints": {"available": False, "reports": []},
    }


def _resolve_scope(
    state: PyTorchMetaBlockState, scope: str
) -> tuple[MethodGraph | None, GroupDefinition | None]:
    if scope in {"root", "forward", "method:forward"}:
        return next(item for item in state.entrypoints if item.name == "forward"), None
    name = scope.removeprefix("method:")
    method = next((item for item in state.entrypoints if item.name == name), None)
    if method:
        return method, None
    group_id = scope.removeprefix("group:")
    group = next((item for item in state.groups if item.id == group_id), None)
    if group:
        return None, group
    raise ValueError(f"unknown PyTorch MetaBlock scope {scope!r}")


def _shape_prefixes(
    state: PyTorchMetaBlockState,
    method: MethodGraph | None,
    group: GroupDefinition | None,
) -> tuple[str, ...]:
    if method is not None:
        return (method.name,)
    if group is None:
        return ()
    groups = {item.id: item for item in state.groups}
    found: list[str] = []

    def visit(graph: MethodGraph | GroupDefinition, scope: str, ancestors: frozenset[str]) -> None:
        for instance in graph.group_instances:
            definition = groups.get(instance.group_id)
            if definition is None or instance.group_id in ancestors:
                continue
            for repeat_index in range(instance.repeat):
                child_scope = f"{scope}/{instance.name}/repeat-{repeat_index + 1}"
                if definition.id == group.id:
                    found.append(child_scope)
                visit(definition, child_scope, ancestors | {instance.group_id})

    for entrypoint in state.entrypoints:
        visit(entrypoint, entrypoint.name, frozenset())
    return tuple(found)


def _shape_variants(shape_map: Any, prefixes: tuple[str, ...], element_id: str, port: str):
    return [
        _shape_payload(shape_map[f"{prefix}/{element_id}/{port}"])
        for prefix in prefixes
        if f"{prefix}/{element_id}/{port}" in shape_map
    ]


def _first_shape(shape_map: Any, prefixes: tuple[str, ...], element_id: str, port: str):
    variants = _shape_variants(shape_map, prefixes, element_id, port)
    return variants[0] if variants else None


def _first_edge_shape(shape_map: Any, prefixes: tuple[str, ...], edge_id: str):
    for prefix in prefixes:
        spec = shape_map.get(f"{prefix}/{edge_id}")
        if spec is not None:
            return _shape_payload(spec)
    return None


def _first_group_output_shape(
    shape_map: Any, prefixes: tuple[str, ...], instance_id: str, definition: GroupDefinition | None
):
    if definition is None or not definition.outputs:
        return None
    return _first_shape(shape_map, prefixes, instance_id, definition.outputs[0].name)


def _palette() -> list[dict[str, Any]]:
    result = []
    for kind, descriptor in ELEMENT_REGISTRY.items():
        if kind == "group_instance":
            continue
        result.append(
            {
                "id": kind,
                "kind": kind,
                "category": _category(kind),
                "inputs": [port.name for port in descriptor.input_ports],
                "outputs": [port.name for port in descriptor.output_ports],
                "parameters": [
                    {
                        "name": spec.name,
                        "type": spec.type,
                        "default": list(spec.default)
                        if isinstance(spec.default, tuple)
                        else spec.default,
                        "minimum": spec.minimum,
                        "maximum": spec.maximum,
                        "choices": list(spec.choices) if spec.choices is not None else None,
                        "structural": spec.structural,
                    }
                    for spec in descriptor.parameters
                ],
            }
        )
    return result


def _category(kind: str) -> str:
    if kind in {"input", "output"}:
        return "I/O"
    if kind in {"embedding", "linear", "rms_norm", "dropout"}:
        return "Layers"
    if kind in {"relu", "gelu", "silu", "softmax", "rope", "causal_mask"}:
        return "Activations and transforms"
    if kind in {"add", "multiply", "matmul", "scale", "concatenate", "cross_entropy"}:
        return "Operations"
    return "Shape operations"


def _shape_payload(spec: Any) -> dict[str, Any] | None:
    if spec is None:
        return None
    return {
        "shape": [item.expr for item in spec.shape],
        "dtype": spec.dtype,
        "semantic": spec.semantic,
    }


def _diagnostic_payload(item: Any) -> dict[str, Any]:
    return {
        "code": item.code,
        "message": item.message,
        "severity": item.severity,
        "element_id": item.element_id,
        "edge_id": item.edge_id,
        "group_id": item.group_id,
        "method_name": item.method_name,
    }
