local zeron = require("./zeron")("graphimo")
local Bridge = require("./marimo_bridge")
local Service = require("./graphimo_service")
local Layout = require("./layout")
local Canvas = require("./canvas")
local BaseBlock = require("./base_block")
local Library = require("./component_library")
local Text = require("@ds/text")
local Button = require("@ds/button")
local Glass = require("@ds/glass")
local Markdown = require("@ds/markdown")
local SearchField = require("@ds/search_field")

local active_notebook = zeron.store("active-notebook-v1", { url = "", session_id = "", path = "", log_path = "" })
local saved_logs = zeron.store("logs-v1", {})
local saved_problems = zeron.store("problems-v1", {})

local function renderer_snapshot(state, path, project_id)
	state = if type(state) == "table" then state else {}
	local cells, blocks_by_id, edges = {}, {}, {}
	for index, block in state.blocks or {} do
		if type(block) == "table" and type(block.id) == "string" then
			local inputs, outputs, refs, defs = {}, {}, {}, {}
			for _, port in block.inputs or {} do
				if type(port) == "table" and type(port.name) == "string" then
					table.insert(inputs, port)
					table.insert(refs, port.name)
				end
			end
			for output_index, port in block.outputs or {} do
				if type(port) == "table" and type(port.name) == "string" then
					table.insert(outputs, port)
					table.insert(defs, port.name)
				end
			end
			local body = if type(block.body) == "table" then block.body else {}
			local cell = {
				id = block.id,
				name = block.title or ("Block " .. tostring(index)),
				kind = block.kind,
				source = "Graphimo " .. tostring(block.kind or "block"),
				status = block.state or "idle",
				defs = defs,
				refs = refs,
				inputs = inputs,
				outputs = outputs,
				output_index = {},
				output = nil,
				errors = {},
				body = body,
				can_open = block.can_open == true,
				can_disable = block.can_disable == true,
				can_enable = block.can_enable == true,
			}
			for output_index, port in outputs do cell.output_index[port.name] = output_index - 1 end
			if #outputs > 0 and outputs[1].preview then
				cell.output = { mimetype = "text/plain", preview = tostring(outputs[1].preview) }
			end
			blocks_by_id[block.id] = cell
			table.insert(cells, cell)
		end
	end
	for _, edge in state.edges or {} do
		if type(edge) == "table" then
			local source = blocks_by_id[edge.source_block_id]
			local target = blocks_by_id[edge.target_block_id]
			local output_name = if source and source.defs[1] then source.defs[1] else edge.variable
			local input_name = if target and target.refs[1] then target.refs[1] else edge.variable
			if source then
				for _, port in source.outputs do
					if port.expression == edge.variable or port.name == edge.variable then output_name = port.name; break end
				end
			end
			if target then
				for _, port in target.inputs do
					if type(port.expression) == "string" and string.find(port.expression, edge.variable, 1, true) then input_name = port.name; break end
				end
			end
			table.insert(edges, {
				id = edge.id,
				source_cell_id = edge.source_block_id,
				target_cell_id = edge.target_block_id,
				variable = edge.variable,
				output_name = output_name,
				input_name = input_name,
				state = edge.state,
			})
		end
	end
	for _, diagnostic in state.diagnostics or {} do
		local block = diagnostic.block_id and blocks_by_id[diagnostic.block_id]
		if block then
			table.insert(block.errors, { kind = diagnostic.code or "Graphimo", message = diagnostic.message or "Graphimo diagnostic" })
		end
	end
	for _, cell in cells do
		for _, edge in edges do
			if edge.target_cell_id == cell.id then
				local found = false
				for _, name in cell.refs do if name == edge.input_name then found = true end end
				if not found then table.insert(cell.refs, edge.input_name) end
			end
		end
	end
	local palette = state.palette or {}
	local components = {}
	for _, item in palette.items or {} do
		local parameters, signature = {}, {}
		for _, schema in item.input_schema or {} do
			if type(schema) == "table" and type(schema.name) == "string" then
				table.insert(parameters, {
					name = schema.name,
					required = schema.required == true,
					positional = true,
					has_default = schema.has_default == true,
					default = schema.default,
					annotation = schema.annotation,
				})
				table.insert(signature, schema.name .. (schema.required and "" or "=…"))
			end
		end
		table.insert(components, {
			id = item.id,
			path = item.id,
			category = item.category or item.kind or "Components",
			kind = item.kind or "node",
			description = item.description,
			callables = {{ name = item.title or item.id, signature = "(" .. table.concat(signature, ", ") .. ")", description = item.description, parameters = parameters }},
		})
	end
	return {
		notebook = { path = path or "", session_id = project_id or "" },
		project_id = state.project_id,
		active_scope_id = state.active_scope_id,
		breadcrumbs = state.breadcrumbs or {},
		cells = cells,
		edges = edges,
		palette = palette,
		diagnostics = state.diagnostics or {},
		components = components,
	}
