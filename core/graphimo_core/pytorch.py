from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable


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
    import torch.nn as nn

    class MultiHeadMLP(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = nn.Sequential(nn.Linear(spec.input_size, spec.hidden_size), nn.ReLU())
            self.heads = nn.ModuleDict({name: nn.Linear(spec.hidden_size, size)
                                        for name, size in sorted(spec.head_sizes.items())})

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
    model: Any, x: Any, targets: dict[str, Any], *, epochs: int = 8, lr: float = 0.03,
    emit: Callable[[str, dict[str, Any], int | None, str | None], None] | None = None,
) -> tuple[dict[str, float], Any]:
    import torch
    import torch.nn.functional as F

    if set(targets) != set(model.heads):
        raise ValueError("targets must be supplied for every model head")
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
    last: dict[str, float] = {}
    with torch.no_grad():
        initial = model(x)
        initial_total = float(sum(F.mse_loss(initial[name], target)
                                  for name, target in targets.items()))

    def probe(_module, _inputs, output):
        if emit:
            detached = output.detach()
            emit("model_probe", {"mean": float(detached.mean()), "std": float(detached.std())}, None,
                 "backbone.1")

    handle = model.backbone[1].register_forward_hook(probe)
    try:
        for epoch in range(epochs):
            optimizer.zero_grad(set_to_none=True)
            predictions = model(x)
            losses = {name: F.mse_loss(predictions[name], target) for name, target in targets.items()}
            total = sum(losses.values())
            total.backward()
            grad_norm = float(torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=10.0))
            optimizer.step()
            last = {name: float(value.detach()) for name, value in losses.items()}
            last["total"] = float(total.detach())
            if emit:
                emit("metric", {"name": "train/loss", "value": last["total"]}, epoch, None)
                for name, value in losses.items():
                    emit("metric", {"name": f"train/{name}_loss", "value": float(value.detach())}, epoch, None)
                emit("metric", {"name": "train/grad_norm", "value": grad_norm}, epoch, None)
                emit("progress", {"fraction": (epoch + 1) / epochs}, epoch, None)
                if torch.cuda.is_available():
                    emit("resource", {"cuda_allocated_bytes": int(torch.cuda.memory_allocated())}, epoch, None)
    finally:
        handle.remove()
    last["initial_total"] = initial_total
    return last, optimizer


def checkpoint_bytes(model: Any, optimizer: Any, spec: ModelSpec, *, metadata: dict[str, Any] | None = None) -> bytes:
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
        "torch_cuda": [state.cpu() for state in torch.cuda.get_rng_state_all()] if torch.cuda.is_available() else [],
    }
    stream = io.BytesIO()
    torch.save({"format": 1, "model_spec": asdict(spec), "model_state": model.state_dict(),
                "optimizer_state": optimizer.state_dict() if optimizer is not None else None,
                "rng_state": rng_state, "metadata": metadata or {}}, stream)
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


def restore_rng(checkpoint: dict[str, Any]) -> None:
    """Restore saved Python, NumPy, and PyTorch RNG states after a checkpoint load."""
    import random
    import numpy as np
    import torch

    state = checkpoint.get("rng_state")
    if not isinstance(state, dict):
        raise ValueError("checkpoint does not contain RNG state")
    python_state = state["python"]
    random.setstate((int(python_state[0]), tuple(python_state[1]), python_state[2]))
    numpy_state = state["numpy"]
    np.random.set_state((numpy_state["bit_generator"], np.asarray(numpy_state["keys"], dtype=np.uint32),
                         int(numpy_state["position"]), int(numpy_state["has_gauss"]),
                         float(numpy_state["cached_gaussian"])))
    torch.random.set_rng_state(state["torch_cpu"])
    if torch.cuda.is_available() and state.get("torch_cuda"):
        torch.cuda.set_rng_state_all(state["torch_cuda"])


