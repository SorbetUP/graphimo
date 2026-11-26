"""Catalogue, metadata, pins, run manifests and bounded previews for artifacts."""

from __future__ import annotations

import json

import pytest

from graphimo import artifacts
from graphimo.application import GraphimoApplication
from graphimo.metablocks import GraphimoHostAPI
from graphimo.preview import preview_value
from graphimo.service.sessions import HostArtifactStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "cas"))
    return tmp_path / "cas"


def _app(tmp_path) -> GraphimoApplication:
    host = GraphimoHostAPI(
        "catalogue",
        tmp_path / "notebook.py",
        artifact_store=HostArtifactStore(),
    )
    return GraphimoApplication(host)


def test_metadata_roundtrips_through_describe_and_list(store) -> None:
    first = artifacts.put_bytes(b"one", kind="checkpoint", metadata={"model": "A", "epoch": 3})
    second = artifacts.put_bytes(b"two", kind="dataset", metadata={"split": "train"})
    orphan = artifacts.put_bytes(b"three", kind="checkpoint")

    entry = artifacts.describe(first.id)
    assert entry["artifact_id"] == first.id
    assert entry["kind"] == "checkpoint"
    assert entry["size_bytes"] == 3
    assert entry["metadata"] == {"model": "A", "epoch": 3}
    assert entry["pins"] == []

    listing = artifacts.list(kind="checkpoint")
    assert {item["artifact_id"] for item in listing["items"]} == {first.id, orphan.id}
    assert listing["next_cursor"] is None
    assert artifacts.list(kind="dataset")["items"][0]["artifact_id"] == second.id
    assert artifacts.list(kind="missing")["items"] == []


def test_list_paginates_with_opaque_cursor_and_filters_pins(store) -> None:
    refs = [artifacts.put_bytes(f"payload-{index}".encode(), kind="blob") for index in range(5)]
    artifacts.pin(refs[0], owner="keep")
    page = artifacts.list(limit=2)
    assert len(page["items"]) == 2
    assert page["next_cursor"] is not None
    rest = artifacts.list(limit=2, cursor=page["next_cursor"])
    assert len(rest["items"]) == 2
    assert rest["next_cursor"] is not None
    last = artifacts.list(limit=2, cursor=rest["next_cursor"])
    assert len(last["items"]) == 1
    assert last["next_cursor"] is None
    seen = {item["artifact_id"] for item in page["items"] + rest["items"] + last["items"]}
    assert seen == {ref.id for ref in refs}

    pinned = artifacts.list(pinned=True)
    assert [item["artifact_id"] for item in pinned["items"]] == [refs[0].id]
    assert all(item["pins"] == ["keep"] for item in pinned["items"])
    unpinned = artifacts.list(pinned=False)
    assert refs[0].id not in {item["artifact_id"] for item in unpinned["items"]}

    children = artifacts.put_bytes(b"child", kind="blob", parent_id=refs[1])
    by_parent = artifacts.list(parent_id=refs[1].id)
    assert [item["artifact_id"] for item in by_parent["items"]] == [children.id]

    with pytest.raises(ValueError, match="limit"):
        artifacts.list(limit=0)
    with pytest.raises(ValueError, match="cursor"):
        artifacts.list(cursor="not-a-cursor")


def test_metadata_is_bounded_and_must_be_an_object(store) -> None:
    with pytest.raises(ValueError, match="metadata"):
        artifacts.put_bytes(b"x", metadata=["not", "an", "object"])
    with pytest.raises(ValueError, match="16 KiB"):
        artifacts.put_bytes(b"x", metadata={"blob": "y" * (17 * 1024)})
    ref = artifacts.put_bytes(b"x", metadata={"ok": True})
    assert artifacts.describe(ref.id)["metadata"] == {"ok": True}


def test_gc_keeps_run_manifest_ancestors(store) -> None:
    run = artifacts.put_bytes(
        json.dumps({"format": 1, "run_id": "r1", "metrics": []}).encode(),
        kind="run-manifest",
        metadata={"run_id": "r1"},
    )
    child = artifacts.put_bytes(
        json.dumps(
            {"format": 1, "run_id": "r1", "metrics": [{"name": "loss", "value": 0.5}]}
        ).encode(),
        kind="run-manifest",
        parent_id=run,
        metadata={"run_id": "r1", "metrics": 1},
    )
    orphan = artifacts.put_bytes(b"orphan", kind="blob")
    result = artifacts.gc(referenced=[child.id])
    assert result["artifacts_removed"] == 1
    assert artifacts.get(run.id).id == run.id
    assert artifacts.get(child.id).id == child.id
    with pytest.raises(FileNotFoundError):
        artifacts.get(orphan.id)


