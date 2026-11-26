-- Test shim for the production Luau bridge. It records the Python code Graphimo
-- sends to marimo while replacing only Zeron's command/promise/JSON host APIs.
local function json_quote(value)
	value = string.gsub(value, "\\", "\\\\")
	value = string.gsub(value, '"', '\\"')
	value = string.gsub(value, "\n", "\\n")
	value = string.gsub(value, "\r", "\\r")
	value = string.gsub(value, "\t", "\\t")
	return '"' .. value .. '"'
end

local json_values = {}
local function json_encode(value)
	local kind = type(value)
	if kind == "string" then return json_quote(value) end
	if kind == "number" or kind == "boolean" then return tostring(value) end
	if value == nil then return "null" end
	if kind ~= "table" then error("unsupported JSON value: " .. kind) end
	local is_array, count = true, 0
	for key in value do
		count += 1
		if type(key) ~= "number" or key < 1 or key % 1 ~= 0 then is_array = false end
	end
	if is_array and count == #value then
		local items = {}
		for _, item in value do table.insert(items, json_encode(item)) end
		local encoded = "[" .. table.concat(items, ",") .. "]"
		json_values[encoded] = value
		return encoded
	end
	local items = {}
	for key, item in value do
		table.insert(items, json_quote(tostring(key)) .. ":" .. json_encode(item))
	end
	local encoded = "{" .. table.concat(items, ",") .. "}"
	json_values[encoded] = value
	return encoded
end

local mode, notebook_path, from_cell_id, variable, to_cell_id, input_ref, cell_id, source_hash, replacement_source = ...
mode = mode or "inspect"
notebook_path = notebook_path or "/tmp/graphimo-live.py"
local session_object = { s_live = { session_id = "stable-live", filename = notebook_path, path = notebook_path } }
local session_array = { { session_id = "s_array", filename = notebook_path, path = notebook_path } }
local session_wrapped = { sessions = session_object }
local session_object_json = json_encode(session_object)
local session_array_json = json_encode(session_array)
local session_wrapped_json = json_encode(session_wrapped)
local capture = 0
local captured_codes = {}
local snapshot = {
	notebook = { path = "/tmp/graphimo-live.py", session_id = "s_live" },
	cells = {},
	edges = {},
}
local components = {
	{
		id = "normalize.py",
		path = "normalize.py",
		category = {},
		kind = "file",
		source_sha256 = string.rep("a", 64),
		callables = {
			{
				name = "normalize",
				signature = "(image)",
				parameters = { { name = "image", required = true, positional = true } },
			},
		},
	},
}

ui = {
	json = {
		encode = json_encode,
		decode = function(value)
			if json_values[value] then return json_values[value] end
			if value == "null" then return nil end
			if value == "STDOUT_RESPONSE" then
				local result
				if mode == "inspect" then result = snapshot
				elseif mode == "list_components" then result = { ok = true, components = components }
				elseif mode == "add_component" and capture == 1 then result = { ok = true, components = components }
				elseif mode == "add_component" then result = { ok = true, cell_id = "added-cell" }
				elseif mode == "connect" then result = { ok = true, changed = true }
				elseif mode == "edit" then result = { ok = true, changed = true, cell_id = cell_id, source = replacement_source }
				elseif mode == "restore_cells" then result = { ok = true, cell_ids = { "restored-a", "restored-b" } }
			else result = { ok = true, status = "queued" } end
				return { data = "GRAPHIMO_RESULT:" .. json_encode(result) }
			elseif value == "DONE_RESPONSE" then
				return { success = true }
			end
			error("Unexpected JSON decode in test shim: " .. tostring(value))
		end,
	},
}

local Bridge = require("../plugin/marimo_bridge")
getfenv(Bridge.inspect).ui = ui

local Promise = {}
Promise.__index = Promise
function Promise:after(success, failure)
	if self.error then
		if failure then failure(self.error) end
		return nil
	end
	if success then success(self.value) end
	return nil
end

local function collect(future)
	local result = { pending = true }
	future:after(function(value)
		result.pending = false
		result.value = value
	end, function(err)
		result.pending = false
		result.error = err
	end)
	assert(not result.pending, "test fixture request unexpectedly stayed asynchronous")
	return result
end

