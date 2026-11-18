"""Check that the Python distribution contains Graphimo's adapter package only."""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ENTRIES = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "adapter",
    "plugin",
    "components",
    "examples",
    "tests",
)


def _copy_project(destination: Path) -> None:
    ignored = shutil.ignore_patterns("__pycache__", ".pytest_cache", ".ruff_cache", "*.pyc")
    for entry in PROJECT_ENTRIES:
        source = ROOT / entry
        target = destination / entry
        if source.is_dir():
            shutil.copytree(source, target, ignore=ignored)
        else:
            shutil.copy2(source, target)


def test_python_wheel_contains_only_the_adapter_package(tmp_path: Path) -> None:
    project = tmp_path / "graphimo"
    project.mkdir()
    _copy_project(project)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(wheelhouse),
            str(project),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, f"wheel build failed:\n{result.stdout}\n{result.stderr}"

    wheels = list(wheelhouse.glob("graphimo-*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())

    assert "adapter/__init__.py" in names
    assert "adapter/graphimo_adapter.py" in names
    for excluded in ("plugin/", "components/", "examples/", "tests/"):
        assert not any(name.startswith(excluded) for name in names), excluded
