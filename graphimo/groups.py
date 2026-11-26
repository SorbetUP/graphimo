"""Reusable marimo-backed Graphimo Group Blocks.

Group execution is a thin call into marimo's App.run(defs=...) API. This
module does not schedule Graphimo nodes or walk a dependency graph.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import keyword
import os
import re
import runpy
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

GROUP_FORMAT = 1
GROUP_DIALECT = "python"
GROUPS_DIRECTORY = "groups"
PINNED_MARIMO_VERSION = "0.25.2"
MAX_GROUP_FILE_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MANAGED_INPUTS = "__graphimo_inputs"
_INPUTS_END = "# Graphimo managed group inputs end."
_CALLABLE_BEGIN = "# Graphimo managed group callable begin."
_CALLABLE_END = "# Graphimo managed group callable end."


class GroupError(ValueError):
    """Raised when a Group Block definition or invocation is invalid."""


@dataclass(frozen=True)
class GroupContract:
    """Stable semantic contract for a reusable Group Block file."""

    group_id: str
    name: str
    module: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    dialect_id: str = GROUP_DIALECT

    def __post_init__(self) -> None:
        if isinstance(self.inputs, (str, bytes)) or not isinstance(self.inputs, Sequence):
            raise GroupError("Group inputs must be a sequence of Python names")
        if isinstance(self.outputs, (str, bytes)) or not isinstance(self.outputs, Sequence):
            raise GroupError("Group outputs must be a sequence of Python names")
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        if not isinstance(self.group_id, str) or not _ID.fullmatch(self.group_id):
            raise GroupError("group_id must contain 1-128 letters, digits, '_' or '-'")
        if not isinstance(self.name, str) or not self.name.strip():
            raise GroupError("Group name must be non-empty text")
        object.__setattr__(self, "name", self.name.strip())
        if (
            not isinstance(self.module, str)
            or not self.module.isidentifier()
            or keyword.iskeyword(self.module)
        ):
            raise GroupError("Group module must be a Python identifier")
        if self.dialect_id != GROUP_DIALECT:
            raise GroupError("generic Group Blocks must use the 'python' dialect")
        _validate_names(self.inputs, "input")
        _validate_names(self.outputs, "output")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": GROUP_FORMAT,
            "group_id": self.group_id,
            "name": self.name.strip(),
            "module": self.module,
            "dialect_id": self.dialect_id,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
        }


@dataclass(frozen=True)
class GroupDefinition:
    """A Group Block contract referenced by its project Python file."""

    contract: GroupContract
    path: Path
    source_sha256: str

    @property
    def relative_path(self) -> str:
        return f"{GROUPS_DIRECTORY}/{self.path.name}"

    @property
    def import_path(self) -> str:
        return f"{GROUPS_DIRECTORY}.{self.contract.module}"

    @property
    def callable_name(self) -> str:
        return self.contract.module

    def to_catalog_item(self) -> dict[str, Any]:
        value = self.contract.to_dict()
        value.update(
            {
                "path": self.relative_path,
                "import_path": self.import_path,
                "callable": self.callable_name,
                "source_sha256": self.source_sha256,
                "import_source": f"from {self.import_path} import {self.callable_name}",
            }
        )
        return value


class GroupResult(Mapping[str, Any]):
    """Opaque result object with stable named access to declared Group outputs."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any]):
        self._values = MappingProxyType(dict(values))

    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("_"):
            values = object.__getattribute__(self, "_values")
            if name in values:
                return values[name]
        return object.__getattribute__(self, name)

    def __getitem__(self, name: str) -> Any:
        return self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __repr__(self) -> str:
        return f"GroupResult({dict(self._values)!r})"


def _validate_names(values: Sequence[str], label: str) -> None:
    if isinstance(values, (str, bytes)):
        raise GroupError(f"Group {label}s must be a sequence of Python names")
    if not isinstance(values, Sequence) or any(not isinstance(value, str) for value in values):
        raise GroupError(f"Group {label}s must be a sequence of Python names")
    if len(values) != len(set(values)):
        raise GroupError(f"Group {label} names must be unique")
    for value in values:
        if (
            not isinstance(value, str)
            or not value.isidentifier()
            or keyword.iskeyword(value)
            or value.startswith("__graphimo_")
        ):
            raise GroupError(f"Group {label} name {value!r} must be a public Python identifier")


