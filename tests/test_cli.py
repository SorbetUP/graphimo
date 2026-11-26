"""Tests for the graphimo command-line interface (graphimo.cli)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from graphimo.service.server import GraphimoService

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def service(tmp_path: Path):
    notebook = tmp_path / "nb.py"
    notebook.write_text("import marimo\napp = marimo.App()\n", encoding="utf-8")
    service = GraphimoService(manifest_path=tmp_path / "service.json").start()
    try:
        yield service, tmp_path / "service.json", notebook
    finally:
        service.close()


def _cli(manifest: Path, *argv: str) -> subprocess.CompletedProcess:
    env_path = str(ROOT)
    import os

    env = os.environ.copy()
    env["PYTHONPATH"] = env_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run(
        [sys.executable, "-m", "graphimo", "--manifest", str(manifest), "--compact", *argv],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=60,
    )


def test_cli_health(service) -> None:
    _, manifest, _ = service
    proc = _cli(manifest, "health")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["result"]["protocol"] == 1


def test_cli_scope_and_addons(service) -> None:
    _, manifest, notebook = service
    proc = _cli(manifest, "--path", str(notebook), "scope")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["result"]["scope"]["scope_id"] == "root"

    proc = _cli(manifest, "--path", str(notebook), "addons")
    assert proc.returncode == 0, proc.stderr
    assert "addons" in json.loads(proc.stdout)["result"]


def test_cli_do_generic_action_and_error_path(service) -> None:
    _, manifest, notebook = service
    proc = _cli(manifest, "--path", str(notebook), "do", "control.catalog", "{}")
    assert proc.returncode == 0, proc.stderr
    assert "items" in json.loads(proc.stdout)["result"]

    proc = _cli(manifest, "--path", str(notebook), "do", "no.such.action", "{}")
    assert proc.returncode == 1
    error = json.loads(proc.stderr)
    assert error["ok"] is False
    assert error["error"]["code"]


def test_cli_requires_path(service) -> None:
    _, manifest, _ = service
    import os

    env = os.environ.copy()
    env.pop("GRAPHIMO_PROJECT", None)
    proc = subprocess.run(
        [sys.executable, "-m", "graphimo", "--manifest", str(manifest), "scope"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**env, "PYTHONPATH": str(ROOT)},
        timeout=60,
    )
    assert proc.returncode != 0
    assert "GRAPHIMO_PROJECT" in proc.stderr or "--path" in proc.stderr


def test_cli_batch_from_file(service, tmp_path: Path) -> None:
    _, manifest, notebook = service
    ops = tmp_path / "ops.json"
    ops.write_text(
        json.dumps(
            [
                {"id": "a", "action": "scope.current", "args": {}},
                {"id": "b", "action": "control.catalog", "args": {}},
            ]
        ),
        encoding="utf-8",
    )
    proc = _cli(manifest, "--path", str(notebook), "batch", str(ops))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    result = payload["result"]
    ids = [item.get("id") for item in result.get("results") or result.get("operations") or []]
    assert ids == ["a", "b"], result
