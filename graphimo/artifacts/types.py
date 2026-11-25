"""Public value objects for the Graphimo ArtifactStore."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    kind: str
