from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "luau_service_bridge_driver.luau"
LUAU = shutil.which("luau")


@pytest.mark.skipif(not LUAU, reason="Luau CLI is required for service bridge tests")
@pytest.mark.parametrize(
    ("mode", "marker"),
    [
        ("open", "GRAPHIMO_SERVICE_OPEN_OK"),
        ("execute", "GRAPHIMO_SERVICE_EXECUTE_OK"),
        ("events", "GRAPHIMO_SERVICE_EVENTS_OK"),
    ],
)
def test_service_bridge_uses_local_client_and_authenticated_http(mode: str, marker: str) -> None:
    result = subprocess.run(
        [LUAU, str(DRIVER), "-a", mode],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker in result.stdout
