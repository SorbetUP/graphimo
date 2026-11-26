"""Validated project-local ``@node`` component source operations."""

from __future__ import annotations

import ast
import hashlib
import json
import keyword
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .instance import build_instance_source, runtime_symbol

MAX_COMPONENT_SOURCE_BYTES = 2 * 1024 * 1024
MAX_VALIDATION_OUTPUT_BYTES = 64 * 1024
_COMPONENT_ID = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.py$")


class ComponentError(ValueError):
    """Raised when a component path or validation request is invalid."""


class ComponentLibrary:
    """Write and validate reusable Python component files under ``components/``."""

    def __init__(self, project_path: str | Path):
        self.project_path = Path(project_path).expanduser().resolve()
        self.root = self.project_path.parent / "components"

    def write(self, path: str, source: str) -> dict[str, Any]:
        """Validate new source and atomically create or replace its component file."""
        target = self._target(path)
        if not isinstance(source, str):
            raise ComponentError("component source must be text")
        encoded = source.encode("utf-8")
        if not encoded.strip():
            raise ComponentError("component source must not be empty")
        if len(encoded) > MAX_COMPONENT_SOURCE_BYTES:
            raise ComponentError(
                f"component source exceeds the {MAX_COMPONENT_SOURCE_BYTES}-byte limit"
            )

        self.root.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.stem}.graphimo-", suffix=".py", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(source)
                stream.flush()
                os.fsync(stream.fileno())
            validation = self._validate_file(temporary, path)
            if not validation["valid"]:
                return {
                    "valid": False,
                    "path": path,
                    "diagnostics": validation["diagnostics"],
                    "nodes": validation.get("nodes", []),
                }
            if target.is_symlink():
                raise ComponentError("component target cannot be a symbolic link")
            created = not target.exists()
            os.replace(temporary, target)
            return {
                "valid": True,
                "created": created,
                "path": path,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "nodes": validation["nodes"],
                "diagnostics": validation["diagnostics"],
            }
        finally:
            temporary.unlink(missing_ok=True)

    def validate(self, path: str) -> dict[str, Any]:
        """Validate an existing component without changing its source."""
        target = self._target(path)
        if not target.is_file():
            raise FileNotFoundError(f"component '{path}' does not exist")
        return self._validate_file(target, path)

    def instance_source(
        self,
        path: str,
        callable_name: str,
        output_name: str,
        bindings: dict[str, Any],
    ) -> dict[str, Any]:
        """Build one readable component-call cell after checking its ``@node`` contract."""
        target = self._target(path)
        validation = self.validate(path)
        if not validation.get("valid"):
            raise ComponentError("component must pass validation before creating an instance")
        if (
            not isinstance(callable_name, str)
            or not callable_name.isidentifier()
            or keyword.iskeyword(callable_name)
        ):
            raise ComponentError("callable must be a Python identifier")
        if (
            not isinstance(output_name, str)
            or not output_name.isidentifier()
            or keyword.iskeyword(output_name)
        ):
            raise ComponentError("output_name must be a Python identifier")
        if not isinstance(bindings, dict):
            raise ComponentError("bindings must be an object")
        node = next((item for item in validation["nodes"] if item["name"] == callable_name), None)
        if node is None:
            raise ComponentError(f"component does not export Graphimo node {callable_name!r}")
        inputs = node["inputs"]
        input_names = [item["name"] for item in inputs]
        unknown = set(bindings) - set(input_names)
        if unknown:
            raise ComponentError(f"unknown component input(s): {', '.join(sorted(unknown))}")
        # Required inputs may stay unbound: they remain formal ports and the
        # run gate refuses execution with missing_required_binding until the
        # user or an agent connects them.

        expressions: dict[str, str] = {}
        for name in input_names:
            if name not in bindings:
                continue
            value = bindings[name]
            if not isinstance(value, dict) or set(value) not in ({"variable"}, {"literal"}):
                raise ComponentError(f"input {name!r} needs exactly one variable or literal")
            if "variable" in value:
                variable = value["variable"]
                if (
                    not isinstance(variable, str)
                    or not variable.isidentifier()
                    or keyword.iskeyword(variable)
                ):
                    raise ComponentError(f"input {name!r} variable must be a Python identifier")
                expressions[name] = variable
            else:
                literal = json.dumps(value["literal"], ensure_ascii=False, allow_nan=False)
                if len(literal.encode("utf-8")) > 16 * 1024:
                    raise ComponentError(f"input {name!r} literal exceeds 16 KiB")
                expressions[name] = f"__import__('json').loads({literal!r})"

        positional_only_indexes = [
            index for index, item in enumerate(inputs) if item.get("positional_only")
        ]
        last_positional_only = max(
            (index for index in positional_only_indexes if inputs[index]["name"] in expressions),
            default=-1,
        )
        positional: list[str] = []
        keywords: list[str] = []
        if last_positional_only >= 0:
            for item in inputs[: last_positional_only + 1]:
                name = item["name"]
                if name not in expressions:
                    raise ComponentError(
                        f"bind positional-only inputs before {inputs[last_positional_only]['name']!r}"
                    )
                positional.append(expressions[name])
        for index, item in enumerate(inputs):
            name = item["name"]
            if name not in expressions or index <= last_positional_only:
                continue
            if item.get("positional_only"):
                raise ComponentError(f"positional-only input {name!r} must be bound")
            keywords.append(f"{name}={expressions[name]}")
        rendered = positional + keywords

        relative = Path(path)
        digest = _component_digest(target, relative)
        if positional_only_indexes:
            # Python cannot call positional-only parameters through the
            # canonical keyword-binding instance format. Preserve the valid
            # Python call and report that this is not a canonical instance.
            module_path = (
                relative.parent if relative.name == "__init__.py" else relative.with_suffix("")
            )
            module_parts = [
                "components",
                *(part for part in module_path.parts if part not in ("", ".")),
            ]
            module_name = ".".join(module_parts)
            called = (
                "__import__('importlib').reload("
                f"__import__('importlib').import_module({module_name!r})).{callable_name}"
            )
            source = (
                f"# Graphimo component: {path}\n"
                f"# Graphimo source sha256: {digest}\n"
                f"# Graphimo callable: {callable_name}\n"
                f"{output_name} = {called}({', '.join(rendered)})\n"
            )
            compile(source, "<graphimo-component-instance>", "exec")
            return {
                "source": source,
                "library_source": None,
                "library_marker": None,
                "source_sha256": digest,
                "output_name": output_name,
                "result_var": output_name,
                "instance_id": None,
                "canonical_instance": False,
            }
        source, instance = build_instance_source(callable_name, expressions, component_path=path)
        library_marker, library_source = self._library_source(path, callable_name, digest)
        compile(source, "<graphimo-component-instance>", "exec")
        return {
            "source": source,
            "library_source": library_source,
            "library_marker": library_marker,
            "source_sha256": digest,
            "component_id": path,
            "callable": callable_name,
            "runtime_symbol": runtime_symbol(path, callable_name),
            "output_name": output_name,
            "result_var": instance.result_var,
            "instance_id": instance.instance_id,
            "canonical_instance": True,
        }

    def library_updates(self, *, limit: int = 512) -> dict[str, str]:
        """Map every current library marker to its canonical source for refresh."""
        updates: dict[str, str] = {}
        for item in self.list().get("items", []):
            path = str(item["path"])
            digest = _component_digest(self._target(path), Path(path))
            for callable_name in item.get("nodes", []):
                if not isinstance(callable_name, str):
                    continue
                marker, source = self._library_source(path, callable_name, digest)
                updates[marker] = source
        if len(updates) > limit:
            raise ComponentError(f"component library exceeds the {limit}-entry refresh limit")
        return updates

    def _library_source(self, path: str, callable_name: str, digest: str) -> tuple[str, str]:
        """Build the canonical library cell binding one namespaced runtime symbol."""
        target = self._target(path)
        if not target.is_file():
            raise FileNotFoundError(f"component '{path}' does not exist")
        relative = Path(path)
        module_path = (
            relative.parent if relative.name == "__init__.py" else relative.with_suffix("")
        )
        module_parts = [
            "components",
            *(part for part in module_path.parts if part not in ("", ".")),
        ]
        module_name = ".".join(module_parts)
        symbol = runtime_symbol(path, callable_name)
        marker = f"# Graphimo node library: {path}:{callable_name}"
        source = (
            f"{marker}\n"
            f"# Graphimo node source sha256: {digest}\n"
            f"# Graphimo node symbol: {symbol}\n"
            "import sys as _graphimo_sys\n"
            f"_graphimo_root = {str(self.root.resolve().parent)!r}\n"
            "if _graphimo_root not in _graphimo_sys.path:\n"
            "    _graphimo_sys.path.insert(0, _graphimo_root)\n"
            "import importlib as _graphimo_importlib\n"
            f"_graphimo_module = _graphimo_importlib.reload(_graphimo_importlib.import_module({module_name!r}))\n"
            f"{symbol} = _graphimo_module.{callable_name}\n"
        )
        return marker, source

    def list(self) -> dict[str, Any]:
        """List Python files with public ``@node`` declarations without importing them."""
        items: list[dict[str, Any]] = []
        diagnostics: list[dict[str, str]] = []
        if not self.root.is_dir():
            return {"items": items, "diagnostics": diagnostics}
        for target in sorted(self.root.rglob("*.py")):
            relative = target.relative_to(self.root)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            if target.is_symlink():
                continue
            try:
                source = target.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(relative))
                aliases, qualified_aliases = _node_import_aliases(tree)
                nodes = [
                    item.name
                    for item in tree.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not item.name.startswith("_")
                    and any(
                        _is_node_decorator(decorator, aliases, qualified_aliases)
                        for decorator in item.decorator_list
                    )
                ]
                if nodes:
                    raw = source.encode("utf-8")
                    callable_items = [
                        _static_callable(source, item)
                        for item in tree.body
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name in nodes
                    ]
                    items.append(
                        {
                            "path": relative.as_posix(),
                            "id": relative.as_posix(),
                            "category": list(relative.parts[:-1]),
                            "kind": "file",
                            "nodes": nodes,
                            "callables": callable_items,
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "source_sha256": hashlib.sha256(raw).hexdigest(),
                        }
                    )
            except (OSError, UnicodeDecodeError, SyntaxError) as exc:
                diagnostics.append(
                    {
                        "code": "component_definition_invalid",
                        "severity": "warning",
                        "message": f"{relative.as_posix()}: {exc}",
                    }
                )
        return {"items": items, "diagnostics": diagnostics}

    def delete(self, path: str) -> dict[str, Any]:
        """Move a component to project trash so the operation can be reversed."""
        target = self._target(path)
        if not target.is_file():
            raise FileNotFoundError(f"component '{path}' does not exist")
        trash_id = uuid.uuid4().hex
        trash_root = self.project_path.parent / ".graphimo" / "component-trash" / trash_id
        destination = trash_root.joinpath(*path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, destination)
        return {"deleted": True, "path": path, "trash_path": str(destination)}

    def _target(self, path: str) -> Path:
        if not isinstance(path, str) or not _COMPONENT_ID.fullmatch(path):
            raise ComponentError("component path must be a relative .py path under components/")
        parts = path.split("/")
        if any(part in {".", ".."} or part.startswith(".") for part in parts):
            raise ComponentError("component path cannot contain hidden or traversal segments")
        if any(part == "__pycache__" for part in parts):
            raise ComponentError("component path cannot target a Python cache directory")

        root = self.root.resolve()
        target = root.joinpath(*parts)
        current = root
        for part in parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ComponentError("component path cannot traverse a symbolic link")
        if not target.resolve(strict=False).is_relative_to(root):
            raise ComponentError("component path escapes the project components/ directory")
        if target.exists() and not target.is_file():
            raise ComponentError("component path must name a regular Python file")
        if target.is_symlink():
            raise ComponentError("component target cannot be a symbolic link")
        return target

    def _validate_file(self, candidate: Path, relative_path: str) -> dict[str, Any]:
        diagnostics: list[dict[str, str]] = []
        try:
            source = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _invalid("component source must be UTF-8")
        try:
            tree = ast.parse(source, filename=relative_path)
        except SyntaxError as exc:
            return _invalid(f"Python syntax error at line {exc.lineno}: {exc.msg}")
        policy_error = _validate_component_module(tree)
        if policy_error:
            return _invalid(policy_error, code="component_top_level_execution")

        ruff = shutil.which("ruff")
        if ruff:
            for command in ([ruff, "format", str(candidate)], [ruff, "check", str(candidate)]):
                result = _run_bounded(command, cwd=self.project_path.parent, timeout=8)
                if result["returncode"] != 0:
                    message = result["output"].strip() or f"{Path(command[1]).name} failed"
                    return _invalid(message, code="component_ruff_failed")
        else:
            diagnostics.append(
                {
                    "code": "ruff_unavailable",
                    "severity": "warning",
                    "message": "ruff was not found; formatting and lint checks were skipped",
                }
            )

        aliases, modules = _node_import_aliases(tree)
        nodes = [
            _static_node_contract(source, item)
            for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not item.name.startswith("_")
            and any(
                _is_node_decorator(decorator, aliases, modules) for decorator in item.decorator_list
            )
        ]
        if not nodes:
            return _invalid(
                "component must export at least one public function decorated with @graphimo.node",
                code="component_node_missing",
            )
        broken = [item for item in nodes if item.get("diagnostic")]
        if broken:
            return _invalid(
                "Graphimo node contract error: "
                + ", ".join(f"{item['name']}: {item['diagnostic']}" for item in broken),
                code="component_node_contract_invalid",
                nodes=nodes,
            )
        return {"valid": True, "nodes": nodes, "diagnostics": diagnostics}


