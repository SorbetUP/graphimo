"""Versioned local Graphimo service protocol helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_BATCH_OPERATIONS = 128
_OPERATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ServiceProtocolError(ValueError):
    """An HTTP-level protocol failure with a stable public error code."""

    def __init__(self, code: str, message: str, *, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(message)


class BatchActionError(ValueError):
    """One failed semantic action in a batch plan."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def parse_json_object(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_REQUEST_BYTES:
        raise ServiceProtocolError(
            "request_too_large",
            f"request body exceeds the {MAX_REQUEST_BYTES}-byte limit",
            status=413,
        )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ServiceProtocolError(
            "invalid_json", f"request body is not valid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ServiceProtocolError("invalid_request", "request body must be a JSON object")
    return value


def encode_response(value: Any) -> bytes:
    try:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ServiceProtocolError(
            "invalid_response", "Graphimo service result is not JSON-safe", status=500
        ) from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise ServiceProtocolError(
            "response_too_large",
            f"response exceeds the {MAX_RESPONSE_BYTES}-byte limit",
            status=500,
        )
    return body


def execute_batch(
    execute: Callable[[str, dict[str, Any]], Any],
    operations: Any,
    *,
    continue_on_error: bool = False,
) -> dict[str, Any]:
    """Execute ordered app actions with references and a per-step journal."""
    if not isinstance(operations, list) or not operations:
        raise ServiceProtocolError("invalid_batch", "operations must be a non-empty array")
    if len(operations) > MAX_BATCH_OPERATIONS:
        raise ServiceProtocolError(
            "batch_too_large",
            f"batch contains more than {MAX_BATCH_OPERATIONS} operations",
        )
    if type(continue_on_error) is not bool:
        raise ServiceProtocolError("invalid_batch", "continue_on_error must be a boolean")

    journal: list[dict[str, Any]] = []
    successful: dict[str, Any] = {}
    seen_ids: set[str] = set()
    stopped_at: str | None = None
    for index, operation in enumerate(operations):
        operation_id = f"step_{index + 1}"
        try:
            if not isinstance(operation, dict):
                raise BatchActionError("invalid_operation", "operation must be an object")
            operation_id = operation.get("id")
            if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
                raise BatchActionError(
                    "invalid_operation_id", "operation id must be 1-128 letters, digits, '_' or '-'"
                )
            if operation_id in seen_ids:
                raise BatchActionError("duplicate_operation_id", "operation IDs must be unique")
            seen_ids.add(operation_id)
            action = operation.get("action")
            args = operation.get("args", {})
            if not isinstance(action, str) or not action:
                raise BatchActionError(
                    "invalid_operation", "operation action must be non-empty text"
                )
            if not isinstance(args, dict):
                raise BatchActionError("invalid_operation", "operation args must be an object")
            resolved_args = _resolve_references(args, successful)
            result = execute(action, resolved_args)
            encoded_result = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded_result) > MAX_RESPONSE_BYTES:
                raise BatchActionError(
                    "response_too_large", "operation result exceeds the response size limit"
                )
            successful[operation_id] = result
            journal.append({"id": operation_id, "action": action, "status": "ok", "result": result})
            if (
                len(json.dumps(journal, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                > MAX_RESPONSE_BYTES - 4096
            ):
                successful.pop(operation_id, None)
                journal[-1] = {
                    "id": operation_id,
                    "action": action,
                    "status": "error",
                    "error": {
                        "code": "batch_response_too_large",
                        "message": "action ran, but its result would exceed the batch response limit",
                    },
                }
                stopped_at = operation_id
                for later_index, later in enumerate(operations[index + 1 :], start=index + 2):
                    later_id = (
                        later.get("id")
                        if isinstance(later, dict) and isinstance(later.get("id"), str)
                        else f"step_{later_index}"
                    )
                    journal.append(
                        {
                            "id": later_id,
                            "action": later.get("action") if isinstance(later, dict) else None,
                            "status": "not_executed",
                        }
                    )
                break
        except Exception as exc:  # semantic actions are isolated into journal entries
            code = getattr(exc, "code", "graphimo_action_failed")
            message = (str(exc) or type(exc).__name__)[:2048]
            journal.append(
                {
                    "id": operation_id,
                    "action": operation.get("action") if isinstance(operation, dict) else None,
                    "status": "error",
                    "error": {"code": code, "message": message},
                }
            )
            stopped_at = operation_id
            if not continue_on_error:
                for later_index, later in enumerate(operations[index + 1 :], start=index + 2):
                    later_id = (
                        later.get("id")
                        if isinstance(later, dict) and isinstance(later.get("id"), str)
                        else f"step_{later_index}"
                    )
                    journal.append(
                        {
                            "id": later_id,
                            "action": later.get("action") if isinstance(later, dict) else None,
                            "status": "not_executed",
                        }
                    )
                break

    return {
        "operations": journal,
        "completed": all(item["status"] == "ok" for item in journal),
        "stopped_at": stopped_at,
    }


def _resolve_references(value: Any, previous: Mapping[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            reference = value["$ref"]
            if not isinstance(reference, str):
                raise BatchActionError("invalid_reference", "$ref must be text")
            parts = reference.split(".")
            if len(parts) < 2 or parts[0] not in previous or parts[1] != "result":
                raise BatchActionError(
                    "invalid_reference", f"reference does not name a prior result: {reference!r}"
                )
            resolved = previous[parts[0]]
            for part in parts[2:]:
                if isinstance(resolved, Mapping) and part in resolved:
                    resolved = resolved[part]
                elif isinstance(resolved, list) and part.isdecimal():
                    try:
                        resolved = resolved[int(part)]
                    except IndexError as exc:
                        raise BatchActionError(
                            "invalid_reference", f"reference index is out of range: {reference!r}"
                        ) from exc
                else:
                    raise BatchActionError(
                        "invalid_reference", f"reference path does not exist: {reference!r}"
                    )
            return resolved
        return {key: _resolve_references(item, previous) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_references(item, previous) for item in value]
    return value


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON numeric constant: {value}")
