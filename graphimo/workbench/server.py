from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlparse

from graphimo.application import ApplicationError, GraphimoApplication
from graphimo.marimo_http import MarimoHttpPort

_LOG = logging.getLogger("graphimo.workbench")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class WorkbenchController:
    """Thin HTTP-facing controller; all Graphimo semantics live in the app layer."""

    def __init__(self) -> None:
        self._lock = RLock()
        self.port: MarimoHttpPort | None = None
        self.app: GraphimoApplication | None = None
        self.notebook_path: Path | None = None

    def state(self) -> dict[str, Any]:
        with self._lock:
            app = self._require_app()
            return _jsonable(app.refresh_project())

    def intent(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        if action == "open_project":
            return self.open_project(payload.get("path"))
        with self._lock:
            app = self._require_app()
            if action == "refresh_project":
                return {"state": _jsonable(app.refresh_project())}
            if action == "select":
                return {"state": _jsonable(app.select(payload.get("ids", [])))}
            if action == "add_palette_item":
                result = app.add_palette_item(
                    payload["item_id"],
                    position=tuple(payload.get("position", (80, 80))),
                    bindings=payload.get("bindings", {}),
                )
                return {"result": result, "state": _jsonable(app.refresh_project())}
            if action in {"connect", "reconnect"}:
                result = app.connect(
                    payload["source_cell_id"],
                    payload["target_cell_id"],
                    input_name=payload["input_name"],
                    output_index=payload.get("output_index", 0),
                )
                return {"result": result, "state": _jsonable(app.refresh_project())}
            if action == "disconnect":
                result = app.disconnect(payload["target_cell_id"], input_name=payload["input_name"])
                return {"result": result, "state": _jsonable(app.refresh_project())}
            if action == "set_literal":
                result = app.set_literal(payload["cell_id"], payload["input_name"], payload["value"])
                return {"result": result, "state": _jsonable(app.refresh_project())}
            if action == "run_target":
                result = app.run_target(payload["cell_id"])
                state = app.refresh_project()
                try:
                    inspection = app.inspect_node_instance(payload["cell_id"])
                except ApplicationError:
                    inspection = None
                return {"result": _jsonable(result), "inspection": inspection, "state": _jsonable(state)}
            if action == "inspect":
                return {"inspection": app.open_inspector(payload["cell_id"])}
            if action == "delete_elements":
                result = app.delete_elements(payload.get("ids", []))
                return {"result": result, "state": _jsonable(app.refresh_project())}
            if action == "duplicate_elements":
                result = app.duplicate_elements(payload.get("ids", []))
                return {"result": result, "state": _jsonable(app.refresh_project())}
            if action == "move_elements":
                positions = {
                    key: tuple(value)
                    for key, value in payload.get("positions", {}).items()
                }
                return {"state": _jsonable(app.move_elements(positions))}
            if action == "close_project":
                app.close_project()
                self._close_port()
                return {"closed": True}
            raise ApplicationError("feature_not_available", f"Workbench action '{action}' is unavailable")

    def open_project(self, path_value: Any) -> dict[str, Any]:
        if not isinstance(path_value, str) or not path_value.strip():
            raise ApplicationError("invalid_project_path", "Enter a path to a marimo Python notebook")
        path = Path(path_value).expanduser().resolve()
        if path.suffix != ".py":
            raise ApplicationError("invalid_project_path", "Graphimo projects are marimo Python files ending in .py")
        with self._lock:
            self._close_port()
            try:
                port = MarimoHttpPort(path)
                self.port = port
                app = GraphimoApplication(
                    port,
                    path.parent / "components",
                    layout_path=path.parent / ".graphimo.json",
                )
                state = app.open_project(path, port.session_id)
            except Exception:
                self._close_port()
                raise
            self.port = port
            self.app = app
            self.notebook_path = path
            return {"state": _jsonable(state), "path": str(path)}

    def shutdown(self) -> None:
        with self._lock:
            self._close_port()

    def _require_app(self) -> GraphimoApplication:
        if self.app is None:
            raise ApplicationError("project_not_open", "Open a marimo project to start using the Workbench")
        return self.app

    def _close_port(self) -> None:
        if self.port is not None:
            self.port.close()
        self.port = None
        self.app = None
        self.notebook_path = None


class WorkbenchHandler(BaseHTTPRequestHandler):
    controller: WorkbenchController

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/api/state":
            try:
                self._send_json(200, {"state": self.controller.state()})
            except ApplicationError as exc:
                self._send_json(409, {"error": {"code": exc.code, "message": str(exc)}})
            except Exception as exc:
                _LOG.exception("Workbench state request failed")
                self._send_json(500, {"error": {"code": "operation_failed", "message": str(exc)}})
            return
        resources = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
        name = resources.get(path)
        if name is None:
            self.send_error(404)
            return
        content_type = "text/html; charset=utf-8" if name.endswith(".html") else "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
        data = files("graphimo.workbench").joinpath(name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if urlparse(self.path).path != "/api/intent":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ApplicationError("invalid_request", "Request body is empty or too large")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ApplicationError("invalid_request", "Intent body must be a JSON object")
            result = self.controller.intent(payload)
            self._send_json(200, result)
        except ApplicationError as exc:
            self._send_json(400, {"error": {"code": exc.code, "message": str(exc)}})
        except Exception as exc:
            _LOG.exception("Workbench intent failed")
            self._send_json(500, {"error": {"code": "operation_failed", "message": str(exc)}})

    def _send_json(self, status: int, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        _LOG.info("%s - %s", self.address_string(), format % args)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    controller = WorkbenchController()
    handler = type("BoundWorkbenchHandler", (WorkbenchHandler,), {"controller": controller})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Graphimo Workbench: http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        controller.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the standalone Graphimo development Workbench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
