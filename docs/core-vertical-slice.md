# Graphimo core vertical slice: validation report

## Scope

The attached architecture plan describes a new scientific workflow core. The
current product remains a Zeron plugin over a live marimo session: marimo owns
its notebook graph and execution. Replacing that path in one change would make
the existing plugin unusable before the proposed runtime is proven.

This spike is therefore an isolated Python package at
`core/graphimo_core/`. It is a prototype and does not change the Zeron plugin's
execution semantics. It covers the plan's highest-risk data, persistence,
training, checkpoint, restart, and lineage claims with an executable path.

## What the prototype does

- Stores file and directory artifacts by SHA-256 and verifies manifest/blob
  integrity. Directory manifests share blobs for unchanged files.
- Pins artifacts and runs garbage collection from explicit workspace/run
  references. Garbage collection follows parent lineage so a retained derived
  dataset or checkpoint keeps its ancestors. `WorkspaceGraph.artifact_references`
  supplies IDs referenced by node definitions and run inputs/outputs.
- Materializes independent branches with APFS `clonefile`, Linux `FICLONE`, or
  a reported byte-copy fallback. Hardlinks are avoided because writes through a
  materialized path could mutate the stored artifact.
- Persists nodes, edges, run status, inputs, outputs, timestamps, and events in
  SQLite. The graph/run ledger can be reopened without a live Python object.
- Builds a small PyTorch `ModelSpec` with one shared backbone and three task
  heads. Training emits progress, per-head/total loss, gradient norm, a
  selected activation probe, elapsed time, and process peak-RSS metadata.
- Compiles `ModelSpec` to a real `nn.Module` and `torch.fx.GraphModule`, then
  runs FX shape propagation to report each head's output shape.
- Runs training and evaluation in a long-lived local worker. A node receives
  JSON inputs, including artifact IDs; it can read immutable artifact files,
  store byte outputs, and declare JSON outputs. The `graphimo` helpers emit
  progress, metric, log, resource, warning, and model-probe events.
- Streams node stdout/stderr as bounded log events, with an 8 KB character
  budget per stream. A request may specify a timeout; callers can cancel the
  worker from another thread. Both stop the whole worker process and the next
  request starts a fresh one.
- Stores model and optimizer state in a checkpoint artifact. The demo kills
  the worker after evaluation and restores the checkpoint in a fresh process
  to verify matching predictions from durable artifacts.
- Forks the model with a changed hidden width. It copies only exact-name,
  exact-shape weights and records loaded, new, incompatible, and ignored keys.
  Optimizer state is intentionally reset for the fork and recorded as such.
- Stores Python, NumPy, PyTorch CPU, and (when available) CUDA random states in
  each checkpoint and exposes an explicit RNG restore function. This prototype
  does not use a scheduler or AMP scaler, so those states are not present.
- Throttles events per series (20 events/second by default, at most 500 per
  node), coalesces rapid updates, and flushes the latest value for each series
  when a node finishes. Each execution reports emitted and dropped counts.
- The historical optional TorchLens adapter has been removed from this prototype.
  Deep inspection belongs to the separately installed `graphimo-pytorch` addon;
  the prototype no longer imports or declares TorchLens.

## Change and debugging log

- Audited the current repository and confirmed it is a marimo-backed Zeron
  frontend, not the core architecture from the attached plan. Kept the existing
  notebook/plugin path intact and added the proof under `core/graphimo_core/`.
- Added content-addressed artifacts, durable parent-artifact metadata, SQLite
  graph/run/event records, cycle checks, a local line-oriented worker, a
  three-head model/trainer, checkpoint/RNG serialization, and a CLI demo.
- The first demo attempt failed because the caller treated the materialization
  report as a filesystem path. Corrected the caller to use its explicit
  destination, then reran the complete demo.
- The first Ruff pass found two unused imports; removed them. Subsequent Ruff
  runs were clean.
