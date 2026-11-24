from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from graphimo import artifacts
from graphimo.artifacts import LocalArtifactStore


def test_public_artifact_api_persists_materializes_and_reports_dedup(tmp_path: Path) -> None:
    store = artifacts.configure_default_store(tmp_path / "store")
    source = tmp_path / "source.bin"
    source.write_bytes(b"shared content")

    first = artifacts.put(source, kind="dataset-file")
    second = artifacts.put_bytes(b"shared content", kind="copy")
    assert first.id == hashlib.sha256(
        b'{"blob":"' + hashlib.sha256(b"shared content").hexdigest().encode()
        + b'","format":1,"kind":"dataset-file","size":14}'
    ).hexdigest()
    assert (first.id, first.kind) != (second.id, second.kind)

    result = artifacts.materialize(first, tmp_path / "materialized.bin")
    assert result == tmp_path / "materialized.bin"
    assert result.read_bytes() == b"shared content"

    usage = artifacts.stats()
    assert usage.logical_bytes == 28
    assert usage.physical_stored_bytes == 14
    assert usage.deduplicated_bytes == 14
    assert usage.artifacts == 2
    assert usage.blobs == 1

    artifacts.pin(first, owner="test-retention")
    report = artifacts.gc(referenced=[second])
    assert report.artifacts_removed == 0
    artifacts.unpin(first, owner="test-retention")
    report = artifacts.gc(referenced=[second])
    assert report.artifacts_removed == 1
    assert report.bytes_reclaimed == 0
    assert store.get(second.id) == second


def test_directory_artifact_deduplicates_files_and_preserves_empty_dirs(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "store")
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "empty").mkdir(parents=True)
    right.mkdir()
    (left / "a.bin").write_bytes(b"same")
    (left / "b.bin").write_bytes(b"same")
    (right / "a.bin").write_bytes(b"same")

    first = store.put(left, kind="dataset")
    second = store.put(right, kind="dataset")
    assert store.stats().physical_stored_bytes == 4
    assert store.stats().deduplicated_bytes == 8
    target = store.materialize(first, tmp_path / "out")
    assert (target / "a.bin").read_bytes() == b"same"
    assert (target / "b.bin").read_bytes() == b"same"
    assert (target / "empty").is_dir()
    assert store.get(second.id) == second


def test_source_hashing_and_blob_verification_never_call_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalArtifactStore(tmp_path / "store")
    source = tmp_path / "large.bin"
    source.write_bytes(b"x" * (3 * 1024 * 1024 + 17))

    def forbidden_read_bytes(_self: Path) -> bytes:
        raise AssertionError("large artifact hashing/verification must stream")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    artifact = store.put(source)
    digest, _ = store._digest_file(source)
    assert store._verify_blob(digest).is_file()
    assert artifact.kind == "file"


def test_gc_keeps_parent_lineage_and_rejects_bad_paths_and_symlinks(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "store")
    source = store.put_bytes(b"parent")
    derived = store.put_bytes(b"child", parent=source)
    orphan = store.put_bytes(b"orphan")
    report = store.gc(referenced=[derived])
    assert report.artifacts_removed == 1
    assert store.get(source.id) == source
    with pytest.raises(FileNotFoundError):
        store.get(orphan.id)

    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "outside").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="symlink"):
        store.put(folder)

    with pytest.raises(ValueError, match="invalid artifact path"):
        from graphimo.artifacts.local import _safe_relative_path

        _safe_relative_path("../escape")
