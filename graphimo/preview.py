from __future__ import annotations

import math
from typing import Any

from .node import NodeSpec


def _safe_type_name(value: Any) -> str:
    value_type = type(value)
    try:
        name = type.__getattribute__(value_type, "__name__")
    except (AttributeError, TypeError):
        return "object"
    return name if isinstance(name, str) else "object"


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return "." * max_chars
    return text[: max_chars - 3] + "..."


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return f"<{_safe_type_name(value)} repr unavailable>"


def _sample_repr(value: Any, max_items: int, depth: int, active: set[int]) -> str:
    if depth >= 3:
        return f"<{_safe_type_name(value)}>"
    if isinstance(value, str):
        return _safe_repr(value[:40] + ("..." if len(value) > 40 else ""))
    if value is None or type(value) in (bool, int, float):
        return _safe_repr(value)
    if isinstance(value, (list, tuple, dict)):
        identity = id(value)
        if identity in active:
            return "..."
        active.add(identity)
        try:
            if isinstance(value, dict):
                pieces = [
                    f"{_sample_repr(key, max_items, depth + 1, active)}: "
                    f"{_sample_repr(item, max_items, depth + 1, active)}"
                    for key, item in list(value.items())[:max_items]
                ]
                suffix = ", ..." if len(value) > max_items else ""
                return "{" + ", ".join(pieces) + suffix + "}"
            pieces = [_sample_repr(item, max_items, depth + 1, active) for item in value[:max_items]]
            suffix = ", ..." if len(value) > max_items else ""
            opening, closing = ("[", "]") if isinstance(value, list) else ("(", ")")
            return opening + ", ".join(pieces) + suffix + closing
        finally:
            active.remove(identity)
    return _safe_repr(value)


def _attribute_text(value: Any, name: str) -> str | None:
    try:
        result = getattr(value, name)
    except Exception:
        return None
    if result is None:
        return None
    if name == "shape":
        try:
            return repr(tuple(result))
        except Exception:
            return _safe_repr(result)
    return _safe_repr(result)


def _preview_details(value: Any, max_items: int) -> tuple[str, str]:
    runtime_type = _safe_type_name(value)
    value_type = type(value)
    module = getattr(value_type, "__module__", "")

    if value is None or type(value) in (bool, int, float):
        if type(value) is float and not math.isfinite(value):
            return runtime_type, _safe_repr(value)
        return runtime_type, _safe_repr(value)
    if isinstance(value, str):
        return runtime_type, value
    if isinstance(value, (list, tuple, dict)):
        return runtime_type, f"len={len(value)} {_sample_repr(value, max_items, 0, set())}"

    if module == "torch" and runtime_type == "Tensor":
        fields = []
        for key in ("shape", "dtype", "device"):
            text = _attribute_text(value, key)
            if text is not None:
                fields.append(f"{key}={text}")
        return runtime_type, ", ".join(fields) if fields else "Tensor"

    if module == "numpy" and runtime_type == "ndarray":
        fields = []
        for key in ("shape", "dtype"):
            text = _attribute_text(value, key)
            if text is not None:
                fields.append(f"{key}={text}")
        return runtime_type, ", ".join(fields) if fields else "ndarray"

    if module.startswith("pandas") and runtime_type == "DataFrame":
        shape = _attribute_text(value, "shape")
        if shape is not None:
            try:
                rows, columns = tuple(value.shape)
                return runtime_type, f"{rows} rows x {columns} cols"
            except Exception:
                return runtime_type, f"shape={shape}"

    return runtime_type, _safe_repr(value)


def preview_value(value: Any, *, max_chars: int = 80, max_items: int = 6) -> dict[str, str]:
    """Return a bounded, JSON-safe runtime type and preview without serialization."""
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    if type(max_items) is not int or max_items < 1:
        raise ValueError("max_items must be a positive integer")
    runtime_type, preview = _preview_details(value, max_items)
    return {"runtime_type": runtime_type, "preview": _truncate(preview, max_chars)}


def describe_result(value: Any, spec: NodeSpec) -> list[dict[str, Any]]:
    """Describe declared outputs with bounded runtime previews."""
    if spec.diagnostic is not None:
        raise ValueError(spec.diagnostic)
    output_count = len(spec.outputs)
    if output_count == 0:
        return []
    if output_count == 1:
        values = (value,)
    elif not isinstance(value, tuple) or len(value) != output_count:
        raise ValueError("node_output_arity_mismatch")
    else:
        values = value

    result = []
    for port, item in zip(spec.outputs, values, strict=True):
        preview = preview_value(item)
        result.append(
            {
                "index": port.index,
                "declared_type": port.declared_type,
                **preview,
                "expressions": list(port.expressions),
                "active_expression": None,
            }
        )
    return result