def test_application_artifact_actions_and_run_manifests(store, tmp_path) -> None:
    app = _app(tmp_path)

    created = app.execute("run.create", {"run_id": "exp-1", "metadata": {"model": "mlp"}})
    assert created["run_id"] == "exp-1"
    assert created["metrics"] == []

    appended = app.execute(
        "run.append",
        {
            "artifact_id": created["artifact_id"],
            "metrics": [{"name": "loss", "value": 0.25, "step": 10}],
        },
    )
    assert appended["run_id"] == "exp-1"
    assert appended["metrics"] == 1
    assert appended["appended"] == 1
    assert appended["parent_id"] == created["artifact_id"]

    runs = app.execute("run.list", {})
    assert {item["artifact_id"] for item in runs["items"]} == {
        created["artifact_id"],
        appended["artifact_id"],
    }
    manifests = [item for item in runs["items"] if item["artifact_id"] == appended["artifact_id"]]
    assert manifests[0]["metadata"]["metrics"] == 1

    checkpoint = app.execute("artifact.list", {"kind": "run-manifest", "limit": 10})
    assert checkpoint["items"]

    pinned = app.execute("artifact.pin", {"artifact_id": created["artifact_id"], "owner": "exp"})
    assert pinned["pinned"] is True
    inspected = app.execute("artifact.inspect", {"artifact_id": created["artifact_id"]})
    assert inspected["pins"] == ["exp"]
    assert inspected["metadata"]["run_id"] == "exp-1"
    assert isinstance(inspected["size_bytes"], int)

    unpinned = app.execute(
        "artifact.unpin", {"artifact_id": created["artifact_id"], "owner": "exp"}
    )
    assert unpinned["pinned"] is False

    collected = app.execute("artifact.gc", {"referenced": [appended["artifact_id"]]})
    assert collected["artifacts_removed"] == 0  # ancestor chain of the root is retained
    app.execute("artifact.pin", {"artifact_id": appended["artifact_id"], "owner": "exp"})
    orphan = app._artifact_store().put_bytes(b"orphan", kind="blob")
    collected = app.execute("artifact.gc", {})
    assert collected["artifacts_removed"] >= 1
    with pytest.raises(Exception):
        app.execute("artifact.inspect", {"artifact_id": orphan.id})

    with pytest.raises(ValueError, match="limit"):
        app.execute("artifact.list", {"limit": 999})
    with pytest.raises(ValueError, match="owner"):
        app.execute("artifact.pin", {"artifact_id": appended["artifact_id"], "owner": ""})
    with pytest.raises(ValueError, match="metrics"):
        app.execute("run.append", {"artifact_id": created["artifact_id"], "metrics": []})
    with pytest.raises((ValueError, OSError)):
        app.execute(
            "run.append",
            {
                "artifact_id": appended["artifact_id"][:-1] + "0",
                "metrics": [{"name": "x", "value": 1}],
            },
        )
    blob = app._artifact_store().put_bytes(b"not-a-run", kind="blob")
    with pytest.raises(ValueError, match="run manifest"):
        app.execute("run.append", {"artifact_id": blob.id, "metrics": [{"name": "x", "value": 1}]})


def test_preview_never_materializes_full_containers() -> None:
    class TrackingDict(dict):
        def __init__(self) -> None:
            super().__init__({f"key-{index}": index for index in range(100_000)})
            self.consumed = 0

        def items(self):  # type: ignore[override]
            for key, value in super().items():
                self.consumed += 1
                if self.consumed > 4:
                    return
                yield key, value

    tracking = TrackingDict()
    preview = preview_value(tracking, max_chars=120, max_items=4)
    assert tracking.consumed <= 4
    assert "len=100000" in preview["preview"]

    huge = "x" * 5_000_000
    preview = preview_value(huge, max_chars=50)
    assert len(preview["preview"]) <= 51

    class ExplodingRepr(str):
        def __repr__(self) -> str:
            if len(self) > 100:
                raise AssertionError("repr called on the untruncated string")
            return super().__repr__()

    preview = preview_value(ExplodingRepr("y" * 10_000), max_chars=40)
    assert len(preview["preview"]) <= 41
