-- The only marimo transport/private-API boundary in Graphimo.
-- Inspection and cell edits run through marimo-pair's documented local
-- /api/kernel/execute endpoint. marimo remains the runtime and graph owner.
local Bridge = {}

local function failure(message, code)
	return { ok = false, error = { code = code or "marimo_error", message = tostring(message) } }
end

local function is_failure(value)
	return type(value) == "table" and value.ok == false
end

-- Zeron's native Promise:after registers callbacks but returns nil. Keep the
-- tiny chain adapter here, at the Zeron/marimo boundary, so bridge operations
-- can sequence HTTP requests without relying on host-specific return values.
local Future = {}
Future.__index = Future

local function future()
	return setmetatable({ state = "pending", handlers = {} }, Future)
end

local function settle(target, state, value)
	if target.state ~= "pending" then return end
	target.state, target.value = state, value
	local handlers = target.handlers
	target.handlers = {}
	for _, handler in handlers do
		local callback = if state == "resolved" then handler.success else handler.failure
		if not callback then
			settle(handler.next, state, value)
		else
			local ok, result = pcall(callback, value)
			if not ok then
				settle(handler.next, "rejected", result)
			elseif getmetatable(result) == Future then
				result:after(function(next_value) settle(handler.next, "resolved", next_value) end, function(err) settle(handler.next, "rejected", err) end)
			else
				settle(handler.next, "resolved", result)
			end
		end
	end
end

function Future:after(success, failure_callback)
	local next_future = future()
	local handler = { success = success, failure = failure_callback, next = next_future }
	if self.state == "pending" then
		table.insert(self.handlers, handler)
	else
		local callback = if self.state == "resolved" then success else failure_callback
		if not callback then
			settle(next_future, self.state, self.value)
		else
			local ok, result = pcall(callback, self.value)
			if not ok then
				settle(next_future, "rejected", result)
			elseif getmetatable(result) == Future then
				result:after(function(value) settle(next_future, "resolved", value) end, function(err) settle(next_future, "rejected", err) end)
			else
				settle(next_future, "resolved", result)
			end
		end
	end
	return next_future
end

local function from_zeron(native)
	local wrapped = future()
	if native == nil then
		settle(wrapped, "rejected", "Zeron did not start the command; check Graphimo's commands permission")
		return wrapped
	end
	local ok, after = pcall(function() return native.after end)
	if not ok or type(after) ~= "function" then
		settle(wrapped, "rejected", "Zeron returned a request without an after callback")
		return wrapped
	end
	local registered, why = pcall(function()
		after(native, function(value) settle(wrapped, "resolved", value) end, function(err) settle(wrapped, "rejected", err) end)
	end)
	if not registered then settle(wrapped, "rejected", why) end
	return wrapped
end

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
	return from_zeron(request)
end

local function auth_config(token)
	if type(token) ~= "string" or token == "" then return "" end
	if string.find(token, "[\r\n]") then error("Marimo token cannot contain a newline") end
	return 'header = "Authorization: Bearer ' .. quote_config(token) .. '"\n'
end

local function api_url(base)
	local path = string.match(base or "", "^[^?#]*") or ""
	return string.gsub(path, "/+$", "")
end

