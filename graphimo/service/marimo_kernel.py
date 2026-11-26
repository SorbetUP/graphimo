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
                "    from graphimo.components import ComponentLibrary",
                "    from graphimo.instance import build_instance_source, parse_instance_source",
                "    hashes = payload.get('hashes') or {}",
                "    if not isinstance(hashes, dict) or len(hashes) > 4096 or any(not isinstance(path, str) or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) for path, digest in hashes.items()): raise ValueError('invalid component source hashes')",
                "    libraries = payload.get('libraries') or {}",
                "    if not isinstance(libraries, dict) or len(libraries) > 512 or any(not isinstance(marker, str) or not marker.startswith('# Graphimo node library: ') or not isinstance(source, str) or not source.startswith(marker + chr(10)) or len(source.encode('utf-8')) > 1048576 for marker, source in libraries.items()): raise ValueError('invalid library updates')",
                "    library = ComponentLibrary(payload['_graphimo_project_path'])",
                "    changed = []; conflicts = []; migrated = []",
                "    for cell in list(ctx.cells):",
                "        marker_line = next((line for line in cell.code.splitlines() if line.startswith('# Graphimo node library: ')), None)",
                "        if marker_line is None or marker_line not in libraries: continue",
                "        relative, sep, exported = marker_line[len('# Graphimo node library: '):].rpartition(':')",
                "        if not sep: continue",
                "        digest_line = next((line for line in cell.code.splitlines() if line.startswith('# Graphimo node source sha256: ')), None)",
                "        old_digest = digest_line.removeprefix('# Graphimo node source sha256: ') if digest_line else None",
                "        expected = library.canonical_library_source(relative, exported, old_digest) if old_digest else None",
                "        if expected is not None and cell.code != expected:",
                "            conflicts.append({'cell_id': str(cell.id), 'marker': marker_line, 'reason': 'library cell was modified outside Graphimo'})",
                "            continue",
                "        if expected is None and ('_graphimo_module.' + exported) not in cell.code:",
                "            conflicts.append({'cell_id': str(cell.id), 'marker': marker_line, 'reason': 'unrecognized legacy library cell'})",
                "            continue",
                "        source = libraries[marker_line]",
                "        if cell.code != source:",
                "            compile(source, '<graphimo-library-refresh>', 'exec')",
                "            ctx.edit_cell(cell.id, code=source)",
                "            if str(cell.id) not in changed: changed.append(str(cell.id))",
                "    for cell in list(ctx.cells):",
                "        component = re.search(r'(?m)^# Graphimo component: (.+)$', cell.code)",
                "        old = re.search(r'(?m)^# Graphimo source sha256: ([0-9a-f]+)$', cell.code)",
                "        if component and old and hashes.get(component.group(1)) and hashes[component.group(1)] != old.group(1):",
                "            updated = re.sub(r'(?m)^# Graphimo source sha256: [0-9a-f]+$', '# Graphimo source sha256: ' + hashes[component.group(1)], cell.code, count=1)",
                "            ctx.edit_cell(cell.id, code=updated)",
                "            if str(cell.id) not in changed: changed.append(str(cell.id))",
                "    for cell in list(ctx.cells):",
                "        instance = parse_instance_source(cell.code)",
                "        if instance is None or instance.component_path is not None: continue",
                "        candidates = [marker for marker in libraries if marker.endswith(':' + instance.node_name)]",
                "        if len(candidates) != 1: continue",
                "        marker = candidates[0]",
                "        relative = marker[len('# Graphimo node library: '):].rpartition(':')[0]",
                "        if relative not in hashes: continue",
                "        updated, _ = build_instance_source(instance.node_name, {binding.input_name: binding.expression for binding in instance.bindings}, instance_id=instance.instance_id, component_path=relative)",
                "        compile(updated, '<graphimo-instance-migration>', 'exec')",
                "        if updated != cell.code:",
                "            ctx.edit_cell(cell.id, code=updated)",
                "            migrated.append(str(cell.id))",
                "    result = {'ok': True, 'changed_cells': changed, 'conflicts': conflicts, 'migrated_instances': migrated}",
            ]
        )
    elif action == "run":
        common.extend(
            [
                "    import inspect, pathlib",
                "    from marimo._runtime.commands import ExecuteCellCommand",
                "    from graphimo.dependencies import blocked_dependencies_for_graph",
                "    from graphimo.instance import load_node_function, parse_instance_source",
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
                "            from graphimo.instance import ComponentUnavailableError",
                "            unavailable = None",
                "            try: function = load_node_function(instance, project_root=project_root, namespace=ctx.globals)",
                "            except ComponentUnavailableError as error: function = None; unavailable = str(error)",
                "            except Exception: function = None",
                "            if unavailable is not None: result = {'ok': False, 'error': {'code': 'component_unavailable', 'message': unavailable[:2000]}}",
                "            elif function is not None:",
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
                "    import json, pathlib",
                "    from graphimo import inspect_node, parse_instance_source, set_instance_binding",
                "    from graphimo.instance import ComponentUnavailableError, load_node_function",
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
                "        try: function = load_node_function(instance, project_root=project_root, namespace=ctx.globals)",
                "        except ComponentUnavailableError as error: raise ValueError('component_unavailable: ' + str(error))",
                "        except Exception: function = None",
                "        if function is None: raise ValueError('node definition is unavailable')",
                "        node_spec = inspect_node(function)",
                "        if input_name not in {port.name for port in node_spec.inputs}: raise ValueError('node has no input named ' + input_name)",
                "        updated = set_instance_binding(selected.code, input_name=input_name, expression=expression)",
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
                "    import pathlib",
                "    from graphimo import inspect_node, output_expression, parse_instance_source, set_instance_binding",
                "    from graphimo.instance import ComponentUnavailableError, load_node_function",
                "    source_id = payload.get('from_cell_id'); target_id = payload.get('to_cell_id')",
                "    variable = payload.get('variable'); input_ref = payload.get('input_ref')",
                "    output_index = payload.get('output_index')",
                "    if any(not isinstance(item, str) or not item or len(item) > 256 for item in (source_id, target_id)): raise ValueError('cell IDs must contain 1-256 characters')",
                "    if any(not isinstance(item, str) or not item.isidentifier() for item in (variable, input_ref)): raise ValueError('connection ports must be Python identifiers')",
                "    if output_index is not None and (type(output_index) is not int or output_index < 0): raise ValueError('output_index must be a non-negative integer')",
                "    project_root = pathlib.Path(payload['_graphimo_project_path']).resolve().parent",
                "    source = ctx.cells[source_id]; target = ctx.cells[target_id]",
                "    source_impl = ctx.graph.cells.get(source.id); target_impl = ctx.graph.cells.get(target.id)",
                "    target_instance = parse_instance_source(target.code)",
                "    if target_instance is None: raise ValueError('unsafe_edit: target is not a Graphimo-owned node instance')",
                "    if not source_impl or not target_impl: raise ValueError('unsafe_edit: selected source or target is stale')",
                "    try: target_function = load_node_function(target_instance, project_root=project_root, namespace=ctx.globals)",
                "    except ComponentUnavailableError as error: raise ValueError('component_unavailable: ' + str(error))",
                "    except Exception: target_function = None",
                "    if target_function is None: raise ValueError('unsafe_edit: target node definition is unavailable')",
                "    target_spec = inspect_node(target_function)",
                "    if input_ref not in {port.name for port in target_spec.inputs}: raise ValueError('invalid_connection: target node has no input named ' + input_ref)",
                "    source_instance = parse_instance_source(source.code)",
                "    if source_instance is not None:",
                "        if source_instance.result_var not in getattr(source_impl, 'defs', set()): raise ValueError('unsafe_edit: selected source output is stale')",
                "        try: source_function = load_node_function(source_instance, project_root=project_root, namespace=ctx.globals)",
                "        except ComponentUnavailableError as error: raise ValueError('component_unavailable: ' + str(error))",
                "        except Exception: source_function = None",
                "        if source_function is None: raise ValueError('unsafe_edit: source node definition is unavailable')",
                "        source_spec = inspect_node(source_function)",
                "        if output_index is None:",
                "            if len(source_spec.outputs) != 1: raise ValueError('output_index_required: choose an output port')",
                "            output_index = 0",
                "        expression = output_expression(source_instance, output_index, len(source_spec.outputs))",
                "    else:",
                "        if variable not in getattr(source_impl, 'defs', set()): raise ValueError('unsafe_edit: selected source variable is stale')",
                "        expression = variable",
                "    if source.id == target.id: raise ValueError('cycle_detected: cannot connect a cell to itself')",
                "    if str(target.id) in {str(ancestor) for ancestor in ctx.graph.ancestors(source.id)}: raise ValueError('cycle_detected: this connection would create a dependency cycle')",
                "    updated = set_instance_binding(target.code, input_name=input_ref, expression=expression)",
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
                "    import pathlib as _graphimo_pathlib",
                "    from graphimo import inspect_node",
                "    from graphimo.components import ComponentLibrary",
                "    from graphimo.dependencies import annotate_blocked_dependencies",
                "    from graphimo.instance import call_target, parse_disabled_instance_source, parse_instance_source",
                "    from graphimo.projection import contract_from_node_spec, contract_from_static, expand_binding_edges, project_instance",
                "    _project_root = _graphimo_pathlib.Path(payload['_graphimo_project_path']).resolve().parent",
                "    _library = ComponentLibrary(payload['_graphimo_project_path'])",
                "    _library_index = {}",
                "    for _cell in ctx.cells:",
                "        _marker = None; _digest = None; _symbol = None",
                "        for _line in _cell.code.splitlines():",
                "            if _line.startswith('# Graphimo node library: '): _marker = _line",
                "            elif _line.startswith('# Graphimo node source sha256: '): _digest = _line.removeprefix('# Graphimo node source sha256: ')",
                "            elif _line.startswith('# Graphimo node symbol: '): _symbol = _line.removeprefix('# Graphimo node symbol: ')",
                "        if _marker is not None:",
                "            _relative, _sep, _exported = _marker[len('# Graphimo node library: '):].rpartition(':')",
                "            if _sep: _library_index[(_relative, _exported)] = {'cell_id': str(_cell.id), 'source_sha256': _digest, 'symbol': _symbol}",
                "    _instances_by_cell = {}",
                "    _result_vars = set()",
                "    for item in cells:",
                "        _active = parse_instance_source(item['source'])",
                "        instance = _active or parse_disabled_instance_source(item['source'])",
                "        if instance is None: continue",
                "        _instances_by_cell[item['id']] = instance",
                "        _result_vars.add(instance.result_var)",
                "        if instance.component_path is not None and not (_project_root / 'components' / instance.component_path).is_file():",
                "            item['graphimo_component_unavailable'] = True",
                "    blocked = annotate_blocked_dependencies(cells, edges)",
                "    for item in cells:",
                "        instance = _instances_by_cell.get(item['id'])",
                "        if instance is None: continue",
                "        _active = parse_instance_source(item['source'])",
                "        symbol = call_target(instance.node_name, instance.component_path)",
                "        function = namespace.get(symbol)",
                "        spec = None",
                "        if function is not None:",
                "            try: spec = inspect_node(function)",
                "            except (TypeError, ValueError): spec = None",
                "        contract = contract_from_node_spec(spec)",
                "        component_available = item.get('graphimo_component_unavailable') is not True",
                "        if contract is None and instance.component_path is not None and component_available:",
                "            static = _library.static_node_contract(instance.component_path, instance.node_name)",
                "            contract = contract_from_static(static)",
                "        bound = {binding.input_name: binding.expression for binding in instance.bindings}",
                "        cached = instance.result_var in namespace",
                "        fresh = bool(cached and item.get('status') == 'idle' and not item.get('errors') and item.get('graphimo_dependency_state') is None)",
                "        def _preview_for(index, count, _instance=instance, _namespace=namespace):",
                "            value = _namespace[_instance.result_var]",
                "            if count > 1: value = value[index]",
                "            return preview_value(value, max_chars=80, max_items=3)",
                "        entry = _library_index.get((instance.component_path, instance.node_name)) if instance.component_path is not None else None",
                "        projection = project_instance(instance, contract=contract, bound=bound, result_vars=_result_vars, cached=cached, fresh=fresh, has_errors=bool(item.get('errors')), dependency_state=item.get('graphimo_dependency_state'), disabled=_active is None, component_entry=entry, component_available=component_available, preview_for=_preview_for if cached else None)",
                "        item['graphimo_instance'] = projection['instance']",
                "        if projection['component'] is not None: item['graphimo_component'] = projection['component']",
                "    edges = expand_binding_edges(edges, _instances_by_cell)",
                "    result = {'ok': True, 'cells': cells[:512], 'edges': edges[:4096], 'blocked_by': blocked, 'truncated': len(cells) > 512 or len(edges) > 4096, 'cells_truncated': len(cells) > 512, 'edges_truncated': len(edges) > 4096}",
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
