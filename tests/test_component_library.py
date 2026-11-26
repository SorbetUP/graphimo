"""Exercise the production component-library projection through the Luau CLI."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "luau_component_library_driver.luau"
LUAU = shutil.which("luau")


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to exercise the production projection")
def test_component_library_groups_searches_and_preserves_metadata():
    result = subprocess.run(
        [LUAU, str(DRIVER)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Luau projection contract failed:\n{result.stdout}\n{result.stderr}"
    assert "COMPONENT_LIBRARY_PROJECTION_OK" in result.stdout
