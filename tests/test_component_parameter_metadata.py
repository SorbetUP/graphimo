"""Contract for JSON-safe component parameter metadata."""

from __future__ import annotations

import json

from adapter.graphimo_adapter import dispatch


def test_list_components_exposes_only_safe_parameter_metadata(tmp_path, host):
    root = tmp_path / "metadata_components"
    root.mkdir()
    (root / "metadata.py").write_text(
        "from typing import Literal\nfrom graphimo import node as _node\n"
        "@_node\n"
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
        "@_node\n"
        "def inspect_unsafe(value: unsafe_annotation = unsafe_default):\n"
        "    return value\n"
        "@_node\n"
        "def inspect_unsupported_literal(choice: Literal[unsafe_default] = 'small'):\n"
        "    return choice\n"
        "@_node\n"
        "def inspect_mixed_literal(value: Literal[True, None, 1, 1.5, 'x'] = 'x'):\n"
        "    return value\n",
        encoding="utf-8",
    )

    result = dispatch({"op": "list_components", "root": str(root)}, host.context)

    assert result["ok"] is True
    callables = {item["name"]: item for item in result["components"][0]["callables"]}
    callable_info = callables["inspect_values"]
    parameters = {parameter["name"]: parameter for parameter in callable_info["parameters"]}
    assert callable_info["signature"].startswith("(required, implicit, none_value=None")

    assert parameters["required"] == {
        "name": "required",
        "required": True,
        "positional": True,
        "has_default": False,
    }
    assert parameters["implicit"]["required"] is True
    assert parameters["implicit"]["has_default"] is False
    assert parameters["none_value"]["has_default"] is True
    assert parameters["none_value"]["default"] is None
    assert parameters["flag"]["default"] is True
    assert parameters["flag"]["annotation"] == "bool"
    assert parameters["count"]["default"] == 7
    assert parameters["count"]["annotation"] == "int"
    assert parameters["ratio"]["default"] == 1.25
    assert parameters["ratio"]["annotation"] == "float"
    assert parameters["label"]["default"] == "sample"
    assert parameters["label"]["annotation"] == "str"
    assert parameters["forward"]["annotation"] == "NotImported"
    assert parameters["absent"]["annotation"] == "None"
    assert parameters["absent"]["default"] is None
    assert parameters["mode"]["choices"] == ["small", "large"]
    assert parameters["mode"]["annotation"].startswith("Literal[")

    for name in ("sequence", "payload", "infinity"):
        assert parameters[name]["has_default"] is True
        assert "default" not in parameters[name]
    assert "annotation" not in parameters["sequence"]

    # The live bridge and plugin transport both use JSON as the metadata boundary.
    decoded = json.loads(json.dumps(result, allow_nan=False))
    assert decoded == result

    unsafe = callables["inspect_unsafe"]
    unsafe_parameter = unsafe["parameters"][0]
    assert unsafe_parameter["has_default"] is True
    assert "default" not in unsafe_parameter
    assert "annotation" not in unsafe_parameter
    assert "Unsafe" not in unsafe["signature"]
    assert "..." in unsafe["signature"]

    unsupported_literal = callables["inspect_unsupported_literal"]
    unsupported_parameter = unsupported_literal["parameters"][0]
    assert "annotation" not in unsupported_parameter
    assert "choices" not in unsupported_parameter
    assert "Unsafe" not in unsupported_literal["signature"]

    mixed_literal = callables["inspect_mixed_literal"]
    mixed_parameter = mixed_literal["parameters"][0]
    assert mixed_parameter["choices"] == [True, None, 1, 1.5, "x"]
    assert mixed_parameter["annotation"] == "Literal[True, None, 1, 1.5, 'x']"
    compile(f"def signature_probe{mixed_literal['signature']}:\n    pass\n", "<signature>", "exec")
