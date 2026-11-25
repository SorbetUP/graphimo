"""Reusable Group Blocks backed by ordinary marimo applications.

Groups are references to Python files, not copied source and not a second
Graphimo scheduler. The only execution operation here is ``App.run``.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import keyword
import secrets
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphimo.instance import Binding, _parse_expression


@dataclass(frozen=True)
class GroupPort:
    name: str
    definition: str
    type: str | None = None
    required: bool = True
    default: Any = None

    def __post_init__(self) -> None:
        for value, label in ((self.name, "port name"), (self.definition, "definition")):
            if not isinstance(value, str) or not value.isidentifier() or keyword.iskeyword(value):
                raise ValueError(f"Group {label} must be a valid Python identifier")
        if self.type is not None and not isinstance(self.type, str):
            raise ValueError("Group port type must be a string or None")
        if type(self.required) is not bool:
            raise ValueError("Group port required must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "definition": self.definition,
            "type": self.type,
            "required": self.required,
            "default": self.default,
        }


@dataclass(frozen=True)
class GroupContract:
    id: str
    name: str
    dialect_id: str
    inputs: tuple[GroupPort, ...]
    outputs: tuple[GroupPort, ...]
    layout: Mapping[str, Any]
    path: Path


class GroupResult(Mapping[str, Any]):
    """Opaque multi-output result with stable mapping and attribute access."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        if not isinstance(values, Mapping) or not values:
            raise ValueError("GroupResult requires a non-empty output mapping")
        copied = dict(values)
        if any(not isinstance(key, str) or not key.isidentifier() for key in copied):
            raise ValueError("GroupResult output names must be Python identifiers")
        object.__setattr__(self, "_values", copied)

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("_"):
            values = object.__getattribute__(self, "_values")
            if name in values:
                return values[name]
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("GroupResult is immutable")


@dataclass(frozen=True)
class GroupInstanceSpec:
    group_id: str
    instance_id: str
    result_var: str
    path: str
    bindings: tuple[Binding, ...]
    outputs: tuple[str, ...]


def build_group_instance_source(
    contract: GroupContract,
    bindings: Mapping[str, str],
    *,
    path: str | Path | None = None,
    instance_id: str | None = None,
    outputs: tuple[str, ...] | None = None,
    allow_incomplete: bool = False,
) -> tuple[str, GroupInstanceSpec]:
    if not isinstance(bindings, Mapping):
        raise TypeError("Group bindings must map input names to Python expressions")
    ports = {port.name: port for port in contract.inputs}
    unknown = set(bindings).difference(ports)
    missing = {name for name, port in ports.items() if port.required and name not in bindings}
    if unknown:
        raise ValueError(f"Unknown Group input(s): {', '.join(sorted(unknown))}")
    if missing and not allow_incomplete:
        raise ValueError(f"Missing required Group input(s): {', '.join(sorted(missing))}")
    selected_outputs = (
        tuple(port.name for port in contract.outputs) if outputs is None else tuple(outputs)
    )
    if not selected_outputs or len(set(selected_outputs)) != len(selected_outputs):
        raise ValueError("Group outputs must be a non-empty sequence of unique names")
    if set(selected_outputs).difference(port.name for port in contract.outputs):
        raise ValueError("Group instance requests an undeclared output")
    chosen_id = secrets.token_hex(4) if instance_id is None else instance_id
    if (
        not isinstance(chosen_id, str)
        or len(chosen_id) != 8
        or any(ch not in "0123456789abcdef" for ch in chosen_id)
    ):
        raise ValueError("Group instance ID must be eight lowercase hexadecimal characters")
    fragment = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in contract.name)
    if not fragment or fragment[0].isdigit():
        fragment = f"group_{fragment}"
    if keyword.iskeyword(fragment):
        fragment += "_"
    result_var = f"g_{fragment}_{chosen_id}"
    group_path = str(Path(path or contract.path).expanduser().resolve())
    bindings_tuple = tuple(
        Binding(name, _parse_expression(value) and value.strip())
        for name, value in bindings.items()
    )
    args = "\n".join(
        f"        {binding.input_name!r}: {binding.expression}," for binding in bindings_tuple
    )
    source = (
        f"# Graphimo group: {contract.id}\n"
        f"# Graphimo group instance: {chosen_id}\n"
        f"{result_var} = graphimo.groups.run_group(\n"
        f"    {group_path!r},\n"
        "    inputs={\n"
        f"{args}\n"
        "    },\n"
        f"    outputs={selected_outputs!r},\n"
        ")"
    )
    return source, GroupInstanceSpec(
        contract.id, chosen_id, result_var, group_path, bindings_tuple, selected_outputs
    )


