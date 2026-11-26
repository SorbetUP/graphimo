"""Validated project-local ``@node`` component source operations."""

from __future__ import annotations

import ast
import hashlib
import json
import keyword
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

MAX_COMPONENT_SOURCE_BYTES = 2 * 1024 * 1024
MAX_VALIDATION_OUTPUT_BYTES = 64 * 1024
_COMPONENT_ID = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.py$")
_NODE_VALIDATION = r"""
import importlib.util, inspect, json, pathlib, sys, traceback
from graphimo import inspect_node, is_node
path = pathlib.Path(sys.argv[1])
module_name = sys.argv[2]
project_root = sys.argv[3]
sys.path.insert(0, project_root)
try:
    package_path = [str(path.parent)] if path.name == "__init__.py" else None
    spec = importlib.util.spec_from_file_location(
        module_name, path, submodule_search_locations=package_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not create a module loader")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    nodes = []
    for name, value in vars(module).items():
        if name.startswith("_") or not is_node(value):
            continue
        contract = inspect_node(value)
        nodes.append({
            "name": name,
            "inputs": [
                {"name": item.name, "required": item.required,
                 "annotation": item.annotation, "has_default": item.has_default}
                for item in contract.inputs
            ],
            "outputs": [
                {"index": item.index, "declared_type": item.declared_type,
                 "expressions": list(item.expressions)}
                for item in contract.outputs
            ],
            "diagnostic": contract.diagnostic,
        })
    print(json.dumps({"nodes": nodes}, ensure_ascii=False, allow_nan=False))
except BaseException as exc:
    print(json.dumps({"error": type(exc).__name__ + ": " + str(exc)}, ensure_ascii=False))
    sys.exit(1)
"""


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
        missing = [
            item["name"] for item in inputs if item["required"] and item["name"] not in bindings
        ]
        if missing:
            raise ComponentError(f"missing required component input(s): {', '.join(missing)}")

        rendered: list[str] = []
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
                rendered.append(f"{name}={variable}")
            else:
                literal = json.dumps(value["literal"], ensure_ascii=False, allow_nan=False)
                if len(literal.encode("utf-8")) > 16 * 1024:
                    raise ComponentError(f"input {name!r} literal exceeds 16 KiB")
                rendered.append(f"{name}=__import__('json').loads({literal!r})")

        relative = Path(path)
        module_path = (
            relative.parent if relative.name == "__init__.py" else relative.with_suffix("")
        )
        module_rel = module_path.as_posix().replace("/", ".")
        module_name = "components" if module_rel in {"", "."} else f"components.{module_rel}"
        digest_builder = hashlib.sha256()
        if relative.name == "__init__.py":
            package_root = target.parent
            source_files = sorted(package_root.rglob("*.py"))
            for source_file in source_files:
                relative_source = source_file.relative_to(package_root).as_posix()
                digest_builder.update(relative_source.encode("utf-8"))
                digest_builder.update(source_file.read_bytes())
        else:
            digest_builder.update(target.read_bytes())
        digest = digest_builder.hexdigest()
        called = (
            f"__import__('importlib').reload("
            f"__import__('importlib').import_module({module_name!r})).{callable_name}"
        )
        source = (
            f"# Graphimo component: {path}\n"
            f"# Graphimo source sha256: {digest}\n"
            f"# Graphimo callable: {callable_name}\n"
            f"{output_name} = {called}({', '.join(rendered)})\n"
        )
        compile(source, "<graphimo-component-instance>", "exec")
        return {"source": source, "source_sha256": digest, "output_name": output_name}

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
                nodes = [
                    item.name
                    for item in tree.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not item.name.startswith("_")
                    and any(_is_node_decorator(decorator) for decorator in item.decorator_list)
                ]
                if nodes:
                    raw = source.encode("utf-8")
                    items.append(
                        {
                            "path": relative.as_posix(),
                            "nodes": nodes,
                            "sha256": hashlib.sha256(raw).hexdigest(),
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
            ast.parse(source, filename=relative_path)
        except SyntaxError as exc:
            return _invalid(f"Python syntax error at line {exc.lineno}: {exc.msg}")

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

        relative = Path(relative_path)
        package_parts = ["components", *relative.with_suffix("").parts]
        if package_parts[-1] == "__init__":
            package_parts.pop()
        module_name = ".".join(package_parts)
        result = _run_bounded(
            [
                sys.executable,
                "-c",
                _NODE_VALIDATION,
                str(candidate),
                module_name,
                str(self.project_path.parent),
            ],
            cwd=self.project_path.parent,
            timeout=8,
        )
        if result["returncode"] != 0:
            detail = _json_error(result["output"])
            return _invalid(
                detail or "component import or @node contract inspection failed",
                code="component_import_failed",
            )
        try:
            payload = json.loads(result["output"])
        except json.JSONDecodeError:
            return _invalid("component validation subprocess returned invalid JSON")
        nodes = payload.get("nodes")
        if not isinstance(nodes, list) or not nodes:
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


def _json_error(output: str) -> str | None:
    for line in reversed(output.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            return payload["error"][:2000]
    return output.strip()[:2000] or None


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


def _is_node_decorator(value: ast.expr) -> bool:
    if isinstance(value, ast.Call):
        value = value.func
    if isinstance(value, ast.Name):
        return value.id == "node"
    return isinstance(value, ast.Attribute) and value.attr == "node"
