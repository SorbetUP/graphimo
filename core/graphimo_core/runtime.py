from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


class LocalPythonWorker:
    """Persistent local worker; it receives artifact IDs, never live tensors."""

    def __init__(self, workspace: str | os.PathLike[str]):
        self.workspace = Path(workspace).resolve()
        self.process: subprocess.Popen[str] | None = None
        self._frames: queue.Queue[dict[str, Any] | None] | None = None
        self._reader: threading.Thread | None = None
        self._request_lock = threading.Lock()

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "graphimo_core.worker"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
        self._frames = queue.Queue()
        process = self.process
        frames = self._frames

        def read_frames() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    try:
                        frames.put(json.loads(line))
                    except json.JSONDecodeError as exc:
                        frames.put({"type": "reader_error", "error": f"invalid worker frame: {exc}"})
            finally:
                frames.put(None)

        self._reader = threading.Thread(target=read_frames, name="graphimo-worker-reader", daemon=True)
        self._reader.start()

    def request(self, payload: dict[str, Any], *, on_event=None,
                timeout_seconds: float | None = None) -> dict[str, Any]:
        if timeout_seconds is not None and (
            not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 86_400
        ):
            raise ValueError("timeout_seconds must be greater than 0 and at most one day")
        with self._request_lock:
            self.start()
            assert self.process and self.process.stdin and self._frames
            frames = self._frames
            request = {**payload, "workspace": str(self.workspace)}
            self.process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
            callback_error: Exception | None = None
            while True:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    self.terminate()
                    raise TimeoutError(f"Graphimo worker request exceeded {timeout_seconds} seconds; worker stopped")
                try:
                    frame = frames.get(timeout=remaining)
                except queue.Empty:
                    self.terminate()
                    raise TimeoutError(f"Graphimo worker request exceeded {timeout_seconds} seconds; worker stopped")
                if frame is None:
                    process = self.process
                    stderr = process.stderr.read() if process and process.stderr else ""
                    self.process = None
                    self._frames = None
                    raise RuntimeError(f"Graphimo worker exited without a response: {stderr[-2000:]}")
                if frame.get("type") == "reader_error":
                    self.terminate()
                    raise RuntimeError(frame.get("error", "Graphimo worker protocol failed"))
                if frame.get("type") == "event":
                    if on_event is not None:
                        try:
                            on_event(frame.get("event", {}))
                        except Exception as exc:
                            callback_error = exc
                    continue
                result = frame.get("result", frame)
                break
        if callback_error is not None:
            raise RuntimeError(f"event callback failed after draining the worker response: {callback_error}")
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "worker operation failed"))
        return result

    def terminate(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None
        self._frames = None

    def cancel(self) -> None:
        """Stop the active process immediately; interrupted node side effects are not rolled back."""
        self.terminate()

    def close(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            try:
                self.request({"op": "shutdown"})
                self.process.wait(timeout=5)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                self.terminate()
        self.process = None
