"""Local streaming content-addressed ArtifactStore implementation."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable

from .types import ArtifactRef, ArtifactStats, GarbageCollectionReport

_CHUNK_SIZE = 1024 * 1024


class LocalArtifactStore:
    """Immutable local file/directory store with streaming SHA-256.

    Files are stored once by content hash. Directory artifacts are manifests
    over those blobs, so unchanged files are shared. Materialization uses
    APFS/Linux copy-on-write clones when available and a streaming copy
    otherwise. Hard links are never used because consumers may mutate outputs.
    """

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve()
        self.blobs = self.root / "objects"
        self.manifests = self.root / "manifests"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.root / "artifact_metadata.sqlite3"
        with sqlite3.connect(self.metadata_path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS artifact_parents (
                    artifact_id TEXT NOT NULL, parent_id TEXT NOT NULL,
                    PRIMARY KEY (artifact_id, parent_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS artifact_pins (
                    artifact_id TEXT NOT NULL, owner TEXT NOT NULL,
                    PRIMARY KEY (artifact_id, owner)
                )"""
            )

    @staticmethod
    def _valid_digest(value: object) -> bool:
        return isinstance(value, str) and len(value) == 64 and all(
            char in "0123456789abcdef" for char in value
        )

    @staticmethod
    def _digest_stream(stream: BinaryIO) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
        return digest.hexdigest(), size

    @classmethod
    def _digest_file(cls, path: Path) -> tuple[str, int]:
        with path.open("rb") as stream:
            return cls._digest_stream(stream)

    def _blob_path(self, digest: str) -> Path:
        return self.blobs / digest[:2] / digest[2:]

    def put_bytes(
        self,
        data: bytes,
        *,
        kind: str = "blob",
        parent: ArtifactRef | str | None = None,
    ) -> ArtifactRef:
        if type(data) is not bytes:
            raise TypeError("artifact data must be bytes")
        import io

        digest, size = self._digest_stream(io.BytesIO(data))
        self._store_stream(io.BytesIO(data), digest, size)
        return self._save_manifest({"format": 1, "kind": kind, "blob": digest, "size": size}, parent)

    def put(
        self,
        path: str | os.PathLike[str],
        *,
        kind: str = "file",
        parent: ArtifactRef | str | None = None,
    ) -> ArtifactRef:
        source = Path(path).expanduser()
        if source.is_symlink():
            raise ValueError(f"artifact source cannot be a symlink: {source}")
        if source.is_file():
            digest, size = self._digest_file(source)
            with source.open("rb") as stream:
                self._store_stream(stream, digest, size)
            return self._save_manifest(
                {"format": 1, "kind": kind, "blob": digest, "size": size}, parent
            )
        if source.is_dir():
            return self.put_directory(source, kind=kind, parent=parent)
        raise ValueError("artifact source must be an existing regular file or directory")

    def put_directory(
        self,
        path: str | os.PathLike[str],
        *,
        kind: str = "dataset",
        parent: ArtifactRef | str | None = None,
    ) -> ArtifactRef:
        root = Path(path).expanduser()
        if root.is_symlink() or not root.is_dir():
            raise ValueError("directory artifact source must be a real directory")
        root = root.resolve()
        files: dict[str, dict[str, str | int]] = {}
        directories: list[str] = []
        for item in sorted(root.rglob("*")):
            relative = item.relative_to(root).as_posix()
            rel_path = _safe_relative_path(relative)
            if item.is_symlink():
                raise ValueError(f"directory artifacts do not accept symlinks: {item}")
            if item.is_dir():
                directories.append(rel_path.as_posix())
                continue
            if not item.is_file():
                raise ValueError(f"unsupported filesystem entry in artifact: {item}")
            digest, size = self._digest_file(item)
            with item.open("rb") as stream:
                self._store_stream(stream, digest, size)
            files[rel_path.as_posix()] = {"blob": digest, "size": size}
        manifest: dict[str, object] = {"format": 1, "kind": kind, "files": files}
        if directories:
            manifest["directories"] = directories
        return self._save_manifest(manifest, parent)

    def _store_stream(self, stream: BinaryIO, digest: str, expected_size: int) -> Path:
        target = self._blob_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self._verify_blob(digest, expected_size)
            return target

        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temp_path = Path(temporary)
        hasher = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as output:
                while chunk := stream.read(_CHUNK_SIZE):
                    hasher.update(chunk)
                    size += len(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if hasher.hexdigest() != digest or size != expected_size:
                raise ValueError("artifact source changed while it was being stored")
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)
        return target

    def _save_manifest(
        self,
        body: dict[str, object],
        parent: ArtifactRef | str | None,
    ) -> ArtifactRef:
        parent_id = parent.id if isinstance(parent, ArtifactRef) else parent
        if parent_id is not None and not self._valid_digest(parent_id):
            raise ValueError("invalid parent artifact id")
        if not isinstance(body.get("kind"), str) or not body["kind"]:
            raise ValueError("artifact kind must be a non-empty string")
        encoded = _canonical_json(body)
        artifact_id = hashlib.sha256(encoded).hexdigest()
        manifest_path = self.manifests / f"{artifact_id}.json"
        if not manifest_path.exists():
            _atomic_write(manifest_path, encoded)
        if parent_id is not None:
            # Ensure lineage never points at a nonexistent artifact.
            self.get(parent_id)
            with sqlite3.connect(self.metadata_path) as db:
                db.execute(
                    "INSERT OR IGNORE INTO artifact_parents VALUES (?,?)",
                    (artifact_id, parent_id),
                )
        return ArtifactRef(artifact_id, str(body["kind"]))

    def get(self, artifact_id: str) -> ArtifactRef:
        if not self._valid_digest(artifact_id):
            raise ValueError("invalid artifact id")
        path = self.manifests / f"{artifact_id}.json"
        body = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(body, dict) or hashlib.sha256(_canonical_json(body)).hexdigest() != artifact_id:
            raise ValueError("artifact manifest checksum mismatch")
        if body.get("format") != 1 or not isinstance(body.get("kind"), str):
            raise ValueError("invalid artifact manifest")
        if "blob" in body:
            if not self._valid_digest(body["blob"]) or not _valid_size(body.get("size")):
                raise ValueError("invalid artifact blob reference")
        elif isinstance(body.get("files"), dict):
            for relative, entry in body["files"].items():
                _safe_relative_path(relative)
                if (
                    not isinstance(entry, dict)
                    or not self._valid_digest(entry.get("blob"))
                    or not _valid_size(entry.get("size"))
                ):
                    raise ValueError("invalid blob entry in artifact manifest")
            directories = body.get("directories", [])
            if not isinstance(directories, list):
                raise ValueError("invalid directory list in artifact manifest")
            for relative in directories:
                _safe_relative_path(relative)
        else:
            raise ValueError("invalid artifact manifest shape")
        return ArtifactRef(artifact_id, body["kind"])

    def _manifest(self, ref: ArtifactRef) -> dict[str, object]:
        validated = self.get(ref.id)
        if validated.kind != ref.kind:
            raise ValueError("artifact reference kind does not match stored manifest")
        return json.loads((self.manifests / f"{ref.id}.json").read_text(encoding="utf-8"))

    def parents(self, artifact: ArtifactRef | str) -> tuple[ArtifactRef, ...]:
        artifact_id = artifact.id if isinstance(artifact, ArtifactRef) else artifact
        if not self._valid_digest(artifact_id):
            raise ValueError("invalid artifact id")
        with sqlite3.connect(self.metadata_path) as db:
            rows = db.execute(
                "SELECT parent_id FROM artifact_parents WHERE artifact_id=? ORDER BY parent_id",
                (artifact_id,),
            ).fetchall()
        return tuple(self.get(row[0]) for row in rows)

    def pin(self, artifact: ArtifactRef | str, *, owner: str) -> None:
        artifact_id = artifact.id if isinstance(artifact, ArtifactRef) else artifact
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 200:
            raise ValueError("pin owner must be non-empty and at most 200 characters")
        self.get(artifact_id)
        with sqlite3.connect(self.metadata_path) as db:
            db.execute(
                "INSERT OR IGNORE INTO artifact_pins VALUES (?,?)", (artifact_id, owner)
            )

    def unpin(self, artifact: ArtifactRef | str, *, owner: str) -> None:
        artifact_id = artifact.id if isinstance(artifact, ArtifactRef) else artifact
        if not self._valid_digest(artifact_id):
            raise ValueError("invalid artifact id")
        with sqlite3.connect(self.metadata_path) as db:
            db.execute("DELETE FROM artifact_pins WHERE artifact_id=? AND owner=?", (artifact_id, owner))

    def stats(self) -> ArtifactStats:
        logical_bytes = 0
        artifact_count = 0
        for path in self.manifests.glob("*.json"):
            body = json.loads(path.read_text(encoding="utf-8"))
            artifact_count += 1
            if "blob" in body:
                logical_bytes += int(body["size"])
            else:
                logical_bytes += sum(int(item["size"]) for item in body.get("files", {}).values())
        physical_bytes = 0
        blob_count = 0
        for path in self.blobs.glob("*/*"):
            if path.is_file():
                physical_bytes += path.stat().st_size
                blob_count += 1
        return ArtifactStats(
            logical_bytes=logical_bytes,
            physical_stored_bytes=physical_bytes,
            deduplicated_bytes=max(logical_bytes - physical_bytes, 0),
            artifacts=artifact_count,
            blobs=blob_count,
        )

    def gc(
        self,
        *,
        referenced: Iterable[ArtifactRef | str] = (),
    ) -> GarbageCollectionReport:
        roots = {item.id if isinstance(item, ArtifactRef) else item for item in referenced}
        if any(not self._valid_digest(item) for item in roots):
            raise ValueError("garbage-collection roots must be valid artifact ids or references")
        with sqlite3.connect(self.metadata_path) as db:
            roots.update(row[0] for row in db.execute("SELECT DISTINCT artifact_id FROM artifact_pins"))
            parents: dict[str, set[str]] = {}
            for child, parent_id in db.execute("SELECT artifact_id,parent_id FROM artifact_parents"):
                parents.setdefault(child, set()).add(parent_id)

        manifests: dict[str, dict[str, object]] = {}
        for path in self.manifests.glob("*.json"):
            artifact_id = path.stem
            if self._valid_digest(artifact_id):
                self.get(artifact_id)
                manifests[artifact_id] = json.loads(path.read_text(encoding="utf-8"))

        reachable: set[str] = set()
        pending = list(roots)
        while pending:
            current = pending.pop()
            if current in reachable or current not in manifests:
                continue
            reachable.add(current)
            pending.extend(parents.get(current, ()))

        live_blobs: set[str] = set()
        for artifact_id in reachable:
            body = manifests[artifact_id]
            if "blob" in body:
                live_blobs.add(str(body["blob"]))
            else:
                live_blobs.update(str(item["blob"]) for item in body.get("files", {}).values())

        removed_manifests = set(manifests) - reachable
        for artifact_id in removed_manifests:
            (self.manifests / f"{artifact_id}.json").unlink(missing_ok=True)
        removed_blobs = 0
        reclaimed = 0
        for path in self.blobs.glob("*/*"):
            if path.is_file() and path.parent.name + path.name not in live_blobs:
                reclaimed += path.stat().st_size
                path.unlink(missing_ok=True)
                removed_blobs += 1
        with sqlite3.connect(self.metadata_path) as db:
            for artifact_id in removed_manifests:
                db.execute(
                    "DELETE FROM artifact_parents WHERE artifact_id=? OR parent_id=?",
                    (artifact_id, artifact_id),
                )
                db.execute("DELETE FROM artifact_pins WHERE artifact_id=?", (artifact_id,))
        return GarbageCollectionReport(len(removed_manifests), removed_blobs, reclaimed)

    def materialize(
        self,
        artifact: ArtifactRef,
        destination: str | os.PathLike[str],
    ) -> Path:
        body = self._manifest(artifact)
        target = Path(destination).expanduser()
        if "blob" in body:
            source = self._verify_blob(str(body["blob"]), int(body["size"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            self._copy_blob(source, target)
            return target

        target.mkdir(parents=True, exist_ok=True)
        for relative in body.get("directories", []):
            target.joinpath(*_safe_relative_path(relative).parts).mkdir(parents=True, exist_ok=True)
        for relative, entry in body["files"].items():
            output = target.joinpath(*_safe_relative_path(relative).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            blob = self._verify_blob(str(entry["blob"]), int(entry["size"]))
            self._copy_blob(blob, output)
        return target

    def read_bytes(self, artifact: ArtifactRef) -> bytes:
        """Read an explicitly requested byte artifact (not used for hashing/UI)."""
        body = self._manifest(artifact)
        if "blob" not in body:
            raise ValueError("artifact is not a byte artifact")
        return self._verify_blob(str(body["blob"]), int(body["size"])).read_bytes()

    def _verify_blob(self, digest: str, expected_size: int | None = None) -> Path:
        if not self._valid_digest(digest):
            raise ValueError("invalid artifact blob id")
        path = self._blob_path(digest)
        actual_digest, actual_size = self._digest_file(path)
        if actual_digest != digest or (expected_size is not None and actual_size != expected_size):
            raise ValueError("artifact blob checksum or size mismatch")
        return path

    @staticmethod
    def _copy_blob(source: Path, destination: Path) -> str:
        destination.unlink(missing_ok=True)
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
        with source.open("rb") as src, destination.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=_CHUNK_SIZE)
        return "copy"


def _safe_relative_path(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise ValueError("artifact path must be a string")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or "" in path.parts:
        raise ValueError(f"invalid artifact path: {value}")
    return path


def _valid_size(value: object) -> bool:
    return type(value) is int and value >= 0


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