end

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

zeron.tool({ name = "run", run = function(args)
	local url, token, session = config_for(args or {})
	if not session then return { ok = false, error = { code = "selection_required", message = "Select a live marimo notebook in Graphimo first" } } end
	return start_tool(function() return Bridge.run(zeron, url, token, session, args.cell_id) end)
end })

zeron.tool({ name = "open_code", run = function(args)
	local url, _, session = config_for(args or {})
	if args.session_id then session = args.session_id end
	if url == "" then return { ok = false, error = { code = "selection_required", message = "Open a notebook in Graphimo first" } } end
	zeron.open_url(string.gsub(url, "/+$", ""))
	return { ok = true, opened = true, session_id = session, cell_id = args.cell_id }
end })

local Page = ui.component(function()
	local saved = active_notebook:peek()
	local url = ui.use_state(saved.url or "")
	local token = ui.use_state("")
	local session_id = ui.use_state(saved.session_id or "")
	local service_connection = ui.use_state(nil)
	local snapshot = ui.use_state({ notebook = {}, cells = {}, edges = {} })
	local visible_cell_ids = ui.use_state({})
	local log_entries = ui.use_state(saved_logs:peek() or {})
	local problem_entries = ui.use_state(saved_problems:peek() or {})
	local server_log_text = ui.use_state("")
	local problem_log_text = ui.use_state("")
	local logs_open = ui.use_state(false)
	local components_open = ui.use_state(true)
	local library_query = ui.use_state("")
	local expanded_component = ui.use_state("")
	local positions = ui.use_state({})
	local components = ui.use_state({})
	local component_id = ui.use_state("")
	local callable_name = ui.use_state("")
	local selected = ui.use_state("")
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

	local base_summaries = ui.computed(function()
		local summaries = {}
		for _, projected in BaseBlock.project(components:get(), cells_now()) do
			summaries[projected.cell_id] = BaseBlock.summary(projected)
		end
		return summaries
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
			table.insert(positioned, view)
		end
		return positioned
	end

	local function apply_project_state(state, path, project_id)
		local value = renderer_snapshot(state, path, project_id)
		snapshot:set(value)
		components:set(value.components or {})
		local ids = {}
		for _, cell in value.cells or {} do table.insert(ids, cell.id) end
		visible_cell_ids:set(ids)
		local function apply_layout(saved_positions)
			local merged, active = {}, {}
			for index, cell in value.cells do
				active[cell.id] = true
				local saved_cell = saved_positions[cell.id] or {}
				merged[cell.id] = {
					x = saved_cell.x or cell.x or (80 + ((index - 1) % 4) * 320),
					y = saved_cell.y or cell.y or (80 + math.floor((index - 1) / 4) * 230),
					collapsed = saved_cell.collapsed or false,
				}
			end
			positions:set(merged)
			if selected:peek() ~= "" and not active[selected:peek()] then selected:set("") end
			notice:set("Showing " .. tostring(#value.cells) .. " Graphimo blocks")
		end
		Layout.load(zeron, path, function(saved_positions, err)
			if err then log("Load canvas layout failed: " .. tostring(err)) end
			apply_layout(saved_positions)
		end)
	end

	local refresh_graph
	refresh_graph = function()
		local connection = service_connection:peek()
		if type(connection) ~= "table" then
			local path = active_notebook:peek().path or ""
			if path ~= "" then
				observe("Connect Graphimo service", function() return Service.open_project(zeron, path) end, function(opened)
					service_connection:set(opened)
					url:set(opened.editor_url or "")
					session_id:set(opened.project_id)
					apply_project_state(opened.state, opened.path, opened.project_id)
				end, function(err) notice:set("Could not connect to Graphimo service: " .. tostring(err)) end)
				return
			end
			notice:set("Open or create a notebook to begin")
			return
		end
		observe("Refresh Graphimo project", function()
			return Service.execute(zeron, connection, "refresh_project", {})
		end, function(response)
			apply_project_state(response.state, connection.path, connection.project_id)
		end, function(err) notice:set("Could not refresh Graphimo project: " .. tostring(err)) end)
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

	local function start_notebook(path)
		notice:set("Opening Graphimo project for " .. path .. "…")
		log("Opening Graphimo project " .. path)
		observe("Open Graphimo project", function()
			return Service.open_project(zeron, path)
		end, function(opened)
			service_connection:set(opened)
			url:set(opened.editor_url or "")
			session_id:set(opened.project_id)
			active_notebook:set({ url = opened.editor_url or "", session_id = opened.project_id, path = opened.path, log_path = "" })
			apply_project_state(opened.state, opened.path, opened.project_id)
			log("Graphimo service connected for " .. opened.path)
		end, function(err)
			local message = "Could not open Graphimo project: " .. tostring(err)
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
			if type(file.path) ~= "string" then return end
			observe("Create Graphimo project", function() return Service.create_project(zeron, file.path) end, function(opened)
				service_connection:set(opened)
				url:set(opened.editor_url or "")
				session_id:set(opened.project_id)
				active_notebook:set({ url = opened.editor_url or "", session_id = opened.project_id, path = opened.path, log_path = "" })
				apply_project_state(opened.state, opened.path, opened.project_id)
				log("Created Graphimo project " .. opened.path)
			end, function(err)
				local message = "Could not create Graphimo project: " .. tostring(err)
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

	local function card(id)
		local function cell() return Canvas.node(snapshot:get().cells or {}, id) end
		local function base_summary() return base_summaries:get()[id] end
		local function ports(names, prefix, suffix)
			local labels = {}
			for _, name in names or {} do table.insert(labels, prefix .. name .. suffix) end
			return table.concat(labels, "\n")
		end
		local style = Glass.style({
			absolute = true,
			left = function() local p = positions:get()[id] or {}; return pan_x:get() + (p.x or 0) * zoom:get() end,
			top = function() local p = positions:get()[id] or {}; return pan_y:get() + (p.y or 0) * zoom:get() end,
			w = Canvas.width * zoom:get(),
			h = function()
				local value = cell()
				return Canvas.height(value or {}, base_summary()) * zoom:get()
			end,
			rounded = "$radius.lg",
			pointer_events = "none",
		}, "island")
		style.border_color = function() return if selected:get() == id then "$accent" else "$border.strong" end
		style.bg = "$zeron.secondary.bg"
		table.insert(style, ui.column { w_full = true, h_full = true, gap = "$space.1", p = "$space.3",
			ui.row { w_full = true, items = "center", gap = "$space.2",
				Text { function() local value = cell(); return if value and value.name then value.name else "Cell " .. string.sub(id, 1, 7) end, style = "label", grow = 1 },
				Text { function() local value = cell(); return value and value.status or "unknown" end, style = "caption", tone = function() local value = cell(); return if value and (value.status == "success" or value.status == "idle") then "success" elseif value and (value.status == "error" or value.status == "exception" or value.status == "marimo-error") then "danger" else "secondary" end },
			},
			Text { function() local value = cell(); return string.match(value and value.source or "", "^[^\n]*") or "Empty cell" end, style = "caption", tone = "secondary", wrap = true },
			ui.show(function() local value = cell(); return value and type(value.output) == "table" and value.output.image_data_url ~= nil end,
				ui.image { src = function() local value = cell(); return value and value.output and value.output.image_data_url or "" end, w_full = true, h = 64, fit = "contain" }),
			ui.show(function() local value = cell(); return value and type(value.output) == "table" and value.output.mimetype == "text/markdown" end,
				Markdown { text = function() local value = cell(); return value and value.output and value.output.preview or "" end }),
			Text { function()
				local value = cell()
				if not value then return "" end
				if type(value.output) == "table" then
					local mime = value.output.mimetype or "text/plain"
					if value.output.image_data_url or mime == "text/markdown" then return "" end
					local preview = value.output.preview or ""
					if preview == "" then return tostring(mime) end
					return tostring(mime) .. " · " .. tostring(preview)
				end
				if value.output and value.output ~= "None" then return tostring(value.output) end
				return value.errors and value.errors[1] and value.errors[1].message or ""
			end, style = "caption", tone = "secondary", wrap = true },
			ui.row { w_full = true, grow = 1, items = "stretch", gap = "$space.2",
				ui.column { w = 82, gap = "$space.1", justify = "start",
					Text { function() local value = cell(); return ports(value and value.refs, "● ", "") end, style = "caption", tone = "secondary", wrap = true },
				},
				ui.div { grow = 1 },
				ui.column { w = 82, gap = "$space.1", items = "end",
					Text { function() local value = cell(); return ports(value and value.defs, "", " ●") end, style = "caption", tone = "primary", wrap = true },
				},
			},
			ui.show(function()
				local summary = base_summary()
				return summary ~= nil and (#summary.inputs > 0 or #summary.outputs > 0)
			end, ui.column { w_full = true, gap = "$space.1",
				ui.show(function() local summary = base_summary(); return summary ~= nil and #summary.inputs > 0 end,
					Text { function()
						local summary = base_summary()
						return "In: " .. table.concat(summary and summary.inputs or {}, " · ")
					end, style = "caption", tone = "secondary", wrap = true }),
				ui.show(function() local summary = base_summary(); return summary ~= nil and #summary.outputs > 0 end,
					Text { function()
						local summary = base_summary()
						return "Out: " .. table.concat(summary and summary.outputs or {}, " · ")
					end, style = "caption", tone = "secondary", wrap = true }),
			}),
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
			right = math.max(right, p.x + Canvas.width); bottom = math.max(bottom, p.y + Canvas.height(cell, base_summaries:peek()[cell.id]))
		end
		local w, h = canvas.width or 800, canvas.height or 600
		local scale = math.min((w - 64) / math.max(1, right - left), (h - 64) / math.max(1, bottom - top), 1.3)
		scale = math.clamp(scale, 0.35, 1.3)
		zoom:set(scale); pan_x:set((w - (right - left) * scale) / 2 - left * scale); pan_y:set((h - (bottom - top) * scale) / 2 - top * scale)
	end

	local function on_press(e)
		local x, y = Canvas.point(e, zoom:peek(), pan_x:peek(), pan_y:peek())
		local current_cells = snapshot:peek().cells or {}
		local ps = positions:peek()
		for index = #current_cells, 1, -1 do
			local cell = current_cells[index]
			local pos = ps[cell.id] or { x = 0, y = 0 }
			for output_index, variable in cell.defs or {} do
				if Canvas.near(x, y, pos.x + Canvas.width, pos.y + Canvas.port(cell, variable, true), 12 / zoom:peek()) then
					selected:set(cell.id); drag = { kind = "wire", cell_id = cell.id, variable = variable, output_index = output_index - 1 }; return
				end
			end
			for _, variable in cell.refs or {} do
				if Canvas.near(x, y, pos.x, pos.y + Canvas.port(cell, variable, false), 12 / zoom:peek()) then selected:set(cell.id); return end
			end
			local height = Canvas.height(cell, base_summaries:peek()[cell.id])
			if x >= pos.x and x <= pos.x + Canvas.width and y >= pos.y and y <= pos.y + height then
				selected:set(cell.id)
				drag = { kind = "node", id = cell.id, x = x, y = y, nx = pos.x, ny = pos.y }
				return
			end
		end
		drag = { kind = "pan", x = e.local_x or 0, y = e.local_y or 0, px = pan_x:peek(), py = pan_y:peek() }
	end

	local function on_move(e)
		if not drag then return end
		if drag.kind == "pan" then
			pan_x:set(drag.px + (e.local_x or 0) - drag.x); pan_y:set(drag.py + (e.local_y or 0) - drag.y)
		elseif drag.kind == "node" then
			local x, y = Canvas.point(e, zoom:peek(), pan_x:peek(), pan_y:peek())
			local next = table.clone(positions:peek()); next[drag.id] = { x = drag.nx + x - drag.x, y = drag.ny + y - drag.y }; positions:set(next)
		end
	end

	local function on_release(e)
		if not drag then return end
		if drag.kind == "node" then save_positions()
		elseif drag.kind == "wire" then
			local x, y = Canvas.point(e, zoom:peek(), pan_x:peek(), pan_y:peek())
			local connected = false
			for _, cell in snapshot:peek().cells or {} do
				local pos = positions:peek()[cell.id] or { x = 0, y = 0 }
				for _, ref in cell.refs or {} do
					if cell.id ~= drag.cell_id and Canvas.near(x, y, pos.x, pos.y + Canvas.port(cell, ref, false), 14 / zoom:peek()) then
						connected = true
						observe("Connect " .. drag.variable .. " to " .. ref, function()
							return Service.execute(zeron, service_connection:peek(), "connect", {
								source_cell_id = drag.cell_id,
								target_cell_id = cell.id,
								input_name = ref,
								output_index = drag.output_index,
							})
						end, function()
							notice:set("Connected blocks; marimo owns the dependency")
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
		observe("Run block " .. cell.id, function()
			return Service.execute(zeron, service_connection:peek(), "run_target", { cell_id = cell.id })
		end, function(response)
			if response.state then apply_project_state(response.state, service_connection:peek().path, session_id:peek()) end
			notice:set("Marimo ran the selected block and its reactive dependents")
			ui.after(500, refresh_graph)
		end, function(err)
			notice:set("Cell run failed: " .. tostring(err))
			refresh_graph()
		end)
	end

	local function open_selected()
		local cell = selected_cell()
		if not cell then notice:set("Select a cell first"); return end
		local editor_url = service_connection:peek() and service_connection:peek().editor_url
		if type(editor_url) ~= "string" or editor_url == "" then notice:set("The marimo editor URL is unavailable"); return end
		zeron.open_url(editor_url)
		notice:set("Opened the marimo editor for cell " .. cell.id)
		log("Opened marimo editor for cell " .. cell.id)
	end

	local function delete_selected()
		local cell = selected_cell()
		if not cell then notice:set("Select a cell first"); return end
		observe("Delete block " .. cell.id, function()
			return Service.execute(zeron, service_connection:peek(), "delete_elements", { ids = { cell.id } })
		end, function(response)
			if response.state then apply_project_state(response.state, service_connection:peek().path, session_id:peek()) end
			selected:set(""); refresh_graph()
		end, function(err) notice:set("Could not delete cell: " .. tostring(err)) end)
	end

	local function add_selected_component(item, callable)
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
		for name, value in bindings do bindings[name] = if type(value) == "table" and value.variable then value.variable else value end
		observe("Add Graphimo block " .. item.id, function()
			return Service.execute(zeron, service_connection:peek(), "add_palette_item", {
				item_id = item.id,
				bindings = bindings,
			})
		end, function(response)
			if response.state then apply_project_state(response.state, service_connection:peek().path, session_id:peek()) end
			notice:set("Added a Graphimo block to the marimo project")
			refresh_graph()
		end, function(err) notice:set("Could not add component: " .. tostring(err)) end)
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
				Canvas.draw_edges(c, positioned_cells(), snapshot:get().edges or {}, zoom:get(), pan_x:get(), pan_y:get(), selected:get())
			end, pointer_events = "none" },
			ui.div { absolute = true, left = 0, right = 0, top = 0, bottom = 0, pointer_events = "none",
				ui.each(visible_cell_ids, card, { key = function(id) return id end, w_full = true, h_full = true, relative = true }),
			},
			ui.canvas { absolute = true, left = 0, top = 0, w_full = true, h_full = true, draw = function() end,
				on_mount = function()
					ui.after(150, function()
						local path = active_notebook:peek().path or ""
						if path ~= "" then start_notebook(path) else notice:set("Open or create a notebook to begin") end
					end)
				end,
				on_press = on_press, on_pointer_move = on_move, on_release = on_release,
				on_wheel = function(e)
					if e.dy ~= 0 then zoom_at(zoom:peek() * math.exp(-math.clamp(e.dy, -120, 120) * 0.002), e.local_x or (canvas.width or 0) / 2, e.local_y or (canvas.height or 0) / 2) end
					return true
				end,
			},
			},
			Text { function() return notice:get() end, style = "caption", tone = "secondary", wrap = true },
			ui.show(function() return logs_open:get() end, ui.column { w_full = true, max_h = 240, overflow = "auto", gap = "$space.1", p = "$space.2", rounded = "$radius.md", bg = "$zeron.secondary.bg",
			ui.row { w_full = true, items = "center", gap = "$space.2",
				Text { function() return tostring(#problem_entries:get()) .. " saved problems · " .. tostring(#log_entries:get()) .. " saved events" end, style = "caption", tone = "secondary", grow = 1 },
				Button { "Read marimo log", tone = "secondary", size = "sm", on_click = refresh_marimo_log },
				Button { "Export logs", tone = "secondary", size = "sm", on_click = export_logs },
			},
			Text { function()
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
			},
	}
end)

zeron.page({ id = "canvas", title = "Graphimo", icon = "share", build = function() return Page {} end })
