from __future__ import annotations

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


@dataclass(frozen=True)
class Event:
    run_id: str
    node_id: str
    kind: str
    timestamp: float
    payload: dict[str, Any]
    step: int | None = None
    component_path: str | None = None


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
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, status TEXT NOT NULL,
                    code_version TEXT NOT NULL, runtime TEXT NOT NULL, started_at REAL NOT NULL,
                    finished_at REAL, inputs TEXT NOT NULL, outputs TEXT NOT NULL, parent_run_id TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL, kind TEXT NOT NULL, timestamp REAL NOT NULL,
                    payload TEXT NOT NULL, step INTEGER, component_path TEXT
                );
            """)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def add_node(self, node_id: str, kind: str, definition: dict[str, Any], code_version: str) -> None:
        if not node_id or not kind or not code_version:
            raise ValueError("node id, kind and code version are required")
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO nodes VALUES (?,?,?,?)",
                       (node_id, kind, json.dumps(definition, sort_keys=True), code_version))

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
            db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", (source_id,target_id,output_name,input_name))

    def graph_snapshot(self) -> dict[str, Any]:
        with self._db() as db:
            nodes = [dict(row) for row in db.execute("SELECT * FROM nodes ORDER BY node_id")]
            for node in nodes:
                node["definition"] = json.loads(node["definition"])
            edges = [dict(row) for row in db.execute("SELECT * FROM edges ORDER BY source_id,target_id")]
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

    def start_run(self, node_id: str, code_version: str, runtime: str,
                  inputs: dict[str, str] | None = None, parent_run_id: str | None = None) -> Run:
        run = Run(uuid.uuid4().hex, node_id, "running", code_version, runtime, time.time(), None,
                  inputs or {}, {}, parent_run_id)
        with self._db() as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)", (
                run.run_id, run.node_id, run.status, run.code_version, run.runtime, run.started_at,
                run.finished_at, json.dumps(run.inputs, sort_keys=True), json.dumps(run.outputs), run.parent_run_id))
        return run

    def finish_run(self, run_id: str, outputs: dict[str, str] | None = None, *, status: str = "succeeded") -> Run:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("invalid terminal run status")
        now = time.time()
        with self._db() as db:
            db.execute("UPDATE runs SET status=?,finished_at=?,outputs=? WHERE run_id=? AND status='running'",
                       (status, now, json.dumps(outputs or {}, sort_keys=True), run_id))
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
        return Run(row["run_id"], row["node_id"], row["status"], row["code_version"], row["runtime"],
                   row["started_at"], row["finished_at"], json.loads(row["inputs"]),
                   json.loads(row["outputs"]), row["parent_run_id"])

    def emit(self, event: Event) -> None:
        with self._db() as db:
            if db.execute("SELECT 1 FROM runs WHERE run_id=?", (event.run_id,)).fetchone() is None:
                raise ValueError("event references an unknown run")
            db.execute("INSERT INTO events(run_id,node_id,kind,timestamp,payload,step,component_path) VALUES (?,?,?,?,?,?,?)",
                       (event.run_id,event.node_id,event.kind,event.timestamp,
                        json.dumps(event.payload, sort_keys=True),event.step,event.component_path))

    def events(self, run_id: str) -> list[Event]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM events WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
        return [Event(row["run_id"],row["node_id"],row["kind"],row["timestamp"],
                      json.loads(row["payload"]),row["step"],row["component_path"]) for row in rows]
