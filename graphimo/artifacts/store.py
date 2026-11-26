"""Internal streaming CAS used by Graphimo's public ArtifactStore API."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    kind: str
    manifest: dict[str, Any]
    parent_id: str | None = None


class LocalArtifactStore:
    """Immutable, content-addressed file and directory store.

    Files are stored once by SHA-256. Directory artifacts are manifests over
    those blobs. Materialization uses copy-on-write reflinks where supported,
    then falls back to a byte copy. Hardlinks are deliberately avoided because
    a consumer mutating a materialized file could corrupt the stored object.
    """

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.blobs = self.root / "objects"
        self.manifests = self.root / "manifests"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.root / "artifact_metadata.sqlite3") as db:
            db.execute("""CREATE TABLE IF NOT EXISTS artifact_parents (
                artifact_id TEXT NOT NULL, parent_id TEXT NOT NULL,
                PRIMARY KEY (artifact_id, parent_id)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS artifact_pins (
                artifact_id TEXT NOT NULL, owner TEXT NOT NULL,
                PRIMARY KEY (artifact_id, owner)
            )""")

    @staticmethod
    def _digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _valid_digest(digest: Any) -> bool:
        return (
            isinstance(digest, str)
            and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest)
        )

    def _blob_path(self, digest: str) -> Path:
        return self.blobs / digest[:2] / digest[2:]

    @staticmethod
    def _validated_metadata(metadata: Any) -> dict[str, Any] | None:
        if metadata is None:
            return None
        if not isinstance(metadata, dict):
            raise ValueError("artifact metadata must be a JSON object")
        encoded = json.dumps(metadata, ensure_ascii=False, allow_nan=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise ValueError("artifact metadata exceeds the 16 KiB limit")
        return metadata

    def put_bytes(
        self,
        data: bytes,
        *,
        kind: str = "blob",
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        if type(data) is not bytes:
            raise TypeError("artifact data must be bytes")
        digest = self._digest(data)
        path = self._blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            self._atomic_write(path, data)
        elif self._file_digest(path) != digest:
            raise ValueError("existing artifact blob checksum mismatch")
        manifest: dict[str, Any] = {"format": 1, "kind": kind, "blob": digest, "size": len(data)}
        validated = self._validated_metadata(metadata)
        if validated is not None:
            manifest["metadata"] = validated
        return self._save_manifest(manifest, parent_id)

    def put_directory(
        self,
        path: str | os.PathLike[str],
        *,
        kind: str = "dataset",
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        root = Path(path).resolve()
        if not root.is_dir():
            raise ValueError("directory artifact source must be a directory")
        files: dict[str, dict[str, Any]] = {}
        for item in sorted(root.rglob("*")):
            if item.is_symlink():
                raise ValueError(f"directory artifacts do not accept symlinks: {item}")
            if not item.is_file():
                continue
            rel = item.relative_to(root).as_posix()
            if not rel or PurePosixPath(rel).is_absolute() or ".." in PurePosixPath(rel).parts:
                raise ValueError(f"invalid artifact path: {rel}")
            digest, size = self._store_file_blob(item)
            files[rel] = {"blob": digest, "size": size}
        manifest: dict[str, Any] = {"format": 1, "kind": kind, "files": files}
        validated = self._validated_metadata(metadata)
        if validated is not None:
            manifest["metadata"] = validated
        return self._save_manifest(manifest, parent_id)

    def put_file(
        self,
        path: str | os.PathLike[str],
        *,
        kind: str = "file",
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        source = Path(path)
        if not source.is_file() or source.is_symlink():
            raise ValueError("artifact source must be a regular file")
        digest, size = self._store_file_blob(source)
        manifest: dict[str, Any] = {"format": 1, "kind": kind, "blob": digest, "size": size}
        validated = self._validated_metadata(metadata)
        if validated is not None:
            manifest["metadata"] = validated
        return self._save_manifest(manifest, parent_id)

    def _store_file_blob(self, source: Path) -> tuple[str, int]:
        digest_builder = hashlib.sha256()
        size = 0
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest_builder.update(chunk)
                size += len(chunk)
        digest = digest_builder.hexdigest()
        blob = self._blob_path(digest)
        blob.parent.mkdir(parents=True, exist_ok=True)
        if blob.exists():
            if blob.stat().st_size != size or self._file_digest(blob) != digest:
                raise ValueError("existing artifact blob checksum mismatch")
            return digest, size

        fd, temporary_name = tempfile.mkstemp(prefix=f".{blob.name}.", dir=blob.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as target, source.open("rb") as stream:
                shutil.copyfileobj(stream, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            if temporary.stat().st_size != size or self._file_digest(temporary) != digest:
                raise ValueError("artifact source changed while it was being imported")
            try:
                os.link(temporary, blob)
            except FileExistsError:
                if blob.stat().st_size != size or self._file_digest(blob) != digest:
                    raise ValueError("existing artifact blob checksum mismatch")
        finally:
            temporary.unlink(missing_ok=True)
        return digest, size

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _save_manifest(self, body: dict[str, Any], parent_id: str | None) -> ArtifactRef:
        if parent_id is not None and (
            len(parent_id) != 64 or any(c not in "0123456789abcdef" for c in parent_id)
        ):
            raise ValueError("invalid parent artifact id")
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        artifact_id = hashlib.sha256(encoded).hexdigest()
        path = self.manifests / f"{artifact_id}.json"
        if not path.exists():
            self._atomic_write(path, encoded)
        if parent_id is not None:
            with sqlite3.connect(self.root / "artifact_metadata.sqlite3") as db:
                db.execute(
                    "INSERT OR IGNORE INTO artifact_parents VALUES (?,?)", (artifact_id, parent_id)
                )
        return ArtifactRef(artifact_id, str(body["kind"]), body, parent_id)

    def get(self, artifact_id: str, *, parent_id: str | None = None) -> ArtifactRef:
        if len(artifact_id) != 64 or any(c not in "0123456789abcdef" for c in artifact_id):
            raise ValueError("invalid artifact id")
        path = self.manifests / f"{artifact_id}.json"
        body = json.loads(path.read_text(encoding="utf-8"))
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(canonical).hexdigest() != artifact_id:
            raise ValueError("artifact manifest checksum mismatch")
        if (
            not isinstance(body, dict)
            or body.get("format") != 1
            or not isinstance(body.get("kind"), str)
        ):
            raise ValueError("invalid artifact manifest")
        if "blob" in body:
            if (
                not self._valid_digest(body["blob"])
                or type(body.get("size")) is not int
                or body["size"] < 0
            ):
                raise ValueError("invalid artifact blob reference")
        elif isinstance(body.get("files"), dict):
            for rel, item in body["files"].items():
                rel_path = PurePosixPath(rel) if isinstance(rel, str) else PurePosixPath("..")
                if rel_path.is_absolute() or ".." in rel_path.parts or not rel_path.parts:
                    raise ValueError("invalid path in artifact manifest")
                if (
                    not isinstance(item, dict)
                    or not self._valid_digest(item.get("blob"))
                    or type(item.get("size")) is not int
                    or item["size"] < 0
                ):
                    raise ValueError("invalid blob entry in artifact manifest")
        else:
            raise ValueError("invalid artifact manifest shape")
        if "metadata" in body and not isinstance(body["metadata"], dict):
            raise ValueError("invalid artifact metadata")
        if parent_id is None:
            parents = self.parents(artifact_id)
            parent_id = parents[0] if parents else None
        return ArtifactRef(artifact_id, str(body["kind"]), body, parent_id)

    def parents(self, artifact_id: str) -> list[str]:
        if len(artifact_id) != 64 or any(c not in "0123456789abcdef" for c in artifact_id):
            raise ValueError("invalid artifact id")
        with sqlite3.connect(self.root / "artifact_metadata.sqlite3") as db:
            rows = db.execute(
                "SELECT parent_id FROM artifact_parents WHERE artifact_id=? ORDER BY parent_id",
                (artifact_id,),
            ).fetchall()
        return [row[0] for row in rows]

    def _pins_index(self) -> dict[str, list[str]]:
        with sqlite3.connect(self.root / "artifact_metadata.sqlite3") as db:
            rows = db.execute(
                "SELECT artifact_id, owner FROM artifact_pins ORDER BY owner"
            ).fetchall()
        index: dict[str, list[str]] = {}
        for artifact_id, owner in rows:
            index.setdefault(artifact_id, []).append(owner)
        return index

    def _summary(
        self, artifact_id: str, body: dict[str, Any], path: Path, pins: list[str]
    ) -> dict[str, Any]:
        if "blob" in body:
            size = int(body["size"])
            files = None
        else:
            size = sum(int(item["size"]) for item in body.get("files", {}).values())
            files = len(body.get("files", {}))
        return {
            "artifact_id": artifact_id,
            "kind": str(body["kind"]),
            "size_bytes": size,
            "files": files,
            "created_unix": path.stat().st_mtime,
            "parents": self.parents(artifact_id),
            "pins": pins,
            "metadata": body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
        }

    def describe(self, artifact_id: str) -> dict[str, Any]:
        """Return the full catalogue entry for one artifact."""
        ref = self.get(artifact_id)
        path = self.manifests / f"{ref.artifact_id}.json"
        pins = self._pins_index().get(ref.artifact_id, [])
        return self._summary(ref.artifact_id, ref.manifest, path, pins)

    def list(
        self,
        *,
        kind: str | None = None,
        parent_id: str | None = None,
        pinned: bool | None = None,
        cursor: str | None = None,
        limit: int = 64,
    ) -> dict[str, Any]:
        """Paginated catalogue of stored artifacts, newest first.

        ``cursor`` is an opaque continuation token returned as ``next_cursor``;
        it is an offset into the mtime-descending ordering and stays valid while
        no new artifact is inserted.
        """
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError("list limit must be an integer between 1 and 256")
        if kind is not None and (not isinstance(kind, str) or not kind or len(kind) > 128):
            raise ValueError("kind filter must be a non-empty string of at most 128 characters")
        if parent_id is not None and not self._valid_digest(parent_id):
            raise ValueError("parent_id filter must be a valid artifact id")
        offset = 0
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 256:
                raise ValueError("invalid list cursor")
            try:
                decoded = json.loads(cursor)
            except (ValueError, TypeError) as exc:
                raise ValueError("invalid list cursor") from exc
            if not isinstance(decoded, dict) or type(decoded.get("offset")) is not int:
                raise ValueError("invalid list cursor")
            offset = decoded["offset"]
            if offset < 0:
                raise ValueError("invalid list cursor")
        pins_index = self._pins_index()
        parent_map: dict[str, list[str]] = {}
        with sqlite3.connect(self.root / "artifact_metadata.sqlite3") as db:
            for child, parent in db.execute("SELECT artifact_id, parent_id FROM artifact_parents"):
                parent_map.setdefault(child, []).append(parent)
        entries: list[tuple[float, str, dict[str, Any]]] = []
        for path in self.manifests.glob("*.json"):
            artifact_id = path.stem
            if not self._valid_digest(artifact_id):
                continue
            try:
                ref = self.get(artifact_id)
            except (ValueError, OSError, json.JSONDecodeError):
                continue
            if kind is not None and ref.kind != kind:
                continue
            if parent_id is not None and parent_id not in parent_map.get(artifact_id, []):
                continue
            pins = pins_index.get(artifact_id, [])
            if pinned is True and not pins:
                continue
            if pinned is False and pins:
                continue
            entries.append((path.stat().st_mtime, artifact_id, ref.manifest))
        entries.sort(key=lambda item: (-item[0], item[1]))
        page = entries[offset : offset + limit]
        items = [
            self._summary(
                artifact_id,
                manifest,
                self.manifests / f"{artifact_id}.json",
                pins_index.get(artifact_id, []),
            )
            for _mtime, artifact_id, manifest in page
        ]
        next_cursor = None
        if offset + limit < len(entries):
            next_cursor = json.dumps({"offset": offset + limit}, separators=(",", ":"))
        return {"items": items, "next_cursor": next_cursor, "scanned": len(entries)}

    def pin(self, artifact_id: str, owner: str) -> None:
        if (
            not self._valid_digest(artifact_id)
            or not isinstance(owner, str)
            or not owner
            or len(owner) > 200
        ):
            raise ValueError(
                "pin requires a valid artifact id and a non-empty owner of at most 200 characters"
            )
        self.get(artifact_id)
        with sqlite3.connect(self.root / "artifact_metadata.sqlite3") as db:
            db.execute("INSERT OR IGNORE INTO artifact_pins VALUES (?,?)", (artifact_id, owner))

    def unpin(self, artifact_id: str, owner: str) -> None:
        with sqlite3.connect(self.root / "artifact_metadata.sqlite3") as db:
            db.execute(
                "DELETE FROM artifact_pins WHERE artifact_id=? AND owner=?", (artifact_id, owner)
            )

    def stats(self) -> dict[str, int]:
        """Report referenced logical bytes and unique stored blob bytes."""
        logical_bytes = 0
        artifact_count = 0
        for manifest_path in self.manifests.glob("*.json"):
            ref = self.get(manifest_path.stem)
            artifact_count += 1
            if "blob" in ref.manifest:
                logical_bytes += int(ref.manifest["size"])
            else:
                logical_bytes += sum(int(item["size"]) for item in ref.manifest["files"].values())
        blob_paths = [path for path in self.blobs.glob("*/*") if path.is_file()]
        physical_bytes = sum(path.stat().st_size for path in blob_paths)
        return {
            "artifacts": artifact_count,
            "blobs": len(blob_paths),
            "logical_bytes": logical_bytes,
            "physical_bytes": physical_bytes,
            "deduplicated_bytes": max(0, logical_bytes - physical_bytes),
        }

    def gc(self, referenced_ids: set[str] | None = None) -> dict[str, int]:
        """Delete unreferenced manifests/blobs, retaining pinned roots and ancestors.

        The caller passes artifact IDs still referenced by workspace/run records.
        Pins provide explicit user or system retention roots.
        """
        with sqlite3.connect(self.root / "artifact_metadata.sqlite3") as db:
            pins = {row[0] for row in db.execute("SELECT DISTINCT artifact_id FROM artifact_pins")}
            parent_map: dict[str, set[str]] = {}
            for child, parent in db.execute("SELECT artifact_id,parent_id FROM artifact_parents"):
                parent_map.setdefault(child, set()).add(parent)
        roots = set(referenced_ids or ()) | pins
        if any(not self._valid_digest(item) for item in roots):
            raise ValueError("garbage-collection roots must be valid artifact ids")

        all_manifests: dict[str, Path] = {}
        manifest_data: dict[str, dict[str, Any]] = {}
        for path in self.manifests.glob("*.json"):
            artifact_id = path.stem
            if not self._valid_digest(artifact_id):
                continue
            ref = self.get(artifact_id)
            all_manifests[artifact_id] = path
            manifest_data[artifact_id] = ref.manifest

        reachable: set[str] = set()
        pending = list(roots)
        while pending:
            artifact_id = pending.pop()
            if artifact_id in reachable:
                continue
            if artifact_id not in all_manifests:
                continue
            reachable.add(artifact_id)
            pending.extend(parent_map.get(artifact_id, ()))

        live_blobs: set[str] = set()
        for artifact_id in reachable:
            manifest = manifest_data[artifact_id]
            if "blob" in manifest:
                live_blobs.add(manifest["blob"])
            else:
                live_blobs.update(item["blob"] for item in manifest.get("files", {}).values())

        deleted_bytes = 0
        deleted_artifacts = 0
        deleted_blobs = 0
        removed = set(all_manifests) - reachable
        for artifact_id in removed:
            all_manifests[artifact_id].unlink(missing_ok=True)
            deleted_artifacts += 1
        for path in self.blobs.glob("*/*"):
            if not path.is_file():
                continue
            digest = path.parent.name + path.name
            if digest not in live_blobs:
                deleted_bytes += path.stat().st_size
                path.unlink(missing_ok=True)
                deleted_blobs += 1
        with sqlite3.connect(self.root / "artifact_metadata.sqlite3") as db:
            for artifact_id in removed:
                db.execute(
                    "DELETE FROM artifact_parents WHERE artifact_id=? OR parent_id=?",
                    (artifact_id, artifact_id),
                )
                db.execute("DELETE FROM artifact_pins WHERE artifact_id=?", (artifact_id,))
        return {
            "artifacts_removed": deleted_artifacts,
            "blobs_removed": deleted_blobs,
            "bytes_reclaimed": deleted_bytes,
        }

    def _verified_blob(self, digest: str) -> Path:
        if not self._valid_digest(digest):
            raise ValueError("invalid artifact blob id")
        path = self._blob_path(digest)
        if self._file_digest(path) != digest:
            raise ValueError("artifact blob checksum mismatch")
        return path

    def read_bytes(self, ref: ArtifactRef) -> bytes:
        digest = ref.manifest.get("blob")
        if not isinstance(digest, str):
            raise ValueError("artifact is not a byte artifact")  # noqa: TRY004
        data = self._verified_blob(digest).read_bytes()
        return data

    def read_file(self, ref: ArtifactRef, relative_path: str) -> bytes:
        if not isinstance(relative_path, str):
            raise TypeError("artifact file path must be a string")
        path = PurePosixPath(relative_path)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("invalid artifact file path")
        files = ref.manifest.get("files")
        item = files.get(path.as_posix()) if isinstance(files, dict) else None
        if not isinstance(item, dict):
            raise FileNotFoundError(relative_path)
        blob = self._verified_blob(item["blob"])
        data = blob.read_bytes()
        if len(data) != item["size"]:
            raise ValueError("artifact file size mismatch")
        return data

    def materialize(self, ref: ArtifactRef, destination: str | os.PathLike[str]) -> dict[str, Any]:
        target = Path(destination)
        if "blob" in ref.manifest:
            target.parent.mkdir(parents=True, exist_ok=True)
            method = self._copy_blob(self._verified_blob(ref.manifest["blob"]), target)
            return {"method": method, "files": 1, "bytes": ref.manifest["size"]}
        target.mkdir(parents=True, exist_ok=True)
        methods: dict[str, int] = {"reflink": 0, "copy": 0}
        total = 0
        files = ref.manifest.get("files")
        if not isinstance(files, dict):
            raise ValueError("invalid directory artifact manifest")  # noqa: TRY004
        for rel, item in files.items():
            rel_path = PurePosixPath(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                raise ValueError("invalid path in artifact manifest")
            output = target.joinpath(*rel_path.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            methods[self._copy_blob(self._verified_blob(item["blob"]), output)] += 1
            total += int(item["size"])
        used_methods = [name for name, count in methods.items() if count]
        method = (
            "empty" if not used_methods else ("mixed" if len(used_methods) > 1 else used_methods[0])
        )
        return {"method": method, "methods": methods, "files": len(files), "bytes": total}

    @staticmethod
    def _copy_blob(source: Path, destination: Path) -> str:
        # APFS clonefile and Linux FICLONE provide copy-on-write snapshots.
        if os.name == "posix" and hasattr(os, "uname") and os.uname().sysname == "Darwin":
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                clonefile = libc.clonefile
                clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
                clonefile.restype = ctypes.c_int
                if clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
                    return "reflink"
            except (AttributeError, OSError):
                destination.unlink(missing_ok=True)
        if os.name == "posix" and hasattr(os, "uname") and os.uname().sysname == "Linux":
            try:
                import fcntl

                with source.open("rb") as src, destination.open("xb") as dst:
                    fcntl.ioctl(dst.fileno(), 0x40049409, src.fileno())
                return "reflink"
            except (OSError, ImportError):
                destination.unlink(missing_ok=True)
        with source.open("rb") as src, destination.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        return "copy"

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
