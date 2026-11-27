-- Thin Zeron transport to Graphimo's authenticated local Python service.
-- All project actions are handled by GraphimoClient and GraphimoApplication.
local Service = {}

local function failure(message, code)
	return { ok = false, error = { code = code or "service_error", message = tostring(message) } }
end

local Future = {}
Future.__index = Future

local function future()
	return setmetatable({ state = "pending", handlers = {} }, Future)
end

local function immediate(value)
	local target = future()
	target.state, target.value = "resolved", value
	return target
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
			if not ok then settle(next_future, "rejected", result)
			elseif getmetatable(result) == Future then
				result:after(function(value) settle(next_future, "resolved", value) end, function(err) settle(next_future, "rejected", err) end)
			else
				settle(next_future, "resolved", result)
			end
		end
	end
	return next_future
end

local function wrap(native)
	local wrapped = future()
	if native == nil then
		settle(wrapped, "rejected", "Zeron did not start python3; check Graphimo's commands permission")
		return wrapped
	end
	local ok, after = pcall(function() return native.after end)
	if not ok or type(after) ~= "function" then
		settle(wrapped, "rejected", "Zeron returned a command without a request promise")
		return wrapped
	end
	local registered, err = pcall(function()
		after(native, function(value) settle(wrapped, "resolved", value) end, function(reason) settle(wrapped, "rejected", reason) end)
	end)
	if not registered then settle(wrapped, "rejected", err) end
	return wrapped
end

local function call(zeron, request, timeout)
	local ok, encoded = pcall(ui.json.encode, request)
	if not ok then return immediate(failure("Could not encode Graphimo service request: " .. tostring(encoded), "invalid_request")) end
	local native = zeron.run("python3", { "-m", "graphimo.service.invoke" }, { stdin = encoded, timeout = timeout or 20 })
	return wrap(native):after(function(response)
		local stdout = if type(response) == "table" and type(response.stdout) == "string" then response.stdout else ""
		local encoded_result = string.match(stdout, "([^\r\n]+)%s*$")
		local decoded_ok, decoded = pcall(ui.json.decode, encoded_result or "")
		if not decoded_ok or type(decoded) ~= "table" then
			local stderr = if type(response) == "table" and type(response.stderr) == "string" then response.stderr else ""
			local detail = if stderr ~= "" then stderr else stdout
			return failure("Graphimo service adapter returned invalid JSON: " .. string.sub(detail, 1, 4000), "service_unavailable")
		end
		if decoded.ok == false then return decoded end
		return decoded.result
	end)
end

function Service.execute(zeron, project_path, action, args)
	return call(zeron, { path = project_path, operation = "execute", action = action, args = args or {} }, 30)
end

function Service.batch(zeron, project_path, operations, continue_on_error)
	return call(zeron, {
		path = project_path,
		operation = "batch",
		operations = operations,
		continue_on_error = continue_on_error == true,
	}, 45)
end

function Service.events(zeron, project_path, cursor, limit, wait_ms)
	return call(zeron, {
		path = project_path,
		operation = "events",
		cursor = cursor,
		limit = limit or 128,
		wait_ms = wait_ms or 750,
	}, 15)
end

function Service.telemetry_config(zeron, project_path, marimo_url, marimo_token, session_id)
	return call(zeron, {
		path = project_path,
		operation = "telemetry_config",
		marimo_url = marimo_url,
		marimo_token = marimo_token,
		session_id = session_id,
	}, 15)
end

return Service
