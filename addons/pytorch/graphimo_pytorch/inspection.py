"""On-demand deep model inspection through the pinned TorchLens addon dependency."""

from __future__ import annotations

import json
import math
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Mapping

from graphimo.artifacts import ArtifactRef, LocalArtifactStore

from .diagnostics import Diagnostic, GraphimoPytorchError
from .runtime import RuntimeModelInfo, runtime_registry
from .schema import JsonValue
from .telemetry import summarize_tensor


@dataclass(frozen=True, slots=True)
class TorchLensIntervention:
    """An explicit, one-run activation experiment; never a graph edit."""

    kind: Literal["zero", "scale"]
    factor: float = 1.0

    def __post_init__(self) -> None:
        if self.kind not in {"zero", "scale"}:
            raise GraphimoPytorchError("pytorch_torchlens_intervention_invalid", "Intervention kind must be 'zero' or 'scale'")
        if type(self.factor) not in {int, float} or not math.isfinite(float(self.factor)):
            raise GraphimoPytorchError("pytorch_torchlens_intervention_invalid", "Intervention factor must be a finite number")


@dataclass(frozen=True, slots=True)
class TorchLensRequest:
    runtime_instance_id: str
    logical_element_id: str
    repetition_index: int | None = None
    capture_gradients: bool = False
    intervention: TorchLensIntervention | None = None

    def __post_init__(self) -> None:
        if not self.runtime_instance_id or not self.logical_element_id:
            raise GraphimoPytorchError("pytorch_torchlens_request_invalid", "Runtime and logical element IDs are required")
        if type(self.capture_gradients) is not bool:
            raise GraphimoPytorchError("pytorch_torchlens_request_invalid", "capture_gradients must be a boolean")
        if self.repetition_index is not None and (type(self.repetition_index) is not int or self.repetition_index < 0):
            raise GraphimoPytorchError("pytorch_torchlens_request_invalid", "repetition_index must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class TorchLensOperation:
    label: str
    function: str


@dataclass(frozen=True, slots=True)
class TorchLensResult:
    runtime_instance_id: str
    metablock_id: str
    graph_revision: str
    materialization_key: str
    logical_element_id: str
    module_path: str
    repetition_index: int | None
    operations: tuple[TorchLensOperation, ...]
    operation_count: int
    activation: Mapping[str, JsonValue] | None
    gradient: Mapping[str, JsonValue] | None
    nonfinite_diagnostics: tuple[str, ...]
    trace_metadata: Mapping[str, JsonValue]
    experimental: bool
    intervention: TorchLensIntervention | None
    diagnostics: tuple[Diagnostic, ...] = ()
    artifact_ref: ArtifactRef | None = None
    _trace: object | None = field(default=None, repr=False, compare=False)


def inspect_with_torchlens(
    request: TorchLensRequest,
    inputs: object,
    *,
    loss_fn: Callable[[object], object] | None = None,
) -> TorchLensResult:
    """Capture one explicit input through TorchLens and return bounded inspector data.

    A tensor is passed as one positional input, tuple/list values as positional inputs,
    and mappings as keyword inputs. No live training batch is captured implicitly.
    """
    runtime, model, module_path = _resolve_target(request)
    torch, torchlens = _load_torchlens()
    started = time.perf_counter()
    args, kwargs = _normalize_inputs(inputs)
    selector = torchlens.module(module_path)
    runtime_state = _snapshot_runtime_state(model)
    capture_options: dict[str, object] = {
        "layers_to_save": [module_path, "output"],
        "verbose": False,
        "cache": False,
    }
    diagnostics: list[Diagnostic] = []
    if request.capture_gradients and loss_fn is not None:
        capture_options["backward_ready"] = True
        # Predicate captures in TorchLens 2.34.1 resolve module gradient selectors
        # differently, so an explicit intervention run retains gradients for that
        # one experiment and the returned result still extracts only the chosen site.
        capture_options["save_grads"] = True if request.intervention is not None else [module_path]
    options: dict[str, object] = {
        "capture": torchlens.options.CaptureOptions(**capture_options),
    }
    if request.intervention is not None:
        helper = (
            torchlens.zero_ablate()
            if request.intervention.kind == "zero"
            else torchlens.scale(float(request.intervention.factor))
        )
        options["intervene"] = torchlens.when(selector, helper)
    elif request.capture_gradients and loss_fn is None:
        diagnostics.append(Diagnostic(
            "pytorch_torchlens_loss_fn_required",
            "Gradient capture was requested, but no loss_fn was supplied; activation inspection completed without backward capture.",
            "info",
            request.logical_element_id,
        ))

    try:
        trace = torchlens.trace(model, *args, input_kwargs=kwargs or None, **options)
        module_sites = trace.find_sites(selector)
        if len(module_sites) == 0:
            raise GraphimoPytorchError(
                "pytorch_torchlens_site_not_found",
                f"TorchLens could not resolve the binding path {module_path!r} for element {request.logical_element_id!r}",
            )
        output = _trace_output(trace, torch)
        gradient_summary: Mapping[str, JsonValue] | None = None
        if request.capture_gradients and loss_fn is not None:
            loss = loss_fn(output)
            if not isinstance(loss, torch.Tensor) or loss.numel() != 1:
                raise GraphimoPytorchError("pytorch_torchlens_loss_invalid", "loss_fn must return a scalar torch.Tensor")
            trace.backward(loss)
            gradient_summary = _selected_gradient(trace, module_path)
            if gradient_summary is None:
                diagnostics.append(Diagnostic(
                    "pytorch_torchlens_gradient_unavailable",
                    "The explicit loss completed, but TorchLens did not retain a gradient for the selected module.",
                    "warning",
                    request.logical_element_id,
                ))

        activation_summary = _selected_activation(
            trace,
            module_path,
            prefer_intervention=request.intervention is not None,
        )
        if activation_summary is None:
            diagnostics.append(Diagnostic(
                "pytorch_torchlens_activation_unavailable",
                "TorchLens resolved the module, but no saved activation payload was available.",
                "warning",
                request.logical_element_id,
            ))
        operations, operation_count = _operation_summary(trace, torchlens, module_path)
        nonfinite = _nonfinite_diagnostics(request.logical_element_id, activation_summary, gradient_summary)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return TorchLensResult(
            runtime.runtime_instance_id,
            runtime.metablock_id,
            runtime.graph_revision,
            runtime.materialization_key,
            request.logical_element_id,
            module_path,
            request.repetition_index,
            operations,
            operation_count,
            activation_summary,
            gradient_summary,
            nonfinite,
            {
                "torchlens_version": str(torchlens.__version__),
                "pytorch_version": str(torch.__version__),
                "saved_layer_count": int(len(trace.saved_layers)),
                "operation_count": int(operation_count),
                "capture_gradients": bool(request.capture_gradients and loss_fn is not None),
                "explicit_input": True,
                "elapsed_ms": round(elapsed_ms, 3),
                "intervention": request.intervention.kind if request.intervention else None,
            },
            request.intervention is not None,
            request.intervention,
            tuple(diagnostics),
            None,
            trace,
        )
    except GraphimoPytorchError:
        raise
    except Exception as error:
        raise GraphimoPytorchError(
            "pytorch_torchlens_inspection_failed",
            f"TorchLens inspection failed: {type(error).__name__}: {error}",
        ) from error
    finally:
        _restore_runtime_state(runtime_state)


def save_torchlens_inspection(result: TorchLensResult, store: LocalArtifactStore) -> ArtifactRef:
    """Explicitly export a full TorchLens trace and provenance report as an Artifact."""
    if result._trace is None:
        raise GraphimoPytorchError("pytorch_torchlens_trace_unavailable", "This inspection result has no live TorchLens Trace to export")
    with tempfile.TemporaryDirectory(prefix="graphimo-torchlens-export-") as temporary:
        root = Path(temporary)
        trace_path = root / "torchlens-trace"
        try:
            result._trace.save(trace_path)
            report: dict[str, JsonValue] = {
                "format": 1,
                "metablock_id": result.metablock_id,
                "graph_revision": result.graph_revision,
                "materialization_key": result.materialization_key,
                "runtime_instance_id": result.runtime_instance_id,
                "logical_element_id": result.logical_element_id,
                "module_path": result.module_path,
                "repetition_index": result.repetition_index,
                "torchlens_version": result.trace_metadata["torchlens_version"],
                "pytorch_version": result.trace_metadata["pytorch_version"],
                "experimental": result.experimental,
                "intervention": result.intervention.kind if result.intervention else None,
                "operations": [{"label": item.label, "function": item.function} for item in result.operations],
                "operation_count": result.operation_count,
                "activation": dict(result.activation) if result.activation else None,
                "gradient": dict(result.gradient) if result.gradient else None,
                "nonfinite_diagnostics": list(result.nonfinite_diagnostics),
            }
            (root / "inspection.json").write_text(
                json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2),
                encoding="utf-8",
            )
            return store.put_directory(root, kind="pytorch-torchlens-trace")
        except Exception as error:
            raise GraphimoPytorchError(
                "pytorch_torchlens_export_failed",
                f"Unable to export the TorchLens trace: {type(error).__name__}: {error}",
            ) from error


