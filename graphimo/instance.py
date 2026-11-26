"""Canonical source generation for Graphimo-owned node instances."""

from __future__ import annotations

import ast
import hashlib
import importlib
import keyword
import re
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_INSTANCE_ID = re.compile(r"^[0-9a-f]{8}$")
_COMPONENT_PATH = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.py$")
_LITERAL_CALL = re.compile(r"^__import__\('json'\)\.loads\(.*\)$", re.DOTALL)
_MAX_EXPRESSION_CHARS = 10_000
_DISABLED_BEGIN = "# graphimo:disabled-begin"
_DISABLED_END = "# graphimo:disabled-end"


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
    component_path: str | None = None


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


def _validate_component_path(component_path: str) -> None:
    if (
        not isinstance(component_path, str)
        or _COMPONENT_PATH.fullmatch(component_path) is None
        or any(
            part in {".", ".."} or part.startswith(".") or part == "__pycache__"
            for part in component_path.split("/")
        )
    ):
        raise ValueError("component_path must be a relative .py path under components/")


def runtime_symbol(component_path: str, node_name: str) -> str:
    """Return the collision-free marimo global bound to one component callable.

    Two components may export functions with the same name, so the runtime
    symbol is derived from the stable component path rather than its content.
    """
    _validate_component_path(component_path)
    _validate_node_name(node_name)
    digest = hashlib.sha256(component_path.encode("utf-8")).hexdigest()[:8]
    return f"__graphimo_c_{digest}_{node_name}"


def call_target(node_name: str, component_path: str | None) -> str:
    """Return the callee used by an instance cell for this node."""
    if component_path is None:
        return node_name
    return runtime_symbol(component_path, node_name)


def binding_kind(expression: str) -> str:
    """Classify a binding expression as a JSON literal or a variable reference."""
    if isinstance(expression, str) and _LITERAL_CALL.match(expression.strip()):
        return "literal"
    return "variable"


def load_node_function(
    instance: InstanceSpec, *, project_root: str | Path, namespace: Mapping[str, Any] | None = None
) -> Any:
    """Resolve the live callable for an instance inside the marimo kernel.

    The library cell owns the runtime symbol; the component module is reloaded
    from disk so inspection always sees the current file. Kernel runtime
    objects never cross this boundary.
    """
    symbol = call_target(instance.node_name, instance.component_path)
    if namespace is not None:
        function = namespace.get(symbol)
        if function is not None:
            return function
    if instance.component_path is not None:
        path = PurePosixPath(instance.component_path)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise ValueError("invalid Graphimo component path")
        root = Path(project_root)
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        module_path = path.parent if path.name == "__init__.py" else path.with_suffix("")
        module_name = ".".join(["components", *module_path.parts])
        module = importlib.reload(importlib.import_module(module_name))
        function = getattr(module, instance.node_name, None)
        if function is not None:
            return function
    if namespace is not None:
        return namespace.get(instance.node_name)
    return None


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
    component_path: str | None = None,
) -> tuple[str, InstanceSpec]:
    """Build the canonical marimo cell body for one `@node` call."""
    _validate_node_name(node_name)
    if component_path is not None:
        _validate_component_path(component_path)
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
    callee = call_target(node_name, component_path)
    if normalized:
        arguments = "\n".join(f"    {item.input_name}={item.expression}," for item in normalized)
        call = f"{result_name} = {callee}(\n{arguments}\n)"
    else:
        call = f"{result_name} = {callee}()"
    header = f"# Graphimo node: {node_name}\n"
    if component_path is not None:
        header += f"# Graphimo component: {component_path}\n"
    header += f"# Graphimo instance: {chosen_id}\n"
    source = f"{header}{call}"
    return source, InstanceSpec(
        chosen_id, node_name, result_name, tuple(normalized), component_path
    )


