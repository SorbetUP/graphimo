"""Bounded, non-blocking producer and cursor-based consumer event journal."""

from __future__ import annotations

import json
import time
from collections import deque
from threading import Condition
from typing import Any

from .protocol import MAX_EVENT_BYTES, MAX_EVENT_LIMIT, MAX_EVENT_WAIT_MS, MAX_RESPONSE_BYTES


class EventJournal:
    def __init__(self, capacity: int = 512) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("event capacity must be a positive integer")
        self._capacity = capacity
        self._events: deque[tuple[int, dict[str, Any]]] = deque(maxlen=capacity)
        self._next_cursor = 0
        self._condition = Condition()

    def publish(self, event: dict[str, Any]) -> int:
        if not isinstance(event, dict):
            raise TypeError("service events must be JSON objects")
        encoded = json.dumps(event, allow_nan=False, separators=(",", ":")).encode("utf-8")
        normalized = json.loads(encoded)
        if len(encoded) > MAX_EVENT_BYTES:
            normalized = {"type": "event.truncated", "original_bytes": len(encoded)}
        with self._condition:
            cursor = self._next_cursor
            self._next_cursor += 1
            self._events.append((cursor, normalized))
            self._condition.notify_all()
            return cursor

    def read(self, cursor: int = 0, *, limit: int = 100, wait_ms: int = 0) -> dict[str, Any]:
        if type(cursor) is not int or cursor < 0:
            raise ValueError("event cursor must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= MAX_EVENT_LIMIT:
            raise ValueError(f"event limit must be between 1 and {MAX_EVENT_LIMIT}")
        if type(wait_ms) is not int or not 0 <= wait_ms <= MAX_EVENT_WAIT_MS:
            raise ValueError(f"event wait must be between 0 and {MAX_EVENT_WAIT_MS} ms")
        deadline = time.monotonic() + wait_ms / 1000
        with self._condition:
            if cursor > self._next_cursor:
                raise ValueError("event cursor is ahead of the current stream")
            while True:
                first = self._events[0][0] if self._events else self._next_cursor
                selected = [(index, item) for index, item in self._events if index >= cursor][
                    :limit
                ]
                if selected or wait_ms == 0:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            dropped = max(0, first - cursor)
            event_values = [item for _, item in selected]
            event_sizes = [
                len(json.dumps(item, separators=(",", ":")).encode("utf-8"))
                for item in event_values
            ]
            while selected:
                page = {
                    "cursor": selected[-1][0] + 1,
                    "events": event_values,
                    "dropped": dropped,
                }
                header = len(
                    json.dumps(
                        {"cursor": page["cursor"], "events": [], "dropped": dropped},
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                size = header + sum(event_sizes) + max(0, len(event_sizes) - 1)
                if size <= MAX_RESPONSE_BYTES:
                    return page
                selected.pop()
                event_values.pop()
                removed_size = event_sizes.pop()
                if event_sizes:
                    removed_size += 1
                # The updated cursor may have a different encoded length.
                next_cursor = selected[-1][0] + 1 if selected else max(cursor, first)
                next_header = len(
                    json.dumps(
                        {"cursor": next_cursor, "events": [], "dropped": dropped},
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                size -= removed_size
                size += next_header - header
            return {"cursor": max(cursor, first), "events": [], "dropped": dropped}