def _resolve_target(request: TorchLensRequest) -> tuple[RuntimeModelInfo, object, str]:
    runtime = runtime_registry.info(request.runtime_instance_id)
    model = runtime_registry.resolve(request.runtime_instance_id)
    if runtime is None or model is None:
        raise GraphimoPytorchError("pytorch_runtime_not_found", "The requested live PyTorch runtime instance is no longer available")
    sites = tuple(site for site in runtime.bindings.for_element(request.logical_element_id) if site.inspectable)
    if request.repetition_index is not None:
        sites = tuple(
            site for site in sites
            if site.group_instance_path and site.group_instance_path[-1].repetition_index == request.repetition_index
        )
    if not sites:
        raise GraphimoPytorchError(
            "pytorch_torchlens_binding_not_found",
            f"No inspectable runtime binding exists for element {request.logical_element_id!r} and repetition {request.repetition_index!r}",
        )
    if len(sites) > 1:
        raise GraphimoPytorchError(
            "pytorch_torchlens_repetition_required",
            f"Element {request.logical_element_id!r} resolves to {len(sites)} runtime sites; provide a repetition_index to select one",
        )
    return runtime, model, sites[0].module_path


def _load_torchlens() -> tuple[object, object]:
    try:
        import torch
        import torchlens
    except ImportError as error:
        raise GraphimoPytorchError(
            "pytorch_torchlens_unavailable",
            "Deep inspection requires the graphimo-pytorch addon dependency torchlens==2.34.1 in the selected Python environment",
        ) from error
    version = str(getattr(torchlens, "__version__", ""))
    if version != "2.34.1":
        raise GraphimoPytorchError(
            "pytorch_torchlens_version_unsupported",
            f"Expected pinned TorchLens 2.34.1, found {version or 'unknown'}",
        )
    return torch, torchlens


