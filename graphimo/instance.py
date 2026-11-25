"""Canonical source generation for Graphimo-owned node instances."""

from __future__ import annotations

import ast
import keyword
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

_INSTANCE_ID = re.compile(r"^[0-9a-f]{8}$")
_MAX_EXPRESSION_CHARS = 10_000


@dataclass(frozen=True)
class Binding:
    input_name: str
    expression: str


@dataclass(frozen=True)
class InstanceSpec:
    instance_id: str
    node_name: str
    result_var: str
    bindings: tuple[Binding, ...]


def new_instance_id() -> str:
    """Create a random, lowercase eight-character instance ID."""
    return secrets.token_hex(4)


def _validate_instance_id(instance_id: str) -> None:
    if not isinstance(instance_id, str) or _INSTANCE_ID.fullmatch(instance_id) is None:
        raise ValueError("instance_id must be exactly eight lowercase hexadecimal characters")


def _validate_node_name(node_name: str) -> None:
    if (
        not isinstance(node_name, str)
        or not node_name.isidentifier()
        or keyword.iskeyword(node_name)
    ):
        raise ValueError("node_name must be a valid Python function identifier")


def _validate_expression(expression: str) -> None:
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("binding expression must be a non-empty Python expression")
    if len(expression) > _MAX_EXPRESSION_CHARS:
        raise ValueError(f"binding expression exceeds {_MAX_EXPRESSION_CHARS} characters")
    try:
        ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise ValueError("binding expression is not valid Python") from error


def _sanitize_name(node_name: str) -> str:
    fragment = re.sub(r"\W", "_", node_name, flags=re.UNICODE).strip("_") or "node"
    if fragment[0].isdigit():
        fragment = f"node_{fragment}"
    return fragment


def result_variable(node_name: str, instance_id: str) -> str:
    """Return the canonical result global for a node and instance ID."""
    _validate_instance_id(instance_id)
    fragment = _sanitize_name(node_name)
    return f"g_{fragment}_{instance_id}"


def build_instance_source(
    node_name: str,
    bindings: Mapping[str, str],
    *,
    instance_id: str | None = None,
) -> tuple[str, InstanceSpec]:
    """Build the canonical marimo cell body for one `@node` call."""
    _validate_node_name(node_name)
    chosen_id = new_instance_id() if instance_id is None else instance_id
    _validate_instance_id(chosen_id)
    if not isinstance(bindings, Mapping):
        raise TypeError("bindings must be a mapping of input names to Python expressions")

    normalized: list[Binding] = []
    for input_name, expression in bindings.items():
        if (
            not isinstance(input_name, str)
            or not input_name.isidentifier()
            or keyword.iskeyword(input_name)
        ):
            raise ValueError(f"invalid Python input name: {input_name!r}")
        _validate_expression(expression)
        normalized.append(Binding(input_name, expression.strip()))

    result_name = result_variable(node_name, chosen_id)
    if normalized:
        arguments = "\n".join(f"    {item.input_name}={item.expression}," for item in normalized)
        call = f"{result_name} = {node_name}(\n{arguments}\n)"
    else:
        call = f"{result_name} = {node_name}()"
    source = f"# Graphimo node: {node_name}\n# Graphimo instance: {chosen_id}\n{call}"
    return source, InstanceSpec(chosen_id, node_name, result_name, tuple(normalized))


def parse_instance_source(source: str) -> InstanceSpec | None:
    """Recognize only a single, marked, direct node-call assignment."""
    if not isinstance(source, str):
        return None
    lines = source.splitlines()
    node_lines = [line for line in lines if line.startswith("# Graphimo node:")]
    instance_lines = [line for line in lines if line.startswith("# Graphimo instance:")]
    if len(node_lines) != 1 or len(instance_lines) != 1:
        return None
    node_marker = node_lines[0]
    instance_marker = instance_lines[0]
    node_name = node_marker.removeprefix("# Graphimo node: ")
    instance_id = instance_marker.removeprefix("# Graphimo instance: ")
    if (
        node_marker != f"# Graphimo node: {node_name}"
        or instance_marker != f"# Graphimo instance: {instance_id}"
    ):
        return None
    if lines.index(node_marker) >= lines.index(instance_marker):
        return None
    try:
        _validate_node_name(node_name)
        _validate_instance_id(instance_id)
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
        return None
    assignment = tree.body[0]
    if assignment.lineno <= lines.index(instance_marker) + 1:
        return None
    if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
        return None
    expected_result = result_variable(node_name, instance_id)
    if assignment.targets[0].id != expected_result or not isinstance(assignment.value, ast.Call):
        return None
    call = assignment.value
    if not isinstance(call.func, ast.Name) or call.func.id != node_name or call.args:
        return None
    if any(argument.arg is None for argument in call.keywords):
        return None
    names = [argument.arg for argument in call.keywords]
    if len(names) != len(set(names)):
        return None
    bindings: list[Binding] = []
    for argument in call.keywords:
        assert argument.arg is not None
        expression = ast.get_source_segment(source, argument.value)
        if expression is None:
            expression = ast.unparse(argument.value)
        try:
            _validate_expression(expression)
        except ValueError:
            return None
        bindings.append(Binding(argument.arg, expression))
    return InstanceSpec(instance_id, node_name, expected_result, tuple(bindings))


def reconnect_instance_source(source: str, *, input_name: str, expression: str) -> str:
    """Replace exactly one binding on a valid Graphimo-owned instance source."""
    spec = parse_instance_source(source)
    if spec is None:
        raise ValueError("source is not a canonical Graphimo node instance")
    if (
        not isinstance(input_name, str)
        or not input_name.isidentifier()
        or keyword.iskeyword(input_name)
    ):
        raise ValueError("input_name must be a valid Python identifier")
    _validate_expression(expression)
    found = False
    updated: dict[str, str] = {}
    for binding in spec.bindings:
        if binding.input_name == input_name:
            updated[binding.input_name] = expression.strip()
            found = True
        else:
            updated[binding.input_name] = binding.expression
    if not found:
        raise ValueError(f"instance has no input named {input_name!r}")
    regenerated, _ = build_instance_source(spec.node_name, updated, instance_id=spec.instance_id)
    return regenerated


def output_expression(instance: InstanceSpec, output_index: int, output_count: int) -> str:
    """Return the Python expression used to wire one node result port."""
    if type(output_index) is not int or type(output_count) is not int or output_count < 1:
        raise ValueError("output_index and positive output_count must be integers")
    if output_index < 0 or output_index >= output_count:
        raise IndexError("output index is outside the node output range")
    if output_count == 1:
        return instance.result_var
    return f"{instance.result_var}[{output_index}]"
