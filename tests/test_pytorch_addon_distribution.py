from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "addons" / "pytorch"


@pytest.mark.full
@pytest.mark.skipif(
    importlib.util.find_spec("torch") is None or importlib.util.find_spec("torchlens") is None,
    reason="install the graphimo-pytorch runtime extra for addon lifecycle tests",
)
def test_addon_wheel_discovers_materializes_and_preserves_state(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheelhouse),
            str(ADDON),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        timeout=90,
    )
    assert build.returncode == 0, f"addon wheel build failed:\n{build.stdout}\n{build.stderr}"
    wheel = next(wheelhouse.glob("graphimo_pytorch-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        packaged = set(archive.namelist())
    assert "graphimo_pytorch/templates.py" in packaged
    assert "graphimo_pytorch/training.py" in packaged
    install_target = tmp_path / "addon-install"
    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(install_target),
            str(wheel),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        timeout=90,
    )
    assert install.returncode == 0, (
        f"addon wheel install failed:\n{install.stdout}\n{install.stderr}"
    )

    notebook = tmp_path / "project.py"
    notebook.write_text("# Graphimo addon lifecycle fixture\n", encoding="utf-8")
    code = r"""
import json
import os
import sys
from pathlib import Path
from graphimo.application import GraphimoApplication
from graphimo.metablocks import GraphimoHostAPI, MetaBlockAddonManager
from graphimo_pytorch import inspect_checkpoint, load_checkpoint, save_checkpoint

root = Path(sys.argv[1])
os.environ["GRAPHIMO_ARTIFACT_STORE"] = str(root / "artifacts")
host = GraphimoHostAPI("test-project", root / "project.py")
application = GraphimoApplication(host)
created = application.execute("metablock.create", {
    "dialect_id": "pytorch.model", "metablock_id": "tiny", "request": {"name": "Tiny"}
})
edited = application.execute("metablock.apply", {
    "metablock_id": "tiny", "intent": {"type": "set_model_parameter", "name": "hidden_size", "value": 12}
})
revision = edited["revision"]
application.execute("layout.set", {
    "scope_id": "tiny_root",
    "layout": {"input": {"x": 15, "y": 25, "width": 210, "height": 120}}
})
sidecar = host.sidecar_path("tiny")
sidecar_before = sidecar.read_bytes()
missing = MetaBlockAddonManager(host, entry_points=lambda: []).project("tiny")
assert missing["available"] is False
assert sidecar.is_file()
assert sidecar.read_bytes() == sidecar_before
assert host.read_layout("tiny_root")["input"]["x"] == 15
restored = GraphimoApplication(host).execute("metablock.project", {"metablock_id": "tiny"})
assert restored["available"] is True and restored["revision"] == revision
assert restored["projection"]["name"] == "Tiny"
assert host.read_layout("tiny_root")["input"]["x"] == 15
materialized = GraphimoApplication(host).execute("metablock.materialize", {
    "metablock_id": "tiny", "bindings": {}
})
scope = {}
exec(materialized["source"], scope, scope)
definition = scope[materialized["result_name"]]
model = definition.instantiate()
assert isinstance(model(__import__("torch").ones(2, 4)), dict)
assert model.__graphimo_pytorch_runtime__.graph_revision == definition.graph_revision
assert definition.graph_revision in definition.generated_source
runtime_id = model.__graphimo_pytorch_runtime__.runtime_instance_id
runtime_list = application.execute("metablock.invoke", {
    "metablock_id": "tiny", "action": "runtime.list", "args": {}
})
assert runtime_list["items"][0]["runtime_instance_id"] == runtime_id
backbone_path = next(
    site["module_path"]
    for site in runtime_list["items"][0]["bindings"]
    if site["logical_element_id"] == "backbone"
)
subscribed = application.execute("metablock.invoke", {
    "metablock_id": "tiny",
    "action": "runtime.subscribe",
    "args": {
        "runtime_instance_id": runtime_id,
        "logical_element_id": "backbone",
        "signal": "activation",
        "rate_hz": 10,
    },
})
model(__import__("torch").ones(2, 4))
events = application.execute("metablock.events", {
    "metablock_id": "tiny", "cursor": "0", "limit": 128
})
assert subscribed["module_path"] == backbone_path
assert any(
    event["kind"] == "activation.summary" and event["logical_element_id"] == "backbone"
    for event in events["events"]
)
inspected = application.execute("metablock.invoke", {
    "metablock_id": "tiny",
    "action": "torchlens.inspect",
    "args": {
        "runtime_instance_id": runtime_id,
        "logical_element_id": "backbone",
        "inputs": [[1.0, 2.0, 3.0, 4.0]],
    },
})
assert inspected["module_path"] == backbone_path
assert inspected["selected_activation"]["available"] is True
reference = save_checkpoint(model, metadata={"epoch": 2})
checkpoint = inspect_checkpoint(reference)
restored = load_checkpoint(definition, reference)
assert checkpoint["artifact_kind"] == "pytorch-checkpoint"
assert checkpoint["metadata"] == {"epoch": 2}
assert restored.__graphimo_pytorch_runtime__.graph_revision == definition.graph_revision
print(json.dumps({"revision": revision, "parameters": definition.total_parameters, "checkpoint": reference.kind}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(install_target), str(ROOT), str(ROOT / "core"), str(ADDON)]
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"addon lifecycle failed:\n{result.stdout}\n{result.stderr}"
    assert '"parameters": 73' in result.stdout
    assert '"checkpoint": "pytorch-checkpoint"' in result.stdout
