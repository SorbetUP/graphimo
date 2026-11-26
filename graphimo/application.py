"""Host-independent application intents for Graphimo semantic scopes."""

from __future__ import annotations

import json
from typing import Any

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
