"""Versioned, JSON-backed semantic schema for the PyTorch addon."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
SCHEMA_VERSION = 1
_RENDERER_METADATA_KEYS = frozenset(
    {
        "layout",
        "renderer",
        "position",
        "x",
        "y",
        "selected",
        "selection",
        "zoom",
        "pan",
        "inspector",
    }
)


class PyTorchSchemaError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PyTorchSchemaError("pytorch_state_invalid", f"{label} must be non-empty text")
    return value


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PyTorchSchemaError(
            "pytorch_state_invalid", f"{label} must be an object with text keys"
        )
    _assert_json(value)
    return value


def _check_unique(values: tuple[Any, ...], label: str) -> None:
    identities = [item.id if hasattr(item, "id") else item.name for item in values]
    if len(identities) != len(set(identities)):
        raise PyTorchSchemaError("pytorch_state_invalid", f"duplicate {label} identity")


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: str = "error"
    metablock_id: str | None = None
    method_name: str | None = None
    element_id: str | None = None
    group_id: str | None = None
    edge_id: str | None = None


@dataclass(frozen=True)
class Endpoint:
    element_id: str
    port: str

    def __post_init__(self) -> None:
        _require_text(self.element_id, "endpoint element_id")
        _require_text(self.port, "endpoint port")


@dataclass(frozen=True)
class EdgeRecord:
    id: str
    source: Endpoint
    target: Endpoint

    def __post_init__(self) -> None:
        _require_text(self.id, "edge id")
        if not isinstance(self.source, Endpoint) or not isinstance(self.target, Endpoint):
            raise PyTorchSchemaError(
                "pytorch_state_invalid", "edge endpoints must be Endpoint values"
            )


@dataclass(frozen=True)
class ParameterBinding:
    external_parameter_id: str

    def __post_init__(self) -> None:
        _require_text(self.external_parameter_id, "external_parameter_id")


@dataclass(frozen=True)
class ElementRecord:
    id: str
    kind: str
    name: str
    params: dict[str, JsonValue]
    parameter_bindings: dict[str, ParameterBinding]
    metadata: dict[str, JsonValue]

    def __post_init__(self) -> None:
        _require_text(self.id, "element id")
        _require_text(self.kind, "element kind")
        _require_text(self.name, "element name")
        _require_object(self.params, "element params")
        _require_object(self.metadata, "element metadata")
        if not isinstance(self.parameter_bindings, dict) or any(
            not isinstance(key, str) or not isinstance(binding, ParameterBinding)
            for key, binding in self.parameter_bindings.items()
        ):
            raise PyTorchSchemaError(
                "pytorch_state_invalid",
                "element parameter_bindings must map names to ParameterBinding values",
            )


@dataclass(frozen=True)
class MethodInput:
    name: str
    shape: tuple[str | int, ...]
    dtype: str
    semantic: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.name, "method input name")
        _require_text(self.dtype, "method input dtype")
        if not isinstance(self.shape, tuple) or any(
            (type(dimension) is not int or dimension <= 0)
            and (not isinstance(dimension, str) or not dimension.strip())
            for dimension in self.shape
        ):
            raise PyTorchSchemaError(
                "pytorch_state_invalid",
                "method input shape must contain positive integers or symbolic dimensions",
            )
        if self.semantic is not None and not isinstance(self.semantic, str):
            raise PyTorchSchemaError(
                "pytorch_state_invalid", "method input semantic must be text or null"
            )


@dataclass(frozen=True)
class MethodOutput:
    name: str
    endpoint: Endpoint

    def __post_init__(self) -> None:
        _require_text(self.name, "method output name")
        if not isinstance(self.endpoint, Endpoint):
            raise PyTorchSchemaError(
                "pytorch_state_invalid", "method output endpoint must be an Endpoint"
            )


@dataclass(frozen=True)
class GroupPort:
    name: str
    endpoint: Endpoint

    def __post_init__(self) -> None:
        _require_text(self.name, "group port name")
        if not isinstance(self.endpoint, Endpoint):
            raise PyTorchSchemaError(
                "pytorch_state_invalid", "group port endpoint must be an Endpoint"
            )


@dataclass(frozen=True)
class GroupInstance:
    id: str
    group_id: str
    name: str
    repeat: int
    params: dict[str, JsonValue]

    def __post_init__(self) -> None:
        _require_text(self.id, "group instance id")
        _require_text(self.group_id, "group instance group_id")
        _require_text(self.name, "group instance name")
        if type(self.repeat) is not int or self.repeat < 1:
            raise PyTorchSchemaError("pytorch_state_invalid", "GroupInstance repeat must be >= 1")
        _require_object(self.params, "group instance params")


@dataclass(frozen=True)
class MethodGraph:
    name: str
    inputs: tuple[MethodInput, ...]
    outputs: tuple[MethodOutput, ...]
    elements: tuple[ElementRecord, ...]
    edges: tuple[EdgeRecord, ...]
    group_instances: tuple[GroupInstance, ...]

    def __post_init__(self) -> None:
        _require_text(self.name, "method graph name")
        for label, values, expected in (
            ("inputs", self.inputs, MethodInput),
            ("outputs", self.outputs, MethodOutput),
            ("elements", self.elements, ElementRecord),
            ("edges", self.edges, EdgeRecord),
            ("group_instances", self.group_instances, GroupInstance),
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(item, expected) for item in values
            ):
                raise PyTorchSchemaError(
                    "pytorch_state_invalid",
                    f"method {label} must be a tuple of {expected.__name__} values",
                )


@dataclass(frozen=True)
class GroupDefinition:
    id: str
    name: str
    inputs: tuple[GroupPort, ...]
    outputs: tuple[GroupPort, ...]
    elements: tuple[ElementRecord, ...]
    edges: tuple[EdgeRecord, ...]
    group_instances: tuple[GroupInstance, ...]

    def __post_init__(self) -> None:
        _require_text(self.id, "group id")
        _require_text(self.name, "group name")
        for label, values, expected in (
            ("inputs", self.inputs, GroupPort),
            ("outputs", self.outputs, GroupPort),
            ("elements", self.elements, ElementRecord),
            ("edges", self.edges, EdgeRecord),
            ("group_instances", self.group_instances, GroupInstance),
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(item, expected) for item in values
            ):
                raise PyTorchSchemaError(
                    "pytorch_state_invalid",
                    f"group {label} must be a tuple of {expected.__name__} values",
                )


@dataclass(frozen=True)
class ExternalParameter:
    id: str
    name: str
    type: str
    default: JsonValue
    structural: bool

    def __post_init__(self) -> None:
        _require_text(self.id, "external parameter id")
        _require_text(self.name, "external parameter name")
        if self.type not in {"int", "float", "bool", "str"}:
            raise PyTorchSchemaError(
                "pytorch_state_invalid", f"unsupported external parameter type: {self.type}"
            )
        if type(self.structural) is not bool:
            raise PyTorchSchemaError(
                "pytorch_state_invalid", "ExternalParameter structural must be boolean"
            )
        expected = {"int": int, "float": (int, float), "bool": bool, "str": str}[self.type]
        if type(self.default) not in (expected if isinstance(expected, tuple) else (expected,)):
            raise PyTorchSchemaError(
                "pytorch_state_invalid",
                f"default does not match external parameter type {self.type}",
            )
        _assert_json(self.default)


@dataclass(frozen=True)
class PyTorchMetaBlockState:
    schema_version: int
    metablock_id: str
    name: str
    external_parameters: tuple[ExternalParameter, ...]
    groups: tuple[GroupDefinition, ...]
    entrypoints: tuple[MethodGraph, ...]
    metadata: dict[str, JsonValue]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise PyTorchSchemaError(
                "pytorch_state_schema_unsupported",
                f"unsupported PyTorch MetaBlock schema version: {self.schema_version}",
            )
        _require_text(self.metablock_id, "metablock_id")
        _require_text(self.name, "MetaBlock name")
        for label, values, expected in (
            ("external_parameters", self.external_parameters, ExternalParameter),
            ("groups", self.groups, GroupDefinition),
            ("entrypoints", self.entrypoints, MethodGraph),
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(item, expected) for item in values
            ):
                raise PyTorchSchemaError(
                    "pytorch_state_invalid",
                    f"{label} must be a tuple of {expected.__name__} values",
                )
        _require_object(self.metadata, "MetaBlock metadata")
        _check_unique(self.external_parameters, "external parameter")
        _check_unique(self.groups, "group")
        _check_unique(self.entrypoints, "entrypoint")
        if not any(method.name == "forward" for method in self.entrypoints):
            raise PyTorchSchemaError(
                "pytorch_state_invalid", "PyTorch MetaBlock requires a forward entrypoint"
            )
        _assert_json(_state_to_dict(self))


def _assert_json(value: Any) -> None:
    try:
        _check_json_shape(value)
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise PyTorchSchemaError(
            "pytorch_state_invalid", f"PyTorch state must be JSON-compatible: {exc}"
        ) from exc


def _check_json_shape(value: Any) -> None:
    if value is None or type(value) in {bool, int, float, str}:
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _check_json_shape(item)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        for item in value.values():
            _check_json_shape(item)
        return
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def _state_to_dict(state: PyTorchMetaBlockState) -> dict[str, Any]:
    value = asdict(state)
    # Stable schema objects are emitted as lists while preserving scientific ordering.
    # Canvas/editor state belongs to Graphimo's separate presentation file.
    value["metadata"] = _semantic_metadata(value["metadata"])
    for method in value["entrypoints"]:
        for element in method["elements"]:
            element["metadata"] = _semantic_metadata(element["metadata"])
    for group in value["groups"]:
        for element in group["elements"]:
            element["metadata"] = _semantic_metadata(element["metadata"])
    return value


def _semantic_metadata(value: dict[str, Any]) -> dict[str, Any]:
    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: clean(nested)
                for key, nested in item.items()
                if key.lower() not in _RENDERER_METADATA_KEYS
            }
        if isinstance(item, list):
            return [clean(nested) for nested in item]
        return item

    return clean(value)


def _endpoint_from_dict(value: Any) -> Endpoint:
    if not isinstance(value, dict):
        raise PyTorchSchemaError("pytorch_state_invalid", "endpoint must be an object")
    return Endpoint(value["element_id"], value["port"])


def _edge_from_dict(value: Any) -> EdgeRecord:
    return EdgeRecord(
        value["id"], _endpoint_from_dict(value["source"]), _endpoint_from_dict(value["target"])
    )


def _element_from_dict(value: Any) -> ElementRecord:
    if not isinstance(value, dict):
        raise PyTorchSchemaError("pytorch_state_invalid", "element must be an object")
    return ElementRecord(
        value["id"],
        value["kind"],
        value["name"],
        value["params"],
        {
            key: ParameterBinding(binding["external_parameter_id"])
            for key, binding in value["parameter_bindings"].items()
        },
        value["metadata"],
    )


def _method_from_dict(value: Any) -> MethodGraph:
    return MethodGraph(
        name=value["name"],
        inputs=tuple(
            MethodInput(item["name"], tuple(item["shape"]), item["dtype"], item.get("semantic"))
            for item in value["inputs"]
        ),
        outputs=tuple(
            MethodOutput(item["name"], _endpoint_from_dict(item["endpoint"]))
            for item in value["outputs"]
        ),
        elements=tuple(_element_from_dict(item) for item in value["elements"]),
        edges=tuple(_edge_from_dict(item) for item in value["edges"]),
        group_instances=tuple(GroupInstance(**item) for item in value["group_instances"]),
    )


def _group_from_dict(value: Any) -> GroupDefinition:
    return GroupDefinition(
        id=value["id"],
        name=value["name"],
        inputs=tuple(
            GroupPort(item["name"], _endpoint_from_dict(item["endpoint"]))
            for item in value["inputs"]
        ),
        outputs=tuple(
            GroupPort(item["name"], _endpoint_from_dict(item["endpoint"]))
            for item in value["outputs"]
        ),
        elements=tuple(_element_from_dict(item) for item in value["elements"]),
        edges=tuple(_edge_from_dict(item) for item in value["edges"]),
        group_instances=tuple(GroupInstance(**item) for item in value["group_instances"]),
    )


def state_from_dict(value: Any) -> PyTorchMetaBlockState:
    if not isinstance(value, dict):
        raise PyTorchSchemaError(
            "pytorch_state_invalid", "PyTorch MetaBlock state must be an object"
        )
    version = value.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PyTorchSchemaError(
            "pytorch_state_schema_unsupported",
            f"unsupported PyTorch MetaBlock schema version: {version}",
        )
    try:
        state = PyTorchMetaBlockState(
            schema_version=version,
            metablock_id=value["metablock_id"],
            name=value["name"],
            external_parameters=tuple(
                ExternalParameter(**item) for item in value["external_parameters"]
            ),
            groups=tuple(_group_from_dict(item) for item in value["groups"]),
            entrypoints=tuple(_method_from_dict(item) for item in value["entrypoints"]),
            metadata=value["metadata"],
        )
    except PyTorchSchemaError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise PyTorchSchemaError(
            "pytorch_state_invalid", f"invalid PyTorch MetaBlock state: {exc}"
        ) from exc
    return state


def canonical_state_json(state: PyTorchMetaBlockState) -> str:
    _assert_json(_state_to_dict(state))
    return json.dumps(
        _state_to_dict(state),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def state_to_dict(state: PyTorchMetaBlockState) -> dict[str, Any]:
    """Return the canonical JSON-compatible state mapping."""
    _assert_json(_state_to_dict(state))
    return _state_to_dict(state)


def graph_revision(state: PyTorchMetaBlockState) -> str:
    return hashlib.sha256(canonical_state_json(state).encode("utf-8")).hexdigest()


def load_state(path: Path) -> PyTorchMetaBlockState:
    source = Path(path)
    try:
        raw = source.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PyTorchSchemaError(
            "pytorch_state_invalid", f"cannot load PyTorch MetaBlock state: {exc}"
        ) from exc
    return state_from_dict(value)


def save_state(path: Path, state: PyTorchMetaBlockState) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    state_value = _state_to_dict(state)
    _assert_json(state_value)
    encoded = (
        json.dumps(state_value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    )
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
