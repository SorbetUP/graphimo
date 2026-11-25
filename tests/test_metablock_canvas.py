"""Exercise the renderer-only MetaBlock projection with the Luau CLI."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "luau_metablock_canvas_driver.luau"
LUAU = shutil.which("luau")


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to exercise the production projection")
def test_metablock_projection_maps_addon_cards_to_the_existing_canvas_contract():
    result = subprocess.run(
        [LUAU, str(DRIVER)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"MetaBlock canvas projection failed:\n{result.stdout}\n{result.stderr}"
    )
    assert "METABLOCK_CANVAS_PROJECTION_OK" in result.stdout
