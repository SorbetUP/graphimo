"""Static contracts for reusable Graphimo Python nodes."""

from __future__ import annotations

import ast
import inspect
import math
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin


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


def node(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a function as an executable Graphimo node without wrapping it."""
    if not callable(fn):
        raise TypeError("@node can only decorate a callable")
    fn.__graphimo_node__ = True
    return fn


def is_node(value: object) -> bool:
    """Return whether *value* carries the Graphimo node marker."""
    return getattr(value, "__graphimo_node__", False) is True


def _safe_annotation(annotation: object) -> str | None:
    if annotation is inspect.Parameter.empty:
        return None
    if annotation is None:
        return "None"
    if isinstance(annotation, str):
        return annotation
    if isinstance(annotation, type):
        return type.__getattribute__(annotation, "__name__")
    if get_origin(annotation) is Literal:
        choices = get_args(annotation)
        if all(
            choice is None
            or type(choice) in (bool, int, str)
            or type(choice) is float
            and math.isfinite(choice)
            for choice in choices
        ):
            return "Literal[" + ", ".join(repr(choice) for choice in choices) + "]"
    return None


def _expr(source: str, expression: ast.expr) -> str:
    value = ast.get_source_segment(source, expression)
    return value if value is not None else ast.unparse(expression)


class _ReturnCollector(ast.NodeVisitor):
    def __init__(self, root: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.root = root
        self.returns: list[ast.Return] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            for statement in node.body:
                self.visit(statement)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self.root:
            for statement in node.body:
                self.visit(statement)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Return(self, node: ast.Return) -> None:
        self.returns.append(node)


def _return_annotation_items(annotation: object) -> list[str | None] | None:
    if isinstance(annotation, str):
        annotation_text = annotation
    else:
        origin = get_origin(annotation)
        if origin is not tuple:
            return None
        arguments = get_args(annotation)
        if not arguments or any(argument is Ellipsis for argument in arguments):
            return None
        return [_safe_annotation(argument) for argument in arguments]
    if annotation_text is None:
        return None
    try:
        expr = ast.parse(annotation_text, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(expr, ast.Subscript):
        return None
    value = expr.value
    is_tuple = (isinstance(value, ast.Name) and value.id in {"tuple", "Tuple"}) or (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "typing"
        and value.attr == "Tuple"
    )
    if not is_tuple:
        return None
    values = expr.slice.elts if isinstance(expr.slice, ast.Tuple) else [expr.slice]
    if not values or any(
        isinstance(value, ast.Constant) and value.value is Ellipsis for value in values
    ):
        return None
    return [ast.unparse(value) for value in values]


def inspect_node(fn: Callable[..., Any]) -> NodeSpec:
    """Describe a decorated function's ordered input/output port contract."""
    if not is_node(fn):
        raise ValueError("inspect_node requires a function decorated with @node")
    signature = inspect.signature(fn)
    inputs = tuple(
        InputPort(
            name=parameter.name,
            required=parameter.default is inspect.Parameter.empty,
            annotation=_safe_annotation(parameter.annotation),
            has_default=parameter.default is not inspect.Parameter.empty,
            default=(None if parameter.default is inspect.Parameter.empty else parameter.default),
        )
        for parameter in signature.parameters.values()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    )
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        module = ast.parse(source)
    except (OSError, TypeError, ValueError, SyntaxError):
        return NodeSpec(fn.__name__, inputs, (), "node_source_unavailable")
    root = next(
        (
            item
            for item in module.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == fn.__name__
        ),
        None,
    )
    if root is None:
        return NodeSpec(fn.__name__, inputs, (), "node_source_unavailable")

    collector = _ReturnCollector(root)
    collector.visit(root)
    returns = collector.returns
    # A function without an explicit return has the same zero-output contract as return None.
    if not returns:
        return NodeSpec(fn.__name__, inputs, (), None)
    rows: list[tuple[ast.expr, ...]] = []
    for statement in returns:
        value = statement.value
        if value is None or isinstance(value, ast.Constant) and value.value is None:
            rows.append(())
        elif isinstance(value, ast.Tuple):
            rows.append(tuple(value.elts))
        else:
            rows.append((value,))
    arities = {len(row) for row in rows}
    if len(arities) != 1:
        return NodeSpec(fn.__name__, inputs, (), "node_output_arity_inconsistent")
    arity = arities.pop()
    if arity == 0:
        return NodeSpec(fn.__name__, inputs, (), None)

    annotation = _safe_annotation(signature.return_annotation)
    tuple_types = _return_annotation_items(signature.return_annotation)
    if tuple_types is not None and len(tuple_types) != arity:
        return NodeSpec(fn.__name__, inputs, (), "node_return_annotation_arity_mismatch")
    output_types = tuple_types if tuple_types is not None else [annotation] * arity
    outputs: list[OutputPort] = []
    for index in range(arity):
        expressions = tuple(dict.fromkeys(_expr(source, row[index]) for row in rows))
        outputs.append(OutputPort(index, output_types[index], expressions))
    return NodeSpec(fn.__name__, inputs, tuple(outputs), None)
