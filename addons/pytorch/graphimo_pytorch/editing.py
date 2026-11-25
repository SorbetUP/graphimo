"""Pure semantic edits used by addon controllers, independent of renderers."""

from __future__ import annotations

import math
import uuid
from dataclasses import replace
from typing import Mapping

from .diagnostics import GraphimoPytorchError
from .registry import ELEMENT_REGISTRY, ParameterSpec
from .schema import ElementRecord, ExternalParameter, JsonScalar, JsonValue, PyTorchMetaBlockState


def set_element_parameter(
    state: PyTorchMetaBlockState, scope_id: str, element_id: str, parameter_name: str, value: JsonScalar,
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

    external = next((item for item in state.external_parameters if item.id == ext_id.external_parameter_id), None)
    if external is None:
        raise GraphimoPytorchError("pytorch_external_parameter_unresolved", f"External parameter {ext_id.external_parameter_id!r} does not exist")
    params[parameter_name] = value
    _validate_parameter_value(element.kind, descriptor, params, state, element)
    external_parameters = tuple(replace(item, default=value) if item.id == external.id else item for item in state.external_parameters)
    return replace(state, external_parameters=external_parameters)


def promote_parameter(
    state: PyTorchMetaBlockState, scope_id: str, element_id: str, parameter_name: str,
    *, external_parameter_id: str | None = None, external_name: str | None = None,
) -> PyTorchMetaBlockState:
    """Promote a scalar literal to a bound MetaBlock input parameter."""
    scope, element = _find_element(state, scope_id, element_id)
    descriptor = _parameter_descriptor(element, parameter_name)
    current = element.params.get(parameter_name, descriptor.default)
    if current is None or isinstance(current, (list, dict)):
        raise GraphimoPytorchError("pytorch_parameter_promotion_unsupported", f"{parameter_name!r} has no promotable scalar value")
    _validate_parameter_value(element.kind, descriptor, {**element.params, parameter_name: current}, state, element)
    identifier = external_parameter_id or f"p-{uuid.uuid4().hex[:12]}"
    if any(item.id == identifier for item in state.external_parameters):
        raise GraphimoPytorchError("pytorch_external_parameter_duplicate", f"External parameter id {identifier!r} already exists")
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
    state: PyTorchMetaBlockState, scope_id: str, element_id: str, parameter_name: str,
    *, external_values: Mapping[str, JsonScalar] | None = None,
) -> PyTorchMetaBlockState:
    """Replace an external binding with its current/default literal value."""
    scope, element = _find_element(state, scope_id, element_id)
    binding = element.parameter_bindings.get(parameter_name)
    if binding is None:
        return state
    external = next((item for item in state.external_parameters if item.id == binding.external_parameter_id), None)
    if external is None:
        raise GraphimoPytorchError("pytorch_external_parameter_unresolved", f"External parameter {binding.external_parameter_id!r} does not exist")
    value = (external_values or {}).get(external.id, external.default)
    params = dict(element.params)
    params[parameter_name] = value
    _validate_parameter_value(element.kind, _parameter_descriptor(element, parameter_name), params, state, element)
    bindings = dict(element.parameter_bindings)
    del bindings[parameter_name]
    updated = replace(element, params=params, parameter_bindings=bindings)
    replaced = _replace_element(state, scope, updated)
    if _binding_count(replaced, external.id) == 0:
        replaced = replace(replaced, external_parameters=tuple(item for item in replaced.external_parameters if item.id != external.id))
    return replaced


def _find_element(state: PyTorchMetaBlockState, scope_id: str, element_id: str) -> tuple[str, ElementRecord]:
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
    raise GraphimoPytorchError("pytorch_element_not_found", f"Element {element_id!r} was not found in scope {scope_id!r}")


def _replace_element(state: PyTorchMetaBlockState, scope_id: str, updated: ElementRecord) -> PyTorchMetaBlockState:
    methods = tuple(
        replace(method, elements=tuple(updated if item.id == updated.id else item for item in method.elements))
        if method.name == scope_id else method
        for method in state.entrypoints
    )
    groups = tuple(
        replace(group, elements=tuple(updated if item.id == updated.id else item for item in group.elements))
        if group.id == scope_id else group
        for group in state.groups
    )
    return replace(state, entrypoints=methods, groups=groups)


def _parameter_descriptor(element: ElementRecord, name: str) -> ParameterSpec:
    kind = ELEMENT_REGISTRY.get(element.kind)
    descriptor = next((item for item in kind.parameters if item.name == name), None) if kind else None
    if descriptor is None:
        raise GraphimoPytorchError("pytorch_parameter_unknown", f"Unknown parameter {name!r} for {element.kind!r}")
    return descriptor


def _validate_parameter_value(
    kind_name: str, descriptor: ParameterSpec, params: Mapping[str, JsonValue],
    state: PyTorchMetaBlockState, element: ElementRecord,
) -> None:
    kind = ELEMENT_REGISTRY[kind_name]
    effective = dict(params)
    for key, binding in element.parameter_bindings.items():
        external = next((item for item in state.external_parameters if item.id == binding.external_parameter_id), None)
        if external is not None:
            effective.setdefault(key, external.default)
    diagnostics = kind.validate_params(effective)
    if diagnostics:
        raise GraphimoPytorchError(diagnostics[0].code, f"{element.name}: {diagnostics[0].message}")
    value = effective.get(descriptor.name)
    if type(value) is float and not math.isfinite(value):
        raise GraphimoPytorchError("pytorch_parameter_invalid", f"{descriptor.name!r} must be finite")


def _binding_count(state: PyTorchMetaBlockState, external_id: str) -> int:
    scopes = [*state.entrypoints, *state.groups]
    return sum(
        binding.external_parameter_id == external_id
        for scope in scopes
        for element in scope.elements
        for binding in element.parameter_bindings.values()
    )

