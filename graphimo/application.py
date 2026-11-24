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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import graphimo
from graphimo.instance import (
    build_instance_source,
    output_expression,
    parse_instance_source,
    reconnect_instance_source,
)
from graphimo.node import InputPort, NodeSpec, OutputPort


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


class GraphimoApplication:
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
        )

    def close_project(self) -> None:
        self._session_id = None
        self._project_path = None
        self._selection = ()

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
    ) -> dict[str, Any]:
        parts = item_id.split(":", 2)
        if len(parts) != 3 or parts[0] != "node":
            raise ApplicationError("palette_item_not_found", f"Palette item '{item_id}' is unavailable")
        component_id = None if parts[1] == "session" else parts[1]
        return self.add_node(component_id, parts[2], bindings or {}, position=position)

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
        if source_instance is None or target_instance is None:
            raise ApplicationError("unsafe_edit", "Connections require Graphimo-owned node instances")
        source_spec = self._node_spec(None, source_instance.node_name)
        target_spec = self._node_spec(None, target_instance.node_name)
        if input_name not in {port.name for port in target_spec.inputs}:
            raise ApplicationError("input_not_found", f"Node has no input named '{input_name}'")
        try:
            expression = output_expression(source_instance, output_index, len(source_spec.outputs))
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
            except (KeyError, LookupError) as exc:
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

    def run_target(self, cell_id: str) -> Any:
        self._cell(cell_id)
        return self.marimo.run_cell(self._require_session(), cell_id)

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
            if instance is None:
                raise ApplicationError("unsupported_duplicate", "Only Graphimo-owned node instances can be duplicated")
            bindings = {binding.input_name: binding.expression for binding in instance.bindings}
            source, clone = build_instance_source(instance.node_name, bindings)
            new_id = self.marimo.add_cell(self._require_session(), source)
            position = positions.get(cell_id, {"x": 80, "y": 80})
            self._save_positions({new_id: {"x": position.get("x", 80) + offset[0], "y": position.get("y", 80) + offset[1]}})
            results.append({"cell_id": new_id, "instance_id": clone.instance_id, "result_var": clone.result_var, "source": source})
        return results

    def open_inspector(self, cell_id: str) -> dict[str, Any]:
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
        items = []
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
        return ScopePalette("root", "python", tuple(items))

    def _view_state(self, snapshot: dict[str, Any]) -> GraphimoViewState:
        saved = self._read_positions()
        edges = tuple(
            EdgeView(
                id=f"{edge.get('source_cell_id')}:{edge.get('target_cell_id')}:{edge.get('variable')}",
                source_block_id=str(edge.get("source_cell_id", "")),
                target_block_id=str(edge.get("target_cell_id", "")),
                variable=str(edge.get("variable", "")),
            )
            for edge in snapshot.get("edges", [])
            if isinstance(edge, dict)
        )
        blocks = []
        diagnostics = []
        for index, cell in enumerate(snapshot.get("cells", [])):
            if not isinstance(cell, dict) or not isinstance(cell.get("id"), str):
                continue
            cell_id = cell["id"]
            instance = parse_instance_source(cell.get("source", ""))
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
                            preview=(runtime_outputs[port.index]["preview"] if port.index < len(runtime_outputs) else None),
                        )
                        for port in spec.outputs
                    )
                    if spec.diagnostic:
                        diagnostics.append(DiagnosticView(spec.diagnostic, "Node output contract is inconsistent", cell_id))
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
                    state=str(cell.get("status", "unknown")),
                    inputs=inputs,
                    outputs=outputs,
                    body=body,
                    badges=(),
                    can_open=True,
                    can_edit_source=True,
                    can_disable=instance is not None,
                )
            )
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
            active_scope_id="root",
            breadcrumbs=(ScopeCrumb("root", Path(project_id).name or "Project"),),
            blocks=tuple(blocks),
            edges=edges,
            palette=self._palette(),
            selection=tuple(item for item in self._selection if any(block.id == item for block in blocks)),
            run_state=run_state,
            diagnostics=tuple(diagnostics),
            available_actions=self.available_actions,
        )

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


class ApplicationError(Exception):
    """A typed, renderer-safe application failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
