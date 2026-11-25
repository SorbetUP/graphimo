"""Line-oriented local Python worker used by the vertical slice prototype."""

from __future__ import annotations

import json
import ast
import contextlib
import io
import keyword
import math
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from .artifacts import LocalArtifactStore
from .telemetry import TelemetryThrottle


class _GraphimoAPI:
    def __init__(
        self,
        emit,
        store: LocalArtifactStore,
        run_id: str,
        node_id: str,
        controls: queue.Queue[dict[str, Any]],
    ):
        self._emit = emit
        self._store = store
        self._run_id = run_id
        self._node_id = node_id
        self._controls = controls
        self._log_counter = 0

    def progress(self, fraction: float, message: str = "", *, step: int | None = None) -> None:
        if not isinstance(fraction, (int, float)) or not math.isfinite(float(fraction)):
            raise ValueError("progress fraction must be a finite number")
        self._emit(
            {
                "kind": "progress",
                "payload": {
                    "fraction": max(0.0, min(1.0, float(fraction))),
                    "message": str(message)[:500],
                },
                "step": step,
                "component_path": None,
            }
        )

    def metric(self, name: str, value: int | float, *, step: int | None = None) -> None:
        if not isinstance(name, str) or not name or len(name) > 200:
            raise ValueError("metric name must be a non-empty string of at most 200 characters")
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("metric value must be finite and numeric")
        self._emit(
            {
                "kind": "metric",
                "payload": {"name": name, "value": float(value)},
                "step": step,
                "component_path": None,
            }
        )

    def log(self, message: str, *, level: str = "info") -> None:
        if level not in {"debug", "info", "warning", "error"}:
            raise ValueError("invalid log level")
        self._log_counter += 1
        self._emit(
            {
                "kind": "log",
                "payload": {
                    "name": f"log/{self._log_counter}",
                    "level": level,
                    "message": str(message)[:2000],
                },
                "step": None,
                "component_path": None,
            }
        )

    def put_bytes(
        self, data: bytes, *, kind: str = "node_output", parent_id: str | None = None
    ) -> dict[str, str]:
        ref = self._store.put_bytes(data, kind=kind, parent_id=parent_id)
        return {"artifact_id": ref.artifact_id, "kind": ref.kind}

    def read_bytes(self, artifact_id: str) -> bytes:
        return self._store.read_bytes(self._store.get(artifact_id))

    def read_file(self, artifact_id: str, relative_path: str) -> bytes:
        return self._store.read_file(self._store.get(artifact_id), relative_path)

    def put_file(
        self, path: str | Path, *, kind: str = "node_output", parent_id: str | None = None
    ) -> dict[str, str]:
        ref = self._store.put_file(path, kind=kind, parent_id=parent_id)
        return {"artifact_id": ref.artifact_id, "kind": ref.kind}

    def put_directory(
        self, path: str | Path, *, kind: str = "dataset", parent_id: str | None = None
    ) -> dict[str, str]:
        ref = self._store.put_directory(path, kind=kind, parent_id=parent_id)
        return {"artifact_id": ref.artifact_id, "kind": ref.kind}

    def materialize(self, artifact_id: str, destination: str | Path) -> dict[str, Any]:
        return self._store.materialize(self._store.get(artifact_id), destination)

    def open_artifact(self, artifact_id: str):
        """Materialize an artifact in a temporary directory for one node invocation."""
        import tempfile
        from contextlib import contextmanager

        @contextmanager
        def opened():
            with tempfile.TemporaryDirectory(prefix="graphimo-artifact-") as directory:
                destination = Path(directory) / "content"
                self.materialize(artifact_id, destination)
                yield destination

        return opened()

    def poll_controls(self) -> list[dict[str, Any]]:
        """Drain control commands sent while this node is executing."""
        pending = []
        while True:
            try:
                pending.append(self._controls.get_nowait())
            except queue.Empty:
                return pending

    def event(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        step: int | None = None,
        component_path: str | None = None,
        durable: bool = True,
    ) -> None:
        if kind not in {"progress", "metric", "log", "resource", "model_probe", "warning"}:
            raise ValueError("unsupported Graphimo event kind")
        if not isinstance(payload, dict) or len(json.dumps(payload, allow_nan=False)) > 8_000:
            raise ValueError("event payload must be a JSON object no larger than 8 KB")
        self._emit(
            {
                "kind": kind,
                "payload": payload,
                "step": step,
                "component_path": component_path[:500] if isinstance(component_path, str) else None,
                "durable": bool(durable),
            }
        )


class _EventTextStream(io.TextIOBase):
    def __init__(self, api: _GraphimoAPI, level: str, max_chars: int = 8_000):
        self.api = api
        self.level = level
        self.max_chars = max_chars
        self.buffer = ""
        self.accepted = 0
        self.truncated = False

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("write() argument must be str")
        count = len(text)
        remaining = max(0, self.max_chars - self.accepted)
        accepted = text[:remaining]
        self.accepted += len(accepted)
        self.truncated = self.truncated or len(accepted) < count
        self.buffer += accepted
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line:
                self.api.log(line, level=self.level)
        return count

    def flush(self) -> None:
        if self.buffer:
            self.api.log(self.buffer, level=self.level)
            self.buffer = ""
        if self.truncated:
            self.api.log("[Graphimo truncated further node output]", level="warning")
            self.truncated = False