def groups_root(project_path: str | Path) -> Path:
    """Return the project's reference-based Group Block library directory."""
    return Path(project_path).expanduser().resolve().parent / GROUPS_DIRECTORY


def _group_source(contract: GroupContract, body_source: str = "") -> str:
    if not isinstance(body_source, str):
        raise GroupError("Group body source must be text")
    if len(body_source.encode("utf-8")) > MAX_GROUP_FILE_BYTES:
        raise GroupError("Group body source exceeds the 2 MiB limit")
    if "# Graphimo managed group" in body_source:
        raise GroupError("Group body source cannot replace Graphimo-managed declarations")

    metadata = json.dumps(contract.to_dict(), ensure_ascii=False, indent=2)
    lines = [
        '"""Graphimo Group Block; edit workflow cells below the managed input cell."""',
        "",
        "import marimo",
        "from graphimo import node",
        "from graphimo.groups import GroupResult, run_group",
        "",
        "# Graphimo managed group inputs begin.",
        f"GRAPHIMO_GROUP = {metadata}",
        "",
        "app = marimo.App()",
        "",
        "@app.cell",
        f"def {_MANAGED_INPUTS}():",
    ]
    if contract.inputs:
        lines.extend(f"    {name} = None" for name in contract.inputs)
        return_value = ", ".join(contract.inputs)
        if len(contract.inputs) == 1:
            return_value += ","
        lines.append(f"    return {return_value}")
    else:
        lines.append("    return ()")
    lines.extend(["", _INPUTS_END, ""])
    if body_source.strip():
        lines.extend([body_source.strip("\n"), ""])
    parameters = ", ".join(contract.inputs)
    signature = f"*, {parameters}" if parameters else ""
    bindings = ", ".join(f"{name!r}: {name}" for name in contract.inputs)
    lines.extend(
        [
            _CALLABLE_BEGIN,
            "@node",
            f"def {contract.module}({signature}) -> GroupResult:",
            "    return run_group(",
            "        __file__,",
            f"        inputs={{{bindings}}},",
            '        outputs=GRAPHIMO_GROUP["outputs"],',
            "    )",
            _CALLABLE_END,
            "",
        ]
    )
    return "\n".join(lines)


def _metadata_from_ast(path: Path, source: str) -> GroupContract:
    try:
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError) as exc:
        raise GroupError(f"Group file is not valid Python: {exc}") from exc
    metadata_values: list[Any] = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "GRAPHIMO_GROUP"
            for target in statement.targets
        ):
            metadata_values.append(statement.value)
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "GRAPHIMO_GROUP"
        ):
            metadata_values.append(statement.value)
    if len(metadata_values) != 1:
        raise GroupError("Group file must declare exactly one GRAPHIMO_GROUP contract")
    try:
        value = ast.literal_eval(metadata_values[0])
    except (ValueError, TypeError, SyntaxError) as exc:
        raise GroupError("GRAPHIMO_GROUP must contain literal JSON-compatible metadata") from exc
    if not isinstance(value, dict) or value.get("format") != GROUP_FORMAT:
        raise GroupError("unsupported Group contract format")
    try:
        contract = GroupContract(
            group_id=value["group_id"],
            name=value["name"],
            module=value["module"],
            dialect_id=value["dialect_id"],
            inputs=tuple(value["inputs"]),
            outputs=tuple(value["outputs"]),
        )
    except (KeyError, TypeError) as exc:
        raise GroupError(f"Group contract is incomplete: {exc}") from exc
    if contract.module != path.stem:
        raise GroupError("Group module name must match its Python filename")

    input_cells = [
        statement
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name == _MANAGED_INPUTS
    ]
    if len(input_cells) != 1 or isinstance(input_cells[0], ast.AsyncFunctionDef):
        raise GroupError("Group file must contain one managed input cell")
    input_cell = input_cells[0]
    if input_cell.args.args or input_cell.args.kwonlyargs:
        raise GroupError("managed Group input cell cannot take Python parameters")
    returns = [node for node in ast.walk(input_cell) if isinstance(node, ast.Return)]
    if len(returns) != 1:
        raise GroupError("managed Group input cell must return all declared inputs together")
    returned = returns[0].value
    if isinstance(returned, ast.Tuple):
        returned_names = tuple(item.id for item in returned.elts if isinstance(item, ast.Name))
        if len(returned_names) != len(returned.elts):
            raise GroupError("managed Group input cell must return named definitions")
    elif isinstance(returned, ast.Name):
        returned_names = (returned.id,)
    else:
        returned_names = ()
    if returned_names != contract.inputs:
        raise GroupError("managed Group input cell must match the declared input contract")
    return contract


