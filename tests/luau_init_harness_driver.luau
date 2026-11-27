-- Headless harness for the real plugin/init.luau. The pytest wrapper
-- (tests/test_luau_init_harness.py) generates tests/.harness/ with a copy of
-- init.luau (plus ui-prelude shims for every plugin module and ds component),
-- then runs: luau tests/luau_init_harness_driver.luau
--
-- The driver loads the plugin, builds the page twice (empty state, then after
-- a canned "Open notebook" flow), and fires every event handler, draw callback
-- and registered tool. Any runtime error fails the driver.

local runtime = require("./luau_harness_runtime")
local zeron_factory = require("./luau_harness_zeron")
local after_queue = runtime.after_queue
local failures = runtime.failures

local function fail(msg)
	table.insert(failures, msg)
	print("HARNESS-FAIL: " .. msg)
end
local function guard(label, fn, ...)
	local ok, err = pcall(fn, ...)
	if not ok then fail(label .. ": " .. tostring(err)) end
end

------------------------------------------------------------ load plugin ---
guard("require plugin/init", function() require("./.harness/init_entry") end)
local registry = zeron_factory.registry
if #registry.pages == 0 then
	fail("no page registered")
	print("INIT_HARNESS_FAILED (no page)")
	error("INIT_HARNESS_FAILED", 0)
end

----------------------------------------------------------------- helpers ---
local function eval_cond(cond)
	if type(cond) == "function" then
		local ok, v = pcall(cond)
		return ok and v or nil
	end
	if type(cond) == "table" and type(cond.get) == "function" then return cond:get() end
	return cond
end

local on_key_handler = nil
local function walk(node, visit)
	if type(node) ~= "table" then return end
	if node.__show then
		if eval_cond(node.cond) then walk(node.child, visit) end
		return
	end
	if node.__presence then
		local ok, watched = pcall(node.watch)
		if ok then
			local rok, rendered = pcall(node.render, watched)
			if rok then walk(rendered, visit) else fail("presence render: " .. tostring(rendered)) end
		end
		return
	end
	if node.__each then
		local list = node.list
		if type(list) == "table" and type(list.get) == "function" then list = list:get() end
		if type(list) == "table" then
			for i, item in list do
				local ok, child = pcall(node.fn, item, i)
				if ok then walk(child, visit) else fail("each callback: " .. tostring(child)) end
			end
		end
		return
	end
	visit(node)
	for _, child in node do if type(child) == "table" then walk(child, visit) end end
	for _, child in node.props or {} do walk(child, visit) end
end

local function fake_canvas()
	local mt
	mt = { __index = function(_t, key)
		if key == "measure_text" then return function(_self, _text) return { w = 10, h = 12 } end end
		return function(_self, ...) return nil end
	end }
	return setmetatable({}, mt)
end

local function fake_event(overrides)
	local e = { local_x = 10, local_y = 10, x = 10, y = 10, dx = 0, dy = 0, button = 0,
		key = "", cmd = false, meta = false, ctrl = false, shift = false, alt = false }
	for k, v in overrides or {} do e[k] = v end
	return e
end

local function collect_buttons(tree)
	local buttons = {}
	walk(tree, function(node)
		local props = node.props or {}
		if type(props.on_click) == "function" then
			table.insert(buttons, { label = tostring(props[1] or ""), fn = props.on_click, kind = node.__kind or "?" })
		end
	end)
	return buttons
end

local function exercise_tree(tree, label, skip_buttons)
	walk(tree, function(node)
		local props = node.props or {}
		local kind = node.__kind or "?"
		if type(props.draw) == "function" then
			guard(label .. " draw " .. kind, function() props.draw(fake_canvas(), 800, 600) end)
		end
		if type(props.on_mount) == "function" then
			guard(label .. " on_mount " .. kind, function() props.on_mount({ focus = function() end }) end)
		end
		if type(props.on_key) == "function" then
			on_key_handler = props.on_key
		end
		if not skip_buttons and type(props.on_click) == "function" then
			guard(label .. " on_click " .. kind, function() props.on_click() end)
		end
		if type(props.on_press) == "function" then
			guard(label .. " on_press " .. kind, function() props.on_press(fake_event()) end)
		end
		if type(props.on_release) == "function" then
			guard(label .. " on_release " .. kind, function() props.on_release(fake_event()) end)
		end
		if type(props.on_pointer_move) == "function" then
			guard(label .. " on_move " .. kind, function() props.on_pointer_move(fake_event({ dx = 3, dy = 4 })) end)
		end
		if type(props.on_wheel) == "function" then
			guard(label .. " on_wheel " .. kind, function() props.on_wheel(fake_event({ dy = -120 })) end)
		end
		if type(props.on_change) == "function" then
			guard(label .. " on_change " .. kind, function() props.on_change("test") end)
		end
	end)
