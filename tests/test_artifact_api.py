from __future__ import annotations

import pytest

from graphimo import artifacts


def test_public_artifact_api_stores_files_directories_and_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "cas"))
    source = tmp_path / "source.bin"
    source.write_bytes(b"shared-data")
    file_ref = artifacts.put(source, kind="checkpoint")
    assert file_ref.id
    assert file_ref.kind == "checkpoint"
    assert artifacts.get(file_ref.id) == file_ref

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "image.bin").write_bytes(b"shared-data")
    (dataset / "nested").mkdir()
    (dataset / "nested" / "label.txt").write_text("label", encoding="utf-8")
    directory_ref = artifacts.put(dataset, kind="dataset")
    destination = artifacts.materialize(directory_ref, tmp_path / "materialized")
    assert destination == tmp_path / "materialized"
    assert (destination / "image.bin").read_bytes() == b"shared-data"
    assert (destination / "nested" / "label.txt").read_text(encoding="utf-8") == "label"

    stats = artifacts.stats()
    assert stats["artifacts"] == 2
    assert stats["blobs"] == 2
    assert stats["logical_bytes"] == 2 * len(b"shared-data") + len("label")
    assert stats["physical_bytes"] == len(b"shared-data") + len("label")
    assert stats["deduplicated_bytes"] == len(b"shared-data")


def test_public_artifact_api_pins_and_collects_only_unreferenced_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "cas"))
    source = artifacts.put_bytes(b"source", kind="blob")
    child = artifacts.put_bytes(b"child", kind="checkpoint", parent_id=source)
    orphan = artifacts.put_bytes(b"orphan", kind="blob")
    artifacts.pin(child, owner="saved-model")

    first = artifacts.gc()
    assert first["artifacts_removed"] == 1
    assert artifacts.get(source.id) == source
    assert artifacts.get(child.id) == child
    with pytest.raises(FileNotFoundError):
        artifacts.get(orphan.id)

    artifacts.unpin(child, owner="saved-model")
    second = artifacts.gc(referenced=[source])
    assert second["artifacts_removed"] == 1
    assert artifacts.get(source.id) == source
    final = artifacts.gc()
    assert final["artifacts_removed"] == 1
    assert final["blobs_removed"] == 1


def test_public_artifact_api_rejects_missing_paths_and_bad_gc_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "cas"))
    with pytest.raises(ValueError, match="regular file"):
        artifacts.put(tmp_path / "missing.bin")
    with pytest.raises(ValueError, match="roots"):
        artifacts.gc(referenced=["bad-id"])


def test_public_artifact_ref_is_the_small_stable_value_object():
    ref = artifacts.ArtifactRef("a" * 64, "checkpoint")
    assert ref.id == "a" * 64
    assert ref.kind == "checkpoint"
    assert not hasattr(ref, "manifest")
