"""Version-1 PyTorch MetaBlock semantic schema, independent of runtime torch."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TypeAlias

from .diagnostics import GraphimoPytorchError

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class ParameterBinding:
    external_parameter_id: str


@dataclass(frozen=True, slots=True)
class ElementRecord:
    id: str
    kind: str
    name: str
    params: dict[str, JsonValue] = field(default_factory=dict)
    parameter_bindings: dict[str, ParameterBinding] = field(default_factory=dict)
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Endpoint:
    element_id: str
    port: str


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    id: str
    source: Endpoint
    target: Endpoint


@dataclass(frozen=True, slots=True)
class TensorContract:
    shape: tuple[JsonValue, ...]
    dtype: str
    semantic: str | None = None


@dataclass(frozen=True, slots=True)
class MethodInput:
    id: str
    name: str
    tensor: TensorContract


@dataclass(frozen=True, slots=True)
class MethodOutput:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class GroupPort:
    id: str
    name: str
    tensor: TensorContract | None = None


@dataclass(frozen=True, slots=True)
class GroupInstance:
    id: str
    group_id: str
    name: str
    repeat: int
    params: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.repeat) is not int or self.repeat < 1:
            raise GraphimoPytorchError("pytorch_group_repeat_invalid", "GroupInstance.repeat must be at least 1")


@dataclass(frozen=True, slots=True)
class GroupDefinition:
    id: str
    name: str
    inputs: tuple[GroupPort, ...]
    outputs: tuple[GroupPort, ...]
    elements: tuple[ElementRecord, ...]
    edges: tuple[EdgeRecord, ...]
    group_instances: tuple[GroupInstance, ...]


@dataclass(frozen=True, slots=True)
class MethodGraph:
    name: str
    inputs: tuple[MethodInput, ...]
    outputs: tuple[MethodOutput, ...]
    elements: tuple[ElementRecord, ...]
    edges: tuple[EdgeRecord, ...]
    group_instances: tuple[GroupInstance, ...]


@dataclass(frozen=True, slots=True)
class ExternalParameter:
    id: str
    name: str
    type: str
    default: JsonValue
    structural: bool

    def __post_init__(self) -> None:
        if self.type not in {"int", "float", "bool", "str"}:
            raise GraphimoPytorchError("pytorch_parameter_type_invalid", f"Unsupported external parameter type: {self.type}")
        if type(self.structural) is not bool:
            raise GraphimoPytorchError("pytorch_parameter_invalid", "ExternalParameter.structural must be bool")
        if not _matches_declared_type(self.default, self.type):
            raise GraphimoPytorchError("pytorch_parameter_invalid", f"Default does not match external parameter type {self.type}")


@dataclass(frozen=True, slots=True)
class PyTorchMetaBlockState:
    schema_version: int
    metablock_id: str
    name: str
    external_parameters: tuple[ExternalParameter, ...]
    groups: tuple[GroupDefinition, ...]
    entrypoints: tuple[MethodGraph, ...]
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise GraphimoPytorchError(
                "pytorch_state_schema_unsupported",
                f"Unsupported PyTorch MetaBlock schema version: {self.schema_version}",
            )
        _validate_state_json(self)
        if not self.metablock_id or not self.name:
            raise GraphimoPytorchError("pytorch_state_invalid", "MetaBlock id and name must be non-empty")
        if not any(graph.name == "forward" for graph in self.entrypoints):
            raise GraphimoPytorchError("pytorch_forward_required", "A PyTorch MetaBlock must define a forward method graph")


def _matches_declared_type(value: JsonValue, declared: str) -> bool:
    if declared == "bool":
        return type(value) is bool
    if declared == "int":
        return type(value) is int
    if declared == "float":
        return type(value) is int or type(value) is float and math.isfinite(value)
    if declared == "str":
        return isinstance(value, str)
    return False


def _validate_state_json(state: PyTorchMetaBlockState) -> None:
    from dataclasses import asdict

    def visit(value: object, path: str) -> None:
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is float:
            if math.isfinite(value):
                return
            raise GraphimoPytorchError("pytorch_state_invalid", f"Non-finite JSON number at {path}")
        if isinstance(value, list) or isinstance(value, tuple):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise GraphimoPytorchError("pytorch_state_invalid", f"Non-string JSON key at {path}")
                visit(item, f"{path}.{key}")
            return
        if hasattr(value, "__dataclass_fields__"):
            visit(asdict(value), path)
            return
        raise GraphimoPytorchError("pytorch_state_invalid", f"Non-JSON value at {path}: {type(value).__name__}")

    visit(asdict(state), "state")
