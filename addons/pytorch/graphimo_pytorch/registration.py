"""Entry-point registration for the optional PyTorch MetaBlock addon."""

from __future__ import annotations

from graphimo.metablocks import MetaBlockContext, MetaDialectRegistration

from .handler import PyTorchMetaBlockDialect
from .version import __version__


def create_pytorch_dialect(api: MetaBlockContext) -> PyTorchMetaBlockDialect:
    return PyTorchMetaBlockDialect(api)


registration = MetaDialectRegistration(
    dialect_id="pytorch.model",
    addon_version=__version__,
    api_version=1,
    factory=create_pytorch_dialect,
)
