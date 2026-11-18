"""Optional integration of the production Luau bridge with pinned marimo."""
from __future__ import annotations

import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
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
        raise AssertionError(f"marimo code-mode response had no Graphimo result: {joined!r}")
    return joined[start + len(marker):].splitlines()[0]


def _bridge_code(mode: str, *args: str) -> list[str]:
    result = subprocess.run(
        [LUAU, str(DRIVER), "-a", mode, *args],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    if result.returncode:
        raise AssertionError(f"Luau bridge shim failed:\n{result.stdout}\n{result.stderr}")
    blocks = re.findall(r"GRAPHIMO_CODE_BEGIN\n(.*?)\nGRAPHIMO_CODE_END", result.stdout, re.S)
    assert blocks, result.stdout
    return blocks


@pytest.mark.skipif(
    not LUAU or not MARIMO_PYTHON,
    reason="set GRAPHIMO_MARIMO_PYTHON and install the Luau CLI to run live bridge tests",
)
def test_managed_launcher_starts_local_marimo_without_a_user_selected_port(tmp_path: Path):
    notebook = tmp_path / "managed_notebook.py"
    notebook.write_text(
        "import marimo\n__generated_with = '0.25.2'\napp = marimo.App()\n"
        "if __name__ == '__main__': app.run()\n",
        encoding="utf-8",
    )
    generated = subprocess.run(
        [LUAU, str(DRIVER), "-a", "launcher_script", str(notebook)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert generated.returncode == 0, generated.stderr
    scripts = re.findall(
        r"GRAPHIMO_LAUNCH_SCRIPT_BEGIN\n(.*?)\nGRAPHIMO_LAUNCH_SCRIPT_END",
        generated.stdout,
        re.S,
    )
    assert len(scripts) == 1
    launched = subprocess.run(
        [MARIMO_PYTHON, "-c", scripts[0], str(notebook)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        timeout=40,
    )
    assert launched.returncode == 0, launched.stderr or launched.stdout
    marker = "GRAPHIMO_LAUNCH:"
    payload = next(line[len(marker) :] for line in launched.stdout.splitlines() if line.startswith(marker))
    result = json.loads(payload)
    assert result["started"] is True
    assert result["session_id"] == ""
    assert result["url"].startswith("http://")
    assert ":2718" in result["url"] or ":" in result["url"]
    assert result["log_path"].endswith(".log")
    try:
        os.kill(int(result["pid"]), 0)
        _request(result["url"] + "/api/sessions")
        from websockets.sync.client import connect

        websocket_url = result["url"].replace("http://", "ws://").replace("https://", "wss://")
        with connect(f"{websocket_url}/ws?session_id=graphimo-managed-test") as websocket:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                event = json.loads(websocket.recv(timeout=max(0.1, deadline - time.monotonic())))
                if event.get("op") == "kernel-ready":
                    break
            else:
                raise AssertionError("managed marimo notebook did not reach kernel-ready")
        reused = subprocess.run(
            [MARIMO_PYTHON, "-c", scripts[0], str(notebook)],
            cwd=tmp_path,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        assert reused.returncode == 0, reused.stderr or reused.stdout
        reused_payload = next(
            line[len(marker) :] for line in reused.stdout.splitlines() if line.startswith(marker)
        )
        reused_result = json.loads(reused_payload)
        assert reused_result["started"] is False
        assert reused_result["url"] == result["url"]
        assert reused_result["session_id"] == "graphimo-managed-test"
    finally:
        try:
            os.killpg(int(result["pid"]), signal.SIGTERM)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(
    not LUAU or os.environ.get("GRAPHIMO_TEST_BOOTSTRAP") != "1",
    reason="set GRAPHIMO_TEST_BOOTSTRAP=1 to test the network-backed private marimo install",
)
def test_managed_launcher_bootstraps_pinned_marimo_when_python_has_no_marimo(tmp_path: Path):
    available = subprocess.run(
        [sys.executable, "-c", "import importlib.util; print(bool(importlib.util.find_spec('marimo')))"] ,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    if available == "True":
        pytest.skip("the test interpreter already has marimo; use a clean Python to verify bootstrap")

    notebook = tmp_path / "bootstrap_notebook.py"
    notebook.write_text(
        "import marimo\n__generated_with = '0.25.2'\napp = marimo.App()\n"
        "if __name__ == '__main__': app.run()\n",
        encoding="utf-8",
    )
    generated = subprocess.run(
        [LUAU, str(DRIVER), "-a", "launcher_script", str(notebook)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert generated.returncode == 0, generated.stderr
    scripts = re.findall(
        r"GRAPHIMO_LAUNCH_SCRIPT_BEGIN\n(.*?)\nGRAPHIMO_LAUNCH_SCRIPT_END",
        generated.stdout,
        re.S,
    )
    assert len(scripts) == 1
    env = {**os.environ, "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    launched = subprocess.run(
        [sys.executable, "-c", scripts[0], str(notebook)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=280,
    )
    assert launched.returncode == 0, launched.stderr or launched.stdout
    marker = "GRAPHIMO_LAUNCH:"
    payload = next(line[len(marker) :] for line in launched.stdout.splitlines() if line.startswith(marker))
    result = json.loads(payload)
    assert result["started"] is True
    managed_python = tmp_path / ".local/share/graphimo/marimo-0.25.2/bin/python"
    assert managed_python.is_file()
    version = subprocess.run(
        [str(managed_python), "-c", "import importlib.metadata as m; print(m.version('marimo'))"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    assert version == "0.25.2"
    try:
        os.kill(int(result["pid"]), 0)
        _request(result["url"] + "/api/sessions")
    finally:
        try:
            os.killpg(int(result["pid"]), signal.SIGTERM)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to test production bridge helpers")
def test_problems_are_appended_to_a_persistent_user_log(tmp_path: Path):
    generated = subprocess.run(
        [LUAU, str(DRIVER), "-a", "problem_log_script"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert generated.returncode == 0, generated.stderr
    scripts = re.findall(
        r"GRAPHIMO_PROBLEM_LOG_SCRIPT_BEGIN\n(.*?)\nGRAPHIMO_PROBLEM_LOG_SCRIPT_END",
        generated.stdout,
        re.S,
    )
    assert len(scripts) == 1
    log_path = tmp_path / ".local/state/graphimo/problems.log"
    env = {**os.environ, "HOME": str(tmp_path)}
    for problem in ("cell execution failed", "marimo connection failed"):
        written = subprocess.run(
            [MARIMO_PYTHON or "python3", "-c", scripts[0], problem],
            env=env,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        assert written.returncode == 0, written.stderr
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        "cell execution failed",
        "marimo connection failed",
    ]


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to run production bridge tests")
def test_bridge_does_not_shift_later_optional_arguments_positionally():
    result = subprocess.run(
        [LUAU, str(DRIVER), "-a", "source_safety"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert ".normalize(std=saved_std)" in result.stdout
    assert ".normalize(saved_std)" not in result.stdout


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to run production bridge tests")
def test_bridge_reports_missing_zeron_command_permission():
    result = subprocess.run(
        [LUAU, str(DRIVER), "-a", "command_denied"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "GRAPHIMO_COMMAND_DENIAL_HANDLED" in result.stdout


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to run production bridge tests")
@pytest.mark.parametrize("mode", ["sessions_object", "sessions_array", "sessions_wrapped"])
def test_sessions_accepts_marimo_object_and_common_wrappers(mode: str):
    result = subprocess.run(
        [LUAU, str(DRIVER), "-a", mode],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    marker = "GRAPHIMO_SESSIONS:"
    encoded = next(line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker))
    sessions = json.loads(encoded)
    assert len(sessions) == 1
    assert sessions[0]["path"] == "/tmp/graphimo-live.py"


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to run production bridge tests")
def test_sessions_strips_editor_query_and_fragment_from_api_url():
    result = subprocess.run(
        [LUAU, str(DRIVER), "-a", "sessions_url"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "GRAPHIMO_REQUEST_URL:http://127.0.0.1:2718/api/sessions" in result.stdout


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to run production bridge tests")
@pytest.mark.parametrize(
    ("mode", "diagnostic"),
    [
        ("sessions_empty", "empty"),
        ("sessions_html", "HTML or markup"),
        ("sessions_null", "JSON null"),
    ],
)
def test_sessions_error_includes_safe_http_diagnostics(mode: str, diagnostic: str):
    result = subprocess.run(
        [LUAU, str(DRIVER), "-a", mode],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    marker = "GRAPHIMO_SESSION_ERROR:"
    message = next(line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker))
    assert diagnostic in message
    assert "HTTP 200" in message
    assert "application/json" in message or "text/html" in message
    assert "secret" not in message.lower()


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to run production bridge tests")
def test_sessions_connection_failure_is_a_structured_result_not_a_callback_exception():
    result = subprocess.run(
        [LUAU, str(DRIVER), "-a", "sessions_connection"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    marker = "GRAPHIMO_SESSION_ERROR:"
    message = next(line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker))
    assert "Could not read marimo /api/sessions: empty" in message
    assert "HTTP 000" in message
    assert "body 0 bytes" in message


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to run production bridge tests")
def test_execute_failure_is_a_structured_result_not_a_callback_exception():
    result = subprocess.run(
        [LUAU, str(DRIVER), "-a", "execute_empty"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "GRAPHIMO_EXECUTE_ERROR:Marimo did not complete the request" in result.stdout


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to run production bridge tests")
def test_list_components_propagates_an_inspect_connection_failure():
    result = subprocess.run(
        [LUAU, str(DRIVER), "-a", "components_connection"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "GRAPHIMO_OPERATION_ERROR:Could not read marimo /api/sessions" in result.stdout


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to run production bridge tests")
def test_zeron_tool_adapter_answers_only_after_async_result_resolves():
    result = subprocess.run(
        [LUAU, str(DRIVER), "-a", "tool_adapter"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "GRAPHIMO_TOOL_AWAITED:resolved" in result.stdout


@pytest.mark.skipif(
    not LUAU or not MARIMO_PYTHON,
    reason="set GRAPHIMO_MARIMO_PYTHON and install the Luau CLI to run live bridge tests",
)
def test_production_luau_bridge_against_live_marimo(tmp_path: Path):
    """Exercise inspect, component discovery/insertion, connect, and run dispatch."""
    notebook = tmp_path / "graphimo_live.py"
    notebook.write_text(
        "import marimo\n"
        "__generated_with = \"0.25.2\"\n"
        "app = marimo.App()\n\n"
        "@app.cell\n"
        "def source():\n"
        "    data = 3\n"
        "    transform = lambda value: value * 2\n"
        "    return data, transform\n\n"
        "@app.cell\n"
        "def prepare(data):\n"
        "    processed = data + 1\n"
        "    return (processed,)\n\n"
        "@app.cell\n"
        "def predict(processed, transform):\n"
        "    prediction = transform(processed)\n"
        "    return (prediction,)\n\n"
        "@app.cell\n"
        "def show_preview():\n"
        "    preview = 'Graphimo output preview'\n"
        "    preview\n"
        "    return (preview,)\n\n"
        "if __name__ == \"__main__\":\n"
        "    app.run()\n",
        encoding="utf-8",
    )
    component_dir = tmp_path / "components"
    component_dir.mkdir()
    (component_dir / "normalize.py").write_text(
        "def normalize(image):\n    return image + 1\n", encoding="utf-8"
    )
    (component_dir / "parameter_metadata.py").write_text(
        "from typing import Literal\n"
        "def inspect_values(required, implicit, none_value=None, flag: bool = True, "
        "count: int = 7, ratio: float = 1.25, label: str = 'sample', "
        "forward: 'NotImported' = 'x', absent: None = None, "
        "mode: Literal['small', 'large'] = 'small', "
        "sequence: list[int] = [], payload: dict = {'x': 1}, "
        "infinity: float = float('inf')):\n"
        "    return required, implicit, none_value, flag, count, ratio, label, "
        "forward, absent, mode, sequence, payload, infinity\n"
        "class Unsafe:\n"
        "    def __repr__(self): raise AssertionError('repr was called')\n"
        "    def __str__(self): raise AssertionError('str was called')\n"
        "unsafe_annotation = Unsafe()\n"
        "unsafe_default = Unsafe()\n"
        "def inspect_unsafe(value: unsafe_annotation = unsafe_default):\n"
        "    return value\n",
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

        websocket = connect(f"ws://127.0.0.1:{port}/ws?session_id=graphimo-live-test")
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
                _request(base)
                sessions = json.loads(_request(f"{base}/api/sessions"))
                if sessions:
                    session = next(iter(sessions))
                    break
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                time.sleep(0.2)
        assert session, f"marimo session did not start:\n{(tmp_path / 'marimo.log').read_text()}"

        initialized = '''import json
import marimo._code_mode as cm
async with cm.get_context() as ctx:
    for cell in list(ctx.cells): ctx.run_cell(cell.id)
print("GRAPHIMO_RESULT:" + json.dumps({"ok": True}))
'''
        assert json.loads(_execute(base, session, initialized))["ok"] is True
        notebook_path = str(notebook)
        inspect_code = _bridge_code("inspect", notebook_path)[0]
        snapshot = None
        initialization_deadline = time.monotonic() + 10
        while time.monotonic() < initialization_deadline:
            snapshot = json.loads(_execute(base, session, inspect_code))
            if any(cell["defs"] for cell in snapshot["cells"]):
                break
            time.sleep(0.2)
        assert snapshot is not None
        cells = snapshot["cells"]
        source = next(cell for cell in cells if "data = 3" in cell["source"])
        target = next(cell for cell in cells if "prediction = transform(processed)" in cell["source"])
        display = next(cell for cell in cells if "Graphimo output preview" in cell["source"])
        assert "data" in source["defs"] and "transform" in source["defs"], source
        assert "processed" in target["refs"] and "transform" in target["refs"], target
        assert isinstance(display["output"], dict)
        assert "Graphimo output preview" in display["output"]["preview"]
        assert any(edge["variable"] == "processed" for edge in snapshot["edges"])

        component_code = _bridge_code("list_components", notebook_path)[0]
        component_result = json.loads(_execute(base, session, component_code))
        normalize = next(item for item in component_result["components"] if item["id"] == "normalize.py")
        assert [item["name"] for item in normalize["callables"]] == ["normalize"]
        metadata = next(item for item in component_result["components"] if item["id"] == "parameter_metadata.py")["callables"]
        metadata = {callable_item["name"]: callable_item for callable_item in metadata}
        values = metadata["inspect_values"]
        parameters = {parameter["name"]: parameter for parameter in values["parameters"]}
        assert parameters["required"]["has_default"] is False
        assert parameters["required"]["required"] is True
        assert parameters["none_value"]["has_default"] is True
        assert parameters["none_value"]["default"] is None
        assert parameters["flag"]["default"] is True and parameters["flag"]["annotation"] == "bool"
        assert parameters["count"]["default"] == 7 and parameters["count"]["annotation"] == "int"
        assert parameters["ratio"]["default"] == 1.25 and parameters["ratio"]["annotation"] == "float"
        assert parameters["label"]["default"] == "sample" and parameters["label"]["annotation"] == "str"
        assert parameters["forward"]["annotation"] == "NotImported"
        assert parameters["absent"]["annotation"] == "None"
        assert parameters["mode"]["choices"] == ["small", "large"]
        for name in ("sequence", "payload", "infinity"):
            assert parameters[name]["has_default"] is True
            assert "default" not in parameters[name]
        unsafe = metadata["inspect_unsafe"]
        unsafe_parameter = unsafe["parameters"][0]
        assert unsafe_parameter["has_default"] is True
        assert "default" not in unsafe_parameter and "annotation" not in unsafe_parameter
        assert "Unsafe" not in unsafe["signature"] and "..." in unsafe["signature"]
        assert json.loads(json.dumps(component_result, allow_nan=False)) == component_result

        add_codes = _bridge_code("add_component", notebook_path)
        assert len(add_codes) == 2
        real_component_result = json.loads(_execute(base, session, add_codes[0]))
        real_normalize = next(item for item in real_component_result["components"] if item["id"] == "normalize.py")
        assert real_normalize["callables"][0]["parameters"][0]["positional"] is True
        added = json.loads(_execute(base, session, add_codes[1]))
        assert added["ok"] is True
        assert "normalize(data)" in added["source"]

        connect_code = _bridge_code(
            "connect", notebook_path, source["id"], "data", target["id"], "processed"
        )[0]
        connected = json.loads(_execute(base, session, connect_code))
        assert connected["ok"] is True and connected["changed"] is True
        after_connect = json.loads(_execute(base, session, inspect_code))
        edited_target = next(cell for cell in after_connect["cells"] if cell["id"] == target["id"])
        assert "prediction = transform(data)" in edited_target["source"]
        assert any(
            edge["source_cell_id"] == source["id"]
            and edge["target_cell_id"] == target["id"]
            and edge["variable"] == "data"
            for edge in after_connect["edges"]
        )

        run_code = _bridge_code("run", notebook_path, "", "", "", "", target["id"])[0]
        run_result = json.loads(_execute(base, session, run_code))
        assert run_result["ok"] is True and run_result["status"] == "queued"
        final_target = None
        run_deadline = time.monotonic() + 10
        while time.monotonic() < run_deadline:
            final_snapshot = json.loads(_execute(base, session, inspect_code))
            final_target = next(cell for cell in final_snapshot["cells"] if cell["id"] == target["id"])
            if final_target["status"] not in {"stale", "queued", "running"}:
                break
            time.sleep(0.2)
        assert final_target is not None
        assert isinstance(final_target["status"], str) and final_target["status"]
        assert not final_target["errors"]

        prepared = next(cell for cell in final_snapshot["cells"] if "processed = data + 1" in cell["source"])
        ambiguous_edit = f'''import json
import marimo._code_mode as cm
async with cm.get_context() as ctx:
    ctx.edit_cell({target["id"]!r}, code="prediction = transform(data, fallback=1)")
print("GRAPHIMO_RESULT:" + json.dumps({{"ok": True}}))
'''
        assert json.loads(_execute(base, session, ambiguous_edit))["ok"] is True
        refusal_code = _bridge_code(
            "connect", notebook_path, prepared["id"], "processed", target["id"], "data"
        )[0]
        refusal = json.loads(_execute(base, session, refusal_code))
        assert refusal["ok"] is False
        assert refusal["error"].startswith("unsafe_edit:")
        unchanged = json.loads(_execute(base, session, inspect_code))
        unchanged_target = next(cell for cell in unchanged["cells"] if cell["id"] == target["id"])
        assert unchanged_target["source"] == "prediction = transform(data, fallback=1)"
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
