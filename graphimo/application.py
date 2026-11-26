"""Host-independent application intents for Graphimo semantic scopes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .groups import GroupLibrary
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
        if len(json.dumps(args, allow_nan=False).encode("utf-8")) > 256 * 1024:
            raise ValueError("action args exceed the 256 KiB limit")

        operations = {
            "metablock.list": self._list_metablocks,
            "metablock.create": self._create_metablock,
            "metablock.catalog": self._catalog,
            "metablock.project": self._project,
            "metablock.apply": self._apply,
            "metablock.materialize": self._materialize,
            "metablock.invoke": self._invoke,
            "metablock.events": self._events,
            "group.list": self._list_groups,
            "group.create": self._create_group,
            "group.inspect": self._inspect_group,
            "group.set_contract": self._set_group_contract,
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

    def _inspect_group(self, args: dict[str, Any]) -> Any:
        return self.groups.get(self._required(args, "group_id")).to_catalog_item()

    def _set_group_contract(self, args: dict[str, Any]) -> Any:
        group_id = self._required(args, "group_id")
        request = args.get("contract", {})
        return self.groups.update_contract(group_id, request).to_catalog_item()

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