- The `rtk pytest` wrapper invocation returned “No tests collected”. Used
  `rtk proxy ... pytest` for the actual pytest executable; this collected and
  ran the suite. The full test run then passed.
- A code review of the first passing demo found that parent lineage was only in
  memory and graph cycles were accepted. Added separate durable lineage
  metadata, manifest/path integrity checks, DAG cycle rejection, and tests for
  both. Checkpoints were also extended to save/restore RNG state.
- The first generic-worker test found protocol frames being captured as node
  stdout while `exec` redirected output. Split protocol output from node
  stdout/stderr, then added latest-value flushing so fast runs retain their
  final metrics.
- Added artifact pin/unpin and mark-and-sweep collection from workspace/run
  references, retaining parent lineage. Added timeout and external-cancel
  tests; a killed worker restarts successfully. Since cancellation kills the
  process, side effects made before the kill are not rolled back.
- An earlier experiment installed TorchLens 3.0.3 into a temporary target and
  called it from the prototype. This crossed the Core/addon boundary and used
  an API that changed in later TorchLens releases. The Core adapter, dependency
  extra, and test have now been removed; P7 implements the pinned 2.34.1
  integration in `addons/pytorch/graphimo_pytorch/inspection.py` instead.
- Existing uncommitted edits to the Zeron plugin skill and API guide were left
  in place. No notebook source or Zeron plugin source was changed by this
  vertical-slice work.

Run the demo with:

```sh
PYTHONPATH=core python -m graphimo_core.vertical_demo /tmp/graphimo-workspace
```

Install the optional prototype dependencies with `python -m pip install -e
'.[prototype]'`. Its automated checks are in `tests/test_core_vertical_slice.py`.

## Validation results

Verified on 2026-11-23 with Python 3.13.2, PyTorch 2.7.1, and marimo 0.25.2.
The machine has no CUDA device. The TorchLens 3.0.3 run below was a historical
prototype experiment. Its direct Core adapter has since been removed; current
deep-inspection validation is in the separately installable PyTorch addon and
uses the pinned TorchLens 2.34.1 API. The APFS `clonefile` path succeeded for
both files in each derived dataset branch.

- Historical full-suite snapshot with the optional Core TorchLens adapter:
  **58 passed, 1 skipped**. This result predates the Core/addon boundary
  correction below and is not evidence for current TorchLens integration.
- The bootstrap path was then run from a temporary clean virtual environment:
  **1 passed**. It installed pinned marimo 0.25.2 into the test's temporary
  home, started a managed session, and cleaned up the process.
- `ruff check adapter core examples components tests`: **no issues**.
- Historical Core-adapter check: **1 passed** with TorchLens 3.0.3; removed
  because it called TorchLens from Core and relied on an API no longer present
  in the required 2.34.1 release.
- Current conformance and addon suite with the exact TorchLens 2.34.1 wheel
  available: **166 passed, 4 skipped**; `ruff check .`: **no issues**. This
  includes the new addon P7 capture, gradient, intervention, artifact-export,
  and Core-boundary tests.
- Standalone vertical demo: **PASS**. It trained three heads and reduced total
  loss, retained 30 events after telemetry coalescing, saved optimizer and RNG
  state, restored the checkpoint in a fresh worker process with matching
  predictions, kept the same worker alive after rejecting an invalid request,
  materialized both branches with reflinks, and preserved the graph after
  close/reopen.
- Wheel build, install into a temporary target, and import of the installed
  `graphimo_core` package: **PASS** as part of the distribution test.

The demo prints the actual materialization strategy and process IDs so
environment-specific behavior remains visible.

