"""Host-independent application/controller seam shared by Graphimo renderers.

The application reads notebook truth from an injected marimo port. It owns
Graphimo commands and view projection; renderers receive view models and send
semantic intents back through this class.
"""

from __future__ import annotations

import ast
import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import graphimo
from graphimo.controls import (
    CONTROL_DEFAULTS,
    build_control_source,
    control_output_type,
    parse_control_source,
    validate_control_value,
)
from graphimo.errors import ApplicationError
from graphimo.instance import (
    build_instance_source,
    disable_instance_source,
    enable_instance_source,
    output_expression,
    parse_disabled_instance_source,
    parse_instance_source,
    reconnect_instance_source,
)
from graphimo.groups import (
    GroupContract,
    build_group_instance_source,
    disconnect_group_instance_source,
    group_output_expression,
    is_group_managed_cell,
    load_group_contract,
    parse_group_instance_source,
    reconnect_group_instance_source,
)
from graphimo.node import InputPort, NodeSpec, OutputPort
from graphimo.metablock_application import MetaBlockApplicationMixin
from graphimo.metablocks import discover_metablock_addons, load_metablock_registration


class MarimoApplicationPort(Protocol):
    def list_active_notebooks(self) -> list[dict[str, Any]]: ...
    def inspect(self, session_id: str) -> dict[str, Any]: ...
    def add_cell(self, session_id: str, source: str) -> str: ...
    def replace_cell_source(self, session_id: str, cell_id: str, source: str) -> Any: ...
    def run_cell(self, session_id: str, cell_id: str) -> Any: ...
    def delete_cell(self, session_id: str, cell_id: str) -> Any: ...
    def get_variable(self, session_id: str, variable_name: str) -> Any: ...


@dataclass(frozen=True)
class ScopeCrumb:
    id: str
    title: str


@dataclass(frozen=True)
class PortView:
    id: str
    name: str
    direction: str
    declared_type: str | None = None
    runtime_type: str | None = None
    expression: str | None = None
    preview: str | None = None
    connected: bool = False
    compatible_hint: str | None = None


@dataclass(frozen=True)
class EdgeView:
    id: str
    source_block_id: str
    target_block_id: str
    variable: str
    state: str = "valid"


@dataclass(frozen=True)
class BlockView:
    id: str
    kind: str
    title: str
    subtitle: str | None
    x: float
    y: float
    width: float
    height: float
    state: str
    inputs: tuple[PortView, ...]
    outputs: tuple[PortView, ...]
    body: dict[str, Any]
    badges: tuple[str, ...]
    can_open: bool
    can_edit_source: bool
    can_disable: bool
    can_enable: bool = False


@dataclass(frozen=True)
class PaletteItem:
    id: str
    title: str
    category: str
    kind: str
    description: str | None
    search_terms: tuple[str, ...]
    source_provider: str
    icon_hint: str | None = None
    enabled: bool = True
    disabled_reason: str | None = None
    input_schema: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ScopePalette:
    scope_id: str
    dialect_id: str
    items: tuple[PaletteItem, ...]


@dataclass(frozen=True)
class DiagnosticView:
    code: str
    message: str
    block_id: str | None = None


@dataclass(frozen=True)
class ProjectRunState:
    state: str
    running_block_id: str | None = None


@dataclass(frozen=True)
class GraphimoViewState:
    project_id: str
    active_scope_id: str
    breadcrumbs: tuple[ScopeCrumb, ...]
    blocks: tuple[BlockView, ...]
    edges: tuple[EdgeView, ...]
    palette: ScopePalette
    selection: tuple[str, ...]
    run_state: ProjectRunState
    diagnostics: tuple[DiagnosticView, ...]
    available_actions: tuple[str, ...]


