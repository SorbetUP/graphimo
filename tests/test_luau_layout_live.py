"""Run the production Luau layout scripts against real notebook-side JSON I/O."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "luau_layout_driver.luau"
LUAU = shutil.which("luau")


def _luau(mode: str, notebook: Path, decoded_json: str = "") -> subprocess.CompletedProcess[str]:
    assert LUAU is not None
    return subprocess.run(
        [LUAU, str(DRIVER), "-a", mode, str(notebook), decoded_json],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )


def _script(output: str) -> str:
    scripts = re.findall(r"GRAPHIMO_SCRIPT_BEGIN\n(.*?)\nGRAPHIMO_SCRIPT_END", output, re.S)
    assert len(scripts) == 1, output
    return scripts[0]


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to exercise the production layout module")
def test_production_layout_save_and_load_round_trip_uses_json_string_notebook_key(tmp_path: Path):
    notebook = tmp_path / "notebook.py"
    notebook.write_text("# ordinary marimo notebook\n", encoding="utf-8")

    saved = _luau("save", notebook)
    assert saved.returncode == 0, saved.stderr
    assert "GRAPHIMO_SAVE_DONE" in saved.stdout

    save_script = _script(saved.stdout)
    notebook_arg = next(line.partition(":")[2] for line in saved.stdout.splitlines() if line.startswith("GRAPHIMO_NOTEBOOK:"))
    cells_arg = next(line.partition(":")[2] for line in saved.stdout.splitlines() if line.startswith("GRAPHIMO_CELLS:"))
    executed_save = subprocess.run(
        [sys.executable, "-c", save_script, notebook_arg, cells_arg],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert executed_save.returncode == 0, executed_save.stderr

    sidecar_path = tmp_path / ".graphimo.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    notebook_key = str(notebook.resolve())
    assert sidecar == {
        "version": 1,
        "notebooks": {
            notebook_key: {"cells": {"stable-cell-id": {"x": 120, "y": 180, "collapsed": False}}}
        },
    }

    loaded = _luau("load", notebook, json.dumps(sidecar["notebooks"][notebook_key]["cells"]))
    assert loaded.returncode == 0, loaded.stderr
    assert "GRAPHIMO_LOAD_DONE" in loaded.stdout
    load_script = _script(loaded.stdout)
    executed_load = subprocess.run(
        [sys.executable, "-c", load_script, str(notebook)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert executed_load.returncode == 0, executed_load.stderr
    assert json.loads(executed_load.stdout) == sidecar["notebooks"][notebook_key]["cells"]
