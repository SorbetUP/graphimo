"""Process-local PyTorch runtime registry and bounded live telemetry.

The addon owns runtime objects and hooks. Graphimo Core sees only the JSON-safe
event envelope returned by the MetaBlock event action.
"""

from __future__ import annotations

import math
import copy
import threading
import time
import uuid
import weakref
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .model_definition import GroupRuntimeIndex

EVENT_SCHEMA_VERSION = 1
MAX_EVENTS = 10_000
MAX_TREE_DEPTH = 5
MAX_TREE_LEAVES = 64
MAX_HISTOGRAM_BINS = 128
MAX_SAMPLE_VALUES = 4096


@dataclass(frozen=True)
class PyTorchEvent:
    schema_version: int
    event_id: str
    kind: str
    timestamp: float
    metablock_id: str
    graph_revision: str
    runtime_instance_id: str
    logical_element_id: str | None
    module_path: str | None
    group_instance_path: tuple[GroupRuntimeIndex, ...]
    step: int | None
    payload: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "metablock_id": self.metablock_id,
            "graph_revision": self.graph_revision,
            "runtime_instance_id": self.runtime_instance_id,
            "logical_element_id": self.logical_element_id,
            "module_path": self.module_path,
            "group_instance_path": _group_path_json(self.group_instance_path),
            "step": self.step,
            "payload": self.payload,
        }


def _runtime_info(model: Any) -> Any:
    return getattr(model, "__graphimo_pytorch_runtime__", None)


