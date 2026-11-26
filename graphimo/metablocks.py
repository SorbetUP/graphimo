"""Generic, addon-owned MetaBlock semantics and persistence.

Core treats addon state as opaque JSON. Dialects own their schema and edit
semantics; the host owns project files, artifacts, events and materialization
cell updates.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

META_DIALECT_ENTRY_POINT_GROUP = "graphimo.metablocks"
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_HISTORY_DEPTH = 50
MAX_HISTORY_BYTES = 16 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DIALECT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class MetaBlockError(RuntimeError):
    """Base error for generic MetaBlock operations."""


class AddonUnavailableError(MetaBlockError):
    """Raised when an operation needs a dialect that is not installed."""


class MetaBlockStateError(MetaBlockError):
    """Raised when a sidecar is malformed or exceeds its persistence limit."""


class MetaBlockHistoryError(MetaBlockError):
    """Raised when a MetaBlock history entry no longer matches its sidecar."""


class MetaBlockRevisionConflict(MetaBlockError):
    """Raised when a mutation's expected_revision does not match the sidecar."""

    def __init__(self, message: str, *, expected: Any, actual: Any):
        self.code = "revision_conflict"
        self.expected = expected
        self.actual = actual
        super().__init__(message)


@dataclass(frozen=True)
class MetaDialectRegistration:
    dialect_id: str
    addon_version: str
    api_version: int
    factory: Callable[[GraphimoHostAPI], object]


@dataclass(frozen=True)
class MetaDialectDescriptor:
    name: str
    value: str
    distribution: str | None
    version: str | None


@dataclass(frozen=True)
class MetaBlockRecord:
    metablock_id: str
    dialect_id: str
    addon_version: str
    api_version: int
    state: Any
    revision: str


@runtime_checkable
class MetaBlockDialect(Protocol):
    """Semantic behavior implemented by a Graphimo MetaBlock addon."""

    dialect_id: str

    def create_state(self, request: dict[str, Any], context: GraphimoHostAPI) -> Any: ...

    def catalog(self, state: Any, scope: str, context: GraphimoHostAPI) -> Any: ...

    def project(self, state: Any, scope: str, context: GraphimoHostAPI) -> Any: ...

    def apply(self, state: Any, intent: dict[str, Any], context: GraphimoHostAPI) -> Any: ...

    def build_materialization(
        self, state: Any, bindings: dict[str, Any], context: GraphimoHostAPI
    ) -> dict[str, Any]: ...

    def invoke(
        self, state: Any, action: str, args: dict[str, Any], context: GraphimoHostAPI
    ) -> Any: ...

    def drain_events(
        self, cursor: str | None, limit: int, context: GraphimoHostAPI
    ) -> dict[str, Any]: ...


