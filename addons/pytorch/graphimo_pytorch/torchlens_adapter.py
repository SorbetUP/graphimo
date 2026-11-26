"""Explicit, bounded TorchLens deep inspection for the PyTorch addon."""

from __future__ import annotations

import copy
import io
import json
import math
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any


MAX_RELEVANT_OPERATIONS = 32


@dataclass(frozen=True)
class TorchLensIntervention:
    """A one-run activation edit. It never changes the semantic graph."""

    kind: str
    factor: float = 1.0

    def __post_init__(self) -> None:
        if self.kind not in {"zero_ablation", "scale"}:
            raise ValueError("intervention kind must be zero_ablation or scale")
        if self.kind == "scale":
            if isinstance(self.factor, bool) or not isinstance(self.factor, (int, float)):
                raise ValueError("scale factor must be a finite number")
            if not math.isfinite(self.factor):
                raise ValueError("scale factor must be finite")


@dataclass(frozen=True)
class TorchLensRequest:
    runtime_instance_id: str
    logical_element_id: str
    repetition_index: int | None = None
    capture_gradients: bool = False
    intervention: TorchLensIntervention | None = None

    def __post_init__(self) -> None:
        if self.repetition_index is not None and (
            type(self.repetition_index) is not int or self.repetition_index < 0
        ):
            raise ValueError("repetition_index must be a non-negative integer or None")
        if type(self.capture_gradients) is not bool:
            raise ValueError("capture_gradients must be a boolean")
        if self.intervention is not None and not isinstance(
            self.intervention, TorchLensIntervention
        ):
            raise ValueError("intervention must be a TorchLensIntervention or None")


@dataclass(frozen=True)
class TorchLensResult:
    inspection_id: str
    runtime_instance_id: str
    logical_element_id: str
    repetition_index: int | None
    module_path: str
    graph_revision: str
    experimental: bool
    intervention: dict[str, Any] | None
    selected_activation: dict[str, Any]
    gradients: dict[str, Any] | None
    operations: tuple[dict[str, Any], ...]
    non_finite: dict[str, Any]
    trace: dict[str, Any]
    torchlens_version: str
    pytorch_version: str
    artifact_ref: dict[str, str] | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _site_for_request(request: TorchLensRequest, registry: Any) -> tuple[Any, Any, Any]:
    if not isinstance(request.runtime_instance_id, str) or not request.runtime_instance_id:
        raise ValueError("runtime_instance_id must be non-empty text")
    if not isinstance(request.logical_element_id, str) or not request.logical_element_id:
        raise ValueError("logical_element_id must be non-empty text")
    model = registry.resolve(request.runtime_instance_id)
    info = registry.info(request.runtime_instance_id)
    if model is None or info is None:
        raise ValueError("runtime model is no longer available")
    sites = [
        site
        for site in info.bindings.sites
        if site.logical_element_id == request.logical_element_id and site.inspectable
    ]
    if request.repetition_index is not None:
        sites = [
            site
            for site in sites
            if site.group_instance_path
            and (
                site.group_instance_path[-1].repetition_index
                if hasattr(site.group_instance_path[-1], "repetition_index")
                else site.group_instance_path[-1][1]
            )
            == request.repetition_index
        ]
    elif len(sites) > 1:
        raise ValueError("a repeated runtime site requires repetition_index")
    if len(sites) != 1:
        raise ValueError("the selected element and repetition must resolve to one inspectable site")
    return model, info, sites[0]


