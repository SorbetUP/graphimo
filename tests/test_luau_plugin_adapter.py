"""Renderer projection checks for the Zeron MetaBlock canvas adapter."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_metablock_projection_converts_dialect_data_to_canvas_cards() -> None:
    luau = shutil.which("luau")
    if luau is None:
        pytest.skip("the Luau CLI is required to exercise the Zeron renderer projection")
    result = subprocess.run(
        [luau, "tests/luau/test_metablock_projection.luau"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Luau projection test failed:\n{result.stdout}\n{result.stderr}"
    assert result.stdout.strip() == "metablock-projection-ok"
