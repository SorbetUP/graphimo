from __future__ import annotations

import ast
import inspect
import textwrap
import typing
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InputPort:
    name: str
    required: bool
    annotation: str | None
    has_default: bool
    default: object | None


@dataclass(frozen=True)
class OutputPort:
    index: int
    declared_type: str | None
    expressions: tuple[str, ...]


@dataclass(frozen=True)
class NodeSpec:
    name: str
    inputs: tuple[InputPort, ...]
    outputs: tuple[OutputPort, ...]
    diagnostic: str | None


def node(fn):
    """Mark a normal Python callable as a Graphimo node without wrapping it."""
    if not callable(fn):
        raise TypeError("@node can only decorate a callable")
    setattr(fn, "__graphimo_node__", True)
    return fn


def is_node(value: Any) -> bool:
    """Return whether ``value`` carries the explicit Graphimo node marker."""
    try:
        return getattr(value, "__graphimo_node__", False) is True
    except Exception:
        return False


def _type_name(value: type) -> str | None:
    try:
        name = type.__getattribute__(value, "__name__")
    except (AttributeError, TypeError):
        return None
    return name if isinstance(name, str) else None


def _safe_annotation_text(annotation: Any) -> str | None:
    """Format common annotation objects without evaluating forward references."""
    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, str):
        return annotation
    if annotation is None:
        return "None"
    if isinstance(annotation, type):
        return _type_name(annotation)

    tuple_items = _tuple_annotation_items(annotation)
    if tuple_items is not None:
        origin = typing.get_origin(annotation)
        origin_name = _type_name(origin) if isinstance(origin, type) else None
        if origin_name == "tuple":
            return "tuple[" + ", ".join(item or "?" for item in tuple_items) + "]"
    return None


def _tuple_annotation_items(annotation: Any) -> list[str | None] | None:
    if isinstance(annotation, str):
        try:
            expression = ast.parse(annotation, mode="eval").body
        except SyntaxError:
            return None
        if not isinstance(expression, ast.Subscript):
            return None
        origin = expression.value
        if isinstance(origin, ast.Name) and origin.id in {"tuple", "Tuple"}:
            pass
        elif (
            isinstance(origin, ast.Attribute)
            and isinstance(origin.value, ast.Name)
            and origin.value.id == "typing"
            and origin.attr == "Tuple"
        ):
            pass
        else:
            return None
        values = expression.slice.elts if isinstance(expression.slice, ast.Tuple) else [expression.slice]
        if not values or any(isinstance(item, ast.Constant) and item.value is Ellipsis for item in values):
            return None
        return [ast.unparse(item) for item in values]

    if typing.get_origin(annotation) is not tuple:
        return None
    values = typing.get_args(annotation)
    if not values or any(item is Ellipsis for item in values):
        return None
    return [_safe_annotation_text(item) for item in values]


def _function_source(fn: Any) -> tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
        name = getattr(fn, "__name__", None)
    except (OSError, TypeError, ValueError, SyntaxError, AttributeError) as exc:
        raise ValueError(f"could not inspect Graphimo node source: {exc}") from exc
    function = next(
        (
            item
            for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
        ),
        None,
    )
    if function is None:
        raise ValueError("could not find the decorated function in its source")
    return source, function


class _ReturnVisitor(ast.NodeVisitor):
    def __init__(self, root: ast.FunctionDef | ast.AsyncFunctionDef):
        self.root = root
        self.returns: list[ast.Return] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Return(self, node: ast.Return) -> None:
        self.returns.append(node)


def _return_items(value: ast.expr | None) -> tuple[ast.expr, ...]:
    if value is None:
        return ()
    if isinstance(value, ast.Tuple):
        return tuple(value.elts)
    return (value,)


def _expression(source: str, expression: ast.expr) -> str:
    result = ast.get_source_segment(source, expression)
    return result if result is not None else ast.unparse(expression)


def inspect_node(fn: Any) -> NodeSpec:
    """Inspect a decorated function's signature and stable return-port contract."""
    if not is_node(fn):
        raise ValueError("callable is not decorated with @node")
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"could not inspect Graphimo node signature: {exc}") from exc

    inputs = tuple(
        InputPort(
            name=parameter.name,
            required=parameter.default is inspect.Parameter.empty,
            annotation=_safe_annotation_text(parameter.annotation),
            has_default=parameter.default is not inspect.Parameter.empty,
            default=None if parameter.default is inspect.Parameter.empty else parameter.default,
        )
        for parameter in signature.parameters.values()
    )
    source, function = _function_source(fn)
    visitor = _ReturnVisitor(function)
    visitor.visit(function)
    paths = [_return_items(statement.value) for statement in visitor.returns]
    arities = {len(items) for items in paths}
    if len(arities) > 1:
        return NodeSpec(function.name, inputs, (), "node_output_arity_inconsistent")
    output_count = next(iter(arities), 0)
    if output_count == 0:
        return NodeSpec(function.name, inputs, (), None)

    annotation_items = _tuple_annotation_items(signature.return_annotation)
    diagnostic = None
    if output_count > 1 and annotation_items is not None and len(annotation_items) != output_count:
        diagnostic = "node_return_annotation_arity_mismatch"
        declared_types: list[str | None] = [None] * output_count
    elif output_count > 1 and annotation_items is not None:
        declared_types = annotation_items
    else:
        declared = _safe_annotation_text(signature.return_annotation)
        declared_types = [declared] * output_count

    outputs = tuple(
        OutputPort(
            index=index,
            declared_type=declared_types[index],
            expressions=tuple(
                dict.fromkeys(_expression(source, items[index]) for items in paths)
            ),
        )
        for index in range(output_count)
    )
    return NodeSpec(function.name, inputs, outputs, diagnostic)
