"""Bounded, JSON-safe runtime previews for Graphimo node results."""

from __future__ import annotations

from typing import Any

from .node import NodeSpec


def _safe_repr(value: object, max_chars: int) -> str:
    try:
        rendered = repr(value)
    except Exception:  # noqa: BLE001 - user object repr must not break inspection
        rendered = f"<{type(value).__name__} repr unavailable>"
    if len(rendered) > max_chars:
        return rendered[: max(0, max_chars - 1)] + "…"
    return rendered


def _runtime_type(value: object) -> str:
    try:
        return type.__getattribute__(type(value), "__name__")
    except (AttributeError, TypeError):
        return "object"


def _safe_attr(value: object, name: str) -> object | None:
    try:
        return getattr(value, name)
    except Exception:  # noqa: BLE001 - optional framework objects may expose failing properties
        return None


def _shape_text(shape: object) -> str | None:
    if isinstance(shape, (tuple, list)) and all(type(item) is int for item in shape):
        return str(tuple(shape))
    try:
        dims = tuple(int(item) for item in shape)  # supports tensor.Size without importing torch
    except (TypeError, ValueError):
        return None
    return str(dims)


def preview_value(value: Any, *, max_chars: int = 80, max_items: int = 6) -> dict[str, str]:
    """Return a bounded preview without invoking arbitrary serialization hooks."""
    if max_chars < 1 or max_items < 0:
        raise ValueError("max_chars must be positive and max_items must be non-negative")

    runtime_type = _runtime_type(value)
    cls = type(value)
    module = getattr(cls, "__module__", "")

    if isinstance(value, str):
        preview = _safe_repr(value, max_chars)
    elif isinstance(value, (list, tuple)):
        sample = ", ".join(
            _safe_repr(item, max(8, max_chars // max(1, max_items))) for item in value[:max_items]
        )
        if len(value) > max_items:
            sample = f"{sample}, …" if sample else "…"
        opening, closing = ("[", "]") if isinstance(value, list) else ("(", ")")
        preview = _safe_repr(f"{opening}{sample}{closing} (len={len(value)})", max_chars)
    elif isinstance(value, dict):
        items = list(value.items())[:max_items]
        sample = ", ".join(
            f"{_safe_repr(key, 16)}: {_safe_repr(item, max(8, max_chars // max(1, max_items)))}"
            for key, item in items
        )
        if len(value) > max_items:
            sample = f"{sample}, …" if sample else "…"
        preview = _safe_repr(f"{{{sample}}} (len={len(value)})", max_chars)
    elif module == "torch" and runtime_type == "Tensor":
        shape = _shape_text(_safe_attr(value, "shape"))
        dtype = _safe_attr(value, "dtype")
        device = _safe_attr(value, "device")
        fields = [f"shape={shape}" if shape is not None else None]
        fields += [f"dtype={dtype}" if dtype is not None else None]
        fields += [f"device={device}" if device is not None else None]
        preview = _safe_repr(", ".join(field for field in fields if field) or "Tensor", max_chars)
    elif module == "numpy" and runtime_type == "ndarray":
        shape = _shape_text(_safe_attr(value, "shape"))
        dtype = _safe_attr(value, "dtype")
        fields = [f"shape={shape}" if shape is not None else None]
        fields += [f"dtype={dtype}" if dtype is not None else None]
        preview = _safe_repr(", ".join(field for field in fields if field) or "ndarray", max_chars)
    elif module.startswith("pandas") and runtime_type == "DataFrame":
        shape = _safe_attr(value, "shape")
        if isinstance(shape, (tuple, list)) and len(shape) == 2:
            preview = f"rows={shape[0]}, columns={shape[1]}"
        else:
            preview = "DataFrame"
        preview = _safe_repr(preview, max_chars)
    elif value is None or type(value) in (bool, int, float, complex, bytes):
        preview = _safe_repr(value, max_chars)
    else:
        preview = _safe_repr(f"<{runtime_type}> {_safe_repr(value, max_chars)}", max_chars)
    return {"runtime_type": runtime_type, "preview": preview}


def describe_result(value: Any, spec: NodeSpec) -> list[dict[str, Any]]:
    """Preview the runtime value against the statically described node outputs."""
    if spec.diagnostic is not None:
        raise ValueError(spec.diagnostic)
    outputs = spec.outputs
    if not outputs:
        return []
    if len(outputs) == 1:
        values = (value,)
    else:
        if not isinstance(value, tuple) or len(value) != len(outputs):
            actual = len(value) if isinstance(value, tuple) else 1
            raise ValueError(
                f"node_output_arity_mismatch: expected {len(outputs)} outputs, got {actual}"
            )
        values = value
    result: list[dict[str, Any]] = []
    for port, item in zip(outputs, values, strict=True):
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
