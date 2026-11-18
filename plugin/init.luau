local zeron = require("./zeron")("graphimo")
local Bridge = require("./marimo_bridge")
local Layout = require("./layout")
local Canvas = require("./canvas")
local BaseBlock = require("./base_block")
local Library = require("./component_library")
local Text = require("@ds/text")
local Button = require("@ds/button")
local Glass = require("@ds/glass")
local Markdown = require("@ds/markdown")
local SearchField = require("@ds/search_field")
local Tooltip = require("@ds/tooltip")
local CodeEditor = require("@ds/code_editor")
local ContextMenu = require("@ds/context_menu")
local Dialog = require("@ds/dialog").Dialog
local TextField = require("@ds/text_field")
local TextArea = require("@ds/text_area")
local PyTorchMeta = require("./pytorch_meta")
local PyTorchMetaEditor = require("./pytorch_meta_editor")

local active_notebook = zeron.store("active-notebook-v1", { url = "", session_id = "", path = "", log_path = "" })
local saved_logs = zeron.store("logs-v1", {})
local saved_problems = zeron.store("problems-v1", {})

local function config_for(args)
	local saved = active_notebook:get()
	return args.url or saved.url or "", "", args.session_id or saved.session_id or ""
end

local function tool_result(promise)
	if promise == nil then
		return { ok = false, error = { code = "command_unavailable", message = "Zeron did not start the request. Check that Graphimo is allowed to run curl and python3 in Settings → Plugins." } }
	end
	return promise:after(function(value)
		if type(value) ~= "table" then return { ok = true, result = value } end
		if value.ok == false then
			local detail = value.error
			if type(detail) == "table" then return value end
			local message = tostring(detail or "Operation failed")
			local code = "operation_failed"
			if string.sub(message, 1, 12) == "unsafe_edit:" then
				code = "unsafe_edit"
				message = string.gsub(string.sub(message, 13), "^%s+", "")
			end
			return { ok = false, error = { code = code, message = message, cell_id = value.cell_id } }
		end
		local result = table.clone(value)
		result.ok = true
		return result
	end, function(err)
		local message = tostring(err)
		local code = "operation_failed"
		if string.sub(message, 1, 12) == "unsafe_edit:" then
			code = "unsafe_edit"
			message = string.gsub(string.sub(message, 13), "^%s+", "")
		end
		return { ok = false, error = { code = code, message = message } }
	end)
end

local function start_tool(request)
	local ok, promise = pcall(request)
	if not ok then
		local message = tostring(promise)
		local code = if string.find(message, "commands permission", 1, true) then "command_unavailable" else "operation_failed"
		return { ok = false, error = { code = code, message = message } }
	end
	return tool_result(promise)
end

local function is_local_url(value)
	return string.match(value or "", "^https?://localhost([:/]|$)") ~= nil
		or string.match(value or "", "^https?://127%.0%.0%.1([:/]|$)") ~= nil
		or string.match(value or "", "^https?://%[::1%]([:/]|$)") ~= nil
end

zeron.tool({ name = "inspect", run = function(args)
	local url, token, session = config_for(args or {})
	return start_tool(function() return Bridge.inspect(zeron, url, token, session) end)
end })

zeron.tool({ name = "list_components", run = function(args)
	local url, token, session = config_for(args or {})
	return start_tool(function() return Bridge.list_components_for_active(zeron, url, token, session) end)
end })

zeron.tool({ name = "add_component", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	return start_tool(function() return Bridge.add_component(zeron, url, token, session, args.component_id, args.callable, args.output_name, args.bindings) end)
end })

zeron.tool({ name = "connect", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	return start_tool(function() return Bridge.connect(zeron, url, token, session, args.from_cell_id, args.variable, args.to_cell_id, args.input_ref) end)
end })

zeron.tool({ name = "save_group", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	local saved = active_notebook:get()
	return start_tool(function()
		return Bridge.save_group(zeron, url, token, session, saved.path or "", args.cell_ids or {}, args.positions or {}, args.name or "Group", args.category or "general")
	end)
end })

zeron.tool({ name = "add_group", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	local saved = active_notebook:get()
	return start_tool(function()
		return Bridge.add_group(zeron, url, token, session, saved.path or "", args.group_id, args.output_prefix or "group")
	end)
end })

zeron.tool({ name = "create_pytorch_meta", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	local saved = active_notebook:get()
	return start_tool(function()
		return Bridge.create_pytorch_meta(zeron, url, token, session, saved.path or "", args.meta_id, args.name or "PyTorch model", args.category or "models")
	end)
end })

zeron.tool({ name = "get_pytorch_meta", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	local saved = active_notebook:get()
	return start_tool(function() return Bridge.get_pytorch_meta(zeron, url, token, session, saved.path or "", args.meta_id) end)
end })

zeron.tool({ name = "save_pytorch_meta", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	local saved = active_notebook:get()
	return start_tool(function() return Bridge.save_pytorch_meta(zeron, url, token, session, saved.path or "", args.meta) end)
end })

zeron.tool({ name = "add_pytorch_meta", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	local saved = active_notebook:get()
	return start_tool(function() return Bridge.add_pytorch_meta(zeron, url, token, session, saved.path or "", args.meta_id, args.output_name or "model") end)
end })

zeron.tool({ name = "run", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	return start_tool(function() return Bridge.run(zeron, url, token, session, args.cell_id) end)
end })

zeron.tool({ name = "create_block", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	return start_tool(function() return Bridge.add_cell(zeron, url, token, session, args.source or "pass") end)
end })

zeron.tool({ name = "edit_block", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	return start_tool(function() return Bridge.edit(zeron, url, token, session, args.cell_id, args.source) end)
end })

zeron.tool({ name = "delete_block", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	return start_tool(function() return Bridge.delete(zeron, url, token, session, args.cell_id) end)
end })

zeron.tool({ name = "open_code", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	return start_tool(function()
		return Bridge.inspect(zeron, url, token, session):after(function(value)
			for _, cell in value.cells or {} do
				if cell.id == args.cell_id then return { ok = true, cell_id = cell.id, source = cell.source } end
			end
			return { ok = false, error = { code = "not_found", message = "Cell not found" } }
		end)
	end)
end })

