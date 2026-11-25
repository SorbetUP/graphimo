from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Run:
    run_id: str
    node_id: str
    status: str
    code_version: str
    runtime: str
    started_at: float
    finished_at: float | None
    inputs: dict[str, str]
    outputs: dict[str, str]
    parent_run_id: str | None = None
    revision_id: str | None = None


@dataclass(frozen=True)
class Event:
    run_id: str
    node_id: str
    kind: str
    timestamp: float
    payload: dict[str, Any]
    step: int | None = None
    component_path: str | None = None
    durable: bool = True


class WorkspaceGraph:
    """Small durable graph/run/event ledger backed by SQLite."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY, kind TEXT NOT NULL, definition TEXT NOT NULL,
                    code_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS edges (
                    source_id TEXT NOT NULL, target_id TEXT NOT NULL, output_name TEXT NOT NULL,
                    input_name TEXT NOT NULL, PRIMARY KEY(source_id,target_id,output_name,input_name)
                );
                CREATE TABLE IF NOT EXISTS node_revisions (
                    revision_id TEXT PRIMARY KEY, node_id TEXT NOT NULL,
                    kind TEXT NOT NULL, definition TEXT NOT NULL,
                    code_version TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(node_id, revision_id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, status TEXT NOT NULL,
                    code_version TEXT NOT NULL, runtime TEXT NOT NULL, started_at REAL NOT NULL,
                    finished_at REAL, inputs TEXT NOT NULL, outputs TEXT NOT NULL, parent_run_id TEXT,
                    revision_id TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL, kind TEXT NOT NULL, timestamp REAL NOT NULL,
                    payload TEXT NOT NULL, step INTEGER, component_path TEXT
                );
                CREATE TRIGGER IF NOT EXISTS runs_require_revision
                BEFORE INSERT ON runs
                WHEN NOT EXISTS (
                    SELECT 1 FROM node_revisions
                    WHERE revision_id=NEW.revision_id AND node_id=NEW.node_id
                      AND code_version=NEW.code_version
                )
                BEGIN SELECT RAISE(ABORT, 'run requires a matching immutable node revision'); END;
                CREATE TRIGGER IF NOT EXISTS node_revisions_immutable_update
                BEFORE UPDATE ON node_revisions
                BEGIN SELECT RAISE(ABORT, 'node revisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS node_revisions_immutable_delete
                BEFORE DELETE ON node_revisions
                BEGIN SELECT RAISE(ABORT, 'node revisions are immutable'); END;
            """)
            run_columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            if "revision_id" not in run_columns:
                db.execute("ALTER TABLE runs ADD COLUMN revision_id TEXT")
            # Upgrade databases created before immutable revisions existed.
            legacy_nodes = db.execute(
                "SELECT node_id,kind,definition,code_version FROM nodes"
            ).fetchall()
            for node_id, kind, definition, code_version in legacy_nodes:
                revision_id = self._revision_id(node_id, kind, definition, code_version)
                db.execute(
                    "INSERT OR IGNORE INTO node_revisions VALUES (?,?,?,?,?,?)",
                    (revision_id, node_id, kind, definition, code_version, time.time()),
                )
                db.execute(
                    "UPDATE runs SET revision_id=? WHERE node_id=? AND code_version=? AND revision_id IS NULL",
                    (revision_id, node_id, code_version),
                )

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _revision_id(node_id: str, kind: str, definition: str, code_version: str) -> str:
        payload = json.dumps(
            {
                "node_id": node_id,
                "kind": kind,
                "definition": json.loads(definition),
                "code_version": code_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def add_node(
        self, node_id: str, kind: str, definition: dict[str, Any], code_version: str
    ) -> str:
        if not node_id or not kind or not code_version:
            raise ValueError("node id, kind and code version are required")
        encoded = json.dumps(definition, sort_keys=True, separators=(",", ":"))
        revision_id = self._revision_id(node_id, kind, encoded, code_version)
        with self._db() as db:
            db.execute(
                "INSERT INTO nodes VALUES (?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET "
                "kind=excluded.kind, definition=excluded.definition, code_version=excluded.code_version",
                (node_id, kind, encoded, code_version),
            )
            db.execute(
                "INSERT OR IGNORE INTO node_revisions VALUES (?,?,?,?,?,?)",
                (revision_id, node_id, kind, encoded, code_version, time.time()),
            )
        return revision_id

    def node_revisions(self, node_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM node_revisions WHERE node_id=? ORDER BY created_at,revision_id",
                (node_id,),
            ).fetchall()
        return [{**dict(row), "definition": json.loads(row["definition"])} for row in rows]

    def add_edge(self, source_id: str, target_id: str, output_name: str, input_name: str) -> None:
        with self._db() as db:
            node_ids = {row[0] for row in db.execute("SELECT node_id FROM nodes")}
            if source_id not in node_ids or target_id not in node_ids:
                raise ValueError("both edge endpoints must exist")
            if source_id == target_id:
                raise ValueError("workspace graph must remain acyclic")
            adjacency: dict[str, set[str]] = {}
            for edge_source, edge_target in db.execute("SELECT source_id,target_id FROM edges"):
                adjacency.setdefault(edge_source, set()).add(edge_target)
            pending = [target_id]
            visited: set[str] = set()
            while pending:
                current = pending.pop()
                if current == source_id:
                    raise ValueError("workspace graph must remain acyclic")
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(adjacency.get(current, ()))
            db.execute(
                "INSERT OR IGNORE INTO edges VALUES (?,?,?,?)",
                (source_id, target_id, output_name, input_name),
            )

    def graph_snapshot(self) -> dict[str, Any]:
        with self._db() as db:
            nodes = [dict(row) for row in db.execute("SELECT * FROM nodes ORDER BY node_id")]
            for node in nodes:
                node["definition"] = json.loads(node["definition"])
            edges = [
                dict(row) for row in db.execute("SELECT * FROM edges ORDER BY source_id,target_id")
            ]
        return {"nodes": nodes, "edges": edges}

    def artifact_references(self) -> set[str]:
        """Find content IDs retained by workspace node definitions and run history."""
        found: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, str):
                if len(value) == 64 and all(char in "0123456789abcdef" for char in value):
                    found.add(value)
            elif isinstance(value, dict):
                for item in value.values():
                    visit(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    visit(item)

        with self._db() as db:
            for row in db.execute("SELECT definition FROM nodes"):
                visit(json.loads(row[0]))
            for row in db.execute("SELECT inputs,outputs FROM runs"):
                visit(json.loads(row[0]))
                visit(json.loads(row[1]))
        return found

    def start_run(
        self,
        node_id: str,
        code_version: str,
        runtime: str,
        inputs: dict[str, str] | None = None,
        parent_run_id: str | None = None,
    ) -> Run:
        run_id = uuid.uuid4().hex
        started_at = time.time()
        with self._db() as db:
            revision = db.execute(
                "SELECT revision_id FROM node_revisions WHERE node_id=? AND code_version=? "
                "ORDER BY created_at DESC LIMIT 1",
                (node_id, code_version),
            ).fetchone()
            if revision is None:
                exists = db.execute("SELECT 1 FROM nodes WHERE node_id=?", (node_id,)).fetchone()
                if exists is None:
                    raise ValueError(f"run references unknown node: {node_id}")
                raise ValueError(f"node {node_id} has no revision with code version {code_version}")
            revision_id = revision[0]
            db.execute(
                "INSERT INTO runs(run_id,node_id,status,code_version,runtime,started_at,finished_at,inputs,outputs,parent_run_id,revision_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    node_id,
                    "running",
                    code_version,
                    runtime,
                    started_at,
                    None,
                    json.dumps(inputs or {}, sort_keys=True),
                    "{}",
                    parent_run_id,
                    revision_id,
                ),
            )
        return Run(
            run_id,
            node_id,
            "running",
            code_version,
            runtime,
            started_at,
            None,
            inputs or {},
            {},
            parent_run_id,
            revision_id,
        )

    def finish_run(
        self, run_id: str, outputs: dict[str, str] | None = None, *, status: str = "succeeded"
    ) -> Run:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("invalid terminal run status")
        now = time.time()
        with self._db() as db:
            db.execute(
                "UPDATE runs SET status=?,finished_at=?,outputs=? WHERE run_id=? AND status='running'",
                (status, now, json.dumps(outputs or {}, sort_keys=True), run_id),
            )
        result = self.get_run(run_id)
        if result is None:
            raise KeyError(run_id)
        if result.status == "running":
            raise ValueError(f"run {run_id} is not running")
        return result

    def get_run(self, run_id: str) -> Run | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return Run(
            row["run_id"],
            row["node_id"],
            row["status"],
            row["code_version"],
            row["runtime"],
            row["started_at"],
            row["finished_at"],
            json.loads(row["inputs"]),
            json.loads(row["outputs"]),
            row["parent_run_id"],
            row["revision_id"],
        )

    def emit(self, event: Event) -> None:
        if not event.durable:
            return
        with self._db() as db:
            run = db.execute("SELECT node_id FROM runs WHERE run_id=?", (event.run_id,)).fetchone()
            if run is None:
                raise ValueError("event references an unknown run")
            if run[0] != event.node_id:
                raise ValueError("event node id does not match its run")
            db.execute(
                "INSERT INTO events(run_id,node_id,kind,timestamp,payload,step,component_path) VALUES (?,?,?,?,?,?,?)",
                (
                    event.run_id,
                    event.node_id,
                    event.kind,
                    event.timestamp,
                    json.dumps(event.payload, sort_keys=True),
                    event.step,
                    event.component_path,
                ),
            )

    def events(self, run_id: str) -> list[Event]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY sequence", (run_id,)
            ).fetchall()
        return [
            Event(
                row["run_id"],
                row["node_id"],
                row["kind"],
                row["timestamp"],
                json.loads(row["payload"]),
                row["step"],
                row["component_path"],
            )
            for row in rows
        ]
