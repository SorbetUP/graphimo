# Graphimo ArtifactStore API

Artifacts are explicitly persisted files or directories. Graphimo does not persist arbitrary node results automatically. The public API is `graphimo.artifacts`; it returns immutable `ArtifactRef(id, kind)` values and uses the local SHA-256 content-addressed store.

```python
from graphimo import artifacts

dataset = artifacts.put("data/train", kind="dataset")
checkpoint = artifacts.put("runs/checkpoint.pt", kind="checkpoint", parent_id=dataset)
materialized = artifacts.materialize(dataset, "/tmp/train-copy")
artifacts.pin(checkpoint, owner="saved-model")
print(artifacts.stats())
```

`put` streams files and builds directory manifests with file-level blob deduplication. Materialization uses copy-on-write reflinks where available, then copies files. `stats()` reports logical bytes referenced by manifests, physical bytes in unique blobs and the difference as deduplicated bytes. `gc(referenced=[...])` retains the supplied roots, pinned artifacts and their recorded parents; `unpin` removes a named retention pin.

The default store root is `$XDG_DATA_HOME/graphimo/artifacts`, or `~/.local/share/graphimo/artifacts` when `XDG_DATA_HOME` is unset. Set `GRAPHIMO_ARTIFACT_STORE` before the process starts to select another local store. The store path is an implementation detail and need not be displayed in the canvas.
