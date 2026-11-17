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

local mode, notebook_path, from_cell_id, variable, to_cell_id, input_ref, cell_id = ...
mode = mode or "inspect"
notebook_path = notebook_path or "/tmp/graphimo-live.py"
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
			if value == "SESSION_RESPONSE" then
				return { s_live = { session_id = "sess-live", filename = notebook_path, path = notebook_path } }
			elseif value == "STDOUT_RESPONSE" then
				local result
				if mode == "inspect" then result = snapshot
				elseif mode == "list_components" then result = { ok = true, components = components }
				elseif mode == "add_component" and capture == 1 then result = { ok = true, components = components }
				elseif mode == "add_component" then result = { ok = true, cell_id = "added-cell" }
				elseif mode == "connect" then result = { ok = true, changed = true }
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
		if not failure then return setmetatable({ error = self.error }, Promise) end
		local ok, value = pcall(failure, self.error)
		return setmetatable(if ok then { value = value } else { error = value }, Promise)
	end
	if not success then return self end
	local ok, value = pcall(success, self.value)
	if not ok then return setmetatable({ error = value }, Promise) end
	if getmetatable(value) == Promise then return value end
	return setmetatable({ value = value }, Promise)
end

local zeron = {}
function zeron.run(_program, args)
	local destination = args[#args]
	if string.find(destination, "/api/sessions", 1, true) then
		return setmetatable({ value = { stdout = "SESSION_RESPONSE" } }, Promise)
	end
	-- The actual JSON encoder is intentionally opaque to this host shim. The
	-- generated request body is the final argument after "-d"; code is captured
	-- separately by Bridge's request encoder below through the registry wrapper.
	if captured_codes[capture] then print("GRAPHIMO_CODE_BEGIN\n" .. captured_codes[capture] .. "\nGRAPHIMO_CODE_END") end
	local events = "event: stdout\ndata: STDOUT_RESPONSE\n\nevent: done\ndata: DONE_RESPONSE\n\n"
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
elseif mode == "inspect" then
	result = Bridge.inspect(zeron, "http://127.0.0.1:2718", "", "s_live")
elseif mode == "list_components" then
	result = Bridge.list_components(zeron, "http://127.0.0.1:2718", "", "s_live", notebook_path)
elseif mode == "add_component" then
	result = Bridge.add_component(zeron, "http://127.0.0.1:2718", "", "s_live", "normalize.py", "normalize", "normalized", { image = { variable = "data" } })
elseif mode == "run" then
	result = Bridge.run(zeron, "http://127.0.0.1:2718", "", "s_live", cell_id or "target-cell")
elseif mode == "connect" then
	result = Bridge.connect(zeron, "http://127.0.0.1:2718", "", "s_live", from_cell_id or "source-cell", variable or "data", to_cell_id or "target-cell", input_ref or "processed")
else
	error("unknown test mode: " .. tostring(mode))
end

if type(result) == "table" and result.error then error(tostring(result.error)) end