def _trace_single_process(
    torchlens: Any, model: Any, positional: Any, keyword: Any, **kwargs: Any
) -> Any:
    """Run ``torchlens.trace`` inside a marimo kernel process.

    TorchLens refuses to run outside the ``MainProcess`` (its global toggle state
    is not concurrency-safe). A marimo kernel executes cells serially in its own
    process, so the guard is neutralized for the duration of this single call and
    restored immediately afterwards.
    """
    import multiprocessing as mp

    if mp.current_process().name == "MainProcess":
        return torchlens.trace(model, positional, keyword, **kwargs)
    guards: list[tuple[Any, Any]] = []
    for module_name in ("torchlens.user_funcs", "torchlens._user_public_impls"):
        try:
            module = __import__(module_name, fromlist=["warn_parallel"])
        except ImportError:
            continue
        original = getattr(module, "warn_parallel", None)
        if original is not None:
            module.warn_parallel = lambda: None
            guards.append((module, original))
    try:
        return torchlens.trace(model, positional, keyword, **kwargs)
    finally:
        for module, original in guards:
            module.warn_parallel = original


def _normalize_inputs(inputs: object) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Normalize explicit tensors/JSON values to TorchLens positional or keyword inputs."""
    import torch

    selected_dtype = None
    if isinstance(inputs, Mapping) and "$inputs" in inputs:
        dtype_name = inputs.get("$dtype", "float32")
        dtype_map = {
            "float16": torch.float16,
            "float32": torch.float32,
            "float64": torch.float64,
            "int64": torch.int64,
            "int32": torch.int32,
            "bool": torch.bool,
        }
        if dtype_name not in dtype_map:
            raise ValueError("explicit input dtype must be float16/32/64, int32/64, or bool")
        selected_dtype = dtype_map[dtype_name]
        inputs = inputs["$inputs"]

    def convert(value: Any) -> Any:
        if isinstance(value, torch.Tensor):
            return value
        if isinstance(value, Mapping):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return tuple(convert(item) for item in value)
        if isinstance(value, list):
            # A JSON numeric array is one tensor; structured lists remain containers.
            try:
                tensor = torch.as_tensor(value)
            except (TypeError, ValueError, RuntimeError):
                return [convert(item) for item in value]
            if tensor.dtype == torch.float64:
                tensor = tensor.float()
            if selected_dtype is not None:
                tensor = tensor.to(dtype=selected_dtype)
            return tensor
        if isinstance(value, (bool, int, float)):
            tensor = torch.as_tensor(value)
            return tensor.to(dtype=selected_dtype) if selected_dtype is not None else tensor
        raise TypeError(f"unsupported explicit TorchLens input value: {type(value).__name__}")

    if isinstance(inputs, Mapping) and ("$args" in inputs or "$kwargs" in inputs):
        args = inputs.get("$args", [])
        kwargs = inputs.get("$kwargs", {})
        if not isinstance(args, list | tuple) or not isinstance(kwargs, Mapping):
            raise ValueError("explicit $args must be an array and $kwargs must be an object")
        return tuple(convert(value) for value in args), {
            key: convert(value) for key, value in kwargs.items()
        }
    if isinstance(inputs, Mapping):
        return (), {key: convert(value) for key, value in inputs.items()}
    if isinstance(inputs, tuple):
        return tuple(convert(value) for value in inputs), {}
    if isinstance(inputs, list):
        # Treat a numeric nested array as a single tensor, matching JSON fixtures.
        try:
            tensor = torch.as_tensor(inputs)
        except (TypeError, ValueError, RuntimeError):
            return tuple(convert(value) for value in inputs), {}
        if tensor.dtype == torch.float64:
            tensor = tensor.float()
        if selected_dtype is not None:
            tensor = tensor.to(dtype=selected_dtype)
        return (tensor,), {}
    return (convert(inputs),), {}


def _tensor_summary(value: Any) -> dict[str, Any]:
    import torch

    tensor = value if isinstance(value, torch.Tensor) else None
    if tensor is None:
        return {"available": False, "reason": "selected site did not return a tensor"}
    detached = tensor.detach()
    finite_mask = (
        torch.isfinite(detached) if detached.is_floating_point() or detached.is_complex() else None
    )
    finite_count = int(finite_mask.sum().item()) if finite_mask is not None else detached.numel()
    result: dict[str, Any] = {
        "available": True,
        "shape": list(detached.shape),
        "dtype": str(detached.dtype),
        "device": str(detached.device),
        "numel": int(detached.numel()),
        "finite_count": finite_count,
        "non_finite_count": int(detached.numel()) - finite_count,
    }
    if detached.numel() and (detached.is_floating_point() or detached.is_complex()):
        numeric = detached.real.float() if detached.is_complex() else detached.float()
        finite_values = numeric[finite_mask] if finite_mask is not None else numeric
        if finite_values.numel():
            result.update(
                minimum=float(finite_values.min().item()),
                maximum=float(finite_values.max().item()),
                mean=float(finite_values.mean().item()),
                std=float(finite_values.std(unbiased=False).item()),
            )
    return result


def _operation_summaries(
    trace: Any, module_path: str, selected_site: Any
) -> tuple[dict[str, Any], ...]:
    selected: list[dict[str, Any]] = []
    target_parts = module_path.split(".")
    for layer in trace.layers:
        stack = list(getattr(layer, "module_call_stack", ()) or ())
        if stack and (stack[-len(target_parts) :] == target_parts or stack[-1] == target_parts[-1]):
            selected.append(
                {
                    "label": str(getattr(layer, "label", "")),
                    "operation": str(getattr(layer, "func_name", "unknown")),
                    "module_path": ".".join(stack),
                    "shape": list(getattr(layer, "shape", ()) or ()),
                    "dtype": str(getattr(layer, "dtype", "unknown")),
                }
            )
            if len(selected) >= MAX_RELEVANT_OPERATIONS:
                break
    selected_label = str(getattr(selected_site, "label", ""))
    if selected_label and not any(item["label"] == selected_label for item in selected):
        selected.insert(
            0,
            {
                "label": selected_label,
                "operation": str(getattr(selected_site, "func_name", "unknown")),
                "module_path": module_path,
                "shape": list(getattr(selected_site, "shape", ()) or ()),
                "dtype": str(getattr(selected_site, "dtype", "unknown")),
            },
        )
    return tuple(selected)


def _perform_inspection(
    request: TorchLensRequest,
    inputs: object,
    *,
    registry: Any,
    loss_fn: Callable[[Any], Any] | None = None,
    save_all_activations: bool = False,
) -> tuple[TorchLensResult, Any]:
    try:
        import torch
        import torchlens
    except Exception as exc:  # pragma: no cover - depends on broken optional install
        raise RuntimeError(f"TorchLens is unavailable or broken: {exc}") from exc

    model, info, site = _site_for_request(request, registry)
    try:
        inspection_model = copy.deepcopy(model)
    except Exception as exc:
        raise RuntimeError(
            "TorchLens inspection needs a copyable eager model so the live runtime is not mutated"
        ) from exc
    for module in inspection_model.modules():
        for hook_name in ("_forward_hooks", "_forward_pre_hooks", "_backward_hooks"):
            hooks = getattr(module, hook_name, {})
            for hook_id, hook in tuple(hooks.items()):
                if getattr(hook, "__graphimo_live_telemetry__", False):
                    hooks.pop(hook_id, None)
    module_path = site.module_path
    try:
        inspection_model.get_submodule(module_path)
    except (AttributeError, KeyError) as exc:
        raise ValueError(f"bound module path {module_path!r} is unavailable") from exc

    positional, keyword = _normalize_inputs(inputs)
    try:
        device = next(inspection_model.parameters()).device
    except StopIteration:
        device = torch.device("cpu")

    def move(value: Any) -> Any:
        if isinstance(value, torch.Tensor):
            return value.to(device=device)
        if isinstance(value, dict):
            return {key: move(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return tuple(move(item) for item in value)
        if isinstance(value, list):
            return [move(item) for item in value]
        return value

    positional = tuple(move(value) for value in positional)
    keyword = {key: move(value) for key, value in keyword.items()}
    captured_output: list[Any] = []
    root_hook = inspection_model.register_forward_hook(
        lambda _module, _inputs, output: captured_output.append(output)
    )
    selector = torchlens.module(module_path)
    capture_options = torchlens.options.CaptureOptions(
        name=f"graphimo_{request.logical_element_id}",
        save_grads=request.capture_gradients,
        backward_ready=request.capture_gradients,
        inference_only=not request.capture_gradients,
    )
    kwargs: dict[str, Any] = {
        "save": None if save_all_activations else selector,
        "capture": capture_options,
    }
    if request.intervention is not None:
        helper = (
            torchlens.zero_ablate()
            if request.intervention.kind == "zero_ablation"
            else torchlens.scale(request.intervention.factor)
        )
        kwargs["intervene"] = torchlens.when(selector, helper)
    try:
        trace = _trace_single_process(
            torchlens, inspection_model, positional, keyword or None, **kwargs
        )
    except Exception as exc:
        root_hook.remove()
        raise RuntimeError(f"TorchLens capture failed: {type(exc).__name__}: {exc}") from exc
    finally:
        root_hook.remove()
    if not captured_output:
        raise RuntimeError("TorchLens completed without exposing the model output for diagnostics")

    gradient_summary = None
    if request.capture_gradients:
        output = captured_output[-1]
        if loss_fn is not None:
            loss = loss_fn(output)
        elif isinstance(output, torch.Tensor) and output.numel() == 1:
            loss = output.reshape(())
        else:
            raise ValueError(
                "gradient inspection needs a loss_fn or a known scalar tensor model output"
            )
        if not isinstance(loss, torch.Tensor) or loss.numel() != 1:
            raise ValueError("loss_fn must return one scalar tensor")
        trace.log_backward(loss.reshape(()))

    target = trace.find_sites(selector).first()
    if target is None:
        raise RuntimeError(f"TorchLens did not resolve selected module {module_path!r}")
    activation = _tensor_summary(target.out)
    if request.capture_gradients:
        gradient = getattr(target, "grad", None)
        gradient_summary = (
            _tensor_summary(gradient)
            if gradient is not None
            else {
                "available": False,
                "reason": "TorchLens did not record a gradient for the selected activation",
            }
        )

    operations = _operation_summaries(trace, module_path, target)
    summary = {
        "model_class": type(inspection_model).__qualname__,
        "operation_count": int(getattr(trace, "num_compute_ops", 0)),
        "layer_count": int(getattr(trace, "num_layers", 0)),
        "saved_site_count": int(getattr(trace, "num_saved_modules", 0)),
        "capture_duration_seconds": float(getattr(trace, "capture_duration", 0.0) or 0.0),
        "selected_operation_count": len(operations),
    }
    result = TorchLensResult(
        inspection_id=__import__("uuid").uuid4().hex,
        runtime_instance_id=request.runtime_instance_id,
        logical_element_id=request.logical_element_id,
        repetition_index=request.repetition_index,
        module_path=module_path,
        graph_revision=info.graph_revision,
        experimental=request.intervention is not None,
        intervention=(asdict(request.intervention) if request.intervention else None),
        selected_activation=activation,
        gradients=gradient_summary,
        operations=operations,
        non_finite={
            "selected_activation_count": activation.get("non_finite_count", 0),
            "trace_first_non_finite": (
                _first_nonfinite(trace) if activation.get("non_finite_count", 0) else None
            ),
        },
        trace=summary,
        torchlens_version=str(getattr(torchlens, "__version__", "unknown")),
        pytorch_version=str(torch.__version__),
    )
    return result, trace


def _first_nonfinite(trace: Any) -> dict[str, Any] | None:
    try:
        item = trace.first_nonfinite()
    except Exception:
        return None
    if item is None:
        return None
    if getattr(item, "label", None) is None and getattr(item, "func_name", None) is None:
        return None
    return {
        "label": str(getattr(item, "label", "unknown")),
        "operation": str(getattr(item, "func_name", "unknown")),
        "shape": list(getattr(item, "shape", ()) or ()),
    }


def inspect_with_torchlens(
    request: TorchLensRequest,
    inputs: object,
    *,
    loss_fn: Callable | None = None,
) -> TorchLensResult:
    """Run one explicit TorchLens pass against the selected Graphimo binding."""
    from .runtime import RUNTIME_REGISTRY

    registry = RUNTIME_REGISTRY
    result, _trace = _perform_inspection(request, inputs, registry=registry, loss_fn=loss_fn)
    return result


def save_inspection_artifact(
    request: TorchLensRequest,
    inputs: object,
    *,
    artifact_store: Any = None,
    loss_fn: Callable | None = None,
) -> TorchLensResult:
    """Explicitly export a fresh bounded TorchLens trace and report as one artifact."""
    from .runtime import RUNTIME_REGISTRY

    registry = RUNTIME_REGISTRY
    result, trace = _perform_inspection(
        request, inputs, registry=registry, loss_fn=loss_fn, save_all_activations=True
    )
    import torch

    operation_records: list[dict[str, Any]] = []
    activation_tensors: dict[str, Any] = {}
    gradient_tensors: dict[str, Any] = {}
    for index, layer in enumerate(trace.layers):
        label = str(getattr(layer, "label", f"operation_{index}"))
        operation_records.append(
            {
                "label": label,
                "operation": str(getattr(layer, "func_name", "unknown")),
                "module_path": ".".join(getattr(layer, "module_call_stack", ()) or ()),
                "shape": list(getattr(layer, "shape", ()) or ()),
                "dtype": str(getattr(layer, "dtype", "unknown")),
                "has_activation": bool(getattr(layer, "has_saved_activation", False)),
                "has_gradient": bool(getattr(layer, "has_saved_gradient", False)),
            }
        )
        if getattr(layer, "has_saved_activation", False):
            value = getattr(layer, "out", None)
            if isinstance(value, torch.Tensor):
                activation_tensors[f"{index}:{label}"] = value.detach().cpu()
        if getattr(layer, "has_saved_gradient", False):
            value = getattr(layer, "grad", None)
            if isinstance(value, torch.Tensor):
                gradient_tensors[f"{index}:{label}"] = value.detach().cpu()
    tensor_buffer = io.BytesIO()
    torch.save({"activations": activation_tensors, "gradients": gradient_tensors}, tensor_buffer)
    trace_manifest = {
        "format": "graphimo-torchlens-trace-v1",
        "trace": result.trace,
        "operations": operation_records,
        "tensor_payload": "tensors.pt",
    }
    info = registry.info(request.runtime_instance_id)
    artifact_metadata = {
        "metablock_id": info.metablock_id,
        "graph_revision": info.graph_revision,
        "materialization_key": info.materialization_key,
        "runtime_instance_id": request.runtime_instance_id,
        "logical_element_id": request.logical_element_id,
        "module_path": result.module_path,
        "torchlens_version": result.torchlens_version,
        "pytorch_version": result.pytorch_version,
    }
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.json", json.dumps(result.to_json(), sort_keys=True))
        archive.writestr("artifact_metadata.json", json.dumps(artifact_metadata, sort_keys=True))
        archive.writestr("trace/ops.json", json.dumps(trace_manifest, sort_keys=True))
        archive.writestr("trace/tensors.pt", tensor_buffer.getvalue())
    payload = archive_buffer.getvalue()

    if artifact_store is not None and hasattr(artifact_store, "put_bytes"):
        artifact = artifact_store.put_bytes(payload, kind="pytorch-torchlens-trace")
        artifact_id = getattr(artifact, "id", None) or getattr(artifact, "artifact_id", None)
    else:
        from graphimo.artifacts import put_bytes

        artifact = put_bytes(payload, kind="pytorch-torchlens-trace")
        artifact_id = artifact.id
    if not artifact_id:
        raise RuntimeError("ArtifactStore did not return an artifact identifier")
    return TorchLensResult(
        **{
            **result.__dict__,
            "artifact_ref": {"id": str(artifact_id), "kind": "pytorch-torchlens-trace"},
        }
    )
