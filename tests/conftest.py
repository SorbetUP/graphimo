from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


class FakeMarimoPort:
    """A small notebook port fake; the snapshot remains the source of truth."""

    def __init__(self, snapshot: dict | None = None, sessions: list | None = None):
        self.snapshot = deepcopy(snapshot or {
            "notebook": {"path": "fixture.py", "session_id": "fixture"},
            "cells": [
                {"id": "load", "name": None, "source": "data = load_data()",
                 "defs": ["data"], "refs": [], "parents": [], "children": [],
                 "status": "success", "output": None},
                {"id": "normalize", "name": None, "source": "processed = normalize(data)",
                 "defs": ["processed"], "refs": ["data"],
                 "parents": [{"cell_id": "load", "variables": ["data"]}],
                 "children": [], "status": "idle", "output": None},
                {"id": "arbitrary", "name": None,
                 "source": "x = complicated_custom_code(processed)",
                 "defs": ["x"], "refs": ["complicated_custom_code", "processed"],
                 "parents": [{"cell_id": "normalize", "variables": ["processed"]}],
                 "children": [], "status": "idle", "output": None},
                {"id": "empty", "name": None, "source": "", "defs": [], "refs": [],
                 "parents": [], "children": [], "status": "unknown", "output": None},
            ],
            "edges": [
                {"source_cell_id": "load", "target_cell_id": "normalize", "variable": "data"},
                {"source_cell_id": "normalize", "target_cell_id": "arbitrary", "variable": "processed"},
            ],
        })
        self.sessions = sessions if sessions is not None else [
            {"session_id": "fixture", "path": "fixture.py"}
        ]
        self.added: list[tuple[str, str]] = []
        self.replaced: list[tuple[str, str, str]] = []
        self.runs: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []

    def list_active_notebooks(self):
        return deepcopy(self.sessions)

    def inspect(self, session_id: str):
        if session_id != self.snapshot["notebook"]["session_id"]:
            raise KeyError(session_id)
        return deepcopy(self.snapshot)

    def add_cell(self, session_id: str, source: str):
        self.added.append((session_id, source))
        return "inserted-cell"

    def replace_cell_source(self, session_id: str, cell_id: str, source: str):
        self.replaced.append((session_id, cell_id, source))
        return {"ok": True}

    def run_cell(self, session_id: str, cell_id: str):
        self.runs.append((session_id, cell_id))
        return {"status": "queued", "request_id": "run-1"}

    def delete_cell(self, session_id: str, cell_id: str):
        self.deleted.append((session_id, cell_id))
        return {"ok": True}


@pytest.fixture
def project(tmp_path: Path):
    components = tmp_path / "components"
    category = components / "data-processing"
    category.mkdir(parents=True)
    (category / "normalize.py").write_text(
        '"""Scale an image."""\n'
        "def normalize(image, mean=0.5, std=0.5):\n"
        "    return (image - mean) / std\n",
        encoding="utf-8",
    )
    package = components / "models" / "bodypart"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "__all__ = ['encode', 'decode']\n"
        "def encode(value):\n    return value\n"
        "def decode(value):\n    return value\n"
        "def _private(value):\n    return value\n",
        encoding="utf-8",
    )
    notebook = tmp_path / "fixture.py"
    notebook.write_text("# marimo fixture\n", encoding="utf-8")
    return SimpleNamespace(
        root=tmp_path,
        components=components,
        notebook=notebook,
        layout=tmp_path / ".graphimo.json",
    )


@pytest.fixture
def host(project):
    marimo = FakeMarimoPort()
    opened: list[str] = []
    context = SimpleNamespace(
        marimo=marimo,
        components_root=project.components,
        layout_path=project.layout,
        open_code=opened.append,
    )
    return SimpleNamespace(context=context, marimo=marimo, opened=opened)
