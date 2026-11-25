"""Subscription-driven PyTorch activation/gradient telemetry and bounded metrics."""

from __future__ import annotations

import math
import queue
import time
import uuid
import weakref
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Mapping

from .bindings import GroupRuntimeIndex, RuntimeSite
from .diagnostics import GraphimoPytorchError
from .runtime import RuntimeModelInfo, runtime_registry
from .schema import JsonValue


@dataclass(frozen=True, slots=True)
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
    payload: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
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
            "group_instance_path": [
                {"group_instance_id": item.group_instance_id, "repetition_index": item.repetition_index}
                for item in self.group_instance_path
            ],
            "step": self.step,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class SubscriptionSpec:
    subscription_id: str
    runtime_instance_id: str
    logical_element_id: str
    repetition_index: int | None
    signal: str
    rate_hz: float
    detail: str = "summary"

    def __post_init__(self) -> None:
        if self.signal not in {"activation", "gradient"}:
            raise GraphimoPytorchError("pytorch_subscription_signal_invalid", f"Unsupported signal {self.signal!r}")
        if self.detail not in {"summary", "histogram", "sample"}:
            raise GraphimoPytorchError("pytorch_subscription_detail_invalid", f"Unsupported detail {self.detail!r}")
        if not math.isfinite(self.rate_hz) or self.rate_hz <= 0:
            raise GraphimoPytorchError("pytorch_subscription_rate_invalid", "rate_hz must be a finite positive number")
        if self.repetition_index is not None and (type(self.repetition_index) is not int or self.repetition_index < 0):
            raise GraphimoPytorchError("pytorch_subscription_repetition_invalid", "repetition_index must be a non-negative integer")


def default_subscription_rate_hz(detail: str = "summary") -> float:
    """Return the contract default event rate for a visible inspection mode."""
    if detail == "summary":
        return 10.0
    if detail in {"histogram", "sample"}:
        return 2.0
    raise GraphimoPytorchError("pytorch_subscription_detail_invalid", f"Unsupported detail {detail!r}")


class EventQueue:
    """Non-blocking bounded queue; producers never wait for the UI consumer."""

    def __init__(self, maxsize: int = 2048):
        if type(maxsize) is not int or maxsize < 1:
            raise ValueError("EventQueue maxsize must be a positive integer")
        self._queue: queue.Queue[PyTorchEvent] = queue.Queue(maxsize=maxsize)
        self._dropped = 0
        self._lock = Lock()

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped

    def put(self, event: PyTorchEvent) -> bool:
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False

    def get_nowait(self) -> PyTorchEvent | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def drain(self, limit: int = 256) -> tuple[PyTorchEvent, ...]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        items = []
        for _ in range(limit):
            event = self.get_nowait()
            if event is None:
                break
            items.append(event)
        return tuple(items)


