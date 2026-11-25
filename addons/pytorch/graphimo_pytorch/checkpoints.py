"""Explicit PyTorch state-dict checkpoints stored as Graphimo Artifacts."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from graphimo.artifacts import ArtifactRef, LocalArtifactStore, materialize, put

from .definition import PyTorchModelDefinition
from .diagnostics import GraphimoPytorchError
from .runtime import RuntimeModelInfo
from .schema import JsonValue
from .version import __version__


@dataclass(frozen=True, slots=True)
class ShapeMismatch:
    name: str
    source_shape: tuple[int, ...]
    target_shape: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WeightMigrationReport:
    copied: tuple[str, ...]
    missing_in_source: tuple[str, ...]
    missing_in_target: tuple[str, ...]
    shape_mismatch: tuple[ShapeMismatch, ...]


def save_checkpoint(
    model: object,
    *,
    optimizer: object | None = None,
    scheduler: object | None = None,
    metadata: Mapping[str, JsonValue] | None = None,
    store: LocalArtifactStore | None = None,
) -> ArtifactRef:
    """Persist model state (never a pickled model object) through ArtifactStore."""
    import torch

    runtime = getattr(model, "__graphimo_pytorch_runtime__", None)
    if not isinstance(runtime, RuntimeModelInfo):
        raise GraphimoPytorchError("pytorch_runtime_not_registered", "Checkpoint model has no Graphimo-PyTorch provenance")
    if not isinstance(metadata or {}, Mapping):
        raise GraphimoPytorchError("pytorch_checkpoint_metadata_invalid", "Checkpoint metadata must be a mapping")
    user_metadata = dict(metadata or {})
    try:
        json.dumps(user_metadata, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise GraphimoPytorchError("pytorch_checkpoint_metadata_invalid", "Checkpoint metadata must contain finite JSON values") from error

    optimizer_state = None
    if optimizer is not None:
        state_dict = getattr(optimizer, "state_dict", None)
        if not callable(state_dict):
            raise GraphimoPytorchError("pytorch_optimizer_unsupported", "Optimizer must provide state_dict()")
        optimizer_state = state_dict()
    scheduler_state = None
    if scheduler is not None:
        state_dict = getattr(scheduler, "state_dict", None)
        if not callable(state_dict):
            raise GraphimoPytorchError("pytorch_scheduler_unsupported", "Scheduler must provide state_dict()")
        scheduler_state = state_dict()

    payload = {
        "format": 1,
        "model_state_dict": model.state_dict(),
        "metablock_id": runtime.metablock_id,
        "graph_revision": runtime.graph_revision,
        "materialization_key": runtime.materialization_key,
        "external_parameters": dict(getattr(model, "__graphimo_pytorch_external_parameters__", {})),
        "pytorch_version": str(torch.__version__),
        "addon_version": __version__,
        "optimizer_state_dict": optimizer_state,
        "scheduler_state_dict": scheduler_state,
        "metadata": user_metadata,
    }
    with tempfile.TemporaryDirectory(prefix="graphimo-pytorch-checkpoint-") as folder:
        path = Path(folder) / "checkpoint.pt"
        torch.save(payload, path)
        return store.put(path, kind="pytorch-checkpoint") if store is not None else put(path, kind="pytorch-checkpoint")


def load_checkpoint(
    model_def: PyTorchModelDefinition,
    artifact: ArtifactRef,
    *,
    device: str | object | None = None,
    dtype: object | None = None,
    strict: bool = True,
    store: LocalArtifactStore | None = None,
) -> object:
    """Load exact-compatible weights, or explicitly migrate when strict=False."""
    import torch

    if artifact.kind != "pytorch-checkpoint":
        raise GraphimoPytorchError("pytorch_checkpoint_kind_invalid", f"Expected pytorch-checkpoint, got {artifact.kind!r}")
    with tempfile.TemporaryDirectory(prefix="graphimo-pytorch-checkpoint-load-") as folder:
        destination = Path(folder) / "checkpoint.pt"
        path = store.materialize(artifact, destination) if store is not None else materialize(artifact, destination)
        try:
            payload = torch.load(path, map_location=device or "cpu", weights_only=True)
        except Exception as error:
            raise GraphimoPytorchError("pytorch_checkpoint_invalid", f"Unable to load checkpoint: {type(error).__name__}: {error}") from error
    if not isinstance(payload, dict) or payload.get("format") != 1 or not isinstance(payload.get("model_state_dict"), dict):
        raise GraphimoPytorchError("pytorch_checkpoint_invalid", "Unsupported or malformed checkpoint payload")
    if strict and payload.get("graph_revision") != model_def.graph_revision:
        raise GraphimoPytorchError(
            "pytorch_checkpoint_revision_mismatch",
            f"Checkpoint revision {payload.get('graph_revision')!r} does not match current revision {model_def.graph_revision!r}",
        )
    if strict and payload.get("materialization_key") != model_def.materialization_key:
        raise GraphimoPytorchError(
            "pytorch_checkpoint_materialization_mismatch",
            "Checkpoint external parameters/compiler materialization do not match the current model definition",
        )
    model = model_def.instantiate(device=device, dtype=dtype)
    try:
        if strict:
            model.load_state_dict(payload["model_state_dict"], strict=True)
        else:
            report = migrate_state_dict(payload["model_state_dict"], model)
            setattr(model, "__graphimo_pytorch_migration_report__", report)
    except Exception as error:
        from .runtime import release_runtime_model

        release_runtime_model(model)
        code = "pytorch_checkpoint_load_failed"
        raise GraphimoPytorchError(code, f"Checkpoint weights could not be loaded: {type(error).__name__}: {error}") from error
    return model


def migrate_state_dict(source_state: Mapping[str, object], target_model: object) -> WeightMigrationReport:
    """Copy only same-name, same-shape tensors; never reshape or interpolate."""
    import torch

    target_state = target_model.state_dict()
    source_keys = set(source_state)
    target_keys = set(target_state)
    common = source_keys & target_keys
    copied: list[str] = []
    mismatches: list[ShapeMismatch] = []
    with torch.no_grad():
        for name in sorted(common):
            source = source_state[name]
            target = target_state[name]
            if not isinstance(source, torch.Tensor) or not isinstance(target, torch.Tensor):
                continue
            source_shape = tuple(int(value) for value in source.shape)
            target_shape = tuple(int(value) for value in target.shape)
            if source_shape != target_shape:
                mismatches.append(ShapeMismatch(name, source_shape, target_shape))
                continue
            target.copy_(source.to(device=target.device, dtype=target.dtype))
            copied.append(name)
    return WeightMigrationReport(
        tuple(copied), tuple(sorted(target_keys - source_keys)), tuple(sorted(source_keys - target_keys)), tuple(mismatches)
    )