def _execute_node(
    request: dict, store: LocalArtifactStore, emit, controls: queue.Queue[dict[str, Any]]
) -> dict:
    source = request.get("source")
    inputs = request.get("inputs", {})
    outputs = request.get("outputs", [])
    if not isinstance(source, str) or len(source.encode("utf-8")) > 256_000:
        return {"ok": False, "error": "source must be a string no larger than 256 KB"}
    if not isinstance(inputs, dict) or not isinstance(outputs, list) or len(outputs) > 100:
        return {
            "ok": False,
            "error": "inputs must be an object and outputs a list of at most 100 names",
        }
    names = [*inputs, *outputs]
    if any(
        not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name)
        for name in names
    ):
        return {"ok": False, "error": "input and output names must be Python identifiers"}
    if len(set(outputs)) != len(outputs):
        return {"ok": False, "error": "output names must be unique"}
    try:
        code = compile(ast.parse(source), "<graphimo-node>", "exec")
    except SyntaxError as exc:
        return {"ok": False, "error": f"invalid Python source: {exc}"}

    throttle = TelemetryThrottle(
        request.get("telemetry_hz", 20.0), request.get("max_series", 10_000)
    )
    run_id = str(request.get("run_id", "local-run"))[:100]
    node_id = str(request.get("node_id", "local-node"))[:100]
    api = _GraphimoAPI(
        lambda event: throttle.emit(
            {"run_id": run_id, "node_id": node_id, "timestamp": time.time(), **event}, emit
        ),
        store,
        run_id,
        node_id,
        controls,
    )
    namespace: dict[str, object] = {**inputs, "graphimo": api, "__name__": "__graphimo_node__"}
    stdout, stderr = _EventTextStream(api, "info"), _EventTextStream(api, "warning")
    started = time.perf_counter()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(code, namespace, namespace)
        stdout.flush()
        stderr.flush()
        throttle.flush(emit)
        values = {name: namespace[name] for name in outputs}
        encoded = json.dumps(values, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 64_000:
            raise ValueError("declared outputs exceed the 64 KB JSON output limit")
        return {
            "ok": True,
            "outputs": values,
            "elapsed_seconds": time.perf_counter() - started,
            "telemetry": throttle.report(),
        }
    except Exception as exc:
        stdout.flush()
        stderr.flush()
        throttle.flush(emit)
        return {
            "ok": False,
            "error": str(exc)[:2000],
            "traceback": traceback.format_exc(limit=8),
            "telemetry": throttle.report(),
        }


def main() -> int:
    protocol_stdout = sys.stdout
    protocol_lock = threading.Lock()
    controls: queue.Queue[dict[str, Any]] = queue.Queue()
    active_lock = threading.Lock()
    active = False
    workspace: Path | None = None

    def send(frame: dict[str, Any]) -> None:
        with protocol_lock:
            protocol_stdout.write(json.dumps(frame, allow_nan=False, sort_keys=True) + "\n")
            protocol_stdout.flush()

    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("type") == "control":
                control_id = request.get("control_id")
                command = request.get("command")
                accepted = False
                error = None
                if isinstance(command, dict) and command.get("op") in {
                    "subscribe_probe",
                    "unsubscribe_probe",
                }:
                    with active_lock:
                        accepted = active
                    if accepted:
                        controls.put(command)
                else:
                    error = "unsupported control command"
                send(
                    {
                        "type": "control_ack",
                        "control_id": control_id,
                        "accepted": accepted,
                        "error": error,
                    }
                )
                continue
            if request.get("op") == "shutdown":
                with active_lock:
                    busy = active
                if busy:
                    send({"type": "result", "result": {"ok": False, "error": "worker is busy"}})
                else:
                    send({"type": "result", "result": {"ok": True, "shutdown": True}})
                    return 0
                continue
            if request.get("op") != "execute_node":
                send({"type": "result", "result": {"ok": False, "error": "unknown operation"}})
                continue
            with active_lock:
                if active:
                    send({"type": "result", "result": {"ok": False, "error": "worker is busy"}})
                    continue
                active = True
            while True:
                try:
                    controls.get_nowait()
                except queue.Empty:
                    break
            workspace = Path(request["workspace"]).resolve()

            def run_node(node_request: dict[str, Any], node_workspace: Path) -> None:
                nonlocal active
                try:
                    store = LocalArtifactStore(node_workspace / "store")

                    def send_event(event: dict[str, Any]) -> None:
                        send({"type": "event", "event": event})

                    result = _execute_node(node_request, store, send_event, controls)
                except Exception as exc:
                    result = {
                        "ok": False,
                        "error": str(exc)[:2000],
                        "traceback": traceback.format_exc(limit=4),
                    }
                send({"type": "result", "result": result})
                with active_lock:
                    active = False

            threading.Thread(
                target=run_node,
                args=(request, workspace),
                name="graphimo-node-execution",
                daemon=True,
            ).start()
        except Exception as exc:  # keep the worker alive after one bad request
            send(
                {
                    "type": "result",
                    "result": {
                        "ok": False,
                        "error": str(exc),
                        "traceback": traceback.format_exc(limit=4),
                    },
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