end

local function drain_after(cap)
	local n = 0
	while #after_queue > 0 and n < (cap or 300) do
		n += 1
		local fn = table.remove(after_queue, 1)
		guard("ui.after #" .. tostring(n), fn)
	end
end

--------------------------------------------------------------- scenario ----
local page = registry.pages[1]
guard("page.build (empty)", function()
	exercise_tree(page.build({}), "empty")
end)
drain_after(50)

-- open the canned notebook through the real "Open notebook" handler chain
guard("open notebook flow", function()
	local tree = page.build({})
	walk(tree, function(node)
		local props = node.props or {}
		if type(props.on_click) == "function" and node.__kind == "button" then
			local label = tostring(props[1] or "")
			if string.find(label, "Open notebook", 1, true) then props.on_click() end
		end
	end)
end)
drain_after(200)

-- rebuild with populated state: click the Blocks buttons first (a later
-- MetaBlock click would hide that section mid-walk), then exercise the rest
guard("page.build (populated) blocks first", function()
	local buttons = collect_buttons(page.build({}))
	for _, button in buttons do
		if string.find(button.label, "Insert", 1, true) or string.find(button.label, "Add block", 1, true) then
			guard("blocks on_click " .. button.label, button.fn)
		end
	end
end)
drain_after(200)
guard("page.build (populated)", function()
	exercise_tree(page.build({}), "full", true)
	-- still click the remaining (non-Blocks) buttons
	for _, button in collect_buttons(page.build({})) do
		if not (string.find(button.label, "Insert", 1, true) or string.find(button.label, "Add block", 1, true)) then
			guard("full on_click " .. button.label, button.fn)
		end
	end
end)
drain_after(200)

-- keyboard shortcuts on the canvas key handler
if on_key_handler then
	local keys = {
		{ key = "z", cmd = true }, { key = "z", cmd = true, shift = true }, { key = "y", ctrl = true },
		{ key = "c", cmd = true }, { key = "v", meta = true }, { key = "a", cmd = true }, { key = "d", cmd = true },
		{ key = "delete" }, { key = "backspace" }, { key = "escape" }, { key = "r" }, { key = "o" },
		{ key = "arrowleft" }, { key = "arrowright", shift = true }, { key = "arrowup" }, { key = "arrowdown" },
		{ key = "+" }, { key = "-" }, { key = "0", cmd = true }, { key = "enter" },
	}
	for _, k in keys do
		guard("on_key " .. k.key, function() on_key_handler(fake_event(k)) end)
	end
else
	fail("no on_key handler found on the canvas")
end

-- registered tools must all run without crashing
for _, tool in registry.tools do
	guard("tool " .. tostring(tool.name), function()
		local result = tool.run({})
		if type(result) == "table" and type(result.after) == "function" then result:after(function() end, function() end) end
	end)
end

drain_after(100)

-- behavioural assertions: the canned "open notebook" flow must have driven the
-- service batch (groups/addons/…), and clicking the Blocks buttons must have
-- inserted + wired + run a pipeline through component.add/notebook.connect.
local run_log = table.concat(registry.run_log, "\n")
local function ran(fragment)
	return string.find(run_log, fragment, 1, true) ~= nil
end
if not ran('"operation":"batch"') then fail("service batch (Load Graphimo project) never ran — is_local_url gate broken?") end
if not ran('"action":"component.write"') then fail("Blocks 'Add block' never wrote a component file") end
if not ran('"action":"component.add"') then fail("pipeline insert never called component.add") end
if not ran('"action":"notebook.connect"') then fail("pipeline insert never wired edges with notebook.connect") end
if not ran('"action":"notebook.run"') then fail("pipeline insert never ran its steps") end

if #failures > 0 then
	print("INIT_HARNESS_FAILED (" .. #failures .. " failures)")
	error("INIT_HARNESS_FAILED", 0)
end
print("INIT_HARNESS_OK tools=" .. tostring(#registry.tools) .. " route=" .. (registry.opened_route and "opened" or "none"))
