"""Pure semantic edits used by addon controllers, independent of renderers."""

from __future__ import annotations

import math
import uuid
from dataclasses import replace
from typing import Mapping

from .diagnostics import GraphimoPytorchError
from .registry import ELEMENT_REGISTRY, ParameterSpec
from .schema import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    ExternalParameter,
    JsonScalar,
    JsonValue,
    PyTorchMetaBlockState,
)


def set_element_parameter(
    state: PyTorchMetaBlockState,
    scope_id: str,
    element_id: str,
    parameter_name: str,
    value: JsonScalar,
) -> PyTorchMetaBlockState:
    """Return a new state with one literal or promoted parameter value changed."""
    scope, element = _find_element(state, scope_id, element_id)
    descriptor = _parameter_descriptor(element, parameter_name)
    params = dict(element.params)
    bindings = dict(element.parameter_bindings)
    ext_id = bindings.get(parameter_name)
    if ext_id is None:
        params[parameter_name] = value
        _validate_parameter_value(element.kind, descriptor, params, state, element)
        updated = replace(element, params=params)
        return _replace_element(state, scope, updated)

    external = next(
        (item for item in state.external_parameters if item.id == ext_id.external_parameter_id),
        None,
    )
    if external is None:
        raise GraphimoPytorchError(
            "pytorch_external_parameter_unresolved",
            f"External parameter {ext_id.external_parameter_id!r} does not exist",
        )
    params[parameter_name] = value
    _validate_parameter_value(element.kind, descriptor, params, state, element)
    external_parameters = tuple(
        replace(item, default=value) if item.id == external.id else item
        for item in state.external_parameters
    )
    return replace(state, external_parameters=external_parameters)


def promote_parameter(
    state: PyTorchMetaBlockState,
    scope_id: str,
    element_id: str,
    parameter_name: str,
    *,
    external_parameter_id: str | None = None,
    external_name: str | None = None,
) -> PyTorchMetaBlockState:
    """Promote a scalar literal to a bound MetaBlock input parameter."""
    scope, element = _find_element(state, scope_id, element_id)
    descriptor = _parameter_descriptor(element, parameter_name)
    current = element.params.get(parameter_name, descriptor.default)
    if current is None or isinstance(current, (list, dict)):
        raise GraphimoPytorchError(
            "pytorch_parameter_promotion_unsupported",
            f"{parameter_name!r} has no promotable scalar value",
        )
    _validate_parameter_value(
        element.kind, descriptor, {**element.params, parameter_name: current}, state, element
    )
    identifier = external_parameter_id or f"p-{uuid.uuid4().hex[:12]}"
    if any(item.id == identifier for item in state.external_parameters):
        raise GraphimoPytorchError(
            "pytorch_external_parameter_duplicate",
            f"External parameter id {identifier!r} already exists",
        )
    name = external_name or parameter_name
    external = ExternalParameter(identifier, name, descriptor.type, current, descriptor.structural)
    params = dict(element.params)
    params.pop(parameter_name, None)
    bindings = dict(element.parameter_bindings)
    from .schema import ParameterBinding

    bindings[parameter_name] = ParameterBinding(identifier)
    updated = replace(element, params=params, parameter_bindings=bindings)
    replaced = _replace_element(state, scope, updated)
    return replace(replaced, external_parameters=(*replaced.external_parameters, external))


def demote_parameter(
    state: PyTorchMetaBlockState,
    scope_id: str,
    element_id: str,
    parameter_name: str,
    *,
    external_values: Mapping[str, JsonScalar] | None = None,
) -> PyTorchMetaBlockState:
    """Replace an external binding with its current/default literal value."""
    scope, element = _find_element(state, scope_id, element_id)
    binding = element.parameter_bindings.get(parameter_name)
    if binding is None:
        return state
    external = next(
        (item for item in state.external_parameters if item.id == binding.external_parameter_id),
        None,
    )
    if external is None:
        raise GraphimoPytorchError(
            "pytorch_external_parameter_unresolved",
            f"External parameter {binding.external_parameter_id!r} does not exist",
        )
    value = (external_values or {}).get(external.id, external.default)
    params = dict(element.params)
    params[parameter_name] = value
    _validate_parameter_value(
        element.kind, _parameter_descriptor(element, parameter_name), params, state, element
    )
    bindings = dict(element.parameter_bindings)
    del bindings[parameter_name]
    updated = replace(element, params=params, parameter_bindings=bindings)
    replaced = _replace_element(state, scope, updated)
    if _binding_count(replaced, external.id) == 0:
        replaced = replace(
            replaced,
            external_parameters=tuple(
                item for item in replaced.external_parameters if item.id != external.id
            ),
        )
    return replaced


