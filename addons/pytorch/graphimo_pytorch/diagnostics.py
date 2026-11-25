"""Structured diagnostics shared by PyTorch addon layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "error"
    element_id: str | None = None


class GraphimoPytorchError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

