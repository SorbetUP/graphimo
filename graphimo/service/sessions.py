"""Persistent Graphimo project sessions and semantic application dispatch."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from graphimo.application import ApplicationError, GraphimoApplication
from graphimo.marimo_http import MarimoSessionPool

from .events import EventJournal
from .protocol import MAX_BATCH_OPERATIONS, jsonable


@dataclass
class ProjectSession:
    id: str
    path: Path
    application: GraphimoApplication
    marimo: Any
    journal: EventJournal = field(default_factory=EventJournal)
    addon_cursors: dict[str, int] = field(default_factory=dict)
    addon_stream_errors: set[str] = field(default_factory=set)
    lock: RLock = field(default_factory=RLock)


class ProjectSessionManager:
    """Own application and marimo sessions for the lifetime of the service."""

    def __init__(self, *, marimo_factory: Callable[[str | Path], Any] = MarimoSessionPool) -> None:
        self._marimo_factory = marimo_factory
        self._lock = RLock()
        self._sessions: dict[str, ProjectSession] = {}
        self._by_path: dict[str, str] = {}

    def open_project(self, path: str | Path) -> dict[str, Any]:
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise ApplicationError("invalid_project_path", "Project path must be non-empty")
        notebook = Path(path).expanduser().resolve()
        if notebook.suffix != ".py":
            raise ApplicationError(
                "invalid_project_path", "Graphimo projects must be marimo .py files"
            )
        if not notebook.is_file():
            raise ApplicationError("project_not_found", f"Project file does not exist: {notebook}")
        key = str(notebook)
        with self._lock:
            existing_id = self._by_path.get(key)
            if existing_id is not None:
                session = self._sessions[existing_id]
                with session.lock:
                    state = session.application.refresh_project()
                return {
                    "project_id": session.id,
                    "path": key,
                    "editor_url": self._editor_url(session.marimo, session.application.session_id),
                    "state": jsonable(state),
                    "reused": True,
                }
            marimo = self._marimo_factory(notebook)
            try:
                application = GraphimoApplication(
                    marimo,
                    notebook.parent / "components",
                    layout_path=notebook.parent / ".graphimo.json",
                )
                state = application.open_project(notebook, marimo.session_id)
            except Exception:
                marimo.close()
                raise
            project_id = secrets.token_urlsafe(18)
            session = ProjectSession(project_id, notebook, application, marimo)
            self._sessions[project_id] = session
            self._by_path[key] = project_id
            session.journal.publish(
                {"type": "project.opened", "project_id": project_id, "path": key}
            )
            return {
                "project_id": project_id,
                "path": key,
                "editor_url": self._editor_url(marimo, application.session_id),
                "state": jsonable(state),
                "reused": False,
            }

    @staticmethod
    def _editor_url(marimo: Any, session_id: str | None) -> str | None:
        url_for_session = getattr(marimo, "url_for_session", None)
        if callable(url_for_session):
            url = url_for_session(session_id)
            return url if isinstance(url, str) else None
        return None

    def create_project(self, path: str | Path) -> dict[str, Any]:
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise ApplicationError("invalid_project_path", "Project path must be non-empty")
        notebook = Path(path).expanduser().resolve()
        if notebook.suffix != ".py":
            raise ApplicationError(
                "invalid_project_path", "Graphimo projects must be marimo .py files"
            )
        if notebook.exists():
            raise ApplicationError(
                "project_already_exists", f"Project file already exists: {notebook}"
            )
        notebook.parent.mkdir(parents=True, exist_ok=True)
        notebook.write_text(
            'import marimo\n\n__generated_with = "0.25.2"\napp = marimo.App()\n\n'
            'if __name__ == "__main__":\n    app.run()\n',
            encoding="utf-8",
        )
        return self.open_project(notebook)

    def close_project(self, project_id: str) -> dict[str, bool]:
        session = self.get(project_id)
        with self._lock:
            self._sessions.pop(project_id, None)
            self._by_path.pop(str(session.path), None)
        with session.lock:
            session.application.close_project()
            session.marimo.close()
            session.journal.publish({"type": "project.closed", "project_id": project_id})
        return {"closed": True}

    def get(self, project_id: str) -> ProjectSession:
        with self._lock:
            session = self._sessions.get(project_id)
        if session is None:
            raise ApplicationError(
                "project_session_not_found", "Graphimo project session was not found"
            )
        return session

    def execute(
        self, project_id: str, action: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        session = self.get(project_id)
        if not isinstance(action, str) or not action:
            raise ApplicationError("invalid_action", "A Graphimo application action is required")
        arguments = args or {}
        if not isinstance(arguments, dict):
            raise ApplicationError("invalid_request", "Action args must be a JSON object")
        if action == "close_project":
            return self.close_project(project_id)
        if action == "inspect":
            action = "open_inspector"
        if action not in session.application.available_actions:
            raise ApplicationError(
                "feature_not_available", f"Graphimo action '{action}' is unavailable"
            )
        operation_id = secrets.token_hex(6)
        started = time.time()
        session.journal.publish(
            {
                "type": "application.operation.started",
                "project_id": session.id,
                "operation_id": operation_id,
                "action": action,
            }
        )
        with session.lock:
            try:
                result = getattr(session.application, action)(**arguments)
                state = session.application.refresh_project()
            except ApplicationError as exc:
                session.journal.publish(
                    {
                        "type": "application.operation.failed",
                        "operation_id": operation_id,
                        "action": action,
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
                self.pump_events(session)
                raise
            except (TypeError, ValueError) as exc:
                session.journal.publish(
                    {
                        "type": "application.operation.failed",
                        "operation_id": operation_id,
                        "action": action,
                        "code": "invalid_arguments",
                        "message": str(exc),
                    }
                )
                self.pump_events(session)
                raise ApplicationError("invalid_arguments", str(exc)) from exc
            except Exception as exc:
                session.journal.publish(
                    {
                        "type": "application.operation.failed",
                        "operation_id": operation_id,
                        "action": action,
                        "code": "operation_failed",
                        "message": str(exc)[:2000],
                    }
                )
                self.pump_events(session)
                raise ApplicationError("operation_failed", str(exc)[:2000]) from exc
            session.journal.publish(
                {
                    "type": "application.operation.completed",
                    "operation_id": operation_id,
                    "action": action,
                    "duration_ms": round((time.time() - started) * 1000),
                }
            )
            self.pump_events(session)
        return {"result": jsonable(result), "state": jsonable(state)}

    def execute_batch(
        self,
        project_id: str,
        operations: list[dict[str, Any]],
        *,
        continue_on_error: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(operations, list) or len(operations) > MAX_BATCH_OPERATIONS:
            raise ApplicationError(
                "invalid_batch", f"Batch must contain at most {MAX_BATCH_OPERATIONS} operations"
            )
        if type(continue_on_error) is not bool:
            raise ApplicationError("invalid_batch", "continue_on_error must be a boolean")
        results: dict[str, Any] = {}
        journal: list[dict[str, Any]] = []
        stopped = False
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise ApplicationError(
                    "invalid_batch", f"Batch operation {index + 1} must be an object"
                )
            operation_id = operation.get("id", f"step_{index + 1}")
            action = operation.get("action")
            if not isinstance(operation_id, str) or not operation_id or operation_id in results:
                raise ApplicationError(
                    "invalid_batch", f"Batch operation {index + 1} has an invalid or duplicate id"
                )
            if stopped:
                journal.append({"id": operation_id, "action": action, "status": "not_executed"})
                continue
            try:
                args = _resolve_references(operation.get("args", {}), results)
                response = self.execute(project_id, action, args)
                entry = {"id": operation_id, "action": action, "status": "success", **response}
                results[operation_id] = entry
            except ApplicationError as exc:
                entry = {
                    "id": operation_id,
                    "action": action,
                    "status": "error",
                    "error": {"code": exc.code, "message": str(exc)},
                }
                results[operation_id] = entry
                if not continue_on_error:
                    stopped = True
            except Exception as exc:
                entry = {
                    "id": operation_id,
                    "action": action,
                    "status": "error",
                    "error": {"code": "operation_failed", "message": str(exc)[:2000]},
                }
                results[operation_id] = entry
                if not continue_on_error:
                    stopped = True
            journal.append(entry)
        session = self.get(project_id)
        with session.lock:
            self.pump_events(session)
            state = jsonable(session.application.refresh_project())
        return {"operations": journal, "state": state}

    def pump_events(self, session: ProjectSession) -> None:
        application = session.application
        drain = getattr(application, "drain_metablock_events", None)
        if not callable(drain):
            return
        paths = {session.path}
        for frame in getattr(application, "_scope_stack", ()):
            try:
                paths.add(Path(frame[3]))
            except (IndexError, TypeError):
                continue
        sidecars = set()
        for notebook in paths:
            sidecars.update((notebook.parent / ".graphimo" / "metablocks").glob("*.json"))
        for path in sorted(sidecars):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            metablock_id = value.get("metablock_id") if isinstance(value, dict) else None
            if not isinstance(metablock_id, str) or not metablock_id:
                continue
            cursor = session.addon_cursors.get(metablock_id, 0)
            try:
                page = drain(metablock_id, cursor=cursor, limit=256)
            except Exception as exc:
                if metablock_id not in session.addon_stream_errors:
                    session.journal.publish(
                        {
                            "type": "addon.event_stream_error",
                            "project_id": session.id,
                            "metablock_id": metablock_id,
                            "message": str(exc)[:1000],
                        }
                    )
                    session.addon_stream_errors.add(metablock_id)
                continue
            if (
                not isinstance(page, dict)
                or type(page.get("cursor")) is not int
                or page["cursor"] < cursor
                or not isinstance(page.get("events"), list)
                or len(page["events"]) > 256
            ):
                if metablock_id not in session.addon_stream_errors:
                    session.journal.publish(
                        {
                            "type": "addon.event_stream_error",
                            "project_id": session.id,
                            "metablock_id": metablock_id,
                            "message": "Addon returned an invalid event page",
                        }
                    )
                    session.addon_stream_errors.add(metablock_id)
                continue
            session.addon_stream_errors.discard(metablock_id)
            session.addon_cursors[metablock_id] = page["cursor"]
            for item in page["events"]:
                session.journal.publish(
                    {
                        "type": "addon.event",
                        "project_id": session.id,
                        "metablock_id": metablock_id,
                        "event": item,
                    }
                )

    def close(self) -> None:
        with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
            self._by_path.clear()
        for session in sessions:
            with session.lock:
                session.application.close_project()
                session.marimo.close()


def _resolve_references(value: Any, results: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            reference = value["$ref"]
            if not isinstance(reference, str) or not reference:
                raise ApplicationError(
                    "invalid_batch_reference", "Batch reference must be a non-empty string"
                )
            parts = reference.split(".")
            if parts[0] not in results:
                raise ApplicationError(
                    "invalid_batch_reference",
                    f"Unknown or not-yet-completed batch reference {parts[0]!r}",
                )
            current: Any = results[parts[0]]
            if isinstance(current, dict) and current.get("status") == "error":
                raise ApplicationError(
                    "batch_dependency_failed",
                    f"Batch reference {reference!r} points to a failed operation",
                )
            for part in parts[1:]:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                elif isinstance(current, list) and part.isdecimal() and int(part) < len(current):
                    current = current[int(part)]
                else:
                    raise ApplicationError(
                        "invalid_batch_reference",
                        f"Batch reference path {reference!r} does not exist",
                    )
            return current
        return {key: _resolve_references(item, results) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_references(item, results) for item in value]
    return value
