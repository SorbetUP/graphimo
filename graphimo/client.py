"""Small Python client for the persistent loopback Graphimo service."""

from __future__ import annotations

import json
import ipaddress
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphimo.service.protocol import MAX_RESPONSE_BYTES
from graphimo.service.server import default_discovery_file


class GraphimoServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class PythonExpression:
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("Python expression must be non-empty")


def expr(source: str) -> PythonExpression:
    """Mark a string as Python source rather than a literal string value."""
    return PythonExpression(source)


class GraphimoClient:
    def __init__(
        self, base_url: str, token: str, *, discovery_file: str | Path | None = None
    ) -> None:
        parsed = urlsplit(base_url) if isinstance(base_url, str) else None
        try:
            loopback = (
                parsed is not None
                and parsed.scheme == "http"
                and ipaddress.ip_address(parsed.hostname or "").is_loopback
            )
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("GraphimoClient only accepts a loopback HTTP service")
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Graphimo service token is invalid")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.discovery_file = (
            Path(discovery_file).expanduser() if discovery_file is not None else None
        )

    @classmethod
    def connect_local(
        cls,
        *,
        discovery_file: str | Path | None = None,
        startup_timeout: float = 10.0,
    ) -> GraphimoClient:
        if not isinstance(startup_timeout, (int, float)) or startup_timeout <= 0:
            raise ValueError("startup_timeout must be a positive number")
        path = Path(discovery_file).expanduser() if discovery_file else default_discovery_file()
        information = _read_discovery(path)
        if information is not None and _service_healthy(information):
            return cls(_base_url(information), information["token"], discovery_file=path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        deadline = time.monotonic() + startup_timeout
        lock_path = path.with_name(f"{path.name}.start.lock")
        lock_fd = None
        while lock_fd is None:
            information = _read_discovery(path)
            if information is not None and _service_healthy(information):
                return cls(_base_url(information), information["token"], discovery_file=path)
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(lock_fd, str(os.getpid()).encode("ascii"))
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > max(30, startup_timeout * 2):
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise GraphimoServiceError(
                        "service_start_timeout",
                        "Timed out waiting for another client to start Graphimo",
                    )
                time.sleep(0.1)
        try:
            information = _read_discovery(path)
            if information is not None and _service_healthy(information):
                return cls(_base_url(information), information["token"], discovery_file=path)
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "graphimo.service.server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--discovery-file",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            while time.monotonic() < deadline:
                information = _read_discovery(path)
                if information is not None and _service_healthy(information):
                    return cls(_base_url(information), information["token"], discovery_file=path)
                time.sleep(0.1)
            raise GraphimoServiceError(
                "service_start_timeout",
                f"Graphimo service did not become ready within {startup_timeout:g} seconds",
            )
        finally:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)

    def open_project(self, path: str | Path) -> GraphimoProject:
        response = self._post("/v1/projects/open", {"path": str(Path(path).expanduser().resolve())})
        return GraphimoProject(self, response["project_id"], response["state"], response["path"])

    def create_project(self, path: str | Path) -> GraphimoProject:
        response = self._post(
            "/v1/projects/create", {"path": str(Path(path).expanduser().resolve())}
        )
        return GraphimoProject(self, response["project_id"], response["state"], response["path"])

    def execute(self, project_id: str, action: str, **args: Any) -> dict[str, Any]:
        return self._post("/v1/execute", {"project_id": project_id, "action": action, "args": args})

    def execute_batch(
        self,
        project_id: str,
        operations: list[dict[str, Any]],
        *,
        continue_on_error: bool = False,
    ) -> dict[str, Any]:
        return self._post(
            "/v1/batch",
            {
                "project_id": project_id,
                "operations": operations,
                "continue_on_error": continue_on_error,
            },
        )

    def events(
        self, project_id: str, *, cursor: int = 0, limit: int = 100, wait_ms: int = 0
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"project": project_id, "cursor": cursor, "limit": limit, "wait_ms": wait_ms}
        )
        return self._get(f"/v1/events?{query}")

    def close_project(self, project_id: str) -> dict[str, Any]:
        return self._post("/v1/projects/close", {"project_id": project_id})

    def stop_service(self) -> dict[str, Any]:
        return self._post("/v1/shutdown", {})

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(
            body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=encoded,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        return self._open(request)

    def _get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            headers={"Accept": "application/json", "Authorization": f"Bearer {self.token}"},
        )
        return self._open(request)

    @staticmethod
    def _open(request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise GraphimoServiceError(
                        "response_too_large", "Graphimo response exceeded its client limit"
                    )
                value = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                value = json.loads(exc.read(MAX_RESPONSE_BYTES).decode("utf-8"))
                error = value.get("error", {})
            except (ValueError, UnicodeError):
                error = {}
            raise GraphimoServiceError(
                error.get("code", "http_error"), error.get("message", str(exc)), exc.code
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GraphimoServiceError("service_unavailable", str(exc)) from exc
        if not isinstance(value, dict):
            raise GraphimoServiceError(
                "invalid_response", "Graphimo service returned a non-object response"
            )
        if "error" in value:
            error = value["error"]
            raise GraphimoServiceError(
                error.get("code", "operation_failed"),
                error.get("message", "Graphimo request failed"),
            )
        return value


class GraphimoProject:
    def __init__(
        self, client: GraphimoClient, project_id: str, state: dict[str, Any], path: str
    ) -> None:
        self.client = client
        self.id = project_id
        self.path = path
        self.state = state

    def refresh(self) -> dict[str, Any]:
        response = self.client.execute(self.id, "refresh_project")
        self.state = response["state"]
        return self.state

    def execute(self, action: str, **args: Any) -> dict[str, Any]:
        response = self.client.execute(self.id, action, **args)
        self.state = response.get("state", self.state)
        return response

    def batch(
        self, operations: list[dict[str, Any]], *, continue_on_error: bool = False
    ) -> dict[str, Any]:
        response = self.client.execute_batch(
            self.id, operations, continue_on_error=continue_on_error
        )
        self.state = response["state"]
        return response

    def add(
        self, item: str, *, position: tuple[float, float] = (80, 80), **bindings: Any
    ) -> BlockReference:
        palette = self.state.get("palette", {}).get("items", [])
        matches = [
            entry for entry in palette if entry.get("id") == item or entry.get("title") == item
        ]
        if len(matches) != 1:
            raise GraphimoServiceError(
                "palette_item_not_found", f"Palette item {item!r} is unavailable or ambiguous"
            )
        expressions = {name: _expression(value) for name, value in bindings.items()}
        response = self.execute(
            "add_palette_item",
            item_id=matches[0]["id"],
            bindings=expressions,
            position=list(position),
        )
        created = response["result"]
        return BlockReference(
            self,
            created["cell_id"],
            created["result_var"],
            created.get("instance_id"),
            created.get("group_id"),
        )

    def connect(self, source: PortReference, target: PortReference) -> dict[str, Any]:
        if source.direction != "output" or target.direction != "input":
            raise ValueError("Connections require source.output(...) and target.input(...)")
        block = _block_by_id(self.state, source.block.cell_id)
        if isinstance(source.port, str):
            names = [port["name"] for port in block.get("outputs", [])]
            if source.port not in names:
                raise GraphimoServiceError(
                    "output_not_found", f"Block has no output {source.port!r}"
                )
            output_index = names.index(source.port)
        else:
            output_index = source.port
        return self.execute(
            "connect",
            source_cell_id=source.block.cell_id,
            target_cell_id=target.block.cell_id,
            input_name=target.port,
            output_index=output_index,
        )

    def run(self, block: BlockReference) -> dict[str, Any]:
        return self.execute("run_target", cell_id=block.cell_id)

    def inspect(self, block: BlockReference) -> dict[str, Any]:
        return self.execute("open_inspector", cell_id=block.cell_id)["result"]

    def events(self, **kwargs: Any) -> dict[str, Any]:
        return self.client.events(self.id, **kwargs)

    def close(self) -> dict[str, Any]:
        return self.client.close_project(self.id)


@dataclass(frozen=True)
class BlockReference:
    project: GraphimoProject
    cell_id: str
    result_var: str
    instance_id: str | None = None
    group_id: str | None = None

    def input(self, name: str) -> PortReference:
        return PortReference(self, "input", name)

    def output(self, name_or_index: str | int = 0) -> PortReference:
        return PortReference(self, "output", name_or_index)

    def run(self) -> dict[str, Any]:
        return self.project.run(self)

    def inspect(self) -> dict[str, Any]:
        return self.project.inspect(self)


@dataclass(frozen=True)
class PortReference:
    block: BlockReference
    direction: str
    port: str | int


def _expression(value: Any) -> str:
    if isinstance(value, PythonExpression):
        return value.source.strip()
    return repr(value)


def _block_by_id(state: dict[str, Any], cell_id: str) -> dict[str, Any]:
    for block in state.get("blocks", []):
        if block.get("id") == cell_id:
            return block
    raise GraphimoServiceError(
        "cell_not_found", "Block is no longer present in the Graphimo project"
    )


def _read_discovery(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    try:
        host = value["host"]
        port = value["port"]
        token = value["token"]
    except KeyError:
        return None
    if (
        host != "127.0.0.1"
        or type(port) is not int
        or not 1 <= port <= 65535
        or not isinstance(token, str)
        or len(token) < 32
    ):
        return None
    return value


def _base_url(information: dict[str, Any]) -> str:
    host = f"[{information['host']}]" if ":" in information["host"] else information["host"]
    return f"http://{host}:{information['port']}"


def _service_healthy(information: dict[str, Any]) -> bool:
    request = urllib.request.Request(
        _base_url(information) + "/health",
        headers={"Authorization": f"Bearer {information['token']}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=0.5) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
