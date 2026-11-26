"""Conservative, exact-name/exact-shape PyTorch weight migration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ShapeMismatch:
    name: str
    source_shape: tuple[int, ...]
    target_shape: tuple[int, ...]


@dataclass(frozen=True)
class WeightMigrationReport:
    copied: tuple[str, ...]
    missing_in_source: tuple[str, ...]
    missing_in_target: tuple[str, ...]
    shape_mismatch: tuple[ShapeMismatch, ...]


def migrate_state_dict(source_state: Mapping[str, Any], target_model: Any) -> WeightMigrationReport:
    """Copy only tensors with identical names and shapes; never resize weights."""
    import torch

    if not isinstance(source_state, Mapping):
        raise TypeError("source_state must be a mapping of tensor names to tensors")
    if not callable(getattr(target_model, "state_dict", None)):
        raise TypeError("target_model must provide state_dict()")
    target_state = target_model.state_dict()
    normalized_source: dict[str, Any] = {}
    for name, tensor in source_state.items():
        if not isinstance(name, str) or not name:
            raise ValueError("state_dict keys must be non-empty strings")
        if not isinstance(tensor, torch.Tensor):
            raise TypeError(f"source state value {name!r} is not a torch.Tensor")
        normalized_source[name] = tensor

    copied: list[str] = []
    mismatches: list[ShapeMismatch] = []
    with torch.no_grad():
        for name, target in target_state.items():
            source = normalized_source.get(name)
            if source is None:
                continue
            if not isinstance(target, torch.Tensor):
                continue
            if tuple(source.shape) != tuple(target.shape):
                mismatches.append(ShapeMismatch(name, tuple(source.shape), tuple(target.shape)))
                continue
            target.copy_(source.detach().to(device=target.device, dtype=target.dtype))
            copied.append(name)

    source_names = set(normalized_source)
    target_names = set(target_state)
    mismatch_names = {item.name for item in mismatches}
    return WeightMigrationReport(
        copied=tuple(sorted(copied)),
        missing_in_source=tuple(sorted(target_names - source_names - mismatch_names)),
        missing_in_target=tuple(sorted(source_names - target_names - mismatch_names)),
        shape_mismatch=tuple(sorted(mismatches, key=lambda item: item.name)),
    )
