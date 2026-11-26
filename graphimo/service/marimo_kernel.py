"""Bounded named Graphimo actions sent to an attached local marimo kernel."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .kernel_action_sources import metablock_action_source, notebook_action_source

_ADDON_ACTION = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_MAX_KERNEL_ACTION_BYTES = 1024 * 1024


class MarimoKernelError(ValueError):
    """A named kernel action could not be delivered or completed."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class MarimoKernelPort:
    """Ephemeral HTTP control port; kernel runtime objects never leave Python."""

    def __init__(self, *, url: str, token: str, session_id: str, project_path: str):
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.path not in ("", "/")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("marimo kernel URL must be a 127.0.0.1 HTTP endpoint")
        if not isinstance(token, str) or "\r" in token or "\n" in token:
            raise ValueError("marimo kernel token must be single-line text")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError("marimo session_id must contain 1-256 characters")
        self.url = url.rstrip("/")
        self.token = token
        self.session_id = session_id
        self.project_path = project_path

    def invoke(self, metablock_id: str, action: str, args: dict[str, Any]) -> Any:
        if not isinstance(action, str) or not _ADDON_ACTION.fullmatch(action):
            raise MarimoKernelError(
                "kernel_action_unsupported", "addon action must be a bounded registered name"
            )
        if not isinstance(metablock_id, str) or not metablock_id:
            raise MarimoKernelError("invalid_request", "metablock_id is required")
        if not isinstance(args, dict):
            raise MarimoKernelError("invalid_request", "kernel action args must be an object")
        payload = {
            "project_path": self.project_path,
            "metablock_id": metablock_id,
            "action": action,
            "args": args,
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise MarimoKernelError(
                "invalid_request", "kernel action must be bounded JSON"
            ) from exc
        if len(encoded.encode("utf-8")) > _MAX_KERNEL_ACTION_BYTES:
            raise MarimoKernelError("request_too_large", "kernel action exceeds the 1 MiB limit")
        source = _action_source(encoded)
        return self._execute(source)

    def invoke_notebook(self, action: str, args: dict[str, Any]) -> Any:
        """Invoke a bounded ordinary notebook operation in the live marimo kernel."""
        if action not in {
            "inspect",
            "add_cell",
            "restore_cells",
            "connect",
            "run",
            "delete",
            "edit_guarded",
            "replace_group_selection",
            "disable_instance",
            "enable_instance",
            "replace_library",
            "set_literal",
            "disconnect",
            "refresh_component_hashes",
            "capture_output",
        }:
            raise MarimoKernelError(
                "notebook_action_unsupported", "notebook action is not supported"
            )
        if not isinstance(args, dict):
            raise MarimoKernelError("invalid_request", "notebook action args must be an object")
        if "_graphimo_project_path" in args:
            raise MarimoKernelError(
                "invalid_request", "notebook action uses a reserved internal argument"
            )
        payload = {**args, "_graphimo_project_path": self.project_path}
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise MarimoKernelError(
                "invalid_request", "notebook action must be bounded JSON"
            ) from exc
        if len(encoded.encode("utf-8")) > _MAX_KERNEL_ACTION_BYTES:
            raise MarimoKernelError("request_too_large", "notebook action exceeds the 1 MiB limit")
        return self._execute(_notebook_action_source(action, encoded))

    def stop_publisher(self) -> None:
        """Stop every installed addon kernel hook, discovered by entry point.

        The Core never imports an addon by name: any distribution exposing the
        ``graphimo.kernel_hooks`` entry-point group with a callable (or an
        object with a ``stop`` method) is stopped here.
        """
        source = "\n".join(
            (
                "import json",
                "from importlib.metadata import entry_points",
                "result = {}",
                "for ep in entry_points(group='graphimo.kernel_hooks'):",
                "    try:",
                "        hook = ep.load()",
                "        stop = getattr(hook, 'stop', None)",
                "        if not callable(stop) and callable(hook): stop = hook",
                "        if callable(stop): result[ep.name] = stop()",
                "    except Exception as exc:",
                "        result[ep.name] = {'error': str(exc)[:200]}",
                "print('GRAPHIMO_KERNEL_RESULT:' + json.dumps({'ok': True, 'result': result}, ensure_ascii=False, allow_nan=False, separators=(',', ':'))) ",
            )
        )
        self._execute(source)

    def _execute(self, source: str) -> Any:
        body = json.dumps({"code": source}, ensure_ascii=False).encode("utf-8")
        headers = {
            "Marimo-Session-Id": self.session_id,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.url + "/api/kernel/execute",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MarimoKernelError("kernel_unavailable", "marimo kernel action failed") from exc
        if len(raw) > 4 * 1024 * 1024:
            raise MarimoKernelError("kernel_response_too_large", "marimo response exceeds 4 MiB")
        result = _parse_result(raw.decode("utf-8", errors="replace"))
        if not result.get("ok"):
            error = result.get("error", {})
            raise MarimoKernelError(
                str(error.get("code", "kernel_action_failed")),
                str(error.get("message", "marimo kernel action failed")),
            )
        return result.get("result")


def _action_source(encoded_payload: str) -> str:
    """Static kernel snippet for one MetaBlock addon action (see kernel_action_sources)."""
    return metablock_action_source(encoded_payload)


def _notebook_action_source(action: str, encoded_payload: str) -> str:
    """Static kernel snippet for one allowlisted notebook action."""
    return notebook_action_source(action, encoded_payload)


def _parse_result(text: str) -> dict[str, Any]:
    output = []
    event = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line.partition(":")[2].strip()
        elif line.startswith("data:") and event == "stdout":
            try:
                record = json.loads(line.partition(":")[2].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and isinstance(record.get("data"), str):
                output.append(record["data"])
    joined = "".join(output)
    marker = "GRAPHIMO_KERNEL_RESULT:"
    position = joined.find(marker)
    if position < 0:
        diagnostics: list[str] = []
        current_event = None
        for line in text.splitlines():
            if line.startswith("event:"):
                current_event = line.partition(":")[2].strip()
            elif line.startswith("data:") and current_event in {"stderr", "error"}:
                diagnostics.append(line.partition(":")[2].strip())
        detail = " ".join(diagnostics)[:500]
        raise MarimoKernelError(
            "kernel_action_failed",
            "marimo did not return a structured Graphimo action result"
            + (f": {detail}" if detail else ""),
        )
    line = joined[position + len(marker) :].splitlines()[0]
    try:
        result = json.loads(line)
    except json.JSONDecodeError as exc:
        raise MarimoKernelError(
            "kernel_invalid_response", "marimo returned invalid action JSON"
        ) from exc
    if not isinstance(result, dict):
        raise MarimoKernelError("kernel_invalid_response", "marimo returned a non-object result")
    return result
