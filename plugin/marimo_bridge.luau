-- The only marimo transport/private-API boundary in Graphimo.
-- Inspection and cell edits run through marimo-pair's documented local
-- /api/kernel/execute endpoint. marimo remains the runtime and graph owner.
local Bridge = {}

local function quote_config(value)
	local result = string.gsub(tostring(value), "\\", "\\\\")
	return string.gsub(result, '"', '\\"')
end

local function result_from_sse(text)
	local event = nil
	local captured, errors, finished = {}, {}, nil
	for line in string.gmatch((text or "") .. "\n", "([^\n]*)\n") do
		line = string.gsub(line, "\r$", "")
		local name = string.match(line, "^event:%s*(.+)$")
		if name then event = name end
		local data = string.match(line, "^data:%s*(.+)$")
		if data then
			local ok, decoded = pcall(ui.json.decode, data)
			if ok and type(decoded) == "table" then
				if event == "stdout" and type(decoded.data) == "string" then
					table.insert(captured, decoded.data)
				elseif event == "stderr" and type(decoded.data) == "string" then
					table.insert(errors, decoded.data)
				elseif event == "done" then
					finished = { success = decoded.success ~= false, output = decoded.output }
				end
			end
		end
	end
	if not finished or finished.success == false then
		local detail = table.concat(errors, "\n")
		return nil, if detail ~= "" then detail else "Marimo did not complete the request"
	end
	local output = table.concat(captured)
	local marker = string.find(output, "GRAPHIMO_RESULT:", 1, true)
	if not marker then return nil, "Marimo returned no Graphimo result" end
	local encoded = string.sub(output, marker + #"GRAPHIMO_RESULT:")
	encoded = string.match(encoded, "^([^\r\n]*)") or encoded
	local ok, value = pcall(ui.json.decode, encoded)
	if not ok or type(value) ~= "table" then return nil, "Marimo returned an invalid Graphimo result" end
	return value, nil
end

local function curl(zeron, args, stdin, timeout)
	local request = zeron.run("curl", args, { stdin = stdin, timeout = timeout or 15 })
	if request == nil then error("Zeron did not start curl. Check Graphimo's commands permission in Settings → Plugins.") end
	return request
end

local function auth_config(token)
	if type(token) ~= "string" or token == "" then return "" end
	if string.find(token, "[\r\n]") then error("Marimo token cannot contain a newline") end
	return 'header = "Authorization: Bearer ' .. quote_config(token) .. '"\n'
end

local function api_url(base)
	return string.gsub(base, "/+$", "")
end

function Bridge.sessions(zeron, base, token)
	base = api_url(base)
	local args = { "-fsS", "--connect-timeout", "3", "-H", "Accept: application/json" }
	local config = auth_config(token)
	if config ~= "" then table.insert(args, "--config"); table.insert(args, "-") end
	table.insert(args, base .. "/api/sessions")
	return curl(zeron, args, config, 8):after(function(response)
		local ok, data = pcall(ui.json.decode, response.stdout or "")
		if not ok or type(data) ~= "table" then error("Could not read active marimo sessions") end
		local sessions = {}
		for session_id, session in data do
			if type(session_id) == "string" and type(session) == "table" then
				table.insert(sessions, { session_id = session_id, path = session.path or session.filename or "" })
			end
		end
		table.sort(sessions, function(a, b) return a.path < b.path end)
		return sessions
	end)
end

local function execute(zeron, base, token, session_id, code, timeout)
	local body = ui.json.encode({ code = code })
	local args = {
		"-fsSN", "-X", "POST", api_url(base) .. "/api/kernel/execute",
		"-H", "Content-Type: application/json",
		"-H", "Accept: text/event-stream",
		"-H", "Marimo-Session-Id: " .. session_id,
		"--config", "-", "-d", body,
	}
	return curl(zeron, args, auth_config(token), timeout or 60):after(function(response)
		local value, why = result_from_sse(response.stdout or "")
		if not value then error(why) end
		if value.ok == false then
			local detail = if type(value.error) == "table" then value.error.message else value.error
			error(detail or "Marimo operation failed")
		end
		return value
	end)
end

local function code_for(operation, args)
	local session_id = ui.json.encode(args.session_id)
	if operation == "inspect" then
		return table.concat({
			"import json",
			"import marimo._code_mode as cm",
			"async with cm.get_context() as ctx:",
			"    graph_cells = ctx.graph.cells",
			"    owners = {}",
			"    for cid, impl in graph_cells.items():",
			"        for name in getattr(impl, 'defs', set()): owners.setdefault(name, []).append(str(cid))",
			"    cells = []",
			"    edges = []",
			"    for cell in ctx.cells:",
			"        cid = str(cell.id)",
			"        impl = graph_cells.get(cell.id)",
			"        defs = sorted(getattr(impl, 'defs', set())) if impl else []",
			"        refs = sorted(getattr(impl, 'refs', set())) if impl else []",
			"        parents = {}",
			"        children = {}",
			"        for name in refs:",
			"            for parent in owners.get(name, []):",
			"                if parent != cid: parents.setdefault(parent, []).append(name)",
			"        for name in defs:",
			"            for child_id, child_impl in graph_cells.items():",
			"                if str(child_id) != cid and name in getattr(child_impl, 'refs', set()): children.setdefault(str(child_id), []).append(name)",
			"        for parent, names in parents.items():",
			"            for name in sorted(set(names)): edges.append({'source_cell_id': parent, 'target_cell_id': cid, 'variable': name})",
			"        status = cell.status",
			"        status = getattr(status, 'value', status)",
			"        output = None if cell.output is None else repr(cell.output)[:500]",
			"        errors = [{'kind': err.kind, 'message': err.msg} for err in cell.errors]",
			"        cells.append({'id': cid, 'name': cell.name or None, 'source': cell.code, 'defs': defs, 'refs': refs, 'parents': [{'cell_id': key, 'variables': sorted(set(value))} for key, value in parents.items()], 'children': [{'cell_id': key, 'variables': sorted(set(value))} for key, value in children.items()], 'status': str(status or 'unknown'), 'output': output, 'errors': errors})",
			"    result = {'notebook': {'path': " .. ui.json.encode(args.notebook_path or "") .. ", 'session_id': " .. session_id .. "}, 'cells': cells, 'edges': edges}",
			"print('GRAPHIMO_RESULT:' + json.dumps(result, default=str, separators=(',', ':'))) ",
		}, "\n")
	elseif operation == "list_components" then
		return table.concat({
			"import hashlib, importlib, inspect, json, pathlib, sys",
			"root = pathlib.Path(" .. ui.json.encode(args.components_root) .. ").resolve()",
			"items = []; paths = []",
			"for file in sorted(root.rglob('*.py')):",
			"    rel_parts = file.relative_to(root).parts",
			"    if any(part.startswith('.') or part == '__pycache__' for part in rel_parts) or file.name == '__init__.py': continue",
			"    component_id = file.relative_to(root).as_posix(); kind = 'file'; module_rel = file.relative_to(root).with_suffix('').parts",
			"    paths.append((component_id, kind, module_rel, file))",
			"for package_file in sorted(root.rglob('__init__.py')):",
			"    rel_parts = package_file.relative_to(root).parts",
			"    if any(part.startswith('.') or part == '__pycache__' for part in rel_parts): continue",
			"    rel = package_file.parent.relative_to(root).as_posix()",
			"    if rel != '.': paths.append((rel, 'package', package_file.parent.relative_to(root).parts, package_file))",
			"sys.path.insert(0, str(root.parent)); components = []",
			"for component_id, kind, parts, source_path in paths:",
			"    module_name = '.'.join((root.name, *parts)); importlib.invalidate_caches()",
			"    for name in list(sys.modules):",
			"        if name == module_name or name.startswith(module_name + '.'): sys.modules.pop(name, None)",
			"    try: module = importlib.import_module(module_name)",
			"    except Exception: continue",
			"    names = getattr(module, '__all__', None) or [name for name in vars(module) if not name.startswith('_')]",
			"    callables = []",
			"    for name in names:",
			"        value = getattr(module, name, None)",
			"        if not isinstance(name, str) or not callable(value) or inspect.isclass(value): continue",
			"        try:",
			"            sig = inspect.signature(value); signature = str(sig)",
			"            parameters = [{'name': p.name, 'required': p.default is inspect.Parameter.empty, 'positional': p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD} for p in sig.parameters.values() if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)]",
			"        except (TypeError, ValueError): signature = '(...)'; parameters = []",
			"        doc = inspect.getdoc(value) or inspect.getdoc(module) or ''",
			"        callables.append({'name': name, 'signature': signature, 'description': doc.splitlines()[0] if doc else '', 'parameters': parameters})",
			"    if callables:",
			"        item_path = pathlib.Path(component_id)",
			"        category = list(item_path.parts[:-1]) if kind == 'file' else list(item_path.parts[:-1])",
			"        digest = hashlib.sha256()",
			"        sources = sorted(source_path.parent.rglob('*.py')) if kind == 'package' else [source_path]",
			"        for source_file in sources:",
			"            digest.update(source_file.relative_to(source_path.parent if kind == 'package' else root).as_posix().encode('utf-8'))",
			"            digest.update(source_file.read_bytes())",
			"        source_hash = digest.hexdigest()",
			"        components.append({'id': component_id, 'path': component_id, 'category': category, 'kind': kind, 'source_sha256': source_hash, 'callables': callables})",
			"components.sort(key=lambda item: item['id'])",
			"print('GRAPHIMO_RESULT:' + json.dumps({'ok': True, 'components': components}, separators=(',', ':'))) ",
		}, "\n")
	elseif operation == "run" then
		return table.concat({
			"import json",
			"import marimo._code_mode as cm",
			"async with cm.get_context() as ctx:",
			"    cell_id = " .. ui.json.encode(args.cell_id),
			"    ctx.run_cell(cell_id)",
			"print('GRAPHIMO_RESULT:' + json.dumps({'ok': True, 'status': 'queued', 'request_id': cell_id}))",
		}, "\n")
	elseif operation == "add_cell" then
		return table.concat({
			"import json",
			"import marimo._code_mode as cm",
			"async with cm.get_context() as ctx:",
			"    cell_id = ctx.create_cell(" .. ui.json.encode(args.source) .. ", hide_code=False)",
			"print('GRAPHIMO_RESULT:' + json.dumps({'ok': True, 'cell_id': str(cell_id), 'source': " .. ui.json.encode(args.source) .. "}))",
		}, "\n")
	elseif operation == "delete" then
		return table.concat({
			"import json",
			"import marimo._code_mode as cm",
			"async with cm.get_context() as ctx:",
			"    ctx.delete_cell(" .. ui.json.encode(args.cell_id) .. ")",
			"print('GRAPHIMO_RESULT:' + json.dumps({'ok': True, 'deleted': True}))",
		}, "\n")
	elseif operation == "connect" then
		return table.concat({
			"import ast, json",
			"import marimo._code_mode as cm",
			"def graphimo_replace(ctx):",
			"    try:",
			"        target = ctx.cells[" .. ui.json.encode(args.to_cell_id) .. "]",
			"        source = ctx.cells[" .. ui.json.encode(args.from_cell_id) .. "]",
			"        target_impl = ctx.graph.cells.get(target.id); source_impl = ctx.graph.cells.get(source.id)",
			"        if not source_impl or not target_impl or " .. ui.json.encode(args.variable) .. " not in getattr(source_impl, 'defs', set()) or " .. ui.json.encode(args.input_ref) .. " not in getattr(target_impl, 'refs', set()) or " .. ui.json.encode(args.variable) .. " in getattr(target_impl, 'defs', set()): return {'ok': False, 'error': 'unsafe_edit: selected cells or ports are stale'}",
			"        tree = ast.parse(target.code)",
			"        if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.Assign, ast.AnnAssign)): return {'ok': False, 'error': 'unsafe_edit: target must be one assignment'}",
			"        assignment = tree.body[0]",
			"        if isinstance(assignment, ast.Assign) and (len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name)): return {'ok': False, 'error': 'unsafe_edit: target assignment is ambiguous'}",
			"        if isinstance(assignment, ast.AnnAssign) and not isinstance(assignment.target, ast.Name): return {'ok': False, 'error': 'unsafe_edit: target assignment is ambiguous'}",
			"        call = assignment.value",
			"        if not isinstance(call, ast.Call) or not isinstance(call.func, (ast.Name, ast.Attribute)) or not call.args or call.keywords or not all(isinstance(arg, ast.Name) for arg in call.args): return {'ok': False, 'error': 'unsafe_edit: target call is ambiguous'}",
			"        matches = [arg for arg in call.args if arg.id == " .. ui.json.encode(args.input_ref) .. "]",
			"        if len(matches) != 1: return {'ok': False, 'error': 'unsafe_edit: selected reference is not a unique positional argument'}",
			"        arg = matches[0]; lines = target.code.splitlines(keepends=True); raw = lines[arg.lineno - 1].encode('utf-8')",
			"        updated = ''.join(lines[:arg.lineno - 1]) + raw[:arg.col_offset].decode('utf-8') + " .. ui.json.encode(args.variable) .. " + raw[arg.end_col_offset:].decode('utf-8') + ''.join(lines[arg.lineno:])",
			"        compile(updated, '<graphimo-connect>', 'exec')",
			"        return {'ok': True, 'changed': True, 'source': updated, 'cell_id': target.id}",
			"    except Exception as exc: return {'ok': False, 'error': 'unsafe_edit: ' + str(exc)}",
			"async with cm.get_context() as ctx:",
			"    result = graphimo_replace(ctx)",
			"    if result.get('ok'): ctx.edit_cell(result['cell_id'], code=result['source'])",
			"print('GRAPHIMO_RESULT:' + json.dumps(result, default=str, separators=(',', ':'))) ",
		}, "\n")
	end
	error("Unsupported marimo operation: " .. tostring(operation))
end

function Bridge.inspect(zeron, base, token, session_id)
	return Bridge.sessions(zeron, base, token):after(function(sessions)
		local chosen = nil
		for _, session in sessions do
			if session.session_id == session_id then chosen = session; break end
		end
		if not chosen and (session_id == nil or session_id == "") and #sessions == 1 then chosen = sessions[1] end
		if not chosen then
			if #sessions == 0 then error("No active marimo notebook session was found") end
			error("Choose one of the " .. tostring(#sessions) .. " active marimo sessions")
		end
		local args = { session_id = chosen.session_id, notebook_path = chosen.path }
		local code = code_for("inspect", args)
		return execute(zeron, base, token, chosen.session_id, code, 45):after(function(snapshot)
			snapshot.notebook.path = chosen.path
			return snapshot
		end)
	end)
end

function Bridge.list_components(zeron, base, token, session_id, notebook_path)
	local root = string.gsub(notebook_path, "[^/]+$", "") .. "components"
	return execute(zeron, base, token, session_id, code_for("list_components", { components_root = root }), 60)
end

local function valid_identifier(value)
	return type(value) == "string" and string.match(value, "^[%a_][%w_]*$") ~= nil
end

function Bridge.component_source(component_id, callable, output_name, bindings, source_hash, parameter_order)
	if type(component_id) ~= "string" or component_id == "" or string.find(component_id, "..", 1, true) or string.sub(component_id, 1, 1) == "/" or string.find(component_id, "[\r\n]") then
		return nil, "Choose a component inside the components folder"
	end
	if not valid_identifier(callable) or not valid_identifier(output_name) then return nil, "Callable and output must be Python identifiers" end
	bindings = bindings or {}
	local bound = {}
	local all_variables = true
	for name, binding in bindings do
		if not valid_identifier(name) or type(binding) ~= "table" then return nil, "Invalid component argument" end
		if type(binding.variable) == "string" then
			if not valid_identifier(binding.variable) then return nil, "Component inputs must be Python variable names" end
			bound[name] = { variable = binding.variable }
		elseif binding.literal ~= nil then
			bound[name] = { literal = binding.literal }
			all_variables = false
		else
			return nil, "Each component argument needs a variable or literal value"
		end
	end
	local order, ordered = {}, {}
	for _, name in parameter_order or {} do
		if bound[name] then table.insert(order, name); ordered[name] = true end
	end
	local remaining = {}
	for name in bound do if not ordered[name] then table.insert(remaining, name) end end
	table.sort(remaining)
	for _, name in remaining do table.insert(order, name) end
	local positional = parameter_order ~= nil and all_variables and #remaining == 0 and #order <= #parameter_order
	if positional then
		for index, name in order do
			if parameter_order[index] ~= name then positional = false; break end
		end
	end
	local args = {}
	for _, name in order do
		local binding = bound[name]
		if positional then
			table.insert(args, binding.variable)
		elseif binding.variable then
			table.insert(args, name .. "=" .. binding.variable)
		else
			table.insert(args, name .. "=__import__('json').loads(" .. ui.json.encode(ui.json.encode(binding.literal)) .. ")")
		end
	end
	local module_rel = string.gsub(component_id, "%.py$", "")
	module_rel = string.gsub(module_rel, "/", ".")
	local module_name = "components." .. module_rel
	local called = "__import__('importlib').reload(__import__('importlib').import_module(" .. ui.json.encode(module_name) .. "))." .. callable
	local source = "# Graphimo component: " .. component_id .. "\n"
	source ..= "# Graphimo source sha256: " .. tostring(source_hash or "unknown") .. "\n"
	source ..= output_name .. " = " .. called .. "(" .. table.concat(args, ", ") .. ")\n"
	return source, nil
end

function Bridge.refresh_component_hashes(zeron, base, token, session_id, components)
	local hashes = {}
	for _, component in components do hashes[component.id] = component.source_sha256 end
	local code = table.concat({
		"import json, re",
		"import marimo._code_mode as cm",
		"hashes = " .. ui.json.encode(hashes),
		"changed = []",
		"async with cm.get_context() as ctx:",
		"    for cell in ctx.cells:",
		"        component = re.search(r'(?m)^# Graphimo component: (.+)$', cell.code)",
		"        old = re.search(r'(?m)^# Graphimo source sha256: ([0-9a-f]+)$', cell.code)",
		"        if component and old and hashes.get(component.group(1)) and hashes[component.group(1)] != old.group(1):",
		"            updated = re.sub(r'(?m)^# Graphimo source sha256: [0-9a-f]+$', '# Graphimo source sha256: ' + hashes[component.group(1)], cell.code, count=1)",
		"            ctx.edit_cell(cell.id, code=updated); changed.append(str(cell.id))",
		"print('GRAPHIMO_RESULT:' + json.dumps({'ok': True, 'changed_cells': changed}))",
	}, "\n")
	return execute(zeron, base, token, session_id, code, 45)
end

function Bridge.add_component(zeron, base, token, session_id, component_id, callable, output_name, bindings)
	local safe = type(component_id) == "string" and not string.find(component_id, "..", 1, true) and string.sub(component_id, 1, 1) ~= "/"
	if not safe then error("Choose a component inside the components folder") end
	return Bridge.sessions(zeron, base, token):after(function(sessions)
		local current = nil
		for _, session in sessions do if session.session_id == session_id then current = session; break end end
		if not current then error("The selected marimo session is no longer active") end
		return Bridge.list_components(zeron, base, token, session_id, current.path):after(function(result)
			local selected_component = nil
			for _, item in result.components or {} do if item.id == component_id then selected_component = item; break end end
			if not selected_component then error("The component changed or disappeared; refresh the library") end
			local parameter_order, callable_found = {}, false
			for _, item in selected_component.callables do
				if item.name == callable then
					callable_found = true
					for _, parameter in item.parameters or {} do if parameter.positional then table.insert(parameter_order, parameter.name) end end
				end
			end
			if not callable_found then error("The callable changed or disappeared; refresh the library") end
			local source, why = Bridge.component_source(component_id, callable, output_name, bindings, selected_component.source_sha256, parameter_order)
			if not source then error(why) end
			return Bridge.add_cell(zeron, base, token, session_id, source)
		end)
	end)
end

function Bridge.run(zeron, base, token, session_id, cell_id)
	return execute(zeron, base, token, session_id, code_for("run", { cell_id = cell_id }), 300)
end

function Bridge.add_cell(zeron, base, token, session_id, source)
	return execute(zeron, base, token, session_id, code_for("add_cell", { source = source }), 60)
end

function Bridge.delete(zeron, base, token, session_id, cell_id)
	return execute(zeron, base, token, session_id, code_for("delete", { cell_id = cell_id }), 60)
end

function Bridge.connect(zeron, base, token, session_id, from_cell_id, variable, to_cell_id, input_ref)
	return execute(zeron, base, token, session_id, code_for("connect", {
		from_cell_id = from_cell_id,
		variable = variable,
		to_cell_id = to_cell_id,
		input_ref = input_ref,
	}), 60)
end

return Bridge
