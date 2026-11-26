"""Python client for the authenticated local Graphimo service."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .service.protocol import PROTOCOL_VERSION
from .service.server import default_manifest_path


class GraphimoServiceError(RuntimeError):
    """An error returned by the local Graphimo service."""

    def __init__(self, code: str, message: str, *, status: int | None = None):
        self.code = code
        self.status = status
        super().__init__(message)


class GraphimoClient:
    """Connect to Graphimo's loopback API or start its local service."""

    def __init__(self, url: str, token: str, *, timeout: float = 10.0):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("Graphimo local service URL must be an HTTP URL")
        if parsed.hostname != "127.0.0.1":
            raise ValueError("Graphimo client refuses service URLs outside 127.0.0.1")
        if not token:
            raise ValueError("Graphimo service token must be non-empty")
        if timeout <= 0:
            raise ValueError("request timeout must be positive")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._closed = False

    @classmethod
    def connect_local(
        cls,
        *,
        manifest_path: str | Path | None = None,
        auto_start: bool = True,
        timeout: float = 10.0,
        startup_timeout: float = 10.0,
    ) -> GraphimoClient:
        path = Path(manifest_path) if manifest_path else default_manifest_path()
        try:
            manifest = _read_manifest(path)
            client = cls(manifest["url"], manifest["token"], timeout=timeout)
            client.health()
            return client
        except (OSError, ValueError, KeyError, GraphimoServiceError, json.JSONDecodeError):
            if not auto_start:
                raise GraphimoServiceError(
                    "service_unavailable", f"no reachable Graphimo service manifest at {path}"
                )

        path.parent.mkdir(parents=True, exist_ok=True)
        log_path = path.with_suffix(path.suffix + ".log")
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(log_path, 0o600)
        with os.fdopen(descriptor, "ab") as log:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "graphimo.service.server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--manifest-path",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                env=os.environ.copy(),
            )
        deadline = time.monotonic() + startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                manifest = _read_manifest(path)
                client = cls(manifest["url"], manifest["token"], timeout=timeout)
                client.health()
                return client
            except (
                OSError,
                ValueError,
                KeyError,
                GraphimoServiceError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
                time.sleep(0.05)
        raise GraphimoServiceError(
            "service_start_failed",
            f"Graphimo local service did not start within {startup_timeout:g}s; see {log_path}: {last_error}",
        )

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/v1/health")

    def open_project(self, path: str | Path) -> GraphimoProject:
        result = self._request("POST", "/v1/projects/open", {"path": str(path)})
        return GraphimoProject(self, result["project_id"], result["path"])

    def close_project(self, project: str | GraphimoProject) -> bool:
        project_id = project.project_id if isinstance(project, GraphimoProject) else project
        result = self._request("POST", "/v1/projects/close", {"project": project_id})
        return bool(result["closed"])

    def execute(
        self, project: str | GraphimoProject, action: str, args: dict[str, Any] | None = None
    ) -> Any:
        project_id = project.project_id if isinstance(project, GraphimoProject) else project
        return self._request(
            "POST",
            "/v1/execute",
            {"project": project_id, "action": action, "args": args or {}},
        )

    def batch(
        self,
        project: str | GraphimoProject,
        operations: list[dict[str, Any]],
        *,
        continue_on_error: bool = False,
    ) -> dict[str, Any]:
        project_id = project.project_id if isinstance(project, GraphimoProject) else project
        return self._request(
            "POST",
            "/v1/batch",
            {
                "project": project_id,
                "operations": operations,
                "continue_on_error": continue_on_error,
            },
        )

    def events(
        self,
        project: str | GraphimoProject,
        *,
        cursor: str | int | None = None,
        limit: int = 128,
        wait_ms: int = 750,
    ) -> dict[str, Any]:
        project_id = project.project_id if isinstance(project, GraphimoProject) else project
        query = {"project": project_id, "limit": str(limit), "wait_ms": str(wait_ms)}
        if cursor is not None:
            query["cursor"] = str(cursor)
        return self._request("GET", "/v1/events?" + urllib.parse.urlencode(query))

    def publish_kernel_events(
        self, project: str | GraphimoProject, events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Push a bounded batch emitted by an attached marimo kernel."""
        project_id = project.project_id if isinstance(project, GraphimoProject) else project
        return self._request(
            "POST", "/v1/events/publish", {"project": project_id, "events": events}
        )

    def attach_kernel(
        self,
        project: str | GraphimoProject,
        *,
        url: str,
        token: str,
        session_id: str,
    ) -> dict[str, Any]:
        project_id = project.project_id if isinstance(project, GraphimoProject) else project
        return self._request(
            "POST",
            "/v1/kernels/attach",
            {"project": project_id, "url": url, "token": token, "session_id": session_id},
        )

    def close(self) -> None:
        self._closed = True

    def _request(self, method: str, route: str, value: Any = None) -> Any:
        if self._closed:
            raise GraphimoServiceError("client_closed", "Graphimo client is closed")
        data = None if value is None else json.dumps(value, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url + route,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise GraphimoServiceError("http_error", str(exc), status=exc.code) from exc
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            raise GraphimoServiceError(
                error.get("code", "http_error"),
                error.get("message", str(exc)),
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GraphimoServiceError("service_unavailable", str(exc)) from exc
        if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
            raise GraphimoServiceError("invalid_response", "Graphimo service returned invalid JSON")
        if not payload["ok"]:
            error = payload.get("error", {})
            raise GraphimoServiceError(
                error.get("code", "service_error"), error.get("message", "request failed")
            )
        return payload.get("result")


class GraphimoProject:
    """Project-scoped convenience handle returned by :meth:`open_project`."""

    def __init__(self, client: GraphimoClient, project_id: str, path: str):
        self.client = client
        self.project_id = project_id
        self.path = path
        self._closed = False

    def execute(self, action: str, args: dict[str, Any] | None = None) -> Any:
        self._ensure_open()
        return self.client.execute(self, action, args)

    def batch(
        self, operations: list[dict[str, Any]], *, continue_on_error: bool = False
    ) -> dict[str, Any]:
        self._ensure_open()
        return self.client.batch(self, operations, continue_on_error=continue_on_error)

    def events(
        self,
        *,
        cursor: str | int | None = None,
        limit: int = 128,
        wait_ms: int = 750,
    ) -> dict[str, Any]:
        self._ensure_open()
        return self.client.events(self, cursor=cursor, limit=limit, wait_ms=wait_ms)

    def attach_kernel(self, *, url: str, token: str, session_id: str) -> dict[str, Any]:
        return self.client.attach_kernel(self, url=url, token=token, session_id=session_id)

    def inspect_notebook(self) -> dict[str, Any]:
        """Read bounded cells, definitions, references and edges from live marimo."""
        return self.execute("notebook.inspect")

    def add_component(
        self,
        path: str,
        callable_name: str,
        *,
        output_name: str,
        bindings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate, generate and insert one reusable ``@node`` component call."""
        return self.execute(
            "component.add",
            {
                "path": path,
                "callable": callable_name,
                "output_name": output_name,
                "bindings": bindings or {},
            },
        )

    def connect(
        self,
        source_cell_id: str,
        variable: str,
        target_cell_id: str,
        input_ref: str,
    ) -> dict[str, Any]:
        """Connect a live marimo definition to one unambiguous call argument."""
        return self.execute(
            "notebook.connect",
            {
                "from_cell_id": source_cell_id,
                "variable": variable,
                "to_cell_id": target_cell_id,
                "input_ref": input_ref,
            },
        )

    def run(self, cell_id: str) -> dict[str, Any]:
        """Ask marimo to run one cell after Graphimo's disabled-dependency check."""
        return self.execute("notebook.run", {"cell_id": cell_id})

    def inspect_artifact(self, artifact_id: str) -> dict[str, Any]:
        """Read an ArtifactStore reference without exposing its storage backend."""
        return self.execute("artifact.inspect", {"artifact_id": artifact_id})

    def artifact_stats(self) -> dict[str, Any]:
        """Read logical and physical ArtifactStore usage statistics."""
        return self.execute("artifact.stats")

    def close(self) -> bool:
        if self._closed:
            return False
        closed = self.client.close_project(self)
        self._closed = True
        return closed

    def _ensure_open(self) -> None:
        if self._closed:
            raise GraphimoServiceError("project_closed", "Graphimo project handle is closed")

    def __enter__(self) -> GraphimoProject:
        self._ensure_open()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("Graphimo service manifest permissions must be owner-only")
    if path.stat().st_size > 16 * 1024:
        raise ValueError("Graphimo service manifest is too large")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("protocol") != PROTOCOL_VERSION:
        raise ValueError("Graphimo service manifest has an unsupported protocol version")
    if not isinstance(manifest.get("url"), str) or not isinstance(manifest.get("token"), str):
        raise ValueError("Graphimo service manifest is missing URL or token")
    return manifest
