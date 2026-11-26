"""One-shot stdio entry point for local host adapters such as the Zeron plugin."""

from __future__ import annotations

import json
import sys
from typing import Any

from graphimo.client import GraphimoClient, GraphimoServiceError
from graphimo.service.protocol import MAX_REQUEST_BYTES, parse_json_object


def dispatch(request: dict[str, Any]) -> Any:
    path = request.get("path")
    operation = request.get("operation")
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty project path")
    if operation not in {"execute", "batch", "events"}:
        raise ValueError("operation must be execute, batch or events")
    client = GraphimoClient.connect_local()
    project = client.open_project(path)
    if operation == "execute":
        action = request.get("action")
        args = request.get("args", {})
        if not isinstance(action, str) or not isinstance(args, dict):
            raise ValueError("execute requires a non-empty action and object args")
        return project.execute(action, args)
    if operation == "batch":
        operations = request.get("operations")
        continue_on_error = request.get("continue_on_error", False)
        if not isinstance(operations, list) or type(continue_on_error) is not bool:
            raise ValueError("batch requires operations and a boolean continue_on_error")
        return project.batch(operations, continue_on_error=continue_on_error)
    cursor = request.get("cursor")
    limit = request.get("limit", 128)
    wait_ms = request.get("wait_ms", 750)
    if cursor is not None and not isinstance(cursor, (str, int)):
        raise ValueError("event cursor must be text or an integer")
    if type(limit) is not int or type(wait_ms) is not int:
        raise ValueError("event limit and wait_ms must be integers")
    return project.events(cursor=cursor, limit=limit, wait_ms=wait_ms)


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    try:
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError(f"adapter request exceeds the {MAX_REQUEST_BYTES}-byte limit")
        request = parse_json_object(raw)
        response = {"ok": True, "result": dispatch(request)}
    except GraphimoServiceError as exc:
        response = {
            "ok": False,
            "error": {"code": exc.code, "message": str(exc), "status": exc.status},
        }
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        response = {"ok": False, "error": {"code": "invalid_request", "message": str(exc)}}
    except Exception as exc:
        response = {
            "ok": False,
            "error": {
                "code": "adapter_error",
                "message": f"Graphimo adapter request failed: {exc}",
            },
        }
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
