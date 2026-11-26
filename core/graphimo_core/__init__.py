"""Experimental Graphimo core prototype — LEGACY, not a production path.

DEPRECATED: the canonical runtime is the ``graphimo`` package (application,
service, marimo kernel bridge) plus the Zeron plugin's ``service_bridge.luau``.
This package is kept only because historical tests (``tests/test_core_vertical_slice.py``)
and ``addons/pytorch/graphimo_pytorch/vertical_demo.py`` exercise it. Do not add
new code here; see ``docs/production-architecture.md``.
"""

from .artifacts import ArtifactRef, LocalArtifactStore
from .ledger import Event, Run, WorkspaceGraph

__all__ = ["ArtifactRef", "Event", "LocalArtifactStore", "Run", "WorkspaceGraph"]
