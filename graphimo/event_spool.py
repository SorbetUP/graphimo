"""Bounded, non-blocking event relay between a marimo kernel and Graphimo service."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import queue
import tempfile
import threading
from pathlib import Path
from typing import Any

MAX_SPOOL_EVENTS = 4096
MAX_SPOOL_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_PENDING_EVENTS = 2048


class ProjectEventSpool:
    """Append events from a producer thread; readers use a monotonic cursor."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.cursor_path = self.path.with_suffix(self.path.suffix + ".cursor")
        self.path.touch(exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        with self._file_lock(exclusive=False):
            records = self._read_records()
            self._next_sequence = self._read_next_sequence(records)
            self._retained_count = len(records)
        self._pending: queue.Queue[dict[str, Any]] = queue.Queue(MAX_PENDING_EVENTS)
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._write_loop, daemon=True, name="graphimo-events"
        )
        self._worker.start()

    def submit(self, event: dict[str, Any]) -> bool:
        """Queue a JSON event without waiting for disk or a UI consumer."""
        try:
            normalized = json.loads(json.dumps(event, allow_nan=False, separators=(",", ":")))
            encoded_size = len(json.dumps(normalized, allow_nan=False, separators=(",", ":")))
            if encoded_size > MAX_EVENT_BYTES:
                raise ValueError("event exceeds the Graphimo event relay limit")
            self._pending.put_nowait(normalized)
            return True
        except (TypeError, ValueError, queue.Full):
            with self._dropped_lock:
                self._dropped += 1
            return False

    def drain(self, cursor: int, limit: int = 100) -> dict[str, Any]:
        if type(cursor) is not int or cursor < 0:
            raise ValueError("event cursor must be a non-negative integer")
        if type(limit) is not int or limit < 1:
            raise ValueError("event limit must be a positive integer")
        with self._file_lock(exclusive=False):
            records = self._read_records()
            next_sequence = self._read_next_sequence(records)
            first_sequence = int(records[0]["sequence"]) if records else next_sequence
            selected = [record for record in records if int(record["sequence"]) >= cursor][:limit]
            next_cursor = (
                int(selected[-1]["sequence"]) + 1 if selected else max(cursor, first_sequence)
            )
            dropped = max(0, first_sequence - cursor)
        with self._dropped_lock:
            dropped += self._dropped
            self._dropped = 0
        return {
            "cursor": next_cursor,
            "events": [record["event"] for record in selected],
            "dropped": dropped,
        }

    def _write_loop(self) -> None:
        while True:
            event = self._pending.get()
            try:
                self._append(event)
            except (OSError, ValueError, TypeError):
                with self._dropped_lock:
                    self._dropped += 1
            finally:
                self._pending.task_done()

    def _append(self, event: dict[str, Any]) -> None:
        with self._file_lock(exclusive=True):
            record = {"sequence": self._next_sequence, "event": event}
            line = json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n"
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            self._next_sequence += 1
            self._retained_count += 1
            if (
                self.path.stat().st_size > MAX_SPOOL_BYTES
                or self._retained_count > MAX_SPOOL_EVENTS
            ):
                self._trim()

    def _read_records(self) -> list[dict[str, Any]]:
        records = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return records
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(value, dict)
                and type(value.get("sequence")) is int
                and isinstance(value.get("event"), dict)
            ):
                records.append(value)
        return records

    def _read_next_sequence(self, records: list[dict[str, Any]]) -> int:
        last_written = int(records[-1]["sequence"]) + 1 if records else 0
        try:
            stored = int(self.cursor_path.read_text(encoding="ascii"))
        except (FileNotFoundError, ValueError, OSError):
            stored = 0
        return max(last_written, stored)

    def _trim(self) -> None:
        records = self._read_records()
        keep_count = max(1, MAX_SPOOL_EVENTS * 3 // 4)
        records = records[-keep_count:]
        while (
            records
            and sum(
                len(json.dumps(item, allow_nan=False, separators=(",", ":"))) + 1
                for item in records
            )
            > MAX_SPOOL_BYTES
        ):
            records.pop(0)
        fd, temporary = tempfile.mkstemp(prefix=".event-spool-", dir=self.path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                for record in records:
                    stream.write(json.dumps(record, allow_nan=False, separators=(",", ":")))
                    stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._retained_count = len(records)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _file_lock(self, *, exclusive: bool):
        return _LockedFile(self.lock_path, exclusive=exclusive)


class _LockedFile:
    def __init__(self, path: Path, *, exclusive: bool):
        self.path = path
        self.exclusive = exclusive
        self.stream = None

    def __enter__(self):
        self.stream = self.path.open("a+")
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH)
        return self.stream

    def __exit__(self, *_args: Any) -> None:
        assert self.stream is not None
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


_spools: dict[tuple[str, str], ProjectEventSpool] = {}
_spools_lock = threading.Lock()


def event_spool_path(project_path: str | Path, metablock_id: str) -> Path:
    project = Path(project_path).expanduser().resolve()
    digest = hashlib.sha256(metablock_id.encode("utf-8")).hexdigest()[:24]
    return project.parent / ".graphimo" / "events" / f"{digest}.jsonl"


def get_project_event_spool(project_path: str | Path, metablock_id: str) -> ProjectEventSpool:
    project = str(Path(project_path).expanduser().resolve())
    key = (project, metablock_id)
    with _spools_lock:
        spool = _spools.get(key)
        if spool is None:
            spool = ProjectEventSpool(event_spool_path(project, metablock_id))
            _spools[key] = spool
        return spool


def project_event_spool_exists(project_path: str | Path, metablock_id: str) -> bool:
    return event_spool_path(project_path, metablock_id).is_file()


def drain_project_event_spool(
    project_path: str | Path, metablock_id: str, cursor: int, limit: int
) -> dict[str, Any] | None:
    path = event_spool_path(project_path, metablock_id)
    if not path.is_file():
        return None
    spool = ProjectEventSpool.__new__(ProjectEventSpool)
    spool.path = path
    spool.lock_path = path.with_suffix(path.suffix + ".lock")
    spool.cursor_path = path.with_suffix(path.suffix + ".cursor")
    spool._dropped = 0
    spool._dropped_lock = threading.Lock()
    return spool.drain(cursor, limit)


__all__ = [
    "ProjectEventSpool",
    "drain_project_event_spool",
    "event_spool_path",
    "get_project_event_spool",
    "project_event_spool_exists",
]