def add_element(
    state: PyTorchMetaBlockState,
    scope_id: str,
    element_id: str,
    kind_name: str,
    name: str,
    params: Mapping[str, JsonValue] | None = None,
) -> PyTorchMetaBlockState:
    """Add one registry element to an entrypoint or reusable Group scope."""
    if not element_id or not name:
        raise GraphimoPytorchError(
            "pytorch_element_invalid", "Element ID and name must be non-empty"
        )
    kind = ELEMENT_REGISTRY.get(kind_name)
    if kind is None or kind_name == "method_call":
        raise GraphimoPytorchError(
            "pytorch_element_kind_unknown", f"Unknown insertable element kind {kind_name!r}"
        )
    if any(
        element.id == element_id
        for scope in (*state.entrypoints, *state.groups)
        for element in (*scope.elements, *scope.group_instances)
    ):
        raise GraphimoPytorchError(
            "pytorch_duplicate_element_id", f"Element ID {element_id!r} already exists"
        )
    values = dict(params or {})
    if any(not isinstance(key, str) for key in values):
        raise GraphimoPytorchError(
            "pytorch_element_parameters_invalid", "Element parameter names must be strings"
        )
    diagnostics = kind.validate_params(values)
    invalid = next(
        (item for item in diagnostics if item.code != "pytorch_parameter_required"), None
    )
    if invalid:
        raise GraphimoPytorchError(invalid.code, invalid.message)
    element = ElementRecord(element_id, kind_name, name, values)
    if any(method.name == scope_id for method in state.entrypoints):
        methods = tuple(
            replace(method, elements=(*method.elements, element))
            if method.name == scope_id
            else method
            for method in state.entrypoints
        )
        return replace(state, entrypoints=methods)
    if any(group.id == scope_id for group in state.groups):
        groups = tuple(
            replace(group, elements=(*group.elements, element)) if group.id == scope_id else group
            for group in state.groups
        )
        return replace(state, groups=groups)
    raise GraphimoPytorchError("pytorch_scope_not_found", f"Scope {scope_id!r} does not exist")


def connect_elements(
    state: PyTorchMetaBlockState,
    scope_id: str,
    edge_id: str,
    source_element_id: str,
    source_port: str,
    target_element_id: str,
    target_port: str,
) -> PyTorchMetaBlockState:
    """Add a typed semantic edge after validating both endpoint ports."""
    if not all((edge_id, source_element_id, source_port, target_element_id, target_port)):
        raise GraphimoPytorchError(
            "pytorch_edge_invalid", "Edge ID and endpoint ports are required"
        )
    scope = _scope(state, scope_id)
    if scope is None:
        raise GraphimoPytorchError("pytorch_scope_not_found", f"Scope {scope_id!r} does not exist")
    if any(
        edge.id == edge_id
        for candidate in (*state.entrypoints, *state.groups)
        for edge in candidate.edges
    ):
        raise GraphimoPytorchError(
            "pytorch_duplicate_edge_id", f"Edge ID {edge_id!r} already exists"
        )
    source_outputs = _scope_ports(state, scope, source_element_id, output=True)
    target_inputs = _scope_ports(state, scope, target_element_id, output=False)
    if source_port not in source_outputs:
        raise GraphimoPytorchError(
            "pytorch_output_port_not_found",
            f"Output {source_port!r} does not exist on {source_element_id!r}",
        )
    if target_port not in target_inputs:
        raise GraphimoPytorchError(
            "pytorch_input_port_not_found",
            f"Input {target_port!r} does not exist on {target_element_id!r}",
        )
    if not target_inputs[target_port].variadic and any(
        edge.target.element_id == target_element_id and edge.target.port == target_port
        for edge in scope.edges
    ):
        raise GraphimoPytorchError(
            "pytorch_input_already_connected", f"Input {target_port!r} already has a connection"
        )
    adjacency: dict[str, set[str]] = {}
    for edge in scope.edges:
        adjacency.setdefault(edge.source.element_id, set()).add(edge.target.element_id)
    pending = [target_element_id]
    visited = set()
    while pending:
        current = pending.pop()
        if current == source_element_id:
            raise GraphimoPytorchError(
                "pytorch_graph_cycle", "The connection would create a directed cycle"
            )
        if current in visited:
            continue
        visited.add(current)
        pending.extend(adjacency.get(current, ()))
    edge = EdgeRecord(
        edge_id, Endpoint(source_element_id, source_port), Endpoint(target_element_id, target_port)
    )
    if any(method.name == scope_id for method in state.entrypoints):
        methods = tuple(
            replace(method, edges=(*method.edges, edge)) if method.name == scope_id else method
            for method in state.entrypoints
        )
        if methods != state.entrypoints:
            return replace(state, entrypoints=methods)
    groups = tuple(
        replace(group, edges=(*group.edges, edge)) if group.id == scope_id else group
        for group in state.groups
    )
    return replace(state, groups=groups)