function Bridge.sessions(zeron, base, token)
	base = api_url(base)
	local args = { "-fsS", "--connect-timeout", "3", "-H", "Accept: application/json" }
	local config = auth_config(token)
	if config ~= "" then table.insert(args, "--config"); table.insert(args, "-") end
	table.insert(args, "-w")
	table.insert(args, "\nGRAPHIMO_HTTP_META:%{http_code}:%{content_type}")
	table.insert(args, base .. "/api/sessions")
	return curl(zeron, args, config, 8):after(function(response)
		local output = if type(response.stdout) == "string" then response.stdout else ""
		local marker = string.find(output, "\nGRAPHIMO_HTTP_META:", 1, true)
		local body, http_status, content_type = output, "unknown", "unknown"
		if marker then
			body = string.sub(output, 1, marker - 1)
			local meta = string.sub(output, marker + #"\nGRAPHIMO_HTTP_META:")
			http_status, content_type = string.match(meta, "^(%d+):([^\r\n]*)")
			http_status, content_type = http_status or "unknown", content_type or "unknown"
		end
		body = string.gsub(body, "[\r\n]+$", "")
		local ok, data = pcall(ui.json.decode, body)
		if not ok or type(data) ~= "table" then
			local body_kind = if string.match(body, "^%s*$") then "empty" elseif string.match(body, "^%s*<") then "HTML or markup" elseif string.match(body, "^%s*null%s*$") then "JSON null" else "non-object JSON or malformed text"
			return failure("Could not read marimo /api/sessions: " .. body_kind .. "; HTTP " .. http_status .. "; Content-Type " .. content_type .. "; body " .. tostring(#body) .. " bytes", "sessions_unavailable")
		end
		local source = if type(data.sessions) == "table" then data.sessions else data
		local sessions = {}
		for key, session in source do
			if type(session) == "table" then
				local session_id = if type(key) == "string" then key else (session.session_id or session.id)
				if type(session_id) == "string" and session_id ~= "" then
					table.insert(sessions, { session_id = session_id, path = session.path or session.filename or "" })
				end
			end
		end
		table.sort(sessions, function(a, b) return a.path < b.path end)
		return sessions
	end)
end

-- Find a running no-token marimo editor for this notebook, or ask marimo to
-- start one on its own default free port. The detached process is only the
-- bridge needed to own a live marimo session; all notebook work stays there.
function Bridge.open_notebook(zeron, notebook_path)
	if type(notebook_path) ~= "string" or notebook_path == "" then
		return future():after(function() return failure("Choose a notebook first", "selection_required") end)
	end
	local script = table.concat({
		"import glob, json, os, pathlib, re, shutil, subprocess, sys, time, urllib.request, venv",
		"notebook = pathlib.Path(sys.argv[1]).expanduser().resolve()",
		"if notebook.suffix.lower() != '.py' or not notebook.is_file(): raise SystemExit('Choose an existing Python notebook (.py)')",
		"registry = pathlib.Path.home() / '.local/state/marimo/servers'",
		"def registered():",
		"    for name in sorted(glob.glob(str(registry / '*.json'))):",
		"        try:",
		"            info = json.loads(pathlib.Path(name).read_text())",
		"            pid = int(info.get('pid') or 0)",
		"            if pid: os.kill(pid, 0)",
		"            url = info.get('base_url') or ('http://%s:%s' % (info.get('host', '127.0.0.1'), info['port']))",
		"            with urllib.request.urlopen(url.rstrip('/') + '/api/sessions', timeout=1) as response: sessions = json.load(response)",
		"            for sid, item in sessions.items():",
		"                path = item.get('path') or item.get('filename') or ''",
		"                if path and pathlib.Path(path).expanduser().resolve() == notebook: return url.rstrip('/'), sid, info.get('pid')",
		"        except Exception: pass",
		"    return None",
		"def server_for(pid):",
		"    for name in sorted(glob.glob(str(registry / '*.json'))):",
		"        try:",
		"            info = json.loads(pathlib.Path(name).read_text())",
		"            if int(info.get('pid') or 0) != pid: continue",
		"            url = info.get('base_url') or ('http://%s:%s' % (info.get('host', '127.0.0.1'), info['port']))",
		"            with urllib.request.urlopen(url.rstrip('/') + '/', timeout=1): pass",
		"            return url.rstrip('/')",
		"        except Exception: pass",
		"    return None",
		"found = registered()",
		"state = pathlib.Path.home() / '.local/state/graphimo'; state.mkdir(parents=True, exist_ok=True)",
		"import hashlib",
		"log_path = state / ('marimo-' + hashlib.sha256(str(notebook).encode()).hexdigest()[:16] + '.log')",
		"if found:",
		"    url, session_id, pid = found",
		"    print('GRAPHIMO_LAUNCH:' + json.dumps({'url': url, 'session_id': session_id, 'pid': pid, 'log_path': str(log_path), 'started': False}))",
		"    raise SystemExit(0)",
		"PINNED_MARIMO = '0.25.2'",
		"def version_for(python):",
		"    try:",
		"        result = subprocess.run([str(python), '-c', 'import importlib.metadata as m; print(m.version(\\\"marimo\\\"))'], capture_output=True, check=False, text=True, timeout=15)",
		"        return result.stdout.strip() if result.returncode == 0 else None",
		"    except Exception: return None",
		"binary_dir = 'Scripts' if os.name == 'nt' else 'bin'",
		"python_name = 'python.exe' if os.name == 'nt' else 'python'",
		"state = pathlib.Path.home() / '.local/share/graphimo/marimo-0.25.2'",
		"candidates = [notebook.parent / '.venv' / binary_dir / python_name, notebook.parent / 'venv' / binary_dir / python_name, pathlib.Path(sys.executable), state / binary_dir / python_name]",
		"runtime = next((str(candidate) for candidate in candidates if candidate.is_file() and version_for(candidate) == PINNED_MARIMO), None)",
		"cli = shutil.which('marimo')",
		"if not runtime and cli:",
		"    try:",
		"        output = subprocess.run([cli, '--version'], capture_output=True, check=False, text=True, timeout=10).stdout",
		"        version = re.search(r'\\b(\\d+\\.\\d+\\.\\d+)\\b', output)",
		"        if version and version.group(1) == PINNED_MARIMO: cli = cli",
		"        else: cli = None",
		"    except Exception: cli = None",
		"with log_path.open('ab') as log:",
		"    try:",
		"        if not runtime and not cli:",
		"            log.write(('No Python environment with marimo==' + PINNED_MARIMO + ' was found; preparing a private environment.\\n').encode())",
		"            state.parent.mkdir(parents=True, exist_ok=True)",
		"            venv.EnvBuilder(with_pip=True, clear=state.exists()).create(str(state))",
		"            managed_python = state / binary_dir / python_name",
		"            installed = subprocess.run([str(managed_python), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', 'marimo==' + PINNED_MARIMO], stdout=log, stderr=subprocess.STDOUT, check=False, timeout=240)",
		"            if installed.returncode != 0 or version_for(managed_python) != PINNED_MARIMO: raise RuntimeError('Could not install the pinned marimo package')",
		"            runtime = str(managed_python)",
		"        command = [cli, 'edit'] if cli else [runtime, '-m', 'marimo', 'edit']",
		"        process = subprocess.Popen(command + [str(notebook), '--headless', '--no-token', '--host', '127.0.0.1', '--skip-update-check'], cwd=str(notebook.parent), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)",
		"    except Exception as error:",
		"        log.flush()",
		"        tail = log_path.read_text(errors='replace')[-6000:] if log_path.exists() else ''",
		"        raise SystemExit('Could not prepare marimo==' + PINNED_MARIMO + ': ' + str(error) + '. See log ' + str(log_path) + '\\n' + tail)",
		"deadline = time.monotonic() + 25",
		"while time.monotonic() < deadline:",
		"    if process.poll() is not None: break",
		"    url = server_for(process.pid)",
		"    if url:",
		"        print('GRAPHIMO_LAUNCH:' + json.dumps({'url': url, 'session_id': '', 'pid': process.pid, 'log_path': str(log_path), 'started': True}))",
		"        raise SystemExit(0)",
		"    time.sleep(.25)",
		"tail = log_path.read_text(errors='replace')[-6000:] if log_path.exists() else ''",
		"raise SystemExit('marimo did not start (pid %s). Log: %s\\n%s' % (process.pid, log_path, tail))",
	}, "\n")
	local request = zeron.run("python3", { "-c", script, notebook_path }, { timeout = 300 })
	return from_zeron(request):after(function(response)
		local stdout = if type(response.stdout) == "string" then response.stdout else ""
		local marker = string.find(stdout, "GRAPHIMO_LAUNCH:", 1, true)
		if not marker then
			local detail = if type(response.stderr) == "string" and response.stderr ~= "" then response.stderr else stdout
			return failure("Could not start marimo: " .. string.sub(detail, 1, 8000), "marimo_start_failed")
		end
		local encoded = string.match(string.sub(stdout, marker + #"GRAPHIMO_LAUNCH:"), "^([^\r\n]+)")
		local ok, value = pcall(ui.json.decode, encoded or "")
		if not ok or type(value) ~= "table" or type(value.url) ~= "string" then
			return failure("Marimo launcher returned an invalid response", "marimo_start_failed")
		end
		return value
	end)
end

function Bridge.read_marimo_log(zeron, path)
	if type(path) ~= "string" or path == "" then
		return future():after(function() return { text = "No marimo log file is active" } end)
	end
	local script = "from pathlib import Path; import sys; p=Path(sys.argv[1]); print(p.read_text(errors='replace')[-12000:] if p.exists() else 'Marimo log file has not been created')"
	return from_zeron(zeron.run("python3", { "-c", script, path }, { timeout = 10 })):after(function(response)
		if response.status ~= 0 then return failure(response.stderr or "Could not read marimo log", "log_read_failed") end
		return { text = response.stdout or "" }
	end)
end

function Bridge.append_problem(zeron, entry)
	local script = table.concat({
		"from pathlib import Path",
		"import sys",
		"path = Path.home() / '.local/state/graphimo/problems.log'",
		"path.parent.mkdir(parents=True, exist_ok=True)",
		"with path.open('a', encoding='utf-8') as stream: stream.write(sys.argv[1] + '\\n')",
	}, "\n")
	return from_zeron(zeron.run("python3", { "-c", script, entry }, { timeout = 10 }))
end

function Bridge.read_problem_log(zeron)
	local script = "from pathlib import Path; p=Path.home()/'.local/state/graphimo/problems.log'; print(p.read_text(errors='replace')[-12000:] if p.exists() else 'No saved problems')"
	return from_zeron(zeron.run("python3", { "-c", script }, { timeout = 10 })):after(function(response)
		if response.status ~= 0 then return failure(response.stderr or "Could not read problem log", "log_read_failed") end
		return { text = response.stdout or "" }
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
		if not value then return failure(why, "execute_failed") end
		if value.ok == false then
			local detail = if type(value.error) == "table" then value.error.message else value.error
			return failure(detail or "Marimo operation failed", "marimo_operation_failed")
		end
		return value
	end)
end

local function code_for(operation, args)
	local session_id = ui.json.encode(args.session_id)
	if operation == "inspect" then
		return table.concat({
			"import ast, json, math, re",
			"import marimo._code_mode as cm",
			"def graphimo_reference(source):",
			"    if not isinstance(source, str): return None",
			"    lines = source.splitlines()",
			"    if len(lines) < 4: return None",
			"    component = re.fullmatch(r'# Graphimo component: ([^\\r\\n]+)', lines[0])",
			"    digest = re.fullmatch(r'# Graphimo source sha256: ([0-9a-f]{64})', lines[1])",
			"    callable_match = re.fullmatch(r'# Graphimo callable: ([A-Za-z_][A-Za-z0-9_]*)', lines[2])",
			"    if not component or not digest or not callable_match: return None",
			"    parts = component.group(1).split('/')",
			"    if any(part in ('', '.', '..') for part in parts) or '\\\\' in component.group(1): return None",
			"    module_parts = list(parts)",
			"    if module_parts[-1].endswith('.py'): module_parts[-1] = module_parts[-1][:-3]",
			"    if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', part) for part in module_parts): return None",
			"    module_name = 'components.' + '.'.join(module_parts); callable_name = callable_match.group(1)",
			"    try: tree = ast.parse(source)",
			"    except SyntaxError: return None",
			"    if len(tree.body) != 1: return None",
			"    statement = tree.body[0]",
			"    if isinstance(statement, ast.Assign):",
			"        if len(statement.targets) != 1 or not isinstance(statement.value, ast.Call): return None",
			"        target = statement.targets[0]; call = statement.value",
			"        if isinstance(target, ast.Name): outputs = [target.id]",
			"        elif isinstance(target, (ast.Tuple, ast.List)) and all(isinstance(item, ast.Name) for item in target.elts): outputs = [item.id for item in target.elts]",
			"        else: return None",
			"    elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):",
			"        call = statement.value; outputs = []",
			"    else: return None",
			"    expected = f\"_out = __import__('importlib').reload(__import__('importlib').import_module({module_name!r})).{callable_name}()\"",
			"    try: expected_func = ast.parse(expected).body[0].value.func",
			"    except (SyntaxError, AttributeError): return None",
			"    if ast.dump(call.func, include_attributes=False) != ast.dump(expected_func, include_attributes=False): return None",
			"    inputs = {}",
			"    for keyword_arg in call.keywords:",
			"        if keyword_arg.arg is None: continue",
			"        value = keyword_arg.value",
			"        if isinstance(value, ast.Name): inputs[keyword_arg.arg] = {'kind': 'variable', 'variable': value.id}",
			"        elif isinstance(value, ast.Constant) and (value.value is None or type(value.value) in (bool, int, float, str)): inputs[keyword_arg.arg] = {'kind': 'literal', 'value': value.value}",
			"        else: inputs[keyword_arg.arg] = {'kind': 'expression', 'expression': ast.unparse(value)}",
			"    return {'component_id': component.group(1), 'callable': callable_name, 'source_sha256': digest.group(1), 'inputs': inputs, 'outputs': outputs}",
			"def graphimo_group_reference(source):",
			"    if not isinstance(source, str): return None",
			"    lines = source.splitlines()",
			"    if len(lines) < 5: return None",
			"    group = re.fullmatch(r'# Graphimo group: ([^\\r\\n]+)', lines[0])",
			"    digest = re.fullmatch(r'# Graphimo group sha256: ([0-9a-f]{64})', lines[1])",
			"    inputs = re.fullmatch(r'# Graphimo group inputs: (.+)', lines[2])",
			"    outputs = re.fullmatch(r'# Graphimo group outputs: (.+)', lines[3])",
			"    if not group or not digest or not inputs or not outputs: return None",
			"    try: input_map, output_map = json.loads(inputs.group(1)), json.loads(outputs.group(1))",
			"    except Exception: return None",
			"    if not isinstance(input_map, dict) or not isinstance(output_map, dict): return None",
			"    return {'group_id': group.group(1), 'source_sha256': digest.group(1), 'inputs': input_map, 'outputs': output_map}",
			"async with cm.get_context() as ctx:",
			"    graph_cells = ctx.graph.cells",
			"    owners = {}",
			"    for cid, impl in graph_cells.items():",
			"        for name in getattr(impl, 'defs', set()): owners.setdefault(name, []).append(str(cid))",
			"    cells = []",
			"    edges = []",
			"    controls = None",
			"    try:",
			"        import importlib.metadata as metadata",
			"        if metadata.version('marimo') == '0.25.2':",
			"            from collections.abc import Mapping",
			"            from marimo._plugins.ui._core.ui_element import UIElement",
			"            from marimo._plugins.ui._impl.input import slider, dropdown",
			"            runtime_globals = ctx.globals",
			"            if isinstance(runtime_globals, Mapping): controls = []",
			"    except Exception:",
			"        controls = None",
			"    def control_scalar(value):",
			"        if value is None or type(value) in (bool, str): return True",
			"        if type(value) is int: return -9007199254740991 <= value <= 9007199254740991",
			"        return type(value) is float and math.isfinite(value)",
			"    for cell in ctx.cells:",
			"        cid = str(cell.id)",
			"        impl = graph_cells.get(cell.id)",
			"        defs = sorted(getattr(impl, 'defs', set())) if impl else []",
			"        refs = sorted(getattr(impl, 'refs', set())) if impl else []",
			"        values = {}",
			"        for name in defs:",
			"            if type(name) is not str or name not in ctx.globals: continue",
			"            try:",
			"                value = ctx.globals[name]; info = {'type': type(value).__name__, 'has_value': False}",
			"                if value is None or type(value) in (bool, int): info.update({'has_value': True, 'value': value})",
			"                elif type(value) is float and math.isfinite(value): info.update({'has_value': True, 'value': value})",
			"                elif type(value) is str and len(value) <= 4000: info.update({'has_value': True, 'value': value})",
			"                else:",
			"                    shape = getattr(value, 'shape', None)",
			"                    if shape is not None:",
			"                        try: info['shape'] = [int(part) for part in shape]",
			"                        except Exception: pass",
			"                    dtype = getattr(value, 'dtype', None)",
			"                    if dtype is not None: info['dtype'] = str(dtype)",
			"                    device = getattr(value, 'device', None)",
			"                    if device is not None: info['device'] = str(device)",
			"                values[name] = info",
			"            except Exception: continue",
			"        if controls is not None:",
			"            for name in defs:",
			"                try:",
			"                    if type(name) is not str or name not in runtime_globals: continue",
			"                    widget = runtime_globals[name]",
			"                    widget_type = type(widget)",
			"                    if widget_type is slider: kind = 'slider'",
			"                    elif widget_type is dropdown: kind = 'dropdown'",
			"                    else: continue",
			"                    if not isinstance(widget, UIElement): continue",
			"                    value = widget.value",
			"                    item = {'cell_id': cid, 'name': name, 'kind': kind, 'label': name}",
			"                    try:",
			"                        raw_label = widget._args.label",
			"                        if type(raw_label) is str and raw_label: item['label'] = raw_label",
			"                    except Exception: pass",
			"                    if control_scalar(value): item['value'] = value",
			"                    if kind == 'dropdown':",
			"                        try:",
			"                            options = widget.options",
			"                            if type(options) is dict and all(type(key) is str and control_scalar(option) for key, option in options.items()):",
			"                                item['choices'] = [{'label': key, 'value': option} for key, option in options.items()]",
			"                        except Exception: pass",
			"                    controls.append(item)",
			"                except Exception: continue",
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
			"        output = None",
			"        if cell.output is not None:",
			"            rendered = cell.output",
			"            mime = getattr(rendered, 'mimetype', None) or getattr(rendered, 'mime_type', None) or 'text/plain'",
			"            raw = getattr(rendered, 'data', rendered)",
			"            if isinstance(raw, str):",
			"                preview = raw",
			"                if mime == 'text/html':",
			"                    import html, re",
			"                    preview = html.unescape(re.sub(r'<[^>]*>', ' ', raw))",
			"                    preview = re.sub(r'\\s+', ' ', preview).strip()",
			"                size = len(raw.encode('utf-8', errors='replace'))",
			"            else:",
			"                preview = str(raw)",
			"                size = len(preview.encode('utf-8', errors='replace'))",
			"            if str(mime).startswith('image/'):",
			"                preview = 'Image output (' + str(mime) + ', ' + str(size) + ' bytes)'",
			"            elif str(mime).endswith('mimebundle'):",
			"                preview = 'Rich marimo output (' + str(mime) + ', ' + str(size) + ' bytes)'",
			"            output = {'mimetype': str(mime), 'preview': preview[:500], 'truncated': len(preview) > 500, 'bytes': size}",
			"            if str(mime).startswith('image/') and isinstance(raw, str) and raw.startswith('data:image/') and len(raw) <= 524288: output['image_data_url'] = raw",
			"        console = []",
			"        for message in cell.console_outputs:",
			"            try:",
			"                mime = getattr(message, 'mimetype', None) or getattr(message, 'mime_type', None) or 'text/plain'",
			"                raw = getattr(message, 'data', message)",
			"                text = raw if isinstance(raw, str) else str(raw)",
			"                console.append({'mimetype': str(mime), 'text': text[:12000], 'truncated': len(text) > 12000})",
			"            except Exception: continue",
			"        errors = [{'kind': err.kind, 'message': err.msg} for err in cell.errors]",
			"        row = {'id': cid, 'name': cell.name or None, 'source': cell.code, 'defs': defs, 'refs': refs, 'values': values, 'console': console, 'parents': [{'cell_id': key, 'variables': sorted(set(value))} for key, value in parents.items()], 'children': [{'cell_id': key, 'variables': sorted(set(value))} for key, value in children.items()], 'status': str(status or 'unknown'), 'output': output, 'errors': errors}",
			"        reference = graphimo_reference(cell.code)",
			"        group_reference = graphimo_group_reference(cell.code)",
			"        if reference is not None: row['graphimo_component'] = reference; row['graphimo_kind'] = 'base'",
			"        elif group_reference is not None: row['graphimo_group'] = group_reference; row['graphimo_kind'] = 'group'",
			"        elif cell.code.startswith('# Graphimo annotation'): row['graphimo_kind'] = 'annotation'",
			"        elif cell.code.startswith('# Graphimo display'): row['graphimo_kind'] = 'display'",
			"        cells.append(row)",
			"    result = {'notebook': {'path': " .. ui.json.encode(args.notebook_path or "") .. ", 'session_id': " .. session_id .. "}, 'cells': cells, 'edges': edges}",
			"    if controls is not None: result['controls'] = controls",
			"print('GRAPHIMO_RESULT:' + json.dumps(result, default=str, separators=(',', ':'))) ",
		}, "\n")
	elseif operation == "list_components" then
		return table.concat({
			"import ast, hashlib, importlib, inspect, json, math, pathlib, sys, textwrap, typing",
			"LITERAL_ALIAS_TYPE = type(typing.Literal[0])",
			"TUPLE_ALIAS_TYPES = {type(tuple[int, ...]), type(typing.Tuple[int, ...])}",
			"def scalar(value): return value is None or type(value) in (bool, int, str) or (type(value) is float and math.isfinite(value))",
			"def literal_choices(annotation):",
			"    if type(annotation) is not LITERAL_ALIAS_TYPE or annotation.__origin__ is not typing.Literal: return None",
			"    return typing.get_args(annotation)",
			"def annotation_text(annotation):",
			"    if annotation is inspect.Parameter.empty: return None",
			"    if type(annotation) is str: return annotation",
			"    if annotation is None: return 'None'",
			"    if type(annotation) is type: return type.__getattribute__(annotation, '__name__')",
			"    choices = literal_choices(annotation)",
			"    if choices is not None and all(scalar(choice) for choice in choices): return 'Literal[' + ', '.join(repr(choice) for choice in choices) + ']'",
			"    return None",
			"class SafeAnnotation:",
			"    def __init__(self, text): self.text = text",
			"    def __repr__(self): return self.text",
			"class OmittedDefault:",
			"    def __repr__(self): return '...'",
			"def safe_signature(sig):",
			"    safe_parameters = []",
			"    for parameter in sig.parameters.values():",
			"        annotation = annotation_text(parameter.annotation); choices = literal_choices(parameter.annotation)",
			"        if annotation is None and choices is not None and all(scalar(choice) for choice in choices): annotation = 'Literal[' + ', '.join(repr(choice) for choice in choices) + ']'",
			"        default = parameter.default if parameter.default is inspect.Parameter.empty or scalar(parameter.default) else OmittedDefault()",
			"        safe_parameters.append(parameter.replace(annotation=SafeAnnotation(annotation) if annotation is not None else inspect.Parameter.empty, default=default))",
			"    returned = annotation_text(sig.return_annotation)",
			"    return str(sig.replace(parameters=safe_parameters, return_annotation=SafeAnnotation(returned) if returned is not None else inspect.Signature.empty))",
			"def tuple_annotation_items(annotation):",
			"    if type(annotation) is str:",
			"        try: expression = ast.parse(annotation, mode='eval').body",
			"        except SyntaxError: return None",
			"        if not isinstance(expression, ast.Subscript): return None",
			"        value = expression.value",
			"        if isinstance(value, ast.Name) and value.id in ('tuple', 'Tuple'): pass",
			"        elif isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == 'typing' and value.attr == 'Tuple': pass",
			"        else: return None",
			"        items = expression.slice.elts if isinstance(expression.slice, ast.Tuple) else [expression.slice]",
			"        if not items or any(isinstance(item, ast.Constant) and item.value is Ellipsis for item in items): return None",
			"        return [ast.unparse(item) for item in items]",
			"    if type(annotation) not in TUPLE_ALIAS_TYPES or typing.get_origin(annotation) is not tuple: return None",
			"    args = typing.get_args(annotation)",
			"    if not args or any(item is Ellipsis for item in args): return None",
			"    return [annotation_text(item) for item in args]",
			"class ReturnVisitor(ast.NodeVisitor):",
			"    def __init__(self, root): self.root = root; self.returns = []",
			"    def visit_FunctionDef(self, node):",
			"        if node is self.root: self.generic_visit(node)",
			"    def visit_AsyncFunctionDef(self, node):",
			"        if node is self.root: self.generic_visit(node)",
			"    def visit_ClassDef(self, node): return None",
			"    def visit_Return(self, node): self.returns.append(node)",
			"def object_contract(inputs): return {'inputs': inputs, 'outputs': [{'index': 1, 'name': 'result', 'annotation': 'object'}], 'output_mode': 'object'}",
			"def base_contract(value, sig, inputs):",
			"    empty = {'inputs': inputs, 'outputs': [], 'output_mode': 'none'}",
			"    ann = sig.return_annotation; ann_text = annotation_text(ann)",
			"    if ann is None or ann_text == 'None': return empty",
			"    try:",
			"        source = textwrap.dedent(inspect.getsource(value)); tree = ast.parse(source); target_name = getattr(value, '__name__', None)",
			"    except (OSError, TypeError, ValueError, SyntaxError, AttributeError): return object_contract(inputs)",
			"    function = next((node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == target_name), None)",
			"    if function is None: return object_contract(inputs)",
			"    visitor = ReturnVisitor(function); visitor.visit(function)",
			"    if not visitor.returns or all(node.value is None for node in visitor.returns): return empty",
			"    if any(node.value is None for node in visitor.returns): return object_contract(inputs)",
			"    values = [node.value for node in visitor.returns]",
			"    tuple_items = tuple_annotation_items(ann)",
			"    if tuple_items is not None:",
			"        if not all(isinstance(node, ast.Tuple) and len(node.elts) == len(tuple_items) for node in values): return object_contract(inputs)",
			"        outputs = []",
			"        for position, item_annotation in enumerate(tuple_items, 1):",
			"            parts = [node.elts[position - 1] for node in values]; expressions = [ast.unparse(part) for part in parts]",
			"            expression = expressions[0] if all(item == expressions[0] for item in expressions) else None",
			"            names = [part.id if isinstance(part, ast.Name) else None for part in parts]",
			"            name = names[0] if names[0] and all(item == names[0] for item in names) else f'output_{position}'",
			"            output = {'index': position, 'name': name}",
			"            if expression is not None: output['expression'] = expression",
			"            if item_annotation is not None: output['annotation'] = item_annotation",
			"            outputs.append(output)",
			"        return {'inputs': inputs, 'outputs': outputs, 'output_mode': 'tuple'}",
			"    if any(isinstance(node, (ast.Tuple, ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp)) for node in values): return object_contract(inputs)",
			"    expressions = [ast.unparse(node) for node in values]; expression = expressions[0] if all(item == expressions[0] for item in expressions) else None",
			"    names = [node.id if isinstance(node, ast.Name) else None for node in values]",
			"    name = names[0] if names[0] and all(item == names[0] for item in names) else 'output_1'",
			"    scalar_annotation = ann_text in ('bool', 'float', 'int', 'str') or (isinstance(ann_text, str) and ann_text.startswith('Literal['))",
			"    scalar_constants = all(isinstance(node, ast.Constant) and (type(node.value) in (bool, int, str) or (type(node.value) is float and math.isfinite(node.value))) for node in values)",
			"    if not scalar_annotation and not scalar_constants: return object_contract(inputs)",
			"    output = {'index': 1, 'name': name}",
			"    if expression is not None: output['expression'] = expression",
			"    if ann_text is not None: output['annotation'] = ann_text",
			"    return {'inputs': inputs, 'outputs': [output], 'output_mode': 'single'}",
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
			"        sig = None",
			"        try:",
			"            sig = inspect.signature(value); signature = safe_signature(sig)",
			"            parameters = []",
			"            for p in sig.parameters.values():",
			"                if p.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY): continue",
			"                has_default = p.default is not inspect.Parameter.empty",
			"                parameter = {'name': p.name, 'required': not has_default, 'positional': p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD, 'has_default': has_default}",
			"                if has_default and scalar(p.default): parameter['default'] = p.default",
			"                annotation = annotation_text(p.annotation)",
			"                if annotation is not None: parameter['annotation'] = annotation",
			"                choices = literal_choices(p.annotation)",
			"                if choices is not None and all(scalar(choice) for choice in choices): parameter['choices'] = list(choices)",
			"                parameters.append(parameter)",
			"        except (TypeError, ValueError): signature = '(...)'; parameters = []",
			"        base_block = base_contract(value, sig, parameters) if sig is not None else {'inputs': [], 'outputs': [{'index': 1, 'name': 'result', 'annotation': 'object'}], 'output_mode': 'object'}",
			"        doc = inspect.getdoc(value) or inspect.getdoc(module) or ''",
			"        callables.append({'name': name, 'signature': signature, 'description': doc.splitlines()[0] if doc else '', 'parameters': parameters, 'base_block': base_block})",
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
			"groups = []",
			"groups_root = root / 'groups'",
			"if groups_root.is_dir():",
			"    for meta_path in sorted(groups_root.rglob('group.json')):",
			"        try:",
			"            meta = json.loads(meta_path.read_text(encoding='utf-8'))",
			"            if not isinstance(meta, dict) or meta.get('kind') != 'graphimo-group' or meta.get('version') != 1: continue",
			"            group_id = meta_path.parent.relative_to(groups_root).as_posix()",
			"            notebook = meta_path.parent / 'group.py'",
			"            if not notebook.is_file(): continue",
			"            digest = hashlib.sha256(notebook.read_bytes()).hexdigest()",
			"            groups.append({'id': group_id, 'name': str(meta.get('name') or meta_path.parent.name), 'category': str(meta.get('category') or 'Groups'), 'path': str(notebook), 'source_sha256': digest, 'inputs': meta.get('inputs') or [], 'outputs': meta.get('outputs') or []})",
			"        except Exception: continue",
			"print('GRAPHIMO_RESULT:' + json.dumps({'ok': True, 'components': components, 'groups': groups}, separators=(',', ':'))) ",
		}, "\n")
	elseif operation == "save_group" then
		return table.concat({
			"import ast, hashlib, json, pathlib, re, textwrap",
			"import marimo._code_mode as cm",
			"def slug(text):",
			"    value = re.sub(r'[^A-Za-z0-9_-]+', '-', str(text).strip()).strip('-_').lower()",
			"    return value or 'group'",
			"selected = set(" .. ui.json.encode(args.cell_ids or {}) .. ")",
			"positions = " .. ui.json.encode(args.positions or {}),
			"name = " .. ui.json.encode(args.name or "Group"),
			"category_raw = " .. ui.json.encode(args.category or "general"),
			"notebook_path = pathlib.Path(" .. ui.json.encode(args.notebook_path or "") .. ").resolve()",
			"async with cm.get_context() as ctx:",
			"    ordered = [cell for cell in ctx.cells if str(cell.id) in selected]",
			"    if not ordered: raise ValueError('No selected marimo cells')",
			"    graph_cells = ctx.graph.cells",
			"    owners = {}",
			"    for cid, impl in graph_cells.items():",
			"        for definition in getattr(impl, 'defs', set()): owners.setdefault(definition, set()).add(str(cid))",
			"    inputs, outside_outputs, leaf_outputs = set(), set(), set()",
			"    for cell in ordered:",
			"        cid = str(cell.id); impl = graph_cells.get(cell.id)",
			"        refs = set(getattr(impl, 'refs', set())) if impl else set(); defs = set(getattr(impl, 'defs', set())) if impl else set()",
			"        for ref in refs:",
			"            if not any(owner in selected for owner in owners.get(ref, set())): inputs.add(ref)",
			"        for definition in defs:",
			"            selected_users, outside_users = False, False",
			"            for child_id, child_impl in graph_cells.items():",
			"                if definition not in getattr(child_impl, 'refs', set()): continue",
			"                if str(child_id) in selected: selected_users = True",
			"                else: outside_users = True",
			"            if outside_users: outside_outputs.add(definition)",
			"            if not selected_users: leaf_outputs.add(definition)",
			"    outputs = sorted(outside_outputs or leaf_outputs)",
			"    inputs = sorted(inputs)",
			"    chunks = ['import marimo', '', '__generated_with = \"0.25.2\"', 'app = marimo.App()', '']",
			"    saved_cells = []",
			"    for index, cell in enumerate(ordered, start=1):",
			"        impl = graph_cells.get(cell.id); refs = sorted(getattr(impl, 'refs', set())) if impl else []; defs = sorted(getattr(impl, 'defs', set())) if impl else []",
			"        body = cell.code if cell.code.strip() else 'pass'",
			"        try:",
			"            tree = ast.parse(body); is_async = any(isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for node in ast.walk(tree))",
			"        except Exception: is_async = False",
			"        prefix = 'async def' if is_async else 'def'",
			"        chunks.append('@app.cell')",
			"        chunks.append(f'{prefix} block_{index}(' + ', '.join(refs) + '):')",
			"        chunks.append(textwrap.indent(body, '    '))",
			"        if defs:",
			"            returned = ', '.join(defs)",
			"            if len(defs) == 1: returned += ','",
			"            chunks.append('    return (' + returned + ')')",
			"        else: chunks.append('    return')",
			"        chunks.append('')",
			"        saved_cells.append({'source_id': str(cell.id), 'function': f'block_{index}', 'refs': refs, 'defs': defs, 'layout': positions.get(str(cell.id), {})})",
			"    chunks.extend(['if __name__ == \"__main__\":', '    app.run()', ''])",
			"    category_parts = [slug(part) for part in re.split(r'[/\\\\]+', category_raw) if part.strip()] or ['general']",
			"    group_slug = slug(name)",
			"    folder = notebook_path.parent / 'components' / 'groups'",
			"    for part in category_parts: folder = folder / part",
			"    folder = folder / group_slug; folder.mkdir(parents=True, exist_ok=True)",
			"    group_file = folder / 'group.py'; group_file.write_text('\\n'.join(chunks), encoding='utf-8')",
			"    group_id = folder.relative_to(notebook_path.parent / 'components' / 'groups').as_posix()",
			"    metadata = {'kind': 'graphimo-group', 'version': 1, 'id': group_id, 'name': name, 'category': '/'.join(category_parts), 'inputs': [{'name': item} for item in inputs], 'outputs': [{'name': item} for item in outputs], 'cells': saved_cells}",
			"    (folder / 'group.json').write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\\n', encoding='utf-8')",
			"    digest = hashlib.sha256(group_file.read_bytes()).hexdigest()",
			"result = {'ok': True, 'group': {'id': group_id, 'name': name, 'category': '/'.join(category_parts), 'path': str(group_file), 'source_sha256': digest, 'inputs': metadata['inputs'], 'outputs': metadata['outputs']}}",
			"print('GRAPHIMO_RESULT:' + json.dumps(result, separators=(',', ':'))) ",
		}, "\n")
	elseif operation == "add_group" then
		return table.concat({
			"import hashlib, json, pathlib",
			"group_id = " .. ui.json.encode(args.group_id),
			"prefix = " .. ui.json.encode(args.output_prefix or "group"),
			"root = pathlib.Path(" .. ui.json.encode(args.components_root) .. ").resolve() / 'groups'",
			"folder = (root / group_id).resolve()",
			"if root not in folder.parents: raise ValueError('Invalid group path')",
			"meta = json.loads((folder / 'group.json').read_text(encoding='utf-8')); notebook = folder / 'group.py'",
			"digest = hashlib.sha256(notebook.read_bytes()).hexdigest()",
			"inputs = [str(item['name']) for item in meta.get('inputs', []) if isinstance(item, dict) and isinstance(item.get('name'), str)]",
			"outputs = [str(item['name']) for item in meta.get('outputs', []) if isinstance(item, dict) and isinstance(item.get('name'), str)]",
			"def ident(text):",
			"    value = ''.join(ch if ch.isalnum() or ch == '_' else '_' for ch in text)",
			"    if not value or value[0].isdigit(): value = '_' + value",
			"    return value",
			"actual_outputs = {name: ident(prefix + '__' + name) for name in outputs}",
			"bindings = {name: name for name in inputs}",
			"lines = [f'# Graphimo group: {group_id}', f'# Graphimo group sha256: {digest}', '# Graphimo group inputs: ' + json.dumps(bindings, separators=(',', ':')), '# Graphimo group outputs: ' + json.dumps(actual_outputs, separators=(',', ':'))]",
			"lines.append(\"_graphimo_group_app = __import__('marimo._ast.load', fromlist=['load_app']).load_app(\" + repr(str(notebook)) + \")\")",
			"defs_expr = '{' + ', '.join(repr(name) + ': ' + name for name in inputs) + '}'",
			"lines.append('_graphimo_group_result = await _graphimo_group_app.clone().embed(defs=' + defs_expr + ')')",
			"for name in outputs: lines.append(actual_outputs[name] + ' = _graphimo_group_result.defs[' + repr(name) + ']')",
			"source = '\\n'.join(lines) + '\\n'",
			"print('GRAPHIMO_RESULT:' + json.dumps({'ok': True, 'source': source, 'group_id': group_id, 'inputs': bindings, 'outputs': actual_outputs, 'source_sha256': digest}, separators=(',', ':'))) ",
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
	elseif operation == "edit" then
		return table.concat({
			"import json",
			"import marimo._code_mode as cm",
			"async with cm.get_context() as ctx:",
			"    cell_id = " .. ui.json.encode(args.cell_id),
			"    current = ctx.cells[cell_id]",
			"    _ = current.code",
			"    ctx.edit_cell(cell_id, code=" .. ui.json.encode(args.source) .. ")",
			"print('GRAPHIMO_RESULT:' + json.dumps({'ok': True, 'cell_id': cell_id}))",
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
			"        variable = " .. ui.json.encode(args.variable),
			"        input_name = " .. ui.json.encode(args.input_ref),
			"        if not source_impl or variable not in getattr(source_impl, 'defs', set()): return {'ok': False, 'error': 'unsafe_edit: selected source port is stale'}",
			"        tree = ast.parse(target.code)",
			"        generated = target.code.startswith('# Graphimo component:')",
			"        display = target.code.startswith('# Graphimo display')",
			"        if display:",
			"            if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr): return {'ok': False, 'error': 'unsafe_edit: display block is invalid'}",
			"            tree.body[0].value = ast.Name(id=variable, ctx=ast.Load()); ast.fix_missing_locations(tree)",
			"            updated = '# Graphimo display\\n' + ast.unparse(tree.body[0]) + '\\n'",
			"            compile(updated, '<graphimo-display-connect>', 'exec')",
			"            return {'ok': True, 'changed': True, 'source': updated, 'cell_id': target.id}",
			"        if generated:",
			"            if len(tree.body) != 1: return {'ok': False, 'error': 'unsafe_edit: generated component body is invalid'}",
			"            statement = tree.body[0]",
			"            if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call): call = statement.value",
			"            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call): call = statement.value",
			"            else: return {'ok': False, 'error': 'unsafe_edit: generated component call is invalid'}",
			"            replaced = False",
			"            for keyword_arg in call.keywords:",
			"                if keyword_arg.arg == input_name:",
			"                    keyword_arg.value = ast.Name(id=variable, ctx=ast.Load()); replaced = True; break",
			"            if not replaced: call.keywords.append(ast.keyword(arg=input_name, value=ast.Name(id=variable, ctx=ast.Load())))",
			"            ast.fix_missing_locations(tree)",
			"            markers = '\\n'.join(target.code.splitlines()[:3])",
			"            updated = markers + '\\n' + ast.unparse(statement) + '\\n'",
			"            compile(updated, '<graphimo-connect>', 'exec')",
			"            return {'ok': True, 'changed': True, 'source': updated, 'cell_id': target.id}",
			"        if not target_impl or input_name not in getattr(target_impl, 'refs', set()) or variable in getattr(target_impl, 'defs', set()): return {'ok': False, 'error': 'unsafe_edit: selected target port is stale'}",
			"        if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.Assign, ast.AnnAssign)): return {'ok': False, 'error': 'unsafe_edit: target must be one assignment'}",
			"        assignment = tree.body[0]",
			"        call = assignment.value",
			"        if not isinstance(call, ast.Call) or not isinstance(call.func, (ast.Name, ast.Attribute)) or not call.args or call.keywords or not all(isinstance(arg, ast.Name) for arg in call.args): return {'ok': False, 'error': 'unsafe_edit: target call is ambiguous'}",
			"        matches = [arg for arg in call.args if arg.id == input_name]",
			"        if len(matches) != 1: return {'ok': False, 'error': 'unsafe_edit: selected reference is not a unique positional argument'}",
			"        arg = matches[0]; lines = target.code.splitlines(keepends=True); raw = lines[arg.lineno - 1].encode('utf-8')",
			"        updated = ''.join(lines[:arg.lineno - 1]) + raw[:arg.col_offset].decode('utf-8') + variable + raw[arg.end_col_offset:].decode('utf-8') + ''.join(lines[arg.lineno:])",
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
		if is_failure(sessions) then return sessions end
		local chosen = nil
		for _, session in sessions do
			if session.session_id == session_id then chosen = session; break end
		end
		if not chosen and (session_id == nil or session_id == "") and #sessions == 1 then chosen = sessions[1] end
		if not chosen then
			if #sessions == 0 then return failure("No active marimo notebook session was found", "no_active_session") end
			return failure("Choose one of the " .. tostring(#sessions) .. " active marimo sessions", "selection_required")
		end
		local args = { session_id = chosen.session_id, notebook_path = chosen.path }
		local code = code_for("inspect", args)
		return execute(zeron, base, token, chosen.session_id, code, 45):after(function(snapshot)
			if is_failure(snapshot) then return snapshot end
			snapshot.notebook.path = chosen.path
			return snapshot
		end)
	end)
end

function Bridge.list_components(zeron, base, token, session_id, notebook_path)
	local root = string.gsub(notebook_path, "[^/]+$", "") .. "components"
	return execute(zeron, base, token, session_id, code_for("list_components", { components_root = root }), 60)
end

function Bridge.list_components_for_active(zeron, base, token, session_id)
	return Bridge.inspect(zeron, base, token, session_id):after(function(snapshot)
		if is_failure(snapshot) then return snapshot end
		return Bridge.list_components(zeron, base, token, snapshot.notebook.session_id, snapshot.notebook.path)
	end)
end

local function valid_identifier(value)
	return type(value) == "string" and string.match(value, "^[%a_][%w_]*$") ~= nil
end

function Bridge.component_source(component_id, callable, output_name, bindings, source_hash, parameters, contract)
	if type(component_id) ~= "string" or component_id == "" or string.find(component_id, "..", 1, true) or string.sub(component_id, 1, 1) == "/" or string.find(component_id, "[\r\n]") then
		return nil, "Choose a component inside the components folder"
	end
	if not valid_identifier(callable) or not valid_identifier(output_name) then return nil, "Callable and output must be Python identifiers" end
	bindings = bindings or {}
	local bound = {}
	for name, binding in bindings do
		if not valid_identifier(name) or type(binding) ~= "table" then return nil, "Invalid component argument" end
		if type(binding.variable) == "string" then
			if not valid_identifier(binding.variable) then return nil, "Component inputs must be Python variable names" end
			bound[name] = { variable = binding.variable }
		elseif binding.literal ~= nil then
			bound[name] = { literal = binding.literal }
		else
			return nil, "Each component argument needs a variable or literal value"
		end
	end
	local args = {}
	for _, parameter in parameters or {} do
		local name = parameter.name
		local binding = bound[name]
		if binding and binding.variable then
			table.insert(args, name .. "=" .. binding.variable)
		elseif binding and binding.literal ~= nil then
			table.insert(args, name .. "=__import__('json').loads(" .. ui.json.encode(ui.json.encode(binding.literal)) .. ")")
		elseif parameter.required then
			-- An unconnected required input remains an ordinary marimo reference
			-- until the user wires another output into this port.
			table.insert(args, name .. "=" .. name)
		end
	end
	local module_rel = string.gsub(component_id, "%.py$", "")
	module_rel = string.gsub(module_rel, "/", ".")
	local module_name = "components." .. module_rel
	local called = "__import__('importlib').reload(__import__('importlib').import_module(" .. ui.json.encode(module_name) .. "))." .. callable
	local call = called .. "(" .. table.concat(args, ", ") .. ")"
	local source = "# Graphimo component: " .. component_id .. "\n"
	source ..= "# Graphimo source sha256: " .. tostring(source_hash or "unknown") .. "\n"
	source ..= "# Graphimo callable: " .. callable .. "\n"
	local outputs = if type(contract) == "table" then (contract.outputs or {}) else {}
	if #outputs == 0 then
		source ..= call .. "\n"
	elseif #outputs == 1 then
		source ..= output_name .. " = " .. call .. "\n"
	else
		local names = {}
		for index, output in outputs do
			local suffix = tostring(output.name or ("output_" .. tostring(index)))
			suffix = string.gsub(suffix, "[^%w_]", "_")
			if not valid_identifier(suffix) then suffix = "output_" .. tostring(index) end
			table.insert(names, output_name .. "_" .. suffix)
		end
		source ..= table.concat(names, ", ") .. " = " .. call .. "\n"
	end
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
	if not safe then return failure("Choose a component inside the components folder", "invalid_component") end
	return Bridge.sessions(zeron, base, token):after(function(sessions)
		if is_failure(sessions) then return sessions end
		local current = nil
		for _, session in sessions do if session.session_id == session_id then current = session; break end end
		if not current then return failure("The selected marimo session is no longer active", "no_active_session") end
		return Bridge.list_components(zeron, base, token, session_id, current.path):after(function(result)
			if is_failure(result) then return result end
			local selected_component = nil
			for _, item in result.components or {} do if item.id == component_id then selected_component = item; break end end
			if not selected_component then return failure("The component changed or disappeared; refresh the library", "component_unavailable") end
			local selected_callable = nil
			for _, item in selected_component.callables do if item.name == callable then selected_callable = item; break end end
			if not selected_callable then return failure("The callable changed or disappeared; refresh the library", "callable_unavailable") end
			local source, why = Bridge.component_source(
				component_id,
				callable,
				output_name,
				bindings,
				selected_component.source_sha256,
				selected_callable.parameters or {},
				selected_callable.base_block
			)
			if not source then return failure(why, "invalid_component_arguments") end
			return Bridge.add_cell(zeron, base, token, session_id, source)
		end)
	end)
end

function Bridge.save_group(zeron, base, token, session_id, notebook_path, cell_ids, positions, name, category)
	return execute(zeron, base, token, session_id, code_for("save_group", {
		notebook_path = notebook_path,
		cell_ids = cell_ids,
		positions = positions,
		name = name,
		category = category,
	}), 60)
end

function Bridge.add_group(zeron, base, token, session_id, notebook_path, group_id, output_prefix)
	local components_root = string.gsub(notebook_path, "[^/]+$", "") .. "components"
	return execute(zeron, base, token, session_id, code_for("add_group", {
		components_root = components_root,
		group_id = group_id,
		output_prefix = output_prefix,
	}), 60):after(function(result)
		if is_failure(result) then return result end
		return Bridge.add_cell(zeron, base, token, session_id, result.source)
	end)
end

function Bridge.run(zeron, base, token, session_id, cell_id)
	return execute(zeron, base, token, session_id, code_for("run", { cell_id = cell_id }), 300)
end

function Bridge.add_cell(zeron, base, token, session_id, source)
	return execute(zeron, base, token, session_id, code_for("add_cell", { source = source }), 60)
end

function Bridge.edit(zeron, base, token, session_id, cell_id, source)
	return execute(zeron, base, token, session_id, code_for("edit", { cell_id = cell_id, source = source }), 60)
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
