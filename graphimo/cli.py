"""Command-line interface to the local Graphimo service.

Any agent or human with a shell can drive Graphimo without an MCP connector::

    graphimo --path /proj/nb.py inspect
    graphimo --path /proj/nb.py attach --url http://127.0.0.1:2719 --session-id live-1
    graphimo --path /proj/nb.py run Hbol
    graphimo --path /proj/nb.py connect --from-cell Hbol --variable data --to-cell zKhV --input-ref values
    graphimo --path /proj/nb.py do notebook.set_literal '{"instance_id": "...", "input_name": "values", "value": [1, 2]}'

Results are printed as JSON on stdout. Service errors are printed as a
structured ``{"ok": false, "error": {...}}`` on stderr with exit code 1.
The project path can also come from the ``GRAPHIMO_PROJECT`` environment
variable; the service manifest from ``--manifest`` or the default state path
(the service is auto-started on first use).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .client import GraphimoClient, GraphimoServiceError


def _json_argument(raw: str, what: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"{what} must be valid JSON: {exc}") from exc


def _load_json_file_or_text(raw: str, what: str) -> Any:
    path = Path(raw)
    if path.is_file():
        return _json_argument(path.read_text(encoding="utf-8"), what)
    return _json_argument(raw, what)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _fail(error: GraphimoServiceError) -> int:
    print(
        json.dumps(
            {"ok": False, "error": {"code": error.code, "message": str(error)}},
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return 1


def _project(args: argparse.Namespace, client: GraphimoClient):
    path = args.path or os.environ.get("GRAPHIMO_PROJECT")
    if not path:
        raise SystemExit("error: provide --path or set GRAPHIMO_PROJECT")
    return client.open_project(path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graphimo",
        description="Drive a local Graphimo project (marimo notebook + components) from the shell.",
    )
    parser.add_argument(
        "--manifest", default=None, help="service manifest path (default: state path)"
    )
    parser.add_argument("--path", default=None, help="project notebook path (or GRAPHIMO_PROJECT)")
    parser.add_argument("--compact", action="store_true", help="print compact JSON")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--compact", action="store_true", default=argparse.SUPPRESS, help="print compact JSON"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", parents=[common], help="check the local service")

    p = sub.add_parser(
        "attach", parents=[common], help="attach a live marimo kernel to the project"
    )
    p.add_argument("--url", required=True, help="marimo base URL, e.g. http://127.0.0.1:2719")
    p.add_argument("--session-id", required=True, help="marimo websocket session id")
    p.add_argument("--token", default="", help="marimo auth token (if any)")

    sub.add_parser("inspect", parents=[common], help="notebook.inspect")
    p = sub.add_parser("run", parents=[common], help="notebook.run")
    p.add_argument("cell_id")
    p = sub.add_parser("add-cell", parents=[common], help="notebook.add_cell")
    p.add_argument("source")
    p = sub.add_parser("delete-cell", parents=[common], help="notebook.delete")
    p.add_argument("cell_id")
    p = sub.add_parser("connect", parents=[common], help="notebook.connect")
    p.add_argument("--from-cell", required=True)
    p.add_argument("--variable", required=True)
    p.add_argument("--to-cell", required=True)
    p.add_argument("--input-ref", required=True)
    p.add_argument("--output-index", type=int, default=None)
    p = sub.add_parser("disconnect", parents=[common], help="notebook.disconnect")
    p.add_argument("--cell-id", default=None)
    p.add_argument("--instance-id", default=None)
    p.add_argument("--input", dest="input_name", required=True)
    p = sub.add_parser("set-literal", parents=[common], help="notebook.set_literal")
    p.add_argument("--cell-id", default=None)
    p.add_argument("--instance-id", default=None)
    p.add_argument("--input", dest="input_name", required=True)
    p.add_argument("--value", required=True, help="JSON literal value")
    p = sub.add_parser("disable", parents=[common], help="notebook.disable_instance")
    p.add_argument("--cell-id", default=None)
    p.add_argument("--instance-id", default=None)
    p = sub.add_parser("enable", parents=[common], help="notebook.enable_instance")
    p.add_argument("--cell-id", default=None)
    p.add_argument("--instance-id", default=None)
    p = sub.add_parser("capture-output", parents=[common], help="notebook.capture_output")
    p.add_argument("--cell-id", required=True)
    p.add_argument("--result-var", required=True)
    p.add_argument("--output-index", type=int, default=0)
    p.add_argument("--pin", action="store_true")

    sub.add_parser("components", parents=[common], help="component.list")
    p = sub.add_parser("component-add", parents=[common], help="component.add")
    p.add_argument("--component", required=True, help="relative path under components/")
    p.add_argument("--callable", required=True)
    p.add_argument("--output", required=True, help="output variable name")
    p.add_argument("--bindings", default="{}", help="JSON bindings object")
    sub.add_parser("controls", parents=[common], help="control.catalog")
    sub.add_parser("groups", parents=[common], help="group.list")
    sub.add_parser("metablocks", parents=[common], help="metablock.list")
    p = sub.add_parser("metablock-create", parents=[common], help="metablock.create")
    p.add_argument("--dialect", required=True)
    p.add_argument("--id", dest="metablock_id", required=True)
    p.add_argument("--request", default="{}", help="JSON creation request")
    p = sub.add_parser("addons", parents=[common], help="addon.list")
    p.add_argument("--addon", default=None, help="describe one addon by id")
    sub.add_parser("artifacts", parents=[common], help="artifact.list")
    sub.add_parser("runs", parents=[common], help="run.list")
    sub.add_parser("scope", parents=[common], help="scope.current")

    p = sub.add_parser("events", parents=[common], help="drain service events")
    p.add_argument("--cursor", default=None)
    p.add_argument("--limit", type=int, default=128)
    p.add_argument("--wait-ms", type=int, default=750)
    p.add_argument(
        "--follow", action="store_true", help="keep printing new events until interrupted"
    )

    p = sub.add_parser(
        "batch", parents=[common], help="run a batch of operations from JSON text or a file"
    )
    p.add_argument("operations", help="JSON array of {id, action, args} or path to a JSON file")
    p.add_argument("--continue-on-error", action="store_true")

    p = sub.add_parser("do", parents=[common], help="generic escape hatch: any service action")
    p.add_argument("action", help="e.g. notebook.inspect, metablock.apply, ...")
    p.add_argument("args", nargs="?", default="{}", help="JSON object of arguments")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    compact = getattr(args, "compact", False)

    def emit(payload: Any) -> None:
        if compact:
            print(json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")))
        else:
            _emit(payload)

    def emit_result(value: Any) -> None:
        emit({"ok": True, "result": value})

    try:
        client = GraphimoClient.connect_local(manifest_path=args.manifest)
    except GraphimoServiceError as error:
        return _fail(error)

    try:
        if args.command == "health":
            emit_result(client.health())
            return 0

        project = _project(args, client)

        if args.command == "attach":
            emit_result(
                project.attach_kernel(url=args.url, token=args.token, session_id=args.session_id)
            )
            return 0

        if args.command == "inspect":
            emit_result(project.execute("notebook.inspect", {}))
        elif args.command == "run":
            emit_result(project.execute("notebook.run", {"cell_id": args.cell_id}))
        elif args.command == "add-cell":
            emit_result(project.execute("notebook.add_cell", {"source": args.source}))
        elif args.command == "delete-cell":
            emit_result(project.execute("notebook.delete", {"cell_id": args.cell_id}))
        elif args.command == "connect":
            payload: dict[str, Any] = {
                "from_cell_id": args.from_cell,
                "variable": args.variable,
                "to_cell_id": args.to_cell,
                "input_ref": args.input_ref,
            }
            if args.output_index is not None:
                payload["output_index"] = args.output_index
            emit_result(project.execute("notebook.connect", payload))
        elif args.command == "disconnect":
            emit_result(
                project.execute(
                    "notebook.disconnect", _target(args) | {"input_name": args.input_name}
                )
            )
        elif args.command == "set-literal":
            emit(
                project.execute(
                    "notebook.set_literal",
                    _target(args)
                    | {
                        "input_name": args.input_name,
                        "value": _json_argument(args.value, "--value"),
                    },
                )
            )
        elif args.command in {"disable", "enable"}:
            action = (
                "notebook.disable_instance"
                if args.command == "disable"
                else "notebook.enable_instance"
            )
            emit_result(project.execute(action, _target(args)))
        elif args.command == "capture-output":
            emit(
                project.execute(
                    "notebook.capture_output",
                    {
                        "cell_id": args.cell_id,
                        "result_var": args.result_var,
                        "output_index": args.output_index,
                        "pin": args.pin,
                    },
                )
            )
        elif args.command == "components":
            emit_result(project.execute("component.list", {}))
        elif args.command == "component-add":
            emit(
                project.execute(
                    "component.add",
                    {
                        "path": args.component,
                        "callable": args.callable,
                        "output_name": args.output,
                        "bindings": _json_argument(args.bindings, "--bindings"),
                    },
                )
            )
        elif args.command == "controls":
            emit_result(project.execute("control.catalog", {}))
        elif args.command == "groups":
            emit_result(project.execute("group.list", {}))
        elif args.command == "metablocks":
            emit_result(project.execute("metablock.list", {}))
        elif args.command == "metablock-create":
            emit(
                project.execute(
                    "metablock.create",
                    {
                        "dialect_id": args.dialect,
                        "metablock_id": args.metablock_id,
                        "request": _json_argument(args.request, "--request"),
                    },
                )
            )
        elif args.command == "addons":
            if args.addon:
                emit_result(project.execute("addon.describe", {"addon_id": args.addon}))
            else:
                emit_result(project.execute("addon.list", {}))
        elif args.command == "artifacts":
            emit_result(project.execute("artifact.list", {}))
        elif args.command == "runs":
            emit_result(project.execute("run.list", {}))
        elif args.command == "scope":
            emit_result(project.execute("scope.current", {}))
        elif args.command == "events":
            cursor = args.cursor
            while True:
                page = project.events(cursor=cursor, limit=args.limit, wait_ms=args.wait_ms)
                emit_result(page)
                cursor = page.get("cursor")
                if not args.follow:
                    return 0
        elif args.command == "batch":
            operations = _load_json_file_or_text(args.operations, "operations")
            if not isinstance(operations, list):
                raise SystemExit("error: batch operations must be a JSON array")
            emit_result(project.batch(operations, continue_on_error=args.continue_on_error))
        elif args.command == "do":
            arguments = _json_argument(args.args, "args")
            if not isinstance(arguments, dict):
                raise SystemExit("error: do args must be a JSON object")
            emit_result(project.execute(args.action, arguments))
        return 0
    except GraphimoServiceError as error:
        return _fail(error)
    finally:
        client.close()


def _target(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if getattr(args, "cell_id", None):
        payload["cell_id"] = args.cell_id
    if getattr(args, "instance_id", None):
        payload["instance_id"] = args.instance_id
    if not payload:
        raise SystemExit("error: provide --cell-id or --instance-id")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
