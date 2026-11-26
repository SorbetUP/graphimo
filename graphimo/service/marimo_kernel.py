"""Bounded named Graphimo actions sent to an attached local marimo kernel."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

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
        source = "\n".join(
            (
                "import json",
                "from graphimo_pytorch.publisher import stop_kernel_publisher",
                "result = stop_kernel_publisher()",
                "print('GRAPHIMO_KERNEL_RESULT:' + json.dumps({'ok': True, 'result': result}, separators=(',', ':'))) ",
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
    return "\n".join(
        (
            "import json",
            "from graphimo.application import GraphimoApplication",
            "from graphimo.metablocks import GraphimoHostAPI",
            "from graphimo.service.sessions import HostArtifactStore",
            f"payload = json.loads({encoded_payload!r})",
            "host = GraphimoHostAPI(project_id='kernel-runtime', project_path=__import__('pathlib').Path(payload['project_path']), artifact_store=HostArtifactStore())",
            "application = GraphimoApplication(host)",
            "result = application.execute('metablock.invoke', {'metablock_id': payload['metablock_id'], 'action': payload['action'], 'args': payload['args']})",
            "print('GRAPHIMO_KERNEL_RESULT:' + json.dumps({'ok': True, 'result': result}, ensure_ascii=False, allow_nan=False, separators=(',', ':'))) ",
        )
    )


def _notebook_action_source(action: str, encoded_payload: str) -> str:
    """Generate static marimo code for allowlisted operations; never accept source code here."""
    common = [
        "import json",
        "import marimo._code_mode as cm",
        f"payload = json.loads({encoded_payload!r})",
        "async with cm.get_context() as ctx:",
    ]
    if action == "add_cell":
        common.extend(
            [
                "    source = payload.get('source')",
                "    if not isinstance(source, str) or not source.strip(): raise ValueError('cell source must be non-empty text')",
                "    if len(source.encode('utf-8')) > 1048576: raise ValueError('cell source exceeds 1 MiB')",
                "    compile(source, '<graphimo-service-cell>', 'exec')",
                "    cell_id = ctx.create_cell(source, hide_code=False)",
                "    result = {'ok': True, 'cell_id': str(cell_id), 'source': source}",
            ]
        )
    elif action == "restore_cells":
        common.extend(
            [
                "    records = payload.get('records')",
                "    if not isinstance(records, list) or len(records) > 128: raise ValueError('invalid_records: expected at most 128 cell records')",
                "    sources = []",
                "    total_size = 0",
                "    for record in records:",
                "        if not isinstance(record, dict) or not isinstance(record.get('source'), str): raise ValueError('invalid_records: each cell record must have source text')",
                "        source = record['source']; size = len(source.encode('utf-8')); total_size += size",
                "        if size > 1048576 or total_size > 4194304: raise ValueError('invalid_records: restored cell sources exceed the size limit')",
                "        compile(source, '<graphimo-history-restore>', 'exec'); sources.append(source)",
                "    created = []",
                "    try:",
                "        for source in sources: created.append(ctx.create_cell(source, hide_code=False))",
                "        result = {'ok': True, 'cell_ids': [str(cell_id) for cell_id in created]}",
                "    except Exception as exc:",
                "        rollback_errors = []",
                "        for cell_id in reversed(created):",
                "            try: ctx.delete_cell(cell_id)",
                "            except Exception as rollback_exc: rollback_errors.append(str(rollback_exc))",
                "        result = {'ok': False, 'error': {'code': 'restore_failed', 'message': str(exc), 'rollback_errors': rollback_errors}}",
            ]
        )
    elif action == "delete":
        common.extend(
            [
                "    cell_id = payload.get('cell_id')",
                "    if not isinstance(cell_id, str) or not cell_id or len(cell_id) > 256: raise ValueError('cell_id must be 1-256 characters')",
                "    ctx.delete_cell(cell_id)",
                "    result = {'ok': True, 'cell_id': cell_id, 'deleted': True}",
            ]
        )
    elif action == "edit_guarded":
        common.extend(
            [
                "    cell_id = payload.get('cell_id'); expected_source = payload.get('expected_source'); replacement_source = payload.get('source')",
                "    try: cell = ctx.cells[cell_id]",
                "    except (KeyError, IndexError, TypeError): cell = None",
                "    if cell is None: result = {'ok': False, 'error': {'code': 'unsafe_edit', 'message': 'history target cell no longer exists'}}",
                "    elif cell.code != expected_source: result = {'ok': False, 'error': {'code': 'unsafe_edit', 'message': 'source changed since this history entry was recorded'}}",
                "    elif not isinstance(replacement_source, str) or len(replacement_source.encode('utf-8')) > 1048576: result = {'ok': False, 'error': {'code': 'unsafe_edit', 'message': 'replacement source is invalid or exceeds 1 MiB'}}",
                "    else:",
                "        try:",
                "            compile(replacement_source, '<graphimo-history>', 'exec')",
                "            ctx.edit_cell(cell.id, code=replacement_source)",
                "            result = {'ok': True, 'changed': True, 'cell_id': str(cell.id), 'source': replacement_source}",
                "        except Exception as exc: result = {'ok': False, 'error': {'code': 'unsafe_edit', 'message': str(exc)}}",
            ]
        )
    elif action == "replace_group_selection":
        common.extend(
            [
                "    import ast, hashlib",
                "    selected_ids = payload.get('selected_ids'); source_hashes = payload.get('source_hashes'); replacement_source = payload.get('parent_source')",
                "    cells = {str(cell.id): cell for cell in ctx.cells}",
                "    if not isinstance(selected_ids, list) or not 2 <= len(selected_ids) <= 128 or any(not isinstance(cell_id, str) or not cell_id for cell_id in selected_ids) or len(set(selected_ids)) != len(selected_ids): result = {'ok': False, 'error': {'code': 'invalid_selection', 'message': 'choose 2-128 unique cells'}}",
                "    elif not isinstance(source_hashes, dict) or any(cell_id not in cells or not isinstance(source_hashes.get(cell_id), str) or hashlib.sha256(cells[cell_id].code.encode('utf-8')).hexdigest() != source_hashes[cell_id] for cell_id in selected_ids): result = {'ok': False, 'error': {'code': 'unsafe_edit', 'message': 'selected cell source changed; inspect the graph and retry'}}",
                "    elif not isinstance(replacement_source, str) or not replacement_source.strip() or len(replacement_source.encode('utf-8')) > 1048576: result = {'ok': False, 'error': {'code': 'invalid_replacement', 'message': 'generated Group call source is empty or exceeds 1 MiB'}}",
                "    else:",
                "        originals = {cell_id: cells[cell_id].code for cell_id in selected_ids}; removed = []",
                "        try:",
                "            compile(replacement_source, '<graphimo-group-call>', 'exec')",
                "            if not replacement_source.startswith('# Graphimo group: '): raise ValueError('missing Graphimo Group marker')",
                "            statements = ast.parse(replacement_source).body",
                "            if len(statements) < 2 or not isinstance(statements[0], ast.ImportFrom) or not isinstance(statements[0].module, str) or not statements[0].module.startswith('groups.') or len(statements[0].names) != 1: raise ValueError('replacement must import exactly one project Group')",
                "            imported = statements[0].names[0]",
                "            if imported.asname is not None or not imported.name.isidentifier() or imported.name.startswith('__'): raise ValueError('invalid project Group callable')",
                "            invocation = statements[1].value if isinstance(statements[1], (ast.Expr, ast.Assign)) else None",
                "            if isinstance(statements[1], ast.Assign) and (len(statements[1].targets) != 1 or not isinstance(statements[1].targets[0], ast.Name) or not statements[1].targets[0].id.startswith('__graphimo_group_result_')): raise ValueError('replacement must capture the imported Group result')",
                "            if not isinstance(invocation, ast.Call) or not isinstance(invocation.func, ast.Name) or invocation.func.id != imported.name or invocation.args: raise ValueError('replacement must call the imported Group with named inputs')",
                "            if any(keyword.arg is None or not keyword.arg.isidentifier() or not isinstance(keyword.value, ast.Name) for keyword in invocation.keywords): raise ValueError('Group inputs must be simple named Python variables')",
                "            for statement in statements[2:] if isinstance(statements[1], ast.Assign) else []:",
                "                if not isinstance(statement, ast.Assign) or len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name) or not isinstance(statement.value, ast.Attribute) or not isinstance(statement.value.value, ast.Name) or statement.value.value.id != statements[1].targets[0].id: raise ValueError('replacement contains an invalid Group output binding')",
                "            for cell_id in selected_ids: ctx.delete_cell(cells[cell_id].id); removed.append(cell_id)",
                "            replacement_id = ctx.create_cell(replacement_source, hide_code=False)",
                "            result = {'ok': True, 'cell_id': str(replacement_id), 'deleted_cell_ids': selected_ids, 'source': replacement_source}",
                "        except Exception as exc:",
                "            restored = []; rollback_errors = []",
                "            for cell_id in removed:",
                "                try: restored.append(str(ctx.create_cell(originals[cell_id], hide_code=False)))",
                "                except Exception as rollback_exc: rollback_errors.append(str(rollback_exc))",
                "            result = {'ok': False, 'error': {'code': 'replacement_failed', 'message': str(exc), 'restored_cell_ids': restored, 'rollback_errors': rollback_errors}}",
            ]
        )
    elif action == "refresh_component_hashes":
        common.extend(
            [
                "    import re",
                "    hashes = payload.get('hashes')",
                "    if not isinstance(hashes, dict) or len(hashes) > 4096 or any(not isinstance(path, str) or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) for path, digest in hashes.items()): raise ValueError('invalid component source hashes')",
                "    changed = []",
                "    for cell in ctx.cells:",
                "        component = re.search(r'(?m)^# Graphimo component: (.+)$', cell.code)",
                "        old = re.search(r'(?m)^# Graphimo source sha256: ([0-9a-f]+)$', cell.code)",
                "        if component and old and hashes.get(component.group(1)) and hashes[component.group(1)] != old.group(1):",
                "            updated = re.sub(r'(?m)^# Graphimo source sha256: [0-9a-f]+$', '# Graphimo source sha256: ' + hashes[component.group(1)], cell.code, count=1)",
                "            ctx.edit_cell(cell.id, code=updated); changed.append(str(cell.id))",
                "    result = {'ok': True, 'changed_cells': changed}",
            ]
        )
    elif action == "run":
        common.extend(
            [
                "    import importlib, inspect, pathlib, sys",
                "    from marimo._runtime.commands import ExecuteCellCommand",
                "    from graphimo.dependencies import blocked_dependencies_for_graph",
                "    from graphimo.instance import parse_instance_source",
                "    async def _run_cell_with_stale_ancestors(target_id):",
                "        roots = {target_id}",
                "        roots.update(ancestor_id for ancestor_id in ctx.graph.ancestors(target_id) if ctx.cells[ancestor_id].status == 'stale')",
                "        requests = [ExecuteCellCommand(cell_id=run_id, code=ctx.cells[run_id].code) for run_id in roots]",
                "        await ctx._kernel.run(requests)",
                "    cell_id = payload.get('cell_id')",
                "    if not isinstance(cell_id, str) or not cell_id or len(cell_id) > 256: raise ValueError('cell_id must be 1-256 characters')",
                "    blocked_by = blocked_dependencies_for_graph(ctx.graph.cells, cell_id)",
                "    if blocked_by:",
                "        result = {'ok': False, 'error': {'code': 'blocked_by_disabled_dependency', 'message': 'Cell is blocked by disabled upstream cell(s): ' + ', '.join(blocked_by), 'blocked_by': blocked_by}}",
                "    else:",
                "        cell = ctx.cells[cell_id]; instance = parse_instance_source(cell.code)",
                "        if instance is not None:",
                "            project_root = pathlib.Path(payload['_graphimo_project_path']).resolve().parent",
                "            if str(project_root) not in sys.path: sys.path.insert(0, str(project_root))",
                "            function = None",
                "            for library_cell in ctx.cells:",
                "                for line in library_cell.code.splitlines():",
                "                    prefix = '# Graphimo node library: '",
                "                    if not line.startswith(prefix): continue",
                "                    relative, separator, exported = line[len(prefix):].rpartition(':')",
                "                    if not separator or exported != instance.node_name: continue",
                "                    path = pathlib.PurePosixPath(relative)",
                "                    if path.is_absolute() or '..' in path.parts or path.suffix != '.py': raise ValueError('invalid Graphimo node library marker')",
                "                    module_path = path.parent if path.name == '__init__.py' else path.with_suffix('')",
                "                    module = importlib.import_module('.'.join(['components', *module_path.parts]))",
                "                    function = getattr(module, instance.node_name, None)",
                "                    break",
                "                if function is not None: break",
                "            if function is None: function = ctx.globals.get(instance.node_name)",
                "            if function is not None:",
                "                parameters = inspect.signature(function).parameters",
                "                bound = {binding.input_name for binding in instance.bindings}",
                "                missing = [name for name, parameter in parameters.items() if parameter.default is inspect.Parameter.empty and parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD) and name not in bound]",
                "                if missing: result = {'ok': False, 'error': {'code': 'missing_required_binding', 'message': 'Required node input(s) are not connected: ' + ', '.join(missing), 'inputs': missing}}",
                "                else: await _run_cell_with_stale_ancestors(cell_id); result = {'ok': True, 'cell_id': cell_id, 'status': 'queued'}",
                "            else: await _run_cell_with_stale_ancestors(cell_id); result = {'ok': True, 'cell_id': cell_id, 'status': 'queued'}",
                "        else: await _run_cell_with_stale_ancestors(cell_id); result = {'ok': True, 'cell_id': cell_id, 'status': 'queued'}",
            ]
        )
    elif action in {"disable_instance", "enable_instance"}:
        operation = (
            "disable_instance_source" if action == "disable_instance" else "enable_instance_source"
        )
        common.extend(
            [
                "    from graphimo.instance import " + operation,
                "    cell_id = payload.get('cell_id')",
                "    try:",
                "        if not isinstance(cell_id, str) or not cell_id or len(cell_id) > 256: raise ValueError('cell_id must be 1-256 characters')",
                "        cell = ctx.cells[cell_id]",
                f"        updated = {operation}(cell.code)",
                "        changed = updated != cell.code",
                "        ctx.edit_cell(cell.id, code=updated)",
                "        result = {'ok': True, 'cell_id': str(cell.id), 'changed': changed, 'source': updated}",
                "    except (TypeError, ValueError, KeyError) as error:",
                "        message = 'cell not found' if isinstance(error, KeyError) else str(error)[:2000]",
                "        result = {'ok': False, 'error': {'code': 'unsafe_edit', 'message': message}}",
            ]
        )
    elif action == "replace_library":
        common.extend(
            [
                "    cell_id = payload.get('cell_id'); marker = payload.get('marker'); source = payload.get('source')",
                "    try:",
                "        if not isinstance(cell_id, str) or not cell_id or len(cell_id) > 256: raise ValueError('cell_id must be 1-256 characters')",
                "        if not isinstance(marker, str) or not marker.startswith('# Graphimo node library: '): raise ValueError('invalid Graphimo library marker')",
                "        if not isinstance(source, str) or not source.startswith(marker + '\\n') or len(source.encode('utf-8')) > 1048576: raise ValueError('invalid or oversized Graphimo library source')",
                "        compile(source, '<graphimo-library-update>', 'exec')",
                "        cell = ctx.cells[cell_id]",
                "        if marker not in cell.code.splitlines(): raise ValueError('selected cell is not the marked Graphimo library')",
                "        ctx.edit_cell(cell.id, code=source)",
                "        result = {'ok': True, 'cell_id': str(cell.id), 'changed': True}",
                "    except (TypeError, ValueError, KeyError) as error:",
                "        message = 'cell not found' if isinstance(error, KeyError) else str(error)[:2000]",
                "        result = {'ok': False, 'error': {'code': 'unsafe_edit', 'message': message}}",
            ]
        )
    elif action == "set_literal":
        common.extend(
            [
                "    import importlib, json, pathlib, sys",
                "    from graphimo import inspect_node, parse_instance_source, reconnect_instance_source",
                "    instance_id = payload.get('instance_id'); input_name = payload.get('input_name')",
                "    try:",
                "        if not isinstance(instance_id, str) or not instance_id: raise ValueError('instance_id is required')",
                "        if not isinstance(input_name, str) or not input_name.isidentifier(): raise ValueError('input_name must be a Python identifier')",
                "        encoded_value = json.dumps(payload.get('value'), ensure_ascii=False, allow_nan=False, separators=(',', ':'))",
                "        if len(encoded_value.encode('utf-8')) > 16384: raise ValueError('literal value exceeds 16 KiB')",
                "        expression = f\"__import__('json').loads({encoded_value!r})\"",
                "        selected = None; instance = None",
                "        for candidate in ctx.cells:",
                "            candidate_instance = parse_instance_source(candidate.code)",
                "            if candidate_instance is not None and candidate_instance.instance_id == instance_id:",
                "                selected = candidate; instance = candidate_instance; break",
                "        if selected is None or instance is None: raise ValueError('Graphimo node instance was not found or is disabled')",
                "        project_root = pathlib.Path(payload['_graphimo_project_path']).resolve().parent",
                "        if str(project_root) not in sys.path: sys.path.insert(0, str(project_root))",
                "        function = None",
                "        for library_cell in ctx.cells:",
                "            for line in library_cell.code.splitlines():",
                "                prefix = '# Graphimo node library: '",
                "                if not line.startswith(prefix): continue",
                "                relative, separator, exported = line[len(prefix):].rpartition(':')",
                "                if not separator or exported != instance.node_name: continue",
                "                path = pathlib.PurePosixPath(relative)",
                "                if path.is_absolute() or '..' in path.parts or path.suffix != '.py': raise ValueError('invalid Graphimo node library marker')",
                "                module_path = path.parent if path.name == '__init__.py' else path.with_suffix('')",
                "                module = importlib.import_module('.'.join(['components', *module_path.parts]))",
                "                function = getattr(module, instance.node_name, None)",
                "                break",
                "            if function is not None: break",
                "        if function is None: function = ctx.globals.get(instance.node_name)",
                "        if function is None: raise ValueError('node definition is unavailable')",
                "        node_spec = inspect_node(function)",
                "        if input_name not in {port.name for port in node_spec.inputs}: raise ValueError('node has no input named ' + input_name)",
                "        updated = reconnect_instance_source(selected.code, input_name=input_name, expression=expression)",
                "        ctx.edit_cell(selected.id, code=updated)",
                "        result = {'ok': True, 'cell_id': str(selected.id), 'instance_id': instance_id, 'input_name': input_name, 'source': updated}",
                "    except (TypeError, ValueError, KeyError) as error:",
                "        result = {'ok': False, 'error': {'code': 'invalid_literal', 'message': str(error)[:2000]}}",
            ]
        )
    elif action == "disconnect":
        common.extend(
            [
                "    from graphimo import disconnect_instance_source, parse_instance_source",
                "    cell_id = payload.get('cell_id'); input_name = payload.get('input_name')",
                "    try:",
                "        if not isinstance(cell_id, str) or not cell_id or len(cell_id) > 256: raise ValueError('cell_id must be 1-256 characters')",
                "        cell = ctx.cells[cell_id]",
                "        instance = parse_instance_source(cell.code)",
                "        if instance is None: raise ValueError('target is not a canonical Graphimo node instance')",
                "        updated = disconnect_instance_source(cell.code, input_name=input_name)",
                "        changed = updated != cell.code",
                "        ctx.edit_cell(cell.id, code=updated)",
                "        result = {'ok': True, 'cell_id': str(cell.id), 'instance_id': instance.instance_id, 'input_name': input_name, 'changed': changed, 'source': updated}",
                "    except (TypeError, ValueError, KeyError) as error:",
                "        message = 'cell not found' if isinstance(error, KeyError) else str(error)[:2000]",
                "        result = {'ok': False, 'error': {'code': 'unsafe_edit', 'message': message}}",
            ]
        )
    elif action == "connect":
        common.extend(
            [
                "    import importlib, pathlib, sys",
                "    from graphimo import build_instance_source, inspect_node, output_expression, parse_instance_source, reconnect_instance_source",
                "    source_id = payload.get('from_cell_id'); target_id = payload.get('to_cell_id')",
                "    variable = payload.get('variable'); input_ref = payload.get('input_ref')",
                "    if any(not isinstance(item, str) or not item or len(item) > 256 for item in (source_id, target_id)): raise ValueError('cell IDs must contain 1-256 characters')",
                "    if any(not isinstance(item, str) or not item.isidentifier() for item in (variable, input_ref)): raise ValueError('connection ports must be Python identifiers')",
                "    source = ctx.cells[source_id]; target = ctx.cells[target_id]",
                "    source_impl = ctx.graph.cells.get(source.id); target_impl = ctx.graph.cells.get(target.id)",
                "    target_instance = parse_instance_source(target.code)",
                "    if target_instance is None: raise ValueError('unsafe_edit: target is not a Graphimo-owned node instance')",
                "    if not source_impl or not target_impl: raise ValueError('unsafe_edit: selected source or target is stale')",
                "    def _resolve_node(name):",
                "        project_root = pathlib.Path(payload['_graphimo_project_path']).resolve().parent",
                "        if str(project_root) not in sys.path: sys.path.insert(0, str(project_root))",
                "        for library_cell in ctx.cells:",
                "            for line in library_cell.code.splitlines():",
                "                prefix = '# Graphimo node library: '",
                "                if not line.startswith(prefix): continue",
                "                relative, separator, exported = line[len(prefix):].rpartition(':')",
                "                if not separator or exported != name: continue",
                "                path = pathlib.PurePosixPath(relative)",
                "                if path.is_absolute() or '..' in path.parts or path.suffix != '.py': raise ValueError('unsafe_edit: invalid node library marker')",
                "                module_path = path.parent if path.name == '__init__.py' else path.with_suffix('')",
                "                parts = ['components', *module_path.parts]",
                "                module_name = '.'.join(parts)",
                "                module = importlib.import_module(module_name)",
                "                function = getattr(module, name, None)",
                "                if function is not None: return function",
                "        return ctx.globals.get(name)",
                "    target_function = _resolve_node(target_instance.node_name)",
                "    if target_function is None: raise ValueError('unsafe_edit: target node definition is unavailable')",
                "    target_spec = inspect_node(target_function)",
                "    if input_ref not in {port.name for port in target_spec.inputs}: raise ValueError('invalid_connection: target node has no input named ' + input_ref)",
                "    source_instance = parse_instance_source(source.code)",
                "    if source_instance is not None:",
                "        if source_instance.result_var not in getattr(source_impl, 'defs', set()): raise ValueError('unsafe_edit: selected source output is stale')",
                "        source_function = _resolve_node(source_instance.node_name)",
                "        if source_function is None: raise ValueError('unsafe_edit: source node definition is unavailable')",
                "        source_spec = inspect_node(source_function)",
                "        output_index = payload.get('output_index')",
                "        if output_index is None:",
                "            if len(source_spec.outputs) != 1: raise ValueError('output_index_required: choose an output port')",
                "            output_index = 0",
                "        expression = output_expression(source_instance, output_index, len(source_spec.outputs))",
                "    else:",
                "        if variable not in getattr(source_impl, 'defs', set()): raise ValueError('unsafe_edit: selected source variable is stale')",
                "        expression = variable",
                "    if any(binding.input_name == input_ref for binding in target_instance.bindings):",
                "        updated = reconnect_instance_source(target.code, input_name=input_ref, expression=expression)",
                "    else:",
                "        bindings = {binding.input_name: binding.expression for binding in target_instance.bindings}",
                "        bindings[input_ref] = expression",
                "        updated, _ = build_instance_source(target_instance.node_name, bindings, instance_id=target_instance.instance_id)",
                "    compile(updated, '<graphimo-connect>', 'exec')",
                "    ctx.edit_cell(target.id, code=updated)",
                "    result = {'ok': True, 'changed': True, 'source': updated, 'cell_id': str(target.id)}",
            ]
        )
    else:  # inspect
        common.extend(
            [
                "    graph = ctx.graph.cells",
                "    namespace = ctx.globals",
                "    from graphimo.preview import preview_value",
                "    owners = {}",
                "    for raw_id, impl in graph.items():",
                "        for name in getattr(impl, 'defs', set()): owners.setdefault(name, []).append(str(raw_id))",
                "    cells = []; edges = []",
                "    for cell in ctx.cells:",
                "        impl = graph.get(cell.id); defs = sorted(getattr(impl, 'defs', set())) if impl else []; refs = sorted(getattr(impl, 'refs', set())) if impl else []",
                "        outputs = []",
                "        for name in defs:",
                "            if name in namespace:",
                "                try: outputs.append({'name': name, **preview_value(namespace[name], max_chars=120, max_items=4)})",
                "                except Exception: outputs.append({'name': name, 'runtime_type': 'object', 'preview': '<preview unavailable>'})",
                "        output = None",
                "        if cell.output is not None:",
                "            rendered = cell.output; mime = getattr(rendered, 'mimetype', None) or getattr(rendered, 'mime_type', None) or 'text/plain'; raw = getattr(rendered, 'data', rendered)",
                "            if isinstance(raw, str): preview = raw; size = len(raw.encode('utf-8', errors='replace'))",
                "            elif isinstance(raw, bytes): preview = f'Binary output ({len(raw)} bytes)'; size = len(raw)",
                "            elif isinstance(raw, (list, tuple)): preview = repr(raw[:6]) + (f' … ({len(raw)} items)' if len(raw) > 6 else ''); size = len(preview.encode('utf-8'))",
                "            elif raw is None or type(raw) in (bool, int, float): preview = repr(raw); size = len(preview.encode('utf-8'))",
                "            else: preview = f'<{type(raw).__name__} output>'; size = 0",
                "            if str(mime).startswith('image/'): preview = f'Image output ({mime}, {size} bytes)'",
                "            output = {'mimetype': str(mime), 'preview': preview[:500], 'truncated': len(preview) > 500, 'bytes': size}",
                "        errors = [{'kind': getattr(error, 'kind', 'error'), 'message': str(getattr(error, 'msg', error))[:2000]} for error in getattr(cell, 'errors', [])]",
                "        cells.append({'id': str(cell.id), 'source': cell.code[:65536], 'source_truncated': len(cell.code) > 65536, 'defs': defs, 'refs': refs, 'outputs': outputs, 'output': output, 'errors': errors, 'status': str(getattr(getattr(cell, 'status', None), 'value', getattr(cell, 'status', None)) or 'unknown')})",
                "        for name in refs:",
                "            for owner in owners.get(name, []):",
                "                if owner != str(cell.id): edges.append({'source_cell_id': owner, 'target_cell_id': str(cell.id), 'variable': name})",
                "    cells_by_id = {item['id']: item for item in cells}",
                "    for item in cells: item['parents'] = []",
                "    for edge in edges:",
                "        target = cells_by_id.get(edge['target_cell_id']); source_id = edge['source_cell_id']",
                "        if target is None: continue",
                "        parent = next((item for item in target['parents'] if item['cell_id'] == source_id), None)",
                "        if parent is None: parent = {'cell_id': source_id, 'variables': []}; target['parents'].append(parent)",
                "        if edge['variable'] not in parent['variables']: parent['variables'].append(edge['variable'])",
                "    from graphimo import inspect_node",
                "    from graphimo.instance import parse_disabled_instance_source, parse_instance_source",
                "    for item in cells:",
                "        instance = parse_instance_source(item['source']) or parse_disabled_instance_source(item['source'])",
                "        if instance is None: continue",
                "        function = namespace.get(instance.node_name)",
                "        output_ports = []",
                "        if function is not None:",
                "            try:",
                "                node_spec = inspect_node(function)",
                "                output_ports = [{'index': port.index, 'declared_type': port.declared_type, 'expressions': list(port.expressions)} for port in node_spec.outputs]",
                "            except (TypeError, ValueError): pass",
                "        item['graphimo_instance'] = {'instance_id': instance.instance_id, 'node': instance.node_name, 'result_var': instance.result_var, 'bindings': [{'name': binding.input_name, 'expression': binding.expression} for binding in instance.bindings], 'outputs': output_ports}",
                "    from graphimo.dependencies import annotate_blocked_dependencies",
                "    blocked = annotate_blocked_dependencies(cells, edges)",
                "    result = {'ok': True, 'cells': cells[:512], 'edges': edges[:4096], 'blocked_by': blocked, 'truncated': len(cells) > 512 or len(edges) > 4096}",
            ]
        )
    common.append(
        "print('GRAPHIMO_KERNEL_RESULT:' + json.dumps(result if result.get('ok') is False else {'ok': True, 'result': result}, ensure_ascii=False, allow_nan=False, separators=(',', ':'))) "
    )
    return "\n".join(common)


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
