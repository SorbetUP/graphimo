#!/usr/bin/env python3
"""Reproducible local Graphimo verification gate."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "addons" / "pytorch"
os.environ.setdefault("GRAPHIMO_MARIMO_PYTHON", sys.executable)
FAST_TESTS = (
    "tests/test_node_decorator.py",
    "tests/test_node_outputs.py",
    "tests/test_value_preview.py",
    "tests/test_node_component_discovery.py",
    "tests/test_node_instance_adapter.py",
    "tests/test_instance_source.py",
    "tests/test_application.py",
    "tests/test_disabled_instance.py",
    "tests/test_artifact_store.py",
    "tests/test_control_inventory_live.py",
    "tests/test_metablocks_opaque.py",
    "tests/test_scopes_addons.py",
    "tests/test_core_addon_boundary.py",
    "tests/test_pytorch_addon_p1.py",
    "tests/test_pytorch_addon_p2.py",
    "tests/test_pytorch_addon_p3.py",
    "tests/test_pytorch_addon_p4_projection.py",
    "tests/test_pytorch_addon_p8_templates.py",
)
FAST_EXCLUDES = ("addon_wheel_declares_entry_point",)
SOURCE_DIRS = ("adapter", "graphimo", "core/graphimo_core", "addons/pytorch/graphimo_pytorch")


class GateFailure(RuntimeError):
    pass


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False, text=True)
    if result.returncode:
        raise GateFailure(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _changed_python_files() -> list[Path]:
    names: set[str] = set()
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    base: str | None = None
    if upstream.returncode == 0:
        merge_base = subprocess.run(
            ["git", "merge-base", "HEAD", upstream.stdout.strip()],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        if merge_base.returncode == 0:
            base = merge_base.stdout.strip()
    if base is None:
        parent = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        if parent.returncode == 0:
            base = parent.stdout.strip()
    if base is not None:
        names.update(_git("diff", "--name-only", base, "--").splitlines())
    else:
        names.update(_git("ls-files").splitlines())
    names.update(_git("diff", "--name-only", "--").splitlines())
    names.update(_git("diff", "--cached", "--name-only", "--").splitlines())
    names.update(_git("ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(ROOT / name for name in names if name.endswith(".py") and (ROOT / name).is_file())


def _run(command: list[str], label: str, *, capture: bool = False) -> str:
    print(f"\n== {label} ==", flush=True)
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=capture,
    )
    if capture:
        output = result.stdout
        if output:
            print(output, end="" if output.endswith("\n") else "\n", flush=True)
    else:
        output = ""
    if result.returncode:
        if capture and result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        raise GateFailure(f"{label} failed with exit code {result.returncode}")
    return output


def _metadata_version(name: str) -> Version:
    try:
        return Version(importlib.metadata.version(name))
    except importlib.metadata.PackageNotFoundError as error:
        raise GateFailure(
            f"Missing required check dependency {name!r}. Install the documented development requirements."
        ) from error


def _requirements(full: bool) -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        root_data = tomllib.load(stream)
    root_project = root_data["project"]
    requirements = list(root_project.get("dependencies", ()))
    optional = root_project.get("optional-dependencies", {})
    for extra in ("dev", "notebook", "workbench"):
        requirements.extend(optional.get(extra, ()))

    requirements.append("torch>=2.6")
    if full:
        with (ADDON / "pyproject.toml").open("rb") as stream:
            addon_data = tomllib.load(stream)
        requirements.extend(addon_data["project"].get("dependencies", ()))
        requirements.append("graphviz>=0.20")
        if shutil.which("luau") is None:
            raise GateFailure(
                "Missing required full-gate tool 'luau'; install the Luau CLI for the live plugin bridge test."
            )

    local_projects = {
        "graphimo": Version(root_project["version"]),
        "graphimo-pytorch": Version(
            tomllib.loads((ADDON / "pyproject.toml").read_text())["project"]["version"]
        ),
    }
    missing: list[str] = []
    for raw in requirements:
        requirement = Requirement(raw)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        actual = local_projects.get(requirement.name.lower())
        if actual is None:
            try:
                actual = _metadata_version(requirement.name)
            except GateFailure as error:
                missing.append(str(error))
                continue
        if requirement.specifier and not requirement.specifier.contains(actual, prereleases=True):
            missing.append(
                f"{requirement} is required, but {requirement.name} {actual} is installed"
            )
    if missing:
        raise GateFailure("Project dependency check failed:\n- " + "\n- ".join(missing))
    print("Declared Graphimo and addon requirements are satisfied.")
    return requirements


def _format_changed_python() -> None:
    files = _changed_python_files()
    # These legacy modules predate the current formatter config. Keep the small
    # integration edits readable without reformatting unrelated code; new host
    # behavior lives in formatted mixins/handlers.
    format_exemptions = {
        ROOT / "graphimo/application.py",
        ADDON / "graphimo_pytorch/checkpoints.py",
        ADDON / "graphimo_pytorch/registry.py",
    }
    files = [path for path in files if path not in format_exemptions]
    if not files:
        print("No changed Python files to format-check.")
        return
    _run(
        [sys.executable, "-m", "ruff", "format", "--check", *map(str, files)],
        "ruff format --check (changed Python files)",
    )


def _generated_path(path: str) -> bool:
    parts = Path(path).parts
    return (
        "build" in parts
        or "dist" in parts
        or any(part.endswith(".egg-info") for part in parts)
        or "__marimo__" in parts
    )


def _check_repository_hygiene() -> None:
    tracked = _git("ls-files").splitlines()
    remaining = [path for path in tracked if _generated_path(path)]
    if remaining:
        raise GateFailure(
            "Generated build artifacts are still tracked:\n- " + "\n- ".join(remaining)
        )

    status = _git("status", "--porcelain", "--untracked-files=all")
    dirty_generated = []
    for line in status.splitlines():
        state, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if _generated_path(path) and "D" not in state:
            dirty_generated.append(path)
    if dirty_generated:
        raise GateFailure(
            "The gate left generated artifacts in the repository:\n- "
            + "\n- ".join(dirty_generated)
        )
    print("No tracked or dirty generated package artifacts remain.")


def _copy_repository(destination: Path) -> None:
    ignored = shutil.ignore_patterns(
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "__pycache__",
        "*.pyc",
        "build",
        "dist",
        "*.egg-info",
        "__marimo__",
        ".marimo",
    )
    shutil.copytree(ROOT, destination, ignore=ignored)


def _build_wheels() -> None:
    print("\n== Isolated root and PyTorch addon wheel builds ==", flush=True)
    with tempfile.TemporaryDirectory(prefix="graphimo-check-wheels-") as temporary:
        root = Path(temporary)
        project_copy = root / "graphimo-source"
        _copy_repository(project_copy)
        wheelhouse = root / "wheelhouse"
        root_wheels = wheelhouse / "root"
        addon_wheels = wheelhouse / "addon"
        root_wheels.mkdir(parents=True)
        addon_wheels.mkdir(parents=True)
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-build-isolation",
                "--no-deps",
                "--wheel-dir",
                str(root_wheels),
                str(project_copy),
            ],
            "Build Graphimo wheel in temporary source copy",
        )
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-build-isolation",
                "--no-deps",
                "--wheel-dir",
                str(addon_wheels),
                str(project_copy / "addons" / "pytorch"),
            ],
            "Build PyTorch addon wheel in temporary source copy",
        )
        root_built = list(root_wheels.glob("graphimo-*.whl"))
        addon_built = list(addon_wheels.glob("graphimo_pytorch-*.whl"))
        if len(root_built) != 1 or len(addon_built) != 1:
            raise GateFailure("Expected one root Graphimo wheel and one graphimo-pytorch wheel")
        for wheel, required_member in (
            (root_built[0], "graphimo/metablocks.py"),
            (addon_built[0], "graphimo_pytorch/templates.py"),
        ):
            with zipfile.ZipFile(wheel) as archive:
                if required_member not in archive.namelist():
                    raise GateFailure(f"{wheel.name} is missing {required_member}")
    print("Both distributions built and their expected package contents were verified.")


def _fast_gate() -> None:
    _requirements(full=False)
    _run(
        [sys.executable, "-m", "compileall", "-q", *SOURCE_DIRS],
        "Python syntax compilation",
    )
    _format_changed_python()
    _run([sys.executable, "-m", "ruff", "check", "."], "ruff check")
    _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *FAST_TESTS,
            "-k",
            "not " + " and not ".join(FAST_EXCLUDES),
        ],
        "Fast Core, Controls, ArtifactStore, and addon tests",
    )
    _check_repository_hygiene()


def _full_gate() -> None:
    _requirements(full=True)
    _fast_gate()
    output = _run(
        [sys.executable, "-m", "pytest", "-q", "-ra"],
        "Complete test suite including marimo, PyTorch, TorchLens, and addon isolation",
        capture=True,
    )
    skip_lines = [line.strip() for line in output.splitlines() if "skipped" in line.lower()]
    if skip_lines:
        print("Reported pytest skips:")
        for line in skip_lines:
            print(f"  {line}")
    _build_wheels()
    _check_repository_hygiene()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Graphimo's local validation gate.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fast", action="store_true", help="Core and quick addon checks")
    mode.add_argument("--full", action="store_true", help="Full local pre-push gate")
    arguments = parser.parse_args()
    try:
        (_full_gate if arguments.full else _fast_gate)()
    except GateFailure as error:
        print(f"\nCHECK FAILED: {error}", file=sys.stderr)
        return 1
    print("\nGraphimo check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
