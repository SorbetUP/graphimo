"""Persistent per-project GraphimoApplication sessions for the local service."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from graphimo import artifacts
from graphimo.application import GraphimoApplication
from graphimo.metablocks import GraphimoHostAPI, MetaBlockError

from .events import EventJournal
from .marimo_kernel import MarimoKernelPort
from .protocol import execute_batch

_OPERATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_OPERATION_JOURNAL = 256


class OperationIdConflict(ValueError):
    """An operation_id was replayed with a different action or arguments."""

    code = "operation_id_conflict"


def _args_fingerprint(action: str, args: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            {"action": action, "args": args},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("operation arguments must be finite JSON data") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_READ_ONLY_ACTIONS = {
    "notebook.inspect",
    "component.list",
    "component.validate",
    "component.instance_source",
    "control.catalog",
    "group.list",
    "group.inspect",
    "metablock.list",
    "metablock.catalog",
    "metablock.project",
    "metablock.events",
    "artifact.inspect",
    "artifact.stats",
    "artifact.list",
    "addon.list",
    "addon.describe",
    "run.list",
    "scope.current",
    "layout.get",
}


class HostArtifactStore:
    """Renderer-neutral facade over Graphimo's standard local ArtifactStore."""

    def put_bytes(
        self,
        data: bytes,
        *,
        kind: str = "blob",
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        return artifacts.put_bytes(data, kind=kind, parent_id=parent_id, metadata=metadata)

    def put_file(
        self,
        path: str | Path,
        *,
        kind: str = "file",
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        return artifacts.put(path, kind=kind, parent_id=parent_id, metadata=metadata)

    def put_directory(
        self,
        path: str | Path,
        *,
        kind: str = "dataset",
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        return artifacts.put(path, kind=kind, parent_id=parent_id, metadata=metadata)

    def get(self, artifact: str):
        return artifacts.get(artifact)

    def describe(self, artifact: str) -> dict[str, Any]:
        return artifacts.describe(artifact)

    def list(self, **kwargs: Any) -> dict[str, Any]:
        return artifacts.list(**kwargs)

    def materialize(self, artifact: str, destination: str | Path):
        return artifacts.materialize(artifact, destination)

    def stats(self) -> dict[str, int]:
        return artifacts.stats()

    def pin(self, artifact: str, *, owner: str) -> None:
        artifacts.pin(artifact, owner=owner)

    def unpin(self, artifact: str, *, owner: str) -> None:
        artifacts.unpin(artifact, owner=owner)

    def gc(self, *, referenced: Iterable[str] = ()) -> dict[str, int]:
        return artifacts.gc(referenced=referenced)


class ProjectSession:
    """One persistent application/event context for a canonical project file."""

    def __init__(self, path: Path):
        self.path = path
        self.project_id = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
        self.events = EventJournal()
        # State reads (especially the kernel event publisher) must never wait
        # behind an application action that is blocked on a live kernel call.
        self._state_lock = threading.RLock()
        # Application mutations are serialized independently from the short
        # state lock. This keeps actions coherent without stalling telemetry.
        self._action_lock = threading.RLock()
        self._addon_cursors: dict[str, str] = {}
        self._subscriptions: set[tuple[str, str, str]] = set()
        self._operation_journal: OrderedDict[str, tuple[str, str, Any]] = OrderedDict()
        self._closed = False
        self._kernel_port: MarimoKernelPort | None = None
        host = GraphimoHostAPI(
            project_id=self.project_id,
            project_path=path,
            artifact_store=HostArtifactStore(),
            emit_event=self._emit_host_event,
            diagnose=self._emit_diagnostic,
            update_materialization_cell=self._materialization_requested,
            invoke_kernel=self._invoke_kernel,
            invoke_notebook=self._invoke_notebook,
            read_kernel_events=self._read_kernel_events,
            current_scope=lambda: "root",
        )
        self.application = GraphimoApplication(host)

    def execute(self, action: str, args: dict[str, Any]) -> Any:
        with self._action_lock:
            return self._execute_locked(action, args)

    def execute_batch(self, operations: Any, *, continue_on_error: bool = False) -> dict[str, Any]:
        """Run a whole batch under one action lock so no other client interleaves."""
        with self._action_lock:
            return execute_batch(
                self._execute_locked, operations, continue_on_error=continue_on_error
            )

    def _execute_locked(self, action: str, args: dict[str, Any]) -> Any:
        """Execute one action; the caller must already hold ``_action_lock``."""
        with self._state_lock:
            self._ensure_open()
        operation_id: str | None = None
        if isinstance(args, dict) and "operation_id" in args:
            candidate = args["operation_id"]
            if not isinstance(candidate, str) or _OPERATION_ID.fullmatch(candidate) is None:
                raise ValueError("operation_id must be 1-128 letters, digits, '_' or '-'")
            operation_id = candidate
            args = {key: value for key, value in args.items() if key != "operation_id"}
        fingerprint = _args_fingerprint(action, args)
        if operation_id is not None and action not in _READ_ONLY_ACTIONS:
            with self._state_lock:
                entry = self._operation_journal.get(operation_id)
            if entry is not None:
                recorded_action, recorded_fingerprint, recorded_result = entry
                if (recorded_action, recorded_fingerprint) != (action, fingerprint):
                    raise OperationIdConflict(
                        f"operation_id {operation_id!r} was already used for a different "
                        f"action or arguments"
                    )
                return {"replayed": True, "operation_id": operation_id, "result": recorded_result}
        result = self.application.execute(action, args)
        self._track_runtime_action(action, args, result)
        self.events.publish(
            {
                "kind": "application.action.completed",
                "project_id": self.project_id,
                "action": action,
            }
        )
        if operation_id is not None and action not in _READ_ONLY_ACTIONS:
            with self._state_lock:
                self._operation_journal[operation_id] = (action, fingerprint, result)
                self._operation_journal.move_to_end(operation_id)
                while len(self._operation_journal) > _MAX_OPERATION_JOURNAL:
                    self._operation_journal.popitem(last=False)
        return result

    def attach_kernel(self, *, url: str, token: str, session_id: str) -> dict[str, Any]:
        port = MarimoKernelPort(
            url=url, token=token, session_id=session_id, project_path=str(self.path)
        )
        with self._action_lock:
            with self._state_lock:
                self._ensure_open()
                previous = self._kernel_port
                switched = previous is not None and (
                    previous.url != port.url or previous.session_id != port.session_id
                )
                metablock_ids = {item[0] for item in self._subscriptions} if switched else set()
                if switched:
                    self._subscriptions.clear()
            if switched:
                for metablock_id in metablock_ids:
                    try:
                        self.application.execute(
                            "metablock.invoke",
                            {
                                "metablock_id": metablock_id,
                                "action": "runtime.unsubscribe_all",
                                "args": {},
                            },
                        )
                    except Exception:
                        continue
                try:
                    previous.stop_publisher()
                except Exception:
                    pass
            with self._state_lock:
                self._ensure_open()
                self._kernel_port = port
        return {"attached": True, "session_id": session_id}

    def poll_events(
        self, cursor: str | None = None, *, limit: int = 128, wait_ms: int = 750
    ) -> dict[str, Any]:
        self._poll_addon_events()
        return self.events.read(cursor, limit=limit, wait_ms=wait_ms)

    def publish_kernel_events(self, batch: Any) -> dict[str, Any]:
        """Ingest bounded JSON events pushed by the attached marimo kernel."""
        if not isinstance(batch, list) or not 1 <= len(batch) <= 128:
            raise ValueError("events must be an array containing 1-128 event objects")
        accepted = 0
        for event in batch:
            if not isinstance(event, dict):
                raise ValueError("each kernel event must be an object")
            required = (
                "kind",
                "project_id",
                "session_id",
                "metablock_id",
                "graph_revision",
                "runtime_instance_id",
            )
            if any(not isinstance(event.get(key), str) or not event[key] for key in required):
                raise ValueError("kernel event is missing runtime identity fields")
            if event.get("project_id") != self.project_id:
                raise ValueError("kernel event project_id does not match the open project")
            with self._state_lock:
                attached_kernel = self._kernel_port
            if (
                attached_kernel is not None
                and event.get("session_id") != attached_kernel.session_id
            ):
                raise ValueError(
                    "kernel event session_id does not match the attached marimo session"
                )
            if "module" in event or "model" in event or "nn_module" in event:
                raise ValueError("kernel event must not contain live model objects")
            kind = event["kind"]
            if kind == "telemetry.dropped":
                count = event.get("count")
                if type(count) is not int or count < 0:
                    raise ValueError("telemetry drop count must be a non-negative integer")
            self.events.publish(
                {
                    "kind": "addon.event",
                    "source": "marimo_kernel",
                    "project_id": self.project_id,
                    "session_id": event["session_id"],
                    "metablock_id": event["metablock_id"],
                    "addon_event": event,
                }
            )
            accepted += 1
        return {"accepted": accepted}

    def close(self) -> None:
        with self._action_lock:
            with self._state_lock:
                if self._closed:
                    return
                pending = tuple(self._subscriptions)
                self._subscriptions.clear()
                port = self._kernel_port
            for metablock_id in {item[0] for item in pending}:
                try:
                    self.application.execute(
                        "metablock.invoke",
                        {
                            "metablock_id": metablock_id,
                            "action": "runtime.unsubscribe_all",
                            "args": {},
                        },
                    )
                except Exception:
                    continue
            if port is not None:
                try:
                    port.stop_publisher()
                except Exception:
                    pass
            with self._state_lock:
                self._kernel_port = None
                self._closed = True
            self.events.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Graphimo project session is closed")

    def _emit_host_event(self, event: dict[str, Any]) -> None:
        self._publish_with_project(event, default_kind="host.event")

    def _emit_diagnostic(self, diagnostic: dict[str, Any]) -> None:
        self._publish_with_project(diagnostic, default_kind="diagnostic")

    def _materialization_requested(self, specification: dict[str, Any]) -> None:
        self.events.publish(
            {
                "kind": "metablock.materialization.requested",
                "project_id": self.project_id,
                "metablock_id": specification.get("metablock_id"),
                "dialect_id": specification.get("dialect_id"),
            }
        )

    def _publish_with_project(self, value: dict[str, Any], *, default_kind: str) -> None:
        event = dict(value)
        event.setdefault("kind", default_kind)
        event["project_id"] = self.project_id
        try:
            self.events.publish(event)
        except ValueError:
            self.events.publish(
                {
                    "kind": "diagnostic",
                    "code": "service_event_invalid",
                    "message": "A host event could not be represented as bounded JSON.",
                    "project_id": self.project_id,
                }
            )

    def _invoke_kernel(self, metablock_id: str, action: str, args: dict[str, Any]) -> Any:
        with self._state_lock:
            self._ensure_open()
            port = self._kernel_port
        if port is None:
            return None
        return port.invoke(metablock_id, action, args)

    def _invoke_notebook(self, action: str, args: dict[str, Any]) -> Any:
        with self._state_lock:
            self._ensure_open()
            port = self._kernel_port
        if port is None:
            raise ValueError("no live marimo kernel is attached to this project")
        return port.invoke_notebook(action, args)

    def _read_kernel_events(
        self, metablock_id: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        with self._state_lock:
            if self._closed or self._kernel_port is None:
                return None
        result = self.events.read_matching(
            cursor, limit=limit, metablock_id=metablock_id, source="marimo_kernel"
        )
        events = []
        for item in result["events"]:
            event = item.get("addon_event")
            if isinstance(event, dict):
                events.append({**event, "sequence": item["sequence"]})
        return {**result, "events": events}

    def _track_runtime_action(self, action: str, args: dict[str, Any], result: Any) -> None:
        if action != "metablock.invoke" or not isinstance(result, dict):
            return
        metablock_id = args.get("metablock_id")
        addon_action = args.get("action")
        addon_args = args.get("args", {})
        if not isinstance(metablock_id, str) or not isinstance(addon_args, dict):
            return
        runtime_id = addon_args.get("runtime_instance_id")
        subscription_id = result.get("subscription_id")
        with self._state_lock:
            if (
                addon_action == "runtime.subscribe"
                and isinstance(runtime_id, str)
                and isinstance(subscription_id, str)
            ):
                self._subscriptions.add((metablock_id, runtime_id, subscription_id))
            elif addon_action == "runtime.unsubscribe" and isinstance(runtime_id, str):
                requested = addon_args.get("subscription_id")
                self._subscriptions = {
                    item
                    for item in self._subscriptions
                    if not (
                        item[0] == metablock_id
                        and item[1] == runtime_id
                        and (requested is None or item[2] == requested)
                    )
                }
            elif addon_action == "runtime.release" and isinstance(runtime_id, str):
                self._subscriptions = {
                    item
                    for item in self._subscriptions
                    if not (item[0] == metablock_id and item[1] == runtime_id)
                }

    def _poll_addon_events(self) -> None:
        with self._action_lock:
            with self._state_lock:
                if self._closed:
                    return
            try:
                listing = self.application.execute("metablock.list", {})
            except Exception:
                return
            for item in listing.get("items", []):
                metablock_id = item.get("metablock_id")
                if not isinstance(metablock_id, str):
                    continue
                with self._state_lock:
                    cursor = self._addon_cursors.get(metablock_id, "0")
                try:
                    response = self.application.execute(
                        "metablock.events",
                        {"metablock_id": metablock_id, "cursor": cursor, "limit": 256},
                    )
                except (MetaBlockError, ValueError, FileNotFoundError):
                    continue
                next_cursor = response.get("cursor")
                if isinstance(next_cursor, str):
                    with self._state_lock:
                        self._addon_cursors[metablock_id] = next_cursor
                dropped = response.get("dropped", 0)
                if type(dropped) is int and dropped > 0:
                    self.events.publish(
                        {
                            "kind": "addon.events.dropped",
                            "project_id": self.project_id,
                            "metablock_id": metablock_id,
                            "count": dropped,
                        }
                    )
                for event in response.get("events", []):
                    self.events.publish(
                        {
                            "kind": "addon.event",
                            "project_id": self.project_id,
                            "metablock_id": metablock_id,
                            "addon_event": event,
                        }
                    )


class ProjectSessionManager:
    """Create/reuse one Graphimo application for each canonical project path."""

    def __init__(self):
        self._lock = threading.RLock()
        self._by_id: dict[str, ProjectSession] = {}
        self._by_path: dict[Path, str] = {}

    def open_project(self, project_path: str | Path) -> tuple[ProjectSession, bool]:
        try:
            path = Path(project_path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise FileNotFoundError(f"Graphimo project does not exist: {project_path}") from exc
        if not path.is_file():
            raise ValueError("Graphimo project path must name an existing file")
        with self._lock:
            existing_id = self._by_path.get(path)
            if existing_id is not None:
                return self._by_id[existing_id], True
            session = ProjectSession(path)
            self._by_id[session.project_id] = session
            self._by_path[path] = session.project_id
            return session, False

    def get(self, project_id: str) -> ProjectSession:
        with self._lock:
            session = self._by_id.get(project_id)
        if session is None:
            raise KeyError(f"Graphimo project session '{project_id}' is not open")
        return session

    def close_project(self, project_id: str) -> bool:
        with self._lock:
            session = self._by_id.pop(project_id, None)
            if session is None:
                return False
            self._by_path.pop(session.path, None)
        session.close()
        return True

    def list(self) -> list[dict[str, str]]:
        with self._lock:
            return [
                {"project_id": session.project_id, "path": str(session.path)}
                for session in self._by_id.values()
            ]

    def close(self) -> None:
        with self._lock:
            sessions = list(self._by_id.values())
            self._by_id.clear()
            self._by_path.clear()
        for session in sessions:
            session.close()