class GraphimoApplication(MetaBlockApplicationMixin):
    """Application/controller that exposes Graphimo operations over marimo."""

    def __init__(
        self,
        marimo: MarimoApplicationPort,
        components_root: str | os.PathLike[str],
        *,
        layout_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.marimo = marimo
        self.components_root = Path(components_root).resolve()
        self.layout_path = Path(layout_path) if layout_path is not None else None
        self._session_id: str | None = None
        self._project_path: str | None = None
        self._selection: tuple[str, ...] = ()
        self._scope_stack: list[tuple[str, str, str, str]] = []
        self._init_metablocks()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def project_path(self) -> str | None:
        return self._project_path

    @property
    def available_actions(self) -> tuple[str, ...]:
        return (
            "open_project", "close_project", "refresh_project", "add_palette_item",
            "delete_elements", "duplicate_elements", "move_elements", "connect",
            "reconnect", "disconnect", "set_literal", "run_target", "open_inspector",
            "disable_instance", "enable_instance", "inspect_artifact_store",
            "set_control_value",
            "list_metablock_addons",
            "create_metablock", "metablock_catalog", "project_metablock", "apply_metablock",
            "build_metablock_materialization", "materialize_metablock", "invoke_metablock",
            "drain_metablock_events",
            "enter_group", "leave_scope",
        )

    def inspect_artifact_store(self) -> Any:
        """Return native store usage without exposing CAS paths to the renderer."""
        return graphimo.artifacts.stats()

    def close_project(self) -> None:
        self._session_id = None
        self._project_path = None
        self._selection = ()
        self._scope_stack = []
        self._close_metablocks()

    def open_project(self, path: str | os.PathLike[str], session_id: str | None = None) -> GraphimoViewState:
        requested_path = str(Path(path).expanduser().resolve())
        sessions = self.marimo.list_active_notebooks()
        matching = [
            item
            for item in sessions
            if session_id == item.get("session_id")
            or (session_id is None and item.get("path") and str(Path(item["path"]).expanduser().resolve()) == requested_path)
        ]
        if len(matching) != 1:
            code = "session_not_found" if not matching else "session_selection_required"
            raise ApplicationError(code, "A unique active marimo session for this project is required")
        self._project_path = requested_path
        self._session_id = matching[0]["session_id"]
        self._selection = ()
        self._scope_stack = [("root", Path(requested_path).name or "Project", self._session_id, requested_path)]
        return self.refresh_project()

    def enter_group(self, group_path: str, session_id: str | None = None) -> GraphimoViewState:
        """Open and enter a Group file through the injected marimo application port."""
        contract = self._resolve_group_path(group_path)
        sessions = self.marimo.list_active_notebooks()
        matching = [
            item for item in sessions
            if item.get("path")
            and str(Path(item["path"]).expanduser().resolve()) == str(contract.path)
            and (session_id is None or item.get("session_id") == session_id)
        ]
        if len(matching) != 1:
            opener = getattr(self.marimo, "open_notebook", None)
            if not callable(opener):
                raise ApplicationError("group_session_required", "The Group file cannot be opened by this marimo host")
            opened = opener(str(contract.path))
            opened_id = opened.get("session_id") if isinstance(opened, dict) else opened
            matching = [item for item in self.marimo.list_active_notebooks() if item.get("session_id") == opened_id and str(Path(item.get("path", "")).expanduser().resolve()) == str(contract.path)]
        if len(matching) != 1:
            raise ApplicationError("group_session_not_found", "Could not select a unique active marimo session for this Group")
        if not self._scope_stack and self._session_id is not None and self._project_path is not None:
            self._scope_stack.append(("root", Path(self._project_path).name or "Project", self._session_id, self._project_path))
        self._scope_stack.append((contract.id, contract.name, matching[0]["session_id"], str(contract.path)))
        self._session_id = matching[0]["session_id"]
        self._project_path = str(contract.path)
        self._selection = ()
        return self.refresh_project()

    def leave_scope(self) -> GraphimoViewState:
        if len(self._scope_stack) <= 1:
            raise ApplicationError("root_scope", "The root Graphimo scope has no parent")
        self._scope_stack.pop()
        _, _, self._session_id, self._project_path = self._scope_stack[-1]
        self._selection = ()
        return self.refresh_project()

    def refresh_project(self) -> GraphimoViewState:
        session_id = self._require_session()
        snapshot = self.marimo.inspect(session_id)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("cells"), list):
            raise ApplicationError("marimo_unavailable", "Marimo returned an invalid notebook snapshot")
        return self._view_state(snapshot)

    def select(self, ids: tuple[str, ...] | list[str]) -> GraphimoViewState:
        state = self.refresh_project()
        existing = {block.id for block in state.blocks}
        self._selection = tuple(item for item in dict.fromkeys(ids) if item in existing)
        return self._view_state(self.marimo.inspect(self._require_session()))

    def add_node(
        self,
        component_id: str | None,
        node_name: str,
        bindings: dict[str, str],
        *,
        position: tuple[float, float] = (80.0, 80.0),
    ) -> dict[str, Any]:
        if len(position) != 2 or not all(type(value) in (int, float) and math.isfinite(value) for value in position):
            raise ApplicationError("invalid_position", "Block position must contain two finite numbers")
        spec = self._node_spec(component_id, node_name)
        if spec.diagnostic:
            raise ApplicationError(spec.diagnostic, f"Node '{node_name}' has an invalid output contract")
        for port in spec.inputs:
            if port.required and port.name not in bindings:
                raise ApplicationError("missing_required_binding", f"Required input '{port.name}' has no connection or value")
        if component_id is not None:
            importer = getattr(self.marimo, "ensure_node_available", None)
            if callable(importer):
                component_path = self._safe_component_path(component_id)
                relative_module = (
                    component_path.parent.relative_to(self.components_root)
                    if component_path.name == "__init__.py"
                    else component_path.relative_to(self.components_root).with_suffix("")
                )
                module_name = ".".join(("components", *relative_module.parts))
                try:
                    importer(self._require_session(), module_name, node_name, str(self.components_root.parent))
                except (KeyError, ValueError, RuntimeError) as exc:
                    raise ApplicationError("node_import_failed", str(exc)) from exc
        try:
            source, instance = build_instance_source(node_name, bindings, node_spec=spec)
            cell_id = self.marimo.add_cell(self._require_session(), source)
        except (TypeError, ValueError) as exc:
            raise ApplicationError("invalid_request", str(exc)) from exc
        self._save_positions({cell_id: {"x": position[0], "y": position[1]}})
        return {"cell_id": cell_id, "instance_id": instance.instance_id, "result_var": instance.result_var, "source": source}

    def add_palette_item(
        self,
        item_id: str,
        *,
        position: tuple[float, float] = (80.0, 80.0),
        bindings: dict[str, str] | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parts = item_id.split(":", 2)
        if len(parts) == 2 and parts[0] == "control":
            return self.add_control(parts[1], parameters or {}, position=position)
        if len(parts) == 2 and parts[0] == "group":
            return self.add_group_instance(parts[1], bindings or {}, position=position)
        if len(parts) != 3 or parts[0] != "node":
            raise ApplicationError("palette_item_not_found", f"Palette item '{item_id}' is unavailable")
        component_id = None if parts[1] == "session" else parts[1]
        return self.add_node(component_id, parts[2], bindings or {}, position=position)

    def add_group_instance(
        self,
        group_id: str,
        bindings: dict[str, str],
        *,
        position: tuple[float, float] = (80.0, 80.0),
    ) -> dict[str, Any]:
        if len(position) != 2 or not all(type(value) in (int, float) and math.isfinite(value) for value in position):
            raise ApplicationError("invalid_position", "Block position must contain two finite numbers")
        group_path = self._safe_group_path(group_id)
        try:
            contract = load_group_contract(group_path)
            self._ensure_group_runtime_alias()
            source, instance = build_group_instance_source(contract, bindings)
            cell_id = self.marimo.add_cell(self._require_session(), source)
        except (TypeError, ValueError) as exc:
            raise ApplicationError("invalid_group", str(exc)) from exc
        self._save_positions({cell_id: {"x": position[0], "y": position[1]}})
        return {"cell_id": cell_id, "instance_id": instance.instance_id, "result_var": instance.result_var, "source": source, "group_id": instance.group_id}

    def _ensure_group_runtime_alias(self) -> None:
        snapshot = self.marimo.inspect(self._require_session())
        if any(isinstance(cell, dict) and "graphimo" in cell.get("defs", []) for cell in snapshot.get("cells", [])):
            return
        if any(isinstance(cell, dict) and "graphimo" in _assigned_global_names(str(cell.get("source", ""))) for cell in snapshot.get("cells", [])):
            return
        cell_id = self.marimo.add_cell(self._require_session(), "# Graphimo group runtime\nimport graphimo")
        self.marimo.run_cell(self._require_session(), cell_id)

    def add_control(
        self,
        control_type: str,
        parameters: dict[str, Any],
        *,
        position: tuple[float, float] = (80.0, 80.0),
    ) -> dict[str, Any]:
        if len(position) != 2 or not all(type(value) in (int, float) and math.isfinite(value) for value in position):
            raise ApplicationError("invalid_position", "Block position must contain two finite numbers")
        merged = dict(CONTROL_DEFAULTS.get(control_type, {}))
        merged.update(parameters)
        try:
            self._ensure_marimo_ui_alias()
            source, spec = build_control_source(control_type, merged)
            cell_id = self.marimo.add_cell(self._require_session(), source)
            self.marimo.run_cell(self._require_session(), cell_id)
        except (TypeError, ValueError) as exc:
            raise ApplicationError("invalid_control", str(exc)) from exc
        self._save_positions({cell_id: {"x": position[0], "y": position[1]}})
        return {"cell_id": cell_id, "instance_id": spec.instance_id, "result_var": spec.result_var}

    def _ensure_marimo_ui_alias(self) -> None:
        snapshot = self.marimo.inspect(self._require_session())
        if any(
            isinstance(cell, dict) and "mo" in cell.get("defs", [])
            for cell in snapshot.get("cells", [])
        ):
            return
        if any(
            isinstance(cell, dict) and "mo" in _assigned_global_names(str(cell.get("source", "")))
            for cell in snapshot.get("cells", [])
        ):
            return
        helper_source = "# Graphimo control runtime\nimport marimo as mo"
        helper_id = self.marimo.add_cell(self._require_session(), helper_source)
        self.marimo.run_cell(self._require_session(), helper_id)

    def set_control_value(self, cell_id: str, value: Any) -> dict[str, Any]:
        cell = self._cell(cell_id)
        spec = parse_control_source(cell.get("source", ""))
        if spec is None:
            raise ApplicationError("not_graphimo_control", "Value editing requires a Graphimo Control Block")
        validator = getattr(self.marimo, "set_control_value", None)
        if not callable(validator):
            raise ApplicationError("control_updates_unavailable", "The active marimo host cannot update UI elements")
        try:
            normalized = validate_control_value(spec, value)
            validator(self._require_session(), spec.result_var, normalized)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise ApplicationError("control_update_failed", str(exc)) from exc
        return {"changed": True, "cell_id": cell_id, "value": value}

    def connect(
        self,
        source_cell_id: str,
        target_cell_id: str,
        *,
        input_name: str,
        output_index: int = 0,
    ) -> dict[str, Any]:
        snapshot = self.marimo.inspect(self._require_session())
        cells = {cell.get("id"): cell for cell in snapshot.get("cells", []) if isinstance(cell, dict)}
        source_cell = cells.get(source_cell_id)
        target_cell = cells.get(target_cell_id)
        if source_cell is None or target_cell is None:
            raise ApplicationError("cell_not_found", "Connection endpoint no longer exists")
        source_instance = parse_instance_source(source_cell.get("source", ""))
        target_instance = parse_instance_source(target_cell.get("source", ""))
        source_group = parse_group_instance_source(source_cell.get("source", ""))
        target_group = parse_group_instance_source(target_cell.get("source", ""))
        source_control = parse_control_source(source_cell.get("source", ""))
        source_metablock = _parse_managed_metablock_source(source_cell.get("source", ""))
        if (source_instance is None and source_group is None and source_control is None and source_metablock is None) or (target_instance is None and target_group is None):
            raise ApplicationError("unsafe_edit", "Connections require a Graphimo output and target Graphimo instance")
        if target_instance is not None:
            target_spec = self._node_spec(None, target_instance.node_name)
            target_input_names = {port.name for port in target_spec.inputs}
        else:
            target_contract = self._resolve_group_path(target_group.path)
            target_input_names = {port.name for port in target_contract.inputs}
        if input_name not in target_input_names:
            raise ApplicationError("input_not_found", f"Target has no input named '{input_name}'")
        try:
            if source_control is not None:
                if output_index != 0:
                    raise ValueError("Control Blocks have exactly one output")
                expression = source_control.output_expression
            elif source_metablock is not None:
                if output_index != 0:
                    raise ValueError("MetaBlocks expose one external definition output")
                materialization = self.build_metablock_materialization(
                    source_metablock["metablock_id"], {}
                )
                if materialization.result_var not in _assigned_global_names(source_cell.get("source", "")):
                    raise ValueError("MetaBlock materialization result is stale")
                expression = materialization.result_var
            elif source_group is not None:
                if not 0 <= output_index < len(source_group.outputs):
                    raise ValueError("Group output index is outside the declared output range")
                expression = group_output_expression(source_group, source_group.outputs[output_index])
            else:
                source_spec = self._node_spec(None, source_instance.node_name)
                expression = output_expression(source_instance, output_index, len(source_spec.outputs))
            if target_group is not None:
                source = reconnect_group_instance_source(target_cell.get("source", ""), input_name=input_name, expression=expression)
            else:
                source, _ = self._reconnect(target_cell.get("source", ""), input_name, expression)
            self.marimo.replace_cell_source(self._require_session(), target_cell_id, source)
        except (ValueError, TypeError) as exc:
            raise ApplicationError("invalid_connection", str(exc)) from exc
        return {"changed": True, "source": source}

    def reconnect(
        self,
        edge_id: str,
        *,
        source_cell_id: str,
        target_cell_id: str,
        input_name: str,
        output_index: int = 0,
    ) -> dict[str, Any]:
        del edge_id
        return self.connect(source_cell_id, target_cell_id, input_name=input_name, output_index=output_index)

    def disconnect(self, target_cell_id: str, *, input_name: str) -> dict[str, Any]:
        cell = self._cell(target_cell_id)
        if parse_group_instance_source(cell.get("source", "")) is not None:
            try:
                source = disconnect_group_instance_source(cell.get("source", ""), input_name=input_name)
            except ValueError as exc:
                raise ApplicationError("invalid_request", str(exc)) from exc
            self.marimo.replace_cell_source(self._require_session(), target_cell_id, source)
            return {"changed": True, "source": source}
        instance = parse_instance_source(cell.get("source", ""))
        if instance is None:
            raise ApplicationError("unsafe_edit", "Disconnection requires a Graphimo-owned node instance")
        bindings = {binding.input_name: binding.expression for binding in instance.bindings}
        if input_name not in bindings:
            raise ApplicationError("input_not_found", f"Instance has no bound input named '{input_name}'")
        del bindings[input_name]
        source, _ = build_instance_source(instance.node_name, bindings, instance_id=instance.instance_id)
        self.marimo.replace_cell_source(self._require_session(), target_cell_id, source)
        return {"changed": True, "source": source}

    def set_literal(self, cell_id: str, input_name: str, value: Any) -> dict[str, Any]:
        cell = self._cell(cell_id)
        expression = _literal_expression(value)
        instance = parse_instance_source(cell.get("source", ""))
        if instance is None:
            raise ApplicationError("unsafe_edit", "Literal editing requires a Graphimo-owned node instance")
        try:
            if any(binding.input_name == input_name for binding in instance.bindings):
                source = reconnect_instance_source(cell["source"], input_name=input_name, expression=expression)
            else:
                spec = self._node_spec(None, instance.node_name)
                if input_name not in {port.name for port in spec.inputs}:
                    raise ApplicationError("input_not_found", f"Node has no input named '{input_name}'")
                bindings = {binding.input_name: binding.expression for binding in instance.bindings}
                bindings[input_name] = expression
                source, _ = build_instance_source(instance.node_name, bindings, instance_id=instance.instance_id)
            self.marimo.replace_cell_source(self._require_session(), cell_id, source)
        except ApplicationError:
            raise
        except ValueError as exc:
            raise ApplicationError("invalid_request", str(exc)) from exc
        return {"changed": True, "source": source}

    def inspect_node_instance(self, cell_id: str) -> dict[str, Any]:
        snapshot = self.marimo.inspect(self._require_session())
        cell = next((item for item in snapshot.get("cells", []) if item.get("id") == cell_id), None)
        if cell is None:
            raise ApplicationError("cell_not_found", f"Cell '{cell_id}' no longer exists")
        instance = parse_instance_source(cell.get("source", ""))
        if instance is None:
            raise ApplicationError("not_graphimo_instance", "Cell is not a Graphimo-owned node instance")
        spec = self._node_spec(None, instance.node_name)
        remote_describer = getattr(self.marimo, "describe_node_result", None)
        if callable(remote_describer):
            try:
                outputs = remote_describer(self._require_session(), instance.result_var, instance.node_name)
            except (KeyError, LookupError, RuntimeError) as exc:
                raise ApplicationError("runtime_value_unavailable", f"Result '{instance.result_var}' is not available") from exc
            except ValueError as exc:
                raise ApplicationError(str(exc), f"Could not describe node result: {exc}") from exc
            return {"instance_id": instance.instance_id, "node": instance.node_name, "outputs": outputs}
        getter = getattr(self.marimo, "get_variable", None)
        if not callable(getter):
            raise ApplicationError("runtime_value_unavailable", "The marimo host does not expose live variable inspection")
        try:
            value = getter(self._require_session(), instance.result_var)
            outputs = graphimo.describe_result(value, spec)
        except (KeyError, LookupError) as exc:
            raise ApplicationError("runtime_value_unavailable", f"Result '{instance.result_var}' is not available") from exc
        except ValueError as exc:
            raise ApplicationError(str(exc), f"Could not describe node result: {exc}") from exc
        return {"instance_id": instance.instance_id, "node": instance.node_name, "outputs": outputs}

    def inspect_group_instance(self, cell_id: str) -> dict[str, Any]:
        cell = self._cell(cell_id)
        instance = parse_group_instance_source(cell.get("source", ""))
        if instance is None:
            raise ApplicationError("not_group_instance", "Cell is not a Graphimo Group instance")
        try:
            contract = self._resolve_group_path(instance.path)
            remote_describer = getattr(self.marimo, "describe_group_result", None)
            if callable(remote_describer):
                raw_outputs = remote_describer(self._require_session(), instance.result_var, [port.name for port in contract.outputs])
                by_name = {item.get("name"): item for item in raw_outputs if isinstance(item, dict)}
                outputs = [
                    {"index": index, "name": port.name, "declared_type": port.type,
                     "runtime_type": by_name.get(port.name, {}).get("runtime_type"),
                     "preview": by_name.get(port.name, {}).get("preview"),
                     "expression": f"{instance.result_var}.{port.name}"}
                    for index, port in enumerate(contract.outputs)
                ]
                return {"instance_id": instance.instance_id, "group_id": instance.group_id, "outputs": outputs}
            value = self.marimo.get_variable(self._require_session(), instance.result_var)
        except (ApplicationError, KeyError, LookupError, RuntimeError) as exc:
            raise ApplicationError("runtime_value_unavailable", "Group result is not available") from exc
        outputs = []
        for port in contract.outputs:
            try:
                output_value = value[port.name]
            except (KeyError, TypeError, AttributeError) as exc:
                raise ApplicationError("group_output_missing", f"Group result has no output {port.name!r}") from exc
            preview = graphimo.preview_value(output_value)
            outputs.append({"index": len(outputs), "name": port.name, "declared_type": port.type,
                            "runtime_type": preview["runtime_type"], "preview": preview["preview"],
                            "expression": f"{instance.result_var}.{port.name}"})
        return {"instance_id": instance.instance_id, "group_id": instance.group_id, "outputs": outputs}

    def run_target(self, cell_id: str) -> Any:
        cell = self._cell(cell_id)
        if parse_disabled_instance_source(cell.get("source", "")) is not None:
            raise ApplicationError("instance_disabled", "Enable this Graphimo instance before running it")
        return self.marimo.run_cell(self._require_session(), cell_id)

    def disable_instance(self, cell_id: str) -> dict[str, Any]:
        cell = self._cell(cell_id)
        try:
            source = disable_instance_source(cell.get("source", ""))
            self.marimo.replace_cell_source(self._require_session(), cell_id, source)
        except ValueError as exc:
            raise ApplicationError("not_graphimo_instance", str(exc)) from exc
        return {"changed": True, "source": source}

    def enable_instance(self, cell_id: str) -> dict[str, Any]:
        cell = self._cell(cell_id)
        try:
            source = enable_instance_source(cell.get("source", ""))
            self.marimo.replace_cell_source(self._require_session(), cell_id, source)
        except ValueError as exc:
            raise ApplicationError("not_disabled_instance", str(exc)) from exc
        return {"changed": True, "source": source}

    def delete_elements(self, ids: tuple[str, ...] | list[str]) -> list[Any]:
        results = []
        for cell_id in dict.fromkeys(ids):
            results.append(self.marimo.delete_cell(self._require_session(), cell_id))
        self._selection = tuple(item for item in self._selection if item not in ids)
        return results

    def duplicate_elements(
        self,
        ids: tuple[str, ...] | list[str],
        *,
        offset: tuple[float, float] = (32.0, 32.0),
    ) -> list[dict[str, Any]]:
        snapshot = self.marimo.inspect(self._require_session())
        cells = {item.get("id"): item for item in snapshot.get("cells", []) if isinstance(item, dict)}
        positions = self._read_positions()
        results = []
        for cell_id in dict.fromkeys(ids):
            cell = cells.get(cell_id)
            instance = parse_instance_source(cell.get("source", "")) if cell else None
            group_instance = parse_group_instance_source(cell.get("source", "")) if cell else None
            if instance is not None:
                bindings = {binding.input_name: binding.expression for binding in instance.bindings}
                source, clone = build_instance_source(instance.node_name, bindings)
                clone_info = {"instance_id": clone.instance_id, "result_var": clone.result_var}
            elif group_instance is not None:
                contract = self._resolve_group_path(group_instance.path)
                bindings = {binding.input_name: binding.expression for binding in group_instance.bindings}
                source, clone = build_group_instance_source(contract, bindings, outputs=group_instance.outputs, allow_incomplete=True)
                clone_info = {"instance_id": clone.instance_id, "result_var": clone.result_var, "group_id": clone.group_id}
            else:
                raise ApplicationError("unsupported_duplicate", "Only Graphimo node and Group instances can be duplicated")
            new_id = self.marimo.add_cell(self._require_session(), source)
            position = positions.get(cell_id, {"x": 80, "y": 80})
            self._save_positions({new_id: {"x": position.get("x", 80) + offset[0], "y": position.get("y", 80) + offset[1]}})
            results.append({"cell_id": new_id, **clone_info, "source": source})
        return results

    def open_inspector(self, cell_id: str) -> dict[str, Any]:
        if parse_group_instance_source(self._cell(cell_id).get("source", "")) is not None:
            return self.inspect_group_instance(cell_id)
        return self.inspect_node_instance(cell_id)

    def open_source(self, element_id: str) -> Any:
        opener = getattr(self.marimo, "open_source", None)
        if not callable(opener):
            raise ApplicationError("open_source_unavailable", "The marimo host does not expose source navigation")
        return opener(self._require_session(), element_id)

    def unavailable(self, action: str) -> None:
        if action not in self.available_actions:
            raise ApplicationError("feature_not_available", f"'{action}' is unavailable in this application milestone")

    def move_elements(self, positions: dict[str, tuple[float, float]]) -> GraphimoViewState:
        self._save_positions({cell_id: {"x": xy[0], "y": xy[1]} for cell_id, xy in positions.items()})
        return self.refresh_project()

    def _require_session(self) -> str:
        if self._session_id is None:
            raise ApplicationError("project_not_open", "Open a marimo project before using Graphimo operations")
        return self._session_id

    def _cell(self, cell_id: str) -> dict[str, Any]:
        snapshot = self.marimo.inspect(self._require_session())
        cell = next((item for item in snapshot.get("cells", []) if item.get("id") == cell_id), None)
        if cell is None:
            raise ApplicationError("cell_not_found", f"Cell '{cell_id}' no longer exists")
        return cell

    def _resolve_node(self, component_id: str, node_name: str) -> Any:
        path = self._safe_component_path(component_id)
        module = self._load_module(path)
        function = getattr(module, node_name, None)
        if not graphimo.is_node(function):
            raise ApplicationError("node_not_found", f"Callable '{node_name}' is not an exported @node")
        return function

    def _node_spec(self, component_id: str | None, node_name: str) -> NodeSpec:
        inspector = getattr(self.marimo, "inspect_node", None)
        if callable(inspector):
            try:
                spec = inspector(self._require_session(), node_name)
                if isinstance(spec, NodeSpec):
                    return spec
                if isinstance(spec, dict):
                    inputs = tuple(InputPort(**item) for item in spec.get("inputs", []))
                    outputs = tuple(
                        OutputPort(
                            index=item["index"],
                            declared_type=item.get("declared_type"),
                            expressions=tuple(item.get("expressions", ())),
                        )
                        for item in spec.get("outputs", [])
                    )
                    return NodeSpec(node_name, inputs, outputs, spec.get("diagnostic"))
            except (KeyError, LookupError, RuntimeError):
                pass
        if component_id is not None:
            return graphimo.inspect_node(self._resolve_node(component_id, node_name))
        return graphimo.inspect_node(self._resolve_node_by_name(node_name))

    def _resolve_node_by_name(self, node_name: str) -> Any:
        inspector = getattr(self.marimo, "resolve_node", None)
        if callable(inspector):
            function = inspector(self._require_session(), node_name)
            if graphimo.is_node(function):
                return function
        matches = []
        if self.components_root.is_dir():
            paths = set(self.components_root.rglob("*.py")) | set(self.components_root.rglob("__init__.py"))
            for path in sorted(paths):
                if any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(self.components_root).parts):
                    continue
                if path.name == "__init__.py" and path.parent == self.components_root:
                    continue
                try:
                    module = self._load_module(path)
                except ApplicationError:
                    continue
                function = getattr(module, node_name, None)
                if graphimo.is_node(function):
                    matches.append(function)
        if len(matches) != 1:
            raise ApplicationError("node_not_found", f"Node definition '{node_name}' is unavailable or ambiguous")
        return matches[0]

    def _safe_component_path(self, component_id: str) -> Path:
        relative = Path(component_id)
        base = self.components_root
        path = (base / relative).resolve()
        if relative.is_absolute() or base not in path.parents:
            raise ApplicationError("component_not_found", "Component path is outside the component library")
        if path.is_dir() and (path / "__init__.py").is_file():
            return path / "__init__.py"
        if path.is_file() and path.suffix == ".py":
            return path
        raise ApplicationError("component_not_found", f"Component '{component_id}' was not found")

    def _safe_group_path(self, group_id_or_path: str) -> Path:
        if not isinstance(group_id_or_path, str) or not group_id_or_path:
            raise ApplicationError("group_not_found", "Group path must be non-empty")
        relative = Path(group_id_or_path)
        base = self.components_root
        path = relative.resolve() if relative.is_absolute() else (base / relative).resolve()
        if base not in path.parents or path.suffix != ".py" or not path.is_file():
            raise ApplicationError("group_not_found", "Group file must be a .py file inside the component library")
        try:
            load_group_contract(path)
        except ValueError as exc:
            raise ApplicationError("group_not_found", str(exc)) from exc
        return path

    def _resolve_group_path(self, group_id_or_path: str) -> GroupContract:
        return load_group_contract(self._safe_group_path(group_id_or_path))

    @staticmethod
    def _load_module(path: Path) -> Any:
        import importlib.util
        import sys

        module_name = f"_graphimo_app_component_{abs(hash(str(path)))}"
        spec = importlib.util.spec_from_file_location(
            module_name,
            path,
            submodule_search_locations=[str(path.parent)] if path.name == "__init__.py" else None,
        )
        if spec is None or spec.loader is None:
            raise ApplicationError("component_load_error", f"Could not load component '{path.name}'")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise ApplicationError("component_load_error", f"Component import failed: {exc}") from exc
        finally:
            sys.modules.pop(module_name, None)
        return module

    @staticmethod
    def _reconnect(source: str, input_name: str, expression: str) -> tuple[str, None]:
        updated = reconnect_instance_source(source, input_name=input_name, expression=expression)
        return updated, None

    def _palette(self) -> ScopePalette:
        items = [
            PaletteItem(
                id=f"control:{control_type}",
                title=control_type.replace("_", " ").title(),
                category="Controls",
                kind="control",
                description="Reactive marimo UI value",
                search_terms=(control_type, "control", "marimo"),
                source_provider="graphimo",
                icon_hint="control",
                input_schema=tuple(
                    {"name": key, "required": False, "default": value}
                    for key, value in defaults.items()
                ),
            )
            for control_type, defaults in CONTROL_DEFAULTS.items()
        ]
        for entry in discover_metablock_addons():
            distribution = getattr(entry, "dist", None)
            title = getattr(distribution, "name", None) or entry.name
            items.append(
                PaletteItem(
                    id=f"metablock:{entry.name}",
                    title=title,
                    category="MetaBlocks",
                    kind="metablock",
                    description="Specialized Graphimo scope",
                    search_terms=(entry.name, title, "metablock", "addon"),
                    source_provider="addon",
                    icon_hint="metablock",
                )
            )
        live_nodes = getattr(self.marimo, "list_graphimo_nodes", None)
        if callable(live_nodes) and self._session_id is not None:
            try:
                for node_info in live_nodes(self._session_id):
                    name = node_info.get("name")
                    if isinstance(name, str):
                        items.append(
                            PaletteItem(
                                id=f"node:session:{name}",
                                title=name,
                                category="Nodes",
                                kind="node",
                                description=node_info.get("description"),
                                search_terms=(name,),
                                source_provider="notebook",
                                icon_hint="function",
                                input_schema=tuple(node_info.get("inputs", ())),
                            )
                        )
            except (KeyError, LookupError, ValueError):
                pass
        if self.components_root.is_dir():
            paths = set(self.components_root.rglob("*.py")) | set(self.components_root.rglob("__init__.py"))
            for path in sorted(paths):
                if any(part.startswith(".") for part in path.relative_to(self.components_root).parts):
                    continue
                try:
                    contract = load_group_contract(path)
                except ValueError:
                    contract = None
                if contract is not None:
                    relative_group = path.relative_to(self.components_root).as_posix()
                    items.append(PaletteItem(
                        id=f"group:{relative_group}", title=contract.name, category="Groups", kind="group",
                        description=f"Reusable {contract.dialect_id} workflow",
                        search_terms=(contract.name, contract.id, "group", "workflow"),
                        source_provider="project", icon_hint="group",
                        input_schema=tuple({"name": port.name, "required": port.required, "type": port.type, "default": port.default} for port in contract.inputs),
                    ))
                    continue
                try:
                    module = self._load_module(path)
                except ApplicationError:
                    continue
                for name, value in vars(module).items():
                    if not isinstance(name, str) or name.startswith("_") or not graphimo.is_node(value):
                        continue
                    if any(item.title == name and item.source_provider == "notebook" for item in items):
                        continue
                    items.append(
                        PaletteItem(
                            id=f"node:{path.relative_to(self.components_root).as_posix()}:{name}",
                            title=name,
                            category="Nodes",
                            kind="node",
                            description=(getattr(value, "__doc__", None) or "").splitlines()[0] if getattr(value, "__doc__", None) else None,
                            search_terms=(name,),
                            source_provider="project",
                            icon_hint="function",
                            input_schema=tuple(
                                {
                                    "name": port.name,
                                    "required": port.required,
                                    "annotation": port.annotation,
                                    "has_default": port.has_default,
                                    "default": port.default if _is_json_safe_value(port.default) else None,
                                }
                                for port in graphimo.inspect_node(value).inputs
                            ),
                        )
                    )
        scope_id = self._scope_stack[-1][0] if self._scope_stack else "root"
        return ScopePalette(scope_id, "python", tuple(items))

    def _view_state(self, snapshot: dict[str, Any]) -> GraphimoViewState:
        saved = self._read_positions()
        current_is_group_file = False
        if self._project_path is not None:
            try:
                load_group_contract(self._project_path)
                current_is_group_file = True
            except ValueError:
                pass
        disabled = {
            instance.result_var: cell["id"]
            for cell in snapshot.get("cells", [])
            if isinstance(cell, dict)
            and isinstance(cell.get("id"), str)
            and (instance := parse_disabled_instance_source(cell.get("source", ""))) is not None
        }
        stale_targets: dict[str, set[str]] = {}
        for cell in snapshot.get("cells", []):
            if not isinstance(cell, dict) or not isinstance(cell.get("id"), str):
                continue
            source_text = cell.get("source", "")
            instance = parse_instance_source(source_text)
            if instance is not None:
                refs = {
                    name
                    for binding in instance.bindings
                    for name in _expression_names(binding.expression)
                    if name in disabled
                }
            else:
                refs = set(cell.get("refs", [])) & set(disabled)
                refs.update(name for name in disabled if isinstance(source_text, str) and name in _expression_names(source_text))
            if refs:
                stale_targets[cell["id"]] = refs
        edges = tuple(
            EdgeView(
                id=f"{edge.get('source_cell_id')}:{edge.get('target_cell_id')}:{edge.get('variable')}",
                source_block_id=str(edge.get("source_cell_id", "")),
                target_block_id=str(edge.get("target_cell_id", "")),
                variable=str(edge.get("variable", "")),
            )
            for edge in snapshot.get("edges", [])
            if isinstance(edge, dict)
        ) + tuple(
            EdgeView(
                id=f"{source_id}:{target_id}:{variable}:stale",
                source_block_id=source_id,
                target_block_id=target_id,
                variable=variable,
                state="stale",
            )
            for target_id, variables in stale_targets.items()
            for variable in variables
            for source_id in (disabled[variable],)
        )
        blocks = []
        diagnostics = []
        managed_metablocks: set[str] = set()
        for index, cell in enumerate(snapshot.get("cells", [])):
            if not isinstance(cell, dict) or not isinstance(cell.get("id"), str):
                continue
            cell_id = cell["id"]
            raw_source = cell.get("source", "")
            if current_is_group_file and (
                is_group_managed_cell(raw_source)
                or cell.get("name") in {"__graphimo_inputs", "__graphimo_outputs"}
            ):
                continue
            if raw_source == "# Graphimo control runtime\nimport marimo as mo":
                continue
            managed_meta = _parse_managed_metablock_source(raw_source)
            if managed_meta is not None:
                metablock_id = managed_meta["metablock_id"]
                dialect_id = managed_meta["dialect_id"]
                try:
                    materialization = self.build_metablock_materialization(metablock_id, {})
                    if materialization.result_var not in _assigned_global_names(raw_source):
                        raise ApplicationError(
                            "metablock_materialization_stale",
                            "Managed MetaBlock cell does not define its declared result variable",
                        )
                    projection = self.project_metablock(
                        metablock_id, {"id": "forward", "method_name": "forward"}
                    )
                    state_path = self._metablock_sidecar_path(metablock_id, dialect_id)
                    title = materialization.output_label
                    try:
                        serialized = json.loads(state_path.read_text(encoding="utf-8"))
                        if isinstance(serialized, dict) and isinstance(serialized.get("name"), str):
                            title = serialized["name"]
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        pass
                    saved_block = saved.get(cell_id, {})
                    outputs = (
                        PortView(
                            id=f"{cell_id}:output:0",
                            name=materialization.output_label,
                            direction="output",
                            declared_type=materialization.output_type,
                            runtime_type=materialization.output_type,
                            expression=materialization.result_var,
                            preview=materialization.output_label,
                        ),
                    )
                    blocks.append(
                        BlockView(
                            id=cell_id,
                            kind="metablock_instance",
                            title=title,
                            subtitle=dialect_id,
                            x=float(saved_block.get("x", 80 + (index % 4) * 280)),
                            y=float(saved_block.get("y", 80 + (index // 4) * 190)),
                            width=float(saved_block.get("width", 240)),
                            height=float(saved_block.get("height", 150)),
                            state=str(cell.get("status", "unknown")),
                            inputs=(),
                            outputs=outputs,
                            body={
                                "metablock_id": metablock_id,
                                "dialect_id": dialect_id,
                                "result_var": materialization.result_var,
                                "output_type": materialization.output_type,
                                "projection": projection,
                                "needs_materialization": False,
                            },
                            badges=("MetaBlock",),
                            can_open=True,
                            can_edit_source=False,
                            can_disable=False,
                        )
                    )
                    managed_metablocks.add(metablock_id)
                except ApplicationError as exc:
                    if exc.code == "metablock_addon_unavailable":
                        continue
                    diagnostics.append(DiagnosticView(exc.code, str(exc), cell_id))
                continue
            disabled_instance = parse_disabled_instance_source(raw_source)
            instance = parse_instance_source(raw_source)
            group_instance = parse_group_instance_source(raw_source)
            is_disabled = disabled_instance is not None
            if is_disabled:
                instance = disabled_instance
            inputs: tuple[PortView, ...] = ()
            outputs: tuple[PortView, ...] = ()
            kind = "python_cell"
            title = cell.get("name") or f"Cell {index + 1}"
            cell_errors = cell.get("errors", []) if isinstance(cell.get("errors", []), list) else []
            body: dict[str, Any] = {"source": cell.get("source", ""), "errors": cell_errors}
            for error in cell_errors:
                if isinstance(error, dict):
                    diagnostics.append(
                        DiagnosticView(
                            "python_runtime_exception",
                            f"{error.get('kind', 'Error')}: {error.get('message', 'Cell execution failed')}",
                            cell_id,
                        )
                    )
            if instance is not None:
                kind = "node_instance"
                title = instance.node_name
                body = {"instance_id": instance.instance_id, "result_var": instance.result_var}
                spec = self._try_node_spec(instance.node_name)
                if spec is not None:
                    inputs = tuple(
                        PortView(
                            id=f"{cell_id}:input:{port.name}",
                            name=port.name,
                            direction="input",
                            declared_type=port.annotation,
                            expression=next((binding.expression for binding in instance.bindings if binding.input_name == port.name), None),
                            connected=_is_connection_expression(
                                next((binding.expression for binding in instance.bindings if binding.input_name == port.name), None)
                            ),
                        )
                        for port in spec.inputs
                    )
                    if is_disabled:
                        runtime_outputs = []
                    else:
                        try:
                            runtime_outputs = self.inspect_node_instance(cell_id)["outputs"]
                        except ApplicationError:
                            runtime_outputs = []
                    outputs = tuple(
                        PortView(
                            id=f"{cell_id}:output:{port.index}",
                            name=port.expressions[0] if port.expressions else f"output_{port.index + 1}",
                            direction="output",
                            declared_type=port.declared_type,
                            runtime_type=(runtime_outputs[port.index]["runtime_type"] if port.index < len(runtime_outputs) else None),
                            expression=(port.expressions[0] if len(port.expressions) == 1 else None),
                            preview=(runtime_outputs[port.index]["preview"] if port.index < len(runtime_outputs) else "unavailable · disabled" if is_disabled else None),
                        )
                        for port in spec.outputs
                    )
                    if spec.diagnostic:
                        diagnostics.append(DiagnosticView(spec.diagnostic, "Node output contract is inconsistent", cell_id))
            elif (control := parse_control_source(raw_source)) is not None:
                kind = "control"
                title = control.parameters.get("label", control.control_type.title())
                inspect_control = getattr(self.marimo, "inspect_control", None)
                runtime_control = None
                if callable(inspect_control):
                    try:
                        runtime_control = inspect_control(self._require_session(), control.result_var)
                    except (KeyError, RuntimeError, ValueError):
                        runtime_control = None
                value = runtime_control.get("value") if isinstance(runtime_control, dict) else control.parameters.get("value")
                preview = graphimo.preview_value(value)
                body = {
                    "control_type": control.control_type,
                    "instance_id": control.instance_id,
                    "result_var": control.result_var,
                    "parameters": control.parameters,
                    "value": value,
                    "ui_element_id": runtime_control.get("object_id") if isinstance(runtime_control, dict) else None,
                    "execution_policy": "on_release" if control.parameters.get("debounce") else "live",
                }
                outputs = (
                    PortView(
                        id=f"{cell_id}:output:0",
                        name="value" if control.control_type != "button" else "trigger",
                        direction="output",
                        declared_type=control_output_type(control),
                        runtime_type=preview["runtime_type"],
                        expression=control.output_expression,
                        preview=preview["preview"],
                    ),
                )
            elif group_instance is not None:
                try:
                    contract = self._resolve_group_path(group_instance.path)
                    if contract.id != group_instance.group_id:
                        raise ValueError("Group instance reference no longer matches its file")
                    kind = "group_instance"
                    title = contract.name
                    body = {
                        "group_id": contract.id,
                        "instance_id": group_instance.instance_id,
                        "result_var": group_instance.result_var,
                        "path": group_instance.path,
                        "summary": f"{len(contract.inputs)} inputs · {len(contract.outputs)} outputs",
                    }
                    inputs = tuple(
                        PortView(
                            id=f"{cell_id}:input:{port.name}", name=port.name, direction="input",
                            declared_type=port.type,
                            expression=next((item.expression for item in group_instance.bindings if item.input_name == port.name), None),
                            connected=_is_connection_expression(next((item.expression for item in group_instance.bindings if item.input_name == port.name), None)),
                        )
                        for port in contract.inputs
                    )
                    runtime_values = {}
                    runtime_descriptions = {}
                    if not is_disabled:
                        describe_group = getattr(self.marimo, "describe_group_result", None)
                        if callable(describe_group):
                            try:
                                runtime_descriptions = {
                                    item["name"]: item
                                    for item in describe_group(self._require_session(), group_instance.result_var, [port.name for port in contract.outputs])
                                }
                            except (KeyError, LookupError, RuntimeError, ValueError):
                                pass
                        elif callable(getattr(self.marimo, "get_variable", None)):
                            try:
                                runtime_result = self.marimo.get_variable(self._require_session(), group_instance.result_var)
                                if isinstance(runtime_result, Mapping):
                                    runtime_values = runtime_result
                            except (KeyError, LookupError, RuntimeError):
                                pass
                    outputs = tuple(
                        PortView(
                            id=f"{cell_id}:output:{port.name}", name=port.name, direction="output",
                            declared_type=port.type,
                            runtime_type=runtime_descriptions[port.name].get("runtime_type") if port.name in runtime_descriptions else graphimo.preview_value(runtime_values.get(port.name))["runtime_type"] if port.name in runtime_values else None,
                            expression=f"{group_instance.result_var}.{port.name}",
                            preview=runtime_descriptions[port.name].get("preview") if port.name in runtime_descriptions else graphimo.preview_value(runtime_values.get(port.name))["preview"] if port.name in runtime_values else None,
                        )
                        for port in contract.outputs
                    )
                except (ApplicationError, ValueError) as exc:
                    diagnostics.append(DiagnosticView("group_reference_invalid", str(exc), cell_id))
                    kind = "group_instance"
                    body = {"group_id": group_instance.group_id, "instance_id": group_instance.instance_id, "path": group_instance.path}
            position = saved.get(cell_id, {})
            blocks.append(
                BlockView(
                    id=cell_id,
                    kind=kind,
                    title=str(title),
                    subtitle=None,
                    x=float(position.get("x", 80 + (index % 4) * 280)),
                    y=float(position.get("y", 80 + (index // 4) * 190)),
                    width=float(position.get("width", 240)),
                    height=float(position.get("height", 150)),
                    state="disabled" if is_disabled else "stale" if cell_id in stale_targets else str(cell.get("status", "unknown")),
                    inputs=inputs,
                    outputs=outputs,
                    body=body,
                    badges=(),
                    can_open=True,
                    can_edit_source=True,
                    can_disable=instance is not None and not is_disabled,
                    can_enable=is_disabled,
                )
            )
        for opaque in self._opaque_metablock_views(saved, managed_metablocks):
            blocks.append(opaque["block"])
            if opaque["diagnostic"] is not None:
                diagnostics.append(opaque["diagnostic"])
        project = snapshot.get("notebook", {})
        project_id = str(project.get("path") or self._project_path or "marimo") if isinstance(project, dict) else self._project_path or "marimo"
        block_states = [block.state.lower() for block in blocks]
        if any("run" in state or "queued" in state for state in block_states):
            run_state = ProjectRunState("running")
        elif any("error" in state or "exception" in state for state in block_states):
            run_state = ProjectRunState("error")
        else:
            run_state = ProjectRunState("idle")
        return GraphimoViewState(
            project_id=project_id,
            active_scope_id=self._scope_stack[-1][0] if self._scope_stack else "root",
            breadcrumbs=tuple(ScopeCrumb(item[0], item[1]) for item in self._scope_stack) or (ScopeCrumb("root", Path(project_id).name or "Project"),),
            blocks=tuple(blocks),
            edges=edges,
            palette=self._palette(),
            selection=tuple(item for item in self._selection if any(block.id == item for block in blocks)),
            run_state=run_state,
            diagnostics=tuple(diagnostics),
            available_actions=self.available_actions,
        )

    def _opaque_metablock_views(
        self,
        saved_positions: dict[str, dict[str, float]],
        managed_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if self._project_path is None:
            return []
        root = Path(self._project_path).parent / ".graphimo" / "metablocks"
        if not root.is_dir():
            return []
        try:
            addons = discover_metablock_addons()
        except (ImportError, RuntimeError, ValueError):
            addons = ()
        managed_ids = managed_ids or set()
        result = []
        for index, path in enumerate(sorted(root.glob("*.json"))):
            filename_stem = path.stem
            fallback_id = filename_stem.split(".", 1)[0]
            diagnostic = None
            serialized: dict[str, Any] = {}
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                serialized = loaded if isinstance(loaded, dict) else {}
                if not isinstance(serialized, dict):
                    raise ValueError("MetaBlock state root must be an object")
                metablock_id = serialized.get("metablock_id")
                if not isinstance(metablock_id, str) or not metablock_id:
                    metablock_id = fallback_id
                name = serialized.get("name")
                if not isinstance(name, str) or not name:
                    name = metablock_id
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                metablock_id = fallback_id
                name = fallback_id
                diagnostic = DiagnosticView(
                    "metablock_state_unreadable",
                    f"Opaque MetaBlock state was preserved but could not be read: {exc}",
                    f"metablock:{metablock_id}",
                )
            if metablock_id in managed_ids:
                continue
            prefix = f"{metablock_id}."
            inferred_dialect = filename_stem[len(prefix) :] if filename_stem.startswith(prefix) else "unknown"
            dialect_id = serialized.get("dialect_id")
            if not isinstance(dialect_id, str) or not dialect_id:
                dialect_id = inferred_dialect
            installed = False
            if addons:
                try:
                    load_metablock_registration(dialect_id)
                    installed = True
                except (ImportError, LookupError, TypeError, ValueError, RuntimeError):
                    installed = False
            position = saved_positions.get(metablock_id, {})
            if installed:
                try:
                    materialization = self.build_metablock_materialization(metablock_id, {})
                    projection = self.project_metablock(
                        metablock_id, {"id": "forward", "method_name": "forward"}
                    )
                    result.append(
                        {
                            "block": BlockView(
                                id=f"metablock:{metablock_id}",
                                kind="metablock_instance",
                                title=str(name),
                                subtitle=dialect_id,
                                x=float(position.get("x", 80 + (index % 4) * 280)),
                                y=float(position.get("y", 80 + (index // 4) * 190)),
                                width=float(position.get("width", 240)),
                                height=float(position.get("height", 150)),
                                state="idle",
                                inputs=(),
                                outputs=(
                                    PortView(
                                        id=f"metablock:{metablock_id}:output:0",
                                        name=materialization.output_label,
                                        direction="output",
                                        declared_type=materialization.output_type,
                                        expression=materialization.result_var,
                                        preview=materialization.output_label,
                                    ),
                                ),
                                body={
                                    "metablock_id": metablock_id,
                                    "dialect_id": dialect_id,
                                    "result_var": materialization.result_var,
                                    "output_type": materialization.output_type,
                                    "projection": projection,
                                    "needs_materialization": True,
                                },
                                badges=("MetaBlock", "Not materialized"),
                                can_open=True,
                                can_edit_source=False,
                                can_disable=False,
                            ),
                            "diagnostic": diagnostic,
                        }
                    )
                    continue
                except ApplicationError as exc:
                    diagnostic = DiagnosticView(exc.code, str(exc), f"metablock:{metablock_id}")
            reason = f"MetaBlock addon '{dialect_id}' is not installed"
            result.append(
                {
                    "block": BlockView(
                        id=f"metablock:{metablock_id}",
                        kind="metablock_opaque",
                        title=str(name),
                        subtitle=f"Unavailable · {dialect_id}",
                        x=float(position.get("x", 80 + (index % 4) * 280)),
                        y=float(position.get("y", 80 + (index // 4) * 190)),
                        width=float(position.get("width", 240)),
                        height=float(position.get("height", 120)),
                        state="unavailable",
                        inputs=(),
                        outputs=(),
                        body={
                            "opaque": True,
                            "metablock_id": metablock_id,
                            "plugin_id": dialect_id,
                            "addon_installed": installed,
                            "unavailable_reason": reason,
                        },
                        badges=("Addon unavailable",),
                        can_open=False,
                        can_edit_source=False,
                        can_disable=False,
                    ),
                    "diagnostic": diagnostic,
                }
            )
        return result

    def _try_resolve_node(self, node_name: str) -> Any | None:
        try:
            return self._resolve_node_by_name(node_name)
        except ApplicationError:
            return None

    def _try_node_spec(self, node_name: str) -> NodeSpec | None:
        try:
            return self._node_spec(None, node_name)
        except ApplicationError:
            return None

    def _read_positions(self) -> dict[str, dict[str, Any]]:
        if self.layout_path is None:
            return {}
        try:
            data = json.loads(self.layout_path.read_text(encoding="utf-8"))
            notebook = data.get("notebooks", {}).get(self._project_path, {})
            cells = notebook.get("cells", {}) if isinstance(notebook, dict) else {}
            return cells if isinstance(cells, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save_positions(self, changes: dict[str, dict[str, Any]]) -> None:
        if self.layout_path is None or self._project_path is None:
            return
        try:
            data = json.loads(self.layout_path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("notebooks"), dict):
                data = {"version": 1, "notebooks": {}}
        except (OSError, ValueError, TypeError, AttributeError):
            data = {"version": 1, "notebooks": {}}
        notebook = data["notebooks"].setdefault(self._project_path, {"cells": {}})
        cells = notebook.setdefault("cells", {})
        for cell_id, state in changes.items():
            current = cells.setdefault(cell_id, {})
            current.update({key: value for key, value in state.items() if key in {"x", "y", "width", "height"}})
        self.layout_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.layout_path.name}.", dir=self.layout_path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, self.layout_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _parse_managed_metablock_source(source: Any) -> dict[str, str] | None:
    if not isinstance(source, str):
        return None
    lines = source.splitlines()
    if len(lines) < 3:
        return None
    dialect_prefix = "# Graphimo metablock: "
    instance_prefix = "# Graphimo instance: "
    if not lines[0].startswith(dialect_prefix) or not lines[1].startswith(instance_prefix):
        return None
    dialect_id = lines[0][len(dialect_prefix) :]
    metablock_id = lines[1][len(instance_prefix) :]
    if not dialect_id or not metablock_id or any(char in dialect_id + metablock_id for char in "/\\\0\r\n"):
        return None
    return {"dialect_id": dialect_id, "metablock_id": metablock_id}


def _literal_expression(value: Any, depth: int = 0) -> str:
    if depth > 5:
        raise ApplicationError("invalid_literal", "Literal is nested too deeply")
    if value is None or type(value) in (bool, int, str):
        expression = repr(value)
    elif type(value) is float and math.isfinite(value):
        expression = repr(value)
    elif type(value) in (list, tuple) and len(value) <= 100:
        items = [_literal_expression(item, depth + 1) for item in value]
        if type(value) is tuple and len(items) == 1:
            items[0] += ","
        expression = ("[" + ", ".join(items) + "]") if type(value) is list else ("(" + ", ".join(items) + ")")
    elif type(value) is dict and len(value) <= 100:
        expression = "{" + ", ".join(
            f"{_literal_expression(key, depth + 1)}: {_literal_expression(item, depth + 1)}"
            for key, item in value.items()
        ) + "}"
    else:
        raise ApplicationError("invalid_literal", "Only bounded built-in Python literals are supported")
    try:
        ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ApplicationError("invalid_literal", "Value cannot be represented as a Python literal") from exc
    return expression


def _is_json_safe_value(value: Any, depth: int = 0) -> bool:
    if depth > 5:
        return False
    if value is None or type(value) in (bool, int, str):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) in (list, tuple):
        return len(value) <= 100 and all(_is_json_safe_value(item, depth + 1) for item in value)
    if type(value) is dict:
        return len(value) <= 100 and all(
            _is_json_safe_value(key, depth + 1) and _is_json_safe_value(item, depth + 1)
            for key, item in value.items()
        )
    return False


def _is_connection_expression(expression: str | None) -> bool:
    if not expression:
        return False
    try:
        parsed = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return False
    if isinstance(parsed, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set)):
        return False
    if isinstance(parsed, ast.Name) and parsed.id in {"True", "False", "None"}:
        return False
    return True


def _expression_names(source: str) -> set[str]:
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return set()
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def _assigned_global_names(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in statement.names)
        elif isinstance(statement, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in statement.names)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names
