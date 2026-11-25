"""Generic, addon-owned MetaBlock semantics and persistence.

Core treats addon state as opaque JSON. Dialects own their schema and edit
semantics; the host owns project files, artifacts, events and materialization
cell updates.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

META_DIALECT_ENTRY_POINT_GROUP = "graphimo.metablocks"
MAX_STATE_BYTES = 8 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DIALECT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class MetaBlockError(RuntimeError):
    """Base error for generic MetaBlock operations."""


class AddonUnavailableError(MetaBlockError):
    """Raised when an operation needs a dialect that is not installed."""


class MetaBlockStateError(MetaBlockError):
    """Raised when a sidecar is malformed or exceeds its persistence limit."""


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
    current_scope: Callable[[], str] = lambda: "root"

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
    state_bytes = _encode_json(value["state"])
    return MetaBlockRecord(
        metablock_id=value["metablock_id"],
        dialect_id=value["dialect_id"],
        addon_version=value["addon_version"],
        api_version=value["api_version"],
        state=value["state"],
        revision=hashlib.sha256(state_bytes).hexdigest(),
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

    def _load(self, dialect_id: str) -> MetaBlockDialect:
        handler = self._handlers.get(dialect_id)
        if handler is not None:
            return handler
        entry_point = next((item for item in self._entry_points() if item.name == dialect_id), None)
        if entry_point is None:
            raise AddonUnavailableError(f"MetaBlock addon '{dialect_id}' is not installed")
        registration = entry_point.load()
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
        state = handler.create_state(request, self.host)
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
        projection = handler.project(record.state, self.host.current_scope(), self.host)
        self._write_record(record)
        return {
            "available": True,
            "metablock_id": record.metablock_id,
            "dialect_id": record.dialect_id,
            "addon_version": record.addon_version,
            "revision": record.revision,
            "projection": projection,
        }

    def project(self, metablock_id: str, scope: str = "root") -> dict[str, Any]:
        record = self.load(metablock_id)
        try:
            handler = self._load(record.dialect_id)
        except AddonUnavailableError:
            return {
                "available": False,
                "metablock_id": record.metablock_id,
                "dialect_id": record.dialect_id,
                "addon_version": record.addon_version,
                "revision": record.revision,
                "state": record.state,
                "diagnostics": [
                    {
                        "code": "metablock_addon_unavailable",
                        "message": f"MetaBlock addon '{record.dialect_id}' is not installed",
                        "severity": "warning",
                    }
                ],
            }
        projection = handler.project(record.state, scope, self.host)
        return {
            "available": True,
            "metablock_id": record.metablock_id,
            "dialect_id": record.dialect_id,
            "addon_version": record.addon_version,
            "revision": record.revision,
            "projection": projection,
        }

    def catalog(self, metablock_id: str, scope: str) -> Any:
        record = self.load(metablock_id)
        return self._load(record.dialect_id).catalog(record.state, scope, self.host)

    def apply(self, metablock_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        record = self.load(metablock_id)
        handler = self._load(record.dialect_id)
        updated = handler.apply(record.state, intent, self.host)
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
        projection = handler.project(next_record.state, self.host.current_scope(), self.host)
        self._write_record(next_record)
        return {
            "available": True,
            "metablock_id": next_record.metablock_id,
            "dialect_id": next_record.dialect_id,
            "addon_version": next_record.addon_version,
            "revision": next_record.revision,
            "projection": projection,
        }

    def materialize(self, metablock_id: str, bindings: dict[str, Any]) -> dict[str, Any]:
        record = self.load(metablock_id)
        result = self._load(record.dialect_id).build_materialization(
            record.state, bindings, self.host
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
        return result

    def invoke(self, metablock_id: str, action: str, args: dict[str, Any]) -> Any:
        record = self.load(metablock_id)
        return self._load(record.dialect_id).invoke(record.state, action, args, self.host)

    def drain_events(
        self, metablock_id: str, cursor: str | None = None, limit: int = 128
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError("event limit must be between 1 and 512")
        record = self.load(metablock_id)
        response = self._load(record.dialect_id).drain_events(cursor, limit, self.host)
        if not isinstance(response, dict) or not isinstance(response.get("events"), list):
            raise MetaBlockError("addon drain_events must return a bounded event list")
        if len(response["events"]) > limit:
            raise MetaBlockError("addon returned more events than the requested limit")
        return response
