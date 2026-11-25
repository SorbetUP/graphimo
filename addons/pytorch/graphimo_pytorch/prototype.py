from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ModelSpec:
    input_size: int
    hidden_size: int
    head_sizes: dict[str, int]

    def __post_init__(self) -> None:
        if self.input_size <= 0 or self.hidden_size <= 0 or not self.head_sizes:
            raise ValueError("model dimensions and at least one task head are required")
        if any(not name.isidentifier() or size <= 0 for name, size in self.head_sizes.items()):
            raise ValueError("head names must be identifiers and sizes must be positive")


def build_model(spec: ModelSpec):
    from torch import nn

    class MultiHeadMLP(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = nn.Sequential(nn.Linear(spec.input_size, spec.hidden_size), nn.ReLU())
            self.heads = nn.ModuleDict(
                {
                    name: nn.Linear(spec.hidden_size, size)
                    for name, size in sorted(spec.head_sizes.items())
                }
            )

        def forward(self, value):
            features = self.backbone(value)
            return {name: head(features) for name, head in self.heads.items()}

    return MultiHeadMLP()


def compile_fx(spec: ModelSpec, *, batch_size: int = 1):
    """Compile the portable ModelSpec to a torch.fx graph and infer tensor shapes."""
    import torch
    from torch.fx.passes.shape_prop import ShapeProp

    if type(batch_size) is not int or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    graph_module = torch.fx.symbolic_trace(build_model(spec))
    ShapeProp(graph_module).propagate(torch.zeros(batch_size, spec.input_size))
    output_node = next(node for node in graph_module.graph.nodes if node.op == "output")
    metadata = output_node.meta.get("tensor_meta")
    if not isinstance(metadata, dict) or set(metadata) != set(spec.head_sizes):
        raise ValueError("FX shape propagation did not return every model head")
    shapes = {name: list(item.shape) for name, item in metadata.items()}
    return graph_module, shapes


def migrate_weights(source: dict[str, Any], target: dict[str, Any]) -> dict[str, list[str]]:
    """Copy only exact-name, exact-shape tensors and report every decision."""
    loaded: list[str] = []
    incompatible: list[str] = []
    new: list[str] = []
    for key, value in target.items():
        old = source.get(key)
        if old is None:
            new.append(key)
        elif tuple(old.shape) == tuple(value.shape):
            target[key] = old
            loaded.append(key)
        else:
            incompatible.append(key)
    ignored = sorted(set(source) - set(target))
    return {"loaded": loaded, "new": new, "incompatible": incompatible, "ignored": ignored}


def train_multihead(
    model: Any,
    x: Any,
    targets: dict[str, Any],
    *,
    epochs: int = 8,
    lr: float = 0.03,
    emit: Callable[[str, dict[str, Any], int | None, str | None, bool], None] | None = None,
    optimizer: Any | None = None,
    start_epoch: int = 0,
    poll_controls: Callable[[], list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, float], Any]:
    import torch
    import torch.nn.functional as F

    if set(targets) != set(model.heads):
        raise ValueError("targets must be supplied for every model head")
    optimizer = optimizer or torch.optim.AdamW(model.parameters(), lr=lr)
    active_probes: dict[str, tuple[Any, Any]] = {}
    last: dict[str, float] = {}
    with torch.no_grad():
        initial = model(x)
        initial_total = float(
            sum(F.mse_loss(initial[name], target) for name, target in targets.items())
        )

    modules = dict(model.named_modules())

    def publish_probe(path: str, phase: str, value: Any, step: int) -> None:
        if emit is None or not isinstance(value, torch.Tensor):
            return
        detached = value.detach().float()
        emit(
            "model_probe",
            {
                "phase": phase,
                "mean": float(detached.mean()),
                "std": float(detached.std()),
                "shape": list(detached.shape),
            },
            step,
            path,
            False,
        )

    def set_probe(path: str, enabled: bool) -> None:
        if path not in modules:
            raise ValueError(f"unknown model component path: {path}")
        if enabled and path not in active_probes:

            def forward_hook(_module, _inputs, output):
                publish_probe(path, "activation", output, current_epoch[0])

            def backward_hook(_module, _grad_input, grad_output):
                if grad_output:
                    publish_probe(path, "gradient", grad_output[0], current_epoch[0])

            active_probes[path] = (
                modules[path].register_forward_hook(forward_hook),
                modules[path].register_full_backward_hook(backward_hook),
            )
        elif not enabled and path in active_probes:
            for hook in active_probes.pop(path):
                hook.remove()

    current_epoch = [start_epoch]
    try:
        for offset in range(epochs):
            epoch = start_epoch + offset
            current_epoch[0] = epoch
            if poll_controls is not None:
                for command in poll_controls():
                    operation = command.get("op")
                    path = command.get("module_path")
                    if operation not in {"subscribe_probe", "unsubscribe_probe"} or not isinstance(
                        path, str
                    ):
                        raise ValueError("invalid probe control command")
                    set_probe(path, operation == "subscribe_probe")
            optimizer.zero_grad(set_to_none=True)
            predictions = model(x)
            losses = {
                name: F.mse_loss(predictions[name], target) for name, target in targets.items()
            }
            total = sum(losses.values())
            total.backward()
            grad_norm = float(torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=10.0))
            optimizer.step()
            last = {name: float(value.detach()) for name, value in losses.items()}
            last["total"] = float(total.detach())
            if emit:
                emit("metric", {"name": "train/loss", "value": last["total"]}, epoch, None, True)
                for name, value in losses.items():
                    emit(
                        "metric",
                        {"name": f"train/{name}_loss", "value": float(value.detach())},
                        epoch,
                        None,
                        True,
                    )
                emit("metric", {"name": "train/grad_norm", "value": grad_norm}, epoch, None, True)
                emit("progress", {"fraction": (offset + 1) / epochs}, epoch, None, True)
                if torch.cuda.is_available():
                    emit(
                        "resource",
                        {"cuda_allocated_bytes": int(torch.cuda.memory_allocated())},
                        epoch,
                        None,
                        True,
                    )
    finally:
        for hooks in active_probes.values():
            for hook in hooks:
                hook.remove()
    last["initial_total"] = initial_total
    return last, optimizer


