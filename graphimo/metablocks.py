"""Generic, bounded host contract for optional MetaBlock addons.

Addon entry points are discovered without importing them. An addon is imported
only when a project actually contains state owned by that dialect or creates a
new MetaBlock of that dialect.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

MAX_DIAGNOSTICS = 200
MAX_EVENTS_PER_DRAIN = 500


@dataclass(frozen=True, slots=True)
class MetaBlockContext:
    """Bounded host services provided to a loaded addon handler."""

    project_id: str
    project_path: str
    sidecar_path: Path
    artifact_store: Any
    scope_id: str
    navigation: tuple[dict[str, str], ...]
    _event_sink: Callable[[dict[str, Any]], None] = field(repr=False, compare=False)
    _event_drain: Callable[[int, int], dict[str, Any]] = field(repr=False, compare=False)
    _sidecar_load: Callable[[], dict[str, Any] | None] = field(repr=False, compare=False)
    _sidecar_save: Callable[[Any], dict[str, Any]] = field(repr=False, compare=False)
    _materialize: Callable[[str, str, str], Any] = field(repr=False, compare=False)
    _diagnostic_sink: Callable[[str, str], None] = field(repr=False, compare=False)

    def emit_event(self, event: dict[str, Any]) -> None:
        """Emit one JSON-safe addon event through the host's bounded queue."""
        self._event_sink(event)

    def drain_host_events(self, cursor: int, limit: int = 100) -> dict[str, Any]:
        """Read the bounded host event stream using a monotonic cursor."""
        return self._event_drain(cursor, limit)

    def load_sidecar(self) -> dict[str, Any] | None:
        """Read addon-owned JSON without interpreting its schema."""
        return self._sidecar_load()

    def save_sidecar(self, state: Any) -> dict[str, Any]:
        """Atomically persist a JSON-compatible addon state object."""
        return self._sidecar_save(state)

    def diagnostic(self, code: str, message: str) -> None:
        """Add a bounded renderer-safe diagnostic."""
        self._diagnostic_sink(code, message)

    def update_materialization_cell(self, cell_id: str, source: str, result_var: str) -> Any:
        """Insert or update the host-owned marimo cell for this addon value."""
        return self._materialize(cell_id, source, result_var)

    @property
    def logger(self) -> logging.Logger:
        """Return an addon-scoped logger (handlers remain host controlled)."""
        return logging.getLogger("graphimo.addons")


# Transitional import compatibility for the separately packaged P1-P9 addon.
# This aliases the concrete generic context, replacing the former empty Protocol.
ExperimentalGraphimoAPI = MetaBlockContext


@dataclass(frozen=True, slots=True)
class MaterializationSpec:
    """Canonical source needed to create an addon value inside marimo."""

    source: str
    result_var: str
    output_type: str
    output_label: str
    bindings: dict[str, str] = field(default_factory=dict)
    state_revision: str = ""

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.result_var.isidentifier():
            raise ValueError("materialization requires source and a valid result variable")
        if not self.output_type or not self.output_label:
            raise ValueError("materialization requires declared output type and label")
        if not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.bindings.items()
        ):
            raise TypeError("materialization bindings must map strings to strings")


@runtime_checkable
class MetaBlockDialect(Protocol):
    """Addon-owned semantics for one specialized Graphimo scope."""

    dialect_id: str

    def create_state(self, request: dict[str, Any], context: MetaBlockContext) -> Any: ...
    def catalog(self, state: Any, scope: dict[str, Any], context: MetaBlockContext) -> Any: ...
    def project(self, state: Any, scope: dict[str, Any], context: MetaBlockContext) -> Any: ...
    def apply(self, state: Any, intent: dict[str, Any], context: MetaBlockContext) -> Any: ...
    def build_materialization(
        self, state: Any, bindings: dict[str, str], context: MetaBlockContext
    ) -> MaterializationSpec: ...
    def invoke(
        self, state: Any, action: str, args: dict[str, Any], context: MetaBlockContext
    ) -> Any: ...
    def drain_events(self, cursor: int, limit: int, context: MetaBlockContext) -> Any: ...