def _group_path_json(path: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [
        {
            "group_instance_id": (
                item.group_instance_id if hasattr(item, "group_instance_id") else item[0]
            ),
            "repetition_index": (
                item.repetition_index if hasattr(item, "repetition_index") else item[1]
            ),
        }
        for item in path
    ]


class PyTorchEventBus:
    """Bounded process-local event history with per-MetaBlock cursors."""

    def __init__(self, max_events: int = MAX_EVENTS):
        if type(max_events) is not int or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        self._events: deque[tuple[int, dict[str, Any]]] = deque()
        self._next_by_metablock: dict[str, int] = {}
        self._dropped_by_metablock: dict[str, int] = {}
        self._max_events = max_events
        self._lock = threading.RLock()

    def emit(
        self,
        *,
        kind: str,
        info: Any,
        logical_element_id: str | None = None,
        module_path: str | None = None,
        group_instance_path: tuple[Any, ...] = (),
        step: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(kind, str) or not kind:
            raise ValueError("event kind must be non-empty text")
        clean_payload = _json_safe(payload or {})
        if not isinstance(clean_payload, dict):
            raise TypeError("event payload must be an object")
        with self._lock:
            metablock_id = info.metablock_id
            sequence = self._next_by_metablock.get(metablock_id, 0) + 1
            self._next_by_metablock[metablock_id] = sequence
            event = PyTorchEvent(
                schema_version=EVENT_SCHEMA_VERSION,
                event_id=uuid.uuid4().hex,
                kind=kind,
                timestamp=time.time(),
                metablock_id=metablock_id,
                graph_revision=info.graph_revision,
                runtime_instance_id=info.runtime_instance_id,
                logical_element_id=logical_element_id,
                module_path=module_path,
                group_instance_path=tuple(
                    GroupRuntimeIndex(
                        item.group_instance_id if hasattr(item, "group_instance_id") else item[0],
                        item.repetition_index if hasattr(item, "repetition_index") else item[1],
                    )
                    for item in group_instance_path
                ),
                step=step,
                payload=clean_payload,
            ).to_json()
            self._events.append((sequence, event))
            while len(self._events) > self._max_events:
                _old_sequence, old = self._events.popleft()
                identity = old["metablock_id"]
                self._dropped_by_metablock[identity] = (
                    self._dropped_by_metablock.get(identity, 0) + 1
                )
            return event

    def drain(
        self, metablock_id: str, cursor: str | None = None, limit: int = 128
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError("event limit must be between 1 and 512")
        try:
            position = int(cursor or "0")
        except (TypeError, ValueError) as exc:
            raise ValueError("event cursor must be a non-negative integer") from exc
        if position < 0:
            raise ValueError("event cursor must be a non-negative integer")
        with self._lock:
            matching = [
                (sequence, event)
                for sequence, event in self._events
                if event["metablock_id"] == metablock_id
            ]
            dropped = self._dropped_by_metablock.get(metablock_id, 0)
            oldest = (
                matching[0][0] if matching else self._next_by_metablock.get(metablock_id, 0) + 1
            )
            missed = max(0, min(dropped, oldest - 1 - position))
            selected = [(seq, event) for seq, event in matching if seq > position][:limit]
            next_cursor = str(selected[-1][0]) if selected else str(position)
            return {
                "cursor": next_cursor,
                "events": [copy.deepcopy(event) for _seq, event in selected],
                "dropped": missed,
            }

    def metablock_ids(self) -> list[str]:
        """Return identities with event streams, for an attached async publisher."""
        with self._lock:
            return sorted(self._next_by_metablock)


@dataclass(frozen=True)
class SubscriptionSpec:
    subscription_id: str
    runtime_instance_id: str
    logical_element_id: str
    repetition_index: int | None = None
    signal: str = "activation"
    rate_hz: float = 1.0
    detail: str = "summary"


@dataclass
class _Subscription:
    spec: SubscriptionSpec
    site: Any
    last_emission: float = 0.0
    warned_gradient_unavailable: bool = False


@dataclass
class _Hook:
    handle: Any
    subscriptions: set[str] = field(default_factory=set)


class RuntimeRegistry:
    """Weak registry: model metadata remains available without owning models."""

    def __init__(self, event_bus: PyTorchEventBus | None = None):
        self.event_bus = event_bus or PyTorchEventBus()
        self._models: dict[str, weakref.ReferenceType[Any]] = {}
        self._infos: dict[str, Any] = {}
        self._subscriptions: dict[str, dict[str, _Subscription]] = {}
        self._hooks: dict[tuple[str, str], _Hook] = {}
        self._last_aux_emission: dict[tuple[str, str], float] = {}
        self._lock = threading.RLock()

    def register(self, model: Any, info: Any) -> None:
        runtime_id = info.runtime_instance_id
        previous = _runtime_info(model)
        if previous is not None and previous.runtime_instance_id != runtime_id:
            raise ValueError("model already carries different Graphimo runtime metadata")
        model.__graphimo_pytorch_runtime__ = info
        with self._lock:
            if runtime_id in self._models and self._models[runtime_id]() is not None:
                raise ValueError(f"runtime instance {runtime_id!r} is already registered")
            registry_ref = weakref.ref(self)

            def collected(_reference: weakref.ReferenceType[Any]) -> None:
                owner = registry_ref()
                if owner is not None:
                    owner._collected(runtime_id)

            self._models[runtime_id] = weakref.ref(model, collected)
            self._infos[runtime_id] = info
        self.event_bus.emit(
            kind="runtime.created", info=info, payload={"device": _model_device(model)}
        )

    def resolve(self, runtime_instance_id: str) -> Any | None:
        with self._lock:
            reference = self._models.get(runtime_instance_id)
            return reference() if reference is not None else None

    def info(self, runtime_instance_id: str) -> Any | None:
        with self._lock:
            return self._infos.get(runtime_instance_id)

    def list_for_metablock(self, metablock_id: str) -> list[Any]:
        with self._lock:
            return [
                info
                for runtime_id, info in self._infos.items()
                if info.metablock_id == metablock_id
                and (reference := self._models.get(runtime_id)) is not None
                and reference() is not None
            ]

    def subscribe(self, spec: SubscriptionSpec) -> dict[str, Any]:
        _validate_subscription(spec)
        model = self.resolve(spec.runtime_instance_id)
        info = self.info(spec.runtime_instance_id)
        if model is None or info is None:
            raise ValueError("runtime model is no longer available")
        sites = [
            site
            for site in info.bindings.sites
            if site.logical_element_id == spec.logical_element_id and site.inspectable
        ]
        if spec.repetition_index is not None:
            sites = [
                site
                for site in sites
                if site.group_instance_path
                and (
                    site.group_instance_path[-1].repetition_index
                    if hasattr(site.group_instance_path[-1], "repetition_index")
                    else site.group_instance_path[-1][1]
                )
                == spec.repetition_index
            ]
        elif len(sites) > 1:
            raise ValueError("a repeated or ambiguous runtime site requires repetition_index")
        if not sites:
            raise ValueError("no inspectable runtime site matches the logical element")
        if len(sites) != 1:
            raise ValueError("subscription must resolve to exactly one runtime site")
        site = sites[0]
        try:
            module = model.get_submodule(site.module_path)
        except (AttributeError, KeyError) as exc:
            raise ValueError(f"runtime module path {site.module_path!r} is unavailable") from exc
        key = (spec.runtime_instance_id, site.module_path)
        with self._lock:
            subscriptions = self._subscriptions.setdefault(spec.runtime_instance_id, {})
            if spec.subscription_id in subscriptions:
                raise ValueError("subscription_id is already active for this runtime")
            hook = self._hooks.get(key)
            if hook is None:
                runtime_id = spec.runtime_instance_id
                module_path = site.module_path
                weak_model = weakref.ref(model)

                def observe(_module: Any, _inputs: Any, output: Any) -> Any:
                    self._observe_forward(runtime_id, module_path, weak_model, output)
                    return None

                observe.__graphimo_live_telemetry__ = True

                hook = _Hook(module.register_forward_hook(observe))
                self._hooks[key] = hook
            hook.subscriptions.add(spec.subscription_id)
            subscriptions[spec.subscription_id] = _Subscription(spec, site)
        return {"subscription_id": spec.subscription_id, "module_path": site.module_path}

    def unsubscribe(self, runtime_instance_id: str, subscription_id: str) -> dict[str, Any]:
        with self._lock:
            subscriptions = self._subscriptions.get(runtime_instance_id, {})
            subscription = subscriptions.pop(subscription_id, None)
            if subscription is None:
                return {"removed": False, "subscription_id": subscription_id}
            key = (runtime_instance_id, subscription.site.module_path)
            hook = self._hooks.get(key)
            if hook is not None:
                hook.subscriptions.discard(subscription_id)
                if not hook.subscriptions:
                    hook.handle.remove()
                    del self._hooks[key]
            if not subscriptions:
                self._subscriptions.pop(runtime_instance_id, None)
            return {"removed": True, "subscription_id": subscription_id}

    def unsubscribe_all_for_metablock(self, metablock_id: str) -> dict[str, Any]:
        with self._lock:
            pending = [
                (runtime_id, subscription_id)
                for runtime_id, subscriptions in self._subscriptions.items()
                for subscription_id, subscription in subscriptions.items()
                if (info := self._infos.get(runtime_id)) is not None
                and info.metablock_id == metablock_id
            ]
        removed = sum(
            bool(self.unsubscribe(runtime_id, subscription_id)["removed"])
            for runtime_id, subscription_id in pending
        )
        return {"removed": removed}

    def release(self, model: Any) -> bool:
        info = _runtime_info(model)
        if info is None:
            return False
        runtime_id = info.runtime_instance_id
        with self._lock:
            if runtime_id not in self._infos:
                return False
            self._remove_runtime(runtime_id)
            try:
                delattr(model, "__graphimo_pytorch_runtime__")
            except AttributeError:
                pass
        self.event_bus.emit(kind="runtime.released", info=info, payload={})
        return True

    def allow_aux_emission(self, runtime_id: str, key: str, rate_hz: float) -> bool:
        now = time.monotonic()
        with self._lock:
            identity = (runtime_id, key)
            if now - self._last_aux_emission.get(identity, 0.0) < 1.0 / rate_hz:
                return False
            self._last_aux_emission[identity] = now
            return True

    def _observe_forward(
        self, runtime_id: str, module_path: str, model_ref: weakref.ReferenceType[Any], output: Any
    ) -> None:
        if model_ref() is None:
            return
        with self._lock:
            active = [
                item
                for item in self._subscriptions.get(runtime_id, {}).values()
                if item.site.module_path == module_path
            ]
        for item in active:
            if not self._is_active(item):
                continue
            if item.spec.signal == "activation" and self._allow(item):
                self._emit_value("activation.summary", item, output)
            elif item.spec.signal == "gradient":
                tensors = list(_tensor_leaves(output))
                available = False
                for tensor in tensors:
                    if tensor.requires_grad:
                        available = True
                        try:
                            tensor.register_hook(
                                lambda gradient, sub=item: self._gradient_hook(sub, gradient)
                            )
                        except Exception as exc:  # noqa: BLE001 - inspection must not fail training
                            self._warning(item, "gradient_hook_failed", exc)
                if not available and not item.warned_gradient_unavailable:
                    item.warned_gradient_unavailable = True
                    self._warning(
                        item, "gradient_unavailable", "module output does not require grad"
                    )

    def _gradient_hook(self, subscription: _Subscription, gradient: Any) -> Any:
        if self._is_active(subscription) and self._allow(subscription):
            self._emit_value("gradient.summary", subscription, gradient)
        return gradient

    def _is_active(self, subscription: _Subscription) -> bool:
        runtime_id = subscription.spec.runtime_instance_id
        with self._lock:
            return (
                self._subscriptions.get(runtime_id, {}).get(subscription.spec.subscription_id)
                is subscription
            )

    def _allow(self, subscription: _Subscription) -> bool:
        now = time.monotonic()
        interval = 1.0 / subscription.spec.rate_hz
        with self._lock:
            if now - subscription.last_emission < interval:
                return False
            subscription.last_emission = now
        return True

    def _emit_value(self, kind: str, subscription: _Subscription, value: Any) -> None:
        info = self.info(subscription.spec.runtime_instance_id)
        if info is None:
            return
        try:
            payload = {
                "signal": subscription.spec.signal,
                "detail": subscription.spec.detail,
                "value": summarize_tree(value, detail=subscription.spec.detail),
                "subscription_id": subscription.spec.subscription_id,
            }
            self.event_bus.emit(
                kind=kind,
                info=info,
                logical_element_id=subscription.spec.logical_element_id,
                module_path=subscription.site.module_path,
                group_instance_path=subscription.site.group_instance_path,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 - telemetry cannot fail model execution
            self._warning(subscription, "summary_failed", exc)

    def _warning(self, subscription: _Subscription, code: str, error: Any) -> None:
        info = self.info(subscription.spec.runtime_instance_id)
        if info is None:
            return
        self.event_bus.emit(
            kind="runtime.warning",
            info=info,
            logical_element_id=subscription.spec.logical_element_id,
            module_path=subscription.site.module_path,
            group_instance_path=subscription.site.group_instance_path,
            payload={"code": code, "message": str(error)[:512]},
        )

    def _collected(self, runtime_id: str) -> None:
        with self._lock:
            info = self._infos.get(runtime_id)
            self._remove_runtime(runtime_id)
        if info is not None:
            self.event_bus.emit(kind="runtime.released", info=info, payload={"reason": "collected"})

    def _remove_runtime(self, runtime_id: str) -> None:
        self._models.pop(runtime_id, None)
        self._infos.pop(runtime_id, None)
        subscriptions = self._subscriptions.pop(runtime_id, {})
        for subscription in subscriptions.values():
            key = (runtime_id, subscription.site.module_path)
            hook = self._hooks.get(key)
            if hook is not None:
                hook.subscriptions.discard(subscription.spec.subscription_id)
                if not hook.subscriptions:
                    hook.handle.remove()
                    self._hooks.pop(key, None)


def _validate_subscription(spec: SubscriptionSpec) -> None:
    if not isinstance(spec, SubscriptionSpec):
        raise TypeError("spec must be a SubscriptionSpec")
    for name in ("subscription_id", "runtime_instance_id", "logical_element_id"):
        value = getattr(spec, name)
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"{name} must contain 1-128 characters")
    if spec.signal not in {"activation", "gradient"}:
        raise ValueError("signal must be activation or gradient")
    if spec.detail not in {"summary", "histogram", "sample"}:
        raise ValueError("detail must be summary, histogram or sample")
    if type(spec.rate_hz) not in {int, float} or not math.isfinite(spec.rate_hz):
        raise ValueError("rate_hz must be finite")
    if not 0.01 <= spec.rate_hz <= 1000:
        raise ValueError("rate_hz must be between 0.01 and 1000")
    if spec.repetition_index is not None and (
        type(spec.repetition_index) is not int or spec.repetition_index < 0
    ):
        raise ValueError("repetition_index must be a non-negative integer")


def summarize_tensor(tensor: Any, *, detail: str = "summary") -> dict[str, Any]:
    """Return bounded scalar metadata without retaining or copying the tensor."""
    import torch

    if not isinstance(tensor, torch.Tensor):
        raise TypeError("value is not a torch.Tensor")
    result: dict[str, Any] = {
        "shape": list(tensor.shape),
        "dtype": str(tensor.dtype),
        "device": str(tensor.device),
        "numel": tensor.numel(),
    }
    if tensor.numel() == 0:
        result["finite_fraction"] = None
        if detail == "histogram":
            result["histogram"] = {"edges": [], "counts": []}
        elif detail == "sample":
            result["sample"] = []
        return result
    with torch.no_grad():
        values = tensor.detach()
        if values.is_floating_point() or values.is_complex():
            finite = torch.isfinite(values)
            result["finite_fraction"] = _scalar(finite.to(torch.float32).mean())
            safe = values[finite]
        else:
            safe = values
        if safe.numel():
            numeric = safe.abs() if safe.is_complex() else safe
            if numeric.is_floating_point():
                result["mean"] = _scalar(numeric.mean())
                result["std"] = _scalar(numeric.std(unbiased=False))
            result["min"] = _scalar(numeric.min())
            result["max"] = _scalar(numeric.max())
        if detail == "histogram":
            result["histogram"] = _histogram(safe)
        elif detail == "sample":
            result["sample"] = _sample(safe)
    return result


def summarize_tree(value: Any, *, detail: str = "summary") -> Any:
    """Summarize bounded tensor leaves in a shallow output structure."""
    import torch

    count = [0]

    def visit(item: Any, depth: int) -> Any:
        if isinstance(item, torch.Tensor):
            count[0] += 1
            if count[0] > MAX_TREE_LEAVES:
                return {"truncated": "leaf_limit"}
            return summarize_tensor(item, detail=detail if depth == 0 else "summary")
        if depth >= MAX_TREE_DEPTH:
            return {"truncated": "depth_limit"}
        if isinstance(item, (tuple, list)):
            result = [visit(child, depth + 1) for child in item[:MAX_TREE_LEAVES]]
            if len(item) > MAX_TREE_LEAVES:
                result.append({"truncated": "item_limit"})
            return result
        if isinstance(item, dict):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= MAX_TREE_LEAVES:
                    result["__truncated__"] = "item_limit"
                    break
                result[str(key)[:128]] = visit(child, depth + 1)
            return result
        return {"type": type(item).__name__}

    return visit(value, 0)


def _tensor_leaves(value: Any):
    import torch

    stack = [(value, 0)]
    visited = 0
    while stack and visited < MAX_TREE_LEAVES:
        item, depth = stack.pop()
        if isinstance(item, torch.Tensor):
            visited += 1
            yield item
        elif depth < MAX_TREE_DEPTH:
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for child in list(item.values())[:MAX_TREE_LEAVES])
            elif isinstance(item, (tuple, list)):
                stack.extend((child, depth + 1) for child in item[:MAX_TREE_LEAVES])


def _scalar(value: Any) -> float | int | None:
    item = value.detach().item()
    if isinstance(item, bool):
        return int(item)
    if isinstance(item, int):
        return item
    number = float(item)
    return number if math.isfinite(number) else None


def _histogram(tensor: Any, bins: int = 64) -> dict[str, list[float | int]]:
    import torch

    if tensor.numel() == 0:
        return {"edges": [], "counts": []}
    values = tensor.detach().reshape(-1)
    if values.is_complex():
        values = values.abs()
    if not values.is_floating_point():
        values = values.float()
    values = values[torch.isfinite(values)]
    if values.numel() == 0:
        return {"edges": [], "counts": []}
    actual_bins = max(1, min(MAX_HISTOGRAM_BINS, bins))
    counts, edges = torch.histogram(values, bins=actual_bins)
    return {
        "edges": [_scalar(item) for item in edges],
        "counts": [_scalar(item) for item in counts],
    }


def _sample(tensor: Any, maximum: int = MAX_SAMPLE_VALUES) -> list[float | int]:
    if tensor.numel() == 0:
        return []
    values = tensor.detach().reshape(-1)
    if values.is_complex():
        values = values.abs()
    if values.numel() > maximum:
        indices = (
            __import__("torch")
            .linspace(0, values.numel() - 1, steps=maximum, device=values.device)
            .long()
        )
        values = values.index_select(0, indices)
    result = values.cpu().tolist()
    if not isinstance(result, list):
        result = [result]
    return [
        int(item) if isinstance(item, bool | int) else float(item)
        for item in result
        if isinstance(item, bool | int | float) and math.isfinite(float(item))
    ]


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > MAX_TREE_DEPTH + 2:
        return "<truncated>"
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {
            str(key)[:128]: _json_safe(item, depth + 1) for key, item in list(value.items())[:128]
        }
    if isinstance(value, list | tuple):
        return [_json_safe(item, depth + 1) for item in value[:4096]]
    raise TypeError(f"event payload contains unsupported value {type(value).__name__}")


def _model_device(model: Any) -> str:
    try:
        return str(next(model.parameters()).device)
    except (AttributeError, StopIteration):
        try:
            return str(next(model.buffers()).device)
        except (AttributeError, StopIteration):
            return "unknown"


RUNTIME_REGISTRY = RuntimeRegistry()


def release_runtime_model(model: Any) -> bool:
    """Remove addon hooks and metadata for an explicitly released model."""
    return RUNTIME_REGISTRY.release(model)


def emit_metric(model: Any, name: str, value: Any, *, step: int | None = None) -> None:
    """Emit one bounded scalar metric tied to the model's runtime provenance."""
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise ValueError("metric name must contain 1-128 characters")
    if step is not None and (type(step) is not int or step < 0):
        raise ValueError("step must be a non-negative integer")
    import torch

    if isinstance(value, torch.Tensor):
        if value.numel() != 1:
            raise ValueError("metric tensor value must contain exactly one scalar")
        scalar = _scalar(value.detach())
    elif type(value) in {int, float}:
        scalar = _json_safe(value)
    else:
        raise TypeError("metric value must be a float, int or scalar tensor")
    if scalar is None:
        raise ValueError("metric value must be finite")
    info = _runtime_info(model)
    if info is None or RUNTIME_REGISTRY.info(info.runtime_instance_id) is None:
        raise ValueError("model is not registered as an active Graphimo runtime")
    RUNTIME_REGISTRY.event_bus.emit(
        kind="metric.scalar",
        info=info,
        step=step,
        payload={"name": name.strip(), "value": scalar},
    )


def emit_cuda_resources(model: Any, *, step: int | None = None) -> None:
    """Emit CUDA memory counters only for a CUDA model and an available CUDA runtime."""
    if step is not None and (type(step) is not int or step < 0):
        raise ValueError("step must be a non-negative integer")
    info = _runtime_info(model)
    if info is None or RUNTIME_REGISTRY.info(info.runtime_instance_id) is None:
        raise ValueError("model is not registered as an active Graphimo runtime")
    device = _model_device(model)
    if not device.startswith("cuda"):
        return
    import torch

    if not torch.cuda.is_available():
        return
    if not RUNTIME_REGISTRY.allow_aux_emission(info.runtime_instance_id, "resource.cuda", 1.0):
        return
    index = torch.device(device).index
    if index is None:
        index = torch.cuda.current_device()
    properties = torch.cuda.get_device_properties(index)
    RUNTIME_REGISTRY.event_bus.emit(
        kind="resource.cuda",
        info=info,
        step=step,
        payload={
            "memory_allocated": int(torch.cuda.memory_allocated(index)),
            "memory_reserved": int(torch.cuda.memory_reserved(index)),
            "max_memory_allocated": int(torch.cuda.max_memory_allocated(index)),
            "max_memory_reserved": int(torch.cuda.max_memory_reserved(index)),
            "device_index": index,
            "device_name": properties.name,
        },
    )
