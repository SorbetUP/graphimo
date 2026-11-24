"""Public value types for Graphimo's explicit artifact persistence API."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Stable reference to an immutable artifact stored by Graphimo."""

    id: str
    kind: str


@dataclass(frozen=True, slots=True)
class ArtifactStats:
    """Storage usage across all manifests currently in the local store.

    ``logical_bytes`` counts every file reference in every artifact. The
    physical count measures unique content-addressed blobs. Their difference
    is the storage saved through file-level deduplication.
    """

    logical_bytes: int
    physical_stored_bytes: int
    deduplicated_bytes: int
    artifacts: int
    blobs: int


@dataclass(frozen=True, slots=True)
class GarbageCollectionReport:
    artifacts_removed: int
    blobs_removed: int
    bytes_reclaimed: int