@dataclass(frozen=True, slots=True)
class MetaDialectRegistration:
    dialect_id: str
    addon_version: str
    api_version: int
    factory: Callable[[MetaBlockContext], MetaBlockDialect]

    def __post_init__(self) -> None:
        if not self.dialect_id or not self.addon_version:
            raise ValueError("MetaDialectRegistration requires dialect and addon version")
        if type(self.api_version) is not int or self.api_version < 1:
            raise ValueError("api_version must be a positive integer")
        if not callable(self.factory):
            raise TypeError("factory must be callable")


class MetaBlockAddonHost:
    """Small runtime helpers shared by GraphimoApplication and addons."""

    def __init__(self) -> None:
        self._events: deque[tuple[int, dict[str, Any]]] = deque(maxlen=MAX_DIAGNOSTICS)
        self._next_event_cursor = 0
        self._diagnostics: deque[dict[str, str]] = deque(maxlen=MAX_DIAGNOSTICS)

    def emit_event(self, event: dict[str, Any]) -> None:
        # JSON serialization also prevents addon-specific live objects escaping.
        normalized = json.loads(json.dumps(event, allow_nan=False))
        self._events.append((self._next_event_cursor, normalized))
        self._next_event_cursor += 1

    def drain_local_events(self, cursor: int, limit: int = MAX_EVENTS_PER_DRAIN) -> dict[str, Any]:
        if type(cursor) is not int or cursor < 0:
            raise ValueError("event cursor must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= MAX_EVENTS_PER_DRAIN:
            raise ValueError(f"event limit must be between 1 and {MAX_EVENTS_PER_DRAIN}")
        first_available = self._events[0][0] if self._events else self._next_event_cursor
        selected = [(sequence, event) for sequence, event in self._events if sequence >= cursor][
            :limit
        ]
        next_cursor = selected[-1][0] + 1 if selected else max(cursor, first_available)
        return {
            "cursor": next_cursor,
            "events": [event for _, event in selected],
            "dropped": max(0, first_available - cursor),
        }

    def add_diagnostic(self, code: str, message: str) -> None:
        self._diagnostics.append({"code": str(code)[:120], "message": str(message)[:2000]})

    @property
    def diagnostics(self) -> tuple[dict[str, str], ...]:
        return tuple(self._diagnostics)


def discover_metablock_addons() -> tuple[EntryPoint, ...]:
    """Return installed entry points without importing or initializing addons."""
    return tuple(entry_points(group="graphimo.metablocks"))


def load_metablock_registration(dialect_id: str) -> MetaDialectRegistration:
    """Load and validate one installed dialect registration on demand."""
    entries = discover_metablock_addons()
    # Prefer the cheap exact-key route. Dialect IDs may differ from distribution
    # entry-point keys; only then import candidates, and only on explicit use.
    candidates = [entry for entry in entries if entry.name == dialect_id] or list(entries)
    for entry in candidates:
        registration = entry.load()
        if not isinstance(registration, MetaDialectRegistration):
            raise TypeError(
                f"MetaBlock entry point {entry.name!r} did not expose MetaDialectRegistration"
            )
        if registration.dialect_id != dialect_id and entry.name != dialect_id:
            continue
        return registration
    raise LookupError(f"MetaBlock dialect {dialect_id!r} is not installed")


__all__ = [
    "MAX_DIAGNOSTICS",
    "MAX_EVENTS_PER_DRAIN",
    "MaterializationSpec",
    "MetaBlockAddonHost",
    "MetaBlockContext",
    "MetaBlockDialect",
    "MetaDialectRegistration",
    "ExperimentalGraphimoAPI",
    "discover_metablock_addons",
    "load_metablock_registration",
]