def parse_group_instance_source(source: str) -> GroupInstanceSpec | None:
    if not isinstance(source, str):
        return None
    lines = source.splitlines()
    group_markers = [line for line in lines if line.startswith("# Graphimo group:")]
    instance_markers = [line for line in lines if line.startswith("# Graphimo group instance:")]
    if len(group_markers) != 1 or len(instance_markers) != 1:
        return None
    group_id = group_markers[0].removeprefix("# Graphimo group:").strip()
    instance_id = instance_markers[0].removeprefix("# Graphimo group instance:").strip()
    if (
        not group_id
        or len(instance_id) != 8
        or any(ch not in "0123456789abcdef" for ch in instance_id)
    ):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
        return None
    assignment = tree.body[0]
    if (
        len(assignment.targets) != 1
        or not isinstance(assignment.targets[0], ast.Name)
        or not isinstance(assignment.value, ast.Call)
    ):
        return None
    call = assignment.value
    function = call.func
    if (
        not (
            isinstance(function, ast.Attribute)
            and function.attr == "run_group"
            and isinstance(function.value, ast.Attribute)
            and function.value.attr == "groups"
            and isinstance(function.value.value, ast.Name)
            and function.value.value.id == "graphimo"
        )
        or len(call.args) != 1
    ):
        return None
    try:
        group_path = ast.literal_eval(call.args[0])
        keywords = {item.arg: item.value for item in call.keywords if item.arg is not None}
        if len(keywords) != len(call.keywords) or set(keywords) != {"inputs", "outputs"}:
            return None
        outputs = ast.literal_eval(keywords["outputs"])
        input_node = keywords["inputs"]
        if (
            not isinstance(group_path, str)
            or not isinstance(input_node, ast.Dict)
            or not isinstance(outputs, tuple)
        ):
            return None
        bindings_list = []
        for key_node, expression in zip(input_node.keys, input_node.values):
            name = ast.literal_eval(key_node)
            if not isinstance(name, str):
                return None
            bindings_list.append(
                Binding(name, ast.get_source_segment(source, expression) or ast.unparse(expression))
            )
        bindings = tuple(bindings_list)
        for binding in bindings:
            _parse_expression(binding.expression)
    except (ValueError, TypeError, SyntaxError):
        return None
    result_var = assignment.targets[0].id
    return GroupInstanceSpec(group_id, instance_id, result_var, group_path, bindings, outputs)


def group_output_expression(instance: GroupInstanceSpec, output_name: str) -> str:
    if output_name not in instance.outputs:
        raise ValueError(f"Group instance has no output named {output_name!r}")
    return f"{instance.result_var}.{output_name}"


def reconnect_group_instance_source(source: str, *, input_name: str, expression: str) -> str:
    instance = parse_group_instance_source(source)
    if instance is None:
        raise ValueError("Source is not a Graphimo Group instance")
    _parse_expression(expression)
    bindings = {item.input_name: item.expression for item in instance.bindings}
    if input_name not in bindings:
        raise ValueError(f"Group instance has no bound input named {input_name!r}")
    bindings[input_name] = expression.strip()
    contract = load_group_contract(instance.path)
    updated, _ = build_group_instance_source(
        contract,
        bindings,
        instance_id=instance.instance_id,
        outputs=instance.outputs,
        allow_incomplete=True,
    )
    return updated


