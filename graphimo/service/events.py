"""Bounded, non-blocking event journal for local Graphimo service sessions."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import Any

MAX_EVENT_BYTES = 64 * 1024
MAX_EVENT_LIMIT = 256
MAX_WAIT_MS = 1000


class EventJournal:
    """Cursor-based event history whose producers never wait for consumers."""

    def __init__(self, *, capacity: int = 10_000):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("event capacity must be a positive integer")
        self._events: deque[tuple[int, dict[str, Any]]] = deque(maxlen=capacity)
        self._next_sequence = 1
        self._closed = False
        self._condition = threading.Condition()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def publish(self, event: Mapping[str, Any]) -> int:
        if not isinstance(event, Mapping):
            raise TypeError("service events must be objects")
        try:
            encoded = json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_EVENT_BYTES:
                raise ValueError(f"service event exceeds the {MAX_EVENT_BYTES}-byte limit")
            normalized = json.loads(encoded)
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ValueError(f"service event must be bounded JSON data: {exc}") from exc
        with self._condition:
            if self._closed:
                return 0
            sequence = self._next_sequence
            self._next_sequence += 1
            self._events.append((sequence, normalized))
            self._condition.notify_all()
            return sequence

    def read(
        self,
        cursor: str | int | None = None,
        *,
        limit: int = 128,
        wait_ms: int = 0,
    ) -> dict[str, Any]:
        sequence = _parse_cursor(cursor)
        if type(limit) is not int or not 1 <= limit <= MAX_EVENT_LIMIT:
            raise ValueError(f"event limit must be between 1 and {MAX_EVENT_LIMIT}")
        if type(wait_ms) is not int or not 0 <= wait_ms <= MAX_WAIT_MS:
            raise ValueError(f"wait_ms must be between 0 and {MAX_WAIT_MS}")

        deadline = time.monotonic() + wait_ms / 1000
        with self._condition:
            while not self._closed and not any(
                item_sequence > sequence for item_sequence, _ in self._events
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)

            oldest = self._events[0][0] if self._events else self._next_sequence
            dropped = max(0, oldest - sequence - 1)
            selected = [
                (item_sequence, event)
                for item_sequence, event in self._events
                if item_sequence > sequence
            ][:limit]
            next_cursor = selected[-1][0] if selected else max(sequence, oldest - 1)
            events = [{**event, "sequence": item_sequence} for item_sequence, event in selected]
            return {
                "cursor": str(next_cursor),
                "events": events,
                "dropped": dropped,
            }

    def read_matching(
        self,
        cursor: str | int | None = None,
        *,
        metablock_id: str,
        source: str,
        limit: int = 128,
        wait_ms: int = 0,
    ) -> dict[str, Any]:
        sequence = _parse_cursor(cursor)
        if type(limit) is not int or not 1 <= limit <= MAX_EVENT_LIMIT:
            raise ValueError(f"event limit must be between 1 and {MAX_EVENT_LIMIT}")
        if type(wait_ms) is not int or not 0 <= wait_ms <= MAX_WAIT_MS:
            raise ValueError(f"wait_ms must be between 0 and {MAX_WAIT_MS}")
        deadline = time.monotonic() + wait_ms / 1000

        def matches(event: dict[str, Any]) -> bool:
            return event.get("metablock_id") == metablock_id and event.get("source") == source

        with self._condition:
            while not self._closed and not any(
                item_sequence > sequence and matches(event) for item_sequence, event in self._events
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            oldest = self._events[0][0] if self._events else self._next_sequence
            dropped = max(0, oldest - sequence - 1)
            selected = [
                (item_sequence, event)
                for item_sequence, event in self._events
                if item_sequence > sequence and matches(event)
            ][:limit]
            next_cursor = selected[-1][0] if selected else max(sequence, oldest - 1)
            return {
                "cursor": str(next_cursor),
                "events": [
                    {**event, "sequence": item_sequence} for item_sequence, event in selected
                ],
                "dropped": dropped,
            }

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


def _parse_cursor(value: str | int | None) -> int:
    if value is None:
        return 0
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        parsed = int(value)
    else:
        raise ValueError("event cursor must be a non-negative decimal sequence")
    if parsed < 0:
        raise ValueError("event cursor must be a non-negative decimal sequence")
    return parsed
