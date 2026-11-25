local mode = ... or "execute"
local ui = { json = {} }
local encoded_values = {}

local function quote(value)
	return '"' .. string.gsub(string.gsub(tostring(value), "\\", "\\\\"), '"', '\\"') .. '"'
end

local function encode(value)
	local kind = type(value)
	if kind == "string" then return quote(value) end
	if kind == "number" or kind == "boolean" then return tostring(value) end
	if value == nil then return "null" end
	if kind ~= "table" then error("unsupported JSON type " .. kind) end
	local is_array, count = true, 0
	for key in value do
		count += 1
		if type(key) ~= "number" or key < 1 or key % 1 ~= 0 then is_array = false end
	end
	local items = {}
	if is_array and count == #value then
		for _, item in value do table.insert(items, encode(item)) end
		return "[" .. table.concat(items, ",") .. "]"
	end
	for key, item in value do table.insert(items, quote(key) .. ":" .. encode(item)) end
	return "{" .. table.concat(items, ",") .. "}"
end

ui.json.encode = function(value)
	local encoded = encode(value)
	encoded_values[encoded] = value
	return encoded
end

local connection = {
	base_url = "http://127.0.0.1:41735",
	token = string.rep("t", 48),
	project_id = "project-123",
	path = "/tmp/project.py",
	state = { blocks = {}, edges = {}, palette = { items = {} } },
}
local response_body = '{"result":{"changed":true},"state":{}}'
ui.json.decode = function(value)
	if value == "OPEN_RESPONSE" then return connection end
	if value == response_body then return { result = { changed = true }, state = {} } end
	if value == '{"cursor":2,"events":[{"type":"addon.event"}]}' then
		return { cursor = 2, events = { { type = "addon.event" } } }
	end
	error("Unexpected JSON decode: " .. tostring(value))
end

local captured = {}
local Promise = {}
Promise.__index = Promise
function Promise:after(success, failure)
	if self.error then
		if failure then failure(self.error) end
	elseif success then
		success(self.value)
	end
	return nil
end

local zeron = {}
function zeron.run(program, args, options)
	captured.program = program
	captured.args = args
	captured.options = options
	if program == "python3" then
		captured.script = args[2]
		return setmetatable({ value = { status = 0, stdout = "GRAPHIMO_PROJECT:OPEN_RESPONSE\n" } }, Promise)
	end
	captured.stdin = options.stdin
	for index, argument in args do
		if argument == "-d" then captured.body = args[index + 1] end
	end
	local stdout = if mode == "events" then '{"cursor":2,"events":[{"type":"addon.event"}]}' else response_body
	stdout ..= "\nGRAPHIMO_HTTP_META:200"
	return setmetatable({ value = { status = 0, stdout = stdout } }, Promise)
end

local Service = require("../plugin/graphimo_service")
getfenv(Service.execute).ui = ui
getfenv(Service.events).ui = ui
getfenv(Service.open_project).ui = ui

local function collect(promise)
	local result = { pending = true }
	promise:after(function(value) result.pending = false; result.value = value end, function(err) result.pending = false; result.error = err end)
	assert(not result.pending, "fixture must settle synchronously")
	assert(result.error == nil, tostring(result.error))
	return result.value
end

if mode == "open" then
	local opened = collect(Service.open_project(zeron, "/tmp/project.py"))
	assert(opened.project_id == "project-123")
	assert(string.find(captured.script, "GraphimoClient.connect_local()", 1, true))
	assert(string.find(captured.script, "client.open_project(sys.argv[1])", 1, true))
	print("GRAPHIMO_SERVICE_OPEN_OK")
elseif mode == "events" then
	local page = collect(Service.events(zeron, connection, 1, 25, 750))
	assert(page.cursor == 2 and #page.events == 1)
	assert(captured.args[1] == "-sS")
	assert(string.find(captured.args[#captured.args], "/v1/events?project=project-123&cursor=1&limit=25&wait_ms=750", 1, true))
	print("GRAPHIMO_SERVICE_EVENTS_OK")
else
	local value = collect(Service.execute(zeron, connection, "connect", { input_name = "x" }))
	assert(value.ok and value.result.changed)
	assert(table.find(captured.args, connection.base_url .. "/v1/execute") ~= nil)
	assert(string.find(captured.stdin, connection.token, 1, true))
	for _, argument in captured.args do assert(not string.find(argument, connection.token, 1, true)) end
	assert(string.find(captured.body, '"action":"connect"', 1, true))
	assert(string.find(captured.body, '"project_id":"project-123"', 1, true))
	print("GRAPHIMO_SERVICE_EXECUTE_OK")
end
