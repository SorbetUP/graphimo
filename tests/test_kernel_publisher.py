from __future__ import annotations

import time
import json
import os
import socket
import subprocess
import sys
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest

from addons.pytorch.graphimo_pytorch.publisher import KernelEventPublisher
from addons.pytorch.graphimo_pytorch.runtime import PyTorchEventBus
from graphimo.client import GraphimoClient
from graphimo.service.server import GraphimoService


def _event_bus(max_events: int = 32) -> PyTorchEventBus:
    return PyTorchEventBus(max_events=max_events)


def _emit(bus: PyTorchEventBus, suffix: str) -> None:
    bus.emit(
        kind="activation.summary",
        info=SimpleNamespace(
            metablock_id="model-kernel",
            graph_revision="revision-1",
            runtime_instance_id="runtime-1",
        ),
        logical_element_id="encoder",
        module_path="encoder.0",
        step=4,
        payload={"suffix": suffix},
    )


def _wait_for_events(client: GraphimoClient, project_id: str, predicate, timeout: float = 3):
    deadline = time.monotonic() + timeout
    cursor = "0"
    received = []
    while time.monotonic() < deadline:
        result = client.events(project_id, cursor=cursor, limit=128, wait_ms=100)
        cursor = result["cursor"]
        received.extend(result["events"])
        if predicate(received):
            return received
    raise AssertionError(f"timed out waiting for events; received {received!r}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _kernel_execute(url: str, session: str, code: str) -> str:
    request = Request(
        url + "/api/kernel/execute",
        data=json.dumps({"code": code}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Marimo-Session-Id": session,
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        output = []
        event = None
        for line in response.read().decode().splitlines():
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
            elif line.startswith("data:") and event == "stdout":
                output.append(json.loads(line.partition(":")[2].strip()).get("data", ""))
    text = "".join(output)
    marker = "GRAPHIMO_RESULT:"
    assert marker in text, text
    return text.split(marker, 1)[1].splitlines()[0]


def test_publisher_batches_live_kernel_events_into_service_spool(tmp_path: Path) -> None:
    notebook = tmp_path / "live.py"
    notebook.write_text("# live kernel\n", encoding="utf-8")
    service = GraphimoService(manifest_path=tmp_path / "service.json").start()
    client = GraphimoClient(service.url, service.token)
    project = client.open_project(notebook)
    bus = _event_bus()
    publisher = KernelEventPublisher(
        bus,
        service_url=service.url,
        token=service.token,
        project_id=project.project_id,
        session_id="marimo-session-a",
        interval=0.01,
    ).start()
    try:
        _emit(bus, "one")
        events = _wait_for_events(client, project.project_id, lambda values: bool(values))
        pushed = next(item["addon_event"] for item in events if item["kind"] == "addon.event")
        assert pushed["project_id"] == project.project_id
        assert pushed["session_id"] == "marimo-session-a"
        assert pushed["runtime_instance_id"] == "runtime-1"
        assert pushed["payload"] == {"suffix": "one"}
        assert "model" not in pushed and "module" not in pushed
        deadline = time.monotonic() + 1
        while publisher.stats()["events_sent"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.stats()["events_sent"] >= 1
    finally:
        publisher.close()
        project.close()
        client.close()
        service.close()


def test_publisher_counts_queue_overflow_and_reconnects_after_service_restart(
    tmp_path: Path,
) -> None:
    notebook = tmp_path / "restart.py"
    notebook.write_text("# restartable kernel\n", encoding="utf-8")
    bus = _event_bus(max_events=1)
    _emit(bus, "dropped-1")
    _emit(bus, "dropped-2")
    _emit(bus, "kept")

    first = GraphimoService(manifest_path=tmp_path / "first.json").start()
    first_client = GraphimoClient(first.url, first.token)
    project = first_client.open_project(notebook)
    publisher = KernelEventPublisher(
        bus,
        service_url=first.url,
        token=first.token,
        project_id=project.project_id,
        session_id="marimo-session-b",
        interval=0.01,
    ).start()
    try:
        first_events = _wait_for_events(
            first_client,
            project.project_id,
            lambda values: any(
                item.get("addon_event", {}).get("kind") == "telemetry.dropped" for item in values
            ),
        )
        drop = next(
            item["addon_event"]
            for item in first_events
            if item.get("addon_event", {}).get("kind") == "telemetry.dropped"
        )
        assert drop["count"] == 2
        assert publisher.stats()["events_lost"] == 2
        first.close()
        _emit(bus, "during-outage")  # producer never performs network I/O
        deadline = time.monotonic() + 2
        while publisher.stats()["failures"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.stats()["failures"] > 0

        second = GraphimoService(manifest_path=tmp_path / "second.json").start()
        second_client = GraphimoClient(second.url, second.token)
        second_project = second_client.open_project(notebook)
        assert second_project.project_id == project.project_id
        publisher.reconfigure(
            service_url=second.url,
            token=second.token,
            project_id=second_project.project_id,
            session_id="marimo-session-b",
        )
        recovered = _wait_for_events(
            second_client,
            project.project_id,
            lambda values: any(
                item.get("addon_event", {}).get("payload", {}).get("suffix") == "during-outage"
                for item in values
            ),
        )
        assert any(
            item.get("addon_event", {}).get("payload", {}).get("suffix") == "during-outage"
            for item in recovered
        )
        deadline = time.monotonic() + 1
        while publisher.stats()["reconnects"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.stats()["reconnects"] >= 1
        publisher.close()
        second_project.close()
        second_client.close()
        second.close()
    finally:
        publisher.close()
        first_client.close()
        first.close()


def test_publisher_queue_saturation_counts_drops_without_blocking_event_producers() -> None:
    bus = _event_bus(max_events=32)
    publisher = KernelEventPublisher(
        bus,
        service_url="http://127.0.0.1:1",
        token="offline-token",
        project_id="project-offline",
        session_id="kernel-offline",
        interval=0.01,
        request_timeout=0.05,
        pending_capacity=1,
    ).start()
    try:
        for index in range(12):
            _emit(bus, str(index))
        deadline = time.monotonic() + 2
        while publisher.stats()["events_lost"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.stats()["events_lost"] > 0

        # The producer path remains a bounded in-memory append, even at full
        # publisher capacity and with the service unreachable.
        start = time.monotonic()
        _emit(bus, "training-still-runs")
        assert time.monotonic() - start < 0.1
    finally:
        publisher.close()


@pytest.mark.skipif(importlib.util.find_spec("marimo") is None, reason="marimo is not installed")
def test_real_marimo_kernel_owns_runtime_hooks_and_reconnects_without_blocking_training(
    tmp_path: Path,
) -> None:
    from websockets.sync.client import connect

    root = Path(__file__).resolve().parents[1]
    notebook = tmp_path / "kernel-live.py"
    notebook.write_text(
        "import marimo\n__generated_with = '0.25.2'\napp = marimo.App()\n"
        "if __name__ == '__main__': app.run()\n",
        encoding="utf-8",
    )
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    pythonpath = [str(root), str(root / "addons" / "pytorch")]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    log_path = tmp_path / "marimo.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
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
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        first = GraphimoService(manifest_path=tmp_path / "service-one.json").start()
        first_client = GraphimoClient(first.url, first.token)
        project = first_client.open_project(notebook)
        second = None
        second_client = None
        try:
            deadline = time.monotonic() + 25
            session = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.flush()
                    raise AssertionError(log_path.read_text(encoding="utf-8"))
                try:
                    with urlopen(base + "/api/sessions", timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            with connect(
                f"ws://127.0.0.1:{port}/ws?session_id=graphimo-kernel-bridge"
            ) as websocket:
                while time.monotonic() < deadline:
                    message = json.loads(
                        websocket.recv(timeout=max(0.1, deadline - time.monotonic()))
                    )
                    if message.get("op") == "kernel-ready":
                        break
                else:
                    raise AssertionError("marimo kernel did not become ready")
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        log.flush()
                        raise AssertionError(log_path.read_text(encoding="utf-8"))
                    try:
                        with urlopen(base + "/api/sessions", timeout=1) as response:
                            sessions = json.loads(response.read().decode())
                        if sessions:
                            session = next(iter(sessions))
                            break
                    except Exception:
                        time.sleep(0.1)
                assert session, log_path.read_text(encoding="utf-8")

            project.execute(
                "metablock.create",
                {"dialect_id": "pytorch.model", "metablock_id": "kernel-model"},
            )
            first_client.attach_kernel(project, url=base, token="", session_id=session)

            config = {
                "service_url": first.url,
                "service_token": first.token,
                "project_id": project.project_id,
                "session_id": session,
            }
            code = """
import builtins, json, torch
from graphimo_pytorch.model_definition import RuntimeBindingMap, RuntimeModelInfo, RuntimeSite
from graphimo_pytorch.runtime import RUNTIME_REGISTRY
from graphimo_pytorch.publisher import configure_kernel_publisher
config = CONFIG
configure_kernel_publisher(service_url=config['service_url'], token=config['service_token'], project_id=config['project_id'], session_id=config['session_id'])
model = torch.nn.Sequential(torch.nn.Linear(1, 1))
info = RuntimeModelInfo('kernel-runtime', 'kernel-model', 'revision-kernel', 'materialization-kernel', RuntimeBindingMap((RuntimeSite('linear', '0'),)))
RUNTIME_REGISTRY.register(model, info)
builtins._graphimo_test_model = model
print('GRAPHIMO_RESULT:' + json.dumps({'ok': True}))
""".replace("CONFIG", repr(config))
            first_result = json.loads(_kernel_execute(base, session, code))
            assert first_result["ok"] is True
            listed = first_client.execute(
                project,
                "metablock.invoke",
                {"metablock_id": "kernel-model", "action": "runtime.list", "args": {}},
            )
            assert listed["items"][0]["runtime_instance_id"] == "kernel-runtime"
            subscription = first_client.execute(
                project,
                "metablock.invoke",
                {
                    "metablock_id": "kernel-model",
                    "action": "runtime.subscribe",
                    "args": {
                        "runtime_instance_id": "kernel-runtime",
                        "logical_element_id": "linear",
                        "signal": "activation",
                        "rate_hz": 1000,
                    },
                },
            )
            gradient = first_client.execute(
                project,
                "metablock.invoke",
                {
                    "metablock_id": "kernel-model",
                    "action": "runtime.subscribe",
                    "args": {
                        "runtime_instance_id": "kernel-runtime",
                        "logical_element_id": "linear",
                        "signal": "gradient",
                        "rate_hz": 1000,
                    },
                },
            )
            assert subscription["subscription_id"] and gradient["subscription_id"]
            run_code = "import builtins, json, torch\nvalue = builtins._graphimo_test_model(torch.ones(2, 1, requires_grad=True)); value.sum().backward()\nprint('GRAPHIMO_RESULT:' + json.dumps({'ok': True}))"
            assert json.loads(_kernel_execute(base, session, run_code))["ok"] is True
            first_events = _wait_for_events(
                first_client,
                project.project_id,
                lambda values: {
                    item.get("addon_event", {}).get("kind")
                    for item in values
                    if item.get("kind") == "addon.event"
                }.issuperset({"activation.summary", "gradient.summary"}),
            )
            kernel_events = [
                item["addon_event"] for item in first_events if item.get("kind") == "addon.event"
            ]
            assert {item["kind"] for item in kernel_events} >= {
                "activation.summary",
                "gradient.summary",
            }
            assert all(item["session_id"] == session for item in kernel_events)
            assert all("model" not in item and "module" not in item for item in kernel_events)
            per_metablock = first_client.execute(
                project,
                "metablock.events",
                {"metablock_id": "kernel-model", "cursor": "0", "limit": 128},
            )
            assert {item["kind"] for item in per_metablock["events"]} >= {
                "activation.summary",
                "gradient.summary",
            }

            first.close()
            stopped = _kernel_execute(
                base,
                session,
                "import json\nfrom graphimo_pytorch.publisher import kernel_publisher_stats\nprint('GRAPHIMO_RESULT:' + json.dumps(kernel_publisher_stats()))",
            )
            assert json.loads(stopped)["running"] is False
            after_stop = "import builtins, json, torch\nvalue = builtins._graphimo_test_model(torch.ones(2, 1, requires_grad=True)); value.sum().backward()\nprint('GRAPHIMO_RESULT:' + json.dumps({'ok': True}))"
            continued = _kernel_execute(
                base,
                session,
                after_stop,
            )
            assert json.loads(continued)["ok"] is True

            second = GraphimoService(manifest_path=tmp_path / "service-two.json").start()
            second_client = GraphimoClient(second.url, second.token)
            reopened = second_client.open_project(notebook)
            assert reopened.project_id == project.project_id
            reconf = {
                "service_url": second.url,
                "service_token": second.token,
                "project_id": reopened.project_id,
                "session_id": session,
            }
            configure_again = """
import builtins, json, torch
from graphimo_pytorch.publisher import configure_kernel_publisher
config = CONFIG
status = configure_kernel_publisher(service_url=config['service_url'], token=config['service_token'], project_id=config['project_id'], session_id=config['session_id'])
value = builtins._graphimo_test_model(torch.ones(2, 1, requires_grad=True)); value.sum().backward()
print('GRAPHIMO_RESULT:' + json.dumps({'ok': True, 'running': status['running']}))
""".replace("CONFIG", repr(reconf))
            assert json.loads(_kernel_execute(base, session, configure_again))["running"] is True
            recovered = _wait_for_events(
                second_client,
                reopened.project_id,
                lambda values: any(item.get("source") == "marimo_kernel" for item in values),
            )
            assert any(item.get("source") == "marimo_kernel" for item in recovered)
            reopened.close()
            second_client.close()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            first_client.close()
            first.close()
            if second_client is not None:
                second_client.close()
            if second is not None:
                second.close()