def load_group_definition(path: str | Path) -> GroupDefinition:
    """Read a Group contract and managed input cell without importing its code."""
    group_path = Path(path).expanduser().resolve(strict=True)
    if group_path.suffix != ".py" or not group_path.is_file():
        raise GroupError("Group reference must point to a Python file")
    raw = group_path.read_bytes()
    if len(raw) > MAX_GROUP_FILE_BYTES:
        raise GroupError("Group file exceeds the 2 MiB limit")
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GroupError("Group file must be UTF-8 Python source") from exc
    contract = _metadata_from_ast(group_path, source)
    return GroupDefinition(contract, group_path, hashlib.sha256(raw).hexdigest())


def create_group_file(
    project_path: str | Path,
    *,
    module: str,
    name: str,
    inputs: Sequence[str],
    outputs: Sequence[str],
    body_source: str = "",
    group_id: str | None = None,
) -> GroupDefinition:
    """Create a new reference-based Group component with managed input bindings."""
    contract = GroupContract(
        group_id=group_id or uuid.uuid4().hex[:12],
        name=name,
        module=module,
        inputs=tuple(inputs),
        outputs=tuple(outputs),
    )
    root = groups_root(project_path)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{contract.module}.py"
    source = _group_source(contract, body_source)
    ast.parse(source, filename=str(path))
    try:
        init_path = root / "__init__.py"
        init_path.touch(exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(source)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except FileExistsError as exc:
        raise FileExistsError(f"Group module '{contract.module}' already exists") from exc
    except OSError as exc:
        raise GroupError(f"could not create Group file: {exc}") from exc
    return load_group_definition(path)


def update_group_contract(
    path: str | Path,
    *,
    name: str | None = None,
    inputs: Sequence[str] | None = None,
    outputs: Sequence[str] | None = None,
) -> GroupDefinition:
    """Update managed Group declarations while preserving workflow cell source."""
    definition = load_group_definition(path)
    source = definition.path.read_text(encoding="utf-8")
    inputs_end = source.find(_INPUTS_END)
    callable_begin = source.find(_CALLABLE_BEGIN)
    callable_end = source.find(_CALLABLE_END)
    if not (0 <= inputs_end < callable_begin < callable_end):
        raise GroupError("Group file is missing its managed contract boundaries")
    body = source[inputs_end + len(_INPUTS_END) : callable_begin].strip("\n")
    current = definition.contract
    contract = GroupContract(
        group_id=current.group_id,
        name=current.name if name is None else name,
        module=current.module,
        dialect_id=current.dialect_id,
        inputs=current.inputs if inputs is None else tuple(inputs),
        outputs=current.outputs if outputs is None else tuple(outputs),
    )
    updated = _group_source(contract, body)
    ast.parse(updated, filename=str(definition.path))
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{definition.path.name}.", dir=definition.path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, definition.path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return load_group_definition(definition.path)


class GroupLibrary:
    """Project-local reusable Group catalog and file reference manager."""

    def __init__(self, project_path: str | Path):
        self.root = groups_root(project_path)

    def list(self) -> dict[str, Any]:
        items = []
        diagnostics = []
        if not self.root.is_dir():
            return {"items": items, "diagnostics": diagnostics}
        for path in sorted(self.root.glob("*.py")):
            if path.name == "__init__.py":
                continue
            try:
                if path.is_symlink() or path.resolve().parent != self.root.resolve():
                    raise GroupError(
                        "Group files must be regular files in the project groups folder"
                    )
                items.append(load_group_definition(path).to_catalog_item())
            except (OSError, GroupError) as exc:
                diagnostics.append(
                    {
                        "code": "group_definition_invalid",
                        "message": f"{path.name}: {exc}",
                        "severity": "warning",
                    }
                )
        return {"items": items, "diagnostics": diagnostics}

    def get(self, group_id: str) -> GroupDefinition:
        matches = [item for item in self.list()["items"] if item.get("group_id") == group_id]
        if len(matches) > 1:
            raise GroupError(f"multiple Group files declare ID '{group_id}'")
        if not matches:
            raise FileNotFoundError(f"Group '{group_id}' does not exist")
        module = matches[0]["module"]
        path = self.root / f"{module}.py"
        return load_group_definition(path)

    def create(self, request: Mapping[str, Any]) -> GroupDefinition:
        if not isinstance(request, Mapping):
            raise GroupError("Group creation request must be an object")
        return create_group_file(
            self.root.parent / "project.py",
            module=request.get("module"),
            name=request.get("name"),
            inputs=request.get("inputs", ()),
            outputs=request.get("outputs", ()),
            body_source=request.get("body_source", ""),
            group_id=request.get("group_id"),
        )

    def update_contract(self, group_id: str, request: Mapping[str, Any]) -> GroupDefinition:
        if not isinstance(request, Mapping):
            raise GroupError("Group contract update must be an object")
        current = self.get(group_id)
        requested_id = request.get("group_id", group_id)
        if requested_id != group_id:
            raise GroupError("Group identity is stable and cannot be changed")
        return update_group_contract(
            current.path,
            name=request.get("name"),
            inputs=request.get("inputs"),
            outputs=request.get("outputs"),
        )


def run_group(
    group_path: str | Path,
    *,
    inputs: Mapping[str, Any],
    outputs: Sequence[str] | None = None,
) -> GroupResult:
    """Run one reusable Group Block using an isolated clone of its marimo app."""
    definition = load_group_definition(group_path)
    contract = definition.contract
    if not isinstance(inputs, Mapping):
        raise GroupError("Group inputs must be a mapping of declared input names to values")
    input_values = dict(inputs)
    if set(input_values) != set(contract.inputs):
        missing = sorted(set(contract.inputs) - set(input_values))
        extra = sorted(set(input_values) - set(contract.inputs))
        raise GroupError(
            f"Group input bindings do not match contract (missing={missing}, extra={extra})"
        )
    declared_outputs = contract.outputs
    selected_outputs = declared_outputs if outputs is None else tuple(outputs)
    _validate_names(selected_outputs, "output")
    if selected_outputs != declared_outputs:
        raise GroupError("requested Group outputs must match its declared output contract")

    try:
        installed_marimo = importlib.metadata.version("marimo")
    except importlib.metadata.PackageNotFoundError as exc:
        raise GroupError(f"Group execution requires marimo=={PINNED_MARIMO_VERSION}") from exc
    if installed_marimo != PINNED_MARIMO_VERSION:
        raise GroupError(
            f"Group execution requires marimo=={PINNED_MARIMO_VERSION}; found {installed_marimo}"
        )

    namespace = runpy.run_path(str(definition.path), run_name=f"_graphimo_group_{uuid.uuid4().hex}")
    if namespace.get("GRAPHIMO_GROUP") != contract.to_dict():
        raise GroupError("Group runtime contract differs from its saved file contract")
    app = namespace.get("app")
    if not callable(getattr(app, "clone", None)) or not callable(getattr(app, "run", None)):
        raise GroupError("Group file must expose a marimo.App named 'app'")
    group_app = app.clone()
    try:
        _visual_outputs, definitions = group_app.run(defs=input_values)
    except Exception as exc:
        raise GroupError(f"Group '{contract.name}' execution failed: {exc}") from exc
    missing_outputs = [name for name in selected_outputs if name not in definitions]
    if missing_outputs:
        raise GroupError(
            f"Group '{contract.name}' did not define declared outputs: "
            + ", ".join(missing_outputs)
        )
    return GroupResult({name: definitions[name] for name in selected_outputs})