| Plan criterion | Result | Evidence / limitation |
| --- | --- | --- |
| Graph and run metadata survive close/reopen | PASS | SQLite reopen comparison in the demo and persistence test. |
| Data A can branch to B and C without mutating A | PASS | The demo materializes independent branches; automated test mutates a branch and checks A remains unchanged. |
| Reuse unchanged dataset file content | PASS, with limit | CAS manifests reuse identical blobs. Materialized files use reflink when supported; otherwise the report says `copy`. No disk-space benchmark is claimed. |
| Artifact pins, lineage, and garbage collection | PASS for prototype | Pin roots and active workspace/run artifact IDs retain their parent chain; the test verifies orphan manifests/blobs are collected. |
| Ordinary Python model code / lightweight `@node` | PARTIAL | PyTorch model is an ordinary Python `nn.Module`; Graphimo's existing marimo cell path remains unchanged. This spike does not define or validate the final `@node` API. |
| ModelSpec to `nn.Module` / FX and head-shape inference | PASS for prototype | The three-head `ModelSpec` compiles to a real `GraphModule`; `ShapeProp` reports all head outputs and the test checks the inferred shapes. No visual model editor or arbitrary-model round-trip is claimed. |
| Persistent worker and durable checkpoint restore | PASS for demo path | Training, evaluation, and fork training execute in the worker. The worker is killed after evaluation and a new process restores the checkpoint and produces matching predictions. Timeout and cancel restart tests also pass. |
| Multi-head training and distinct losses | PASS | Three regression heads, per-head loss events, and global loss are emitted. |
| Live progress, metrics, and resource/activation events | PARTIAL | Events are persisted; selected activation uses a normal PyTorch forward hook. Throttling is configurable and reports suppression, but overhead is not benchmarked and CUDA was unavailable here. |
| Targeted TorchLens inspection | Moved to PyTorch addon | The old Core adapter was removed. P7 tests in the separate addon exercise explicit-input activation/gradient capture, interventions, and ArtifactStore export against pinned TorchLens 2.34.1. |
| Checkpoint after process destruction and re-evaluation | PASS for demo path | Optimizer and model state are stored; evaluation runs in a separate process after the training model is discarded. |
| Fork and explicit weight migration report | PASS for demo path | Width change yields both compatible and incompatible keys. Optimizer state is reset and disclosed. |
| End-to-end experience inside one Graphimo canvas | NOT IMPLEMENTED | This spike is a CLI/library proof. It is not connected to the Zeron UI, notebook editor, or marimo session. |
| Reproducible full section 9.1 scenario | PARTIAL | Synthetic data, branching, training, checkpoint, restart, pseudo-label output, fork, and provenance are exercised. No visual canvas, real dataset, TorchLens, GPU telemetry, or UI comparison is included. |

## Known gaps and architectural decision

The vertical path demonstrates that local CAS + SQLite + a Python worker can
support the durable training/checkpoint lifecycle without Ray, DVC, MLflow, or
Kishu. It does **not** yet justify replacing marimo as the existing notebook
execution backend: the current plugin is intentionally a marimo frontend, while
the proposed Core is a separate product mode. The least disruptive next step is
to stabilize these interfaces and add a disposable-canvas adapter only after
the core gates pass.

The plan's GO gate is **not complete**. Before calling it complete, the next
required work is:

1. Add safe-point cancellation/checkpointing and idempotency for nodes with
   side effects. Current timeout/cancel kills the process and cannot roll back
   work already performed.
2. Add `RuntimeRef` selection for Conda/uv environments and robust codecs for
   non-JSON values. The spike currently runs in the host's Python environment.
3. Benchmark telemetry overhead and reflink/copy storage, and test CUDA metrics
   on a CUDA host. Event suppression was tested here, but only CPU was available.
4. Wire the P7 TorchLens API to the on-demand Workbench inspector after Core
   §31's addon handler API is fixed; the full trace is now explicitly exportable.
5. Add a minimal Graphimo canvas adapter and exercise the complete slice in
   Zeron. The current deliverable remains a CLI/library proof.

Python nodes execute trusted code with the user's process permissions. The
worker is persistent but is not a security sandbox.

No external tracking or orchestration framework was added. The current report
is a prototype validation, not a claim that the full product architecture has
passed GO.