def disconnect_edge(
    state: PyTorchMetaBlockState, scope_id: str, edge_id: str
) -> PyTorchMetaBlockState:
    """Remove one edge from exactly the selected semantic scope."""
    if any(method.name == scope_id for method in state.entrypoints):
        methods = tuple(
            replace(method, edges=tuple(edge for edge in method.edges if edge.id != edge_id))
            if method.name == scope_id
            else method
            for method in state.entrypoints
        )
        if methods == state.entrypoints:
            raise GraphimoPytorchError("pytorch_edge_not_found", f"Edge {edge_id!r} does not exist")
        return replace(state, entrypoints=methods)
    if any(group.id == scope_id for group in state.groups):
        groups = tuple(
            replace(group, edges=tuple(edge for edge in group.edges if edge.id != edge_id))
            if group.id == scope_id
            else group
            for group in state.groups
        )
        if groups == state.groups:
            raise GraphimoPytorchError("pytorch_edge_not_found", f"Edge {edge_id!r} does not exist")
        return replace(state, groups=groups)
    raise GraphimoPytorchError("pytorch_scope_not_found", f"Scope {scope_id!r} does not exist")


def _scope(state: PyTorchMetaBlockState, scope_id: str):
    return next(
        (
            item
            for item in (*state.entrypoints, *state.groups)
            if getattr(item, "name", None) == scope_id or getattr(item, "id", None) == scope_id
        ),
        None,
    )


def _scope_ports(state: PyTorchMetaBlockState, scope: object, element_id: str, *, output: bool):
    from .registry import PortSpec

    elements = getattr(scope, "elements", ())
    element = next((item for item in elements if item.id == element_id), None)
    if element is not None:
        kind = ELEMENT_REGISTRY.get(element.kind)
        if kind is None:
            return {}
        ports = kind.output_ports if output else kind.input_ports
        return {item.name: item for item in ports}
    groups = {group.id: group for group in state.groups}
    instance = next(
        (item for item in getattr(scope, "group_instances", ()) if item.id == element_id), None
    )
    if instance is not None:
        definition = groups.get(instance.group_id)
        ports = (definition.outputs if output else definition.inputs) if definition else ()
        return {item.name: PortSpec(item.name) for item in ports}
    if hasattr(scope, "inputs") and hasattr(scope, "outputs"):
        ports = getattr(scope, "inputs" if output else "outputs", ())
        found = next((item for item in ports if item.id == element_id), None)
        if found is not None:
            return {"out" if output else "x": PortSpec("out" if output else "x")}
    return {}


def _find_element(
    state: PyTorchMetaBlockState, scope_id: str, element_id: str
) -> tuple[str, ElementRecord]:
    for method in state.entrypoints:
        if method.name == scope_id:
            found = next((item for item in method.elements if item.id == element_id), None)
            if found:
                return scope_id, found
    for group in state.groups:
        if group.id == scope_id:
            found = next((item for item in group.elements if item.id == element_id), None)
            if found:
                return scope_id, found
    raise GraphimoPytorchError(
        "pytorch_element_not_found", f"Element {element_id!r} was not found in scope {scope_id!r}"
    )


def _replace_element(
    state: PyTorchMetaBlockState, scope_id: str, updated: ElementRecord
) -> PyTorchMetaBlockState:
    methods = tuple(
        replace(
            method,
            elements=tuple(updated if item.id == updated.id else item for item in method.elements),
        )
        if method.name == scope_id
        else method
        for method in state.entrypoints
    )
    groups = tuple(
        replace(
            group,
            elements=tuple(updated if item.id == updated.id else item for item in group.elements),
        )
        if group.id == scope_id
        else group
        for group in state.groups
    )
    return replace(state, entrypoints=methods, groups=groups)


def _parameter_descriptor(element: ElementRecord, name: str) -> ParameterSpec:
    kind = ELEMENT_REGISTRY.get(element.kind)
    descriptor = (
        next((item for item in kind.parameters if item.name == name), None) if kind else None
    )
    if descriptor is None:
        raise GraphimoPytorchError(
            "pytorch_parameter_unknown", f"Unknown parameter {name!r} for {element.kind!r}"
        )
    return descriptor


def _validate_parameter_value(
    kind_name: str,
    descriptor: ParameterSpec,
    params: Mapping[str, JsonValue],
    state: PyTorchMetaBlockState,
    element: ElementRecord,
) -> None:
    kind = ELEMENT_REGISTRY[kind_name]
    effective = dict(params)
    for key, binding in element.parameter_bindings.items():
        external = next(
            (
                item
                for item in state.external_parameters
                if item.id == binding.external_parameter_id
            ),
            None,
        )
        if external is not None:
            effective.setdefault(key, external.default)
    diagnostics = kind.validate_params(effective)
    invalid = next(
        (item for item in diagnostics if item.code != "pytorch_parameter_required"), None
    )
    if invalid:
        raise GraphimoPytorchError(invalid.code, f"{element.name}: {invalid.message}")
    value = effective.get(descriptor.name)
    if type(value) is float and not math.isfinite(value):
        raise GraphimoPytorchError(
            "pytorch_parameter_invalid", f"{descriptor.name!r} must be finite"
        )


def _binding_count(state: PyTorchMetaBlockState, external_id: str) -> int:
    scopes = [*state.entrypoints, *state.groups]
    return sum(
        binding.external_parameter_id == external_id
        for scope in scopes
        for element in scope.elements
        for binding in element.parameter_bindings.values()
    )