class SubscriptionManager:
    def __init__(self, events: EventQueue):
        self.events = events
        self._handles: dict[str, tuple[SubscriptionSpec, tuple[object, ...], weakref.ReferenceType[object]]] = {}
        self._last_emit: dict[str, float] = {}
        self._gradient_warned: set[str] = set()
        self._lock = Lock()

    def subscribe(self, model: object, spec: SubscriptionSpec) -> tuple[RuntimeSite, ...]:
        runtime = getattr(model, "__graphimo_pytorch_runtime__", None)
        if not isinstance(runtime, RuntimeModelInfo) or runtime.runtime_instance_id != spec.runtime_instance_id:
            raise GraphimoPytorchError("pytorch_runtime_not_registered", "Subscription runtime does not match this model")
        if runtime_registry.resolve(spec.runtime_instance_id) is not model:
            raise GraphimoPytorchError("pytorch_runtime_not_registered", "Runtime model is not registered")
        sites = runtime.bindings.for_element(spec.logical_element_id)
        if spec.repetition_index is not None:
            sites = tuple(site for site in sites if _repetition_index(site) == spec.repetition_index)
        elif len(sites) > 1 and any(site.group_instance_path for site in sites):
            raise GraphimoPytorchError("pytorch_subscription_repetition_required", "Select one repeated Group index for V1 live inspection")
        if not sites:
            raise GraphimoPytorchError("pytorch_runtime_site_missing", f"No runtime site for element {spec.logical_element_id!r}")
        if not all(site.inspectable for site in sites):
            raise GraphimoPytorchError("pytorch_runtime_site_uninspectable", "The selected element has no inspectable runtime module")

        handles = []
        for site in sites:
            try:
                module = model.get_submodule(site.module_path)
            except Exception as error:
                raise GraphimoPytorchError("pytorch_runtime_site_missing", f"Cannot resolve module path {site.module_path!r}: {error}") from error
            handles.append(module.register_forward_hook(self._forward_hook(spec, runtime, site)))
        runtime_ref = weakref.ref(model, lambda _: self._forget_runtime(spec.runtime_instance_id))
        with self._lock:
            if spec.subscription_id in self._handles:
                for handle in handles:
                    handle.remove()
                raise GraphimoPytorchError("pytorch_subscription_duplicate", f"Subscription id {spec.subscription_id!r} is already active")
            self._handles[spec.subscription_id] = (spec, tuple(handles), runtime_ref)
        return tuple(sites)

    def unsubscribe(self, subscription_id: str) -> bool:
        with self._lock:
            record = self._handles.pop(subscription_id, None)
        if record is None:
            return False
        for handle in record[1]:
            handle.remove()
        self._last_emit.pop(subscription_id, None)
        self._gradient_warned.discard(subscription_id)
        return True

    def unsubscribe_runtime(self, runtime_instance_id: str) -> None:
        with self._lock:
            identifiers = [key for key, (spec, _, _) in self._handles.items() if spec.runtime_instance_id == runtime_instance_id]
        for identifier in identifiers:
            self.unsubscribe(identifier)

    def active(self) -> tuple[SubscriptionSpec, ...]:
        with self._lock:
            return tuple(record[0] for record in self._handles.values())

    def _forget_runtime(self, runtime_instance_id: str) -> None:
        self.unsubscribe_runtime(runtime_instance_id)

    def _forward_hook(self, spec: SubscriptionSpec, runtime: RuntimeModelInfo, site: RuntimeSite) -> Callable[..., None]:
        def hook(_module: object, _args: tuple[object, ...], output: object) -> None:
            if spec.signal == "activation":
                if not self._allow_emit(spec):
                    return
                try:
                    leaves = _tensor_leaves(output)
                    if spec.detail != "summary" and len(leaves) > 1:
                        payload = _summarize_tree(output, "summary")
                        self.events.put(_event(runtime, "runtime.warning", {
                            "code": "tensor_leaf_selection_required",
                            "message": "Select one tensor output leaf before requesting histogram or sample detail",
                            "summary": payload,
                        }, spec, site))
                        return
                    payload = _summarize_tree(output, spec.detail)
                    self.events.put(_event(runtime, "activation.summary", payload, spec, site))
                except Exception as error:
                    self.events.put(_event(runtime, "runtime.warning", {"code": "tensor_summary_failed", "message": _bounded_error(error)}, spec, site))
                return

            leaves = list(_tensor_leaves(output))
            if not leaves or not any(value.requires_grad for value in leaves):
                if spec.subscription_id not in self._gradient_warned:
                    self._gradient_warned.add(spec.subscription_id)
                    self.events.put(_event(runtime, "runtime.warning", {
                        "code": "gradient_unavailable", "message": "Selected output does not require gradients"
                    }, spec, site))
                return
            for leaf in leaves:
                if not leaf.requires_grad:
                    continue

                output_shape = tuple(int(value) for value in leaf.shape)

                def capture(gradient: object, expected_shape: tuple[int, ...] = output_shape) -> object:
                    if self._allow_emit(spec):
                        try:
                            payload = _summarize_tensor(gradient, spec.detail)
                            payload["output_shape"] = list(expected_shape)
                            self.events.put(_event(runtime, "gradient.summary", payload, spec, site))
                        except Exception as error:
                            self.events.put(_event(runtime, "runtime.warning", {
                                "code": "gradient_summary_failed", "message": _bounded_error(error)
                            }, spec, site))
                    return gradient

                try:
                    leaf.register_hook(capture)
                except Exception as error:
                    self.events.put(_event(runtime, "runtime.warning", {
                        "code": "gradient_hook_failed", "message": _bounded_error(error)
                    }, spec, site))
        return hook

    def _allow_emit(self, spec: SubscriptionSpec) -> bool:
        now = time.monotonic()
        minimum_interval = 1.0 / spec.rate_hz
        with self._lock:
            previous = self._last_emit.get(spec.subscription_id)
            if previous is not None and now - previous < minimum_interval:
                return False
            self._last_emit[spec.subscription_id] = now
            return True


event_queue = EventQueue()
subscription_manager = SubscriptionManager(event_queue)


