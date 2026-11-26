"""Host-independent application intents for Graphimo semantic scopes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .components import ComponentLibrary
from .controls import control_catalog, create_control_source
from .groups import GroupLibrary, create_group_from_selection
from .metablocks import GraphimoHostAPI, MetaBlockAddonManager


class GraphimoApplication:
    """Route renderer and agent intents through one semantic application seam.

    The application owns no canvas state and does not schedule notebook
    execution. Host adapters call :meth:`execute`; the MetaBlock manager keeps
    addon-specific state and persistence behind the same operation boundary.
    """

    def __init__(self, host: GraphimoHostAPI, metablocks: MetaBlockAddonManager | None = None):
        self.host = host
        self.metablocks = metablocks or MetaBlockAddonManager(host)
        self.groups = GroupLibrary(host.project_path)
        self.components = ComponentLibrary(host.project_path)
        project_name = Path(host.project_path).stem or host.project_id
        self._scope_stack = [
            {
                "scope_id": "root",
                "container_id": None,
                "name": project_name,
                "kind": "project",
                "dialect_id": "python",
            }
        ]

    def execute(self, action: str, args: dict[str, Any]) -> Any:
        if not isinstance(action, str) or not action:
            raise ValueError("action must be a non-empty string")
        if not isinstance(args, dict):
            raise TypeError("action args must be an object")
        if len(json.dumps(args, allow_nan=False).encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("action args exceed the 4 MiB limit")

        operations = {
            "metablock.list": self._list_metablocks,
            "metablock.create": self._create_metablock,
            "metablock.catalog": self._catalog,
            "metablock.project": self._project,
            "metablock.apply": self._apply,
            "metablock.undo": self._undo_metablock,
            "metablock.redo": self._redo_metablock,
            "metablock.materialize": self._materialize,
            "metablock.invoke": self._invoke,
            "metablock.events": self._events,
            "group.list": self._list_groups,
            "group.create": self._create_group,
            "group.create_from_selection": self._create_group_from_selection,
            "group.delete": self._delete_group,
            "group.inspect": self._inspect_group,
            "group.set_contract": self._set_group_contract,
            "control.catalog": self._control_catalog,
            "control.create": self._create_control,
            "control.add": self._add_control,
            "component.list": self._list_components,
            "component.write": self._write_component,
            "component.validate": self._validate_component,
            "component.instance_source": self._component_instance_source,
            "component.add": self._add_component,
            "component.delete": self._delete_component,
            "component.refresh_instances": self._refresh_component_instances,
            "artifact.inspect": self._inspect_artifact,
            "artifact.stats": self._artifact_stats,
            "notebook.inspect": lambda value: self._invoke_notebook("inspect", value),
            "notebook.add_cell": lambda value: self._invoke_notebook("add_cell", value),
            "notebook.restore_cells": lambda value: self._invoke_notebook("restore_cells", value),
            "notebook.connect": lambda value: self._invoke_notebook("connect", value),
            "notebook.run": lambda value: self._invoke_notebook("run", value),
            "notebook.delete": lambda value: self._invoke_notebook("delete", value),
            "notebook.edit_guarded": lambda value: self._invoke_notebook("edit_guarded", value),
            "notebook.replace_group_selection": lambda value: self._invoke_notebook(
                "replace_group_selection", value
            ),
            "notebook.refresh_component_hashes": lambda value: self._invoke_notebook(
                "refresh_component_hashes", value
            ),
            "notebook.disable_instance": lambda value: self._invoke_notebook(
                "disable_instance", value
            ),
            "notebook.enable_instance": lambda value: self._invoke_notebook(
                "enable_instance", value
            ),
            "notebook.set_literal": lambda value: self._invoke_notebook("set_literal", value),
            "notebook.disconnect": lambda value: self._invoke_notebook("disconnect", value),
            "scope.enter": self._enter_group,
            "scope.leave": self._leave_scope,
            "scope.current": self._current_scope,
            "layout.get": self._get_layout,
            "layout.set": self._set_layout,
        }
        operation = operations.get(action)
        if operation is None:
            raise ValueError(f"unknown Graphimo action: {action}")
        result = operation(args)
        return _bounded_json(result)

    @staticmethod
    def _required(args: dict[str, Any], name: str) -> Any:
        if name not in args:
            raise ValueError(f"missing required action argument: {name}")
        return args[name]

    def _create_metablock(self, args: dict[str, Any]) -> Any:
        return self.metablocks.create(
            self._required(args, "dialect_id"),
            self._required(args, "metablock_id"),
            args.get("request", {}),
        )

    def _list_metablocks(self, _args: dict[str, Any]) -> Any:
        return self.metablocks.list()

    def _catalog(self, args: dict[str, Any]) -> Any:
        return self.metablocks.catalog(
            self._required(args, "metablock_id"), args.get("scope", "root")
        )

    def _project(self, args: dict[str, Any]) -> Any:
        return self.metablocks.project(
            self._required(args, "metablock_id"), args.get("scope", "root")
        )

    def _apply(self, args: dict[str, Any]) -> Any:
        return self.metablocks.apply(
            self._required(args, "metablock_id"), self._required(args, "intent")
        )

    def _undo_metablock(self, args: dict[str, Any]) -> Any:
        return self.metablocks.undo(self._required(args, "metablock_id"), args.get("scope", "root"))

    def _redo_metablock(self, args: dict[str, Any]) -> Any:
        return self.metablocks.redo(self._required(args, "metablock_id"), args.get("scope", "root"))

    def _materialize(self, args: dict[str, Any]) -> Any:
        return self.metablocks.materialize(
            self._required(args, "metablock_id"), args.get("bindings", {})
        )

    def _invoke(self, args: dict[str, Any]) -> Any:
        return self.metablocks.invoke(
            self._required(args, "metablock_id"),
            self._required(args, "action"),
            args.get("args", {}),
        )

    def _events(self, args: dict[str, Any]) -> Any:
        return self.metablocks.drain_events(
            self._required(args, "metablock_id"),
            args.get("cursor"),
            args.get("limit", 128),
        )

    def _list_groups(self, _args: dict[str, Any]) -> Any:
        return self.groups.list()

    def _create_group(self, args: dict[str, Any]) -> Any:
        request = args.get("request", args)
        return self.groups.create(request).to_catalog_item()

    def _create_group_from_selection(self, args: dict[str, Any]) -> Any:
        return create_group_from_selection(
            self.host.project_path,
            name=self._required(args, "name"),
            module=self._required(args, "module"),
            cells=self._required(args, "cells"),
            selected_ids=self._required(args, "selected_ids"),
        )

    def _delete_group(self, args: dict[str, Any]) -> Any:
        return self.groups.delete(
            self._required(args, "group_id"),
            expected_source_sha256=self._required(args, "expected_source_sha256"),
        )

    def _inspect_group(self, args: dict[str, Any]) -> Any:
        return self.groups.get(self._required(args, "group_id")).to_catalog_item()

    def _set_group_contract(self, args: dict[str, Any]) -> Any:
        group_id = self._required(args, "group_id")
        request = args.get("contract", {})
        return self.groups.update_contract(group_id, request).to_catalog_item()

    @staticmethod
    def _control_catalog(_args: dict[str, Any]) -> Any:
        return control_catalog()

    @staticmethod
    def _create_control(args: dict[str, Any]) -> Any:
        return create_control_source(args)

    def _add_control(self, args: dict[str, Any]) -> Any:
        built = create_control_source(args)
        snapshot = self._invoke_notebook("inspect", {})
        cells = snapshot.get("cells", []) if isinstance(snapshot, dict) else []
        instance_marker = f"# Graphimo instance: {built['identity']}"
        if any(
            isinstance(cell, dict)
            and "# Graphimo control:" in cell.get("source", "")
            and instance_marker in cell.get("source", "")
            for cell in cells
        ):
            raise FileExistsError(f"Graphimo control '{built['identity']}' already exists")
        control_cell = self._invoke_notebook("add_cell", {"source": built["control_source"]})
        value_cell = self._invoke_notebook("add_cell", {"source": built["value_source"]})
        self._invoke_notebook("run", {"cell_id": control_cell["cell_id"]})
        self._invoke_notebook("run", {"cell_id": value_cell["cell_id"]})
        return {
            **built,
            "control_cell_id": control_cell["cell_id"],
            "value_cell_id": value_cell["cell_id"],
        }

    def _list_components(self, _args: dict[str, Any]) -> Any:
        return self.components.list()

    def _write_component(self, args: dict[str, Any]) -> Any:
        return self.components.write(self._required(args, "path"), self._required(args, "source"))

    def _validate_component(self, args: dict[str, Any]) -> Any:
        return self.components.validate(self._required(args, "path"))

    def _component_instance_source(self, args: dict[str, Any]) -> Any:
        return self.components.instance_source(
            self._required(args, "path"),
            self._required(args, "callable"),
            self._required(args, "output_name"),
            args.get("bindings", {}),
        )

    def _add_component(self, args: dict[str, Any]) -> Any:
        built = self._component_instance_source(args)
        library_cell_id = None
        library_source = built.get("library_source")
        library_marker = built.get("library_marker")
        if isinstance(library_source, str) and isinstance(library_marker, str):
            snapshot = self._invoke_notebook("inspect", {})
            cells = snapshot.get("cells", []) if isinstance(snapshot, dict) else []
            library_cell = next(
                (
                    cell
                    for cell in cells
                    if isinstance(cell, dict) and library_marker in cell.get("source", "")
                ),
                None,
            )
            if library_cell is None:
                library_cell = self._invoke_notebook("add_cell", {"source": library_source})
                library_cell_id = library_cell.get("cell_id")
            else:
                library_cell_id = library_cell.get("id")
                if library_cell.get("source") != library_source:
                    self._invoke_notebook(
                        "replace_library",
                        {
                            "cell_id": library_cell_id,
                            "marker": library_marker,
                            "source": library_source,
                        },
                    )
            if isinstance(library_cell_id, str):
                self._invoke_notebook("run", {"cell_id": library_cell_id})
        inserted = self._invoke_notebook("add_cell", {"source": built["source"]})
        return {**built, **inserted, "library_cell_id": library_cell_id}

    def _delete_component(self, args: dict[str, Any]) -> Any:
        return self.components.delete(self._required(args, "path"))

    def _refresh_component_instances(self, _args: dict[str, Any]) -> Any:
        listing = self.components.list().get("items", [])
        hashes = {str(item["path"]): str(item["sha256"]) for item in listing}
        libraries = self.components.library_updates()
        return self._invoke_notebook(
            "refresh_component_hashes", {"hashes": hashes, "libraries": libraries}
        )

    def _inspect_artifact(self, args: dict[str, Any]) -> Any:
        artifact_id = self._required(args, "artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("artifact_id must be a non-empty string")
        store = self.host.artifact_store
        if store is None or not callable(getattr(store, "get", None)):
            raise ValueError("ArtifactStore inspection is unavailable for this host")
        reference = store.get(artifact_id)
        result = {"id": getattr(reference, "id", None), "kind": getattr(reference, "kind", None)}
        if not isinstance(result["id"], str) or not isinstance(result["kind"], str):
            raise TypeError("ArtifactStore get() must return an ArtifactRef with id and kind")
        return result

    def _artifact_stats(self, _args: dict[str, Any]) -> Any:
        store = self.host.artifact_store
        if store is None or not callable(getattr(store, "stats", None)):
            raise ValueError("ArtifactStore statistics are unavailable for this host")
        stats = store.stats()
        if not isinstance(stats, dict):
            raise TypeError("ArtifactStore stats() must return an object")
        return {"stats": stats}

    def _invoke_notebook(self, action: str, args: dict[str, Any]) -> Any:
        invoke = self.host.invoke_notebook
        if invoke is None:
            raise ValueError("no live marimo kernel is attached to this project")
        return invoke(action, args)

    def _enter_group(self, args: dict[str, Any]) -> Any:
        group_id = self._required(args, "container_id")
        definition = self.groups.get(group_id)
        if definition.contract.dialect_id != self._scope_stack[-1]["dialect_id"]:
            raise ValueError("Group Blocks must inherit their parent scope dialect")
        if any(item.get("container_id") == group_id for item in self._scope_stack):
            raise ValueError(f"Group nesting cycle detected for '{group_id}'")
        scope_id = "group_" + hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:32]
        self._scope_stack.append(
            {
                "scope_id": scope_id,
                "container_id": group_id,
                "name": definition.contract.name,
                "kind": "group",
                "dialect_id": definition.contract.dialect_id,
                "path": definition.relative_path,
            }
        )
        return self._current_scope({})

    def _leave_scope(self, _args: dict[str, Any]) -> Any:
        if len(self._scope_stack) == 1:
            raise ValueError("cannot leave the root Graphimo project scope")
        self._scope_stack.pop()
        return self._current_scope({})

    def _current_scope(self, _args: dict[str, Any]) -> Any:
        return {
            "scope": dict(self._scope_stack[-1]),
            "breadcrumbs": [dict(item) for item in self._scope_stack],
        }

    def _get_layout(self, args: dict[str, Any]) -> Any:
        return {"layout": self.host.read_layout(self._required(args, "scope_id"))}

    def _set_layout(self, args: dict[str, Any]) -> Any:
        self.host.write_layout(self._required(args, "scope_id"), self._required(args, "layout"))
        return {"saved": True}


def _bounded_json(value: Any, *, max_bytes: int = 4 * 1024 * 1024) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > max_bytes:
            raise ValueError(f"Graphimo action result exceeds the {max_bytes}-byte limit")
        return json.loads(encoded)
    except (TypeError, UnicodeEncodeError) as exc:
        raise TypeError("Graphimo application results must be bounded JSON values") from exc