def _component_digest(target: Path, relative: Path) -> str:
    """Hash a component file, or every file of a package, deterministically."""
    digest_builder = hashlib.sha256()
    if relative.name == "__init__.py":
        package_root = target.parent
        for source_file in sorted(package_root.rglob("*.py")):
            relative_source = source_file.relative_to(package_root).as_posix()
            digest_builder.update(relative_source.encode("utf-8"))
            digest_builder.update(source_file.read_bytes())
    else:
        digest_builder.update(target.read_bytes())
    return digest_builder.hexdigest()


def _run_bounded(command: list[str], *, cwd: Path, timeout: float) -> dict[str, Any]:
    """Run validation with a time bound and file-backed output capture."""
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.pathsep.join(sys.path)},
            )
        except subprocess.TimeoutExpired:
            return {"returncode": 124, "output": "component validation timed out"}
        output.seek(0)
        raw = output.read(MAX_VALIDATION_OUTPUT_BYTES + 1)
        text = raw[:MAX_VALIDATION_OUTPUT_BYTES].decode("utf-8", errors="replace")
        if len(raw) > MAX_VALIDATION_OUTPUT_BYTES:
            text += "\n[validation output truncated]"
        return {"returncode": result.returncode, "output": text}


def _invalid(
    message: str,
    *,
    code: str = "component_validation_failed",
    nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "valid": False,
        "nodes": nodes or [],
        "diagnostics": [{"code": code, "severity": "error", "message": message[:2000]}],
    }