def subscribe(model: object, spec: SubscriptionSpec) -> tuple[RuntimeSite, ...]:
    return subscription_manager.subscribe(model, spec)


def unsubscribe(subscription_id: str) -> bool:
    return subscription_manager.unsubscribe(subscription_id)


def emit_metric(model: object, name: str, value: float | int | object, *, step: int | None = None) -> PyTorchEvent:
    runtime = _runtime_info(model)
    if not name:
        raise GraphimoPytorchError("pytorch_metric_name_invalid", "Metric name must not be empty")
    numeric = _scalar_value(value)
    if not math.isfinite(numeric):
        raise GraphimoPytorchError("pytorch_metric_value_invalid", "Metric values must be finite")
    return _emit_runtime(runtime, "metric.scalar", {"name": name, "value": numeric}, step=step)


def emit_cuda_resources(model: object, *, step: int | None = None) -> PyTorchEvent | None:
    runtime = _runtime_info(model)
    import torch

    device = next((item.device for item in (*model.parameters(), *model.buffers())), torch.device("cpu"))
    if device.type != "cuda" or not torch.cuda.is_available():
        return None
    now = time.monotonic()
    key = f"resource:{runtime.runtime_instance_id}"
    previous = _RESOURCE_LAST.get(key)
    if previous is not None and now - previous < 1.0:
        return None
    _RESOURCE_LAST[key] = now
    index = device.index if device.index is not None else torch.cuda.current_device()
    payload: dict[str, JsonValue] = {
        "memory_allocated": int(torch.cuda.memory_allocated(device)),
        "memory_reserved": int(torch.cuda.memory_reserved(device)),
        "max_memory_allocated": int(torch.cuda.max_memory_allocated(device)),
        "max_memory_reserved": int(torch.cuda.max_memory_reserved(device)),
        "device_index": int(index),
    }
    try:
        payload["device_name"] = str(torch.cuda.get_device_name(index))
    except Exception:
        pass
    return _emit_runtime(runtime, "resource.cuda", payload, step=step)


def emit_runtime_created(runtime: RuntimeModelInfo) -> PyTorchEvent:
    return _emit_runtime(runtime, "runtime.created", {})


def emit_runtime_released(runtime: RuntimeModelInfo) -> PyTorchEvent:
    return _emit_runtime(runtime, "runtime.released", {})


def summarize_tensor(tensor: object, *, detail: str = "summary", bins: int = 64, max_values: int = 4096) -> dict[str, JsonValue]:
    """Return bounded JSON-safe stats without keeping or copying the full tensor."""
    return _summarize_tensor(tensor, detail, bins=bins, max_values=max_values)


_RESOURCE_LAST: dict[str, float] = {}


def _runtime_info(model: object) -> RuntimeModelInfo:
    runtime = getattr(model, "__graphimo_pytorch_runtime__", None)
    if not isinstance(runtime, RuntimeModelInfo) or runtime_registry.resolve(runtime.runtime_instance_id) is not model:
        raise GraphimoPytorchError("pytorch_runtime_not_registered", "Model does not have registered Graphimo-PyTorch runtime metadata")
    return runtime


def _scalar_value(value: object) -> float:
    try:
        import torch

        if isinstance(value, torch.Tensor):
            if value.numel() != 1:
                raise GraphimoPytorchError("pytorch_metric_not_scalar", "Tensor metrics must contain exactly one value")
            value = value.detach().item()
    except ImportError:
        pass
    if type(value) not in {int, float}:
        raise GraphimoPytorchError("pytorch_metric_value_invalid", "Metric value must be an int, float or scalar tensor")
    return float(value)


