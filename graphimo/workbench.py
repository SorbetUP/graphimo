"""Standalone local Graphimo Workbench development host.

The HTTP layer only serves the renderer and forwards structured actions to
``GraphimoApplication``. It does not interpret graph semantics or execute
notebook code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .application import GraphimoApplication
from .metablocks import GraphimoHostAPI

MAX_REQUEST_BYTES = 256 * 1024
_STATIC_ROOT = files("graphimo").joinpath("workbench")


class GraphimoWorkbenchController:
    """Renderer-neutral controller around the shared application action seam."""

    def __init__(self, application: GraphimoApplication, project_path: Path):
        self.application = application
        self.project_path = Path(project_path).resolve()

    def action(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        action = request.get("action")
        args = request.get("args", {})
        return {"ok": True, "result": self.application.execute(action, args)}

    def project(self) -> dict[str, Any]:
        return {
            "project_id": self.application.host.project_id,
            "project_name": self.project_path.name,
            "project_path": str(self.project_path),
            "metablocks": self.application.execute("metablock.list", {}),
            "addons": [
                {
                    "name": item.name,
                    "version": item.version,
                    "distribution": item.distribution,
                }
                for item in self.application.metablocks.installed()
            ],
        }


def create_server(
    controller: GraphimoWorkbenchController,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Create a local-only development server for the standalone Workbench."""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Graphimo Workbench only binds to a loopback address")

    class Handler(BaseHTTPRequestHandler):
        server_version = "GraphimoWorkbench/1"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = unquote(self.path.split("?", 1)[0])
            if path == "/api/project":
                self._json(200, controller.project())
                return
            resource_name = "index.html" if path in {"/", "/index.html"} else path.lstrip("/")
            if not resource_name or "/" in resource_name or "\\" in resource_name:
                self._json(404, {"error": "not found"})
                return
            resource = _STATIC_ROOT.joinpath(resource_name)
            if not resource.is_file():
                self._json(404, {"error": "not found"})
                return
            content = resource.read_bytes()
            content_type = mimetypes.guess_type(resource_name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/api/action":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= MAX_REQUEST_BYTES:
                    raise ValueError("request body must be between 1 and 256 KiB")
                body = self.rfile.read(length)
                request = json.loads(body)
                self._json(200, controller.action(request))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._json(400, {"ok": False, "error": {"message": str(exc)}})
            except Exception as exc:  # noqa: BLE001 - return bounded host error text
                self._json(422, {"ok": False, "error": {"message": str(exc)}})

        def _json(self, status: int, value: Any) -> None:
            content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def build_controller(project_path: Path) -> GraphimoWorkbenchController:
    project = Path(project_path).expanduser().resolve()
    if not project.is_file():
        raise FileNotFoundError(f"Graphimo project does not exist: {project}")
    project_id = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:16]
    host = GraphimoHostAPI(project_id=project_id, project_path=project)
    return GraphimoWorkbenchController(GraphimoApplication(host), project)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the standalone Graphimo Workbench")
    parser.add_argument(
        "--project", required=True, type=Path, help="local Graphimo/marimo project file"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    options = parser.parse_args(argv)
    controller = build_controller(options.project)
    server = create_server(controller, host=options.host, port=options.port)
    url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    print(f"Graphimo Workbench: {url}")
    if not options.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
