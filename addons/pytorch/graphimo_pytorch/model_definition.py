"""Opaque external model-definition value exported by the PyTorch MetaBlock."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import PyTorchMetaBlockState, state_from_dict, graph_revision


@dataclass(frozen=True)
class GroupRuntimeIndex:
    group_instance_id: str
    repetition_index: int


@dataclass(frozen=True)
class RuntimeSite:
    logical_element_id: str
    module_path: str
    group_instance_path: tuple[GroupRuntimeIndex, ...] = ()
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
    _total_parameters: int = field(repr=False, compare=False)

    @property
    def total_parameters(self) -> int:
        return self._total_parameters

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
        from .runtime import RUNTIME_REGISTRY

        RUNTIME_REGISTRY.register(model, model.__graphimo_pytorch_runtime__)
        return model

    def __call__(self, *, device: Any = None, dtype: Any = None) -> Any:
        return self.instantiate(device=device, dtype=dtype)


def materialize_definition(
    *,
    state_path: str | Path,
    metablock_id: str,
    revision: str | None = None,
    external_values: dict[str, Any] | None = None,
) -> PyTorchModelDefinition:
    """Recreate a model definition inside the active marimo Python process."""
    path = Path(state_path)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if (
        envelope.get("metablock_id") != metablock_id
        or envelope.get("dialect_id") != "pytorch.model"
    ):
        raise ValueError("PyTorch MetaBlock state identity does not match the materialization cell")
    raw_state = envelope.get("state")
    state = state_from_dict(raw_state)
    actual_revision = graph_revision(state)
    if revision is not None and revision != actual_revision:
        raise ValueError("PyTorch MetaBlock revision does not match the materialization cell")
    from .compiler import compile_model

    compiled = compile_model(state, external_values)
    from .compiler import _resolved_values

    by_id, resolved_by_name = _resolved_values(state, external_values)
    total_parameters = _count_parameters(state, by_id)
    return PyTorchModelDefinition(
        metablock_id=metablock_id,
        graph_revision=actual_revision,
        materialization_key=compiled.materialization_key,
        model_name=state.name,
        generated_source=compiled.generated_source,
        external_parameters=resolved_by_name,
        bindings=compiled.bindings,
        _factory=compiled.factory,
        _total_parameters=total_parameters,
    )


def _count_parameters(state: PyTorchMetaBlockState, external_values: Mapping[str, Any]) -> int:
    from .compiler import _resolved_params

    groups = {item.id: item for item in state.groups}

    def count_element(element) -> int:
        params = _resolved_params(element, external_values)
        if element.kind == "linear":
            return int(params["in_features"]) * int(params["out_features"]) + (
                int(params["out_features"]) if params.get("bias", True) else 0
            )
        if element.kind == "embedding":
            return int(params["num_embeddings"]) * int(params["embedding_dim"])
        if element.kind == "rms_norm":
            normalized = params["normalized_shape"]
            if isinstance(normalized, (tuple, list)):
                import math

                return math.prod(normalized)
            return int(normalized)
        return 0

    def count_group(group_id: str, stack: tuple[str, ...] = ()) -> int:
        if group_id in stack:
            raise ValueError("recursive Group definitions cannot be materialized")
        group = groups[group_id]
        total = sum(count_element(element) for element in group.elements)
        total += sum(
            count_group(instance.group_id, stack + (group_id,)) * instance.repeat
            for instance in group.group_instances
        )
        return total

    total = 0
    for method in state.entrypoints:
        total += sum(count_element(element) for element in method.elements)
        total += sum(
            count_group(instance.group_id) * instance.repeat for instance in method.group_instances
        )
    return total
