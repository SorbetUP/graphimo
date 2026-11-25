"""Entry-point registration for the optional PyTorch MetaBlock addon."""

from __future__ import annotations

from dataclasses import dataclass

from graphimo.metablocks import ExperimentalGraphimoAPI, MetaDialectRegistration

from .version import __version__


@dataclass(frozen=True, slots=True)
class _ExperimentalDialect:
    """Private P1 marker object; the stable host handler API is not frozen."""

    api: ExperimentalGraphimoAPI


def create_pytorch_dialect(api: ExperimentalGraphimoAPI) -> object:
    return _ExperimentalDialect(api)


registration = MetaDialectRegistration(
    dialect_id="pytorch.model",
    addon_version=__version__,
    api_version=1,
    factory=create_pytorch_dialect,
)
