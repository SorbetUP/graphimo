"""Bounded JSON protocol primitives shared by Graphimo service and client."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 4_000_000
MAX_BATCH_OPERATIONS = 256
MAX_EVENT_LIMIT = 256
MAX_EVENT_WAIT_MS = 30_000
MAX_EVENT_BYTES = 128_000


def jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "items"):
        return {str(key): jsonable(item) for key, item in value.items()}
    raise TypeError(f"Value of type {type(value).__name__} is not JSON-compatible")


def error_payload(code: str, message: str, *, details: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": {"code": str(code), "message": str(message)}}
    if details is not None:
        payload["error"]["details"] = jsonable(details)
    return payload
