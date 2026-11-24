"""Minimal optional MetaBlock addon discovery seam."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import Callable, Protocol


class ExperimentalGraphimoAPI(Protocol):
    """Opaque placeholder for the not-yet-frozen experimental host API."""


@dataclass(frozen=True, slots=True)
class MetaDialectRegistration:
    dialect_id: str
    addon_version: str
    api_version: int
    factory: Callable[[ExperimentalGraphimoAPI], object]

    def __post_init__(self) -> None:
        if not self.dialect_id or not self.addon_version:
            raise ValueError("MetaDialectRegistration requires dialect and addon version")
        if type(self.api_version) is not int or self.api_version < 1:
            raise ValueError("api_version must be a positive integer")
        if not callable(self.factory):
            raise TypeError("factory must be callable")


def discover_metablock_addons() -> tuple[EntryPoint, ...]:
    """Return installed entry points without importing or initializing addons.

    Addons register in the ``graphimo.metablocks`` entry-point group. Core
    intentionally does not load factories until the stable handler API is
    specified.
    """
    return tuple(entry_points(group="graphimo.metablocks"))


__all__ = ["ExperimentalGraphimoAPI", "MetaDialectRegistration", "discover_metablock_addons"]
