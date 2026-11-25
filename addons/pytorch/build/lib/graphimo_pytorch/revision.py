"""Semantic content revision for PyTorch MetaBlock state."""

from __future__ import annotations

import hashlib

from .serialization import canonical_state_json
from .schema import PyTorchMetaBlockState


def graph_revision(state: PyTorchMetaBlockState) -> str:
    return hashlib.sha256(canonical_state_json(state).encode("utf-8")).hexdigest()
