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


def test_plugin_manifest_tools_match_luau_tool_registrations() -> None:
    """Every agent-visible tool schema must exist in init.luau and vice versa."""
    import json
    import re

    manifest = json.loads((ROOT / "plugin" / "zeron-plugin.json").read_text(encoding="utf-8"))
    schema_tools = {tool["name"] for tool in manifest["tools"]}
    init_source = (ROOT / "plugin" / "init.luau").read_text(encoding="utf-8")
    registered = set(re.findall(r'zeron\.tool\(\{\s*name\s*=\s*"([^"]+)"', init_source))
    assert schema_tools == registered, (
        f"manifest/registered tool mismatch: only in manifest={sorted(schema_tools - registered)}, "
        f"only in init.luau={sorted(registered - schema_tools)}"
    )
    # The connect tool must expose output_index: multi-output nodes are
    # connectable through the agent API, not only through drag-and-drop.
    connect = next(tool for tool in manifest["tools"] if tool["name"] == "connect")
    assert "output_index" in connect["input"]["properties"]
    assert connect["input"]["properties"]["output_index"]["type"] == "integer"
