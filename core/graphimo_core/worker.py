"""Line-oriented local Python worker used by the vertical slice prototype."""
from __future__ import annotations

import hashlib
import json
import ast
import contextlib
import io
import keyword
import math
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

import numpy as np

from .artifacts import LocalArtifactStore
from .pytorch import load_checkpoint
from .telemetry import TelemetryThrottle


class _GraphimoAPI:
    def __init__(self, emit, store: LocalArtifactStore, run_id: str, node_id: str):
        self._emit = emit
        self._store = store
        self._run_id = run_id
        self._node_id = node_id
        self._log_counter = 0

    def progress(self, fraction: float, message: str = "", *, step: int | None = None) -> None:
        if not isinstance(fraction, (int, float)) or not math.isfinite(float(fraction)):
            raise ValueError("progress fraction must be a finite number")
        self._emit({"kind": "progress", "payload": {"fraction": max(0.0, min(1.0, float(fraction))),
                                                         "message": str(message)[:500]},
                    "step": step, "component_path": None})

    def metric(self, name: str, value: int | float, *, step: int | None = None) -> None:
        if not isinstance(name, str) or not name or len(name) > 200:
            raise ValueError("metric name must be a non-empty string of at most 200 characters")
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("metric value must be finite and numeric")
        self._emit({"kind": "metric", "payload": {"name": name, "value": float(value)},
                    "step": step, "component_path": None})

    def log(self, message: str, *, level: str = "info") -> None:
        if level not in {"debug", "info", "warning", "error"}:
            raise ValueError("invalid log level")
        self._log_counter += 1
        self._emit({"kind": "log", "payload": {"name": f"log/{self._log_counter}",
                                                   "level": level, "message": str(message)[:2000]},
                    "step": None, "component_path": None})

    def put_bytes(self, data: bytes, *, kind: str = "node_output",
                  parent_id: str | None = None) -> dict[str, str]:
        ref = self._store.put_bytes(data, kind=kind, parent_id=parent_id)
        return {"artifact_id": ref.artifact_id, "kind": ref.kind}

    def read_bytes(self, artifact_id: str) -> bytes:
        return self._store.read_bytes(self._store.get(artifact_id))

    def read_file(self, artifact_id: str, relative_path: str) -> bytes:
        return self._store.read_file(self._store.get(artifact_id), relative_path)

    def event(self, kind: str, payload: dict[str, Any], *, step: int | None = None,
              component_path: str | None = None) -> None:
        if kind not in {"progress", "metric", "log", "resource", "model_probe", "warning"}:
            raise ValueError("unsupported Graphimo event kind")
        if not isinstance(payload, dict) or len(json.dumps(payload, allow_nan=False)) > 8_000:
            raise ValueError("event payload must be a JSON object no larger than 8 KB")
        self._emit({"kind": kind, "payload": payload, "step": step,
                    "component_path": component_path[:500] if isinstance(component_path, str) else None})


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


def _execute_node(request: dict, store: LocalArtifactStore, emit) -> dict:
    source = request.get("source")
    inputs = request.get("inputs", {})
    outputs = request.get("outputs", [])
    if not isinstance(source, str) or len(source.encode("utf-8")) > 256_000:
        return {"ok": False, "error": "source must be a string no larger than 256 KB"}
    if not isinstance(inputs, dict) or not isinstance(outputs, list) or len(outputs) > 100:
        return {"ok": False, "error": "inputs must be an object and outputs a list of at most 100 names"}
    names = [*inputs, *outputs]
    if any(not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name) for name in names):
        return {"ok": False, "error": "input and output names must be Python identifiers"}
    if len(set(outputs)) != len(outputs):
        return {"ok": False, "error": "output names must be unique"}
    try:
        code = compile(ast.parse(source), "<graphimo-node>", "exec")
    except SyntaxError as exc:
        return {"ok": False, "error": f"invalid Python source: {exc}"}

    throttle = TelemetryThrottle(request.get("telemetry_hz", 20.0), request.get("max_events", 500))
    run_id = str(request.get("run_id", "local-run"))[:100]
    node_id = str(request.get("node_id", "local-node"))[:100]
    api = _GraphimoAPI(lambda event: throttle.emit(
        {"run_id": run_id, "node_id": node_id, "timestamp": time.time(), **event}, emit),
        store, run_id, node_id)
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
        return {"ok": True, "outputs": values, "elapsed_seconds": time.perf_counter() - started,
                "telemetry": throttle.report()}
    except Exception as exc:
        stdout.flush()
        stderr.flush()
        throttle.flush(emit)
        return {"ok": False, "error": str(exc)[:2000], "traceback": traceback.format_exc(limit=8),
                "telemetry": throttle.report()}


def handle(request: dict, emit=None) -> dict:
    emit = emit or (lambda _event: None)
    if request.get("op") == "shutdown":
        return {"ok": True, "shutdown": True}
    if request.get("op") == "execute_node":
        workspace = Path(request["workspace"]).resolve()
        return _execute_node(request, LocalArtifactStore(workspace / "store"), emit)
    if request.get("op") != "evaluate_checkpoint":
        return {"ok": False, "error": "unknown operation"}
    workspace = Path(request["workspace"]).resolve()
    store = LocalArtifactStore(workspace / "store")
    checkpoint = store.get(request["checkpoint_id"])
    dataset = store.get(request["dataset_id"])
    model, _spec, checkpoint_data = load_checkpoint(store.read_bytes(checkpoint))
    with tempfile.TemporaryDirectory(prefix="graphimo-eval-") as temporary:
        source = Path(temporary) / "input"
        store.materialize(dataset, source)
        with np.load(source / "data.npz") as data:
            x = data["x"].astype(np.float32)
            tensor = __import__("torch").from_numpy(x)
            start = time.perf_counter()
            predictions = model(tensor)
            elapsed = time.perf_counter() - start
            pseudo = predictions[sorted(predictions)[0]].detach().cpu().numpy()
            output = Path(temporary) / "pseudo"
            output.mkdir()
            np.savez_compressed(output / "pseudo_labels.npz", x=x, pseudo=pseudo)
            ref = store.put_directory(output, kind="pseudo_labels", parent_id=dataset.artifact_id)
            return {"ok": True, "artifact_id": ref.artifact_id, "elapsed_seconds": elapsed,
                    "samples": int(len(x)), "prediction_sha256": hashlib.sha256(pseudo.tobytes()).hexdigest(),
                    "checkpoint_metadata": checkpoint_data["metadata"]}


def main() -> int:
    protocol_stdout = sys.stdout
    for line in sys.stdin:
        try:
            request = json.loads(line)
            def send_event(event):
                protocol_stdout.write(json.dumps({"type": "event", "event": event}, allow_nan=False) + "\n")
                protocol_stdout.flush()

            result = handle(request, send_event)
        except Exception as exc:  # keep the worker alive after one bad request
            result = {"ok": False, "error": str(exc), "traceback": traceback.format_exc(limit=4)}
        protocol_stdout.write(json.dumps({"type": "result", "result": result}, sort_keys=True, allow_nan=False) + "\n")
        protocol_stdout.flush()
        if result.get("shutdown"):
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