def _node_import_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    modules: set[str] = set()
    for item in tree.body:
        if isinstance(item, ast.ImportFrom) and item.module == "graphimo":
            for alias in item.names:
                if alias.name == "node":
                    names.add(alias.asname or alias.name)
        elif isinstance(item, ast.Import):
            for alias in item.names:
                if alias.name == "graphimo":
                    modules.add(alias.asname or alias.name)
    return names, modules


def _static_node_contract(
    source: str, function: ast.FunctionDef | ast.AsyncFunctionDef
) -> dict[str, Any]:
    static = _static_callable(source, function)
    inputs = [
        {
            "name": item["name"],
            "required": item["required"],
            "annotation": item.get("annotation"),
            "has_default": item["has_default"],
            "positional_only": item["positional_only"],
        }
        for item in static["parameters"]
    ]
    outputs = [
        {
            "index": item["index"] - 1,
            "declared_type": item["declared_type"],
            "expressions": item["expressions"],
        }
        for item in static["outputs"]
    ]
    return {
        "name": static["name"],
        "inputs": inputs,
        "outputs": outputs,
        "diagnostic": static["diagnostic"],
    }


def _is_node_decorator(
    value: ast.expr, aliases: set[str] | None = None, module_aliases: set[str] | None = None
) -> bool:
    aliases = {"node"} if aliases is None else aliases
    module_aliases = {"graphimo"} if module_aliases is None else module_aliases
    if isinstance(value, ast.Name):
        return value.id in aliases
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "node"
        and isinstance(value.value, ast.Name)
        and value.value.id in module_aliases
    )


