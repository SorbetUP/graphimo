from __future__ import annotations

import ast
import keyword
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from .node import NodeSpec

_INSTANCE_ID = re.compile(r"^[0-9a-f]{8}$")


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
    """Return a cryptographically random eight-character lowercase hex ID."""
    return secrets.token_hex(4)


def _name_fragment(node_name: str) -> str:
    if not isinstance(node_name, str) or not node_name:
        raise ValueError("node name must be a non-empty string")
    fragment = re.sub(r"\W", "_", node_name, flags=re.UNICODE)
    if not fragment or fragment[0].isdigit():
        fragment = f"node_{fragment}"
    if keyword.iskeyword(fragment):
        fragment += "_"
    if not fragment.isidentifier():
        raise ValueError("node name cannot be converted to a Python identifier")
    return fragment


def result_variable(node_name: str, instance_id: str) -> str:
    if not isinstance(instance_id, str) or _INSTANCE_ID.fullmatch(instance_id) is None:
        raise ValueError("instance ID must be exactly eight lowercase hexadecimal characters")
    return f"g_{_name_fragment(node_name)}_{instance_id}"


def _parse_expression(expression: str) -> ast.expr:
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("binding expression must be a non-empty Python expression")
    try:
        return ast.parse(expression, mode="eval").body
    except SyntaxError as exc:
        raise ValueError("binding expression is not valid Python") from exc


def _ordered_bindings(
    bindings: Mapping[str, str], node_spec: NodeSpec | None
) -> tuple[Binding, ...]:
    if not isinstance(bindings, Mapping):
        raise TypeError("bindings must be a mapping of input names to expressions")
    names = list(bindings)
    if any(not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name) for name in names):
        raise ValueError("binding names must be valid Python identifiers")
    if node_spec is not None:
        input_order = [item.name for item in node_spec.inputs]
        unknown = set(names).difference(input_order)
        if unknown:
            raise ValueError(f"unknown node input(s): {', '.join(sorted(unknown))}")
        names = [name for name in input_order if name in bindings]
    result = []
    for name in names:
        expression = bindings[name]
        _parse_expression(expression)
        result.append(Binding(input_name=name, expression=expression.strip()))
    return tuple(result)


def build_instance_source(
    node_name: str,
    bindings: Mapping[str, str],
    *,
    instance_id: str | None = None,
    node_spec: NodeSpec | None = None,
) -> tuple[str, InstanceSpec]:
    if not isinstance(node_name, str) or not node_name.isidentifier() or keyword.iskeyword(node_name):
        raise ValueError("node name must be a valid Python identifier")
    chosen_id = new_instance_id() if instance_id is None else instance_id
    result_var = result_variable(node_name, chosen_id)
    ordered = _ordered_bindings(bindings, node_spec)
    arguments = "\n".join(f"    {binding.input_name}={binding.expression}," for binding in ordered)
    call = f"{result_var} = {node_name}(\n{arguments}\n)" if arguments else f"{result_var} = {node_name}()"
    source = f"# Graphimo node: {node_name}\n# Graphimo instance: {chosen_id}\n{call}"
    return source, InstanceSpec(chosen_id, node_name, result_var, ordered)


def parse_instance_source(source: str) -> InstanceSpec | None:
    if not isinstance(source, str):
        return None
    lines = source.splitlines()
    node_markers = [line for line in lines if line.startswith("# Graphimo node:")]
    instance_markers = [line for line in lines if line.startswith("# Graphimo instance:")]
    if len(node_markers) != 1 or len(instance_markers) != 1:
        return None
    node_prefix = "# Graphimo node: "
    instance_prefix = "# Graphimo instance: "
    if not node_markers[0].startswith(node_prefix) or not instance_markers[0].startswith(instance_prefix):
        return None
    node_name = node_markers[0][len(node_prefix) :]
    instance_id = instance_markers[0][len(instance_prefix) :]
    if not node_name.isidentifier() or keyword.iskeyword(node_name):
        return None
    try:
        result_var = result_variable(node_name, instance_id)
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
        return None
    statement = tree.body[0]
    if max(lines.index(node_markers[0]), lines.index(instance_markers[0])) >= statement.lineno - 1:
        return None
    if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
        return None
    if statement.targets[0].id != result_var or not isinstance(statement.value, ast.Call):
        return None
    call = statement.value
    if not isinstance(call.func, ast.Name) or call.func.id != node_name or call.args:
        return None
    bindings = []
    seen_names = set()
    for argument in call.keywords:
        if argument.arg is None or argument.arg in seen_names:
            return None
        seen_names.add(argument.arg)
        raw_expression = ast.get_source_segment(source, argument.value)
        bindings.append(Binding(argument.arg, raw_expression or ast.unparse(argument.value)))
    return InstanceSpec(instance_id, node_name, result_var, tuple(bindings))


def reconnect_instance_source(source: str, *, input_name: str, expression: str) -> str:
    instance = parse_instance_source(source)
    if instance is None:
        raise ValueError("source is not a Graphimo-owned instance")
    if not isinstance(input_name, str) or not input_name.isidentifier() or keyword.iskeyword(input_name):
        raise ValueError("input name must be a valid Python identifier")
    _parse_expression(expression)
    found = False
    bindings = {}
    for binding in instance.bindings:
        if binding.input_name == input_name:
            if found:
                raise ValueError("instance input is not unique")
            bindings[binding.input_name] = expression.strip()
            found = True
        else:
            bindings[binding.input_name] = binding.expression
    if not found:
        raise ValueError(f"instance has no input named {input_name!r}")
    regenerated, _ = build_instance_source(
        instance.node_name,
        bindings,
        instance_id=instance.instance_id,
    )
    return regenerated


def output_expression(instance: InstanceSpec, output_index: int, output_count: int) -> str:
    if type(output_count) is not int or output_count < 1:
        raise ValueError("output count must be a positive integer")
    if type(output_index) is not int or not 0 <= output_index < output_count:
        raise ValueError("output index is outside the node output range")
    return instance.result_var if output_count == 1 else f"{instance.result_var}[{output_index}]"
