-- Self-contained Zeron `ui` runtime stub for the headless init harness.
-- Required modules in the Luau CLI get a pristine environment, so the pytest
-- wrapper prepends `local ui = require("../luau_harness_runtime").ui` to every
-- generated plugin module copy under tests/.harness/.

local M = { failures = {}, after_queue = {} }

local function fail(msg)
	table.insert(M.failures, msg)
	print("HARNESS-FAIL: " .. msg)
end
M.fail = fail

---------------------------------------------------------------------- JSON --
local json = {}
local function json_encode(value)
	local t = type(value)
	if value == nil then return "null" end
	if t == "number" or t == "boolean" then return tostring(value) end
	if t == "string" then
		return '"' .. string.gsub(string.gsub(string.gsub(value, "\\", "\\\\"), '"', '\\"'), "\n", "\\n") .. '"'
	end
	if t ~= "table" then error("json: unsupported " .. t) end
	if #value > 0 then
		local parts = {}
		for _, item in value do table.insert(parts, json_encode(item)) end
		return "[" .. table.concat(parts, ",") .. "]"
	end
	local parts = {}
	for key, item in value do
		if type(key) == "string" then table.insert(parts, json_encode(key) .. ":" .. json_encode(item)) end
	end
	table.sort(parts)
	return "{" .. table.concat(parts, ",") .. "}"
end
json.encode = json_encode

local function json_decode(text)
	local pos = 1
	local function skip_ws()
		local _, e = string.find(text, "^%s*", pos)
		if e then pos = e + 1 end
	end
	local function parse_value()
		skip_ws()
		local c = string.sub(text, pos, pos)
		if c == "{" then
			pos += 1
			local obj = {}
			skip_ws()
			if string.sub(text, pos, pos) == "}" then pos += 1; return obj end
			while true do
				skip_ws()
				local key = parse_value()
				skip_ws()
				assert(string.sub(text, pos, pos) == ":", "json: expected :")
				pos += 1
				obj[key] = parse_value()
				skip_ws()
				local d = string.sub(text, pos, pos)
				pos += 1
				if d == "}" then return obj end
				assert(d == ",", "json: expected , or }")
			end
		elseif c == "[" then
			pos += 1
			local arr = {}
			skip_ws()
			if string.sub(text, pos, pos) == "]" then pos += 1; return arr end
			while true do
				table.insert(arr, parse_value())
				skip_ws()
				local d = string.sub(text, pos, pos)
				pos += 1
				if d == "]" then return arr end
				assert(d == ",", "json: expected , or ]")
			end
		elseif c == '"' then
			pos += 1
			local out = {}
			while true do
				local ch = string.sub(text, pos, pos)
				if ch == "" then error("json: unterminated string") end
				if ch == '"' then pos += 1; return table.concat(out) end
				if ch == "\\" then
					local esc = string.sub(text, pos + 1, pos + 1)
					pos += 2
					if esc == "n" then table.insert(out, "\n")
					elseif esc == "t" then table.insert(out, "\t")
					elseif esc == "r" then table.insert(out, "\r")
					else table.insert(out, esc) end
				else
					table.insert(out, ch)
					pos += 1
				end
			end
		elseif string.sub(text, pos, pos + 3) == "true" then pos += 4; return true
		elseif string.sub(text, pos, pos + 4) == "false" then pos += 5; return false
		elseif string.sub(text, pos, pos + 3) == "null" then pos += 4; return nil
		else
			local num = string.match(text, "^-?%d+%.?%d*[eE]?[+-]?%d*", pos)
			assert(num, "json: bad value at " .. pos)
			pos += #num
			return tonumber(num)
		end
	end
	return parse_value()
end
json.decode = json_decode
M.json = json

------------------------------------------------------------------- state ---
local function make_state(v)
	local s = { v = v }
	function s:get() return s.v end
	function s:peek() return s.v end
	function s:set(nv) s.v = nv end
	return s
end
M.make_state = make_state

------------------------------------------------------------------ nodes ----
local function comp(kind)
	return function(props)
		return { __kind = kind, props = props or {} }
	end
end

local ui = {
	json = json,
	null = {},
	use_state = function(v) return make_state(v) end,
	state = function(v) return make_state(v) end,
	computed = function(fn)
		local s = make_state(nil)
		function s:get() return fn() end
		function s:peek() return fn() end
		return s
	end,
	ref = function()
		return { peek = function() return { focus = function() end, width = 800, height = 600 } end,
			get = function() return { focus = function() end, width = 800, height = 600 } end }
	end,
	show = function(cond, child) return { __show = true, cond = cond, child = child } end,
	each = function(list, fn, opts) return { __each = true, list = list, fn = fn, opts = opts } end,
	after = function(_ms, fn) table.insert(M.after_queue, fn) end,
	clipboard = { write = function(_v) return true end, read = function() return "" end },
	save_file = function(_opts)
		local p = {}
		function p:after(s) s({ path = "/tmp/lfw-demo/new_nb.py" }); return nil end
		return p
	end,
	write_file = function(_file, _source)
		local p = {}
		function p:after(s) s(true); return nil end
		return p
	end,
	open_files = function(_opts)
		local p = {}
		function p:after(s) s({ { path = "/tmp/lfw-demo/nb.py" } }); return nil end
		return p
	end,
	read_file = function(_path)
		local p = {}
		function p:after(s) s(""); return nil end
		return p
	end,
	invoke = function(_op, _args) end,
	call = function(_op, _args)
		local p = {}
		function p:after(s) s({}); return nil end
		return p
	end,
	call_sync = function(_op, _args) return {} end,
	shared = function(_key, def) return make_state(def) end,
	persist = function(_key, def) return make_state(def) end,
	presence = function(_key, def) return make_state(def) end,
	effect = function(fn) local ok, err = pcall(fn); if not ok then fail("ui.effect: " .. tostring(err)) end end,
	task = function(fn)
		local ok, err = pcall(fn)
		if not ok then fail("ui.task: " .. tostring(err)) end
		local p = {}
		function p:after(s) s({}); return nil end
		return p
	end,
	now = function() return 0 end,
	on_cleanup = function(_fn) end,
	use_context = function(_key) return nil end,
	component = function(fn, _name)
		return function(props) return fn(props) end
	end,
	add = function(_node) end,
	change = function(_sel, _node) end,
	slot = function(_name) return nil end,
	override = function(_a, _b) end,
	filter = function(list, _fn) return list end,
	every = function(list, _fn) return list end,
	image = comp("image"),
	column = comp("column"),
	row = comp("row"),
	div = comp("div"),
	text = comp("text"),
	svg = comp("svg"),
	canvas = comp("canvas"),
	path = function()
		local p = {}
		local mt = { __index = function(_t, _k) return function(self) return self end end }
		return setmetatable(p, mt)
	end,
}
M.ui = ui

return M
