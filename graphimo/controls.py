"""Readable marimo-backed Control Block source contracts."""

from __future__ import annotations

import ast
import json
import math
import re
import secrets
from dataclasses import dataclass
from typing import Any

_ID = re.compile(r"^[0-9a-f]{8}$")
_CONTROL_NAMES = {"slider", "range_slider", "number", "select", "checkbox", "switch", "text", "button"}
CONTROL_DEFAULTS: dict[str, dict[str, Any]] = {
    "slider": {"start": 0.0, "stop": 1.0, "step": 0.01, "value": 0.2, "debounce": True},
    "range_slider": {"start": 0.0, "stop": 1.0, "step": 0.01, "value": [0.25, 0.75], "debounce": True},
    "number": {"value": 0.0, "step": 1.0, "debounce": True},
    "select": {"options": ["A", "B"], "value": "A", "searchable": False},
    "checkbox": {"value": False},
    "switch": {"value": False},
    "text": {"value": "", "placeholder": "", "debounce": True},
    "button": {"value": 0},
}


@dataclass(frozen=True, slots=True)
class ControlSpec:
    control_type: str
    instance_id: str
    result_var: str
    parameters: dict[str, Any]

    @property
    def output_expression(self) -> str:
        return f"{self.result_var}.value"


def build_control_source(
    control_type: str,
    parameters: dict[str, Any],
    *,
    instance_id: str | None = None,
) -> tuple[str, ControlSpec]:
    if control_type not in _CONTROL_NAMES:
        raise ValueError(f"unsupported control type: {control_type}")
    chosen_id = secrets.token_hex(4) if instance_id is None else instance_id
    if not isinstance(chosen_id, str) or not _ID.fullmatch(chosen_id):
        raise ValueError("control instance ID must be exactly eight lowercase hexadecimal characters")
    if not isinstance(parameters, dict):
        raise TypeError("control parameters must be a dictionary")
    normalized = _validate_parameters(control_type, parameters)
    result_var = f"g_{control_type}_{chosen_id}"
    arguments = [f"    {name}={_python_literal(value)}" for name, value in normalized.items()]
    if control_type == "button":
        arguments.append("    on_click=lambda value: value + 1")
    args = ",\n".join(arguments)
    source = (
        f"# Graphimo control: {control_type}\n"
        f"# Graphimo instance: {chosen_id}\n"
        f"{result_var} = mo.ui.{control_type if control_type != 'select' else 'dropdown'}(\n{args}\n)"
    )
    spec = ControlSpec(control_type, chosen_id, result_var, normalized)
    return source, spec


def parse_control_source(source: str) -> ControlSpec | None:
    if not isinstance(source, str):
        return None
    lines = source.splitlines()
    control_markers = [line for line in lines if line.startswith("# Graphimo control:")]
    instance_markers = [line for line in lines if line.startswith("# Graphimo instance:")]
    if len(control_markers) != 1 or len(instance_markers) != 1:
        return None
    control_type = control_markers[0].removeprefix("# Graphimo control: ")
    instance_id = instance_markers[0].removeprefix("# Graphimo instance: ")
    if control_type not in _CONTROL_NAMES or not _ID.fullmatch(instance_id):
        return None
    result_var = f"g_{control_type}_{instance_id}"
    try:
        tree = ast.parse(source)
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
            return None
        assignment = tree.body[0]
        if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
            return None
        if assignment.targets[0].id != result_var or not isinstance(assignment.value, ast.Call):
            return None
        call = assignment.value
        expected_name = "dropdown" if control_type == "select" else control_type
        if not (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == expected_name
            and isinstance(call.func.value, ast.Attribute)
            and call.func.value.attr == "ui"
            and isinstance(call.func.value.value, ast.Name)
            and call.func.value.value.id == "mo"
            and not call.args
        ):
            return None
        raw: dict[str, Any] = {}
        has_button_counter = False
        for keyword in call.keywords:
            if keyword.arg is None or keyword.arg in raw:
                return None
            if keyword.arg == "on_click" and control_type == "button":
                callback = keyword.value
                if not (
                    isinstance(callback, ast.Lambda)
                    and len(callback.args.args) == 1
                    and callback.args.args[0].arg == "value"
                    and isinstance(callback.body, ast.BinOp)
                    and isinstance(callback.body.op, ast.Add)
                    and isinstance(callback.body.left, ast.Name)
                    and callback.body.left.id == "value"
                    and isinstance(callback.body.right, ast.Constant)
                    and callback.body.right.value == 1
                    and type(callback.body.right.value) is int
                ):
                    return None
                has_button_counter = True
                continue
            raw[keyword.arg] = ast.literal_eval(keyword.value)
        if control_type == "button" and not has_button_counter:
            return None
        params = _validate_parameters(control_type, raw)
    except (SyntaxError, ValueError, TypeError, RecursionError):
        return None
    if max(lines.index(control_markers[0]), lines.index(instance_markers[0])) >= assignment.lineno - 1:
        return None
    return ControlSpec(control_type, instance_id, result_var, params)