def disconnect_group_instance_source(source: str, *, input_name: str) -> str:
    instance = parse_group_instance_source(source)
    if instance is None:
        raise ValueError("Source is not a Graphimo Group instance")
    bindings = {item.input_name: item.expression for item in instance.bindings}
    if input_name not in bindings:
        raise ValueError(f"Group instance has no bound input named {input_name!r}")
    del bindings[input_name]
    contract = load_group_contract(instance.path)
    updated, _ = build_group_instance_source(
        contract,
        bindings,
        instance_id=instance.instance_id,
        outputs=instance.outputs,
        allow_incomplete=True,
    )
    return updated


def create_group_file(
    path: str | Path,
    *,
    group_id: str,
    name: str,
    inputs: tuple[GroupPort, ...] | list[GroupPort] = (),
    outputs: tuple[GroupPort, ...] | list[GroupPort],
    dialect_id: str = "python",
) -> GroupContract:
    """Create a reusable marimo file with Graphimo-owned contract cells."""
    target = Path(path).expanduser().resolve()
    if target.suffix != ".py":
        raise ValueError("Group files must use the .py extension")
    if target.exists():
        raise FileExistsError(f"Group file already exists: {target}")
    if (
        not isinstance(group_id, str)
        or not group_id
        or not isinstance(name, str)
        or not name.strip()
    ):
        raise ValueError("Group id and name must be non-empty strings")
    if not isinstance(dialect_id, str) or not dialect_id:
        raise ValueError("Group dialect id must be non-empty")
    input_ports = tuple(inputs)
    output_ports = tuple(outputs)
    _validate_ports(input_ports, "input")
    _validate_ports(output_ports, "output")
    if not output_ports:
        raise ValueError("A Group must declare at least one output")
    if any(port.name.startswith("_") for port in output_ports):
        raise ValueError(
            "Group output names must be public identifiers for stable attribute access"
        )
    if len({port.definition for port in input_ports}) != len(input_ports):
        raise ValueError("Group input definitions must be unique")
    if len({port.definition for port in output_ports}) != len(output_ports):
        raise ValueError("Group output definitions must be unique")
    metadata = {
        "schema_version": 1,
        "id": group_id,
        "name": name.strip(),
        "dialect_id": dialect_id,
        "inputs": [port.to_dict() for port in input_ports],
        "outputs": [port.to_dict() for port in output_ports],
        "layout": {},
    }
    _ensure_json_value(metadata, "Group contract")
    input_defs = [port.definition for port in input_ports]
    input_assignments = "\n".join(f"    {definition} = None" for definition in input_defs)
    input_return = ", ".join(input_defs)
    if len(input_defs) == 1:
        input_return += ","
    input_cell = (
        "@app.cell\n"
        "def __graphimo_inputs():\n"
        f"{input_assignments or '    pass'}\n"
        f"    return ({input_return})"
        if input_defs
        else "@app.cell\ndef __graphimo_inputs():\n    return ()"
    )
    output_values = ",\n".join(f"        {port.name!r}: {port.definition}" for port in output_ports)
    output_return = (
        "@app.cell\n"
        "def __graphimo_outputs(" + ", ".join(port.definition for port in output_ports) + "):\n"
        "    from graphimo.groups import GroupResult\n"
        "    graphimo_group_result = GroupResult({\n"
        f"{output_values}\n"
        "    })\n"
        "    return (graphimo_group_result,)"
    )
    source = (
        "import marimo\n"
        "from graphimo.groups import GroupResult\n\n"
        f"__graphimo_group__ = {metadata!r}\n\n"
        "app = marimo.App()\n\n"
        f"{input_cell}\n\n"
        "# Add normal Graphimo/marimo cells below.\n\n"
        f"{output_return}\n"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return load_group_contract(target)


def load_group_contract(path: str | Path) -> GroupContract:
    """Read and validate contract metadata and managed cells without execution."""
    target = Path(path).expanduser().resolve()
    if target.suffix != ".py" or not target.is_file():
        raise ValueError("Group file must be an existing .py file")
    try:
        source = target.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(target))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise ValueError(f"Could not read Group file: {exc}") from exc
    metadata = None
    for statement in tree.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == "__graphimo_group__"
        ):
            if metadata is not None:
                raise ValueError("Group file has duplicate contract metadata")
            try:
                metadata = ast.literal_eval(statement.value)
            except (ValueError, TypeError) as exc:
                raise ValueError("Group contract metadata must be a Python literal") from exc
    if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
        raise ValueError("Group contract metadata is missing or has an unsupported version")
    _ensure_json_value(metadata, "Group contract")
    group_id, name, dialect_id = (metadata.get(key) for key in ("id", "name", "dialect_id"))
    if not all(isinstance(item, str) and item for item in (group_id, name, dialect_id)):
        raise ValueError("Group contract requires non-empty id, name, and dialect_id")
    inputs = _ports_from_metadata(metadata.get("inputs"), "input")
    outputs = _ports_from_metadata(metadata.get("outputs"), "output")
    if not outputs:
        raise ValueError("Group contract must declare at least one output")
    if any(port.name.startswith("_") for port in outputs):
        raise ValueError(
            "Group output names must be public identifiers for stable attribute access"
        )
    layout = metadata.get("layout", {})
    if not isinstance(layout, dict):
        raise ValueError("Group layout metadata must be an object")
    _validate_managed_cell(
        tree, "__graphimo_inputs", [port.definition for port in inputs], is_input=True
    )
    _validate_managed_cell(
        tree, "__graphimo_outputs", [port.definition for port in outputs], is_input=False
    )
    return GroupContract(group_id, name, dialect_id, inputs, outputs, dict(layout), target)


