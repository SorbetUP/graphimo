"""Generic MetaBlock lifecycle methods mixed into GraphimoApplication."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from graphimo.metablocks import (
    MAX_EVENTS_PER_DRAIN,
    MaterializationSpec,
    MetaBlockAddonHost,
    MetaBlockContext,
    MetaBlockDialect,
    load_metablock_registration,
)
from graphimo.errors import ApplicationError


class MetaBlockApplicationMixin:
    """Expose the shared addon contract without adding addon semantics to Core."""

    def _init_metablocks(self) -> None:
        self._metablock_host = MetaBlockAddonHost()
        self._metablock_handlers: dict[str, MetaBlockDialect] = {}

    def _close_metablocks(self) -> None:
        self._metablock_handlers.clear()

    def list_metablock_addons(self) -> list[dict[str, str | None]]:
        """List installed entry points without importing addon code."""
        result = []
        from graphimo.metablocks import discover_metablock_addons

        entries = discover_metablock_addons()
        for entry in entries:
            distribution = getattr(entry, "dist", None)
            result.append(
                {
                    "entry_point": entry.name,
                    "distribution": getattr(distribution, "name", None),
                    "version": getattr(distribution, "version", None),
                }
            )
        return result

    def create_metablock(
        self, dialect_id: str, request: dict[str, Any], metablock_id: str
    ) -> dict[str, Any]:
        try:
            registration = load_metablock_registration(dialect_id)
            if registration.api_version != 1:
                raise ValueError(f"Unsupported MetaBlock API version: {registration.api_version}")
            canonical_dialect = getattr(registration, "dialect_id", dialect_id)
            handler = self._metablock_handlers.get(canonical_dialect)
            context = self._metablock_context(canonical_dialect, metablock_id)
            if handler is None:
                handler = registration.factory(context)
                if not isinstance(handler, MetaBlockDialect):
                    raise TypeError("Addon handler does not implement the Graphimo MetaBlock API")
                self._metablock_handlers[canonical_dialect] = handler
        except (LookupError, ImportError, TypeError, ValueError, RuntimeError) as exc:
            raise ApplicationError("metablock_addon_unavailable", str(exc)) from exc
        if context.sidecar_path.exists():
            raise ApplicationError("metablock_exists", f"MetaBlock {metablock_id!r} already exists")
        state = handler.create_state({**request, "metablock_id": metablock_id}, context)
        return self._save_metablock_state(canonical_dialect, metablock_id, state)

    def metablock_catalog(self, metablock_id: str, scope: dict[str, Any] | None = None) -> Any:
        dialect_id, state = self._load_metablock_state(metablock_id)
        handler, context = self._metablock_handler(dialect_id, metablock_id, scope=scope)
        return _json_safe(handler.catalog(state, scope or {}, context))

    def project_metablock(self, metablock_id: str, scope: dict[str, Any] | None = None) -> Any:
        dialect_id, state = self._load_metablock_state(metablock_id)
        handler, context = self._metablock_handler(dialect_id, metablock_id, scope=scope)
        return _json_safe(handler.project(state, scope or {}, context))

    def apply_metablock(self, metablock_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        dialect_id, state = self._load_metablock_state(metablock_id)
        scope = intent.get("scope") if isinstance(intent.get("scope"), dict) else None
        handler, context = self._metablock_handler(dialect_id, metablock_id, scope=scope)
        result = handler.apply(state, intent, context)
        diagnostics: Any = ()
        invalidated = True
        if isinstance(result, tuple) and len(result) == 3:
            next_state, diagnostics, invalidated = result
        elif isinstance(result, dict) and "state" in result:
            next_state = result["state"]
            diagnostics = result.get("diagnostics", ())
            invalidated = result.get("projection_invalidated", True)
        else:
            next_state = result
        saved = self._save_metablock_state(dialect_id, metablock_id, next_state)
        return {
            "state": saved,
            "diagnostics": _json_safe(diagnostics),
            "projection_invalidated": bool(invalidated),
        }

    def build_metablock_materialization(
        self, metablock_id: str, bindings: dict[str, str]
    ) -> MaterializationSpec:
        dialect_id, state = self._load_metablock_state(metablock_id)
        handler, context = self._metablock_handler(dialect_id, metablock_id)
        spec = handler.build_materialization(state, bindings, context)
        if not isinstance(spec, MaterializationSpec):
            raise ApplicationError(
                "invalid_metablock_materialization",
                "Addon returned an invalid materialization specification",
            )
        return spec

    def materialize_metablock(self, metablock_id: str, bindings: dict[str, str]) -> dict[str, Any]:
        """Build and update the host-owned marimo cell for an addon value."""
        spec = self.build_metablock_materialization(metablock_id, bindings)
        dialect_id, _ = self._load_metablock_state(metablock_id)
        _handler, context = self._metablock_handler(dialect_id, metablock_id)
        cell_id = context.update_materialization_cell(metablock_id, spec.source, spec.result_var)
        return {"cell_id": cell_id, "materialization": spec}

    def invoke_metablock(
        self, metablock_id: str, action: str, args: dict[str, Any] | None = None
    ) -> Any:
        dialect_id, state = self._load_metablock_state(metablock_id)
        handler, context = self._metablock_handler(dialect_id, metablock_id)
        return _json_safe(handler.invoke(state, action, args or {}, context))

    def drain_metablock_events(self, metablock_id: str, cursor: int = 0, limit: int = 100) -> Any:
        if type(limit) is not int or not 1 <= limit <= MAX_EVENTS_PER_DRAIN:
            raise ApplicationError(
                "invalid_event_limit", f"Event limit must be between 1 and {MAX_EVENTS_PER_DRAIN}"
            )
        if type(cursor) is not int or cursor < 0:
            raise ApplicationError(
                "invalid_event_cursor", "Event cursor must be a non-negative integer"
            )
        dialect_id, state = self._load_metablock_state(metablock_id)
        handler, context = self._metablock_handler(dialect_id, metablock_id)
        result = _json_safe(handler.drain_events(cursor, limit, context))
        if (
            not isinstance(result, dict)
            or type(result.get("cursor")) is not int
            or result["cursor"] < cursor
            or not isinstance(result.get("events"), list)
            or len(result["events"]) > limit
        ):
            raise ApplicationError(
                "invalid_metablock_events", "Addon returned an invalid event batch"
            )
        return result

    def _metablock_handler(
        self, dialect_id: str, metablock_id: str, *, scope: dict[str, Any] | None = None
    ) -> tuple[MetaBlockDialect, MetaBlockContext]:
        if self._project_path is None:
            raise ApplicationError(
                "project_not_open", "Open a Graphimo project before using MetaBlocks"
            )
        handler = self._metablock_handlers.get(dialect_id)
        if handler is None:
            try:
                registration = load_metablock_registration(dialect_id)
                if registration.api_version != 1:
                    raise ValueError(
                        f"Unsupported MetaBlock API version: {registration.api_version}"
                    )
                handler = registration.factory(
                    self._metablock_context(dialect_id, metablock_id, scope=scope)
                )
                if not isinstance(handler, MetaBlockDialect):
                    raise TypeError("Addon handler does not implement the Graphimo MetaBlock API")
            except (LookupError, ImportError, TypeError, ValueError, RuntimeError) as exc:
                raise ApplicationError("metablock_addon_unavailable", str(exc)) from exc
            self._metablock_handlers[dialect_id] = handler
        return handler, self._metablock_context(dialect_id, metablock_id, scope=scope)

    def _metablock_context(
        self, dialect_id: str, metablock_id: str, *, scope: dict[str, Any] | None = None
    ) -> MetaBlockContext:
        from graphimo.artifacts import default_store

        project_path = Path(self._project_path)
        sidecar = self._metablock_sidecar_path(metablock_id, dialect_id)
        scope = scope or {}
        scope_id = scope.get("id") if isinstance(scope.get("id"), str) else metablock_id
        requested_navigation = scope.get("navigation")
        navigation = (
            tuple(
                {"id": item["id"], "title": item["title"]}
                for item in requested_navigation
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and isinstance(item.get("title"), str)
            )
            if isinstance(requested_navigation, list)
            else (
                {"id": "root", "title": project_path.name},
                {"id": metablock_id, "title": dialect_id},
            )
        )
        return MetaBlockContext(
            project_id=str(project_path),
            project_path=str(project_path),
            sidecar_path=sidecar,
            artifact_store=default_store(),
            scope_id=scope_id,
            navigation=navigation,
            _event_sink=self._metablock_host.emit_event,
            _event_drain=self._metablock_host.drain_local_events,
            _sidecar_load=lambda: self._read_metablock_sidecar(sidecar),
            _sidecar_save=lambda state: self._save_metablock_state(dialect_id, metablock_id, state),
            _materialize=lambda cell_id, source, result_var: self._update_metablock_materialization(
                cell_id, source, result_var, dialect_id=dialect_id, metablock_id=metablock_id
            ),
            _diagnostic_sink=self._metablock_host.add_diagnostic,
        )

    def _metablock_sidecar_path(self, metablock_id: str, dialect_id: str) -> Path:
        if self._project_path is None:
            raise ApplicationError(
                "project_not_open", "Open a project before accessing MetaBlock state"
            )
        return (
            Path(self._project_path).parent
            / ".graphimo"
            / "metablocks"
            / f"{_safe_component(metablock_id)}.{_safe_component(dialect_id)}.json"
        )

    def _load_metablock_state(self, metablock_id: str) -> tuple[str, Any]:
        if self._project_path is None:
            raise ApplicationError(
                "project_not_open", "Open a project before accessing MetaBlock state"
            )
        safe_id = _safe_component(metablock_id)
        root = Path(self._project_path).parent / ".graphimo" / "metablocks"
        candidates = []
        if root.is_dir():
            for path in sorted(root.glob("*.json")):
                if not path.name.startswith(f"{safe_id}."):
                    continue
                state = self._read_metablock_sidecar(path)
                if state is not None and state.get("metablock_id", metablock_id) == metablock_id:
                    candidates.append((path, state))
        if len(candidates) != 1:
            raise ApplicationError(
                "metablock_not_found", f"Expected one saved MetaBlock state for {metablock_id!r}"
            )
        path, state = candidates[0]
        return path.stem[len(safe_id) + 1 :], state

    def _save_metablock_state(
        self, dialect_id: str, metablock_id: str, state: Any
    ) -> dict[str, Any]:
        normalized = _json_safe(state)
        if not isinstance(normalized, dict):
            raise ApplicationError(
                "metablock_state_invalid", "Addon state must serialize to a JSON object"
            )
        path = self._metablock_sidecar_path(metablock_id, dialect_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(normalized, stream, indent=2, sort_keys=True, allow_nan=False)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return normalized

    @staticmethod
    def _read_metablock_sidecar(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ApplicationError(
                "metablock_state_unreadable", f"MetaBlock state cannot be read: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise ApplicationError(
                "metablock_state_invalid", "MetaBlock sidecar root must be a JSON object"
            )
        return value

    def _update_metablock_materialization(
        self, cell_id: str, source: str, result_var: str, *, dialect_id: str, metablock_id: str
    ) -> Any:
        session_id = self._require_session()
        if not cell_id or not isinstance(source, str) or not result_var.isidentifier():
            raise ApplicationError(
                "invalid_materialization", "Materialization cell identity/source is invalid"
            )
        _safe_component(dialect_id)
        _safe_component(metablock_id)
        managed_source = (
            f"# Graphimo metablock: {dialect_id}\n"
            f"# Graphimo instance: {metablock_id}\n"
            f"{source.rstrip()}\n"
        )
        cells = self.marimo.inspect(session_id).get("cells", [])
        marker = f"# Graphimo instance: {metablock_id}"
        dialect_marker = f"# Graphimo metablock: {dialect_id}"
        owned = [
            cell
            for cell in cells
            if isinstance(cell, dict)
            and marker in str(cell.get("source", "")).splitlines()
            and dialect_marker in str(cell.get("source", "")).splitlines()
        ]
        if len(owned) > 1:
            raise ApplicationError(
                "duplicate_metablock_materialization",
                "Multiple managed marimo cells exist for this MetaBlock",
            )
        if owned:
            return self.marimo.replace_cell_source(session_id, owned[0]["id"], managed_source)
        target = next(
            (cell for cell in cells if isinstance(cell, dict) and cell.get("id") == cell_id), None
        )
        if target is not None:
            raise ApplicationError(
                "unsafe_materialization_edit",
                "Refusing to replace a marimo cell not owned by this MetaBlock",
            )
        return self.marimo.add_cell(session_id, managed_source)


def _safe_component(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or any(char in value for char in "/\\\0")
    ):
        raise ApplicationError(
            "invalid_metablock_id", "MetaBlock identifiers cannot contain path separators"
        )
    return value


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ApplicationError(
            "metablock_result_not_json", "Addon state and results must contain JSON values only"
        ) from exc
