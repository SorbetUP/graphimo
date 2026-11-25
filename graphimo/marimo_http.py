"""Local marimo process/HTTP port used by the standalone development Workbench."""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MarimoHttpPort:
    """Use marimo's local code-mode API while marimo owns notebook execution."""

    def __init__(
        self,
        notebook_path: str | Path,
        *,
        startup_timeout: float = 30.0,
        initialize_notebook: bool = True,
    ) -> None:
        self.notebook_path = str(Path(notebook_path).expanduser().resolve())
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen[str] | None = None
        self._websocket: Any = None
        self._websocket_thread: threading.Thread | None = None
        self._closing = threading.Event()
        self._session_id: str | None = None
        self._start(startup_timeout)
        if initialize_notebook:
            self._initialize_notebook()

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            raise RuntimeError("marimo session is not ready")
        return self._session_id

    def _start(self, timeout: float) -> None:
        notebook = Path(self.notebook_path)
        notebook.parent.mkdir(parents=True, exist_ok=True)
        if not notebook.exists():
            notebook.write_text(
                'import marimo\n\n__generated_with = "0.25.2"\napp = marimo.App()\n\n'
                'if __name__ == "__main__":\n    app.run()\n',
                encoding="utf-8",
            )
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "marimo",
                "edit",
                self.notebook_path,
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--headless",
                "--no-token",
                "--no-sandbox",
                "--skip-update-check",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline and self.process.poll() is None:
            try:
                self._request_json("/api/sessions")
                break
            except (OSError, urllib.error.URLError, ValueError) as exc:
                last_error = exc
                time.sleep(0.15)
        else:
            self.close()
            raise RuntimeError(f"marimo did not start: {last_error or 'process exited'}")
        self._open_websocket()
        sessions = self._request_json("/api/sessions")
        if not isinstance(sessions, dict) or not sessions:
            self.close()
            raise RuntimeError("marimo did not expose an active kernel session")
        self._session_id = str(next(iter(sessions)))
        self._server_token = self._discover_server_token()

    def _initialize_notebook(self) -> None:
        self._execute(
            "import json\nimport marimo._code_mode as cm\n"
            "async with cm.get_context() as ctx:\n"
            "    for cell in list(ctx.cells): ctx.run_cell(cell.id)\n"
            "print('GRAPHIMO_RESULT:' + json.dumps({'initialized': True}))",
            timeout=180,
        )

    def _open_websocket(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            self.close()
            raise RuntimeError(
                "The Workbench live marimo port requires the websockets package"
            ) from exc
        websocket_session = f"graphimo-workbench-{uuid.uuid4().hex}"
        ws_url = f"ws://127.0.0.1:{self.port}/ws?session_id={websocket_session}"
        self._websocket = connect(ws_url, open_timeout=10, close_timeout=2)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                payload = json.loads(
                    self._websocket.recv(timeout=max(0.2, deadline - time.monotonic()))
                )
            except Exception as exc:
                self.close()
                raise RuntimeError(f"marimo websocket did not become ready: {exc}") from exc
            if payload.get("op") == "kernel-ready":
                break
        else:
            self.close()
            raise RuntimeError("marimo websocket did not report kernel-ready")
        self._websocket_thread = threading.Thread(target=self._drain_websocket, daemon=True)
        self._websocket_thread.start()

    def _drain_websocket(self) -> None:
        while not self._closing.is_set() and self._websocket is not None:
            try:
                self._websocket.recv(timeout=0.5)
            except TimeoutError:
                continue
            except Exception:
                return

    def _request_json(self, path: str) -> Any:
        with urllib.request.urlopen(self.base_url + path, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _discover_server_token(self) -> str:
        """Read marimo's skew-protection token from its rendered notebook page."""
        with urllib.request.urlopen(self.base_url + "/", timeout=10) as response:
            html = response.read().decode("utf-8", errors="replace")
        match = re.search(r'"serverToken"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', html)
        if match is None:
            raise RuntimeError("marimo page did not expose its server token")
        return json.loads('"' + match.group(1) + '"')

    def _execute(self, code: str, *, timeout: float = 120.0) -> Any:
        payload = json.dumps({"code": code}).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/api/kernel/execute",
            data=payload,
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                "Marimo-Session-Id": self.session_id,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            stream = response.read().decode("utf-8", errors="replace")
        event = None
        stdout: list[str] = []
        stderr: list[str] = []
        done = None
        for line in stream.splitlines():
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line.partition(":")[2].strip())
                except ValueError:
                    continue
                if event == "stdout":
                    stdout.append(str(data.get("data", "")))
                elif event == "stderr":
                    stderr.append(str(data.get("data", "")))
                elif event == "done":
                    done = data
        if not done or done.get("success") is False:
            detail = "".join(stderr).strip()
            raise RuntimeError(detail or "Marimo did not complete the request")
        combined = "".join(stdout)
        marker = "GRAPHIMO_RESULT:"
        position = combined.find(marker)
        if position < 0:
            raise RuntimeError("Marimo returned no Graphimo result")
        result_line = combined[position + len(marker) :].splitlines()[0]
        try:
            return json.loads(result_line)
        except ValueError as exc:
            raise RuntimeError("Marimo returned malformed Graphimo JSON") from exc

    @staticmethod
    def _literal(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))

    def list_active_notebooks(self) -> list[dict[str, Any]]:
        return [{"session_id": self.session_id, "path": self.notebook_path}]

    def inspect(self, session_id: str) -> dict[str, Any]:
        if session_id != self.session_id:
            raise KeyError(session_id)
        code = """
import json
import marimo._code_mode as cm
async with cm.get_context() as ctx:
    graph_cells = ctx.graph.cells
    owners = {}
    for cid, impl in graph_cells.items():
        for name in getattr(impl, "defs", set()): owners.setdefault(name, []).append(str(cid))
    cells = []
    edges = []
    for cell in ctx.cells:
        cid = str(cell.id)
        impl = graph_cells.get(cell.id)
        defs = sorted(getattr(impl, "defs", set())) if impl else []
        refs = sorted(getattr(impl, "refs", set())) if impl else []
        parents = {}
        children = {}
        for name in refs:
            for parent in owners.get(name, []):
                if parent != cid: parents.setdefault(parent, []).append(name)
        for name in defs:
            for child_id, child_impl in graph_cells.items():
                if str(child_id) != cid and name in getattr(child_impl, "refs", set()): children.setdefault(str(child_id), []).append(name)
        for parent, names in parents.items():
            for name in sorted(set(names)): edges.append({"source_cell_id": parent, "target_cell_id": cid, "variable": name})
        status = getattr(cell.status, "value", cell.status)
        errors = [{"kind": err.kind, "message": err.msg} for err in cell.errors]
        cells.append({"id": cid, "name": cell.name or None, "source": cell.code, "defs": defs, "refs": refs,
            "parents": [{"cell_id": key, "variables": sorted(set(value))} for key, value in parents.items()],
            "children": [{"cell_id": key, "variables": sorted(set(value))} for key, value in children.items()],
            "status": str(status or "unknown"), "output": None, "errors": errors})
    notebook = {"path": __import__("os").environ.get("GRAPHIMO_NOTEBOOK_PATH", ""), "session_id": ""}
    result = {"notebook": notebook, "cells": cells, "edges": edges}
print("GRAPHIMO_RESULT:" + json.dumps(result, default=str, separators=(",", ":")))
"""
        result = self._execute(code)
        result["notebook"] = {"path": self.notebook_path, "session_id": session_id}
        return result

    def add_cell(self, session_id: str, source: str) -> str:
        self._check_session(session_id)
        result = self._execute(
            "import json\nimport marimo._code_mode as cm\n"
            "async with cm.get_context() as ctx:\n"
            f"    cell_id = ctx.create_cell({self._literal(source)}, hide_code=False)\n"
            "print('GRAPHIMO_RESULT:' + json.dumps({'cell_id': str(cell_id)}))"
        )
        return result["cell_id"]

    def inspect_control(self, session_id: str, variable_name: str) -> dict[str, Any]:
        self._check_session(session_id)
        code = (
            "import json\nimport marimo._code_mode as cm\n"
            "async with cm.get_context() as ctx:\n"
            f"    control = ctx.globals[{self._literal(variable_name)}]\n"
            "    result = {'object_id': str(control._id), 'value': control.value}\n"
            "print('GRAPHIMO_RESULT:' + json.dumps(result, default=str, separators=(',', ':')))"
        )
        return self._execute(code)

    def set_control_value(self, session_id: str, variable_name: str, value: Any) -> None:
        self._check_session(session_id)
        control = self.inspect_control(session_id, variable_name)
        payload = json.dumps(
            {"objectIds": [control["object_id"]], "values": [value]},
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/api/kernel/set_ui_element_value",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Marimo-Session-Id": self.session_id,
                "Marimo-Server-Token": self._server_token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status >= 400:
                    raise RuntimeError(f"marimo rejected the control update ({response.status})")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"marimo rejected the control update: {detail or exc.code}") from exc

    def replace_cell_source(self, session_id: str, cell_id: str, source: str) -> dict[str, Any]:
        self._check_session(session_id)
        return self._execute(
            "import json\nimport marimo._code_mode as cm\n"
            "async with cm.get_context() as ctx:\n"
            f"    ctx.edit_cell({self._literal(cell_id)}, code={self._literal(source)})\n"
            "print('GRAPHIMO_RESULT:' + json.dumps({'ok': True}))"
        )

    def run_cell(self, session_id: str, cell_id: str) -> dict[str, Any]:
        self._check_session(session_id)
        return self._execute(
            "import json\nimport marimo._code_mode as cm\n"
            "async with cm.get_context() as ctx:\n"
            f"    ctx.run_cell({self._literal(cell_id)})\n"
            "print('GRAPHIMO_RESULT:' + json.dumps({'status': 'completed'}))"
        )

    def delete_cell(self, session_id: str, cell_id: str) -> dict[str, Any]:
        self._check_session(session_id)
        return self._execute(
            "import json\nimport marimo._code_mode as cm\n"
            "async with cm.get_context() as ctx:\n"
            f"    ctx.delete_cell({self._literal(cell_id)})\n"
            "print('GRAPHIMO_RESULT:' + json.dumps({'deleted': True}))"
        )

    def list_graphimo_nodes(self, session_id: str) -> list[dict[str, Any]]:
        self._check_session(session_id)
        result = self._execute(
            "import graphimo, json\nimport marimo._code_mode as cm\n"
            "async with cm.get_context() as ctx:\n"
            "    nodes = []\n"
            "    for name, value in ctx.globals.items():\n"
            "        if isinstance(name, str) and graphimo.is_node(value):\n"
            "            spec = graphimo.inspect_node(value)\n"
            "            inputs = [{'name': p.name, 'required': p.required, 'annotation': p.annotation, 'has_default': p.has_default, 'default': None} for p in spec.inputs]\n"
            "            nodes.append({'name': name, 'description': (getattr(value, '__doc__', '') or '').splitlines()[0] if getattr(value, '__doc__', None) else None, 'inputs': inputs})\n"
            "print('GRAPHIMO_RESULT:' + json.dumps(nodes))"
        )
        return result

    def inspect_node(self, session_id: str, node_name: str) -> dict[str, Any]:
        self._check_session(session_id)
        code = (
            "import graphimo, json\nimport marimo._code_mode as cm\n"
            "async with cm.get_context() as ctx:\n"
            f"    value = ctx.globals.get({self._literal(node_name)})\n"
            "    if not graphimo.is_node(value): raise KeyError('node_not_found')\n"
            "    spec = graphimo.inspect_node(value)\n"
            "    result = {'inputs': [{'name': p.name, 'required': p.required, 'annotation': p.annotation, 'has_default': p.has_default, 'default': None} for p in spec.inputs], 'outputs': [{'index': p.index, 'declared_type': p.declared_type, 'expressions': list(p.expressions)} for p in spec.outputs], 'diagnostic': spec.diagnostic}\n"
            "print('GRAPHIMO_RESULT:' + json.dumps(result, default=str))"
        )
        return self._execute(code)

    def ensure_node_available(
        self, session_id: str, module_name: str, node_name: str, import_root: str
    ) -> None:
        self._check_session(session_id)
        result = self._execute(
            "import graphimo, json\nimport marimo._code_mode as cm\n"
            "async with cm.get_context() as ctx:\n"
            f"    name = {self._literal(node_name)}\n"
            "    current = ctx.globals.get(name)\n"
            "    if graphimo.is_node(current): cell_id = None\n"
            "    elif name in ctx.globals: raise ValueError('A different notebook value already defines ' + name)\n"
            "    else:\n"
            f"        root = {self._literal(import_root)}\n"
            "        prefix = '__import__(\"sys\").path.insert(0, ' + repr(root) + ')\\n' if root not in __import__('sys').path else ''\n"
            f"        source = prefix + name + ' = __import__(\"importlib\").import_module({self._literal(module_name)}).' + name\n"
            "        cell_id = ctx.create_cell(source, hide_code=False)\n"
            "        ctx.run_cell(cell_id)\n"
            "        cell_id = str(cell_id)\n"
            "print('GRAPHIMO_RESULT:' + json.dumps({'cell_id': cell_id}))"
        )
        if result.get("cell_id") is not None and not isinstance(result["cell_id"], str):
            raise RuntimeError("Marimo returned an invalid node import cell ID")

    def describe_node_result(
        self, session_id: str, result_var: str, node_name: str
    ) -> list[dict[str, Any]]:
        self._check_session(session_id)
        code = (
            "import graphimo, json\nimport marimo._code_mode as cm\n"
            "async with cm.get_context() as ctx:\n"
            f"    result = graphimo.describe_result(ctx.globals[{self._literal(result_var)}], graphimo.inspect_node(ctx.globals[{self._literal(node_name)}]))\n"
            "print('GRAPHIMO_RESULT:' + json.dumps(result, default=str, separators=(',', ':')))"
        )
        return self._execute(code)

    def describe_group_result(
        self, session_id: str, result_var: str, output_names: list[str]
    ) -> list[dict[str, Any]]:
        self._check_session(session_id)
        code = (
            "import graphimo, json\nimport marimo._code_mode as cm\n"
            "async with cm.get_context() as ctx:\n"
            f"    value = ctx.globals[{self._literal(result_var)}]\n"
            f"    names = {self._literal(output_names)}\n"
            "    outputs = []\n"
            "    for name in names:\n"
            "        summary = graphimo.preview_value(value[name])\n"
            "        outputs.append({'index': len(outputs), 'name': name, 'runtime_type': summary['runtime_type'], 'preview': summary['preview'], 'expression': name})\n"
            "print('GRAPHIMO_RESULT:' + json.dumps(outputs, separators=(',', ':')))"
        )
        return self._execute(code)

    def get_variable(self, session_id: str, variable_name: str) -> Any:
        del session_id, variable_name
        raise RuntimeError(
            "Raw Python values never cross the Workbench marimo boundary; use describe_node_result"
        )

    def invoke_metablock(
        self,
        session_id: str,
        *,
        project_path: str,
        project_id: str,
        dialect_id: str,
        metablock_id: str,
        sidecar_path: str,
        action: str,
        args: dict[str, Any],
    ) -> Any:
        """Run an addon action in the kernel that owns its live Python values."""
        payload = {
            "project_path": project_path,
            "project_id": project_id,
            "dialect_id": dialect_id,
            "metablock_id": metablock_id,
            "sidecar_path": sidecar_path,
            "action": action,
            "args": args,
        }
        return self._run_metablock_rpc(session_id, "invoke_metablock", payload)

    def drain_metablock_events(
        self,
        session_id: str,
        *,
        project_path: str,
        project_id: str,
        dialect_id: str,
        metablock_id: str,
        sidecar_path: str,
        cursor: int,
        limit: int,
    ) -> dict[str, Any]:
        """Drain addon events in the kernel process that produced them."""
        payload = {
            "project_path": project_path,
            "project_id": project_id,
            "dialect_id": dialect_id,
            "metablock_id": metablock_id,
            "sidecar_path": sidecar_path,
            "cursor": cursor,
            "limit": limit,
        }
        return self._run_metablock_rpc(session_id, "drain_metablock_events", payload)

    def _run_metablock_rpc(self, session_id: str, function: str, payload: dict[str, Any]) -> Any:
        self._check_session(session_id)
        encoded = self._literal(json.dumps(payload, allow_nan=False, separators=(",", ":")))
        code = (
            "import json\nimport marimo._code_mode as cm\n"
            "import graphimo.metablock_kernel as _graphimo_metablock_kernel\n"
            "async with cm.get_context() as ctx:\n"
            f"    result = _graphimo_metablock_kernel.{function}(marimo_context=ctx, **json.loads({encoded}))\n"
            "print('GRAPHIMO_RESULT:' + json.dumps(result, default=str, allow_nan=False, separators=(',', ':')))"
        )
        return self._execute(code)

    def _check_session(self, session_id: str) -> None:
        if session_id != self.session_id:
            raise KeyError(session_id)

    def close(self) -> None:
        self._closing.set()
        if self._websocket is not None:
            try:
                self._websocket.close()
            except Exception:
                pass
            self._websocket = None
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None

    def __enter__(self) -> MarimoHttpPort:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class MarimoSessionPool:
    """Own one local marimo server per open notebook scope."""

    _SESSION_METHODS = frozenset(
        {
            "inspect",
            "add_cell",
            "inspect_control",
            "set_control_value",
            "replace_cell_source",
            "run_cell",
            "delete_cell",
            "list_graphimo_nodes",
            "inspect_node",
            "ensure_node_available",
            "describe_node_result",
            "describe_group_result",
            "get_variable",
            "invoke_metablock",
            "drain_metablock_events",
        }
    )

    def __init__(self, notebook_path: str | Path, *, port_factory=MarimoHttpPort) -> None:
        self._lock = threading.RLock()
        self._port_factory = port_factory
        root = port_factory(notebook_path)
        self._ports_by_path = {str(Path(root.notebook_path).resolve()): root}
        self._ports_by_session = {root.session_id: root}
        self.notebook_path = root.notebook_path

    @property
    def session_id(self) -> str:
        with self._lock:
            root = self._ports_by_path.get(str(Path(self.notebook_path).resolve()))
            if root is None:
                raise RuntimeError("root marimo session is not open")
            return root.session_id

    def list_active_notebooks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"session_id": port.session_id, "path": port.notebook_path}
                for port in self._ports_by_path.values()
            ]

    def url_for_session(self, session_id: str | None) -> str | None:
        """Return the local editor URL for an owned marimo session."""
        if not isinstance(session_id, str):
            return None
        with self._lock:
            port = self._ports_by_session.get(session_id)
        return port.base_url if port is not None else None

    def open_notebook(self, path: str | Path) -> dict[str, str]:
        resolved = str(Path(path).expanduser().resolve())
        with self._lock:
            port = self._ports_by_path.get(resolved)
            if port is None:
                port = self._port_factory(resolved, initialize_notebook=False)
                self._ports_by_path[resolved] = port
                self._ports_by_session[port.session_id] = port
            return {"session_id": port.session_id, "path": port.notebook_path}

    def __getattr__(self, name: str):
        if name not in self._SESSION_METHODS:
            raise AttributeError(name)

        def route(session_id: str, *args: Any, **kwargs: Any):
            with self._lock:
                port = self._ports_by_session.get(session_id)
            if port is None:
                raise KeyError(session_id)
            return getattr(port, name)(session_id, *args, **kwargs)

        return route

    def close(self) -> None:
        with self._lock:
            ports = tuple(self._ports_by_path.values())
            self._ports_by_path.clear()
            self._ports_by_session.clear()
        for port in ports:
            port.close()