def is_group_managed_cell(source: str) -> bool:
    """Return whether source is one of the two reserved Group boundary cells."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, TypeError):
        return False
    return any(
        isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name in {"__graphimo_inputs", "__graphimo_outputs"}
        for statement in tree.body
    )


def run_group(
    path: str | Path,
    *,
    inputs: Mapping[str, Any],
    outputs: tuple[str, ...] | list[str] | None = None,
) -> GroupResult:
    """Execute a referenced Group by cloning its marimo App and calling App.run."""
    contract = load_group_contract(path)
    if not isinstance(inputs, Mapping):
        raise TypeError("Group inputs must be a mapping")
    declared_inputs = {port.name: port for port in contract.inputs}
    unknown = set(inputs).difference(declared_inputs)
    missing = {
        name for name, port in declared_inputs.items() if port.required and name not in inputs
    }
    if unknown:
        raise ValueError(f"Unknown Group input(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"Missing required Group input(s): {', '.join(sorted(missing))}")
    selected_outputs = (
        tuple(port.name for port in contract.outputs) if outputs is None else tuple(outputs)
    )
    declared_outputs = {port.name for port in contract.outputs}
    if not selected_outputs or len(set(selected_outputs)) != len(selected_outputs):
        raise ValueError("Group outputs must be a non-empty sequence of unique names")
    unknown_outputs = set(selected_outputs).difference(declared_outputs)
    if unknown_outputs:
        raise ValueError(f"Unknown Group output(s): {', '.join(sorted(unknown_outputs))}")
    values = {port.definition: inputs.get(port.name, port.default) for port in contract.inputs}
    module_name = f"_graphimo_group_{secrets.token_hex(8)}"
    spec = importlib.util.spec_from_file_location(module_name, contract.path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not load Group file {contract.path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        app = getattr(module, "app", None)
        if (
            app is None
            or not callable(getattr(app, "run", None))
            or not callable(getattr(app, "clone", None))
        ):
            raise ValueError("Group file must define a marimo App named 'app' with clone()")
        isolated_app = app.clone()
        _, definitions = isolated_app.run(defs=values)
        result = definitions.get("graphimo_group_result")
        if not isinstance(result, GroupResult):
            raise ValueError("Group output cell did not produce a Graphimo GroupResult")
        if set(result) != declared_outputs:
            raise ValueError("Group output cell does not match the declared output contract")
        return GroupResult({name: result[name] for name in selected_outputs})
    finally:
        sys.modules.pop(module_name, None)


def _validate_ports(ports: tuple[GroupPort, ...], label: str) -> None:
    if any(not isinstance(port, GroupPort) for port in ports):
        raise TypeError(f"Group {label}s must be GroupPort values")
    names = [port.name for port in ports]
    if len(names) != len(set(names)):
        raise ValueError(f"Group {label} port names must be unique")


def _ports_from_metadata(value: Any, label: str) -> tuple[GroupPort, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Group {label}s must be an array")
    try:
        ports = tuple(
            GroupPort(
                name=item["name"],
                definition=item.get("definition", item["name"]),
                type=item.get("type"),
                required=item.get("required", label == "input"),
                default=item.get("default"),
            )
            for item in value
            if isinstance(item, dict)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid Group {label} port metadata: {exc}") from exc
    if len(ports) != len(value):
        raise ValueError(f"Invalid Group {label} port metadata")
    _validate_ports(ports, label)
    return ports


def _validate_managed_cell(
    tree: ast.Module, function_name: str, definitions: list[str], *, is_input: bool
) -> None:
    matches = []
    for statement in tree.body:
        if (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement.name == function_name
        ):
            matches.append(statement)
    if len(matches) != 1:
        raise ValueError(f"Group must contain exactly one managed {function_name} cell")
    function = matches[0]
    decorators = [
        item
        for item in function.decorator_list
        if (isinstance(item, ast.Attribute) and item.attr == "cell")
        or (
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and item.func.attr == "cell"
        )
    ]
    if len(decorators) != 1:
        raise ValueError(f"Group {function_name} must be a marimo cell")
    returned = next(
        (item for item in reversed(function.body) if isinstance(item, ast.Return)), None
    )
    if returned is None or not isinstance(returned.value, (ast.Tuple, ast.List)):
        raise ValueError(f"Group {function_name} must return all managed definitions")
    returned_names = [item.id for item in returned.value.elts if isinstance(item, ast.Name)]
    if is_input:
        assigned_names = [
            target.id
            for item in function.body
            if isinstance(item, ast.Assign)
            for target in item.targets
            if isinstance(target, ast.Name)
        ]
        if assigned_names != definitions or returned_names != definitions:
            raise ValueError("Group input cell definitions do not match its port contract")
    else:
        argument_names = [item.arg for item in function.args.args]
        if argument_names != definitions:
            raise ValueError("Group output cell definitions do not match its port contract")
        if returned_names != ["graphimo_group_result"]:
            raise ValueError("Group output cell must return graphimo_group_result")
        result_assignments = [
            item
            for item in function.body
            if isinstance(item, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "graphimo_group_result"
                for target in item.targets
            )
        ]
        if len(result_assignments) != 1 or not isinstance(result_assignments[0].value, ast.Call):
            raise ValueError(
                "Group output cell must build graphimo_group_result from declared outputs"
            )
        result_call = result_assignments[0].value
        if (
            not isinstance(result_call.func, ast.Name)
            or result_call.func.id != "GroupResult"
            or len(result_call.args) != 1
            or not isinstance(result_call.args[0], ast.Dict)
        ):
            raise ValueError("Group output cell must use GroupResult with a named output mapping")
        mapping = result_call.args[0]
        try:
            mapping_keys = [ast.literal_eval(item) for item in mapping.keys]
        except (ValueError, TypeError) as exc:
            raise ValueError("Group output names must be literal strings") from exc
        mapping_values = [
            item.id if isinstance(item, ast.Name) else None for item in mapping.values
        ]
        if (
            mapping_keys != [port.name for port in _contract_outputs(tree)]
            or mapping_values != definitions
        ):
            raise ValueError("Group output cell mapping does not match its port contract")


def _contract_outputs(tree: ast.Module) -> tuple[GroupPort, ...]:
    for statement in tree.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == "__graphimo_group__"
        ):
            value = ast.literal_eval(statement.value)
            return _ports_from_metadata(value.get("outputs"), "output")
    raise ValueError("Group contract metadata is missing")


def _ensure_json_value(value: Any, label: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain JSON-compatible values") from exc
