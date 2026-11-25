"""Deterministic JSON conversion and atomic persistence for addon state."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from .diagnostics import GraphimoPytorchError
from .schema import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    ExternalParameter,
    GroupDefinition,
    GroupInstance,
    GroupPort,
    MethodGraph,
    MethodInput,
    MethodOutput,
    ParameterBinding,
    PyTorchMetaBlockState,
    TensorContract,
    JsonValue,
)


def state_to_dict(state: PyTorchMetaBlockState) -> dict[str, Any]:
    return {
        "schema_version": state.schema_version,
        "metablock_id": state.metablock_id,
        "name": state.name,
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
        "groups": [_group_to_dict(item) for item in state.groups],
        "entrypoints": [_method_to_dict(item) for item in state.entrypoints],
        "metadata": state.metadata,
    }


def state_from_dict(value: dict[str, Any]) -> PyTorchMetaBlockState:
    try:
        if not isinstance(value, dict):
            raise TypeError("state root must be an object")
        version = value.get("schema_version")
        if type(version) is not int or version != 1:
            raise GraphimoPytorchError(
                "pytorch_state_schema_unsupported",
                f"Unsupported PyTorch MetaBlock schema version: {version}",
            )
        external = _list(value, "external_parameters")
        groups = _list(value, "groups")
        entrypoints = _list(value, "entrypoints")
        metadata = _object(value, "metadata", default={})
        return PyTorchMetaBlockState(
            schema_version=1,
            metablock_id=_string(value, "metablock_id"),
            name=_string(value, "name"),
            external_parameters=tuple(_external_from_dict(item) for item in external),
            groups=tuple(_group_from_dict(item) for item in groups),
            entrypoints=tuple(_method_from_dict(item) for item in entrypoints),
            metadata=_json_object(metadata, "metadata"),
        )
    except GraphimoPytorchError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise GraphimoPytorchError("pytorch_state_invalid", f"Invalid PyTorch MetaBlock state: {exc}") from exc


def load_state(path: Path) -> PyTorchMetaBlockState:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GraphimoPytorchError("pytorch_state_invalid", f"Could not load PyTorch MetaBlock state: {exc}") from exc
    return state_from_dict(payload)


def save_state(path: Path, state: PyTorchMetaBlockState) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(state_to_dict(state), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temp_path.unlink(missing_ok=True)


def canonical_state_json(state: PyTorchMetaBlockState) -> str:
    return json.dumps(
        state_to_dict(state),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _element_to_dict(item: ElementRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "name": item.name,
        "params": item.params,
        "parameter_bindings": {
            name: {"external_parameter_id": binding.external_parameter_id}
            for name, binding in item.parameter_bindings.items()
        },
        "metadata": item.metadata,
    }


def _edge_to_dict(item: EdgeRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "source": {"element_id": item.source.element_id, "port": item.source.port},
        "target": {"element_id": item.target.element_id, "port": item.target.port},
    }


def _tensor_to_dict(item: TensorContract) -> dict[str, Any]:
    result = {"shape": list(item.shape), "dtype": item.dtype}
    if item.semantic is not None:
        result["semantic"] = item.semantic
    return result


def _group_port_to_dict(item: GroupPort) -> dict[str, Any]:
    result = {"id": item.id, "name": item.name}
    if item.tensor is not None:
        result["tensor"] = _tensor_to_dict(item.tensor)
    return result


def _group_instance_to_dict(item: GroupInstance) -> dict[str, Any]:
    return {"id": item.id, "group_id": item.group_id, "name": item.name, "repeat": item.repeat, "params": item.params}


def _group_to_dict(item: GroupDefinition) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "inputs": [_group_port_to_dict(port) for port in item.inputs],
        "outputs": [_group_port_to_dict(port) for port in item.outputs],
        "elements": [_element_to_dict(element) for element in item.elements],
        "edges": [_edge_to_dict(edge) for edge in item.edges],
        "group_instances": [_group_instance_to_dict(group) for group in item.group_instances],
    }


def _method_to_dict(item: MethodGraph) -> dict[str, Any]:
    return {
        "name": item.name,
        "inputs": [
            {"id": port.id, "name": port.name, "tensor": _tensor_to_dict(port.tensor)}
            for port in item.inputs
        ],
        "outputs": [{"id": port.id, "name": port.name} for port in item.outputs],
        "elements": [_element_to_dict(element) for element in item.elements],
        "edges": [_edge_to_dict(edge) for edge in item.edges],
        "group_instances": [_group_instance_to_dict(group) for group in item.group_instances],
    }


def _external_from_dict(value: Any) -> ExternalParameter:
    value = _as_object(value, "external parameter")
    return ExternalParameter(
        _string(value, "id"),
        _string(value, "name"),
        _string(value, "type"),
        _json_value(value.get("default"), "external parameter.default"),
        _bool(value, "structural"),
    )


def _element_from_dict(value: Any) -> ElementRecord:
    value = _as_object(value, "element")
    params = _json_object(_object(value, "params", default={}), "element.params")
    raw_bindings = _object(value, "parameter_bindings", default={})
    bindings = {
        name: ParameterBinding(_string(_as_object(item, "parameter binding"), "external_parameter_id"))
        for name, item in raw_bindings.items()
    }
    return ElementRecord(
        _string(value, "id"),
        _string(value, "kind"),
        _string(value, "name"),
        params,
        bindings,
        _json_object(_object(value, "metadata", default={}), "element.metadata"),
    )


def _endpoint_from_dict(value: Any) -> Endpoint:
    value = _as_object(value, "edge endpoint")
    return Endpoint(_string(value, "element_id"), _string(value, "port"))


def _edge_from_dict(value: Any) -> EdgeRecord:
    value = _as_object(value, "edge")
    return EdgeRecord(
        _string(value, "id"),
        _endpoint_from_dict(value.get("source")),
        _endpoint_from_dict(value.get("target")),
    )


def _tensor_from_dict(value: Any) -> TensorContract:
    value = _as_object(value, "tensor contract")
    shape = value.get("shape")
    if not isinstance(shape, list):
        raise TypeError("tensor.shape must be an array")
    semantic = value.get("semantic")
    if semantic is not None and not isinstance(semantic, str):
        raise TypeError("tensor.semantic must be text or null")
    return TensorContract(
        tuple(_json_value(item, "tensor.shape") for item in shape),
        _string(value, "dtype"),
        semantic,
    )


def _group_port_from_dict(value: Any) -> GroupPort:
    value = _as_object(value, "group port")
    tensor = value.get("tensor")
    return GroupPort(_string(value, "id"), _string(value, "name"), None if tensor is None else _tensor_from_dict(tensor))


def _group_instance_from_dict(value: Any) -> GroupInstance:
    value = _as_object(value, "group instance")
    return GroupInstance(
        _string(value, "id"),
        _string(value, "group_id"),
        _string(value, "name"),
        _integer(value, "repeat"),
        _json_object(_object(value, "params", default={}), "group instance.params"),
    )


def _group_from_dict(value: Any) -> GroupDefinition:
    value = _as_object(value, "group definition")
    return GroupDefinition(
        _string(value, "id"),
        _string(value, "name"),
        tuple(_group_port_from_dict(item) for item in _list(value, "inputs")),
        tuple(_group_port_from_dict(item) for item in _list(value, "outputs")),
        tuple(_element_from_dict(item) for item in _list(value, "elements")),
        tuple(_edge_from_dict(item) for item in _list(value, "edges")),
        tuple(_group_instance_from_dict(item) for item in _list(value, "group_instances")),
    )


def _method_from_dict(value: Any) -> MethodGraph:
    value = _as_object(value, "method graph")
    inputs = []
    for item in _list(value, "inputs"):
        port = _as_object(item, "method input")
        inputs.append(MethodInput(_string(port, "id"), _string(port, "name"), _tensor_from_dict(port.get("tensor"))))
    outputs = []
    for item in _list(value, "outputs"):
        port = _as_object(item, "method output")
        outputs.append(MethodOutput(_string(port, "id"), _string(port, "name")))
    return MethodGraph(
        _string(value, "name"),
        tuple(inputs),
        tuple(outputs),
        tuple(_element_from_dict(item) for item in _list(value, "elements")),
        tuple(_edge_from_dict(item) for item in _list(value, "edges")),
        tuple(_group_instance_from_dict(item) for item in _list(value, "group_instances")),
    )


def _as_object(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{description} must be an object")
    return value


def _object(parent: dict[str, Any], key: str, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    value = parent.get(key, default)
    if not isinstance(value, dict):
        raise TypeError(f"{key} must be an object")
    return value


def _list(parent: dict[str, Any], key: str) -> list[Any]:
    value = parent.get(key)
    if not isinstance(value, list):
        raise TypeError(f"{key} must be an array")
    return value


def _string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{key} must be non-empty text")
    return value


def _bool(parent: dict[str, Any], key: str) -> bool:
    value = parent.get(key)
    if type(value) is not bool:
        raise TypeError(f"{key} must be bool")
    return value


def _integer(parent: dict[str, Any], key: str) -> int:
    value = parent.get(key)
    if type(value) is not int:
        raise TypeError(f"{key} must be int")
    return value


def _json_object(value: dict[str, Any], description: str) -> dict[str, JsonValue]:
    return {key: _json_value(item, f"{description}.{key}") for key, item in value.items()}


def _json_value(value: Any, description: str) -> JsonValue:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json_value(item, description) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError(f"{description} must use string keys")
        return {key: _json_value(item, f"{description}.{key}") for key, item in value.items()}
    raise TypeError(f"{description} contains a non-JSON value")