def checkpoint_bytes(
    model: Any, optimizer: Any, spec: ModelSpec, *, metadata: dict[str, Any] | None = None
) -> bytes:
    import io
    import random

    import numpy as np
    import torch

    numpy_state = np.random.get_state()
    rng_state = {
        "python": list(random.getstate()),
        "numpy": {
            "bit_generator": numpy_state[0],
            "keys": numpy_state[1].tolist(),
            "position": int(numpy_state[2]),
            "has_gauss": int(numpy_state[3]),
            "cached_gaussian": float(numpy_state[4]),
        },
        "torch_cpu": torch.random.get_rng_state(),
        "torch_cuda": [state.cpu() for state in torch.cuda.get_rng_state_all()]
        if torch.cuda.is_available()
        else [],
    }
    stream = io.BytesIO()
    torch.save(
        {
            "format": 1,
            "model_spec": asdict(spec),
            "model_state": model.state_dict(),
            "optimizer_state": optimizer.state_dict() if optimizer is not None else None,
            "rng_state": rng_state,
            "metadata": metadata or {},
        },
        stream,
    )
    return stream.getvalue()


def load_checkpoint(data: bytes):
    import io

    import torch

    checkpoint = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    spec = ModelSpec(**checkpoint["model_spec"])
    model = build_model(spec)
    model.load_state_dict(checkpoint["model_state"])
    model.eval()
    return model, spec, checkpoint


def load_training_checkpoint(data: bytes, *, lr: float = 0.03):
    """Restore model, optimizer and random generators for a deterministic continuation."""
    import torch

    model, spec, checkpoint = load_checkpoint(data)
    optimizer_state = checkpoint.get("optimizer_state")
    if optimizer_state is None:
        raise ValueError("checkpoint does not contain optimizer state")
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
    optimizer.load_state_dict(optimizer_state)
    model.train()
    restore_rng(checkpoint)
    return model, spec, optimizer, checkpoint


def restore_rng(checkpoint: dict[str, Any]) -> None:
    """Restore saved Python, NumPy, and PyTorch RNG states after a checkpoint load."""
    import random

    import numpy as np
    import torch

    state = checkpoint.get("rng_state")
    if not isinstance(state, dict):
        raise ValueError("checkpoint does not contain RNG state")  # noqa: TRY004
    python_state = state["python"]
    random.setstate((int(python_state[0]), tuple(python_state[1]), python_state[2]))
    numpy_state = state["numpy"]
    np.random.set_state(
        (
            numpy_state["bit_generator"],
            np.asarray(numpy_state["keys"], dtype=np.uint32),
            int(numpy_state["position"]),
            int(numpy_state["has_gauss"]),
            float(numpy_state["cached_gaussian"]),
        )
    )
    torch.random.set_rng_state(state["torch_cpu"])
    if torch.cuda.is_available() and state.get("torch_cuda"):
        torch.cuda.set_rng_state_all(state["torch_cuda"])


def inspect_model(model: Any, inputs: Any, module_path: str) -> dict[str, Any]:
    """Capture and validate selected operations with the optional TorchLens adapter."""
    import torchlens

    if not isinstance(module_path, str) or not module_path:
        raise ValueError("module_path must be a non-empty string")
    trace = torchlens.trace(model, inputs, save=torchlens.in_module(module_path))
    record = trace[module_path]
    activation = record.out.detach()
    # TorchLens 2.34.1 requires an explicit validation scope. The inspection
    # path captures a forward trace, so validate the same forward behavior.
    valid = torchlens.validate(model, inputs, scope="forward")
    return {
        "module_path": module_path,
        "shape": list(activation.shape),
        "mean": float(activation.mean()),
        "std": float(activation.std()),
        "operation_count": len(trace),
        "validated": bool(valid),
    }
