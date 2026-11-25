"""Compiled semantic PyTorch model definition, separate from Core runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

from .bindings import RuntimeBindingMap
from .runtime import RuntimeModelInfo, new_runtime_info, runtime_registry
from .schema import JsonScalar


@dataclass(frozen=True, slots=True)
class PyTorchModelDefinition:
    metablock_id: str
    graph_revision: str
    materialization_key: str
    model_name: str
    generated_source: str
    external_parameters: Mapping[str, JsonScalar]
    bindings: RuntimeBindingMap
    _factory: Callable[[], object] = field(repr=False, compare=False)

    def instantiate(self, *, device: str | object | None = None, dtype: object | None = None) -> object:
        model = self._factory()
        if device is not None or dtype is not None:
            options = {}
            if device is not None:
                options["device"] = device
            if dtype is not None:
                options["dtype"] = dtype
            model.to(**options)
        info: RuntimeModelInfo = new_runtime_info(
            self.metablock_id, self.graph_revision, self.materialization_key, self.bindings
        )
        setattr(model, "__graphimo_pytorch_runtime__", info)
        runtime_registry.register(model, info)
        return model

    def __call__(self, *, device: str | object | None = None, dtype: object | None = None) -> object:
        return self.instantiate(device=device, dtype=dtype)