def _safe_annotation_expression(value: ast.expr) -> bool:
    if isinstance(value, ast.Name):
        return True
    if isinstance(value, ast.Constant):
        return value.value is None or type(value.value) in (bool, int, float, str)
    if isinstance(value, ast.Attribute):
        return _safe_annotation_expression(value.value)
    if isinstance(value, ast.Subscript):
        return _safe_annotation_expression(value.value) and _safe_annotation_expression(value.slice)
    if isinstance(value, (ast.Tuple, ast.List)):
        return all(_safe_annotation_expression(item) for item in value.elts)
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr):
        return _safe_annotation_expression(value.left) and _safe_annotation_expression(value.right)
    return False


def _literal_default(value: ast.expr) -> tuple[bool, Any]:
    try:
        result = ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False, None
    if isinstance(result, float) and not math.isfinite(result):
        return False, None
    return True, result


def _validate_component_module(tree: ast.Module) -> str | None:
    aliases, modules = _node_import_aliases(tree)
    for index, item in enumerate(tree.body):
        if isinstance(item, (ast.Import, ast.ImportFrom)):
            continue
        if (
            isinstance(item, ast.Expr)
            and index == 0
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        ):
            continue
        if isinstance(item, (ast.Assign, ast.AnnAssign)):
            value = item.value
            if value is None:
                continue
            try:
                ast.literal_eval(value)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                return "module-level assignments must use literal constants; executable expressions are not allowed"
            continue
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_node = any(
                _is_node_decorator(decorator, aliases, modules) for decorator in item.decorator_list
            )
            if any(
                isinstance(decorator, ast.Call)
                or not _is_node_decorator(decorator, aliases, modules)
                for decorator in item.decorator_list
            ):
                return f"function '{item.name}' uses an executable decorator; only bare @node is allowed"
            if item.args.vararg or item.args.kwarg:
                return (
                    f"function '{item.name}' cannot use *args or **kwargs as a Graphimo component"
                )
            for default in [
                *item.args.defaults,
                *[value for value in item.args.kw_defaults if value is not None],
            ]:
                safe, _ = _literal_default(default)
                if not safe:
                    return f"function '{item.name}' defaults must be literal values"
            annotations = [
                arg.annotation
                for arg in [*item.args.posonlyargs, *item.args.args, *item.args.kwonlyargs]
                if arg.annotation is not None
            ]
            if item.returns is not None:
                annotations.append(item.returns)
            if any(not _safe_annotation_expression(annotation) for annotation in annotations):
                return f"function '{item.name}' uses an executable annotation expression"
            if is_node and not item.name.startswith("_"):
                continue
            continue
        return (
            f"module-level {type(item).__name__} statements are not allowed in a Graphimo component"
        )
    return None


