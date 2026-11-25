"""Loopback-only persistent HTTP service for GraphimoApplication."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from graphimo.application import ApplicationError

from .protocol import (
    MAX_EVENT_LIMIT,
    MAX_EVENT_WAIT_MS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    error_payload,
    jsonable,
)
from .sessions import ProjectSessionManager


class GraphimoService:
    """Persistent service object; its HTTP thread and project sessions live together."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        token: str | None = None,
        sessions: ProjectSessionManager | None = None,
        discovery_file: str | Path | None = None,
    ) -> None:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Graphimo service host must be a loopback IP address") from exc
        if not address.is_loopback or address.version != 4:
            raise ValueError("Graphimo service may bind only to the IPv4 loopback address")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Graphimo service port must be between 0 and 65535")
        self.host = str(address)
        self.token = token or secrets.token_urlsafe(32)
        if not isinstance(self.token, str) or len(self.token) < 32:
            raise ValueError("Graphimo service token must contain at least 32 characters")
        self.sessions = sessions or ProjectSessionManager()
        self.discovery_file = Path(discovery_file).expanduser() if discovery_file else None
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._server = self._make_server(port)
        self.port = int(self._server.server_address[1])
        self.base_url = f"http://{self.host}:{self.port}"

    def _make_server(self, port: int) -> ThreadingHTTPServer:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                path = urlsplit(self.path)
                if path.path == "/health":
                    self._send(200, {"ok": True, "service": "graphimo"})
                    return
                if not owner._authorized(
                    self.headers.get("Authorization"), self.headers.get("X-Graphimo-Token")
                ):
                    self._send(
                        401,
                        error_payload("unauthorized", "A valid Graphimo service token is required"),
                    )
                    return
                if path.path != "/v1/events":
                    self._send(404, error_payload("not_found", "Service endpoint was not found"))
                    return
                try:
                    query = parse_qs(path.query, strict_parsing=True)
                    project_id = _one(query, "project")
                    cursor = _integer(query, "cursor", 0)
                    limit = _integer(query, "limit", 100)
                    wait_ms = _integer(query, "wait_ms", 0)
                    if not 1 <= limit <= MAX_EVENT_LIMIT or not 0 <= wait_ms <= MAX_EVENT_WAIT_MS:
                        raise ApplicationError(
                            "invalid_event_request", "Event limit or wait is outside its bound"
                        )
                    session = owner.sessions.get(project_id)
                    deadline = time.monotonic() + wait_ms / 1000
                    while True:
                        page = session.journal.read(cursor, limit=limit)
                        if page["events"] or page["dropped"]:
                            self._send(200, page)
                            return
                        remaining = max(0, int((deadline - time.monotonic()) * 1000))
                        if remaining == 0:
                            self._send(200, page)
                            return
                        if session.lock.acquire(blocking=False):
                            try:
                                owner.sessions.pump_events(session)
                            finally:
                                session.lock.release()
                        page = session.journal.read(
                            cursor, limit=limit, wait_ms=min(200, remaining)
                        )
                        if page["events"] or page["dropped"] or remaining <= 200:
                            self._send(200, page)
                            return
                except ApplicationError as exc:
                    self._send(400, error_payload(exc.code, str(exc)))
                except (KeyError, ValueError) as exc:
                    self._send(400, error_payload("invalid_request", str(exc)))
                except Exception:
                    self._send(
                        500,
                        error_payload(
                            "event_read_failed", "Graphimo could not read the event stream"
                        ),
                    )

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                if not owner._authorized(
                    self.headers.get("Authorization"), self.headers.get("X-Graphimo-Token")
                ):
                    self._send(
                        401,
                        error_payload("unauthorized", "A valid Graphimo service token is required"),
                    )
                    return
                payload = self._read_payload()
                if payload is None:
                    return
                try:
                    if self.path == "/v1/projects/open":
                        result = owner.sessions.open_project(payload.get("path"))
                    elif self.path == "/v1/projects/create":
                        result = owner.sessions.create_project(payload.get("path"))
                    elif self.path == "/v1/projects/close":
                        result = owner.sessions.close_project(
                            _required_string(payload, "project_id")
                        )
                    elif self.path == "/v1/execute":
                        result = owner.sessions.execute(
                            _required_string(payload, "project_id"),
                            _required_string(payload, "action"),
                            payload.get("args", {}),
                        )
                    elif self.path == "/v1/batch":
                        result = owner.sessions.execute_batch(
                            _required_string(payload, "project_id"),
                            payload.get("operations"),
                            continue_on_error=payload.get("continue_on_error", False),
                        )
                    elif self.path == "/v1/shutdown":
                        self._send(200, {"stopping": True})
                        threading.Timer(0.1, owner.close).start()
                        return
                    else:
                        self._send(
                            404, error_payload("not_found", "Service endpoint was not found")
                        )
                        return
                    self._send(200, jsonable(result))
                except ApplicationError as exc:
                    self._send(400, error_payload(exc.code, str(exc)))
                except (TypeError, ValueError, KeyError) as exc:
                    self._send(400, error_payload("invalid_request", str(exc)))
                except Exception:
                    self._send(
                        500,
                        error_payload("operation_failed", "Graphimo application operation failed"),
                    )

            def _read_payload(self) -> dict[str, Any] | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send(400, error_payload("invalid_request", "Content-Length is invalid"))
                    return None
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    self._send(
                        413,
                        error_payload(
                            "request_too_large", "Request must be between 1 byte and 1 MB"
                        ),
                    )
                    return None
                try:
                    payload = json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._send(
                        400, error_payload("invalid_json", "Request body must be valid UTF-8 JSON")
                    )
                    return None
                if not isinstance(payload, dict):
                    self._send(
                        400, error_payload("invalid_request", "Request body must be a JSON object")
                    )
                    return None
                return payload

            def _send(self, status: int, value: Any) -> None:
                data = json.dumps(
                    value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
                if len(data) > MAX_RESPONSE_BYTES:
                    status = 500
                    data = json.dumps(
                        error_payload(
                            "response_too_large", "Graphimo response exceeded the 4 MB limit"
                        ),
                        separators=(",", ":"),
                    ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                return

        return ThreadingHTTPServer((self.host, port), Handler)

    def _authorized(self, authorization: str | None, fallback: str | None) -> bool:
        supplied = authorization.removeprefix("Bearer ") if authorization else fallback
        return isinstance(supplied, str) and hmac.compare_digest(supplied, self.token)

    def start(self) -> GraphimoService:
        if self._thread is not None:
            return self
        if self.discovery_file is not None:
            self._write_discovery_file()
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="graphimo-service", daemon=True
        )
        self._thread.start()
        return self

    def _write_discovery_file(self) -> None:
        target = self.discovery_file
        assert target is not None
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            target.parent.chmod(0o700)
        except OSError:
            pass
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
        payload = {"host": self.host, "port": self.port, "token": self.token, "pid": os.getpid()}
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, target)

    def close(self) -> None:
        with self._close_lock:
            if self._closed.is_set():
                return
            if self._thread is not None:
                self._server.shutdown()
            self._server.server_close()
            self.sessions.close()
            if self._thread is not None:
                self._thread.join(timeout=5)
            if self.discovery_file is not None:
                try:
                    value = json.loads(self.discovery_file.read_text(encoding="utf-8"))
                    if value.get("pid") == os.getpid():
                        self.discovery_file.unlink(missing_ok=True)
                except (OSError, UnicodeError, json.JSONDecodeError):
                    pass
            self._closed.set()

    def __enter__(self) -> GraphimoService:
        return self.start()

    def __exit__(self, *_args: Any) -> None:
        self.close()


def _one(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key)
    if values is None or len(values) != 1:
        raise ValueError(f"Query parameter {key!r} must be supplied exactly once")
    return values[0]


def _integer(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = query.get(key)
    if raw is None:
        return default
    if len(raw) != 1:
        raise ValueError(f"Query parameter {key!r} must be supplied at most once")
    try:
        return int(raw[0])
    except ValueError as exc:
        raise ValueError(f"Query parameter {key!r} must be an integer") from exc


def _required_string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ApplicationError("invalid_request", f"Field {key!r} must be a non-empty string")
    return result


def default_discovery_file() -> Path:
    override = os.environ.get("GRAPHIMO_SERVICE_FILE")
    return Path(override).expanduser() if override else Path.home() / ".graphimo" / "service.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Graphimo application service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--discovery-file", default=str(default_discovery_file()))
    args = parser.parse_args()
    service = GraphimoService(args.host, args.port, discovery_file=args.discovery_file).start()
    print(f"Graphimo service listening at {service.base_url}", flush=True)
    try:
        while not service._closed.wait(3600):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        service.close()


if __name__ == "__main__":
    main()