def _normalize_inputs(inputs: object) -> tuple[tuple[object, ...], dict[str, object]]:
    if isinstance(inputs, Mapping):
        if not all(isinstance(key, str) for key in inputs):
            raise GraphimoPytorchError("pytorch_torchlens_input_invalid", "Mapping input keys must be strings for model keyword arguments")
        return (), dict(inputs)
    if isinstance(inputs, tuple):
        return inputs, {}
    if isinstance(inputs, list):
        return tuple(inputs), {}
    return (inputs,), {}


def _snapshot_runtime_state(model: object) -> tuple[tuple[object, str, object | None], ...]:
    """Keep explicit inspection/backward from leaving gradients or buffers behind."""
    snapshot: list[tuple[object, str, object | None]] = []
    for accessor_name in ("parameters", "buffers"):
        accessor = getattr(model, accessor_name, None)
        if not callable(accessor):
            continue
        for tensor in accessor():
            if accessor_name == "parameters":
                gradient = getattr(tensor, "grad", None)
                snapshot.append((tensor, "gradient", None if gradient is None else gradient.detach().clone()))
            else:
                snapshot.append((tensor, "buffer", tensor.detach().clone()))
    return tuple(snapshot)


def _restore_runtime_state(snapshot: tuple[tuple[object, str, object | None], ...]) -> None:
    try:
        import torch
    except ImportError:
        return
    with torch.no_grad():
        for tensor, kind, value in snapshot:
            if kind == "gradient":
                tensor.grad = value
            elif value is not None:
                tensor.copy_(value)


def _trace_output(trace: object, torch: object) -> object:
    outputs = [getattr(operation, "out", None) for operation in trace.output_ops]
    outputs = [output for output in outputs if isinstance(output, torch.Tensor)]
    if not outputs:
        raise GraphimoPytorchError("pytorch_torchlens_output_unavailable", "TorchLens did not retain a tensor model output")
    return outputs[0] if len(outputs) == 1 else tuple(outputs)


def _selected_activation(
    trace: object,
    module_path: str,
    *,
    prefer_intervention: bool,
) -> Mapping[str, JsonValue] | None:
    candidates = []
    for layer in trace.saved_layers:
        module_sites = getattr(layer, "modules", ()) or ()
        if any(str(site).split(":", 1)[0] == module_path for site in module_sites):
            candidates.append(layer)
    if prefer_intervention:
        candidates.sort(key=lambda layer: getattr(layer, "func_name", "") != "intervention_replacement")
    for layer in candidates:
        tensor = getattr(layer, "out", None)
        if tensor is not None:
            return summarize_tensor(tensor, detail="summary")
    return None


def _selected_gradient(trace: object, module_path: str) -> Mapping[str, JsonValue] | None:
    for layer in trace.saved_grad_layers:
        module_sites = getattr(layer, "modules", ()) or ()
        if any(str(site).split(":", 1)[0] == module_path for site in module_sites):
            tensor = getattr(layer, "grad", None)
            if tensor is not None:
                return summarize_tensor(tensor, detail="summary")
    return None


def _operation_summary(trace: object, torchlens: object, module_path: str) -> tuple[tuple[TorchLensOperation, ...], int]:
    sites = trace.find_sites(torchlens.in_module(module_path))
    labels = sites.labels()
    operations: list[TorchLensOperation] = []
    for site in list(sites)[:128]:
        operations.append(TorchLensOperation(
            str(getattr(site, "layer_label", getattr(site, "label", "unknown"))),
            str(getattr(site, "func_name", "unknown")),
        ))
    return tuple(operations), len(labels)


def _nonfinite_diagnostics(
    element_id: str,
    activation: Mapping[str, JsonValue] | None,
    gradient: Mapping[str, JsonValue] | None,
) -> tuple[str, ...]:
    findings: list[str] = []
    for name, value in (("activation", activation), ("gradient", gradient)):
        fraction = value.get("finite_fraction") if value else None
        if isinstance(fraction, (float, int)) and fraction < 1.0:
            findings.append(f"{element_id} {name} contains non-finite values (finite_fraction={fraction})")
    return tuple(findings)