def _summarize_tensor(tensor: object, detail: str, *, bins: int = 64, max_values: int = 4096) -> dict[str, JsonValue]:
    import torch

    if not isinstance(tensor, torch.Tensor):
        raise TypeError("expected a torch.Tensor")
    if detail not in {"summary", "histogram", "sample"}:
        raise GraphimoPytorchError("pytorch_subscription_detail_invalid", f"Unsupported detail {detail!r}")
    if bins < 1 or bins > 256 or max_values < 1 or max_values > 4096:
        raise GraphimoPytorchError("pytorch_summary_bound_invalid", "Histogram bins must be 1..256 and samples 1..4096")
    summary: dict[str, JsonValue] = {
        "shape": [int(value) for value in tensor.shape],
        "dtype": str(tensor.dtype),
        "device": str(tensor.device),
        "numel": int(tensor.numel()),
    }
    if tensor.numel() == 0:
        if detail == "sample":
            summary["sample"] = []
        elif detail == "histogram":
            summary["histogram"] = {"edges": [], "counts": []}
        return summary
    with torch.no_grad():
        if tensor.is_floating_point() or tensor.is_complex():
            finite = torch.isfinite(tensor)
            summary["finite_fraction"] = _number(finite.to(torch.float32).mean())
            if tensor.is_complex():
                real = tensor.real
                magnitudes = tensor.abs()
                summary["mean"] = _number(real.mean())
                summary["std"] = _number(magnitudes.std(unbiased=False))
                summary["min"] = _number(magnitudes.min())
                summary["max"] = _number(magnitudes.max())
                working = magnitudes
            else:
                summary["mean"] = _number(tensor.mean())
                summary["std"] = _number(tensor.std(unbiased=False))
                summary["min"] = _number(tensor.min())
                summary["max"] = _number(tensor.max())
                working = tensor
        else:
            summary["finite_fraction"] = 1.0
            summary["min"] = _number(tensor.min())
            summary["max"] = _number(tensor.max())
            working = tensor.to(torch.float32)
        if detail == "histogram":
            sampled = _bounded_flat_sample(working, max_values)
            counts, edges = torch.histogram(sampled, bins=bins)
            summary["histogram"] = {"edges": [float(x) for x in edges.detach().cpu().tolist()],
                                    "counts": [int(x) for x in counts.detach().cpu().tolist()]}
        elif detail == "sample":
            sample = _bounded_flat_sample(working, max_values)
            summary["sample"] = [float(value) for value in sample.detach().cpu().tolist()]
    return summary


def _bounded_flat_sample(tensor: object, maximum: int) -> object:
    import torch

    count = tensor.numel()
    if count <= maximum:
        return tensor.detach().reshape(-1)
    indices = torch.linspace(0, count - 1, steps=maximum, device=tensor.device).to(dtype=torch.long)
    return tensor.detach().reshape(-1).index_select(0, indices)


def _number(value: object) -> float | None:
    result = float(value.detach().cpu().item())
    return result if math.isfinite(result) else None


def _tensor_leaves(value: object, *, depth: int = 0) -> tuple[object, ...]:
    import torch

    if isinstance(value, torch.Tensor):
        return (value,)
    if depth >= 4:
        return ()
    if isinstance(value, Mapping):
        return tuple(leaf for _, child in list(value.items())[:16] for leaf in _tensor_leaves(child, depth=depth + 1))
    if isinstance(value, (tuple, list)):
        return tuple(leaf for child in value[:16] for leaf in _tensor_leaves(child, depth=depth + 1))
    return ()


def _summarize_tree(value: object, detail: str, depth: int = 0) -> dict[str, JsonValue]:
    import torch

    if isinstance(value, torch.Tensor):
        return {"type": "tensor", "summary": _summarize_tensor(value, detail)}
    if depth >= 4:
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, Mapping):
        items = list(value.items())[:16]
        return {"type": "dict", "items": [{"key": str(key)[:128], "value": _summarize_tree(child, detail, depth + 1)} for key, child in items]}
    if isinstance(value, (tuple, list)):
        children = value[:16]
        return {"type": "tuple" if isinstance(value, tuple) else "list",
                "items": [_summarize_tree(child, detail, depth + 1) for child in children],
                "truncated": len(value) > len(children)}
    if value is None or type(value) in {str, int, float, bool}:
        return {"type": type(value).__name__, "value": value if type(value) is not float or math.isfinite(value) else str(value)}
    return {"type": type(value).__name__}


def _event(runtime: RuntimeModelInfo, kind: str, payload: dict[str, JsonValue],
           spec: SubscriptionSpec | None = None, site: RuntimeSite | None = None,
           step: int | None = None) -> PyTorchEvent:
    return PyTorchEvent(1, uuid.uuid4().hex, kind, time.time(), runtime.metablock_id,
                        runtime.graph_revision, runtime.runtime_instance_id,
                        site.logical_element_id if site else spec.logical_element_id if spec else None,
                        site.module_path if site else None,
                        site.group_instance_path if site else (), step, payload)


def _emit_runtime(runtime: RuntimeModelInfo, kind: str, payload: dict[str, JsonValue],
                  *, step: int | None = None) -> PyTorchEvent:
    event = _event(runtime, kind, payload, step=step)
    event_queue.put(event)
    return event


def _repetition_index(site: RuntimeSite) -> int | None:
    return site.group_instance_path[-1].repetition_index if site.group_instance_path else None


def _bounded_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:512]

