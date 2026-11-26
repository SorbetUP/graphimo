"""Ordinary Graphimo Python-node helpers for PyTorch training.

This module never schedules or owns graph execution. It is a normal Python
component a user can call from a marimo/Graphimo node.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from graphimo import node


@node
def train_pytorch_model(
    model_def: Any,
    batches: Iterable[Any] | Callable[[], Iterable[Any]],
    epochs: int = 1,
    steps: int | None = None,
    lr: float = 3e-4,
    weight_decay: float = 0.01,
    device: str | None = None,
    loss_fn: Callable[[Any, Any], Any] | None = None,
    emit_grad_norm: bool = False,
    emit_cuda: bool = False,
    persist_checkpoint: bool = False,
) -> tuple[Any, dict[str, Any], Any]:
    """Train a PyTorch model definition and return model, bounded history, ArtifactRef.

    ``batches`` can be a DataLoader/re-iterable or a zero-argument provider that
    creates a fresh iterable for each epoch. Causal-LM batches may be token
    tensors, ``{"input_ids": tokens}``, or ``(tokens, labels)``. Other models
    should provide ``loss_fn(output, target)`` and batches as input/target pairs.
    Checkpoint creation is opt-in and uses the Graphimo ArtifactStore API.
    """
    import torch

    if type(epochs) is not int or epochs < 1:
        raise ValueError("epochs must be a positive integer")
    if steps is not None and (type(steps) is not int or steps < 1):
        raise ValueError("steps must be a positive integer or None")
    if type(lr) not in {int, float} or lr <= 0:
        raise ValueError("lr must be positive")
    if type(weight_decay) not in {int, float} or weight_decay < 0:
        raise ValueError("weight_decay cannot be negative")
    if type(emit_grad_norm) is not bool or type(emit_cuda) is not bool:
        raise TypeError("telemetry flags must be booleans")
    if type(persist_checkpoint) is not bool:
        raise TypeError("persist_checkpoint must be a boolean")
    if not callable(getattr(model_def, "instantiate", None)):
        raise TypeError("model_def must be a PyTorchModelDefinition")

    model = model_def.instantiate(device=device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(lr), weight_decay=float(weight_decay))
    history: list[dict[str, float | int]] = []
    checkpoint = None
    step = 0
    stop = False
    for epoch in range(epochs):
        epoch_batches = batches() if callable(batches) else batches
        seen = False
        for batch in epoch_batches:
            seen = True
            if isinstance(batch, Mapping):
                input_ids = batch.get("input_ids")
                inputs = batch.get("inputs", input_ids)
                targets = batch.get("targets", batch.get("labels", input_ids))
            elif isinstance(batch, (tuple, list)) and len(batch) == 2:
                inputs, targets = batch
                input_ids = inputs
            else:
                inputs = batch
                targets = batch
                input_ids = batch
            if inputs is None:
                raise ValueError("each batch must contain inputs or input_ids")
            if device is not None:
                inputs = _move_batch(inputs, device)
                targets = _move_batch(targets, device)
                input_ids = _move_batch(input_ids, device)
            optimizer.zero_grad(set_to_none=True)
            output = model(inputs)
            if loss_fn is not None:
                loss = loss_fn(output, targets)
            elif input_ids is not None and callable(getattr(model, "loss", None)):
                logits = output
                if isinstance(logits, Mapping):
                    logits = logits.get("logits", next(iter(logits.values())))
                loss = model.loss(logits, input_ids)
            else:
                raise ValueError("provide loss_fn for models without a loss(logits, input_ids) method")
            if not isinstance(loss, torch.Tensor) or loss.numel() != 1:
                raise ValueError("training loss must be a scalar PyTorch tensor")
            loss.backward()
            grad_norm = None
            if emit_grad_norm:
                norms = [parameter.grad.detach().norm(2) for parameter in model.parameters() if parameter.grad is not None]
                if norms:
                    grad_norm = torch.stack(norms).norm(2)
            optimizer.step()
            scalar_loss = float(loss.detach().item())
            history.append({"step": step, "epoch": epoch, "loss": scalar_loss})
            if len(history) > 5000:
                history = history[::2][-2500:]
            from .runtime import emit_cuda_resources, emit_metric

            emit_metric(model, "train/loss", scalar_loss, step=step)
            if grad_norm is not None:
                emit_metric(model, "grad_norm", grad_norm, step=step)
            if emit_cuda:
                emit_cuda_resources(model, step=step)
            step += 1
            if steps is not None and step >= steps:
                stop = True
                break
        if not seen:
            raise ValueError(f"batch provider produced no batches for epoch {epoch}")
        if stop:
            break
    if persist_checkpoint:
        from .checkpoint import save_checkpoint

        checkpoint = save_checkpoint(
            model,
            optimizer=optimizer,
            metadata={"training_steps": step, "epochs_requested": epochs},
        )
    return model, {"steps": step, "history": history}, checkpoint


def _move_batch(value: Any, device: str) -> Any:
    import torch

    if isinstance(value, torch.Tensor):
        return value.to(device)
    if isinstance(value, Mapping):
        return {key: _move_batch(item, device) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_move_batch(item, device) for item in value)
    if isinstance(value, list):
        return [_move_batch(item, device) for item in value]
    return value
