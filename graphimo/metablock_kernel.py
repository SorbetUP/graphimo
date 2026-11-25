"""Run generic MetaBlock addon actions inside the active marimo kernel.

The Graphimo service owns project semantics, but addon runtime objects live in
the marimo kernel. This module is imported by a short-lived code-mode request in
that kernel so addon actions and event drains see the same process-local state
as materialized model definitions.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from graphimo.artifacts import default_store
from graphimo.event_spool import get_project_event_spool
from graphimo.metablocks import (
    MAX_EVENTS_PER_DRAIN,
    MetaBlockAddonHost,
    MetaBlockContext,
    load_metablock_registration,
)


@dataclass
class _KernelAddon:
    handler: Any
    host: MetaBlockAddonHost
    context: MetaBlockContext


_lock = RLock()
_addons: dict[tuple[str, str, str], _KernelAddon] = {}


def invoke_metablock(
    *,
    marimo_context: Any,
    project_path: str,
    project_id: str,
    dialect_id: str,
    metablock_id: str,
    sidecar_path: str,
    action: str,
    args: dict[str, Any],
) -> Any:
    """Invoke one addon action in the kernel that owns its runtime values."""
    if not isinstance(args, dict):
        raise TypeError("MetaBlock action args must be an object")
    runtime = _get_or_create(
        marimo_context=marimo_context,
        project_path=project_path,
        project_id=project_id,
        dialect_id=dialect_id,
        metablock_id=metablock_id,
        sidecar_path=sidecar_path,
    )
    state = _load_state(Path(sidecar_path))
    result = runtime.handler.invoke(state, action, args, runtime.context)
    from graphimo.metablock_application import _json_safe

    return _json_safe(result)


def drain_metablock_events(
    *,
    marimo_context: Any,
    project_path: str,
    project_id: str,
    dialect_id: str,
    metablock_id: str,
    sidecar_path: str,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    """Drain addon events in their producer process with the shared cursor."""
    if type(cursor) is not int or cursor < 0:
        raise ValueError("event cursor must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= MAX_EVENTS_PER_DRAIN:
        raise ValueError(f"event limit must be between 1 and {MAX_EVENTS_PER_DRAIN}")
    runtime = _get_or_create(
        marimo_context=marimo_context,
        project_path=project_path,
        project_id=project_id,
        dialect_id=dialect_id,
        metablock_id=metablock_id,
        sidecar_path=sidecar_path,
    )
    result = runtime.handler.drain_events(cursor, limit, runtime.context)
    if (
        not isinstance(result, dict)
        or type(result.get("cursor")) is not int
        or result["cursor"] < cursor
        or not isinstance(result.get("events"), list)
        or len(result["events"]) > limit
    ):
        raise ValueError("Addon returned an invalid event batch")
    from graphimo.metablock_application import _json_safe

    return _json_safe(result)


def _get_or_create(
    *,
    marimo_context: Any,
    project_path: str,
    project_id: str,
    dialect_id: str,
    metablock_id: str,
    sidecar_path: str,
) -> _KernelAddon:
    path = Path(sidecar_path).expanduser().resolve()
    key = (str(path), dialect_id, metablock_id)
    with _lock:
        runtime = _addons.get(key)
        if runtime is not None:
            return runtime
        host = MetaBlockAddonHost()
        context = _make_context(
            marimo_context=marimo_context,
            host=host,
            project_path=Path(project_path).expanduser().resolve(),
            project_id=project_id,
            dialect_id=dialect_id,
            metablock_id=metablock_id,
            sidecar_path=path,
        )
        registration = load_metablock_registration(dialect_id)
        handler = registration.factory(context)
        runtime = _KernelAddon(handler=handler, host=host, context=context)
        _addons[key] = runtime
        return runtime


def _make_context(
    *,
    marimo_context: Any,
    host: MetaBlockAddonHost,
    project_path: Path,
    project_id: str,
    dialect_id: str,
    metablock_id: str,
    sidecar_path: Path,
) -> MetaBlockContext:
    spool = get_project_event_spool(project_path, metablock_id)
    navigation = (
        {"id": "root", "title": project_path.name},
        {"id": metablock_id, "title": dialect_id},
    )

    def materialize(cell_id: str, source: str, result_var: str) -> Any:
        if not cell_id or not result_var.isidentifier():
            raise ValueError("MetaBlock materialization identity is invalid")
        managed_source = (
            f"# Graphimo metablock: {dialect_id}\n"
            f"# Graphimo instance: {metablock_id}\n"
            f"{source.rstrip()}\n"
        )
        marker = f"# Graphimo instance: {metablock_id}"
        dialect_marker = f"# Graphimo metablock: {dialect_id}"
        owned = [
            cell
            for cell in marimo_context.cells
            if marker in str(getattr(cell, "code", "")).splitlines()
            and dialect_marker in str(getattr(cell, "code", "")).splitlines()
        ]
        if len(owned) > 1:
            raise ValueError("Multiple managed marimo cells exist for this MetaBlock")
        if owned:
            return marimo_context.edit_cell(str(owned[0].id), code=managed_source)
        if any(str(cell.id) == cell_id for cell in marimo_context.cells):
            raise ValueError("Refusing to replace a marimo cell not owned by this MetaBlock")
        return marimo_context.create_cell(managed_source, hide_code=False)

    def save_sidecar(state: Any) -> dict[str, Any]:
        normalized = json.loads(json.dumps(state, allow_nan=False))
        if not isinstance(normalized, dict):
            raise TypeError("MetaBlock sidecar state must be a JSON object")
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{sidecar_path.name}.", dir=sidecar_path.parent, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(normalized, stream, indent=2, sort_keys=True, allow_nan=False)
                stream.write("\n")
            os.replace(temporary, sidecar_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return normalized

    def emit_event(event: dict[str, Any]) -> None:
        host.emit_event(event)
        record = dict(event)
        record.setdefault("metablock_id", metablock_id)
        spool.submit(record)

    return MetaBlockContext(
        project_id=project_id,
        project_path=str(project_path),
        sidecar_path=sidecar_path,
        artifact_store=default_store(),
        scope_id=metablock_id,
        navigation=navigation,
        _event_sink=emit_event,
        _event_drain=host.drain_local_events,
        _sidecar_load=lambda: _load_state(sidecar_path),
        _sidecar_save=save_sidecar,
        _materialize=materialize,
        _diagnostic_sink=host.add_diagnostic,
    )


def _load_state(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("MetaBlock sidecar root must be a JSON object")
    return value


__all__ = ["drain_metablock_events", "invoke_metablock"]
