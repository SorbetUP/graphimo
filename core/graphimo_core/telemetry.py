from __future__ import annotations

import time
from typing import Any, Callable


class TelemetryThrottle:
    """Per-series event rate limit with explicit emitted/dropped counters."""

    def __init__(
        self,
        max_events_per_second: float = 20.0,
        max_series: int = 10_000,
        *,
        max_events: int | None = None,
    ):
        if (
            not isinstance(max_events_per_second, (int, float))
            or not 0 < max_events_per_second <= 1000
        ):
            raise ValueError("max_events_per_second must be greater than 0 and at most 1000")
        # max_events is kept as a compatibility alias from the first prototype.
        if max_events is not None:
            max_series = max_events
        if type(max_series) is not int or not 1 <= max_series <= 100_000:
            raise ValueError("max_series must be between 1 and 100000")
        self.interval = 1.0 / float(max_events_per_second)
        self.max_series = max_series
        self._last: dict[str, float] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self.emitted = 0
        self.dropped = 0

    def emit(self, event: dict[str, Any], sink: Callable[[dict[str, Any]], None]) -> bool:
        kind = event.get("kind", "event")
        payload = event.get("payload", {})
        series = payload.get("name", "") if isinstance(payload, dict) else ""
        run_id = event.get("run_id", "")
        node_id = event.get("node_id", "")
        component_path = event.get("component_path") or ""
        key = f"{run_id}:{node_id}:{kind}:{component_path}:{series}"
        now = time.monotonic()
        previous = self._last.get(key)
        if key not in self._last and len(self._last) >= self.max_series:
            self.dropped += 1
            return False
        if previous is not None and now - previous < self.interval:
            self.dropped += 1
            self._pending[key] = event
            return False
        self._last[key] = now
        self.emitted += 1
        sink(event)
        return True

    def flush(self, sink: Callable[[dict[str, Any]], None]) -> None:
        """Send the latest coalesced event per series before the node result."""
        pending, self._pending = self._pending, {}
        for key, event in pending.items():
            self._last[key] = time.monotonic()
            self.emitted += 1
            sink(event)

    def report(self) -> dict[str, int]:
        return {"emitted": self.emitted, "dropped": self.dropped, "series": len(self._last)}