def _static_callable(
    source: str, function: ast.FunctionDef | ast.AsyncFunctionDef
) -> dict[str, Any]:
    positional = [*function.args.posonlyargs, *function.args.args]
    defaults = [None] * (len(positional) - len(function.args.defaults)) + list(
        function.args.defaults
    )
    parameters: list[dict[str, Any]] = []
    for arg, default in zip(positional, defaults, strict=True):
        has_default = default is not None
        parameter: dict[str, Any] = {
            "name": arg.arg,
            "required": not has_default,
            "positional": arg not in function.args.posonlyargs,
            "positional_only": arg in function.args.posonlyargs,
            "has_default": has_default,
        }
        if arg.annotation is not None:
            parameter["annotation"] = ast.unparse(arg.annotation)
        if has_default:
            safe, value = _literal_default(default)
            if safe and (value is None or type(value) in (bool, int, float, str)):
                parameter["default"] = value
        parameters.append(parameter)
    for arg, default in zip(function.args.kwonlyargs, function.args.kw_defaults, strict=True):
        has_default = default is not None
        parameter = {
            "name": arg.arg,
            "required": not has_default,
            "positional": False,
            "positional_only": False,
            "has_default": has_default,
        }
        if arg.annotation is not None:
            parameter["annotation"] = ast.unparse(arg.annotation)
        if has_default:
            safe, value = _literal_default(default)
            if safe and (value is None or type(value) in (bool, int, float, str)):
                parameter["default"] = value
        parameters.append(parameter)

    class ReturnCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.values: list[ast.expr | None] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is function:
                for statement in node.body:
                    self.visit(statement)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Return(self, node: ast.Return) -> None:
            self.values.append(node.value)

    returns = ReturnCollector()
    returns.visit(function)
    rows: list[tuple[ast.expr, ...]] = []
    for value in returns.values:
        if value is None or isinstance(value, ast.Constant) and value.value is None:
            rows.append(())
        elif isinstance(value, ast.Tuple):
            rows.append(tuple(value.elts))
        else:
            rows.append((value,))
    arities = {len(row) for row in rows}
    outputs: list[dict[str, Any]] = []
    diagnostic = None
    if len(arities) > 1:
        diagnostic = "node_output_arity_inconsistent"
    elif arities and next(iter(arities)):
        arity = next(iter(arities))
        annotation = function.returns
        tuple_types = None
        if isinstance(annotation, ast.Subscript):
            target = annotation.value
            if (isinstance(target, ast.Name) and target.id in {"tuple", "Tuple"}) or (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "typing"
                and target.attr == "Tuple"
            ):
                tuple_types = (
                    annotation.slice.elts
                    if isinstance(annotation.slice, ast.Tuple)
                    else [annotation.slice]
                )
        if tuple_types is not None and len(tuple_types) != arity:
            diagnostic = "node_return_annotation_arity_mismatch"
        for index in range(arity):
            expressions = list(
                dict.fromkeys(
                    ast.get_source_segment(source, row[index]) or ast.unparse(row[index])
                    for row in rows
                )
            )
            declared = (
                ast.unparse(tuple_types[index])
                if tuple_types is not None and len(tuple_types) == arity
                else ast.unparse(annotation)
                if annotation is not None
                else None
            )
            outputs.append(
                {
                    "index": index + 1,
                    "name": f"output_{index + 1}",
                    "declared_type": declared,
                    "annotation": declared,
                    "expressions": expressions,
                    **({"expression": expressions[0]} if len(expressions) == 1 else {}),
                }
            )
    doc = ast.get_docstring(function) or ""
    positional_defaults = {
        arg.arg: default
        for arg, default in zip(positional, defaults, strict=True)
        if default is not None
    }
    signature_parts = []
    for index, arg in enumerate(positional):
        part = arg.arg
        if arg.annotation is not None:
            part += ": " + ast.unparse(arg.annotation)
        if arg.arg in positional_defaults:
            part += " = " + ast.unparse(positional_defaults[arg.arg])
        signature_parts.append(part)
        if arg in function.args.posonlyargs and (index + 1 == len(function.args.posonlyargs)):
            signature_parts.append("/")
    if function.args.kwonlyargs:
        signature_parts.append("*")
        for arg, default in zip(function.args.kwonlyargs, function.args.kw_defaults, strict=True):
            part = arg.arg
            if arg.annotation is not None:
                part += ": " + ast.unparse(arg.annotation)
            if default is not None:
                part += " = " + ast.unparse(default)
            signature_parts.append(part)
    signature = f"{function.name}({', '.join(signature_parts)})"
    if function.returns is not None:
        signature += " -> " + ast.unparse(function.returns)
    return {
        "name": function.name,
        "signature": signature,
        "description": doc.splitlines()[0] if doc else "",
        "parameters": parameters,
        "base_block": {
            "inputs": parameters,
            "outputs": outputs,
            "output_mode": "tuple"
            if outputs and len(outputs) > 1
            else "single"
            if outputs
            else "empty",
        },
        "outputs": outputs,
        "diagnostic": diagnostic,
    }