local Page = ui.component(function()
	local saved = active_notebook:peek()
	local url = ui.use_state(saved.url or "")
	local token = ui.use_state("")
	local session_id = ui.use_state(saved.session_id or "")
	local sessions = ui.use_state({})
	local snapshot = ui.use_state({ notebook = {}, cells = {}, edges = {} })
	local visible_cell_ids = ui.use_state({})
	local log_entries = ui.use_state(saved_logs:peek() or {})
	local problem_entries = ui.use_state(saved_problems:peek() or {})
	local server_log_text = ui.use_state("")
	local problem_log_text = ui.use_state("")
	local logs_open = ui.use_state(false)
	local terminal_mode = ui.use_state("terminal")
	local components_open = ui.use_state(true)
	local code_open = ui.use_state(false)
	local code_cell_id = ui.use_state("")
	local code_text = ui.use_state("")
	local library_query = ui.use_state("")
	local expanded_component = ui.use_state("")
	local positions = ui.use_state({})
	local components = ui.use_state({})
	local groups = ui.use_state({})
	local metas = ui.use_state({})
	local meta_create_dialog = ui.use_state(false)
	local meta_create_name = ui.use_state("PyTorch model")
	local meta_create_category = ui.use_state("models")
	local meta_create_x = ui.use_state(120)
	local meta_create_y = ui.use_state(120)
	local meta_editor_open = ui.use_state(false)
	local meta_editor_id = ui.use_state("")
	local meta_editor_data = ui.use_state({})
	local meta_selected = ui.use_state("")
	local meta_params_text = ui.use_state("{}")
	local meta_dirty = ui.use_state(false)
	local meta_pending = ui.use_state(nil)
	local meta_node_counter = ui.use_state(10)
	local group_dialog = ui.use_state(false)
	local group_name = ui.use_state("New group")
	local group_category = ui.use_state("general")
	local component_id = ui.use_state("")
	local callable_name = ui.use_state("")
	local selected = ui.use_state("")
	local selected_many = ui.use_state({})
	local zoom = ui.use_state(0.85)
	local pan_x = ui.use_state(24)
	local pan_y = ui.use_state(24)
	local grid = ui.use_state(true)
	local output_counter = ui.use_state(1)
	local notice = ui.use_state("")
	local canvas = ui.ref()
	local drag = nil

	local function cells_now()
		return snapshot:get().cells or {}
	end

	local base_blocks = ui.computed(function()
		local blocks = BaseBlock.by_cell(components:get(), cells_now())
		for _, cell in cells_now() do
			if cell.graphimo_kind == "display" then
				blocks[cell.id] = { cell_id = cell.id, callable = "Display", kind = "display", inputs = { { name = "value", port_key = "value", annotation = "Any" } }, outputs = {} }
			elseif cell.graphimo_kind == "annotation" then
				blocks[cell.id] = { cell_id = cell.id, callable = "Annotation", kind = "annotation", inputs = {}, outputs = {} }
			elseif cell.graphimo_kind == "group" and type(cell.graphimo_group) == "table" then
				local ref, inputs, outputs = cell.graphimo_group, {}, {}
				for name, variable in ref.inputs or {} do
					table.insert(inputs, { name = name, port_key = name, annotation = "Any", binding = { kind = "variable", variable = variable } })
				end
				for name, variable in ref.outputs or {} do
					table.insert(outputs, { name = name, port_key = name, annotation = "Any", expression = name, variable = variable, runtime = cell.values and cell.values[variable] or nil })
				end
				table.sort(inputs, function(a, b) return a.name < b.name end)
				table.sort(outputs, function(a, b) return a.name < b.name end)
				local shown = string.match(ref.group_id or "Group", "([^/]+)$") or "Group"
				blocks[cell.id] = { cell_id = cell.id, callable = shown, kind = "group", group_id = ref.group_id, inputs = inputs, outputs = outputs }
			elseif cell.graphimo_kind == "meta" and type(cell.graphimo_meta) == "table" then
				local ref = cell.graphimo_meta
				local shown = string.match(ref.meta_id or "PyTorch model", "([^/]+)$") or "PyTorch model"
				for _, meta in metas:get() do if meta.id == ref.meta_id then shown = meta.name or shown; break end end
				blocks[cell.id] = {
					cell_id = cell.id,
					callable = shown,
					kind = "meta",
					meta_kind = ref.kind,
					meta_id = ref.meta_id,
					inputs = {},
					outputs = { { name = "model", port_key = "model", annotation = "torch.nn.Module", expression = shown, variable = ref.output, runtime = cell.values and cell.values[ref.output] or nil } },
				}
			end
		end
		return blocks
	end)

	local function log(message, level)
		level = level or "info"
		local entry = tostring(os.time()) .. " [" .. string.upper(level) .. "] " .. tostring(message)
		local entries = table.clone(log_entries:peek())
		table.insert(entries, entry)
		while #entries > 1200 do table.remove(entries, 1) end
		log_entries:set(entries)
		saved_logs:set(entries)
		if level == "error" then
			local problems = table.clone(problem_entries:peek())
			table.insert(problems, entry)
			while #problems > 3000 do table.remove(problems, 1) end
			problem_entries:set(problems)
			saved_problems:set(problems)
			local ok, saved = pcall(Bridge.append_problem, zeron, entry)
			if ok and saved then
				saved:after(nil, function(err) print("[Graphimo] Could not append problem log: " .. tostring(err)) end)
			else
				print("[Graphimo] Could not start persistent problem-log write: " .. tostring(saved))
			end
		end
		print("[Graphimo] " .. entry)
	end

	local function observe(label, start, on_success, on_failure)
		local function report_failure(err)
			local detail = err
			if type(err) == "table" and type(err.error) == "table" then detail = err.error.message or err.error.code or "Marimo operation failed" end
			local message = label .. " failed: " .. tostring(detail)
			log(message, "error")
			if on_failure then pcall(on_failure, detail) end
		end
		local ok, promise = pcall(start)
		if not ok then
			local message = label .. " failed to start: " .. tostring(promise)
			log(message, "error")
			if on_failure then on_failure(promise) end
			return
		end
		local valid, after = pcall(function() return promise and promise.after end)
		if not valid or type(after) ~= "function" then
			local message = label .. " failed: Zeron returned no request promise; check Graphimo's curl/python3 permissions"
			log(message, "error")
			if on_failure then on_failure(message) end
			return
		end
		log(label .. " started")
		promise:after(function(value)
			if type(value) == "table" and value.ok == false then
				report_failure(value)
				return
			end
			log(label .. " completed")
			if on_success then
				local success, err = pcall(on_success, value)
				if not success then report_failure(err) end
			end
		end, function(err)
			report_failure(err)
		end)
	end

	local function positioned_cells()
		local positioned = {}
		local saved_positions = positions:get()
		for _, cell in cells_now() do
			local view = table.clone(cell)
			local position = saved_positions[cell.id] or { x = 0, y = 0 }
			view.x, view.y = position.x, position.y
			view.width, view.height = position.width, position.height
			table.insert(positioned, view)
		end
		return positioned
	end

	local refresh_graph
	local function refresh_sessions()
		if url:peek() == "" then
			notice:set("Open or create a notebook to begin")
			return
		end
		observe("List marimo sessions", function()
			return Bridge.sessions(zeron, url:peek(), token:peek())
		end, function(items)
			sessions:set(items)
			if #items == 1 and session_id:peek() == "" then session_id:set(items[1].session_id) end
			if session_id:peek() ~= "" then
				local exists = false
				for _, item in items do if item.session_id == session_id:peek() then exists = true end end
				if not exists then session_id:set("") end
			end
			if session_id:peek() == "" then
				snapshot:set({ notebook = {}, cells = {}, edges = {} })
				visible_cell_ids:set({})
				positions:set({}); components:set({}); groups:set({}); metas:set({}); selected:set(""); selected_many:set({})
			end
			notice:set(if #items == 0 then "No active marimo notebooks found" else "Found " .. tostring(#items) .. " active notebook(s)")
			if session_id:peek() ~= "" then refresh_graph() end
		end, function(err) notice:set("Marimo connection failed: " .. tostring(err)) end)
	end

	refresh_graph = function()
		observe("Inspect marimo graph", function()
			return Bridge.inspect(zeron, url:peek(), token:peek(), session_id:peek())
		end, function(value)
			snapshot:set(value)
			local ids = {}
			for _, cell in value.cells or {} do table.insert(ids, cell.id) end
			visible_cell_ids:set(ids)
			if value.notebook.session_id then session_id:set(value.notebook.session_id) end
			local path = value.notebook.path or ""
			local function apply_layout(saved_positions)
				local merged, active = {}, {}
				for index, cell in value.cells do
					active[cell.id] = true
					local saved_cell = saved_positions[cell.id] or {}
					merged[cell.id] = {
						x = saved_cell.x or (80 + ((index - 1) % 4) * 320),
						y = saved_cell.y or (80 + math.floor((index - 1) / 4) * 230),
						collapsed = saved_cell.collapsed or false,
					}
				end
				positions:set(merged)
				if selected:peek() ~= "" and not active[selected:peek()] then selected:set("") end
				notice:set("Showing " .. tostring(#value.cells) .. " marimo cells")
				observe("Discover components", function()
					return Bridge.list_components(zeron, url:peek(), token:peek(), value.notebook.session_id, path)
				end, function(component_result)
					local available = component_result.components or {}
					components:set(available)
					groups:set(component_result.groups or {})
					metas:set(component_result.metas or {})
					observe("Check component sources", function()
						return Bridge.refresh_component_hashes(zeron, url:peek(), token:peek(), value.notebook.session_id, available)
					end, function(hash_result)
						if #(hash_result.changed_cells or {}) > 0 then
							notice:set("A component source changed; marimo marked its cell stale")
							ui.after(300, refresh_graph)
						end
					end, function(err) notice:set("Component freshness check failed: " .. tostring(err)) end)
				end, function(err) notice:set("Component discovery failed: " .. tostring(err)) end)
			end
			if is_local_url(url:peek()) then
				Layout.load(zeron, path, function(saved_positions, err)
					if err then log("Load canvas layout failed: " .. tostring(err)) end
					apply_layout(saved_positions)
				end)
			else
				apply_layout({})
			end
		end, function(err) notice:set("Could not inspect marimo: " .. tostring(err)) end)
	end

	local function save_positions()
		local path = snapshot:peek().notebook.path
		if not path or path == "" or not is_local_url(url:peek()) then return end
		Layout.save(zeron, path, positions:peek(), function(ok, err)
			if ok then log("Canvas layout saved") else
				local message = "Layout save failed: " .. tostring(err)
				log(message)
				notice:set(message)
			end
		end)
	end

	local function select_session(value)
		session_id:set(value)
		local next_saved = table.clone(active_notebook:peek())
		next_saved.session_id = value
		active_notebook:set(next_saved)
		if value ~= "" then refresh_graph() end
	end

	local function wait_for_notebook(path, base_url, attempt)
		attempt = attempt or 0
		if attempt >= 40 then
			local message = "Marimo editor opened, but its notebook session did not become active. See Logs for the marimo process output."
			log(message, "error")
			notice:set(message)
			return
		end
		Bridge.sessions(zeron, base_url, ""):after(function(items)
			if type(items) == "table" and items.ok == false then
				if attempt == 39 then log("Could not read marimo sessions: " .. tostring(items.error.message), "error") end
				ui.after(750, function() wait_for_notebook(path, base_url, attempt + 1) end)
				return
			end
			for _, item in items or {} do
				local same_path = item.path == path
				if not same_path and item.path ~= "" then
					local a = string.gsub(item.path, "\\\\", "/")
					local b = string.gsub(path, "\\\\", "/")
					same_path = string.gsub(a, "/+$", "") == string.gsub(b, "/+$", "")
				end
				if same_path then
					sessions:set(items)
					session_id:set(item.session_id)
					local current = table.clone(active_notebook:peek())
					current.url, current.path, current.session_id = base_url, path, item.session_id
					active_notebook:set(current)
					log("Notebook session connected: " .. path)
					refresh_graph()
					return
				end
			end
			ui.after(750, function() wait_for_notebook(path, base_url, attempt + 1) end)
		end, function(err)
			if attempt == 39 then log("Could not connect to marimo sessions: " .. tostring(err), "error") end
			ui.after(750, function() wait_for_notebook(path, base_url, attempt + 1) end)
		end)
	end

	local function start_notebook(path)
		notice:set("Starting marimo for " .. path .. "…")
		log("Opening notebook " .. path)
		observe("Start marimo", function()
			return Bridge.open_notebook(zeron, path)
		end, function(started)
			url:set(started.url)
			session_id:set(started.session_id or "")
			local current = { url = started.url, session_id = started.session_id or "", path = path, log_path = started.log_path or "" }
			active_notebook:set(current)
			if started.started then log("Started marimo process " .. tostring(started.pid)) else log("Reused existing marimo process") end
			if started.session_id and started.session_id ~= "" then
				refresh_graph()
			else
				zeron.open_url(started.url)
				log("Opened marimo editor; waiting for its live notebook session")
				wait_for_notebook(path, started.url, 0)
			end
		end, function(err)
			local message = "Could not start marimo: " .. tostring(err)
			notice:set(message)
			log(message, "error")
		end)
	end

	local function choose_notebook()
		ui.open_files({ title = "Open marimo notebook", filters = { { "Python notebook", { "py" } } } }):after(function(files)
			local file = files and files[1]
			if file and type(file.path) == "string" then start_notebook(file.path) end
		end, function(err)
			local message = "Could not choose notebook: " .. tostring(err)
			notice:set(message); log(message, "error")
		end)
	end

	local function create_notebook()
		ui.save_file({ title = "Create marimo notebook", name = "notebook.py", filters = { { "Python notebook", { "py" } } } }):after(function(file)
			if not file then return end
			local source = "import marimo\n\n__generated_with = \"0.25.2\"\napp = marimo.App()\n\n\nif __name__ == \"__main__\":\n    app.run()\n"
			ui.write_file(file, source):after(function()
				if type(file.path) == "string" then start_notebook(file.path) end
			end, function(err)
				local message = "Could not create notebook: " .. tostring(err)
				notice:set(message); log(message, "error")
			end)
		end, function(err)
			local message = "Could not choose notebook location: " .. tostring(err)
			notice:set(message); log(message, "error")
		end)
	end

	local function refresh_marimo_log()
		local path = active_notebook:peek().log_path
		local function read_process_log()
			if not path or path == "" then server_log_text:set("No managed marimo process log yet."); return end
			observe("Read marimo process log", function()
				return Bridge.read_marimo_log(zeron, path)
			end, function(result)
				server_log_text:set(result.text or "")
			end, function(err)
				server_log_text:set("Could not read marimo log: " .. tostring(err))
			end)
		end
		observe("Read persistent problem log", function()
			return Bridge.read_problem_log(zeron)
		end, function(result)
			problem_log_text:set(result.text or "")
			read_process_log()
		end, function(err)
			problem_log_text:set("Could not read problem log: " .. tostring(err))
			read_process_log()
		end)
	end

	local function export_logs()
		local body = "Graphimo problems\n\n" .. table.concat(problem_entries:peek(), "\n") .. "\n\nPersistent problem log\n\n" .. problem_log_text:peek() .. "\n\nRecent Graphimo events\n\n" .. table.concat(log_entries:peek(), "\n") .. "\n\nMarimo process output\n\n" .. server_log_text:peek()
		ui.save_file({ title = "Export Graphimo logs", name = "graphimo-logs.txt", filters = { { "Text log", { "txt" } } } }):after(function(file)
			if file then
				ui.write_file(file, body):after(function() log("Exported Graphimo logs") end, function(err) log("Could not export logs: " .. tostring(err), "error") end)
			end
		end, function(err) log("Could not choose log export location: " .. tostring(err), "error") end)
	end

	local function truncate(text, limit)
		text = tostring(text or "")
		if #text <= limit then return text end
		return string.sub(text, 1, math.max(1, limit - 1)) .. "…"
	end

	local function card(id)
		local function cell() return Canvas.node(snapshot:get().cells or {}, id) end
		local function block() return base_blocks:get()[id] end
		local style = Glass.style({
			absolute = true,
			left = function() local p = positions:get()[id] or {}; return pan_x:get() + (p.x or 0) * zoom:get() end,
			top = function() local p = positions:get()[id] or {}; return pan_y:get() + (p.y or 0) * zoom:get() end,
			w = function()
				local p = positions:get()[id] or {}
				return Canvas.width_of(p) * zoom:get()
			end,
			h = function()
				local value, p = cell(), positions:get()[id] or {}
				return Canvas.height(value or {}, block(), p) * zoom:get()
			end,
			rounded = "$radius.lg",
			pointer_events = "none",
		}, "island")
		style.border_color = function()
			local many = selected_many:get()
			return if selected:get() == id or many[id] then "$accent" else "$border.strong"
		end
		style.bg = "$zeron.secondary.bg"
		table.insert(style, ui.column { w_full = true, h_full = true, gap = "$space.2", p = "$space.3",
			ui.row { w_full = true, items = "center", gap = "$space.2",
				Text { function()
					local b, value = block(), cell()
					if b and b.callable then return b.callable end
					return if value and value.name then value.name else "Cell " .. string.sub(id, 1, 7)
				end, style = "label", grow = 1 },
				Text { function() local value = cell(); return value and value.status or "unknown" end, style = "caption",
					tone = function() local value = cell(); return if value and (value.status == "success" or value.status == "idle") then "success" elseif value and (value.status == "error" or value.status == "exception" or value.status == "marimo-error") then "danger" else "secondary" end },
			},
			ui.show(function() return block() == nil end,
				Text { function() local value = cell(); return truncate(string.match(value and value.source or "", "^[^\n]*") or "Empty cell", 52) end, style = "caption", tone = "secondary" }),
			ui.row { w_full = true, grow = 1, items = "start", gap = "$space.2",
				ui.column { grow = 1, basis = 0, gap = "$space.2",
					ui.each(function()
						local b, value = block(), cell()
						if b then return b.inputs or {} end
						local generic = {}
						for _, name in value and value.refs or {} do table.insert(generic, { name = name, port_key = name }) end
						return generic
					end, function(input)
						return ui.row {
							w_full = true, h = 23, items = "center", gap = "$space.1",
							tooltip = Tooltip { function()
								local b = block()
								return if b then BaseBlock.input_text(input).full else tostring(input.name)
							end },
							Text { "●", style = "caption", tone = "secondary" },
							Text { function()
								local b = block()
								return truncate(if b then BaseBlock.input_text(input).short else tostring(input.name), 25)
							end, style = "caption", tone = "secondary" },
						}
					end, { key = function(input) return tostring(input.port_key or input.name) end }),
				},
				ui.column { grow = 1, basis = 0, gap = "$space.2", items = "end",
					ui.each(function()
						local b, value = block(), cell()
						if b then return b.outputs or {} end
						local generic = {}
						for _, name in value and value.defs or {} do
							local runtime = value.values and value.values[name] or nil
							table.insert(generic, { name = name, port_key = name, variable = name, runtime = runtime })
						end
						return generic
					end, function(output)
						return ui.row {
							w_full = true, h = 23, items = "center", justify = "end", gap = "$space.1",
							tooltip = Tooltip { function()
								local b = block()
								if b then return BaseBlock.output_text(output).full end
								local runtime = output.runtime
								return tostring((runtime and runtime.type) or "Any") .. " (" .. tostring(output.name) .. ")"
							end },
							Text { function()
								local b = block()
								if b then return truncate(BaseBlock.output_text(output).short, 25) end
								local runtime = output.runtime
								return truncate(tostring((runtime and runtime.type) or "Any") .. " (" .. tostring(output.name) .. ")", 25)
							end, style = "caption", tone = "primary" },
							Text { "●", style = "caption", tone = "primary" },
						}
					end, { key = function(output) return tostring(output.port_key or output.variable or output.name) end }),
				},
			},
			-- Function blocks expose values through ports. Display/annotation blocks
			-- intentionally render marimo's main rich output inside the card.
			ui.show(function()
				local value, b = cell(), block()
				return value and type(value.output) == "table" and value.output.image_data_url ~= nil and (b == nil or b.kind == "display" or b.kind == "annotation")
			end, ui.image { src = function() local value = cell(); return value and value.output and value.output.image_data_url or "" end, w_full = true, h = 74, fit = "contain" }),
			ui.show(function()
				local value, b = cell(), block()
				return value and type(value.output) == "table" and value.output.mimetype == "text/markdown" and (b == nil or b.kind == "display" or b.kind == "annotation")
			end, Markdown { text = function() local value = cell(); return value and value.output and value.output.preview or "" end }),
			ui.show(function()
				local value, b = cell(), block()
				return value and type(value.output) == "table" and not value.output.image_data_url and value.output.mimetype ~= "text/markdown" and (b and b.kind == "display")
			end, Text { function() local value = cell(); return truncate(value and value.output and value.output.preview or "", 180) end, style = "caption", tone = "secondary", wrap = true }),
		})
		return ui.div(style)
	end

	local function zoom_at(next_zoom, x, y)
		local old = zoom:peek()
		next_zoom = math.clamp(next_zoom, 0.35, 2)
		if old == next_zoom then return end
		pan_x:set(x - (x - pan_x:peek()) * next_zoom / old)
		pan_y:set(y - (y - pan_y:peek()) * next_zoom / old)
		zoom:set(next_zoom)
	end

	local function fit()
		local ids = cell_ids()
		if #ids == 0 then zoom:set(0.85); pan_x:set(24); pan_y:set(24); return end
		local ps = positions:peek()
		local left, top, right, bottom = math.huge, math.huge, -math.huge, -math.huge
		for _, cell in snapshot:peek().cells do
			local p = ps[cell.id] or { x = 0, y = 0 }
			left = math.min(left, p.x); top = math.min(top, p.y)
			right = math.max(right, p.x + Canvas.width_of(p)); bottom = math.max(bottom, p.y + Canvas.height(cell, base_blocks:peek()[cell.id], p))
		end
		local w, h = canvas.width or 800, canvas.height or 600
		local scale = math.min((w - 64) / math.max(1, right - left), (h - 64) / math.max(1, bottom - top), 1.3)
		scale = math.clamp(scale, 0.35, 1.3)
		zoom:set(scale); pan_x:set((w - (right - left) * scale) / 2 - left * scale); pan_y:set((h - (bottom - top) * scale) / 2 - top * scale)
	end

	local function selected_set()
		local many = table.clone(selected_many:peek())
		if selected:peek() ~= "" then many[selected:peek()] = true end
		return many
	end

	local function select_one(id, additive)
		if additive then
			local many = table.clone(selected_many:peek())
			many[id] = if many[id] then nil else true
			selected_many:set(many)
			selected:set(id)
		else
			selected_many:set({})
			selected:set(id)
		end
	end

	local function on_press(e)
		local x, y = Canvas.point(e, zoom:peek(), pan_x:peek(), pan_y:peek())
		local current_cells = snapshot:peek().cells or {}
		local ps, blocks = positions:peek(), base_blocks:peek()
		for index = #current_cells, 1, -1 do
			local cell = current_cells[index]
			local pos, block = ps[cell.id] or { x = 0, y = 0 }, blocks[cell.id]
			local width, height = Canvas.width_of(pos), Canvas.height(cell, block, pos)

			-- Bottom-right resize handle. It is deliberately part of the canvas
			-- hit-testing layer so cards remain visually passive.
			if Canvas.near(x, y, pos.x + width, pos.y + height, 16 / zoom:peek()) then
				select_one(cell.id, e.shift == true)
				drag = { kind = "resize", id = cell.id, x = x, y = y, width = width, height = height }
				return
			end

			if block then
				for _, output in block.outputs or {} do
					if output.variable and Canvas.near(x, y, pos.x + width, Canvas.port(cell, output.port_key, true, block), 12 / zoom:peek()) then
						select_one(cell.id, e.shift == true)
						drag = { kind = "wire", cell_id = cell.id, variable = output.variable, port_key = output.port_key }
						return
					end
				end
				for _, input in block.inputs or {} do
					if Canvas.near(x, y, pos.x, Canvas.port(cell, input.port_key, false, block), 12 / zoom:peek()) then
						select_one(cell.id, e.shift == true); return
					end
				end
			else
				for _, variable in cell.defs or {} do
					if Canvas.near(x, y, pos.x + width, Canvas.port(cell, variable, true, nil), 12 / zoom:peek()) then
						select_one(cell.id, e.shift == true); drag = { kind = "wire", cell_id = cell.id, variable = variable }; return
					end
				end
				for _, variable in cell.refs or {} do
					if Canvas.near(x, y, pos.x, Canvas.port(cell, variable, false, nil), 12 / zoom:peek()) then select_one(cell.id, e.shift == true); return end
				end
			end

			if x >= pos.x and x <= pos.x + width and y >= pos.y and y <= pos.y + height then
				local additive = e.shift == true
				select_one(cell.id, additive)
				local moving = selected_set()
				moving[cell.id] = true
				local origins = {}
				for move_id in moving do
					local origin = ps[move_id]
					if origin then origins[move_id] = { x = origin.x, y = origin.y, width = origin.width, height = origin.height } end
				end
				drag = { kind = "node", id = cell.id, x = x, y = y, origins = origins }
				return
			end
		end
		if not e.shift then selected:set(""); selected_many:set({}) end
		drag = { kind = "pan", x = e.local_x or 0, y = e.local_y or 0, px = pan_x:peek(), py = pan_y:peek() }
	end

	local function on_move(e)
		if not drag then return end
		if drag.kind == "pan" then
			pan_x:set(drag.px + (e.local_x or 0) - drag.x); pan_y:set(drag.py + (e.local_y or 0) - drag.y)
		elseif drag.kind == "node" then
			local x, y = Canvas.point(e, zoom:peek(), pan_x:peek(), pan_y:peek())
			local dx, dy = x - drag.x, y - drag.y
			local next = table.clone(positions:peek())
			for id, origin in drag.origins do
				local current = table.clone(next[id] or {})
				current.x, current.y = origin.x + dx, origin.y + dy
				current.width, current.height = origin.width, origin.height
				next[id] = current
			end
			positions:set(next)
		elseif drag.kind == "resize" then
			local x, y = Canvas.point(e, zoom:peek(), pan_x:peek(), pan_y:peek())
			local next = table.clone(positions:peek())
			local current = table.clone(next[drag.id] or {})
			current.width = math.clamp(drag.width + x - drag.x, Canvas.min_width, Canvas.max_width)
			current.height = math.max(96, drag.height + y - drag.y)
			next[drag.id] = current
			positions:set(next)
		end
	end

	local function on_release(e)
		if not drag then return end
		if drag.kind == "node" or drag.kind == "resize" then
			save_positions()
		elseif drag.kind == "wire" then
			local x, y = Canvas.point(e, zoom:peek(), pan_x:peek(), pan_y:peek())
			local connected = false
			local blocks = base_blocks:peek()
			for _, cell in snapshot:peek().cells or {} do
				if cell.id == drag.cell_id then continue end
				local pos, block = positions:peek()[cell.id] or { x = 0, y = 0 }, blocks[cell.id]
				local targets = {}
				if block then
					for _, input in block.inputs or {} do table.insert(targets, { key = input.port_key, name = input.name }) end
				else
					for _, ref in cell.refs or {} do table.insert(targets, { key = ref, name = ref }) end
				end
				for _, target in targets do
					if Canvas.near(x, y, pos.x, Canvas.port(cell, target.key, false, block), 14 / zoom:peek()) then
						connected = true
						observe("Connect " .. drag.variable .. " to " .. target.name, function()
							return Bridge.connect(zeron, url:peek(), token:peek(), session_id:peek(), drag.cell_id, drag.variable, cell.id, target.name)
						end, function()
							notice:set("Python source updated; marimo owns the new dependency")
							refresh_graph()
						end, function(err) notice:set("Connection refused: " .. tostring(err) .. " · Open code to edit it") end)
						break
					end
				end
				if connected then break end
			end
			if not connected then notice:set("Drop an output port onto an input port") end
		end
		drag = nil
	end

	local function selected_cell()
		return Canvas.node(snapshot:peek().cells or {}, selected:peek())
	end

	local function run_selected()
		local cell = selected_cell()
		if not cell then notice:set("Select a cell first"); return end
		notice:set("Sending run request to marimo…")
		observe("Run cell " .. cell.id, function()
			return Bridge.run(zeron, url:peek(), token:peek(), session_id:peek(), cell.id)
		end, function()
			notice:set("Marimo queued the cell and its reactive dependents")
			ui.after(500, refresh_graph)
		end, function(err)
			notice:set("Cell run failed: " .. tostring(err))
			refresh_graph()
		end)
	end

	local function open_selected()
		local cell = selected_cell()
		if not cell then notice:set("Select a cell first"); return end
		code_cell_id:set(cell.id)
		code_text:set(cell.source or "")
		code_open:set(true)
		notice:set("Editing cell " .. cell.id .. " inside Zeron")
	end

	local function delete_selected()
		local cell = selected_cell()
		if not cell then notice:set("Select a cell first"); return end
		observe("Delete cell " .. cell.id, function()
			return Bridge.delete(zeron, url:peek(), token:peek(), session_id:peek(), cell.id)
		end, function()
			selected:set(""); refresh_graph()
		end, function(err) notice:set("Could not delete cell: " .. tostring(err)) end)
	end

	local function add_selected_component(item, callable, at_x, at_y)
		if not item or not callable then
			for _, candidate in components:peek() do
				if candidate.id == component_id:peek() then
					item = candidate
					for _, entry in candidate.callables do if entry.name == callable_name:peek() then callable = entry end end
				end
			end
		end
		if not item or not callable then notice:set("Choose a component and callable"); return end
		local bindings = {}
		for _, parameter in callable.parameters or {} do
			if parameter.required then bindings[parameter.name] = { variable = parameter.name } end
		end
		local defined = {}
		for _, cell in cells_now() do for _, name in cell.defs or {} do defined[name] = true end end
		local next_number = output_counter:peek()
		local out = callable.name .. "_result_" .. tostring(next_number)
		while defined[out] do next_number += 1; out = callable.name .. "_result_" .. tostring(next_number) end
		output_counter:set(next_number + 1)
		observe("Add component " .. item.id .. "::" .. callable.name, function()
			return Bridge.add_component(zeron, url:peek(), token:peek(), session_id:peek(), item.id, callable.name, out, bindings)
		end, function(value)
			if at_x and at_y and type(value) == "table" and value.cell_id then
				local next = table.clone(positions:peek())
				next[value.cell_id] = { x = at_x, y = at_y }
				positions:set(next)
				save_positions()
			end
			notice:set("Inserted ordinary Python into the marimo notebook")
			ui.after(120, refresh_graph)
		end, function(err) notice:set("Could not add component: " .. tostring(err)) end)
	end

	local function selected_ids()
		local set = selected_set()
		local ids = {}
		for id in set do table.insert(ids, id) end
		table.sort(ids)
		return ids
	end

	local function selected_count()
		return #selected_ids()
	end

	local function save_selected_group()
		local ids = selected_ids()
		if #ids < 2 then notice:set("Select at least two blocks with Shift-click"); return end
		local path = snapshot:peek().notebook.path or ""
		observe("Save Group Block", function()
			return Bridge.save_group(zeron, url:peek(), token:peek(), session_id:peek(), path, ids, positions:peek(), group_name:peek(), group_category:peek())
		end, function(value)
			group_dialog:set(false)
			notice:set("Saved Group Block " .. tostring(value and value.group and value.group.name or group_name:peek()))
			refresh_graph()
		end, function(err) notice:set("Could not save Group Block: " .. tostring(err)) end)
	end

	local function group_by_id(id)
		for _, group in groups:peek() do if group.id == id then return group end end
		return nil
	end

	local function add_group_at(group, x, y)
		if not group then return end
		local prefix = string.gsub(string.match(group.id or "group", "([^/]+)$") or "group", "[^%w_]", "_")
		observe("Add Group Block " .. tostring(group.name or group.id), function()
			return Bridge.add_group(zeron, url:peek(), token:peek(), session_id:peek(), snapshot:peek().notebook.path or "", group.id, prefix)
		end, function(value)
			if type(value) == "table" and value.cell_id then
				local next = table.clone(positions:peek())
				next[value.cell_id] = { x = x, y = y }
				positions:set(next); save_positions(); selected:set(value.cell_id); selected_many:set({})
			end
			ui.after(120, refresh_graph)
		end, function(err) notice:set("Could not add Group Block: " .. tostring(err)) end)
	end

	local function edit_group_graphically()
		local cell = selected_cell()
		if not cell or not cell.graphimo_group then notice:set("Select a Group Block first"); return end
		local group = group_by_id(cell.graphimo_group.group_id)
		if not group then notice:set("Group definition is missing; refresh Components"); return end
		start_notebook(group.path)
	end

	local function slug(text)
		local value = string.lower(tostring(text or ""))
		value = string.gsub(value, "[^%w_-]+", "-")
		value = string.gsub(value, "^-+", "")
		value = string.gsub(value, "-+$", "")
		return if value == "" then "item" else value
	end

	local function meta_by_id(id)
		for _, meta in metas:peek() do if meta.id == id then return meta end end
		return nil
	end

	local function unique_model_output(prefix)
		local used = {}
		for _, cell in cells_now() do for _, name in cell.defs or {} do used[name] = true end end
		local base = string.gsub(prefix or "model", "[^%w_]", "_")
		if base == "" then base = "model" end
		if string.match(base, "^%d") then base = "_" .. base end
		local candidate, index = base, 2
		while used[candidate] do candidate = base .. "_" .. tostring(index); index += 1 end
		return candidate
	end

	local function add_meta_at(meta, x, y)
		if not meta then return end
		local output = unique_model_output((string.match(meta.id or "model", "([^/]+)$") or "model") .. "_model")
		observe("Add PyTorch Meta Block " .. tostring(meta.name or meta.id), function()
			return Bridge.add_pytorch_meta(zeron, url:peek(), token:peek(), session_id:peek(), snapshot:peek().notebook.path or "", meta.id, output)
		end, function(value)
			if type(value) == "table" and value.cell_id then
				local next = table.clone(positions:peek()); next[value.cell_id] = { x = x, y = y }
				positions:set(next); save_positions(); selected:set(value.cell_id); selected_many:set({})
			end
			ui.after(120, refresh_graph)
		end, function(err) notice:set("Could not add PyTorch Meta Block: " .. tostring(err)) end)
	end

	local function create_meta_definition()
		local category = slug(meta_create_category:peek())
		local name_slug = slug(meta_create_name:peek())
		local base = category .. "/" .. name_slug
		local id, index = base, 2
		while meta_by_id(id) do id = base .. "-" .. tostring(index); index += 1 end
		observe("Create PyTorch Meta Block", function()
			return Bridge.create_pytorch_meta(zeron, url:peek(), token:peek(), session_id:peek(), snapshot:peek().notebook.path or "", id, meta_create_name:peek(), category)
		end, function(value)
			meta_create_dialog:set(false)
			local meta = {
				id = id,
				name = meta_create_name:peek(),
				category = category,
				kind = "pytorch",
				path = value and value.path,
				model_path = value and value.model_path,
				source_sha256 = value and value.source_sha256,
			}
			local next = table.clone(metas:peek()); table.insert(next, meta); metas:set(next)
			add_meta_at(meta, meta_create_x:peek(), meta_create_y:peek())
		end, function(err) notice:set("Could not create PyTorch Meta Block: " .. tostring(err)) end)
	end

	local function open_meta_editor(meta_id)
		local meta = meta_by_id(meta_id)
		if not meta then notice:set("PyTorch Meta Block definition is missing"); return end
		observe("Open PyTorch Meta Block", function()
			return Bridge.get_pytorch_meta(zeron, url:peek(), token:peek(), session_id:peek(), snapshot:peek().notebook.path or "", meta_id)
		end, function(value)
			meta_editor_id:set(meta_id)
			meta_editor_data:set(value.meta or {})
			meta_selected:set("")
			meta_params_text:set("{}")
			meta_dirty:set(false)
			meta_pending:set(nil)
			code_open:set(false)
			meta_editor_open:set(true)
		end, function(err) notice:set("Could not open PyTorch Meta Block: " .. tostring(err)) end)
	end

	local function edit_selected_meta()
		local cell = selected_cell()
		if not cell or not cell.graphimo_meta then notice:set("Select a PyTorch Meta Block first"); return end
		open_meta_editor(cell.graphimo_meta.meta_id)
	end

	local function save_meta_definition(meta)
		observe("Save PyTorch Meta Block", function()
			return Bridge.save_pytorch_meta(zeron, url:peek(), token:peek(), session_id:peek(), snapshot:peek().notebook.path or "", meta)
		end, function()
			meta_dirty:set(false)
			notice:set("PyTorch model.py regenerated from Meta Elements")
			refresh_graph()
		end, function(err) notice:set("PyTorch Meta Block is invalid: " .. tostring(err)) end)
	end

	local function meta_editor_panel()
		return PyTorchMetaEditor.View {
			data = meta_editor_data,
			selected = meta_selected,
			params_text = meta_params_text,
			dirty = meta_dirty,
			pending = meta_pending,
			on_save = save_meta_definition,
			on_notice = function(message) notice:set(message) end,
			on_close = function() meta_editor_open:set(false) end,
		}
	end

	local function code_editor_panel()
		return ui.column {
			w = 500, h_full = true, min_h = 0, shrink = 0, gap = "$space.2", p = "$space.2",
			rounded = "$radius.lg", bg = "$zeron.secondary.bg",
			ui.row { w_full = true, items = "center", gap = "$space.2",
				Text { function() return "Code · " .. string.sub(code_cell_id:get(), 1, 10) end, style = "label", grow = 1 },
				Button { "Close", tone = "secondary", size = "sm", on_click = function() code_open:set(false) end },
			},
			CodeEditor {
				value = code_text,
				language = "python",
				w_full = true,
				h_full = true,
				label = "Graphimo block source",
				on_save = function(text)
					local id = code_cell_id:peek()
					if id == "" then return end
					observe("Save code " .. id, function()
						return Bridge.edit(zeron, url:peek(), token:peek(), session_id:peek(), id, text)
					end, function()
						code_text:set(text)
						notice:set("Cell source saved; marimo recomputes the graph")
						refresh_graph()
					end, function(err) notice:set("Could not save cell: " .. tostring(err)) end)
				end,
			},
		}
	end

	local function create_block_at(source, x, y, label)
		observe(label or "Create block", function()
			return Bridge.add_cell(zeron, url:peek(), token:peek(), session_id:peek(), source)
		end, function(value)
			if type(value) == "table" and value.cell_id then
				local next = table.clone(positions:peek())
				next[value.cell_id] = { x = x, y = y }
				positions:set(next)
				save_positions()
				selected:set(value.cell_id)
			end
			ui.after(120, refresh_graph)
		end, function(err) notice:set("Could not create block: " .. tostring(err)) end)
	end

	local function component_context_entries(x, y)
		local groups = Library.project(components:peek(), "")
		local result = {}
		for _, group in groups do
			local entries = {}
			for _, item in group.components or {} do
				if #(item.callables or {}) == 1 then
					local callable = item.callables[1]
					table.insert(entries, { label = callable.name, on_select = function() add_selected_component(item, callable, x, y) end })
				else
					local callable_entries = {}
					for _, callable in item.callables or {} do
						table.insert(callable_entries, { label = callable.name, on_select = function() add_selected_component(item, callable, x, y) end })
					end
					table.insert(entries, { label = string.gsub(item.id, "/", " · "), submenu = callable_entries })
				end
			end
			if #entries > 0 then table.insert(result, { label = group.category, submenu = entries }) end
		end
		return result
	end

	local function canvas_menu(at)
		local x, y = Canvas.point({ local_x = at.local_x, local_y = at.local_y }, zoom:peek(), pan_x:peek(), pan_y:peek())
		local component_entries = component_context_entries(x, y)
		local group_entries = {}
		for _, group in groups:peek() do
			local chosen = group
			table.insert(group_entries, { label = tostring(group.category or "general") .. " · " .. tostring(group.name or group.id), on_select = function() add_group_at(chosen, x, y) end })
		end
		local meta_entries = {
			{ label = "New PyTorch Model…", on_select = function()
				meta_create_x:set(x); meta_create_y:set(y); meta_create_dialog:set(true)
			end },
		}
		if #metas:peek() > 0 then table.insert(meta_entries, { separator = true }) end
		for _, meta in metas:peek() do
			local chosen = meta
			table.insert(meta_entries, { label = tostring(meta.category or "models") .. " · " .. tostring(meta.name or meta.id), on_select = function() add_meta_at(chosen, x, y) end })
		end
		return {
			{ label = "Code block", on_select = function() create_block_at("pass", x, y, "Create code block") end },
			{ label = "Annotation", on_select = function() create_block_at("# Graphimo annotation\\n__import__('marimo').md(\\"Annotation\\")", x, y, "Create annotation") end },
			{ label = "Display", on_select = function() create_block_at("# Graphimo display\\nNone", x, y, "Create display block") end },
			{ separator = true },
			{ label = "Component", disabled = if #component_entries == 0 then "No components found" else false, submenu = component_entries },
			{ label = "Group Block", disabled = if #group_entries == 0 then "No saved groups" else false, submenu = group_entries },
			{ label = "Meta Block", submenu = { { label = "PyTorch", submenu = meta_entries } } },
		}
	end

	local function component_library_panel()
		local groups = ui.computed(function()
			return Library.project(components:get(), library_query:get())
		end)
		return ui.column { w = 250, h_full = true, shrink = 0, gap = "$space.2", p = "$space.2", rounded = "$radius.lg", bg = "$zeron.secondary.bg", overflow = "auto",
			Text { "Components", style = "heading" },
			SearchField { value = library_query, placeholder = "Search components", name = "Search components", size = "sm", quiet = true, w_full = true },
			ui.show(function() return #groups:get() == 0 end, Text { function()
				return if #components:get() == 0 then "No components found in this project's components/ folder" else "No components match this search"
			end, style = "caption", tone = "secondary", wrap = true }),
			ui.each(groups, function(group)
				return ui.column { w_full = true, gap = "$space.1",
					Text { group.category, style = "caption", tone = "secondary" },
					ui.each(group.components, function(item)
						return ui.column { w_full = true, gap = "$space.1",
							Button {
								function() return (if expanded_component:get() == item.id then "▾ " else "▸ ") .. string.gsub(item.id, "/", " · ") end,
								tone = "secondary", size = "sm", w_full = true,
								on_click = function()
									component_id:set(item.id)
									callable_name:set(item.callables and item.callables[1] and item.callables[1].name or "")
									expanded_component:set(if expanded_component:peek() == item.id then "" else item.id)
								end,
							},
							ui.show(function() return expanded_component:get() == item.id end, ui.column { w_full = true, gap = "$space.1",
								ui.each(item.callables or {}, function(callable)
									return ui.row { w_full = true, items = "center", gap = "$space.1",
										Text { (callable.name or "") .. (callable.signature or ""), style = "caption", tone = "secondary", grow = 1, wrap = true },
										Button { "Add", tone = "secondary", size = "sm", disabled = function() return session_id:get() == "" end, on_click = function() add_selected_component(item, callable) end },
									}
								end),
							}),
						}
					end),
				}
			end),
			ui.show(function() return #groups:get() > 0 end, ui.column {
				w_full = true, gap = "$space.1",
				Text { "Group Blocks", style = "caption", tone = "secondary" },
				ui.each(groups, function(group)
					return Button { tostring(group.category or "general") .. " · " .. tostring(group.name or group.id), tone = "secondary", size = "sm", w_full = true, on_click = function() add_group_at(group, 120, 120) end }
				end),
			}),
			ui.column {
				w_full = true, gap = "$space.1",
				Text { "Meta Blocks", style = "caption", tone = "secondary" },
				Button { "+ PyTorch Model", tone = "secondary", size = "sm", w_full = true, on_click = function() meta_create_x:set(120); meta_create_y:set(120); meta_create_dialog:set(true) end },
				ui.each(metas, function(meta)
					return ui.row { w_full = true, gap = "$space.1",
						Button { tostring(meta.category or "models") .. " · " .. tostring(meta.name or meta.id), tone = "secondary", size = "sm", grow = 1, on_click = function() add_meta_at(meta, 120, 120) end },
						Button { "Edit", tone = "ghost", size = "sm", on_click = function() open_meta_editor(meta.id) end },
					}
				end),
			},
		}
	end

	return ui.column { w_full = true, h_full = true, min_h = 0, gap = "$space.3", p = "$space.4",
		ui.row { w_full = true, items = "center", gap = "$space.2",
			ui.canvas { w = 30, h = 30, draw = function(c)
				local path = ui.path():move(7, 15):cubic(10, 5, 20, 5, 23, 15):cubic(20, 25, 10, 25, 7, 15)
				c:stroke(path, "$accent", 2, { cap = "round" })
				c:circle(7, 15, 3, "$accent"); c:circle(23, 15, 3, "$accent"); c:circle(15, 8, 3, "$accent"); c:circle(15, 22, 3, "$accent")
			end, pointer_events = "none" },
			Text { "Graphimo", style = "heading" },
			Text { function() return snapshot:get().notebook.path or "Visual view of your marimo notebook" end, style = "caption", tone = "secondary", grow = 1 },
			Button { "Open notebook", size = "sm", on_click = choose_notebook },
			Button { "New notebook", tone = "secondary", size = "sm", on_click = create_notebook },
			Button { "Refresh", tone = "secondary", size = "sm", disabled = function() return session_id:get() == "" end, on_click = refresh_graph },
			Button { "Fit", tone = "secondary", size = "sm", on_click = fit },
			Button { "−", tone = "secondary", size = "sm", on_click = function() zoom_at(zoom:peek() / 1.15, (canvas.width or 0) / 2, (canvas.height or 0) / 2) end },
			Button { "+", tone = "secondary", size = "sm", on_click = function() zoom_at(zoom:peek() * 1.15, (canvas.width or 0) / 2, (canvas.height or 0) / 2) end },
			Button { function() return if components_open:get() then "Components −" else "Components +" end, tone = "secondary", size = "sm", on_click = function() components_open:set(not components_open:peek()) end },
			Button { function() return if logs_open:get() then "Terminal −" else "Terminal +" end, tone = "secondary", size = "sm", on_click = function() logs_open:set(not logs_open:peek()) end },
		},
		ui.row { w_full = true, gap = "$space.2", items = "center",
			Button { "Run", size = "sm", disabled = function() return selected:get() == "" end, on_click = run_selected },
			Button { "Open code", tone = "secondary", size = "sm", disabled = function() return selected:get() == "" end, on_click = open_selected },
			Button { "Delete", tone = "secondary", size = "sm", disabled = function() return selected:get() == "" end, on_click = delete_selected },
			Button { "Save group", tone = "secondary", size = "sm", disabled = function() return selected_count() < 2 end, on_click = function() group_dialog:set(true) end },
			Button { "Edit group", tone = "secondary", size = "sm", disabled = function() local cell = selected_cell(); return not (cell and cell.graphimo_group) end, on_click = edit_group_graphically },
			Button { "Edit meta", tone = "secondary", size = "sm", disabled = function() local cell = selected_cell(); return not (cell and cell.graphimo_meta) end, on_click = edit_selected_meta },
			Button { function() return if grid:get() then "Hide grid" else "Show grid" end, tone = "secondary", size = "sm", on_click = function() grid:set(not grid:peek()) end },
		},
		ui.row { grow = 1, min_h = 0, w_full = true, gap = "$space.3",
			ui.show(function() return components_open:get() end, component_library_panel()),
			ui.column { grow = 1, min_w = 0, min_h = 0, gap = "$space.3",
			ui.div { ref = canvas, grow = 1, min_h = 0, w_full = true, relative = true, rounded = "$radius.lg", overflow = "hidden",
			ui.canvas { absolute = true, left = 0, top = 0, w_full = true, h_full = true, draw = function(c, w, h)
				Canvas.draw_grid(c, w, h, zoom:get(), pan_x:get(), pan_y:get(), grid:get())
			end, pointer_events = "none" },
			ui.canvas { absolute = true, left = 0, top = 0, w_full = true, h_full = true, draw = function(c)
				Canvas.draw_edges(c, positioned_cells(), snapshot:get().edges or {}, zoom:get(), pan_x:get(), pan_y:get(), selected:get(), nil, base_blocks:get())
			end, pointer_events = "none" },
			ui.div { absolute = true, left = 0, right = 0, top = 0, bottom = 0, pointer_events = "none",
				ui.each(visible_cell_ids, card, { key = function(id) return id end, w_full = true, h_full = true, relative = true }),
			},
			ContextMenu {
				items = canvas_menu,
				label = "Graphimo canvas actions",
				absolute = true, left = 0, top = 0, right = 0, bottom = 0,
				ui.canvas { absolute = true, left = 0, top = 0, w_full = true, h_full = true, draw = function() end,
				on_mount = function()
					ui.after(150, function()
						if url:peek() ~= "" and session_id:peek() ~= "" then refresh_sessions()
						else notice:set("Open or create a notebook to begin") end
					end)
				end,
				on_press = on_press, on_pointer_move = on_move, on_release = on_release,
				on_wheel = function(e)
					if e.dy ~= 0 then zoom_at(zoom:peek() * math.exp(-math.clamp(e.dy, -120, 120) * 0.002), e.local_x or (canvas.width or 0) / 2, e.local_y or (canvas.height or 0) / 2) end
					return true
				end,
				},
			},
			},
			Text { function() return notice:get() end, style = "caption", tone = "secondary", wrap = true },
			ui.show(function() return logs_open:get() end, ui.column { w_full = true, max_h = 240, overflow = "auto", gap = "$space.1", p = "$space.2", rounded = "$radius.md", bg = "$zeron.secondary.bg",
			ui.row { w_full = true, items = "center", gap = "$space.2",
				Button { "Terminal", tone = function() return if terminal_mode:get() == "terminal" then "primary" else "secondary" end, size = "sm", on_click = function() terminal_mode:set("terminal") end },
				Button { "All logs", tone = function() return if terminal_mode:get() == "all" then "primary" else "secondary" end, size = "sm", on_click = function() terminal_mode:set("all") end },
				ui.div { grow = 1 },
				Button { "Read marimo log", tone = "secondary", size = "sm", on_click = refresh_marimo_log },
				Button { "Export logs", tone = "secondary", size = "sm", on_click = export_logs },
			},
			Text { function()
				if terminal_mode:get() == "terminal" then
					local lines = {}
					for _, cell in snapshot:get().cells or {} do
						if #(cell.console or {}) > 0 then
							local b = base_blocks:get()[cell.id]
							table.insert(lines, "> " .. tostring((b and b.callable) or cell.name or ("Cell " .. string.sub(cell.id, 1, 7))))
							for _, message in cell.console or {} do table.insert(lines, tostring(message.text or "")) end
						end
					end
					return if #lines == 0 then "No stdout/stderr from blocks yet" else table.concat(lines, "\n")
				end
				local recent = log_entries:get()
				local problems = problem_entries:get()
				local first = math.max(1, #recent - 30)
				local view = {}
				for index = first, #recent do table.insert(view, recent[index]) end
				if #problems > 0 then table.insert(view, "--- Saved problems ---") end
				for index = math.max(1, #problems - 20), #problems do table.insert(view, problems[index]) end
				if problem_log_text:get() ~= "" then table.insert(view, "--- Persistent problem log file ---"); table.insert(view, problem_log_text:get()) end
				if server_log_text:get() ~= "" then table.insert(view, "--- Marimo process log ---"); table.insert(view, server_log_text:get()) end
				return if #view == 0 then "No log events yet" else table.concat(view, "\n")
			end, style = "caption", tone = "secondary", wrap = true },
			}),
			},
			ui.show(function() return meta_editor_open:get() end, meta_editor_panel()),
			ui.show(function() return code_open:get() end, code_editor_panel()),
		},
		Dialog {
			open = meta_create_dialog,
			title = "New PyTorch Meta Block",
			body = "Creates one reusable model definition. Its internal Elements generate one nn.Module; they are not marimo cells.",
			confirm = { label = "Create", on_click = create_meta_definition, disabled = function() return meta_create_name:get() == "" end },
			TextField { value = meta_create_name, label = "Name", placeholder = "Body Part model", w_full = true, autofocus = true },
			TextField { value = meta_create_category, label = "Category", placeholder = "models", w_full = true },
		},
		Dialog {
			open = group_dialog,
			title = "Save Group Block",
			body = "The selected blocks become one reusable referenced marimo group. Instances point to this definition; they do not copy it.",
			confirm = { label = "Save", on_click = save_selected_group, disabled = function() return group_name:get() == "" end },
			TextField { value = group_name, label = "Name", placeholder = "Body Part model", w_full = true, autofocus = true },
			TextField { value = group_category, label = "Category", placeholder = "models", w_full = true },
		},
	}
end)

zeron.page({ id = "canvas", title = "Graphimo", icon = "share", build = function() return Page {} end })
