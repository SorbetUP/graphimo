from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from graphimo.application import GraphimoApplication
from graphimo_pytorch import (
    EventQueue,
    PyTorchEvent,
    default_subscription_rate_hz,
    graph_revision,
    mlp_template,
    save_state,
)


ROOT = Path(__file__).resolve().parents[1]
ADDON_ROOT = ROOT / "addons" / "pytorch"


def _run_addon_probe(addon_target: Path | None, state_path: Path, revision: str) -> str:
    probe = """
import builtins
import sys
from pathlib import Path

root, core, target, state_path, expected_revision = sys.argv[1:]
sys.path[:0] = [root, core]
if target != "-":
    sys.path.insert(0, target)
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split(".", 1)[0] in {"torch", "torchlens"}:
        raise AssertionError(f"Core/addon discovery imported optional runtime dependency {name}")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from graphimo.metablocks import discover_metablock_addons
entries = discover_metablock_addons()
entry = next((item for item in entries if item.name == "pytorch"), None)
if target == "-":
    assert entry is None
    import graphimo.application
    print("addon-absent-ok")
else:
    assert entry is not None
    registration = entry.load()
    assert registration.dialect_id == "pytorch.model"
    assert registration.addon_version == "0.1.0"
    assert "torch" not in sys.modules and "torchlens" not in sys.modules
    from graphimo_pytorch import graph_revision, load_state
    restored = load_state(Path(state_path))
    assert restored.metablock_id == "p9-lifecycle"
    assert graph_revision(restored) == expected_revision
    print("addon-reinstalled-ok")
"""
    target_arg = str(addon_target) if addon_target is not None else "-"
    result = subprocess.run(
        [
            sys.executable, "-S", "-c", probe, str(ROOT), str(ROOT / "core"), target_arg,
            str(state_path), revision,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_p9_sidecar_and_layout_survive_addon_absence_and_wheel_reinstall(
    tmp_path: Path, project, host, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = mlp_template(input_size=4, hidden=8, output_size=2, metablock_id="p9-lifecycle")
    state_path = project.root / ".graphimo" / "metablocks" / "p9-lifecycle.pytorch.json"
    save_state(state_path, state)
    original_state = state_path.read_bytes()
    layout_data = {
        "version": 1,
        "notebooks": {
            str(project.notebook): {
                "cells": {"metablock:p9-lifecycle": {"x": 321, "y": 654, "width": 400, "height": 210}}
            }
        },
    }
    project.layout.write_text(json.dumps(layout_data, sort_keys=True) + "\n", encoding="utf-8")
    original_layout = project.layout.read_bytes()

    monkeypatch.setattr("graphimo.application.discover_metablock_addons", lambda: ())
    host.marimo.snapshot["notebook"]["path"] = str(project.notebook)
    host.marimo.sessions[0]["path"] = str(project.notebook)
    app = GraphimoApplication(host.marimo, project.components, layout_path=project.layout)
    view = app.open_project(project.notebook)
    placeholder = next(block for block in view.blocks if block.id == "metablock:p9-lifecycle")
    assert placeholder.state == "unavailable"
    assert placeholder.body["plugin_id"] == "pytorch"
    assert placeholder.body["addon_installed"] is False
    assert state_path.read_bytes() == original_state
    assert project.layout.read_bytes() == original_layout
    assert _run_addon_probe(None, state_path, graph_revision(state)) == "addon-absent-ok"

    source_copy = tmp_path / "addon-source"
    shutil.copytree(
        ADDON_ROOT,
        source_copy,
        ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__", "*.pyc"),
    )
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    build = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps", "--wheel-dir", str(wheelhouse), str(source_copy)],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheelhouse.glob("graphimo_pytorch-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        assert "graphimo_pytorch/templates.py" in members
        assert "graphimo_pytorch/inspection.py" in members

    install_target = tmp_path / "reinstalled-addon"
    install = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(install_target), str(wheel)],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    assert install.returncode == 0, install.stderr
    assert _run_addon_probe(install_target, state_path, graph_revision(state)) == "addon-reinstalled-ok"
    assert state_path.read_bytes() == original_state
    assert project.layout.read_bytes() == original_layout


def test_p9_event_queue_stays_bounded_and_drops_without_waiting() -> None:
    events = EventQueue(maxsize=2)
    event = PyTorchEvent(1, "event-1", "probe", 1.0, "model", "revision", "runtime", None, None, (), None, {})

    assert events.put(event)
    assert events.put(event)
    assert events.put(event) is False
    assert events.dropped_count == 1
    assert len(events.drain(20)) == 2
    assert events.dropped_count == 1


def test_p9_inspection_defaults_respect_contract_event_rates() -> None:
    assert default_subscription_rate_hz() == 10.0
    assert default_subscription_rate_hz("summary") <= 10.0
    assert default_subscription_rate_hz("histogram") <= 2.0
    assert default_subscription_rate_hz("sample") <= 2.0


@pytest.mark.parametrize("maxsize", [0, -1, 1.5, True])
def test_p9_event_queue_rejects_unbounded_or_malformed_capacity(maxsize: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        EventQueue(maxsize=maxsize)  # type: ignore[arg-type]
