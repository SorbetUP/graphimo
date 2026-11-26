"""Non-blocking authenticated telemetry bridge from a marimo kernel to Graphimo."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any

MAX_BATCH_EVENTS = 64
MAX_BATCH_BYTES = 256 * 1024
MAX_PENDING_EVENTS = 2048
_publisher_lock = threading.Lock()
_publisher: KernelEventPublisher | None = None


class KernelEventPublisher:
    """Drain the addon's bounded event bus and push batches on a daemon thread.

    The model execution path only appends to the event bus. Network I/O, retries,
    serialization, and queue pressure are isolated to this publisher thread.
    """

    def __init__(
        self,
        event_bus: Any,
        *,
        service_url: str,
        token: str,
        project_id: str,
        session_id: str = "",
        interval: float = 0.05,
        request_timeout: float = 0.25,
        pending_capacity: int = MAX_PENDING_EVENTS,
    ):
        parsed = urllib.parse.urlsplit(service_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.path not in ("", "/")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("telemetry service URL must be a 127.0.0.1 HTTP endpoint")
        if not token or not project_id:
            raise ValueError("telemetry token and project_id are required")
        if not 0 < interval <= 1 or not 0 < request_timeout <= 2:
            raise ValueError("publisher interval and request timeout must be bounded")
        if type(pending_capacity) is not int or not 1 <= pending_capacity <= MAX_PENDING_EVENTS:
            raise ValueError(f"pending_capacity must be between 1 and {MAX_PENDING_EVENTS}")
        self.event_bus = event_bus
        self.service_url = service_url.rstrip("/")
        self.token = token
        self.project_id = project_id
        self.session_id = session_id
        self.interval = interval
        self.request_timeout = request_timeout
        self.pending_capacity = pending_capacity
        self._cursors: dict[str, str] = {}
        self._pending: deque[dict[str, Any]] = deque()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._events_lost = 0
        self._pending_loss_count = 0
        self._events_sent = 0
        self._reconnects = 0
        self._failures = 0
        self._last_identity: dict[str, Any] | None = None

    def start(self) -> KernelEventPublisher:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="graphimo-pytorch-event-publisher", daemon=True
            )
            self._thread.start()
        return self

    def reconfigure(
        self, *, service_url: str, token: str, project_id: str, session_id: str = ""
    ) -> None:
        parsed = urllib.parse.urlsplit(service_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.path not in ("", "/")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("telemetry service URL must be a 127.0.0.1 HTTP endpoint")
        if not token or not project_id:
            raise ValueError("telemetry token and project_id are required")
        with self._lock:
            self.service_url = service_url.rstrip("/")
            self.token = token
            self.project_id = project_id
            self.session_id = session_id

    def close(self, timeout: float = 1.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, min(timeout, 2.0)))

    def stats(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "pending": len(self._pending),
                "events_lost": self._events_lost,
                "events_sent": self._events_sent,
                "reconnects": self._reconnects,
                "failures": self._failures,
            }

    def _run(self) -> None:
        backoff = self.interval
        while not self._stop.is_set():
            self._collect()
            batch = self._batch()
            if batch:
                try:
                    self._post(batch)
                    with self._lock:
                        self._events_sent += len(batch)
                        if self._failures:
                            self._reconnects += 1
                        self._failures = 0
                    backoff = self.interval
                    continue
                except (OSError, ValueError, urllib.error.URLError, TimeoutError):
                    with self._lock:
                        self._failures += 1
                    self._restore(batch)
                    backoff = min(max(backoff * 2, self.interval), 2.0)
            self._stop.wait(backoff)

    def _collect(self) -> None:
        for metablock_id in self.event_bus.metablock_ids():
            cursor = self._cursors.get(metablock_id, "0")
            try:
                result = self.event_bus.drain(metablock_id, cursor, limit=MAX_BATCH_EVENTS)
            except (TypeError, ValueError):
                continue
            next_cursor = result.get("cursor")
            if isinstance(next_cursor, str):
                self._cursors[metablock_id] = next_cursor
            dropped = result.get("dropped", 0)
            if type(dropped) is int and dropped > 0:
                with self._lock:
                    self._events_lost += dropped
                    self._pending_loss_count += dropped
            for event in result.get("events", []):
                if not isinstance(event, dict):
                    continue
                clean = {**event, "project_id": self.project_id, "session_id": self.session_id}
                self._last_identity = clean
                self._enqueue(clean)

    def _enqueue(self, event: dict[str, Any]) -> None:
        encoded = json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_BATCH_BYTES:
            with self._lock:
                self._events_lost += 1
                self._pending_loss_count += 1
            return
        with self._lock:
            while len(self._pending) >= self.pending_capacity:
                self._pending.popleft()
                self._events_lost += 1
                self._pending_loss_count += 1
            self._pending.append(event)

    def _restore(self, batch: list[dict[str, Any]]) -> None:
        with self._lock:
            for event in reversed(batch):
                while len(self._pending) >= self.pending_capacity:
                    self._pending.pop()
                    self._events_lost += 1
                    self._pending_loss_count += 1
                self._pending.appendleft(event)

    def _batch(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._pending_loss_count and self._last_identity is not None:
                identity = self._last_identity
                count = self._pending_loss_count
                self._pending_loss_count = 0
                while len(self._pending) >= self.pending_capacity:
                    self._pending.pop()
                    self._events_lost += 1
                    count += 1
                self._pending.appendleft(
                    {
                        "schema_version": 1,
                        "event_id": f"publisher-dropped-{time.time_ns()}",
                        "kind": "telemetry.dropped",
                        "timestamp": time.time(),
                        "project_id": self.project_id,
                        "session_id": self.session_id,
                        "metablock_id": identity["metablock_id"],
                        "graph_revision": identity["graph_revision"],
                        "runtime_instance_id": identity["runtime_instance_id"],
                        "logical_element_id": None,
                        "module_path": None,
                        "step": None,
                        "payload": {},
                        "count": count,
                    }
                )
            batch: list[dict[str, Any]] = []
            size = 2
            while self._pending and len(batch) < MAX_BATCH_EVENTS:
                encoded_size = len(
                    json.dumps(self._pending[0], ensure_ascii=False, allow_nan=False).encode(
                        "utf-8"
                    )
                )
                if batch and size + encoded_size > MAX_BATCH_BYTES:
                    break
                batch.append(self._pending.popleft())
                size += encoded_size + 1
            return batch

    def _post(self, batch: list[dict[str, Any]]) -> None:
        body = json.dumps(
            {"project": self.project_id, "events": batch},
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.service_url + "/v1/events/publish",
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                if response.status != 200:
                    raise OSError(f"Graphimo service returned HTTP {response.status}")
                response.read(4096)
        except urllib.error.HTTPError as exc:
            raise OSError(f"Graphimo service returned HTTP {exc.code}") from exc


def configure_kernel_publisher(
    *, service_url: str, token: str, project_id: str, session_id: str = ""
) -> dict[str, Any]:
    """Attach or reconfigure the one ephemeral kernel-to-service publisher."""
    global _publisher
    from .runtime import RUNTIME_REGISTRY

    with _publisher_lock:
        previous = _publisher
        if previous is not None:
            if previous.event_bus is RUNTIME_REGISTRY.event_bus:
                previous.reconfigure(
                    service_url=service_url,
                    token=token,
                    project_id=project_id,
                    session_id=session_id,
                )
                previous.start()
                return previous.stats()
            previous.close()
        _publisher = KernelEventPublisher(
            RUNTIME_REGISTRY.event_bus,
            service_url=service_url,
            token=token,
            project_id=project_id,
            session_id=session_id,
        ).start()
        return _publisher.stats()


def stop_kernel_publisher() -> dict[str, Any]:
    """Stop and detach the publisher without touching live model runtimes."""
    global _publisher
    with _publisher_lock:
        previous = _publisher
        _publisher = None
    if previous is not None:
        previous.close()
        return previous.stats()
    return {
        "running": False,
        "pending": 0,
        "events_lost": 0,
        "events_sent": 0,
        "reconnects": 0,
        "failures": 0,
    }


def kernel_publisher_stats() -> dict[str, Any]:
    with _publisher_lock:
        current = _publisher
    if current is None:
        return {
            "running": False,
            "pending": 0,
            "events_lost": 0,
            "events_sent": 0,
            "reconnects": 0,
            "failures": 0,
        }
    return current.stats()