def control_output_type(spec: ControlSpec) -> str:
    if spec.control_type in {"slider", "number"}:
        value = spec.parameters.get("value")
        return "int" if type(value) is int else "float"
    if spec.control_type == "range_slider":
        return "tuple[float, float]"
    if spec.control_type in {"checkbox", "switch"}:
        return "bool"
    if spec.control_type == "text":
        return "str"
    if spec.control_type == "button":
        return "event"
    selected = spec.parameters.get("value")
    return {str: "str", int: "int", float: "float", bool: "bool", type(None): "None"}.get(type(selected), "Any")


def validate_control_value(spec: ControlSpec, value: Any) -> Any:
    kind = spec.control_type
    params = spec.parameters
    if kind in {"slider", "number"}:
        if not _finite_number(value):
            raise ValueError("control value must be a finite number")
        if params.get("start") is not None and value < params["start"]:
            raise ValueError("control value is below its minimum")
        if params.get("stop") is not None and value > params["stop"]:
            raise ValueError("control value is above its maximum")
    elif kind == "range_slider":
        if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(_finite_number(v) for v in value):
            raise ValueError("range slider value must contain two finite numbers")
        if value[0] > value[1]:
            raise ValueError("range slider minimum must not exceed its maximum")
        if params["start"] is not None and value[0] < params["start"]:
            raise ValueError("range slider value is below its minimum")
        if params["stop"] is not None and value[1] > params["stop"]:
            raise ValueError("range slider value is above its maximum")
        return list(value)
    elif kind in {"checkbox", "switch"}:
        if type(value) is not bool:
            raise ValueError("boolean control value must be bool")
    elif kind == "text":
        if not isinstance(value, str):
            raise ValueError("text control value must be text")
    elif kind == "select":
        options = params["options"]
        matching = next((option for option in options if type(option) is type(value) and option == value), None)
        if matching is None and not (value is None and None in options):
            raise ValueError("selected value must exactly match a configured option")
    elif kind == "button":
        if type(value) is not int or value < 0:
            raise ValueError("button trigger value must be a non-negative integer")
    return value


def _validate_parameters(control_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
    common = {"label"}
    control_fields = {
        "slider": {"start", "stop", "step", "value", "debounce"},
        "range_slider": {"start", "stop", "step", "value", "debounce"},
        "number": {"start", "stop", "step", "value", "debounce"},
        "select": {"options", "value", "searchable"},
        "checkbox": {"value"},
        "switch": {"value"},
        "text": {"value", "placeholder", "debounce"},
        "button": {"value"},
    }
    allowed = common | control_fields[control_type]
    if set(parameters).difference(allowed):
        raise ValueError(f"unsupported {control_type} parameter(s): {', '.join(sorted(set(parameters) - allowed))}")
    result = dict(parameters)
    result.setdefault("label", control_type.replace("_", " ").title())
    if not isinstance(result["label"], str):
        raise ValueError("control label must be text")
    if control_type in {"slider", "range_slider", "number"}:
        start = result.get("start")
        stop = result.get("stop")
        step = result.get("step")
        value = result.get("value")
        for name, number in (("start", start), ("stop", stop), ("step", step)):
            if number is not None and not _finite_number(number):
                raise ValueError(f"control {name} must be a finite number")
        if start is not None and stop is not None and start >= stop:
            raise ValueError("control start must be less than stop")
        if step is not None and step <= 0:
            raise ValueError("control step must be positive")
        if control_type == "range_slider":
            if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(_finite_number(v) for v in value):
                raise ValueError("range slider value must contain two finite numbers")
            if value[0] > value[1]:
                raise ValueError("range slider minimum must not exceed its maximum")
            if start is not None and value[0] < start or stop is not None and value[1] > stop:
                raise ValueError("range slider value must be within its bounds")
            result["value"] = list(value)
        elif value is not None and not _finite_number(value):
            raise ValueError("control value must be a finite number")
        if value is not None and control_type != "range_slider":
            if start is not None and value < start or stop is not None and value > stop:
                raise ValueError("control value must be within its bounds")
    elif control_type in {"checkbox", "switch"}:
        if type(result.get("value", False)) is not bool:
            raise ValueError("boolean control value must be bool")
    elif control_type == "text":
        if not isinstance(result.get("value", ""), str) or not isinstance(result.get("placeholder", ""), str):
            raise ValueError("text control value and placeholder must be text")
    elif control_type == "select":
        options = result.get("options")
        if not isinstance(options, list) or not options or not all(_json_scalar(item) for item in options):
            raise ValueError("select options must be a non-empty list of JSON scalar values")
        selected = result.get("value", options[0])
        if not any(type(item) is type(selected) and item == selected for item in options):
            raise ValueError("select value must match one option and its scalar type")
        result["value"] = selected
    elif control_type == "button":
        initial = result.get("value", 0)
        if type(initial) is not int or initial < 0:
            raise ValueError("button initial value must be a non-negative integer")
    # JSON serialization is also the safety boundary for source literals.
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("control parameters must be finite JSON values") from exc
    if "debounce" in result and type(result["debounce"]) is not bool:
        raise ValueError("control debounce must be bool")
    if "searchable" in result and type(result["searchable"]) is not bool:
        raise ValueError("control searchable must be bool")
    return result


def _finite_number(value: Any) -> bool:
    return type(value) is int or type(value) is float and math.isfinite(value)


def _json_scalar(value: Any) -> bool:
    return value is None or type(value) in (str, bool, int) or type(value) is float and math.isfinite(value)


def _python_literal(value: Any) -> str:
    json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return repr(value)
