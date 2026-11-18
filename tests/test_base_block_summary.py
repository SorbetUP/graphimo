"""Contract tests for read-only Base Block card summaries."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LUAU = shutil.which("luau")
DRIVER = ROOT / "tests" / "luau_base_block_summary_driver.luau"
HEIGHT_DRIVER = ROOT / "tests" / "luau_base_block_height_driver.luau"


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to exercise BaseBlock.summary")
def test_base_block_summary_preserves_order_and_formats_labels():
    result = subprocess.run(
        [LUAU, str(DRIVER)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Base Block summary contract failed:\n{result.stdout}\n{result.stderr}"
    assert "BASE_BLOCK_SUMMARY_OK" in result.stdout


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to exercise Canvas.height")
def test_base_block_summary_height_wraps_without_moving_ports():
    result = subprocess.run(
        [LUAU, str(HEIGHT_DRIVER)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Base Block card height contract failed:\n{result.stdout}\n{result.stderr}"
    assert "BASE_BLOCK_HEIGHT_OK" in result.stdout
