"""Small, SDK-independent operations over a live marimo session.

The host supplies a MarimoPort; Graphimo never owns notebook or dependency
state. This module contains only request validation and presentation helpers.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import keyword
import math
import os
import re
import sys
import tempfile
import textwrap
import typing
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, get_args, get_origin

import graphimo
from graphimo.instance import (
    build_instance_source,
    output_expression,
    parse_instance_source,
    reconnect_instance_source,
)

_LITERAL_ALIAS_TYPE = type(Literal[0])
_TUPLE_ALIAS_TYPES = {type(tuple[int, ...]), type(typing.Tuple[int, ...])}  # noqa: UP006 - support the typing.Tuple spelling
_SAFE_SCALAR_ANNOTATIONS = {"bool", "float", "int", "str"}


class _RequestError(Exception):
    def __init__(self, code: str, message: str, cell_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.cell_id = cell_id


def _error(code: str, message: str, cell_id: str | None = None) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, "cell_id": cell_id}}


def _required_string(request: dict[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _RequestError("invalid_request", f"'{key}' must be a non-empty string")
    return value


def _session(context: Any, requested: Any = None) -> tuple[str, dict[str, Any]]:
    sessions = context.marimo.list_active_notebooks()
    if not isinstance(sessions, list):
        sessions = []
    if requested is None:
        if not sessions:
            raise _RequestError("no_active_session", "No active marimo notebook session was found")
        if len(sessions) > 1:
            error = _RequestError("selection_required", "Choose an active marimo notebook session")
            error.sessions = sessions
            raise error
        selected = sessions[0]
    else:
        selected = next((item for item in sessions if item.get("session_id") == requested), None)
        if selected is None:
            raise _RequestError("session_not_found", f"Active marimo session '{requested}' was not found")
    return selected["session_id"], selected


def _generated_component_ref(source: Any) -> dict[str, str] | None:
    if type(source) is not str:
        return None
    lines = source.splitlines()
    if len(lines) != 4:
        return None
    component_match = re.fullmatch(r"# Graphimo component: ([^\r\n]+)", lines[0])
    hash_match = re.fullmatch(r"# Graphimo source sha256: ([0-9a-f]{64})", lines[1])
    callable_match = re.fullmatch(r"# Graphimo callable: ([A-Za-z_][A-Za-z0-9_]*)", lines[2])
    if not component_match or not hash_match or not callable_match:
        return None
    component_id = component_match.group(1)
    parts = component_id.split("/")
    if any(part in {"", ".", ".."} for part in parts) or "\\" in component_id:
        return None
    module_parts = parts.copy()
    module_parts[-1] = module_parts[-1].removesuffix(".py")
    if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", part) for part in module_parts):
        return None
    module_name = "components." + ".".join(module_parts)
    callable_name = callable_match.group(1)
    try:
        parsed = ast.parse(source)
    except SyntaxError:
        return None
    if len(parsed.body) != 1 or not isinstance(parsed.body[0], ast.Assign):
        return None
    assignment = parsed.body[0]
    if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name) or not isinstance(assignment.value, ast.Call):
        return None
    expected = f"_out = __import__('importlib').reload(__import__('importlib').import_module({module_name!r})).{callable_name}()"
    try:
        expected_call = ast.parse(expected).body[0].value
    except (SyntaxError, AttributeError):
        return None
    if ast.dump(assignment.value.func, include_attributes=False) != ast.dump(expected_call.func, include_attributes=False):
        return None
    return {
        "component_id": component_id,
        "callable": callable_name,
        "source_sha256": hash_match.group(1),
    }


def _snapshot(context: Any, session_id: str) -> dict[str, Any]:
    try:
        snapshot = context.marimo.inspect(session_id)
    except KeyError as exc:
        raise _RequestError("session_not_found", f"Active marimo session '{session_id}' was not found") from exc
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("cells"), list):
        raise _RequestError("marimo_unavailable", "Marimo returned an invalid notebook snapshot")
    projected = dict(snapshot)
    cells = []
    for cell in snapshot["cells"]:
        if not isinstance(cell, dict):
            cells.append(cell)
            continue
        item = dict(cell)
        reference = _generated_component_ref(item.get("source"))
        if reference is not None:
            item["graphimo_component"] = reference
        cells.append(item)
    projected["cells"] = cells
    return projected


def _cell(snapshot: dict[str, Any], cell_id: str) -> dict[str, Any]:
    found = next((cell for cell in snapshot["cells"] if cell.get("id") == cell_id), None)
    if found is None:
        raise _RequestError("cell_not_found", f"Cell '{cell_id}' no longer exists", cell_id)
    return found


def _safe_component_id(root: Path, component_id: Any) -> Path:
    if not isinstance(component_id, str) or not component_id:
        raise _RequestError("component_not_found", "Choose a component from the component library")
    relative = Path(component_id)
    if relative.is_absolute() or any(part in ("..", "") for part in relative.parts):
        raise _RequestError("component_not_found", "Component path is outside the component library")
    base = root.resolve()
    path = (base / relative).resolve()
    if base not in path.parents and path != base:
        raise _RequestError("component_not_found", "Component path is outside the component library")
    if path.is_dir() and (path / "__init__.py").is_file():
        return path / "__init__.py"
    if path.suffix == ".py" and path.is_file():
        return path
    raise _RequestError("component_not_found", f"Component '{component_id}' was not found")


def _load_module(path: Path) -> ModuleType:
    # Use a unique import name and always remove it, so refreshed source is read
    # deterministically and no Graphimo module cache persists across requests.
    module_name = f"_graphimo_component_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        path,
        submodule_search_locations=[str(path.parent)] if path.name == "__init__.py" else None,
    )
    if spec is None or spec.loader is None:
        raise _RequestError("component_load_error", f"Could not load component '{path.name}'")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise _RequestError("component_load_error", f"Component import failed: {exc}") from exc
    finally:
        sys.modules.pop(module_name, None)
    return module


class _SafeAnnotation:
    """An annotation token whose repr is controlled by Graphimo."""

    def __init__(self, text: str):
        self.text = text

    def __repr__(self) -> str:
        return self.text


class _OmittedDefault:
    def __repr__(self) -> str:
        return "..."


def _safe_annotation_text(annotation: Any) -> str | None:
    if annotation is inspect.Parameter.empty:
        return None
    if type(annotation) is str:
        return annotation
    if annotation is None:
        return "None"
    if type(annotation) is type:
        try:
            return type.__getattribute__(annotation, "__name__")
        except (AttributeError, TypeError):
            return None
    choices = _literal_choices(annotation)
    if choices is not None and all(_scalar_default(choice) for choice in choices):
        return "Literal[" + ", ".join(repr(choice) for choice in choices) + "]"
    return None


def _literal_choices(annotation: Any) -> tuple[Any, ...] | None:
    if type(annotation) is not _LITERAL_ALIAS_TYPE or annotation.__origin__ is not Literal:
        return None
    return get_args(annotation)


def _scalar_default(value: Any) -> bool:
    if value is None or type(value) in (bool, int, str):
        return True
    return type(value) is float and math.isfinite(value)


def _parameter_metadata(parameter: inspect.Parameter) -> dict[str, Any]:
    has_default = parameter.default is not inspect.Parameter.empty
    result: dict[str, Any] = {
        "name": parameter.name,
        "required": not has_default,
        "positional": parameter.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD,
        "has_default": has_default,
    }
    if has_default and _scalar_default(parameter.default):
        result["default"] = parameter.default
    annotation = _safe_annotation_text(parameter.annotation)
    if annotation is not None:
        result["annotation"] = annotation
    choices = _literal_choices(parameter.annotation)
    if choices is not None and all(_scalar_default(choice) for choice in choices):
        result["choices"] = list(choices)
    return result


def _safe_signature(signature: inspect.Signature) -> str:
    parameters = []
    for parameter in signature.parameters.values():
        text = _safe_annotation_text(parameter.annotation)
        choices = _literal_choices(parameter.annotation)
        if text is None and choices is not None and all(_scalar_default(choice) for choice in choices):
            text = "Literal[" + ", ".join(repr(choice) for choice in choices) + "]"
        default = (
            parameter.default
            if parameter.default is inspect.Parameter.empty or _scalar_default(parameter.default)
            else _OmittedDefault()
        )
        safe = parameter.replace(
            annotation=_SafeAnnotation(text) if text is not None else inspect.Parameter.empty,
            default=default,
        )
        parameters.append(safe)
    return_annotation = _safe_annotation_text(signature.return_annotation)
    safe_signature = signature.replace(
        parameters=parameters,
        return_annotation=_SafeAnnotation(return_annotation) if return_annotation is not None else inspect.Signature.empty,
    )
    return str(safe_signature)


def _tuple_annotation_items(annotation: Any) -> list[str | None] | None:
    if type(annotation) is str:
        try:
            expression = ast.parse(annotation, mode="eval").body
        except SyntaxError:
            return None
        if not isinstance(expression, ast.Subscript):
            return None
        value = expression.value
        if not (
            isinstance(value, ast.Name) and value.id in {"tuple", "Tuple"}
            or isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "typing"
            and value.attr == "Tuple"
        ):
            return None
        items = expression.slice.elts if isinstance(expression.slice, ast.Tuple) else [expression.slice]
        if not items or any(isinstance(item, ast.Constant) and item.value is Ellipsis for item in items):
            return None
        return [ast.unparse(item) for item in items]

    if type(annotation) not in _TUPLE_ALIAS_TYPES or get_origin(annotation) is not tuple:
        return None
    args = get_args(annotation)
    if not args or any(item is Ellipsis for item in args):
        return None
    return [_safe_annotation_text(item) for item in args]


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


def _callable_function(value: Any) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    try:
        source = textwrap.dedent(inspect.getsource(value))
        tree = ast.parse(source)
        name = getattr(value, "__name__", None)
    except (OSError, TypeError, ValueError, SyntaxError, AttributeError):
        return None
    return next(
        (node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name),
        None,
    )


def _object_base_block() -> dict[str, Any]:
    return {"outputs": [{"index": 1, "name": "result", "annotation": "object"}], "output_mode": "object"}


def _base_block(parsed_signature: inspect.Signature, value: Any) -> dict[str, Any]:
    inputs = [
        _parameter_metadata(parameter)
        for parameter in parsed_signature.parameters.values()
        if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    annotation = parsed_signature.return_annotation
    annotation_text = _safe_annotation_text(annotation)
    if annotation is None or annotation_text == "None":
        return {"inputs": inputs, "outputs": [], "output_mode": "none"}

    function = _callable_function(value)
    if function is None:
        return {"inputs": inputs, **_object_base_block()}
    visitor = _ReturnVisitor(function)
    visitor.visit(function)
    if not visitor.returns or all(node.value is None for node in visitor.returns):
        return {"inputs": inputs, "outputs": [], "output_mode": "none"}
    if any(node.value is None for node in visitor.returns):
        return {"inputs": inputs, **_object_base_block()}

    values = [node.value for node in visitor.returns if node.value is not None]
    tuple_items = _tuple_annotation_items(annotation)
    if tuple_items is not None:
        if not all(isinstance(node, ast.Tuple) and len(node.elts) == len(tuple_items) for node in values):
            return {"inputs": inputs, **_object_base_block()}
        outputs = []
        for position, item_annotation in enumerate(tuple_items, 1):
            expressions = [ast.unparse(node.elts[position - 1]) for node in values]
            unique_expression = expressions[0] if all(item == expressions[0] for item in expressions) else None
            names = [node.elts[position - 1].id if isinstance(node.elts[position - 1], ast.Name) else None for node in values]
            unique_name = names[0] if names[0] and all(item == names[0] for item in names) else None
            descriptor: dict[str, Any] = {
                "index": position,
                "name": unique_name or f"output_{position}",
            }
            if unique_expression is not None:
                descriptor["expression"] = unique_expression
            if item_annotation is not None:
                descriptor["annotation"] = item_annotation
            outputs.append(descriptor)
        return {"inputs": inputs, "outputs": outputs, "output_mode": "tuple"}

    if any(isinstance(node, (ast.Tuple, ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp)) for node in values):
        return {"inputs": inputs, **_object_base_block()}

    expressions = [ast.unparse(node) for node in values]
    unique_expression = expressions[0] if all(item == expressions[0] for item in expressions) else None
    names = [node.id if isinstance(node, ast.Name) else None for node in values]
    unique_name = names[0] if names[0] and all(item == names[0] for item in names) else None
    scalar_annotation = annotation_text in _SAFE_SCALAR_ANNOTATIONS or (
        isinstance(annotation_text, str) and annotation_text.startswith("Literal[")
    )
    scalar_constants = all(
        isinstance(node, ast.Constant)
        and (type(node.value) in (bool, int, str) or (type(node.value) is float and math.isfinite(node.value)))
        for node in values
    )
    if not scalar_annotation and not scalar_constants:
        return {"inputs": inputs, **_object_base_block()}
    descriptor = {"index": 1, "name": unique_name or "output_1"}
    if unique_expression is not None:
        descriptor["expression"] = unique_expression
    if annotation_text is not None:
        descriptor["annotation"] = annotation_text
    return {"inputs": inputs, "outputs": [descriptor], "output_mode": "single"}


def _callables(path: Path) -> list[dict[str, Any]]:
    module = _load_module(path)
    exports = getattr(module, "__all__", None)
    names = exports if isinstance(exports, (list, tuple)) else [name for name in vars(module) if not name.startswith("_")]
    result: list[dict[str, Any]] = []
    for name in names:
        value = getattr(module, name, None)
        if not isinstance(name, str) or not callable(value) or inspect.isclass(value) or inspect.isbuiltin(value):
            continue
        try:
            parsed_signature = inspect.signature(value)
            signature = _safe_signature(parsed_signature)
            parameters = [
                _parameter_metadata(parameter)
                for parameter in parsed_signature.parameters.values()
                if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            ]
        except (TypeError, ValueError):
            parsed_signature = None
            signature = "(...)"
            parameters = []
        doc = inspect.getdoc(value) or inspect.getdoc(module) or ""
        marked_node = graphimo.is_node(value)
        input_parameters = parameters
        outputs: list[dict[str, Any]] = []
        diagnostic = None
        if marked_node:
            try:
                node_spec = graphimo.inspect_node(value)
            except ValueError:
                diagnostic = "node_source_unavailable"
                input_parameters = parameters
            else:
                input_parameters = parameters
                outputs = [
                    {
                        "index": port.index,
                        "declared_type": port.declared_type,
                        "expressions": list(port.expressions),
                    }
                    for port in node_spec.outputs
                ]
                diagnostic = node_spec.diagnostic
        base_block = _base_block_from_node(input_parameters, outputs, diagnostic) if marked_node else {
            "inputs": input_parameters,
            "outputs": [],
            "output_mode": "not_node",
        }
        result.append({
            "name": name,
            "signature": signature,
            "description": doc.splitlines()[0] if doc else "",
            "parameters": parameters,
            "graphimo_node": marked_node,
            "inputs": input_parameters,
            "outputs": outputs,
            "diagnostic": diagnostic,
            "base_block": base_block,
        })
    return result


def _json_safe_literal(value: Any, depth: int = 0) -> bool:
    if depth > 5:
        return False
    if value is None or type(value) in (bool, int, str):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) in (list, tuple):
        return len(value) <= 100 and all(_json_safe_literal(item, depth + 1) for item in value)
    if type(value) is dict:
        return len(value) <= 100 and all(
            type(key) in (str, int, float, bool, type(None))
            and _json_safe_literal(key, depth + 1)
            and _json_safe_literal(item, depth + 1)
            for key, item in value.items()
        )
    return False


def _base_block_from_node(
    inputs: list[dict[str, Any]], outputs: list[dict[str, Any]], diagnostic: str | None
) -> dict[str, Any]:
    if diagnostic:
        return {"inputs": inputs, "outputs": [], "output_mode": "invalid", "diagnostic": diagnostic}
    mode = "none" if not outputs else "single" if len(outputs) == 1 else "tuple"
    projected = []
    for port in outputs:
        item: dict[str, Any] = {
            "index": port["index"] + 1,
            "name": f"output_{port['index'] + 1}",
            "expressions": port["expressions"],
        }
        if port["declared_type"] is not None:
            item["annotation"] = port["declared_type"]
        if len(port["expressions"]) == 1:
            item["expression"] = port["expressions"][0]
        projected.append(item)
    return {"inputs": inputs, "outputs": projected, "output_mode": mode}


def _components(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        raise _RequestError("component_root_not_found", f"Component library '{root}' was not found")
    items: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.py")):
        if any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts):
            continue
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(root).as_posix()
        try:
            callables = _callables(path)
        except _RequestError:
            continue
        if callables:
            items.append({"id": rel, "path": rel, "category": list(path.relative_to(root).parts[:-1]), "kind": "file", "callables": callables})
    for init_path in sorted(root.rglob("__init__.py")):
        if any(part.startswith(".") or part == "__pycache__" for part in init_path.relative_to(root).parts):
            continue
        rel = init_path.parent.relative_to(root).as_posix()
        if rel == ".":
            continue
        try:
            callables = _callables(init_path)
        except _RequestError:
            continue
        if callables:
            items.append({"id": rel, "path": rel, "category": list(init_path.parent.relative_to(root).parts[:-1]), "kind": "package", "callables": callables})
    items.sort(key=lambda item: item["id"])
    return items


def _identifier(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.isidentifier() or keyword.iskeyword(value):
        raise _RequestError("invalid_request", f"'{key}' must be a Python identifier")
    return value


def _python_literal(value: Any, depth: int = 0) -> str:
    if not _json_safe_literal(value, depth) or depth > 5:
        raise _RequestError("invalid_request", "Binding literal is not a supported bounded Python literal")
    if type(value) in (list, tuple):
        opening, closing = ("[", "]") if type(value) is list else ("(", ")")
        items = ", ".join(_python_literal(item, depth + 1) for item in value)
        if type(value) is tuple and len(value) == 1:
            items += ","
        return opening + items + closing
    if type(value) is dict:
        items = (
            f"{_python_literal(key, depth + 1)}: {_python_literal(item, depth + 1)}"
            for key, item in value.items()
        )
        return "{" + ", ".join(items) + "}"
    return repr(value)


def _find_node(root: Path, node_name: str) -> tuple[dict[str, Any], Any] | None:
    matches = []
    for component in _components(root):
        path = _safe_component_id(root, component["id"])
        for item in component["callables"]:
            if item["name"] == node_name and item["graphimo_node"]:
                module = _load_module(path)
                function = getattr(module, node_name, None)
                if graphimo.is_node(function):
                    matches.append((item, function))
    return matches[0] if len(matches) == 1 else None


def _add_component(request: dict[str, Any], context: Any, session_id: str) -> dict[str, Any]:
    root = Path(request.get("root") or context.components_root)
    component_id = request.get("component_id")
    path = _safe_component_id(root, component_id)
    exports = _callables(path)
    callable_name = request.get("callable")
    if not callable_name:
        if len(exports) == 1:
            callable_name = exports[0]["name"]
        else:
            raise _RequestError("callable_required", "Choose a callable from this component")
    export = next((item for item in exports if item["name"] == callable_name), None)
    if export is None:
        raise _RequestError("callable_not_found", f"Callable '{callable_name}' was not found in this component")
    if not export["graphimo_node"]:
        raise _RequestError("node_required", f"Callable '{callable_name}' must be decorated with @node")
    module = _load_module(path)
    function = getattr(module, callable_name, None)
    if not graphimo.is_node(function):
        raise _RequestError("node_required", f"Callable '{callable_name}' must be decorated with @node")
    node_spec = graphimo.inspect_node(function)
    if node_spec.diagnostic:
        raise _RequestError(node_spec.diagnostic, f"Node '{callable_name}' has an invalid output contract")
    bindings = request.get("bindings", {})
    if not isinstance(bindings, dict):
        raise _RequestError("invalid_request", "'bindings' must be an object")
    expressions: dict[str, str] = {}
    for name, value in bindings.items():
        name = _identifier(name, "binding name")
        if not isinstance(value, dict) or len(value) != 1:
            raise _RequestError("invalid_request", f"Binding '{name}' must contain exactly one of 'variable' or 'literal'")
        if "variable" in value:
            variable = _identifier(value["variable"], "binding variable")
            expressions[name] = variable
        elif "literal" in value:
            expressions[name] = _python_literal(value["literal"])
        else:
            raise _RequestError("invalid_request", f"Binding '{name}' must contain 'variable' or 'literal'")
    for port in node_spec.inputs:
        if port.required and port.name not in expressions:
            raise _RequestError(
                "missing_required_binding",
                f"Required input '{port.name}' has no connection or value",
            )
    try:
        source, _ = build_instance_source(callable_name, expressions, node_spec=node_spec)
    except (TypeError, ValueError) as exc:
        raise _RequestError("invalid_request", str(exc)) from exc
    try:
        compile(source, "<graphimo-component>", "exec")
        cell_id = context.marimo.add_cell(session_id, source)
    except Exception as exc:
        raise _RequestError("marimo_unavailable", f"Could not insert component cell: {exc}") from exc
    return {"ok": True, "cell_id": cell_id, "source": source}


def _connect(request: dict[str, Any], context: Any, session_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    target_id = _required_string(request, "to_cell_id")
    target = _cell(snapshot, target_id)
    source_id = _required_string(request, "from_cell_id")
    source = _cell(snapshot, source_id)
    source_spec = parse_instance_source(source.get("source", ""))
    target_spec = parse_instance_source(target.get("source", ""))
    if source_spec is None or target_spec is None:
        raise _RequestError("unsafe_edit", "Connections require Graphimo-owned source and target instances", target_id)
    input_name = _identifier(request.get("input_name", request.get("input_ref")), "input_name")
    if input_name not in {binding.input_name for binding in target_spec.bindings}:
        raise _RequestError("input_not_found", f"Target instance has no bound input named '{input_name}'", target_id)
    root = Path(request.get("root") or context.components_root)
    source_node = _find_node(root, source_spec.node_name)
    target_node = _find_node(root, target_spec.node_name)
    if source_node is None or target_node is None:
        raise _RequestError("node_not_found", "A connected node definition is unavailable", target_id)
    source_contract = graphimo.inspect_node(source_node[1])
    target_contract = graphimo.inspect_node(target_node[1])
    try:
        output_index = request.get("output_index", 0)
        expression = output_expression(source_spec, output_index, len(source_contract.outputs))
    except ValueError as exc:
        raise _RequestError("invalid_connection", str(exc), source_id) from exc
    if input_name not in {port.name for port in target_contract.inputs}:
        raise _RequestError("input_not_found", f"Node has no input named '{input_name}'", target_id)
    try:
        updated = reconnect_instance_source(
            target.get("source", ""), input_name=input_name, expression=expression
        )
    except ValueError as exc:
        raise _RequestError("unsafe_edit", str(exc), target_id) from exc
    try:
        compile(updated, "<graphimo-connect>", "exec")
        context.marimo.replace_cell_source(session_id, target_id, updated)
    except Exception as exc:
        raise _RequestError("marimo_unavailable", f"Could not update cell source: {exc}", target_id) from exc
    return {"ok": True, "changed": True, "source": updated}


def _inspect_node_instance(request: dict[str, Any], context: Any, session_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    cell_id = _required_string(request, "cell_id")
    cell = _cell(snapshot, cell_id)
    instance = parse_instance_source(cell.get("source", ""))
    if instance is None:
        raise _RequestError("not_graphimo_instance", "Cell is not a Graphimo-owned node instance", cell_id)
    root = Path(request.get("root") or context.components_root)
    found = _find_node(root, instance.node_name)
    if found is None:
        raise _RequestError("node_not_found", f"Node definition '{instance.node_name}' is unavailable or ambiguous", cell_id)
    spec = graphimo.inspect_node(found[1])
    if spec.diagnostic:
        raise _RequestError(spec.diagnostic, f"Node '{instance.node_name}' has an invalid output contract", cell_id)
    getter = getattr(context.marimo, "get_variable", None)
    if not callable(getter):
        raise _RequestError("runtime_value_unavailable", "The marimo host does not expose live variable inspection", cell_id)
    try:
        value = getter(session_id, instance.result_var)
    except (KeyError, LookupError) as exc:
        raise _RequestError("runtime_value_unavailable", f"Result '{instance.result_var}' is not available", cell_id) from exc
    try:
        outputs = graphimo.describe_result(value, spec)
    except ValueError as exc:
        raise _RequestError(str(exc), f"Could not describe node result: {exc}", cell_id) from exc
    return {
        "ok": True,
        "instance_id": instance.instance_id,
        "node": instance.node_name,
        "outputs": outputs,
    }


def dispatch(request: Any, context: Any) -> dict[str, Any]:
    """Dispatch one host operation. All failures use the common error shape."""
    try:
        if not isinstance(request, dict):
            raise _RequestError("invalid_request", "Request must be an object")
        operation = request.get("op")
        if operation == "list_components":
            root = Path(request.get("root") or context.components_root)
            return {"ok": True, "components": _components(root)}
        if operation not in {"inspect", "inspect_node_instance", "add_component", "connect", "run", "open_code", "delete"}:
            raise _RequestError("invalid_request", "Unknown or missing operation")
        session_id, _ = _session(context, request.get("session_id"))
        snapshot = _snapshot(context, session_id)
        if operation == "inspect":
            return {"ok": True, "notebook": snapshot.get("notebook", {"path": None, "session_id": session_id}), "cells": snapshot["cells"], "edges": snapshot.get("edges", [])}
        if operation == "inspect_node_instance":
            return _inspect_node_instance(request, context, session_id, snapshot)
        if operation == "add_component":
            return _add_component(request, context, session_id)
        if operation == "connect":
            return _connect(request, context, session_id, snapshot)
        cell_id = _required_string(request, "cell_id")
        _cell(snapshot, cell_id)
        if operation == "run":
            state = context.marimo.run_cell(session_id, cell_id)
            return {"ok": True, **(state if isinstance(state, dict) else {"status": str(state)})}
        if operation == "delete":
            state = context.marimo.delete_cell(session_id, cell_id)
            return {"ok": True, **(state if isinstance(state, dict) else {"status": str(state)})}
        callback = getattr(context, "open_code", None)
        if not callable(callback):
            raise _RequestError("open_code_unavailable", "Zeron code navigation is unavailable", cell_id)
        callback(cell_id)
        return {"ok": True, "opened": True}
    except _RequestError as exc:
        result = _error(exc.code, str(exc), exc.cell_id)
        if hasattr(exc, "sessions"):
            result["sessions"] = exc.sessions
        return result
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        return _error("operation_failed", str(exc))


def read_layout(path: str | os.PathLike[str], notebook_path: str) -> dict[str, dict[str, Any]]:
    """Read the presentation-only v1 layout for one notebook."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("notebooks"), dict):
            return {}
        notebook = data["notebooks"].get(notebook_path, {})
        cells = notebook.get("cells", {}) if isinstance(notebook, dict) else {}
        if not isinstance(cells, dict):
            return {}
        return {key: value for key, value in cells.items() if isinstance(key, str) and isinstance(value, dict)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def write_layout(path: str | os.PathLike[str], notebook_path: str, cells: dict[str, dict[str, Any]]) -> None:
    """Atomically update one notebook's allowed visual metadata."""
    allowed = {"x", "y", "collapsed", "width", "height", "group"}
    clean = {
        cell_id: {key: value for key, value in state.items() if key in allowed}
        for cell_id, state in cells.items()
        if isinstance(cell_id, str) and isinstance(state, dict)
    }
    target = Path(path)
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing.get("version") != 1 or not isinstance(existing.get("notebooks"), dict):
            existing = {"version": 1, "notebooks": {}}
    except (OSError, ValueError, TypeError, AttributeError):
        existing = {"version": 1, "notebooks": {}}
    existing["notebooks"][notebook_path] = {"cells": clean}
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(existing, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def merge_layout(cells: list[dict[str, Any]], saved: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge matching positions and place new cells in a simple grid."""
    result: list[dict[str, Any]] = []
    for index, cell in enumerate(cells):
        merged = dict(cell)
        state = saved.get(cell.get("id"), {})
        if isinstance(state, dict):
            for key in ("x", "y", "collapsed", "width", "height", "group"):
                if key in state:
                    merged[key] = state[key]
        merged.setdefault("x", 80 + (index % 4) * 280)
        merged.setdefault("y", 80 + (index // 4) * 190)
        result.append(merged)
    return result
