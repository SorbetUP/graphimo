"""Explicit artifact persistence for files, directories and byte payloads.

The default store lives at ``$GRAPHIMO_ARTIFACTS_DIR`` when set, otherwise
``~/.graphimo/artifacts``. Importing this module does not create the store;
the directory is initialized on the first operation. Applications and tests
may call :func:`configure_default_store` to provide an explicit location.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .local import LocalArtifactStore
from .types import ArtifactRef, ArtifactStats, GarbageCollectionReport

_default_store: LocalArtifactStore | None = None


def configure_default_store(root: str | os.PathLike[str]) -> LocalArtifactStore:
    """Set the process-wide store used by the module-level convenience API."""
    global _default_store
    _default_store = LocalArtifactStore(root)
    return _default_store


def _store() -> LocalArtifactStore:
    global _default_store
    if _default_store is None:
        root = os.environ.get("GRAPHIMO_ARTIFACTS_DIR")
        if root is None:
            root = Path.home() / ".graphimo" / "artifacts"
        _default_store = LocalArtifactStore(root)
    return _default_store


def put(
    path: str | os.PathLike[str],
    *,
    kind: str = "file",
    parent: ArtifactRef | str | None = None,
) -> ArtifactRef:
    return _store().put(path, kind=kind, parent=parent)


def put_bytes(
    data: bytes,
    *,
    kind: str = "blob",
    parent: ArtifactRef | str | None = None,
) -> ArtifactRef:
    return _store().put_bytes(data, kind=kind, parent=parent)


def materialize(artifact: ArtifactRef, destination: str | os.PathLike[str]) -> Path:
    return _store().materialize(artifact, destination)


def stats() -> ArtifactStats:
    return _store().stats()


def pin(artifact: ArtifactRef | str, *, owner: str) -> None:
    _store().pin(artifact, owner=owner)


def unpin(artifact: ArtifactRef | str, *, owner: str) -> None:
    _store().unpin(artifact, owner=owner)


def gc(*, referenced: Iterable[ArtifactRef | str] = ()) -> GarbageCollectionReport:
    return _store().gc(referenced=referenced)


__all__ = [
    "ArtifactRef",
    "ArtifactStats",
    "GarbageCollectionReport",
    "LocalArtifactStore",
    "configure_default_store",
    "put",
    "put_bytes",
    "materialize",
    "stats",
    "pin",
    "unpin",
    "gc",
]
