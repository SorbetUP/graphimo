"""Opaque external model-definition value exported by the PyTorch MetaBlock."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graphimo.metablocks import state_revision

from .prototype import ModelSpec, build_model


@dataclass(frozen=True)
class RuntimeSite:
    logical_element_id: str
    module_path: str
    group_instance_path: tuple[tuple[str, int], ...] = ()
    inspectable: bool = True


@dataclass(frozen=True)
class RuntimeBindingMap:
    sites: tuple[RuntimeSite, ...]


@dataclass(frozen=True)
class RuntimeModelInfo:
    runtime_instance_id: str
    metablock_id: str
    graph_revision: str
    materialization_key: str
    bindings: RuntimeBindingMap


@dataclass(frozen=True)
class PyTorchModelDefinition:
    metablock_id: str
    graph_revision: str
    materialization_key: str
    model_name: str
    generated_source: str
    external_parameters: Mapping[str, Any]
    bindings: RuntimeBindingMap
    _factory: Callable[[], Any]
    _resolved_parameters: Mapping[str, Any] = field(repr=False, compare=False)

    @property
    def total_parameters(self) -> int:
        return _parameter_count(self._resolved_parameters)

    @property
    def trainable_parameters(self) -> int:
        return self.total_parameters

    @property
    def parameter_bytes(self) -> int:
        return self.total_parameters * 4

    def instantiate(self, *, device: Any = None, dtype: Any = None) -> Any:
        model = self._factory()
        if device is not None or dtype is not None:
            model.to(device=device, dtype=dtype)
        model.__graphimo_pytorch_runtime__ = RuntimeModelInfo(
            runtime_instance_id=uuid.uuid4().hex,
            metablock_id=self.metablock_id,
            graph_revision=self.graph_revision,
            materialization_key=self.materialization_key,
            bindings=self.bindings,
        )
        return model

    def __call__(self, *, device: Any = None, dtype: Any = None) -> Any:
        return self.instantiate(device=device, dtype=dtype)


def materialize_definition(
    *, state_path: str | Path, metablock_id: str, external_values: dict[str, Any] | None = None
) -> PyTorchModelDefinition:
    """Recreate a model definition inside the active marimo Python process."""
    path = Path(state_path)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if (
        envelope.get("metablock_id") != metablock_id
        or envelope.get("dialect_id") != "pytorch.model"
    ):
        raise ValueError("PyTorch MetaBlock state identity does not match the materialization cell")
    state = envelope.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("model_spec"), dict):
        raise ValueError("PyTorch MetaBlock state has no model specification")  # noqa: TRY004
    values = {**state["model_spec"], **(external_values or {})}
    spec = ModelSpec(
        input_size=int(values["input_size"]),
        hidden_size=int(values["hidden_size"]),
        head_sizes={str(name): int(size) for name, size in values["head_sizes"].items()},
    )
    revision = state_revision(state)
    materialization_payload = {
        "revision": revision,
        "external_parameters": external_values or {},
        "addon_version": "0.1.0",
    }
    materialization_key = hashlib.sha256(
        json.dumps(materialization_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    bindings = RuntimeBindingMap(
        (
            RuntimeSite("backbone_linear", "backbone.0"),
            RuntimeSite("backbone_activation", "backbone.1"),
            *(RuntimeSite(f"head_{name}", f"heads.{name}") for name in sorted(spec.head_sizes)),
        )
    )
    generated_source = _generated_source(spec)
    return PyTorchModelDefinition(
        metablock_id=metablock_id,
        graph_revision=revision,
        materialization_key=materialization_key,
        model_name=state.get("name", "GraphimoModel"),
        generated_source=generated_source,
        external_parameters=external_values or {},
        bindings=bindings,
        _factory=lambda: build_model(spec),
        _resolved_parameters={
            "input_size": spec.input_size,
            "hidden_size": spec.hidden_size,
            "head_sizes": spec.head_sizes,
        },
    )


def _generated_source(spec: ModelSpec) -> str:
    heads = repr(dict(sorted(spec.head_sizes.items())))
    return (
        "from graphimo_pytorch.prototype import ModelSpec, build_model\n\n"
        f"model_spec = ModelSpec(input_size={spec.input_size}, hidden_size={spec.hidden_size}, "
        f"head_sizes={heads})\n"
        "model = build_model(model_spec)\n"
    )


def _parameter_count(values: Mapping[str, Any]) -> int:
    input_size = int(values["input_size"])
    hidden_size = int(values["hidden_size"])
    heads = values["head_sizes"]
    return (
        input_size * hidden_size
        + hidden_size
        + sum(hidden_size * int(size) + int(size) for size in heads.values())
    )
