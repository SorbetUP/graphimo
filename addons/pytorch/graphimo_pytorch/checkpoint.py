"""Explicit PyTorch checkpoint persistence through Graphimo ArtifactStore."""

from __future__ import annotations

import importlib.metadata
import json
import math
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from graphimo.artifacts import ArtifactRef

from .migration import WeightMigrationReport, migrate_state_dict
from .runtime import RUNTIME_REGISTRY, _runtime_info

CHECKPOINT_KIND = "pytorch-checkpoint"
CHECKPOINT_FORMAT = 1


class CheckpointError(ValueError):
    """Checkpoint failures expose stable addon diagnostic codes."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def save_checkpoint(
    model: Any,
    *,
    optimizer: Any | None = None,
    scheduler: object | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ArtifactRef:
    """Persist model state and provenance; never serialize the model object."""
    import torch
    from graphimo import artifacts

    if not isinstance(model, torch.nn.Module):
        raise TypeError("model must be a torch.nn.Module")
    info = _runtime_info(model)
    if info is None or RUNTIME_REGISTRY.info(info.runtime_instance_id) is None:
        raise CheckpointError(
            "pytorch_checkpoint_runtime_missing", "model has no active Graphimo runtime metadata"
        )
    if optimizer is not None and not isinstance(optimizer, torch.optim.Optimizer):
        raise TypeError("optimizer must be a torch.optim.Optimizer")
    if scheduler is not None and not callable(getattr(scheduler, "state_dict", None)):
        raise TypeError("scheduler must provide state_dict()")
    user_metadata = _json_mapping(metadata or {}, "metadata")
    optimizer_state = optimizer.state_dict() if optimizer is not None else None
    scheduler_state = scheduler.state_dict() if scheduler is not None else None
    _validate_torch_tree(optimizer_state, "optimizer_state")
    _validate_torch_tree(scheduler_state, "scheduler_state")
    model_state = model.state_dict()
    _validate_model_state(model_state)
    envelope = {
        "format": CHECKPOINT_FORMAT,
        "kind": CHECKPOINT_KIND,
        "model_state": model_state,
        "provenance": {
            "metablock_id": info.metablock_id,
            "graph_revision": info.graph_revision,
            "materialization_key": info.materialization_key,
            "model_name": getattr(info, "model_name", ""),
            "external_parameters": _json_mapping(
                getattr(info, "external_parameters", {}), "external_parameters"
            ),
            "pytorch_version": str(torch.__version__),
            "addon_version": _addon_version(),
            "runtime_instance_id": info.runtime_instance_id,
        },
        "optimizer_state": optimizer_state,
        "scheduler_state": scheduler_state,
        "metadata": user_metadata,
    }
    with tempfile.TemporaryDirectory(prefix="graphimo-pytorch-checkpoint-") as directory:
        path = Path(directory) / "checkpoint.pt"
        torch.save(envelope, path)
        return artifacts.put(path, kind=CHECKPOINT_KIND)


def load_checkpoint(
    model_def: Any,
    artifact: ArtifactRef | str,
    *,
    device: str | Any | None = None,
    dtype: Any | None = None,
    strict: bool = True,
) -> Any:
    """Instantiate a definition and restore an exact revision or report migration."""
    import torch
    from graphimo import artifacts

    if type(strict) is not bool:
        raise TypeError("strict must be a boolean")
    if not callable(getattr(model_def, "instantiate", None)):
        raise TypeError("model_def must be a PyTorchModelDefinition")
    factory = getattr(model_def, "_factory", None)
    if not callable(factory):
        raise TypeError("model_def must be a PyTorchModelDefinition")
    stored_ref = artifacts.get(artifact)
    if stored_ref.kind != CHECKPOINT_KIND:
        raise CheckpointError(
            "pytorch_checkpoint_artifact_kind_invalid",
            f"artifact kind must be {CHECKPOINT_KIND!r}",
        )
    with tempfile.TemporaryDirectory(prefix="graphimo-pytorch-checkpoint-load-") as directory:
        path = Path(directory) / "checkpoint.pt"
        artifacts.materialize(stored_ref, path)
        try:
            envelope = torch.load(path, map_location="cpu", weights_only=True)
        except Exception as exc:  # noqa: BLE001 - convert corrupt/unsafe archive to addon error
            raise CheckpointError(
                "pytorch_checkpoint_invalid", f"could not load checkpoint: {exc}"
            ) from exc
    _validate_envelope(envelope)
    provenance = envelope["provenance"]
    target_revision = getattr(model_def, "graph_revision", None)
    target_materialization_key = getattr(model_def, "materialization_key", None)
    revision_match = provenance["graph_revision"] == target_revision
    if not revision_match and strict:
        raise CheckpointError(
            "pytorch_checkpoint_revision_mismatch",
            "checkpoint graph revision does not match the current PyTorch model definition",
        )

    if strict:
        shape_probe = factory()
        state_matches = _state_dict_shapes_match(envelope["model_state"], shape_probe.state_dict())
        del shape_probe
        if not state_matches:
            code = (
                "pytorch_checkpoint_materialization_mismatch"
                if provenance["materialization_key"] != target_materialization_key
                else "pytorch_checkpoint_state_incompatible"
            )
            raise CheckpointError(
                code,
                "checkpoint tensor names or shapes do not match the current model definition",
            )
        model = model_def.instantiate(device=device, dtype=dtype)
        try:
            model.load_state_dict(envelope["model_state"], strict=True)
        except RuntimeError as exc:
            raise CheckpointError(
                "pytorch_checkpoint_state_incompatible",
                f"checkpoint tensors do not match the graph revision: {exc}",
            ) from exc
    else:
        model = model_def.instantiate(device=device, dtype=dtype)
        state_matches = _state_dict_shapes_match(envelope["model_state"], model.state_dict())
        if revision_match and state_matches:
            model.load_state_dict(envelope["model_state"], strict=True)
        else:
            report = migrate_state_dict(envelope["model_state"], model)
            model.__graphimo_pytorch_migration_report__ = report
            info = _runtime_info(model)
            if info is not None:
                RUNTIME_REGISTRY.event_bus.emit(
                    kind="runtime.warning",
                    info=info,
                    payload={"code": "pytorch_checkpoint_migrated", "report": _report_json(report)},
                )
    model.__graphimo_pytorch_checkpoint__ = {
        "artifact_id": stored_ref.id,
        "provenance": provenance,
        "metadata": envelope["metadata"],
        "optimizer_state_present": envelope["optimizer_state"] is not None,
        "scheduler_state_present": envelope["scheduler_state"] is not None,
    }
    return model


def get_migration_report(model: Any) -> WeightMigrationReport | None:
    """Return the report produced by an explicit ``strict=False`` load."""
    return getattr(model, "__graphimo_pytorch_migration_report__", None)


def preview_checkpoint_migration(
    model_def: Any, artifact: ArtifactRef | str
) -> WeightMigrationReport:
    """Compare a checkpoint to a fresh target instance without changing either one."""
    import torch
    from graphimo import artifacts

    stored_ref = artifacts.get(artifact)
    if stored_ref.kind != CHECKPOINT_KIND:
        raise CheckpointError(
            "pytorch_checkpoint_artifact_kind_invalid",
            f"artifact kind must be {CHECKPOINT_KIND!r}",
        )
    with tempfile.TemporaryDirectory(prefix="graphimo-pytorch-checkpoint-preview-") as directory:
        path = Path(directory) / "checkpoint.pt"
        artifacts.materialize(stored_ref, path)
        try:
            envelope = torch.load(path, map_location="cpu", weights_only=True)
        except Exception as exc:  # noqa: BLE001 - convert corrupt archive to addon error
            raise CheckpointError(
                "pytorch_checkpoint_invalid", f"could not load checkpoint: {exc}"
            ) from exc
    _validate_envelope(envelope)
    factory = getattr(model_def, "_factory", None)
    if not callable(factory):
        raise TypeError("model_def must be a PyTorchModelDefinition")
    target_model = factory()
    return migrate_state_dict(envelope["model_state"], target_model)


def inspect_checkpoint(artifact: ArtifactRef | str) -> dict[str, Any]:
    """Read bounded provenance without constructing a model or optimizer."""
    import torch
    from graphimo import artifacts

    stored_ref = artifacts.get(artifact)
    if stored_ref.kind != CHECKPOINT_KIND:
        raise CheckpointError(
            "pytorch_checkpoint_artifact_kind_invalid",
            f"artifact kind must be {CHECKPOINT_KIND!r}",
        )
    with tempfile.TemporaryDirectory(prefix="graphimo-pytorch-checkpoint-inspect-") as directory:
        path = Path(directory) / "checkpoint.pt"
        artifacts.materialize(stored_ref, path)
        try:
            envelope = torch.load(path, map_location="meta", weights_only=True, mmap=True)
        except Exception as exc:  # noqa: BLE001 - artifact may be truncated or from a newer format
            raise CheckpointError(
                "pytorch_checkpoint_invalid", f"could not load checkpoint: {exc}"
            ) from exc
    _validate_envelope(envelope)
    return {
        "artifact_id": stored_ref.id,
        "artifact_kind": stored_ref.kind,
        "metablock_id": envelope["provenance"]["metablock_id"],
        "graph_revision": envelope["provenance"]["graph_revision"],
        "materialization_key": envelope["provenance"]["materialization_key"],
        "model_name": envelope["provenance"]["model_name"],
        "pytorch_version": envelope["provenance"]["pytorch_version"],
        "addon_version": envelope["provenance"]["addon_version"],
        "metadata": envelope["metadata"],
        "optimizer_state_present": envelope["optimizer_state"] is not None,
        "scheduler_state_present": envelope["scheduler_state"] is not None,
    }


def _validate_envelope(value: Any) -> None:
    if not isinstance(value, dict) or value.get("format") != CHECKPOINT_FORMAT:
        raise CheckpointError("pytorch_checkpoint_invalid", "unsupported checkpoint envelope")
    if value.get("kind") != CHECKPOINT_KIND:
        raise CheckpointError("pytorch_checkpoint_invalid", "checkpoint kind is invalid")
    provenance = value.get("provenance")
    required = {
        "metablock_id",
        "graph_revision",
        "materialization_key",
        "external_parameters",
        "pytorch_version",
        "addon_version",
        "runtime_instance_id",
        "model_name",
    }
    if not isinstance(provenance, dict) or not required.issubset(provenance):
        raise CheckpointError("pytorch_checkpoint_invalid", "checkpoint provenance is incomplete")
    for key in required - {"external_parameters"}:
        if not isinstance(provenance[key], str):
            raise CheckpointError(
                "pytorch_checkpoint_invalid", f"checkpoint provenance {key!r} is invalid"
            )
    if not isinstance(value.get("model_state"), Mapping):
        raise CheckpointError("pytorch_checkpoint_invalid", "checkpoint model state is invalid")
    try:
        _validate_model_state(value["model_state"])
        _validate_torch_tree(value.get("optimizer_state"), "optimizer_state")
        _validate_torch_tree(value.get("scheduler_state"), "scheduler_state")
    except (TypeError, ValueError) as exc:
        raise CheckpointError("pytorch_checkpoint_invalid", str(exc)) from exc
    try:
        _json_mapping(value.get("metadata"), "metadata")
        _json_mapping(provenance.get("external_parameters"), "external_parameters")
    except TypeError as exc:
        raise CheckpointError("pytorch_checkpoint_invalid", str(exc)) from exc


def _validate_model_state(state: Mapping[str, Any]) -> None:
    import torch

    for name, value in state.items():
        if not isinstance(name, str) or not isinstance(value, torch.Tensor):
            raise TypeError("model_state must map string names to torch.Tensor values")


def _state_dict_shapes_match(source: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    if source.keys() != target.keys():
        return False
    return all(tuple(source[name].shape) == tuple(target[name].shape) for name in source)


def _json_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, allow_nan=False)
        normalized = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must contain only finite JSON values") from exc
    if not isinstance(normalized, dict):
        raise TypeError(f"{label} must be a JSON object")
    return normalized


def _validate_torch_tree(value: Any, label: str, depth: int = 0) -> None:
    import torch

    if depth > 32:
        raise TypeError(f"{label} exceeds the checkpoint nesting limit")
    if value is None or isinstance(value, torch.Tensor | str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{label} contains a non-finite number")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str | int | float | bool):
                raise TypeError(f"{label} contains an unsupported mapping key")
            _validate_torch_tree(item, label, depth + 1)
        return
    if isinstance(value, tuple | list):
        for item in value:
            _validate_torch_tree(item, label, depth + 1)
        return
    raise TypeError(f"{label} contains unsupported value {type(value).__name__}")


def _addon_version() -> str:
    try:
        return importlib.metadata.version("graphimo-pytorch")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def _report_json(report: WeightMigrationReport) -> dict[str, Any]:
    return {
        "copied": list(report.copied),
        "missing_in_source": list(report.missing_in_source),
        "missing_in_target": list(report.missing_in_target),
        "shape_mismatch": [
            {
                "name": item.name,
                "source_shape": list(item.source_shape),
                "target_shape": list(item.target_shape),
            }
            for item in report.shape_mismatch
        ],
    }
