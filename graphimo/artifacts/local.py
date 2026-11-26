"""Public local ArtifactStore API backed by Graphimo's streaming CAS."""

from __future__ import annotations

import os
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from .store import ArtifactRef as _StoredArtifactRef
from .store import LocalArtifactStore

from .types import ArtifactRef


def _default_root() -> Path:
    configured = os.environ.get("GRAPHIMO_ARTIFACT_STORE")
    if configured:
        return Path(configured).expanduser()
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "graphimo" / "artifacts"


@lru_cache(maxsize=8)
def _store_at(root: str) -> LocalArtifactStore:
    return LocalArtifactStore(root)


def _store() -> LocalArtifactStore:
    return _store_at(str(_default_root().resolve()))


def _artifact_id(artifact: ArtifactRef | _StoredArtifactRef | str) -> str:
    if isinstance(artifact, str):
        return artifact
    if isinstance(artifact, ArtifactRef):
        return artifact.id
    if isinstance(artifact, _StoredArtifactRef):
        return artifact.artifact_id
    raise TypeError("artifact must be an ArtifactRef or artifact ID")


def _public_ref(ref: _StoredArtifactRef) -> ArtifactRef:
    return ArtifactRef(id=ref.artifact_id, kind=ref.kind)


def put(
    path: str | os.PathLike[str], *, kind: str = "file", parent_id: ArtifactRef | str | None = None
) -> ArtifactRef:
    """Store a file or directory by content; source files are streamed."""
    source = Path(path)
    parent = _artifact_id(parent_id) if parent_id is not None else None
    if source.is_dir() and not source.is_symlink():
        return _public_ref(_store().put_directory(source, kind=kind, parent_id=parent))
    return _public_ref(_store().put_file(source, kind=kind, parent_id=parent))


def put_bytes(
    data: bytes, *, kind: str = "blob", parent_id: ArtifactRef | str | None = None
) -> ArtifactRef:
    parent = _artifact_id(parent_id) if parent_id is not None else None
    return _public_ref(_store().put_bytes(data, kind=kind, parent_id=parent))


def get(artifact: ArtifactRef | str) -> ArtifactRef:
    return _public_ref(_store().get(_artifact_id(artifact)))


def materialize(artifact: ArtifactRef | str, destination: str | os.PathLike[str]) -> Path:
    target = Path(destination)
    _store().materialize(_store().get(_artifact_id(artifact)), target)
    return target


def stats() -> dict[str, int]:
    return _store().stats()


def pin(artifact: ArtifactRef | str, *, owner: str) -> None:
    _store().pin(_artifact_id(artifact), owner)


def unpin(artifact: ArtifactRef | str, *, owner: str) -> None:
    _store().unpin(_artifact_id(artifact), owner)


def gc(*, referenced: Iterable[ArtifactRef | str] = ()) -> dict[str, int]:
    referenced_ids = {_artifact_id(artifact) for artifact in referenced}
    return _store().gc(referenced_ids)
