"""Opt-in live contract tests for the read-only marimo control inventory."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "luau_bridge_driver.luau"
LUAU = shutil.which("luau")
MARIMO_PYTHON = os.environ.get("GRAPHIMO_MARIMO_PYTHON")


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(url: str, *, session: str | None = None, code: str | None = None) -> str:
    headers = {"Accept": "text/event-stream"}
    data = None
    method = "GET"
    if code is not None:
        method = "POST"
        data = json.dumps({"code": code}).encode()
        headers["Content-Type"] = "application/json"
    if session:
        headers["Marimo-Session-Id"] = session
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=40) as response:
        return response.read().decode()


def _execute(base: str, session: str, code: str) -> str:
    response = _request(f"{base}/api/kernel/execute", session=session, code=code)
    values = []
    event = None
    for line in response.splitlines():
        if line.startswith("event:"):
            event = line.partition(":")[2].strip()
        elif line.startswith("data:") and event == "stdout":
            values.append(json.loads(line.partition(":")[2].strip()).get("data", ""))
    joined = "".join(values)
    marker = "GRAPHIMO_RESULT:"
    start = joined.find(marker)
    if start < 0:
        raise AssertionError(
            f"marimo response had no Graphimo result: stdout={joined!r}; SSE={response[:12000]!r}"
        )
    return joined[start + len(marker) :].splitlines()[0]


def _inspect_code() -> str:
    result = subprocess.run(
        [LUAU, str(DRIVER), "-a", "inspect", "/tmp/control-inventory.py"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, f"Luau bridge shim failed:\n{result.stderr}"
    blocks = re.findall(r"GRAPHIMO_CODE_BEGIN\n(.*?)\nGRAPHIMO_CODE_END", result.stdout, re.DOTALL)
    assert len(blocks) == 1, result.stdout
    return blocks[0]


@pytest.mark.skipif(
    not LUAU or not MARIMO_PYTHON,
    reason="set GRAPHIMO_MARIMO_PYTHON and install the Luau CLI to run live bridge tests",
)
def test_production_inspect_projects_only_safe_live_slider_and_dropdown_controls(tmp_path: Path):
    notebook = tmp_path / "control_inventory.py"
    notebook.write_text(
        "import marimo\n"
        "__generated_with = '0.25.2'\n"
        "app = marimo.App()\n\n"
        "@app.cell\n"
        "def imports():\n"
        "    import marimo as mo\n"
        "    return (mo,)\n\n"
        "@app.cell\n"
        "def controls(mo):\n"
        "    class UnsafeValue:\n"
        "        def __repr__(self): raise AssertionError('unsafe repr called')\n"
        "        def __str__(self): raise AssertionError('unsafe str called')\n"
        "    z_slider = mo.ui.slider(0, 10, value=3, label='')\n"
        "    a_dropdown = mo.ui.dropdown({'One': 1, 'Two': 2}, value='Two', label='Choose')\n"
        "    toggle = mo.ui.checkbox(value=True, label='Toggle')\n"
        "    unsafe_dropdown = mo.ui.dropdown({'Unsafe': UnsafeValue()}, value='Unsafe', label='Unsafe')\n"
        "    return a_dropdown, z_slider, toggle, unsafe_dropdown\n\n"
        "@app.cell\n"
        "def later(mo):\n"
        "    aa_later = mo.ui.slider(-2, 2, value=1, label='Later')\n"
        "    return (aa_later,)\n\n"
        "if __name__ == '__main__': app.run()\n",
        encoding="utf-8",
    )

    port = _port()
    base = f"http://127.0.0.1:{port}"
    server_log = (tmp_path / "marimo.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [
            MARIMO_PYTHON,
            "-m",
            "marimo",
            "edit",
            str(notebook),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--headless",
            "--no-token",
            "--no-sandbox",
            "--skip-update-check",
        ],
        cwd=tmp_path,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    websocket = None
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and server.poll() is None:
            try:
                _request(base)
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.2)
        from websockets.sync.client import connect

        websocket = connect(f"ws://127.0.0.1:{port}/ws?session_id=control-inventory-test")
        while time.monotonic() < deadline:
            message = json.loads(websocket.recv(timeout=max(0.1, deadline - time.monotonic())))
            if message.get("op") == "kernel-ready":
                break
        else:
            raise AssertionError("marimo websocket did not report kernel-ready")

        session = None
        while time.monotonic() < deadline:
            if server.poll() is not None:
                server_log.flush()
                raise AssertionError(f"marimo exited:\n{(tmp_path / 'marimo.log').read_text()}")
            try:
                sessions = json.loads(_request(f"{base}/api/sessions"))
                if sessions:
                    session = next(iter(sessions))
                    break
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                time.sleep(0.2)
        assert session, f"marimo session did not start:\n{(tmp_path / 'marimo.log').read_text()}"

        initialize = '''import json
import marimo._code_mode as cm
async with cm.get_context() as ctx:
    for cell in list(ctx.cells): ctx.run_cell(cell.id)
print("GRAPHIMO_RESULT:" + json.dumps({"ok": True}))
'''
        assert json.loads(_execute(base, session, initialize))["ok"] is True
        inspect_code = _inspect_code()
        snapshot = json.loads(_execute(base, session, inspect_code))
        cells = snapshot["cells"]
        controls_cell = next(cell for cell in cells if "a_dropdown" in cell["defs"])
        later_cell = next(cell for cell in cells if "aa_later" in cell["defs"])
        controls = snapshot["controls"]

        assert [(item["cell_id"], item["name"]) for item in controls] == [
            (controls_cell["id"], "a_dropdown"),
            (controls_cell["id"], "unsafe_dropdown"),
            (controls_cell["id"], "z_slider"),
            (later_cell["id"], "aa_later"),
        ]
        assert controls[0] == {
            "cell_id": controls_cell["id"],
            "name": "a_dropdown",
            "kind": "dropdown",
            "label": "Choose",
            "value": 2,
            "choices": [{"label": "One", "value": 1}, {"label": "Two", "value": 2}],
        }
        assert controls[1] == {
            "cell_id": controls_cell["id"],
            "name": "unsafe_dropdown",
            "kind": "dropdown",
            "label": "Unsafe",
        }
        assert controls[2] == {
            "cell_id": controls_cell["id"],
            "name": "z_slider",
            "kind": "slider",
            "label": "z_slider",
            "value": 3,
        }
        assert controls[3] == {
            "cell_id": later_cell["id"],
            "name": "aa_later",
            "kind": "slider",
            "label": "Later",
            "value": 1,
        }
        assert "toggle" not in {item["name"] for item in controls}
        assert json.loads(json.dumps(snapshot, allow_nan=False)) == snapshot

        version_guard = "metadata.version('marimo') == '0.25.2'"
        assert inspect_code.count(version_guard) == 1
        unsupported_version_code = inspect_code.replace(
            version_guard, "metadata.version('marimo') == '99.0.0'"
        )
        unsupported_version = json.loads(_execute(base, session, unsupported_version_code))
        assert "controls" not in unsupported_version
        assert unsupported_version["cells"]

        widget_import = "            from marimo._plugins.ui._core.ui_element import UIElement"
        assert inspect_code.count(widget_import) == 1
        unsupported_api_code = inspect_code.replace(
            widget_import, "            raise ImportError('simulated unsupported marimo UI API')"
        )
        unsupported_api = json.loads(_execute(base, session, unsupported_api_code))
        assert "controls" not in unsupported_api
        assert unsupported_api["cells"]
    finally:
        if websocket is not None:
            websocket.close()
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        server_log.close()
