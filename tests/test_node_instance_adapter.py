import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from graphimo.instance import build_instance_source, parse_instance_source
from adapter.graphimo_adapter import dispatch

ROOT = Path(__file__).resolve().parents[1]


def _write_node(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "scale.py").write_text(
        "from graphimo import node\n"
        "@node\n"
        "def scale(x: float, factor: float = 1.0) -> float:\n"
        "    y = x * factor\n"
        "    return y\n",
        encoding="utf-8",
    )


def test_add_component_requires_node_and_required_bindings(project, host):
    plain = project.components / "plain.py"
    plain.write_text("def helper(value):\n    return value\n", encoding="utf-8")
    not_node = dispatch(
        {"op": "add_component", "session_id": "fixture", "component_id": "plain.py", "callable": "helper"},
        host.context,
    )
    assert not_node["error"]["code"] == "node_required"

    _write_node(project.components)
    missing = dispatch(
        {
            "op": "add_component", "session_id": "fixture", "component_id": "scale.py",
            "callable": "scale", "bindings": {},
        },
        host.context,
    )
    assert missing["error"]["code"] == "missing_required_binding"
    assert host.marimo.added == []


def test_two_instances_share_node_and_reconnect_is_local(project, host):
    _write_node(project.components)
    call = {"op": "add_component", "session_id": "fixture", "component_id": "scale.py", "callable": "scale"}
    first = dispatch({**call, "bindings": {"x": {"variable": "data"}, "factor": {"literal": 2}}}, host.context)
    second = dispatch({**call, "bindings": {"x": {"variable": "data"}, "factor": {"literal": 3}}}, host.context)
    assert first["ok"] and second["ok"]
    assert first["cell_id"] != second["cell_id"]
    assert host.marimo.added[0][1].splitlines()[0] == "# Graphimo node: scale"
    assert host.marimo.added[1][1].splitlines()[0] == "# Graphimo node: scale"
    original_first = host.marimo.added[0][1]
    original_second = host.marimo.added[1][1]
    first_instance = parse_instance_source(original_first)
    second_instance = parse_instance_source(original_second)
    assert first_instance is not None and second_instance is not None
    host.marimo.snapshot["cells"] = [
        {"id": first["cell_id"], "source": original_first, "defs": [first_instance.result_var], "refs": ["data"]},
        {"id": second["cell_id"], "source": original_second, "defs": [second_instance.result_var], "refs": ["data"]},
    ]
    changed = dispatch(
        {
            "op": "connect", "session_id": "fixture", "from_cell_id": first["cell_id"],
            "to_cell_id": second["cell_id"], "input_name": "x",
        },
        host.context,
    )
    assert changed["ok"] is True
    assert host.marimo.replaced == [("fixture", second["cell_id"], changed["source"])]
    assert host.marimo.added[0][1] == original_first
    assert f"x={first_instance.result_var}" in changed["source"]


def test_instance_runtime_inspection_returns_bounded_preview(project, host):
    _write_node(project.components)
    source, instance = build_instance_source(
        "scale", {"x": "4.2", "factor": "1.0"}, instance_id="4f2a7c91"
    )
    host.marimo.snapshot["cells"] = [
        {"id": "scale-cell", "source": source, "defs": [instance.result_var], "refs": []}
    ]
    host.marimo.values[instance.result_var] = 4.2

    result = dispatch(
        {"op": "inspect_node_instance", "session_id": "fixture", "cell_id": "scale-cell"},
        host.context,
    )

    assert result["ok"] is True
    assert result["instance_id"] == "4f2a7c91"
    assert result["outputs"] == [
        {
            "index": 0,
            "declared_type": "float",
            "runtime_type": "float",
            "preview": "4.2",
            "expressions": ["y"],
            "active_expression": None,
        }
    ]


@pytest.mark.skipif(importlib.util.find_spec("marimo") is None, reason="marimo is not installed")
def test_two_instance_example_executes_in_live_marimo(tmp_path):
    output = tmp_path / "simple-pipeline.html"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "marimo",
            "export",
            "html",
            str(ROOT / "examples" / "simple_pipeline.py"),
            "-o",
            str(output),
            "--force",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"live marimo export failed:\n{result.stdout}\n{result.stderr}"
    html = output.read_text(encoding="utf-8")
    assert "g_scale_2a000001" in html
    assert "g_scale_3a000001" in html
    assert "scale_2_mean" in html and "scale_3_mean" in html
