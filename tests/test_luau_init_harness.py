"""Headless harness: run the real plugin/init.luau under the Luau CLI with a
stubbed Zeron runtime, build the page and fire every handler/tool.

The Luau CLI gives required modules a pristine global environment, so the
wrapper generates tests/.harness/: a copy of plugin/init.luau with a
`local ui = require("../luau_harness_runtime").ui` prelude and @ds requires
rewritten to local shims, plus the same prelude for every plugin module.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / ".harness"

PLUGIN_MODULES = [
    "marimo_bridge",
    "service_bridge",
    "layout",
    "canvas",
    "base_block",
    "component_library",
    "metablock_projection",
    "presets",
    "logo",
    "theme",
    "tool_adapter",
]
DS_COMPONENTS = [
    "text",
    "button",
    "glass",
    "markdown",
    "search_field",
    "context_menu",
    "dialog",
    "text_field",
    "badge",
    "empty_state",
    "section_title",
    "icon_button",
    "icon",
]
UI_PRELUDE = 'local ui = require("../luau_harness_runtime").ui\n'


def _generate_harness() -> None:
    HARNESS.mkdir(parents=True, exist_ok=True)
    init_source = (ROOT / "plugin" / "init.luau").read_text(encoding="utf-8")
    # The CLI rejects "@ds/..." aliases; point them at local shim files.
    init_source = re.sub(r'require\("@ds/([^"]+)"\)', r'require("./ds_\1")', init_source)
    (HARNESS / "init_entry.luau").write_text(UI_PRELUDE + init_source + "\nreturn true\n", encoding="utf-8")
    (HARNESS / "zeron.luau").write_text(
        'return require("../luau_harness_zeron")\n', encoding="utf-8"
    )
    for module in PLUGIN_MODULES:
        source = (ROOT / "plugin" / f"{module}.luau").read_text(encoding="utf-8")
        (HARNESS / f"{module}.luau").write_text(UI_PRELUDE + source, encoding="utf-8")
    for component in DS_COMPONENTS:
        (HARNESS / f"ds_{component}.luau").write_text(
            f'local f = require("../luau_harness_ds")\nreturn f("{component}")\n', encoding="utf-8"
        )


def test_luau_init_harness_builds_page_and_survives_handlers() -> None:
    luau = shutil.which("luau")
    if luau is None:
        pytest.skip("the Luau CLI is required for the init harness")
    _generate_harness()
    try:
        result = subprocess.run(
            [luau, "tests/luau_init_harness_driver.luau"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    finally:
        shutil.rmtree(HARNESS, ignore_errors=True)
    assert result.returncode == 0, f"init harness failed:\n{result.stdout}\n{result.stderr}"
    assert "INIT_HARNESS_OK" in result.stdout, result.stdout[-4000:]
