-- Self-contained fake Zeron host for the headless init harness. Copied to
-- tests/.harness/zeron.luau by the pytest wrapper; init.luau calls
-- require("./zeron")("graphimo") and gets this factory. The driver reads
-- factory.registry for the pages and tools the plugin registered.

local runtime = require("./luau_harness_runtime")
local json = runtime.json
local make_state = runtime.make_state

local inspect_payload = {
	notebook = { path = "/tmp/lfw-demo/nb.py", session_id = "lfwprobe", url = "http://127.0.0.1:2721" },
	cells = {
		{ id = "cLib", source = "# Graphimo node library: ml/digits_torch.py:load_digits\nimport x\n", status = "idle", refs = {}, defs = { "__graphimo_c_abc_load_digits" } },
		{
			id = "c1",
			source = "# Graphimo node: load_digits\n# Graphimo component: ml/digits_torch.py\n# Graphimo instance: e144a289\ng_load_digits_e144a289 = f()",
			status = "idle", refs = {}, defs = { "g_load_digits_e144a289" },
			graphimo_instance = {
				instance_id = "e144a289", node = "load_digits", component_id = "ml/digits_torch.py",
				result_var = "g_load_digits_e144a289", bindings = {},
				inputs = {}, outputs = { { index = 0, name = "output_1" }, { index = 1, name = "output_2" } },
			},
		},
		{ id = "c2", source = "x = 1\nprint(x)", status = "running", refs = { "g_load_digits_e144a289" }, defs = { "x" }, errors = {} },
	},
	edges = {
		{ source_cell_id = "c1", source_variable = "g_load_digits_e144a289", output_index = 0, target_cell_id = "c2", input_ref = "x", variable = "g_load_digits_e144a289", status = "ok" },
	},
}

local components_payload = {
	items = {
		{
			id = "ml/digits_torch.py", category = { "ml" }, source_sha256 = "abc",
			callables = {
				{ name = "load_digits", signature = "load_digits()", base_block = { inputs = {}, outputs = { { index = 0, name = "output_1" }, { index = 1, name = "output_2" } }, output_mode = "tuple" } },
				{ name = "train_mlp", signature = "train_mlp(X, y)", base_block = { inputs = { { name = "X" }, { name = "y" } }, outputs = { { index = 0, name = "output_1" } }, output_mode = "tuple" } },
			},
		},
	},
}

local add_counter = 0
local function service_result(request_text)
	if string.find(request_text, '"operation":"batch"', 1, true) then
		return { operations = {
			{ id = "groups", status = "ok", result = { items = {} } },
			{ id = "controls", status = "ok", result = { items = {} } },
			{ id = "metablocks", status = "ok", result = { items = {} } },
			{ id = "addons", status = "ok", result = { addons = { { id = "graphimo_pytorch", capabilities = { "metablock" }, dialects = { "pytorch.model" } } } } },
			{ id = "scope", status = "ok", result = { breadcrumbs = { { scope_id = "root", dialect_id = "python", kind = "project", name = "root" } } } },
		} }
	end
	if string.find(request_text, '"action":"notebook.inspect"', 1, true) then return inspect_payload end
	if string.find(request_text, '"action":"component.list"', 1, true) then return components_payload end
	if string.find(request_text, '"action":"component.refresh_instances"', 1, true) then return { changed_cells = {} } end
	if string.find(request_text, '"action":"component.write"', 1, true) then return { valid = true, nodes = {} } end
	if string.find(request_text, '"action":"component.add"', 1, true) then
		add_counter += 1
		local id = string.format("ab%06d", add_counter)
		return {
			cell_id = "new" .. tostring(add_counter),
			instance_id = id,
			result_var = "g_node_" .. id,
			source = "# Graphimo node: node\n# Graphimo instance: " .. id .. "\ng_node_" .. id .. " = f()",
			library_cell_id = "lib" .. tostring(add_counter),
		}
	end
	if string.find(request_text, '"action":"notebook.connect"', 1, true) then
		return { ok = true, changed = true, cell_id = "new1", source = "# Graphimo node: node\n# Graphimo instance: ab000001\ng_node_ab000001 = f(X=g_node_ab000000[0],)" }
	end
	if string.find(request_text, '"action":"notebook.run"', 1, true) then return { ok = true } end
	if string.find(request_text, '"action":"notebook.delete"', 1, true) then return { ok = true } end
	if string.find(request_text, '"action":"notebook.restore_cells"', 1, true) then return { cell_ids = { "r1", "r2" } } end
	if string.find(request_text, '"action":"control.add"', 1, true) then
		return { control_cell_id = "cc1", value_cell_id = "vc1", control_source = "# Graphimo control: slider\n", value_source = "v = 1\n", label = "Slider", value_name = "v" }
	end
	if string.find(request_text, '"action":"metablock.create"', 1, true) then
		return { metablock_id = "mb1", dialect_id = "pytorch.model", state = { graph_revision = "rev1" } }
	end
	if string.find(request_text, '"action":"metablock.project"', 1, true) then
		return { metablock_id = "mb1", graph_revision = "rev1", name = "PyTorch Model", generated_source = "import torch\n", method_tabs = {}, groups = {}, palette = {}, external_parameters = {}, ui = nil }
	end
	if string.find(request_text, '"operation":"events"', 1, true) then return { events = {}, cursor = "1", dropped = 0 } end
	return {}
end

local run_log = {}
local function fake_run(program, args, opts)
	local stdin = opts and opts.stdin or ""
	table.insert(run_log, program .. " " .. table.concat(args or {}, " ") .. "|" .. string.sub(stdin, 1, 400))
	local stdout = ""
	if program == "python3" and args and args[1] == "-m" and args[2] == "graphimo.service.invoke" then
		local stdin = opts and opts.stdin or ""
		stdout = json.encode({ ok = true, result = service_result(stdin) }) .. "\n"
	elseif program == "python3" and args and args[1] == "-c" then
		stdout = "GRAPHIMO_LAUNCH:" .. json.encode({ url = "http://127.0.0.1:2721", session_id = "lfwprobe", started = true, pid = 4242, log_path = "/tmp/lfw-demo/marimo.log" }) .. "\n"
	elseif program == "curl" and args then
		local joined = table.concat(args, " ")
		if string.find(joined, "/api/sessions", 1, true) then
			stdout = json.encode({ lfwprobe = { session_id = "sess-1", path = "/tmp/lfw-demo/nb.py", filename = "/tmp/lfw-demo/nb.py" } }) .. "\n"
		end
	end
	local p = {}
	function p:after(s) s({ status = 0, stdout = stdout, stderr = "" }); return nil end
	return p
end

local factory = setmetatable({ registry = { pages = {}, tools = {}, opened_route = nil, run_log = run_log } }, {
	__call = function(self, _id)
		return {
			store = function(_key, def) return make_state(def) end,
			tool = function(t) table.insert(self.registry.tools, t) end,
			page = function(p) table.insert(self.registry.pages, p) end,
			run = fake_run,
			open = function(route) self.registry.opened_route = route end,
			open_url = function(_url) end,
			primary = function() return nil end,
			secondary = function() return nil end,
		}
	end,
})

return factory
