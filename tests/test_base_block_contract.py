"""Static contracts for function-backed Graphimo Base Blocks."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import adapter.graphimo_adapter as adapter
from adapter.graphimo_adapter import dispatch

ROOT = Path(__file__).resolve().parents[1]
LUAU = shutil.which("luau")


def _component_callables(result):
    assert result["ok"] is True
    return {
        callable_item["name"]: callable_item
        for item in result["components"]
        for callable_item in item["callables"]
    }


def test_component_discovery_projects_conservative_static_return_contract(tmp_path, host):
    root = tmp_path / "base_block_components"
    root.mkdir()
    contract_source = (
        "from graphimo import node\n"
        "from typing import Literal, NamedTuple\n"
        "def obtenir_infos(prix: float, quantite: int, stock: int) -> tuple[float, int, int]:\n"
        "    flower = 3 + stock\n"
        "    return prix + 0.2, quantite + stock, flower\n"
        "def scalar(value: int) -> int:\n"
        "    answer = value * 2\n"
        "    return answer\n"
        "def scalar_expression(value: int) -> int:\n"
        "    return value * 2\n"
        "def scalar_literal(value) -> Literal[1, 2.0]:\n"
        "    return 1\n"
        "def nonfinite_scalar():\n"
        "    return 1e309\n"
        "def no_return(value):\n"
        "    value + 1\n"
        "def explicit_none() -> None:\n"
        "    return None\n"
        "def coherent_branches(value: int, flag: bool) -> tuple[int, int]:\n"
        "    if flag:\n"
        "        return value, value + 1\n"
        "    return value, value + 2\n"
        "def mismatched_branches(flag: bool) -> tuple[int, int]:\n"
        "    if flag:\n"
        "        return 1, 2\n"
        "    return 1\n"
        "def dynamic_tuple(value) -> tuple[int, int]:\n"
        "    return tuple(value)\n"
        "def dictionary_output(value) -> dict:\n"
        "    return {'value': value}\n"
        "def named_tuple_output(value):\n"
        "    return NamedTuple('Output', [('value', int)])(value)\n"
        "calls = []\n"
        "def must_not_run(value: int) -> int:\n"
        "    calls.append(value)\n"
        "    return value\n"
    ).replace("def ", "@node\ndef ")
    (root / "contracts.py").write_text(contract_source, encoding="utf-8")

    callables = _component_callables(
        dispatch({"op": "list_components", "root": str(root)}, host.context)
    )

    info = callables["obtenir_infos"]
    assert info["base_block"]["inputs"] == info["parameters"]
    assert info["base_block"]["output_mode"] == "tuple"
    assert info["outputs"] == [
        {"index": 0, "declared_type": "float", "expressions": ["prix + 0.2"]},
        {"index": 1, "declared_type": "int", "expressions": ["quantite + stock"]},
        {"index": 2, "declared_type": "int", "expressions": ["flower"]},
    ]

    scalar = callables["scalar"]["base_block"]
    assert scalar["output_mode"] == "single"
    assert scalar["outputs"] == [
        {"index": 1, "name": "output_1", "expressions": ["answer"], "expression": "answer", "annotation": "int"}
    ]
    scalar_expression = callables["scalar_expression"]["base_block"]
    assert scalar_expression["outputs"] == [
        {"index": 1, "name": "output_1", "expressions": ["value * 2"], "expression": "value * 2", "annotation": "int"}
    ]
    scalar_literal = callables["scalar_literal"]["base_block"]
    assert scalar_literal["output_mode"] == "single"
    assert scalar_literal["outputs"] == [
        {"index": 1, "name": "output_1", "expressions": ["1"], "expression": "1"}
    ]
    nonfinite = callables["nonfinite_scalar"]["base_block"]
    assert nonfinite["output_mode"] == "single"
    assert nonfinite["outputs"] == [
        {"index": 1, "name": "output_1", "expressions": ["1e309"], "expression": "1e309"}
    ]

    assert callables["no_return"]["base_block"] == {
        "inputs": callables["no_return"]["parameters"],
        "outputs": [],
        "output_mode": "none",
    }
    assert callables["explicit_none"]["outputs"] == [
        {"index": 0, "declared_type": "None", "expressions": ["None"]}
    ]

    branches = callables["coherent_branches"]["base_block"]
    assert branches["output_mode"] == "tuple"
    assert branches["outputs"] == [
        {"index": 1, "name": "output_1", "expressions": ["value"], "expression": "value", "annotation": "int"},
        {"index": 2, "name": "output_2", "expressions": ["value + 1", "value + 2"], "annotation": "int"},
    ]
    assert callables["mismatched_branches"]["diagnostic"] == "node_output_arity_inconsistent"
    for name in ("dynamic_tuple", "dictionary_output", "named_tuple_output"):
        contract = callables[name]["base_block"]
        assert contract["output_mode"] == "single", name
        assert len(contract["outputs"]) == 1, name

    # The functions were inspected statically; discovery did not call them.
    imported_contracts = adapter._load_module(root / "contracts.py")
    assert imported_contracts.calls == []
    json.dumps(callables, allow_nan=False)


def test_unavailable_function_source_falls_back_to_one_object(tmp_path, host, monkeypatch):
    root = tmp_path / "source_unavailable_components"
    root.mkdir()
    (root / "unavailable.py").write_text(
        "from graphimo import node\n"
        "@node\n"
        "def compute(value: int) -> tuple[int, int]:\n"
        "    return value, value + 1\n",
        encoding="utf-8",
    )
    original_getsource = adapter.inspect.getsource

    def unavailable_source(value):
        if getattr(value, "__name__", None) == "compute":
            raise OSError("source is unavailable")
        return original_getsource(value)

    monkeypatch.setattr(adapter.inspect, "getsource", unavailable_source)
    callables = _component_callables(
        dispatch({"op": "list_components", "root": str(root)}, host.context)
    )
    assert callables["compute"]["graphimo_node"] is True
    assert callables["compute"]["diagnostic"] == "node_source_unavailable"
    assert callables["compute"]["outputs"] == []


def test_inspect_links_only_exact_generated_component_markers_and_call(tmp_path, host):
    component = tmp_path / "components" / "normalize.py"
    component.parent.mkdir(exist_ok=True)
    component.write_text(
        "def normalize(image):\n    return image + 1\n",
        encoding="utf-8",
    )
    source_hash = "a" * 64
    generated = (
        "# Graphimo component: normalize.py\n"
        f"# Graphimo source sha256: {source_hash}\n"
        "# Graphimo callable: normalize\n"
        "normalized = __import__('importlib').reload(__import__('importlib').import_module("
        "'components.normalize')).normalize(image=data)\n"
    )
    host.marimo.snapshot["cells"] = [
        {"id": "instance-a", "source": generated, "defs": ["normalized"], "refs": ["data"]},
        {"id": "instance-b", "source": generated.replace("normalized =", "again ="), "defs": ["again"], "refs": ["data"]},
        {"id": "legacy", "source": generated.replace("# Graphimo callable: normalize\n", ""), "defs": ["normalized"], "refs": ["data"]},
        {"id": "wrong-call", "source": generated.replace(".normalize(image=data)", ".other(image=data)"), "defs": ["normalized"], "refs": ["data"]},
        {"id": "stale", "source": generated.replace(source_hash, "0" * 64), "defs": ["normalized"], "refs": ["data"]},
    ]

    result = dispatch({"op": "inspect", "session_id": "fixture"}, host.context)

    cells = {cell["id"]: cell for cell in result["cells"]}
    for cell_id in ("instance-a", "instance-b"):
        assert cells[cell_id]["graphimo_component"] == {
            "component_id": "normalize.py",
            "callable": "normalize",
            "source_sha256": source_hash,
        }
    for cell_id in ("legacy", "wrong-call"):
        assert "graphimo_component" not in cells[cell_id]
    assert cells["stale"]["graphimo_component"] == {
        "component_id": "normalize.py",
        "callable": "normalize",
        "source_sha256": "0" * 64,
    }


@pytest.mark.skipif(not LUAU, reason="install the Luau CLI to exercise the production projection")
def test_base_block_luau_projection_orders_and_rejects_mismatched_references():
    driver = ROOT / "tests" / "luau_base_block_driver.luau"
    result = subprocess.run(
        [LUAU, str(driver)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Base Block projection failed:\n{result.stdout}\n{result.stderr}"
    assert "BASE_BLOCK_PROJECTION_OK" in result.stdout
