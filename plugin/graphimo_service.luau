-- Transport adapter for the host-independent Graphimo local service.
-- This module contains HTTP/bootstrap mechanics only; all graph semantics stay
-- in GraphimoApplication and the service.
local Service = {}

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
				result:after(
					function(next_value) settle(handler.next, "resolved", next_value) end,
					function(err) settle(handler.next, "rejected", err) end
				)
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
				result:after(
					function(value) settle(next_future, "resolved", value) end,
					function(err) settle(next_future, "rejected", err) end
				)
			else
				settle(next_future, "resolved", result)
			end
		end
	end
	return next_future
end

local function failure(message, code)
	return { ok = false, error = { code = code or "service_error", message = tostring(message) } }
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

local function valid_connection(connection)
	if type(connection) ~= "table" then return false end
	local base = connection.base_url
	local host, port = string.match(base or "", "^http://(127%.0%.0%.1):(%d+)/?$")
	return host ~= nil and tonumber(port) ~= nil and tonumber(port) >= 1 and tonumber(port) <= 65535
		and type(connection.token) == "string" and #connection.token >= 32
		and type(connection.project_id) == "string" and connection.project_id ~= ""
end

function Service.open_project(zeron, path)
	if type(path) ~= "string" or path == "" then
		return future():after(function() return failure("Choose a notebook first", "selection_required") end)
	end
	local script = table.concat({
		"import json, sys",
		"from graphimo.client import GraphimoClient",
		"client = GraphimoClient.connect_local()",
		"project = client.open_project(sys.argv[1])",
		"print('GRAPHIMO_PROJECT:' + json.dumps({'base_url': client.base_url, 'token': client.token, 'project_id': project.id, 'editor_url': project.editor_url, 'path': project.path, 'state': project.state}, allow_nan=False, separators=(',', ':')))"
	}, "\n")
	return from_zeron(zeron.run("python3", { "-c", script, path }, { timeout = 180 })):after(function(response)
		if type(response) ~= "table" or response.status ~= 0 then
			local detail = if type(response) == "table" and type(response.stderr) == "string" and response.stderr ~= "" then response.stderr else (response.stdout or "Graphimo service bootstrap failed")
			return failure(string.sub(detail, 1, 8000), "service_start_failed")
		end
		local stdout = response.stdout or ""
		local encoded = nil
		for line in string.gmatch(stdout .. "\n", "([^\r\n]*)[\r\n]") do
			if string.sub(line, 1, #"GRAPHIMO_PROJECT:") == "GRAPHIMO_PROJECT:" then encoded = string.sub(line, #"GRAPHIMO_PROJECT:" + 1) end
		end
		local ok, connection = pcall(ui.json.decode, encoded or "")
		if not ok or not valid_connection(connection) or type(connection.state) ~= "table" then
			return failure("Graphimo client returned an invalid project connection", "invalid_response")
		end
		return connection
	end)
end

function Service.create_project(zeron, path)
	if type(path) ~= "string" or path == "" then
		return future():after(function() return failure("Choose a notebook location first", "selection_required") end)
	end
	local script = table.concat({
		"import json, sys",
		"from graphimo.client import GraphimoClient",
		"client = GraphimoClient.connect_local()",
		"project = client.create_project(sys.argv[1])",
		"print('GRAPHIMO_PROJECT:' + json.dumps({'base_url': client.base_url, 'token': client.token, 'project_id': project.id, 'editor_url': project.editor_url, 'path': project.path, 'state': project.state}, allow_nan=False, separators=(',', ':')))"
	}, "\n")
	return from_zeron(zeron.run("python3", { "-c", script, path }, { timeout = 180 })):after(function(response)
		if type(response) ~= "table" or response.status ~= 0 then
			local detail = if type(response) == "table" and type(response.stderr) == "string" and response.stderr ~= "" then response.stderr else (response.stdout or "Graphimo project creation failed")
			return failure(string.sub(detail, 1, 8000), "project_create_failed")
		end
		local stdout = response.stdout or ""
		local encoded = nil
		for line in string.gmatch(stdout .. "\n", "([^\r\n]*)[\r\n]") do
			if string.sub(line, 1, #"GRAPHIMO_PROJECT:") == "GRAPHIMO_PROJECT:" then encoded = string.sub(line, #"GRAPHIMO_PROJECT:" + 1) end
		end
		local ok, connection = pcall(ui.json.decode, encoded or "")
		if not ok or not valid_connection(connection) or type(connection.state) ~= "table" then
			return failure("Graphimo client returned an invalid project connection", "invalid_response")
		end
		return connection
	end)
end

local function http(zeron, connection, method, path, body, timeout)
	if not valid_connection(connection) then
		return future():after(function() return failure("No active Graphimo service project", "selection_required") end)
	end
	local token = connection.token
	if string.find(token, "[\r\n]") then
		return future():after(function() return failure("Graphimo service token is malformed", "invalid_token") end)
	end
	local config = 'header = "Authorization: Bearer ' .. quote_config(token) .. '"\nheader = "Accept: application/json"\n'
	local args = { "-sS", "--connect-timeout", "3", "--max-time", tostring(timeout or 40), "-X", method,
		"--config", "-", "-w", "\\nGRAPHIMO_HTTP_META:%{http_code}", connection.base_url .. path }
	if body ~= nil then
		table.insert(args, "-H")
		table.insert(args, "Content-Type: application/json")
		table.insert(args, "-d")
		table.insert(args, ui.json.encode(body))
		config ..= 'header = "Content-Type: application/json"\n'
	end
	return from_zeron(zeron.run("curl", args, { stdin = config, timeout = timeout or 45 })):after(function(response)
		if type(response) ~= "table" then return failure("curl returned an invalid response", "transport_error") end
		if response.status ~= 0 then
			local detail = if type(response.stderr) == "string" and response.stderr ~= "" then response.stderr else "Graphimo service request failed"
			return failure(string.sub(detail, 1, 4000), "service_unavailable")
		end
		local output = response.stdout or ""
		local marker = string.find(output, "\nGRAPHIMO_HTTP_META:", 1, true)
		if not marker then return failure("Graphimo service returned no HTTP status", "invalid_response") end
		local payload = string.sub(output, 1, marker - 1)
		local status = tonumber(string.match(string.sub(output, marker + #"\nGRAPHIMO_HTTP_META:"), "^(%d+)"))
		local ok, value = pcall(ui.json.decode, payload)
		if not ok or type(value) ~= "table" then return failure("Graphimo service returned invalid JSON", "invalid_response") end
		if status == nil or status < 200 or status >= 300 or value.error ~= nil then
			local problem = value.error
			if type(problem) == "table" then return failure(problem.message or "Graphimo service request failed", problem.code or "service_error") end
			return failure("Graphimo service returned HTTP " .. tostring(status), "http_error")
		end
		value.ok = true
		return value
	end)
end

function Service.execute(zeron, connection, action, args)
	return http(zeron, connection, "POST", "/v1/execute", {
		project_id = connection and connection.project_id,
		action = action,
		args = args or {},
	}, 60)
end

function Service.events(zeron, connection, cursor, limit, wait_ms)
	local query = "?project=" .. tostring(connection and connection.project_id or "")
		.. "&cursor=" .. tostring(cursor or 0)
		.. "&limit=" .. tostring(limit or 100)
		.. "&wait_ms=" .. tostring(wait_ms or 0)
	return http(zeron, connection, "GET", "/v1/events" .. query, nil, (wait_ms or 0) / 1000 + 5)
end

function Service.close_project(zeron, connection)
	return http(zeron, connection, "POST", "/v1/projects/close", {
		project_id = connection and connection.project_id,
	}, 15)
end

return Service
