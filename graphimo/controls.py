"""Validated source generation for marimo-native Graphimo controls."""

from __future__ import annotations

import keyword
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,95}$")
_DEFAULTS: dict[str, dict[str, Any]] = {
    "slider": {
        "start": 0.0,
        "stop": 1.0,
        "step": 0.01,
        "value": 0.2,
        "label": "threshold",
        "show_value": True,
    },
    "range_slider": {
        "start": 0.0,
        "stop": 1.0,
        "step": 0.01,
        "value": (0.25, 0.75),
        "label": "range",
        "show_value": True,
    },
    "number": {"value": 0.0, "label": "value"},
    "dropdown": {"options": ("option_a", "option_b"), "value": "option_a", "label": "choice"},
    "checkbox": {"value": False, "label": "enabled"},
    "text": {"value": "", "label": "text"},
    "run_button": {"label": "Run"},
}
_ALLOWED_PARAMETERS = {
    "slider": {"start", "stop", "step", "value", "label", "show_value"},
    "range_slider": {"start", "stop", "step", "value", "label", "show_value"},
    "number": {"start", "stop", "step", "value", "label"},
    "dropdown": {"options", "value", "label"},
    "checkbox": {"value", "label"},
    "text": {"value", "label"},
    "run_button": {"label"},
}


def control_catalog() -> dict[str, Any]:
    """Return the controls supported by the marimo renderer contract."""
    return {
        "items": [
            {"kind": kind, "label": str(defaults["label"]), "dialect_id": "python"}
            for kind, defaults in _DEFAULTS.items()
        ]
    }


def create_control_source(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a control request and return its two ordinary marimo cells."""
    if not isinstance(request, Mapping):
        raise ValueError("control request must be an object")
    kind = request.get("kind")
    if not isinstance(kind, str) or kind not in _DEFAULTS:
        raise ValueError(f"unsupported Graphimo control kind: {kind!r}")
    identity = request.get("identity")
    if (
        not isinstance(identity, str)
        or not _IDENTIFIER.fullmatch(identity)
        or keyword.iskeyword(identity)
    ):
        raise ValueError("control identity must be a safe Python identifier")

    values = dict(_DEFAULTS[kind])
    parameters = request.get("parameters", {})
    if not isinstance(parameters, Mapping):
        raise ValueError("control parameters must be an object")
    if any(not isinstance(name, str) for name in parameters):
        raise ValueError("control parameter names must be strings")
    unexpected = set(parameters).difference(_ALLOWED_PARAMETERS[kind])
    if unexpected:
        raise ValueError(f"unsupported {kind} parameters: {', '.join(sorted(unexpected))}")
    values.update(parameters)
    if kind in {"slider", "range_slider"} and type(values.get("show_value")) is not bool:
        raise ValueError("show_value must be boolean")
    label = values.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > 160:
        raise ValueError("control label must contain 1-160 characters")
    values["label"] = label.strip()

    if kind in {"slider", "range_slider", "number"}:
        for name in ("start", "stop", "step", "value"):
            if name not in values:
                continue
            value = values[name]
            if kind == "number" and name in {"start", "stop", "step"} and value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value):
                if name == "value" and kind == "range_slider":
                    continue
                raise ValueError(f"control parameter {name!r} must be a finite number")
        if kind in {"slider", "range_slider"}:
            start, stop, step = values["start"], values["stop"], values["step"]
            if not start < stop or step <= 0:
                raise ValueError("slider requires start < stop and a positive step")
        if kind == "slider" and not values["start"] <= values["value"] <= values["stop"]:
            raise ValueError("control value must be inside its bounds")
        if kind == "range_slider":
            value = values["value"]
            if (
                isinstance(value, (str, bytes))
                or not isinstance(value, Sequence)
                or len(value) != 2
                or any(type(item) not in (int, float) or not math.isfinite(item) for item in value)
                or not start <= value[0] <= value[1] <= stop
            ):
                raise ValueError("range slider value must be an ordered pair inside its bounds")
        elif kind == "number" and (
            (values.get("start") is None) != (values.get("stop") is None)
            or (
                values.get("start") is not None
                and values.get("stop") is not None
                and values["start"] >= values["stop"]
            )
            or (values.get("step") is not None and values["step"] <= 0)
        ):
            raise ValueError("number input bounds must be paired and ordered with a positive step")
        if (
            kind == "number"
            and values.get("start") is not None
            and not values["start"] <= values["value"] <= values["stop"]
        ):
            raise ValueError("control value must be inside its bounds")
    elif kind == "dropdown":
        options = values.get("options")
        if (
            isinstance(options, (str, bytes))
            or not isinstance(options, Sequence)
            or not 1 <= len(options) <= 128
            or any(
                type(item) not in (str, int, float, bool)
                or (type(item) is float and not math.isfinite(item))
                for item in options
            )
        ):
            raise ValueError("dropdown options must be 1-128 JSON scalar choices")
        if values.get("value") not in options:
            raise ValueError("dropdown value must be one of its choices")
        values["options"] = tuple(options)
    elif kind == "checkbox" and type(values.get("value")) is not bool:
        raise ValueError("checkbox value must be boolean")
    elif kind == "text":
        if not isinstance(values.get("value"), str) or len(values["value"]) > 16_384:
            raise ValueError("text control value must be text no longer than 16384 characters")

    widget_name = f"g_{identity}"
    module_name = f"g_marimo_{identity}"
    value_name = f"{widget_name}_value"
    arguments = _source_arguments(kind, values)
    widget_source = "\n".join(
        (
            f"# Graphimo control: {kind}",
            f"# Graphimo instance: {identity}",
            f"import marimo as {module_name}",
            f"{widget_name} = {module_name}.ui.{kind}({arguments})",
            widget_name,
        )
    )
    value_source = "\n".join(
        (
            f"# Graphimo control value: {identity}",
            f"{value_name} = {widget_name}.value",
            value_name,
        )
    )
    return {
        "kind": kind,
        "identity": identity,
        "label": values["label"],
        "control_name": widget_name,
        "value_name": value_name,
        "control_source": widget_source,
        "value_source": value_source,
        "dialect_id": "python",
    }


def _source_arguments(kind: str, values: Mapping[str, Any]) -> str:
    ordered = {
        "slider": ("start", "stop", "step", "value", "label", "show_value"),
        "range_slider": ("start", "stop", "step", "value", "label", "show_value"),
        "number": ("start", "stop", "step", "value", "label"),
        "dropdown": ("options", "value", "label"),
        "checkbox": ("value", "label"),
        "text": ("value", "label"),
        "run_button": ("label",),
    }[kind]
    if kind in {"slider", "range_slider"} and "show_value" not in values:
        values = {**values, "show_value": True}
    return ", ".join(f"{name}={values[name]!r}" for name in ordered if name in values)
