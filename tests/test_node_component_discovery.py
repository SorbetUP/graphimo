import json

from adapter.graphimo_adapter import dispatch


def test_discovery_marks_only_explicit_nodes_executable(tmp_path, host):
    root = tmp_path / "discovery_components"
    root.mkdir(exist_ok=True)
    (root / "nodes.py").write_text(
        "from graphimo import node\n"
        "def helper(value):\n    return value + 1\n"
        "@node\n"
        "def scale(value: float, factor: float = 2.0) -> float:\n"
        "    result = value * factor\n"
        "    return result\n",
        encoding="utf-8",
    )

    result = dispatch({"op": "list_components", "root": str(root)}, host.context)

    assert result["ok"] is True, result
    callables = {item["name"]: item for item in result["components"][0]["callables"]}
    assert callables["helper"]["graphimo_node"] is False
    assert callables["helper"]["outputs"] == []
    scale = callables["scale"]
    assert scale["graphimo_node"] is True
    assert scale["inputs"] == [
        {"name": "value", "required": True, "positional": True, "has_default": False, "annotation": "float"},
        {"name": "factor", "required": False, "positional": True, "has_default": True, "default": 2.0, "annotation": "float"},
    ]
    assert scale["outputs"] == [
        {"index": 0, "declared_type": "float", "expressions": ["result"]}
    ]
    json.dumps(result, allow_nan=False)
