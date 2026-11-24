# Graphimo ArtifactStore

Graphimo artifacts are immutable content-addressed files or directory
manifests. Persistence is explicit: adding or running a Python node does not
automatically persist its values. This store is native Core functionality;
datasets, checkpoints, exports, traces and other artifacts use the same API.

## Store location

The first operation initializes the local store at `GRAPHIMO_ARTIFACTS_DIR`,
or at `~/.graphimo/artifacts` when the environment variable is unset. Importing
`graphimo.artifacts` alone creates no files. A host can select a location
before use:

```python
from graphimo import artifacts

artifacts.configure_default_store("/project/.graphimo/artifacts")
```

The module-level functions share one process-wide store. Code that needs an
isolated store can instantiate `LocalArtifactStore(path)` directly.

## Persist and materialize

```python
from graphimo import artifacts

dataset = artifacts.put("data/images", kind="dataset")
checkpoint = artifacts.put("model.pt", kind="checkpoint", parent=dataset)
small_value = artifacts.put_bytes(b"metadata", kind="metadata")

artifacts.materialize(dataset, "work/images")
```

`put` accepts a regular file or a directory. Directory manifests map safe
relative paths to SHA-256 blobs. Identical files across artifacts share one
stored blob. Empty directories are recorded. Symlinks are rejected. A parent
reference records lineage and keeps ancestors reachable during garbage
collection.

`ArtifactRef` is an immutable pair of `id` and `kind`. The ID is the SHA-256
of the canonical manifest. File contents are hashed and verified in 1 MiB
chunks. Materialization uses APFS `clonefile` or Linux `FICLONE` when
available, otherwise it streams a copy; it does not use hard links.

## Retention, collection and usage

```python
artifacts.pin(checkpoint, owner="saved-model")
artifacts.unpin(checkpoint, owner="saved-model")

report = artifacts.gc(referenced=[dataset])
usage = artifacts.stats()
```

Pins and explicitly referenced artifacts are GC roots. Their recorded parent
lineage is retained. `gc` removes other manifests and blobs and returns counts
and reclaimed bytes. `stats()` reports:

- `logical_bytes`: all file references across stored manifests, counting
  shared files once per artifact reference;
- `physical_stored_bytes`: bytes in unique stored blob files;
- `deduplicated_bytes`: logical bytes minus physical bytes, bounded at zero;
- `artifacts` and `blobs`: manifest and blob counts.

V1 deduplicates complete files, not chunks. Chunking, DVC, Borg, Kopia and
FastCDC are not dependencies of this contract.