local zeron = {}
function zeron.run(_program, args)
	local destination = args[#args]
	if string.find(destination, "/api/sessions", 1, true) then
		local body, content_type = session_object_json, "application/json"
		if mode == "sessions_array" then body = session_array_json
		elseif mode == "sessions_wrapped" then body = session_wrapped_json
		elseif mode == "sessions_html" then body, content_type = "<!doctype html><html></html>", "text/html; charset=utf-8"
		elseif mode == "sessions_empty" then body = ""
		elseif mode == "sessions_null" then body = "null" end
		if mode == "sessions_url" then print("GRAPHIMO_REQUEST_URL:" .. destination) end
		if mode == "sessions_connection" or mode == "components_connection" then
			return setmetatable({ value = { status = 7, stdout = "\nGRAPHIMO_HTTP_META:000:" } }, Promise)
		end
		return setmetatable({ value = { status = 0, stdout = body .. "\nGRAPHIMO_HTTP_META:200:" .. content_type } }, Promise)
	end
	-- The actual JSON encoder is intentionally opaque to this host shim. The
	-- generated request body is the final argument after "-d"; code is captured
	-- separately by Bridge's request encoder below through the registry wrapper.
	if captured_codes[capture] then print("GRAPHIMO_CODE_BEGIN\n" .. captured_codes[capture] .. "\nGRAPHIMO_CODE_END") end
	local events = if mode == "execute_empty" then "" else "event: stdout\ndata: STDOUT_RESPONSE\n\nevent: done\ndata: DONE_RESPONSE\n\n"
	return setmetatable({ value = { stdout = events } }, Promise)
end

-- Intercept the encoder at the call site so the harness can expose the code
-- embedded in the JSON request without trying to decode arbitrary JSON.
local real_encode = ui.json.encode
local latest_request
ui.json.encode = function(value)
	local result = real_encode(value)
	if type(value) == "table" and type(value.code) == "string" then latest_request = value.code end
	return result
end
local original_run = zeron.run
zeron.run = function(program, args, options)
	local destination = args[#args]
	if not string.find(destination, "/api/sessions", 1, true) and latest_request then
		capture += 1
		captured_codes[capture] = latest_request
		latest_request = nil
	end
	return original_run(program, args, options)
end

local result
if mode == "source_safety" then
	local source, why = Bridge.component_source(
		"normalize.py",
		"normalize",
		"normalized",
		{ std = { variable = "saved_std" } },
		string.rep("a", 64),
		{ "image", "mean", "std" }
	)
	assert(source, why)
	print("GRAPHIMO_SOURCE:" .. source)
	result = true
	elseif mode == "command_denied" then
	local unavailable = { run = function() return nil end }
	local request = Bridge.sessions(unavailable, "http://127.0.0.1:2718", "")
	local result = collect(request)
	assert(result.error and string.find(tostring(result.error), "commands permission", 1, true), tostring(result.error))
	print("GRAPHIMO_COMMAND_DENIAL_HANDLED")
	result = true
elseif string.sub(mode, 1, 9) == "sessions_" then
	local base = if mode == "sessions_url" then "http://127.0.0.1:2718/?file=notebook.py#cell-1" else "http://127.0.0.1:2718"
	result = Bridge.sessions(zeron, base, "")
elseif mode == "inspect" then
	result = Bridge.inspect(zeron, "http://127.0.0.1:2718", "", "s_live")
elseif mode == "list_components" then
	result = Bridge.list_components(zeron, "http://127.0.0.1:2718", "", "s_live", notebook_path)
elseif mode == "add_component" then
	result = Bridge.add_component(zeron, "http://127.0.0.1:2718", "", "s_live", "normalize.py", "normalize", "normalized", { image = { variable = "data" } })
elseif mode == "run" or mode == "execute_empty" then
	result = Bridge.run(zeron, "http://127.0.0.1:2718", "", "s_live", cell_id or "target-cell")
elseif mode == "components_connection" then
	result = Bridge.list_components_for_active(zeron, "http://127.0.0.1:2718", "", "")
elseif mode == "tool_adapter" then
	local ToolAdapter = require("../plugin/tool_adapter")
	local delayed = {}
	function delayed:after(success, failure)
		self.success, self.failure = success, failure
		return nil
	end
	local answer = nil
	ToolAdapter.dispatch(function() return delayed end, {}, {}, function(ok, value) answer = { ok = ok, value = value } end)
	assert(answer == nil, "async tool must wait for its after callback")
	delayed.success({ ok = true, result = "resolved" })
	assert(answer and answer.ok and answer.value.result == "resolved", "host must receive resolved tool output")
	print("GRAPHIMO_TOOL_AWAITED:resolved")
	result = true
	elseif mode == "launcher_script" then
	getfenv(Bridge.open_notebook).ui = ui
	local launcher = {}
	function launcher.run(_program, args)
		assert(args[1] == "-c", "marimo launcher must use python -c")
		print("GRAPHIMO_LAUNCH_SCRIPT_BEGIN\n" .. args[2] .. "\nGRAPHIMO_LAUNCH_SCRIPT_END")
		local launch_result = { url = "http://127.0.0.1:2718", session_id = "", pid = 1, log_path = "/tmp/graphimo.log", started = true }
		local encoded = json_encode(launch_result)
		json_values[encoded] = launch_result
		return setmetatable({ value = { status = 0, stdout = "GRAPHIMO_LAUNCH:" .. encoded } }, Promise)
	end
	result = Bridge.open_notebook(launcher, notebook_path)
	elseif mode == "problem_log_script" then
	local logger = {}
	function logger.run(_program, args)
		print("GRAPHIMO_PROBLEM_LOG_SCRIPT_BEGIN\n" .. args[2] .. "\nGRAPHIMO_PROBLEM_LOG_SCRIPT_END")
		return setmetatable({ value = { status = 0, stdout = "" } }, Promise)
	end
	result = Bridge.append_problem(logger, "test problem")
elseif mode == "connect" then
	result = Bridge.connect(zeron, "http://127.0.0.1:2718", "", "s_live", from_cell_id or "source-cell", variable or "data", to_cell_id or "target-cell", input_ref or "processed")
	elseif mode == "edit" then
		result = Bridge.edit(zeron, "http://127.0.0.1:2718", "", "s_live", cell_id or "target-cell", source_hash or "", replacement_source or "")
	elseif mode == "restore_cells" then
		result = Bridge.restore_cells(zeron, "http://127.0.0.1:2718", "", "s_live", {
			{ source = source_hash or "restored_value = 1" },
			{ source = replacement_source or "restored_next = restored_value + 1" },
		})
	elseif mode == "replace_group_selection" then
		result = Bridge.replace_group_selection(zeron, "http://127.0.0.1:2718", "", "s_live", {
			selected_ids = { from_cell_id or "selected-a", to_cell_id or "selected-b" },
			source_hashes = {
				[from_cell_id or "selected-a"] = variable or string.rep("a", 64),
				[to_cell_id or "selected-b"] = input_ref or string.rep("b", 64),
			},
			parent_source = replacement_source or "# Graphimo group: group_1\\nfrom groups.group_1 import group_1\\nresult = group_1()\\n",
		})
	elseif mode == "replace_group_selection_invalid_ids" then
		result = Bridge.replace_group_selection(zeron, "http://127.0.0.1:2718", "", "s_live", {
			selected_ids = { { "not-a-cell-id" }, "selected-b" },
			source_hashes = {},
			parent_source = "# Graphimo group: group_1\\nfrom groups.group_1 import group_1\\nresult = group_1()\\n",
		})
else
	error("unknown test mode: " .. tostring(mode))
end

if type(result) == "table" and type(result.after) == "function" then result = collect(result) end
local operation_result = if type(result) == "table" and result.value ~= nil then result.value else result
if type(operation_result) == "table" and operation_result.error then
	local message = if type(operation_result.error) == "table" then tostring(operation_result.error.message) else tostring(operation_result.error)
	if mode == "sessions_connection" then
		assert(result.error == nil and operation_result.ok == false, "session failure should resolve as a structured value")
		assert(operation_result.error.code == "sessions_unavailable", "missing session error code")
		print("GRAPHIMO_SESSION_ERROR:" .. message)
	elseif mode == "execute_empty" then
		assert(result.error == nil and operation_result.ok == false, "execute failure should resolve as a structured value")
		assert(operation_result.error.code == "execute_failed", "missing execute error code")
		print("GRAPHIMO_EXECUTE_ERROR:" .. message)
	elseif mode == "components_connection" then
		assert(result.error == nil and operation_result.ok == false, "component-list failure should be propagated as a structured value")
		assert(operation_result.error.code == "sessions_unavailable", "missing propagated component-list error code")
		print("GRAPHIMO_OPERATION_ERROR:" .. message)
	elseif string.sub(mode, 1, 9) == "sessions_" then print("GRAPHIMO_SESSION_ERROR:" .. message)
	else error(tostring(operation_result.error)) end
elseif string.sub(mode, 1, 9) == "sessions_" then
	print("GRAPHIMO_SESSIONS:" .. json_encode(operation_result))
end
