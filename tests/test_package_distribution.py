"""Check that the Graphimo Core wheel excludes renderers and addons."""

from __future__ import annotations

import os
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
    "graphimo",
    "core",
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


def test_python_wheel_contains_product_packages_but_not_core_prototype(tmp_path: Path) -> None:
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
    assert "graphimo/__init__.py" in names
    assert "graphimo/groups.py" in names
    assert "graphimo/artifacts/__init__.py" in names
    assert "graphimo/artifacts/local.py" in names
    assert "graphimo/client.py" in names
    assert "graphimo/service/__init__.py" in names
    assert "graphimo/service/server.py" in names
    assert "graphimo/service/invoke.py" in names
    assert "graphimo/service/protocol.py" in names
    assert "graphimo/service/sessions.py" in names
    assert "graphimo/metablocks.py" in names
    assert "graphimo/workbench.py" in names
    assert "graphimo/workbench/index.html" in names
    assert "graphimo/workbench/workbench.js" in names
    assert "graphimo/workbench/workbench.css" in names
    for excluded in (
        "plugin/",
        "components/",
        "examples/",
        "tests/",
        "core/",
        "graphimo_core/",
        "graphimo_pytorch/",
    ):
        assert not any(name.startswith(excluded) for name in names), excluded

    install_target = tmp_path / "install"
    install_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(install_target),
            str(wheels[0]),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        timeout=90,
    )
    assert install_result.returncode == 0, (
        f"wheel install failed:\n{install_result.stdout}\n{install_result.stderr}"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(install_target)
    import_result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from importlib.resources import files; from graphimo import GraphimoApplication, MetaBlockAddonManager, artifacts, run_group; from graphimo.client import GraphimoClient; assets=files('graphimo').joinpath('workbench'); assert all(assets.joinpath(name).is_file() for name in ('index.html','workbench.js','workbench.css')); assert callable(artifacts.put) and callable(run_group) and GraphimoClient and not any(name in sys.modules for name in ('torch','torchlens','marimo','graphimo_core')); print('import-ok')",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert import_result.returncode == 0, import_result.stderr
    assert import_result.stdout.strip() == "import-ok"
