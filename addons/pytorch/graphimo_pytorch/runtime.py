"""Process-local, weakly held metadata for live PyTorch instances."""

from __future__ import annotations

import uuid
import weakref
from dataclasses import dataclass

from .bindings import RuntimeBindingMap


@dataclass(frozen=True, slots=True)
class RuntimeModelInfo:
    runtime_instance_id: str
    metablock_id: str
    graph_revision: str
    materialization_key: str
    bindings: RuntimeBindingMap


class RuntimeRegistry:
    def __init__(self) -> None:
        self._models: dict[str, weakref.ReferenceType[object]] = {}
        self._infos: dict[str, RuntimeModelInfo] = {}

    def register(self, model: object, info: RuntimeModelInfo) -> None:
        runtime_id = info.runtime_instance_id

        def discard(_: weakref.ReferenceType[object]) -> None:
            self._models.pop(runtime_id, None)
            self._infos.pop(runtime_id, None)

        self._models[runtime_id] = weakref.ref(model, discard)
        self._infos[runtime_id] = info

    def resolve(self, runtime_instance_id: str) -> object | None:
        reference = self._models.get(runtime_instance_id)
        return reference() if reference is not None else None

    def info(self, runtime_instance_id: str) -> RuntimeModelInfo | None:
        if self.resolve(runtime_instance_id) is None:
            return None
        return self._infos.get(runtime_instance_id)

    def list_for_metablock(self, metablock_id: str) -> list[RuntimeModelInfo]:
        return [info for info in self._infos.values() if info.metablock_id == metablock_id and self.resolve(info.runtime_instance_id) is not None]

    def release(self, model: object) -> None:
        runtime = getattr(model, "__graphimo_pytorch_runtime__", None)
        runtime_id = getattr(runtime, "runtime_instance_id", None)
        if runtime_id:
            self._models.pop(runtime_id, None)
            self._infos.pop(runtime_id, None)
        try:
            delattr(model, "__graphimo_pytorch_runtime__")
        except AttributeError:
            pass


runtime_registry = RuntimeRegistry()


def new_runtime_info(metablock_id: str, graph_revision: str, materialization_key: str,
                     bindings: RuntimeBindingMap) -> RuntimeModelInfo:
    return RuntimeModelInfo(uuid.uuid4().hex, metablock_id, graph_revision, materialization_key, bindings)


def release_runtime_model(model: object) -> None:
    runtime_registry.release(model)