def parse_instance_source(source: str) -> InstanceSpec | None:
    """Recognize only a single, marked, direct node-call assignment."""
    if not isinstance(source, str):
        return None
    lines = source.splitlines()
    node_lines = [line for line in lines if line.startswith("# Graphimo node:")]
    instance_lines = [line for line in lines if line.startswith("# Graphimo instance:")]
    component_lines = [line for line in lines if line.startswith("# Graphimo component:")]
    if len(node_lines) != 1 or len(instance_lines) != 1 or len(component_lines) > 1:
        return None
    node_marker = node_lines[0]
    instance_marker = instance_lines[0]
    node_name = node_marker.removeprefix("# Graphimo node: ")
    instance_id = instance_marker.removeprefix("# Graphimo instance: ")
    component_path: str | None = None
    if component_lines:
        component_path = component_lines[0].removeprefix("# Graphimo component: ")
        if component_lines[0] != f"# Graphimo component: {component_path}":
            return None
    if (
        node_marker != f"# Graphimo node: {node_name}"
        or instance_marker != f"# Graphimo instance: {instance_id}"
    ):
        return None
    node_index = lines.index(node_marker)
    instance_index = lines.index(instance_marker)
    if node_index >= instance_index:
        return None
    if component_lines and not node_index < lines.index(component_lines[0]) < instance_index:
        return None
    try:
        _validate_node_name(node_name)
        _validate_instance_id(instance_id)
        if component_path is not None:
            _validate_component_path(component_path)
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
        return None
    assignment = tree.body[0]
    if assignment.lineno <= instance_index + 1:
        return None
    if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
        return None
    expected_result = result_variable(node_name, instance_id)
    if assignment.targets[0].id != expected_result or not isinstance(assignment.value, ast.Call):
        return None
    call = assignment.value
    expected_callee = call_target(node_name, component_path)
    if not isinstance(call.func, ast.Name) or call.func.id != expected_callee or call.args:
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
    return InstanceSpec(instance_id, node_name, expected_result, tuple(bindings), component_path)


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
    regenerated, _ = build_instance_source(
        spec.node_name, updated, instance_id=spec.instance_id, component_path=spec.component_path
    )
    return regenerated


def disconnect_instance_source(source: str, *, input_name: str) -> str:
    """Remove exactly one bound input from a canonical Graphimo instance."""
    spec = parse_instance_source(source)
    if spec is None:
        raise ValueError("source is not a canonical Graphimo node instance")
    if (
        not isinstance(input_name, str)
        or not input_name.isidentifier()
        or keyword.iskeyword(input_name)
    ):
        raise ValueError("input_name must be a valid Python identifier")
    bindings = {binding.input_name: binding.expression for binding in spec.bindings}
    if input_name not in bindings:
        raise ValueError(f"instance has no bound input named {input_name!r}")
    del bindings[input_name]
    regenerated, _ = build_instance_source(
        spec.node_name, bindings, instance_id=spec.instance_id, component_path=spec.component_path
    )
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


def disable_instance_source(source: str) -> str:
    """Comment a marked instance out of marimo's executable dependency graph."""
    instance = parse_instance_source(source)
    if instance is None:
        raise ValueError("only a valid Graphimo node instance can be disabled")
    canonical, _ = build_instance_source(
        instance.node_name,
        {binding.input_name: binding.expression for binding in instance.bindings},
        instance_id=instance.instance_id,
        component_path=instance.component_path,
    )
    if source != canonical:
        raise ValueError("only canonical Graphimo node source can be disabled")
    lines = source.splitlines(keepends=True)
    if _DISABLED_BEGIN in lines or _DISABLED_END in lines:
        raise ValueError("instance source already contains disabled markers")
    body = "".join("# " + line for line in lines)
    separator = "" if body.endswith(("\n", "\r")) else "\n"
    return f"{_DISABLED_BEGIN}\n{body}{separator}{_DISABLED_END}"


def enable_instance_source(source: str) -> str:
    """Restore the exact source body from a valid disabled Graphimo instance."""
    if not isinstance(source, str):
        raise TypeError("disabled source must be text")
    lines = source.splitlines(keepends=True)
    bare = [line.rstrip("\r\n") for line in lines]
    if bare.count(_DISABLED_BEGIN) != 1 or bare.count(_DISABLED_END) != 1:
        raise ValueError("disabled instance markers are missing, nested, or duplicated")
    start = bare.index(_DISABLED_BEGIN)
    end = bare.index(_DISABLED_END)
    if start != 0 or end != len(lines) - 1 or start >= end:
        raise ValueError("disabled instance markers are not in canonical order")
    restored: list[str] = []
    for line in lines[start + 1 : end]:
        if line.endswith("\r\n"):
            content, ending = line[:-2], "\r\n"
        elif line.endswith(("\r", "\n")):
            content, ending = line[:-1], line[-1:]
        else:
            content, ending = line, ""
        if content == "#":
            restored.append(ending)
        elif content.startswith("# "):
            restored.append(content[2:] + ending)
        else:
            raise ValueError("disabled instance body contains an uncommented line")
    active = "".join(restored)
    # build_instance_source emits a source body without a final newline; the
    # line break separating its last commented line from disabled-end is framing.
    active = active.removesuffix("\n")
    if parse_instance_source(active) is None:
        raise ValueError("disabled body does not contain a valid Graphimo node instance")
    return active


def parse_disabled_instance_source(source: str) -> InstanceSpec | None:
    """Return instance identity for a disabled body while keeping it non-executable."""
    try:
        return parse_instance_source(enable_instance_source(source))
    except ValueError:
        return None
