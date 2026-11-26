"""Authenticated loopback-only HTTP service for GraphimoApplication."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from typing import Any

from ..metablocks import MetaBlockError
from .protocol import (
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    ServiceProtocolError,
    encode_response,
    parse_json_object,
)
from .sessions import ProjectSession, ProjectSessionManager

_ACTION_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


def _action_error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return (
        code if isinstance(code, str) and _ACTION_ERROR_CODE.fullmatch(code) else "invalid_action"
    )


def default_manifest_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "graphimo" / "service.json"


class GraphimoService:
    """Own a persistent project-session manager and an authenticated local API."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        manifest_path: str | Path | None = None,
        sessions: ProjectSessionManager | None = None,
    ):
        if host != "127.0.0.1":
            raise ValueError("Graphimo service may bind only to 127.0.0.1")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("service port must be between 0 and 65535")
        self.host = host
        self.port = port
        self.manifest_path = Path(manifest_path) if manifest_path else default_manifest_path()
        self.sessions = sessions or ProjectSessionManager()
        self.token = secrets.token_urlsafe(32)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._manifest_written = False
        self._closed = False

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("Graphimo service has not been started")
        bound_host, bound_port = self._server.server_address[:2]
        display_host = f"[{bound_host}]" if ":" in bound_host else bound_host
        return f"http://{display_host}:{bound_port}"

    def start(self) -> GraphimoService:
        if self._closed:
            raise RuntimeError("Graphimo service is closed")
        if self._server is not None:
            return self
        service = self

        class RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def do_GET(self) -> None:
                service._handle(self)

            def do_POST(self) -> None:
                service._handle(self)

            def do_PUT(self) -> None:
                service._handle(self)

            def do_DELETE(self) -> None:
                service._handle(self)

        server = ThreadingHTTPServer((self.host, self.port), RequestHandler)
        server.daemon_threads = True
        server.allow_reuse_address = False
        self._server = server
        try:
            self._write_manifest()
        except Exception:
            server.server_close()
            self._server = None
            raise
        self._thread = threading.Thread(
            target=server.serve_forever, name="graphimo-service", daemon=True
        )
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self.start()
        assert self._thread is not None
        self._thread.join()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.sessions.close()
        if self._manifest_written:
            try:
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if manifest.get("token") == self.token:
                    self.manifest_path.unlink(missing_ok=True)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            self._manifest_written = False

    def __enter__(self) -> GraphimoService:
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def _write_manifest(self) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "protocol": PROTOCOL_VERSION,
            "url": self.url,
            "token": self.token,
            "pid": os.getpid(),
        }
        encoded = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        temporary = self.manifest_path.with_name(
            f".{self.manifest_path.name}.{secrets.token_hex(8)}.tmp"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.manifest_path)
            self.manifest_path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
        self._manifest_written = True

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        remote = handler.client_address[0]
        try:
            if not ipaddress.ip_address(remote).is_loopback:
                raise ServiceProtocolError(
                    "loopback_only", "service accepts loopback clients only", status=403
                )
            supplied = handler.headers.get("Authorization", "")
            expected = f"Bearer {self.token}"
            if not hmac.compare_digest(supplied, expected):
                raise ServiceProtocolError(
                    "unauthorized", "a valid bearer token is required", status=401
                )
            parsed = urlsplit(handler.path)
            if handler.command == "GET" and parsed.path == "/v1/health":
                result: Any = {
                    "protocol": PROTOCOL_VERSION,
                    "projects": self.sessions.list(),
                }
            elif handler.command == "POST":
                body = self._read_body(handler)
                request = parse_json_object(body)
                result = self._post(parsed.path, request)
            elif handler.command == "GET" and parsed.path == "/v1/events":
                result = self._get_events(parsed.query)
            else:
                raise ServiceProtocolError(
                    "not_found", "unknown Graphimo service route", status=404
                )
            self._write(handler, 200, {"ok": True, "result": result})
        except ServiceProtocolError as exc:
            self._write_error(handler, exc.status, exc.code, str(exc))
        except KeyError as exc:
            self._write_error(handler, 404, "project_not_found", str(exc))
        except (ValueError, TypeError, FileNotFoundError, MetaBlockError) as exc:
            self._write_error(handler, 422, _action_error_code(exc), str(exc))
        except Exception:
            self._write_error(handler, 500, "internal_error", "Graphimo service action failed")

    @staticmethod
    def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
        raw_length = handler.headers.get("Content-Length")
        if raw_length is None or not raw_length.isascii() or not raw_length.isdecimal():
            raise ServiceProtocolError("invalid_content_length", "Content-Length is required")
        if len(raw_length) > 10:
            raise ServiceProtocolError(
                "request_too_large", "request body exceeds the size limit", status=413
            )
        length = int(raw_length)
        if length > MAX_REQUEST_BYTES:
            raise ServiceProtocolError(
                "request_too_large",
                f"request body exceeds the {MAX_REQUEST_BYTES}-byte limit",
                status=413,
            )
        body = handler.rfile.read(length)
        if len(body) != length:
            raise ServiceProtocolError("incomplete_request", "request body ended prematurely")
        return body

    def _post(self, path: str, request: dict[str, Any]) -> Any:
        if path == "/v1/projects/open":
            project_path = request.get("path")
            if not isinstance(project_path, str) or not project_path:
                raise ServiceProtocolError("invalid_request", "path must be non-empty text")
            session, reused = self.sessions.open_project(project_path)
            return {"project_id": session.project_id, "path": str(session.path), "reused": reused}
        if path == "/v1/projects/close":
            project_id = request.get("project")
            if not isinstance(project_id, str):
                raise ServiceProtocolError("invalid_request", "project must be text")
            return {"closed": self.sessions.close_project(project_id)}
        if path == "/v1/events/publish":
            session = self._project(request)
            try:
                return session.publish_kernel_events(request.get("events"))
            except (ValueError, TypeError) as exc:
                raise ServiceProtocolError("invalid_kernel_events", str(exc), status=422) from exc
        if path == "/v1/kernels/attach":
            session = self._project(request)
            try:
                return session.attach_kernel(
                    url=request.get("url"),
                    token=request.get("token"),
                    session_id=request.get("session_id"),
                )
            except (ValueError, TypeError) as exc:
                raise ServiceProtocolError("invalid_kernel_config", str(exc), status=422) from exc
        if path == "/v1/execute":
            session = self._project(request)
            action = request.get("action")
            args = request.get("args", {})
            if not isinstance(action, str) or not isinstance(args, dict):
                raise ServiceProtocolError("invalid_request", "action and object args are required")
            try:
                return session.execute(action, args)
            except (ValueError, TypeError, FileNotFoundError) as exc:
                raise ServiceProtocolError(_action_error_code(exc), str(exc), status=422) from exc
        if path == "/v1/batch":
            session = self._project(request)
            return session.execute_batch(
                request.get("operations"),
                continue_on_error=request.get("continue_on_error", False),
            )
        raise ServiceProtocolError("not_found", "unknown Graphimo service route", status=404)

    def _get_events(self, query_string: str) -> dict[str, Any]:
        try:
            query = parse_qs(query_string, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise ServiceProtocolError("invalid_query", "events query is malformed") from exc
        if set(query) - {"project", "cursor", "limit", "wait_ms"}:
            raise ServiceProtocolError("invalid_query", "events query contains unknown fields")
        project_values = query.get("project", [])
        if len(project_values) != 1 or not project_values[0]:
            raise ServiceProtocolError("invalid_query", "exactly one project is required")
        cursor = _one(query, "cursor")
        limit = _integer_query(query, "limit", 128)
        wait_ms = _integer_query(query, "wait_ms", 750)
        try:
            return self.sessions.get(project_values[0]).poll_events(
                cursor, limit=limit, wait_ms=wait_ms
            )
        except (ValueError, TypeError) as exc:
            raise ServiceProtocolError("invalid_query", str(exc)) from exc

    def _project(self, request: dict[str, Any]) -> ProjectSession:
        project_id = request.get("project")
        if not isinstance(project_id, str) or not project_id:
            raise ServiceProtocolError("invalid_request", "project must be non-empty text")
        try:
            return self.sessions.get(project_id)
        except KeyError as exc:
            raise ServiceProtocolError("project_not_found", str(exc), status=404) from exc

    @staticmethod
    def _write(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
        try:
            body = encode_response(payload)
        except ServiceProtocolError as exc:
            status = exc.status
            body = json.dumps(
                {"ok": False, "error": {"code": exc.code, "message": str(exc)}},
                ensure_ascii=False,
            ).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        handler.wfile.write(body)

    @classmethod
    def _write_error(
        cls, handler: BaseHTTPRequestHandler, status: int, code: str, message: str
    ) -> None:
        cls._write(handler, status, {"ok": False, "error": {"code": code, "message": message}})


def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key, [])
    if len(values) > 1:
        raise ServiceProtocolError("invalid_query", f"{key} may appear only once")
    return values[0] if values else None


def _integer_query(query: dict[str, list[str]], key: str, default: int) -> int:
    value = _one(query, key)
    if value is None:
        return default
    if not value.isascii() or not value.isdecimal() or len(value) > 10:
        raise ServiceProtocolError("invalid_query", f"{key} must be a non-negative integer")
    return int(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Graphimo service")
    parser.add_argument("--host", default="127.0.0.1", help="loopback address to bind")
    parser.add_argument("--port", default=0, type=int, help="port (0 selects an available port)")
    parser.add_argument("--manifest-path", type=Path, default=None)
    options = parser.parse_args()
    service = GraphimoService(
        host=options.host, port=options.port, manifest_path=options.manifest_path
    )
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