@dataclass
class GraphimoHostAPI:
    """Bounded host services available to addon dialects.

    The object deliberately exposes no renderer widgets or domain-specific
    helpers. Artifact and marimo services are injected by the application.
    """

    project_id: str
    project_path: Path
    artifact_store: Any = None
    emit_event: Callable[[dict[str, Any]], None] = lambda _event: None
    diagnose: Callable[[dict[str, Any]], None] = lambda _diagnostic: None
    update_materialization_cell: Callable[[dict[str, Any]], Any] = lambda _spec: None
    invoke_kernel: Callable[[str, str, dict[str, Any]], Any] | None = None
    invoke_notebook: Callable[[str, dict[str, Any]], Any] | None = None
    read_kernel_events: Callable[[str, str | None, int], dict[str, Any]] | None = None
    current_scope: Callable[[], str] = lambda: "root"
    current_metablock_id: str | None = None

    @property
    def metablock_root(self) -> Path:
        return self.project_path.parent / ".graphimo" / "metablocks"

    def sidecar_path(self, metablock_id: str, dialect_id: str | None = None) -> Path:
        if not isinstance(metablock_id, str) or not _ID_RE.fullmatch(metablock_id):
            raise ValueError("metablock_id must contain 1-128 letters, digits, '_' or '-'")
        if dialect_id is not None:
            if not isinstance(dialect_id, str) or not _DIALECT_RE.fullmatch(dialect_id):
                raise ValueError("dialect_id contains unsupported path characters")
            suffix = dialect_id.split(".", 1)[0]
            return self.metablock_root / f"{metablock_id}.{suffix}.json"
        candidates = sorted(self.metablock_root.glob(f"{metablock_id}.*.json"))
        if len(candidates) > 1:
            raise MetaBlockStateError(f"multiple MetaBlock sidecars found for '{metablock_id}'")
        if candidates:
            return candidates[0]
        return self.metablock_root / f"{metablock_id}.json"

    def read_sidecar(self, metablock_id: str) -> dict[str, Any] | None:
        path = self.sidecar_path(metablock_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        if len(raw) > MAX_STATE_BYTES:
            raise MetaBlockStateError("MetaBlock sidecar exceeds the 8 MiB limit")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MetaBlockStateError(f"invalid MetaBlock sidecar JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise MetaBlockStateError("MetaBlock sidecar root must be a JSON object")
        return value

    def write_sidecar(self, metablock_id: str, value: dict[str, Any]) -> None:
        path = self.sidecar_path(metablock_id, value.get("dialect_id"))
        encoded = _encode_json(value)
        if len(encoded) > MAX_STATE_BYTES:
            raise MetaBlockStateError("MetaBlock sidecar exceeds the 8 MiB limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def remove_sidecar(self, metablock_id: str) -> None:
        legacy_path = self.sidecar_path(metablock_id)
        root = self.metablock_root
        if root.exists():
            for path in root.glob(f"{metablock_id}.*.json"):
                path.unlink(missing_ok=True)
        legacy_path.unlink(missing_ok=True)

    @property
    def presentation_path(self) -> Path:
        return self.project_path.parent / ".graphimo.json"

    def read_layout(self, scope_id: str) -> dict[str, dict[str, Any]]:
        _validate_scope_id(scope_id)
        scoped_key = _layout_scope_key(self.project_path, scope_id)
        path = self.presentation_path
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {}
        if len(raw) > MAX_STATE_BYTES:
            raise MetaBlockStateError("Graphimo presentation file exceeds the 8 MiB limit")
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MetaBlockStateError(f"invalid Graphimo presentation JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise MetaBlockStateError("unsupported Graphimo presentation format")
        if document.get("format") not in {None, 1} or document.get("version") not in {None, 1}:
            raise MetaBlockStateError("unsupported Graphimo presentation format")
        layout = document.get("layout", {})
        if not isinstance(layout, dict):
            raise MetaBlockStateError("Graphimo presentation layout must be an object")
        if scoped_key in layout:
            return _validate_layout(layout[scoped_key])
        if scope_id in layout:
            # Read early Graphimo presentation files whose scope IDs were global.
            return _validate_layout(layout[scope_id])
        if scope_id == "root":
            notebooks = document.get("notebooks", {})
            if not isinstance(notebooks, dict):
                raise MetaBlockStateError("legacy Graphimo notebook layouts must be an object")
            notebook = notebooks.get(str(self.project_path.resolve()), {})
            if not isinstance(notebook, dict):
                raise MetaBlockStateError("legacy Graphimo notebook layout must be an object")
            return _validate_layout(notebook.get("cells", {}))
        return {}

    def write_layout(self, scope_id: str, layout: dict[str, Any]) -> None:
        _validate_scope_id(scope_id)
        validated = _validate_layout(layout)
        path = self.presentation_path
        document: dict[str, Any] = {"format": 1, "version": 1, "layout": {}, "notebooks": {}}
        if path.exists():
            try:
                raw = path.read_bytes()
                if len(raw) > MAX_STATE_BYTES:
                    raise MetaBlockStateError("Graphimo presentation file exceeds the 8 MiB limit")
                existing = json.loads(raw)
                if not isinstance(existing, dict):
                    raise MetaBlockStateError("unsupported Graphimo presentation format")
                if existing.get("format") not in {None, 1} or existing.get("version") not in {
                    None,
                    1,
                }:
                    raise MetaBlockStateError("unsupported Graphimo presentation format")
                document = dict(existing)
                layout_root = existing.get("layout", {})
                if not isinstance(layout_root, dict):
                    raise MetaBlockStateError("Graphimo presentation layout must be an object")
                document["layout"] = dict(layout_root)
                notebook_root = existing.get("notebooks", {})
                if not isinstance(notebook_root, dict):
                    raise MetaBlockStateError("legacy Graphimo notebook layouts must be an object")
                document["notebooks"] = dict(notebook_root)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MetaBlockStateError(f"invalid Graphimo presentation JSON: {exc}") from exc
        scoped_key = _layout_scope_key(self.project_path, scope_id)
        document["layout"][scoped_key] = validated
        document["format"] = 1
        document["version"] = 1
        if scope_id == "root":
            document["notebooks"][str(self.project_path.resolve())] = {"cells": validated}
        encoded = _encode_json(document)
        if len(encoded) > MAX_STATE_BYTES:
            raise MetaBlockStateError("Graphimo presentation file exceeds the 8 MiB limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def for_metablock(self, metablock_id: str) -> GraphimoHostAPI:
        self.sidecar_path(metablock_id)
        return replace(self, current_metablock_id=metablock_id)


def _encode_json(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise MetaBlockStateError(f"MetaBlock state must be UTF-8 JSON data: {exc}") from exc


def _validate_scope_id(value: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError("scope ID must contain 1-128 letters, digits, '_' or '-'")


def _layout_scope_key(project_path: Path, scope_id: str) -> str:
    project = str(project_path.expanduser().resolve())
    project_key = hashlib.sha256(project.encode("utf-8")).hexdigest()[:20]
    return f"project_{project_key}_{scope_id}"


def _validate_layout(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise MetaBlockStateError("Graphimo layout must be an object")
    result: dict[str, dict[str, Any]] = {}
    for element_id, rect in value.items():
        if not isinstance(element_id, str) or not _ID_RE.fullmatch(element_id):
            raise MetaBlockStateError("Graphimo layout has an invalid element ID")
        if not isinstance(rect, dict) or not {"x", "y"}.issubset(rect):
            raise MetaBlockStateError("Graphimo element layout requires x and y")
        allowed = {"x", "y", "width", "height", "collapsed", "group"}
        if set(rect) - allowed:
            raise MetaBlockStateError("Graphimo element layout contains unsupported fields")
        normalized: dict[str, Any] = {}
        for key in {"x", "y", "width", "height"} & set(rect):
            number = rect[key]
            if type(number) not in {int, float} or not math.isfinite(number):
                raise MetaBlockStateError("Graphimo layout coordinates must be finite numbers")
            normalized[key] = float(number)
        if any(normalized[key] <= 0 for key in {"width", "height"} & set(normalized)):
            raise MetaBlockStateError("Graphimo layout width and height must be positive")
        if "collapsed" in rect:
            if type(rect["collapsed"]) is not bool:
                raise MetaBlockStateError("Graphimo collapsed layout state must be boolean")
            normalized["collapsed"] = rect["collapsed"]
        if "group" in rect:
            if not isinstance(rect["group"], str):
                raise MetaBlockStateError("Graphimo layout group metadata must be text")
            normalized["group"] = rect["group"]
        result[element_id] = normalized
    return result


def state_revision(state: Any) -> str:
    """Return the canonical content revision for addon-owned JSON state."""
    return hashlib.sha256(_encode_json(state)).hexdigest()


def _record_from_sidecar(value: dict[str, Any]) -> MetaBlockRecord:
    required = {"format", "metablock_id", "dialect_id", "addon_version", "api_version", "state"}
    if not required.issubset(value) or value.get("format") != 1:
        raise MetaBlockStateError("unsupported or incomplete MetaBlock sidecar")
    if not isinstance(value["dialect_id"], str) or not value["dialect_id"]:
        raise MetaBlockStateError("MetaBlock sidecar has an invalid dialect id")
    if not isinstance(value["addon_version"], str) or type(value["api_version"]) is not int:
        raise MetaBlockStateError("MetaBlock sidecar has invalid addon version metadata")
    if not isinstance(value["metablock_id"], str) or not _ID_RE.fullmatch(value["metablock_id"]):
        raise MetaBlockStateError("MetaBlock sidecar has an invalid MetaBlock id")
    revision = state_revision(value["state"])
    return MetaBlockRecord(
        metablock_id=value["metablock_id"],
        dialect_id=value["dialect_id"],
        addon_version=value["addon_version"],
        api_version=value["api_version"],
        state=value["state"],
        revision=revision,
    )


class MetaBlockAddonManager:
    """Load addon handlers lazily and persist semantic state without interpreting it."""

    def __init__(self, host: GraphimoHostAPI, *, entry_points: Callable[[], Any] | None = None):
        self.host = host
        self._entry_points = entry_points or (
            lambda: importlib.metadata.entry_points(group=META_DIALECT_ENTRY_POINT_GROUP)
        )
        self._handlers: dict[str, MetaBlockDialect] = {}
        self._registrations: dict[str, MetaDialectRegistration] = {}
        self._history: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _snapshot_state(state: Any) -> Any:
        return json.loads(_encode_json(state))

    def _history_state(self, metablock_id: str, revision: str) -> dict[str, Any]:
        history = self._history.setdefault(
            metablock_id, {"undo": [], "redo": [], "expected_revision": revision}
        )
        return history

    @staticmethod
    def _history_summary(history: dict[str, Any], revision: str) -> dict[str, Any]:
        return {
            "undo_depth": len(history["undo"]),
            "redo_depth": len(history["redo"]),
            "stale": history["expected_revision"] != revision,
        }

    @staticmethod
    def _trim_history(history: dict[str, Any]) -> None:
        def size() -> int:
            return sum(
                len(_encode_json(state))
                for stack_name in ("undo", "redo")
                for state in history[stack_name]
            )

        while (
            len(history["undo"]) + len(history["redo"]) > MAX_HISTORY_DEPTH
            or size() > MAX_HISTORY_BYTES
        ):
            stack = history["undo"] if history["undo"] else history["redo"]
            if not stack:
                break
            stack.pop(0)

    def installed(self) -> list[MetaDialectDescriptor]:
        """List entry-point metadata without importing addon modules."""
        result = []
        for entry_point in self._entry_points():
            dist = getattr(entry_point, "dist", None)
            result.append(
                MetaDialectDescriptor(
                    name=entry_point.name,
                    value=entry_point.value,
                    distribution=getattr(dist, "name", None),
                    version=getattr(dist, "version", None),
                )
            )
        return sorted(result, key=lambda item: item.name)

    def list(self) -> dict[str, Any]:
        """List persisted MetaBlocks without requiring their addons to load."""
        root = self.host.metablock_root
        records: list[dict[str, Any]] = []
        diagnostics: list[dict[str, str]] = []
        if not root.exists():
            return {"items": records, "diagnostics": diagnostics}
        for path in sorted(root.glob("*.json")):
            try:
                raw = path.read_bytes()
                if len(raw) > MAX_STATE_BYTES:
                    raise MetaBlockStateError("sidecar exceeds the 8 MiB limit")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise MetaBlockStateError("sidecar root must be a JSON object")
                record = _record_from_sidecar(value)
                if self.host.sidecar_path(record.metablock_id) != path:
                    raise MetaBlockStateError("sidecar filename does not match its metadata")
                records.append(
                    {
                        "metablock_id": record.metablock_id,
                        "dialect_id": record.dialect_id,
                        "addon_version": record.addon_version,
                        "api_version": record.api_version,
                        "revision": record.revision,
                    }
                )
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                MetaBlockError,
                ValueError,
            ) as exc:
                diagnostics.append(
                    {
                        "code": "metablock_sidecar_invalid",
                        "message": f"{path.name}: {exc}",
                        "severity": "warning",
                    }
                )
        return {"items": records, "diagnostics": diagnostics}

    def _load(self, dialect_id: str) -> MetaBlockDialect:
        handler = self._handlers.get(dialect_id)
        if handler is not None:
            return handler
        entries = list(self._entry_points())
        if not entries:
            raise AddonUnavailableError(f"MetaBlock addon '{dialect_id}' is not installed")
        exact_name = next((item for item in entries if item.name == dialect_id), None)
        candidates = [exact_name] if exact_name is not None else entries
        registration = None
        for entry_point in candidates:
            loaded = entry_point.load()
            if isinstance(loaded, MetaDialectRegistration) and loaded.dialect_id == dialect_id:
                registration = loaded
                break
            if exact_name is not None:
                registration = loaded
                break
        if registration is None:
            raise AddonUnavailableError(f"MetaBlock addon '{dialect_id}' is not installed")
        if not isinstance(registration, MetaDialectRegistration):
            raise MetaBlockError(
                f"entry point '{dialect_id}' did not expose MetaDialectRegistration"
            )
        if registration.dialect_id != dialect_id:
            raise MetaBlockError(
                f"entry point '{dialect_id}' registered mismatched dialect '{registration.dialect_id}'"
            )
        if type(registration.api_version) is not int or registration.api_version != 1:
            raise MetaBlockError(f"unsupported MetaBlock API version: {registration.api_version}")
        if not isinstance(registration.addon_version, str) or not registration.addon_version:
            raise MetaBlockError("addon registration must include a non-empty version")
        if not callable(registration.factory):
            raise MetaBlockError("addon registration factory must be callable")
        handler = registration.factory(self.host)
        if getattr(handler, "dialect_id", None) != dialect_id:
            raise MetaBlockError(f"addon handler did not declare dialect_id '{dialect_id}'")
        required_methods = (
            "create_state",
            "catalog",
            "project",
            "apply",
            "build_materialization",
            "invoke",
            "drain_events",
        )
        missing_methods = [
            name for name in required_methods if not callable(getattr(handler, name, None))
        ]
        if missing_methods:
            raise MetaBlockError(
                f"addon handler is missing required methods: {', '.join(missing_methods)}"
            )
        self._registrations[dialect_id] = registration
        self._handlers[dialect_id] = handler
        return handler

    def _write_record(self, record: MetaBlockRecord) -> None:
        self.host.write_sidecar(
            record.metablock_id,
            {
                "format": 1,
                "metablock_id": record.metablock_id,
                "dialect_id": record.dialect_id,
                "addon_version": record.addon_version,
                "api_version": record.api_version,
                "state": record.state,
            },
        )

    def _projection_result(
        self,
        record: MetaBlockRecord,
        scope: str,
        handler: MetaBlockDialect,
        *,
        changed: bool | None = None,
    ) -> dict[str, Any]:
        projection = handler.project(
            record.state, scope, self.host.for_metablock(record.metablock_id)
        )
        history = self._history_state(record.metablock_id, record.revision)
        result = {
            "available": True,
            "metablock_id": record.metablock_id,
            "dialect_id": record.dialect_id,
            "addon_version": record.addon_version,
            "revision": record.revision,
            "projection": projection,
            "history": self._history_summary(history, record.revision),
        }
        if changed is not None:
            result["changed"] = changed
        return result

    def load(self, metablock_id: str) -> MetaBlockRecord:
        raw = self.host.read_sidecar(metablock_id)
        if raw is None:
            raise FileNotFoundError(f"MetaBlock '{metablock_id}' does not exist")
        record = _record_from_sidecar(raw)
        if record.metablock_id != metablock_id:
            raise MetaBlockStateError("MetaBlock id does not match its sidecar filename")
        return record

    def create(self, dialect_id: str, metablock_id: str, request: dict[str, Any]) -> dict[str, Any]:
        if self.host.read_sidecar(metablock_id) is not None:
            raise FileExistsError(f"MetaBlock '{metablock_id}' already exists")
        handler = self._load(dialect_id)
        registration = self._registrations[dialect_id]
        scoped_host = self.host.for_metablock(metablock_id)
        state = handler.create_state(request, scoped_host)
        record = _record_from_sidecar(
            {
                "format": 1,
                "metablock_id": metablock_id,
                "dialect_id": dialect_id,
                "addon_version": registration.addon_version,
                "api_version": registration.api_version,
                "state": state,
            }
        )
        self._write_record(record)
        self._history[metablock_id] = {"undo": [], "redo": [], "expected_revision": record.revision}
        return self._projection_result(record, "root", handler)

    def project(self, metablock_id: str, scope: str = "root") -> dict[str, Any]:
        record = self.load(metablock_id)
        try:
            handler = self._load(record.dialect_id)
        except AddonUnavailableError:
            history = self._history_state(record.metablock_id, record.revision)
            return {
                "available": False,
                "metablock_id": record.metablock_id,
                "dialect_id": record.dialect_id,
                "addon_version": record.addon_version,
                "revision": record.revision,
                "state": record.state,
                "history": self._history_summary(history, record.revision),
                "diagnostics": [
                    {
                        "code": "metablock_addon_unavailable",
                        "message": f"MetaBlock addon '{record.dialect_id}' is not installed",
                        "severity": "warning",
                    }
                ],
            }
        return self._projection_result(record, scope, handler)

    def catalog(self, metablock_id: str, scope: str) -> Any:
        record = self.load(metablock_id)
        return self._load(record.dialect_id).catalog(
            record.state, scope, self.host.for_metablock(metablock_id)
        )

    def apply(
        self,
        metablock_id: str,
        intent: dict[str, Any],
        scope: str = "root",
        expected_revision: Any = None,
    ) -> dict[str, Any]:
        record = self.load(metablock_id)
        if expected_revision is not None and expected_revision != record.revision:
            raise MetaBlockRevisionConflict(
                f"MetaBlock '{metablock_id}' changed since revision {expected_revision!r}; "
                f"current revision is {record.revision!r}",
                expected=expected_revision,
                actual=record.revision,
            )
        handler = self._load(record.dialect_id)
        scoped_host = self.host.for_metablock(metablock_id)
        history = self._history_state(metablock_id, record.revision)
        history_stale = history["expected_revision"] != record.revision
        previous_state = self._snapshot_state(record.state)
        updated = handler.apply(self._snapshot_state(record.state), intent, scoped_host)
        registration = self._registrations[record.dialect_id]
        next_record = _record_from_sidecar(
            {
                "format": 1,
                "metablock_id": record.metablock_id,
                "dialect_id": record.dialect_id,
                "addon_version": registration.addon_version,
                "api_version": registration.api_version,
                "state": updated,
            }
        )
        projection = handler.project(next_record.state, scope, scoped_host)
        self._write_record(next_record)
        if history_stale:
            history.update(undo=[], redo=[])
        if next_record.revision != record.revision:
            history["undo"].append(previous_state)
            history["redo"].clear()
            self._trim_history(history)
        history["expected_revision"] = next_record.revision
        result = {
            "available": True,
            "metablock_id": next_record.metablock_id,
            "dialect_id": next_record.dialect_id,
            "addon_version": next_record.addon_version,
            "revision": next_record.revision,
            "projection": projection,
            "history": self._history_summary(history, next_record.revision),
        }
        return result

    def undo(self, metablock_id: str, scope: str = "root") -> dict[str, Any]:
        return self._move_history(metablock_id, scope, undoing=True)

    def redo(self, metablock_id: str, scope: str = "root") -> dict[str, Any]:
        return self._move_history(metablock_id, scope, undoing=False)

    def _move_history(self, metablock_id: str, scope: str, *, undoing: bool) -> dict[str, Any]:
        record = self.load(metablock_id)
        handler = self._load(record.dialect_id)
        history = self._history_state(metablock_id, record.revision)
        if history["expected_revision"] != record.revision:
            raise MetaBlockHistoryError(
                "MetaBlock history is stale because its sidecar changed outside this application session"
            )
        source_stack = history["undo"] if undoing else history["redo"]
        destination_stack = history["redo"] if undoing else history["undo"]
        if not source_stack:
            return self._projection_result(record, scope, handler, changed=False)

        target_state = source_stack[-1]
        registration = self._registrations[record.dialect_id]
        next_record = _record_from_sidecar(
            {
                "format": 1,
                "metablock_id": record.metablock_id,
                "dialect_id": record.dialect_id,
                "addon_version": registration.addon_version,
                "api_version": registration.api_version,
                "state": self._snapshot_state(target_state),
            }
        )
        projection = handler.project(
            next_record.state, scope, self.host.for_metablock(metablock_id)
        )
        self._write_record(next_record)
        source_stack.pop()
        destination_stack.append(self._snapshot_state(record.state))
        history["expected_revision"] = next_record.revision
        self._trim_history(history)
        return {
            "available": True,
            "metablock_id": next_record.metablock_id,
            "dialect_id": next_record.dialect_id,
            "addon_version": next_record.addon_version,
            "revision": next_record.revision,
            "projection": projection,
            "history": self._history_summary(history, next_record.revision),
            "changed": True,
        }

    def materialize(self, metablock_id: str, bindings: dict[str, Any]) -> dict[str, Any]:
        record = self.load(metablock_id)
        result = self._load(record.dialect_id).build_materialization(
            record.state, bindings, self.host.for_metablock(metablock_id)
        )
        required = {"source", "result_name", "output_type", "bindings", "revision"}
        if not isinstance(result, dict) or not required.issubset(result):
            raise MetaBlockError(
                "addon materialization must include source, result_name, output_type, bindings and revision"
            )
        if result["revision"] != record.revision:
            raise MetaBlockError("addon materialization revision does not match persisted state")
        self.host.update_materialization_cell(
            {**result, "metablock_id": metablock_id, "dialect_id": record.dialect_id}
        )
        # The materialized cell now matches the persisted revision; older cells
        # materialized from a previous revision are reported as outdated by the
        # renderer comparing graph_revision values.
        result = dict(result)
        result["materialization_state"] = "current"
        return result

    def invoke(self, metablock_id: str, action: str, args: dict[str, Any]) -> Any:
        record = self.load(metablock_id)
        return self._load(record.dialect_id).invoke(
            record.state, action, args, self.host.for_metablock(metablock_id)
        )

    def drain_events(
        self, metablock_id: str, cursor: str | None = None, limit: int = 128
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError("event limit must be between 1 and 512")
        record = self.load(metablock_id)
        if self.host.read_kernel_events is not None:
            response = self.host.read_kernel_events(metablock_id, cursor, min(limit, 256))
            if response is not None and (
                not isinstance(response, dict) or not isinstance(response.get("events"), list)
            ):
                raise MetaBlockError("kernel event spool must return a bounded event list")
            if response is not None:
                return response
        response = self._load(record.dialect_id).drain_events(
            cursor, limit, self.host.for_metablock(metablock_id)
        )
        if not isinstance(response, dict) or not isinstance(response.get("events"), list):
            raise MetaBlockError("addon drain_events must return a bounded event list")
        if len(response["events"]) > limit:
            raise MetaBlockError("addon returned more events than the requested limit")
        return response
