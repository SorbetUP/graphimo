local zeron = require("./zeron")("graphimo")
local Bridge = require("./marimo_bridge")
local Service = require("./service_bridge")
local Layout = require("./layout")
local Canvas = require("./canvas")
local BaseBlock = require("./base_block")
local Library = require("./component_library")
local MetaBlockProjection = require("./metablock_projection")
local Text = require("@ds/text")
local Button = require("@ds/button")
local Glass = require("@ds/glass")
local Markdown = require("@ds/markdown")
local SearchField = require("@ds/search_field")
local ContextMenu = require("@ds/context_menu")
local Dialog = require("@ds/dialog").Dialog
local TextField = require("@ds/text_field")

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

local function error_message(value)
	if type(value) ~= "table" then return tostring(value or "Graphimo operation failed") end
	local detail = value.error
	if type(detail) == "table" then return tostring(detail.message or detail.code or "Graphimo operation failed") end
	return tostring(detail or value.message or "Graphimo operation failed")
end

local function is_group_selection_rejection(value)
	local message = error_message(value)
	return string.sub(message, 1, 12) == "unsafe_edit:" or string.sub(message, 1, 18) == "invalid_selection:"
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

local function service_project_path(args)
	local saved = active_notebook:get()
	local path = if type(args) == "table" and type(args.path) == "string" and args.path ~= "" then args.path else (saved.root_path or saved.path or "")
	if path == "" then
		return nil, { ok = false, error = { code = "selection_required", message = "Open a local project in Graphimo or provide its path" } }
	end
	return path, nil
end

zeron.tool({ name = "graphimo_inspect", run = function(args)
	args = args or {}
	local path, err = service_project_path(args)
	if err then return err end
	local saved = active_notebook:get()
	local url = args.url or saved.url or ""
	local session = args.session_id or saved.session_id or ""
	local request = start_tool(function()
		return Service.batch(zeron, path, {
			{ id = "scope", action = "scope.current", args = {} },
			{ id = "groups", action = "group.list", args = {} },
			{ id = "components", action = "component.list", args = {} },
			{ id = "controls", action = "control.catalog", args = {} },
			{ id = "metablocks", action = "metablock.list", args = {} },
		})
	end)
	if type(request) ~= "table" or type(request.after) ~= "function" then return request end
	return request:after(function(batch)
		local result = { project_path = path, graphimo = batch }
		if url == "" or session == "" then return result end
		return Bridge.inspect(zeron, url, "", session):after(function(notebook)
			if type(notebook) == "table" and notebook.ok == false then return notebook end
			result.notebook = notebook
			return result
		end)
	end)
end })

zeron.tool({ name = "graphimo_execute", run = function(args)
	local path, err = service_project_path(args or {})
	if err then return err end
	return start_tool(function()
		return Service.execute(zeron, path, args.action, args.args or {})
	end)
end })

zeron.tool({ name = "graphimo_group_from_selection", run = function(args)
	args = args or {}
	local path, err = service_project_path(args)
	if err then return err end
	local saved = active_notebook:get()
	local url = args.url or saved.url or ""
	local session = args.session_id or saved.session_id or ""
	if url == "" or session == "" then
		return { ok = false, error = { code = "selection_required", message = "Open a live marimo notebook in Graphimo before grouping cells" } }
	end
	if type(args.selected_ids) ~= "table" or #args.selected_ids < 2 or #args.selected_ids > 128 then
		return { ok = false, error = { code = "invalid_selection", message = "Provide between 2 and 128 selected marimo cell IDs" } }
	end
	return Bridge.inspect(zeron, url, "", session):after(function(graph)
		if type(graph) ~= "table" or graph.ok == false then return graph end
		return Service.execute(zeron, path, "group.create_from_selection", {
			name = args.name,
			module = args.module,
			cells = graph.cells or {},
			selected_ids = args.selected_ids,
		}):after(function(compiled)
			if type(compiled) ~= "table" or compiled.ok == false then return compiled end
			return Bridge.replace_group_selection(zeron, url, "", session, compiled):after(function(replaced)
				if type(replaced) ~= "table" or replaced.ok == false then
					if not is_group_selection_rejection(replaced) then return replaced end
					local ok, cleanup = pcall(function()
						return Service.execute(zeron, path, "group.delete", {
							group_id = compiled.group.group_id,
							expected_source_sha256 = compiled.group.source_sha256,
						})
					end)
					if not ok or cleanup == nil then return replaced end
					return cleanup:after(function() return replaced end, function() return replaced end)
				end
				return { ok = true, group = compiled.group, replacement = replaced, inputs = compiled.inputs, outputs = compiled.outputs }
			end)
		end)
	end)
end })

zeron.tool({ name = "graphimo_execute_batch", run = function(args)
	local path, err = service_project_path(args or {})
	if err then return err end
	return start_tool(function()
		return Service.batch(zeron, path, args.operations, args.continue_on_error == true)
	end)
end })

zeron.tool({ name = "graphimo_component_write", run = function(args)
	local path, err = service_project_path(args or {})
	if err then return err end
	return start_tool(function()
		return Service.execute(zeron, path, "component.write", {
			path = args.component_path,
			source = args.source,
		})
	end)
end })

zeron.tool({ name = "graphimo_validate", run = function(args)
	local path, err = service_project_path(args or {})
	if err then return err end
	return start_tool(function()
		return Service.execute(zeron, path, "component.validate", { path = args.component_path })
	end)
end })

zeron.tool({ name = "graphimo_events", run = function(args)
	local path, err = service_project_path(args or {})
	if err then return err end
	return start_tool(function()
		return Service.events(
			zeron,
			path,
			args.cursor or "0",
			args.limit or 128,
			args.wait_ms or 750
		)
	end)
end })

local Page = ui.component(function()
	local saved = active_notebook:peek()
	local url = ui.use_state(saved.url or "")
	local token = ui.use_state("")
	local session_id = ui.use_state(saved.session_id or "")
	local sessions = ui.use_state({})
	local project_root_path = ui.use_state(saved.root_path or saved.path or "")
	local group_scope_stack = ui.use_state({})
	local snapshot = ui.use_state({ notebook = {}, cells = {}, edges = {} })
	local notebook_snapshot = ui.use_state({ notebook = {}, cells = {}, edges = {} })
	local groups = ui.use_state({})
	local controls = ui.use_state({})
	local metablocks = ui.use_state({})
	local active_metablock = ui.use_state("")
	local active_metablock_scope = ui.use_state("root")
	local metablock_projection = ui.use_state(nil)
	local metablock_history = ui.use_state({ undo_depth = 0, redo_depth = 0, stale = false })
	local service_event_cursor = ui.use_state("0")
	local service_polling = ui.use_state(false)
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
	local selected_many = ui.use_state({})
	local group_dialog = ui.use_state(false)
	local group_name = ui.use_state("")
	local generated_source_open = ui.use_state(false)
	local zoom = ui.use_state(0.85)
	local pan_x = ui.use_state(24)
	local pan_y = ui.use_state(24)
	local grid = ui.use_state(true)
	local output_counter = ui.use_state(1)
	local notice = ui.use_state("")
	local canvas = ui.ref()
	local canvas_keyboard = ui.ref()
	local drag = nil
	local undo_stack, redo_stack = {}, {}
	local history_busy = false

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

	local refresh_graph
	local refresh_metablock
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
				positions:set({}); components:set({}); selected:set(""); selected_many:set({})
			end
			notice:set(if #items == 0 then "No active marimo notebooks found" else "Found " .. tostring(#items) .. " active notebook(s)")
			if session_id:peek() ~= "" then refresh_graph() end
		end, function(err) notice:set("Marimo connection failed: " .. tostring(err)) end)
	end

	refresh_graph = function()
		if active_metablock:peek() ~= "" and refresh_metablock then
			refresh_metablock()
			return
		end
		observe("Inspect marimo graph", function()
			return Bridge.inspect(zeron, url:peek(), token:peek(), session_id:peek())
		end, function(value)
			value.cells = MetaBlockProjection.annotate_parent_cells(value.cells)
			notebook_snapshot:set(value)
			snapshot:set(value)
			local ids = {}
			for _, cell in value.cells or {} do table.insert(ids, cell.id) end
			visible_cell_ids:set(ids)
			if value.notebook.session_id then session_id:set(value.notebook.session_id) end
			local path = value.notebook.path or ""
			local project_path = project_root_path:peek() ~= "" and project_root_path:peek() or path
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
				local next_selected, first_selected = {}, ""
				for id in selected_many:peek() do
					if active[id] then next_selected[id] = true; if first_selected == "" then first_selected = id end end
				end
				selected_many:set(next_selected)
				if not active[selected:peek()] then selected:set(first_selected) end
				notice:set("Showing " .. tostring(#value.cells) .. " marimo cells")
				observe("Discover components", function()
					return Bridge.list_components(zeron, url:peek(), token:peek(), value.notebook.session_id, project_path)
				end, function(component_result)
					local available = component_result.components or {}
					components:set(available)
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
				observe("Load Graphimo project", function()
					return Service.batch(zeron, project_path, {
						{ id = "groups", action = "group.list", args = {} },
						{ id = "controls", action = "control.catalog", args = {} },
						{ id = "metablocks", action = "metablock.list", args = {} },
						{ id = "scope", action = "scope.current", args = {} },
					})
				end, function(batch)
					local results = {}
					for _, item in batch.operations or {} do
						if item.status == "ok" then results[item.id] = item.result end
					end
					local breadcrumbs = (results.scope or {}).breadcrumbs or {}
					local restored_groups = {}
					for index = 2, #breadcrumbs do
						table.insert(restored_groups, breadcrumbs[index])
					end
					group_scope_stack:set(restored_groups)
					groups:set((results.groups or {}).items or {})
					controls:set((results.controls or {}).items or {})
					metablocks:set((results.metablocks or {}).items or {})
					local active_scope = (results.scope or {}).scope or {}
					local layout_scope = active_scope.scope_id or "root"
					observe("Load canvas layout", function()
						return Service.execute(zeron, project_path, "layout.get", { scope_id = layout_scope })
					end, function(layout)
						apply_layout(layout.layout or {})
					end, function(layout_error)
						log("Could not load scope layout: " .. tostring(layout_error), "error")
						apply_layout({})
					end)
				end, function(err)
					log("Graphimo service unavailable; loading legacy canvas layout: " .. tostring(err), "error")
					Layout.load(zeron, path, function(saved_positions, layout_error)
						if layout_error then log("Load canvas layout failed: " .. tostring(layout_error)) end
						apply_layout(saved_positions)
					end)
				end)
			else
				groups:set({}); metablocks:set({})
				apply_layout({})
			end
		end, function(err) notice:set("Could not inspect marimo: " .. tostring(err)) end)
	end

	local function save_positions()
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or snapshot:peek().notebook.path
		if not path or path == "" or not is_local_url(url:peek()) then return end
		local active_group = group_scope_stack:peek()[#group_scope_stack:peek()]
		local scope_id = if active_metablock:peek() ~= "" then MetaBlockProjection.scope_layout_id(active_metablock:peek(), active_metablock_scope:peek()) elseif active_group then active_group.scope_id else "root"
		observe("Save canvas layout", function()
			return Service.execute(zeron, path, "layout.set", { scope_id = scope_id, layout = positions:peek() })
		end, function() log("Canvas layout saved through Graphimo Core") end, function(err)
			if active_metablock:peek() ~= "" then
				local message = "MetaBlock layout save failed: " .. tostring(err)
				log(message, "error")
				notice:set(message)
				return
			end
			Layout.save(zeron, path, positions:peek(), function(ok, layout_error)
				if ok then log("Canvas layout saved through the legacy compatibility path") else
					local message = "Layout save failed: " .. tostring(layout_error or err)
					log(message, "error")
					notice:set(message)
				end
			end)
		end)
	end

	refresh_metablock = function()
		local metablock_id = active_metablock:peek()
		local scope = active_metablock_scope:peek()
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (notebook_snapshot:peek().notebook.path or active_notebook:peek().root_path or active_notebook:peek().path or "")
		if metablock_id == "" or path == "" then return end
		local layout_scope = MetaBlockProjection.scope_layout_id(metablock_id, scope)
		observe("Inspect PyTorch MetaBlock", function()
			return Service.batch(zeron, path, {
				{ id = "projection", action = "metablock.project", args = { metablock_id = metablock_id, scope = scope } },
				{ id = "layout", action = "layout.get", args = { scope_id = layout_scope } },
			})
		end, function(batch)
			local results = {}
			for _, item in batch.operations or {} do
				if item.status == "ok" then results[item.id] = item.result end
			end
			local projected, projection = MetaBlockProjection.to_canvas(
				metablock_id,
				scope,
				path,
				results.projection
			)
			metablock_projection:set(projection)
			metablock_history:set((results.projection or {}).history or { undo_depth = 0, redo_depth = 0, stale = false })
			local saved_layout = (results.layout or {}).layout or {}
			local merged, ids = {}, {}
			for index, cell in projected.cells do
				ids[cell.id] = true
				local saved_cell = saved_layout[cell.id] or {}
				merged[cell.id] = {
					x = saved_cell.x or (80 + ((index - 1) % 4) * 320),
					y = saved_cell.y or (80 + math.floor((index - 1) / 4) * 230),
					collapsed = saved_cell.collapsed or false,
				}
			end
			local visible = {}
			for _, cell in projected.cells do table.insert(visible, cell.id) end
			positions:set(merged)
			visible_cell_ids:set(visible)
			local next_selected, first_selected = {}, ""
			for id in selected_many:peek() do
				if ids[id] then next_selected[id] = true; if first_selected == "" then first_selected = id end end
			end
			selected_many:set(next_selected)
			if not ids[selected:peek()] then selected:set(first_selected) end
			snapshot:set(projected)
			notice:set(
				if projection
					then "Editing " .. tostring(projection.name or metablock_id) .. " · " .. scope
					else "MetaBlock addon unavailable; persisted model state is preserved"
			)
		end, function(err)
			notice:set("Could not inspect MetaBlock: " .. tostring(err))
		end)
	end

	local function open_metablock(metablock_id, scope)
		if active_metablock:peek() == "" then notebook_snapshot:set(snapshot:peek()) end
		active_metablock:set(metablock_id)
		active_metablock_scope:set(scope or "root")
		metablock_history:set({ undo_depth = 0, redo_depth = 0, stale = false })
		generated_source_open:set(false)
		selected:set("")
		selected_many:set({})
		refresh_metablock()
	end

	local function leave_metablock()
		active_metablock:set("")
		active_metablock_scope:set("root")
		metablock_projection:set(nil)
		metablock_history:set({ undo_depth = 0, redo_depth = 0, stale = false })
		generated_source_open:set(false)
		selected:set("")
		selected_many:set({})
		snapshot:set(notebook_snapshot:peek())
		refresh_graph()
	end

	local start_notebook

	local function group_notebook_path(relative_path)
		local root = project_root_path:peek()
		if root == "" or type(relative_path) ~= "string" or relative_path == "" then return nil end
		local parent = string.match(root, "^(.*[/\\])")
		if not parent then return nil end
		local relative = string.gsub(relative_path, "\\", "/")
		if string.sub(relative, 1, 1) == "/" or string.find(relative, "..", 1, true) then return nil end
		return parent .. relative
	end

	local function open_group(group)
		local path = project_root_path:peek()
		if path == "" then path = active_notebook:peek().root_path or active_notebook:peek().path or "" end
		if path == "" then notice:set("Open a local project before entering a Group Block"); return end
		observe("Enter Group Block " .. tostring(group.name or group.group_id), function()
			return Service.execute(zeron, path, "scope.enter", { container_id = group.group_id })
		end, function(scope_state)
			local breadcrumbs = scope_state.breadcrumbs or {}
			local next_groups = {}
			for index = 2, #breadcrumbs do table.insert(next_groups, breadcrumbs[index]) end
			local active = breadcrumbs[#breadcrumbs]
			local notebook_path = active and group_notebook_path(active.path)
			if not notebook_path then
				observe("Restore Group Block scope", function()
					return Service.execute(zeron, path, "scope.leave", {})
				end, function() notice:set("Could not resolve the Group Block notebook path") end, function(err)
					notice:set("Could not restore the Graphimo scope: " .. tostring(err))
				end)
				return
			end
			project_root_path:set(path)
			group_scope_stack:set(next_groups)
			start_notebook(notebook_path, true)
		end, function(err)
			notice:set("Could not enter Group Block: " .. tostring(err))
		end)
	end

	local function leave_group(on_left)
		local stack = group_scope_stack:peek()
		if #stack == 0 then return end
		local path = project_root_path:peek()
		observe("Leave Group Block", function()
			return Service.execute(zeron, path, "scope.leave", {})
		end, function(scope_state)
			local breadcrumbs = scope_state.breadcrumbs or {}
			local next_groups = {}
			for index = 2, #breadcrumbs do table.insert(next_groups, breadcrumbs[index]) end
			local active = breadcrumbs[#breadcrumbs]
			local notebook_path = if #next_groups == 0 then path else group_notebook_path(active and active.path)
			if not notebook_path then
				notice:set("Could not resolve the parent Group Block notebook path")
				return
			end
			group_scope_stack:set(next_groups)
			start_notebook(notebook_path, true, on_left)
		end, function(err)
			notice:set("Could not leave Group Block: " .. tostring(err))
		end)
	end

	local function leave_to_group_depth(depth)
		if #group_scope_stack:peek() <= depth then return end
		leave_group(function() leave_to_group_depth(depth) end)
	end

	local function apply_metablock_intent(intent)
		local metablock_id = active_metablock:peek()
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (notebook_snapshot:peek().notebook.path or "")
		if metablock_id == "" or path == "" then return end
		intent.scope = active_metablock_scope:peek()
		observe("Update PyTorch MetaBlock", function()
			return Service.execute(zeron, path, "metablock.apply", { metablock_id = metablock_id, intent = intent })
		end, function(value)
			if type(value) == "table" and type(value.history) == "table" then metablock_history:set(value.history) end
			refresh_metablock()
		end, function(err)
			notice:set("MetaBlock update failed: " .. tostring(err))
		end)
	end

	local function create_metablock()
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (snapshot:peek().notebook.path or "")
		if path == "" then notice:set("Open a local notebook before creating a MetaBlock"); return end
		output_counter:set(output_counter:peek() + 1)
		local metablock_id = "model_" .. tostring(os.time()) .. "_" .. tostring(output_counter:peek())
		observe("Create PyTorch MetaBlock", function()
			return Service.execute(zeron, path, "metablock.create", {
				dialect_id = "pytorch.model",
				metablock_id = metablock_id,
				request = { name = "PyTorch Model" },
			})
		end, function(created)
			local next = table.clone(metablocks:peek())
			table.insert(next, {
				metablock_id = created.metablock_id,
				dialect_id = created.dialect_id,
				addon_version = created.addon_version,
			})
			metablocks:set(next)
			open_metablock(created.metablock_id, "root")
		end, function(err)
			notice:set("Could not create PyTorch MetaBlock: " .. tostring(err))
		end)
	end

	local function materialize_metablock()
		local metablock_id = active_metablock:peek()
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (notebook_snapshot:peek().notebook.path or "")
		if metablock_id == "" or path == "" or session_id:peek() == "" then
			notice:set("Open a live local marimo notebook before inserting a MetaBlock definition")
			return
		end
		observe("Build PyTorch MetaBlock definition", function()
			return Service.execute(zeron, path, "metablock.materialize", {
				metablock_id = metablock_id,
				bindings = {},
			})
		end, function(definition)
			if type(definition.source) ~= "string" or definition.source == "" then
				notice:set("Graphimo returned no generated PyTorch definition")
				return
			end
			observe("Insert PyTorch definition into marimo", function()
				return Bridge.add_cell(zeron, url:peek(), token:peek(), session_id:peek(), definition.source)
			end, function()
				log("Inserted generated PyTorch definition for " .. metablock_id)
				leave_metablock()
			end, function(err)
				notice:set("Could not insert the generated definition: " .. tostring(err))
			end)
		end, function(err)
			notice:set("Could not materialize PyTorch MetaBlock: " .. tostring(err))
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
					current.root_path = project_root_path:peek()
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

	start_notebook = function(path, nested, on_started)
		if not nested then
			project_root_path:set(path)
			group_scope_stack:set({})
		end
		active_metablock:set("")
		active_metablock_scope:set("root")
		metablock_projection:set(nil)
		service_event_cursor:set("0")
		notice:set("Starting marimo for " .. path .. "…")
		log("Opening notebook " .. path)
		observe("Start marimo", function()
			return Bridge.open_notebook(zeron, path)
		end, function(started)
			url:set(started.url)
			session_id:set(started.session_id or "")
			local current = {
				url = started.url,
				session_id = started.session_id or "",
				path = path,
				root_path = project_root_path:peek(),
				log_path = started.log_path or "",
			}
			active_notebook:set(current)
			if started.started then log("Started marimo process " .. tostring(started.pid)) else log("Reused existing marimo process") end
			if started.session_id and started.session_id ~= "" then
				refresh_graph()
			else
				zeron.open_url(started.url)
				log("Opened marimo editor; waiting for its live notebook session")
				wait_for_notebook(path, started.url, 0)
			end
			if on_started then on_started() end
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

	local function read_service_events()
		local path = snapshot:peek().notebook.path or active_notebook:peek().path or ""
		if path == "" then notice:set("Open a local project to inspect Graphimo events"); return end
		observe("Read Graphimo Core events", function()
			return Service.events(zeron, path, service_event_cursor:peek(), 128, 0)
		end, function(result)
			service_event_cursor:set(result.cursor or service_event_cursor:peek())
			for _, event in result.events or {} do
				log("Core event " .. tostring(event.sequence) .. " · " .. tostring(event.kind or "event") .. " · " .. tostring(event.action or event.code or event.metablock_id or ""))
			end
			if (result.dropped or 0) > 0 then
				log("Core event history dropped " .. tostring(result.dropped) .. " old event(s)", "warn")
			end
		end, function(err)
			notice:set("Could not read Graphimo Core events: " .. tostring(err))
		end)
	end

	local function append_service_events(result)
		service_event_cursor:set(result.cursor or service_event_cursor:peek())
		for _, event in result.events or {} do
			log("Core event " .. tostring(event.sequence) .. " · " .. tostring(event.kind or "event") .. " · " .. tostring(event.action or event.code or event.metablock_id or ""))
		end
		if (result.dropped or 0) > 0 then
			log("Core event history dropped " .. tostring(result.dropped) .. " old event(s)", "warn")
		end
	end

	local function poll_service_events()
		if not service_polling:peek() then return end
		local path = snapshot:peek().notebook.path or active_notebook:peek().path or ""
		if path == "" then return end
		local ok, request = pcall(function()
			return Service.events(zeron, path, service_event_cursor:peek(), 128, 750)
		end)
		if not ok or request == nil then
			ui.after(1500, poll_service_events)
			return
		end
		request:after(function(result)
			if type(result) == "table" and result.ok ~= false then append_service_events(result) end
			if service_polling:peek() then ui.after(100, poll_service_events) end
		end, function(err)
			if service_polling:peek() then
				log("Graphimo event polling failed: " .. tostring(err), "warn")
				ui.after(1500, poll_service_events)
			end
		end)
	end

	local function toggle_logs()
		local next_value = not logs_open:peek()
		logs_open:set(next_value)
		service_polling:set(next_value)
		if next_value then poll_service_events() end
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
		style.border_color = function() return if selected_many:get()[id] then "$accent" else "$border.strong" end
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

	local function select_cell(id, additive)
		if additive then
			local many = table.clone(selected_many:peek())
			many[id] = if many[id] then nil else true
			selected_many:set(many)
			if many[id] then
				selected:set(id)
			elseif selected:peek() == id then
				local next_id = ""
				for candidate in many do next_id = candidate; break end
				selected:set(next_id)
			end
		else
			selected_many:set({ [id] = true })
			selected:set(id)
		end
	end

	local function selected_ids()
		local many = selected_many:peek()
		local ids = {}
		for _, cell in snapshot:peek().cells or {} do
			if many[cell.id] then table.insert(ids, cell.id) end
		end
		return ids
	end

	local function clone_positions(value)
		local copy = {}
		for id, position in value or {} do copy[id] = table.clone(position) end
		return copy
	end

	local function default_cell_position(index)
		return { x = 80 + ((index - 1) % 4) * 320, y = 80 + math.floor((index - 1) / 4) * 230 }
	end

	local function history_context()
		local group_stack = group_scope_stack:peek()
		local active_group = group_stack[#group_stack]
		local scope = if active_metablock:peek() ~= "" then MetaBlockProjection.scope_layout_id(active_metablock:peek(), active_metablock_scope:peek()) elseif active_group then active_group.scope_id else "root"
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (snapshot:peek().notebook.path or "")
		return table.concat({ path, session_id:peek(), scope }, "\n")
	end

	local function push_history(action)
		action.context = history_context()
		table.insert(undo_stack, action)
		if #undo_stack > 50 then table.remove(undo_stack, 1) end
		table.clear(redo_stack)
	end

	local function records_for_ids(ids, cells)
		local records, by_id = {}, {}
		for _, cell in cells or cells_now() do by_id[cell.id] = cell end
		for _, id in ids do
			local cell = by_id[id]
			if cell and type(cell.source) == "string" then
				local point = positions:peek()[id] or { x = 80, y = 80 }
				table.insert(records, { id = id, source = cell.source, x = point.x or 80, y = point.y or 80 })
			end
		end
		return records
	end

	local function delete_history_ids(ids, index, deleted, callback)
		index = index or 1
		deleted = deleted or {}
		if index > #ids then callback(deleted); return end
		local id = ids[index]
		observe("Remove block for history", function()
			return Bridge.delete(zeron, url:peek(), token:peek(), session_id:peek(), id)
		end, function()
			table.insert(deleted, id)
			delete_history_ids(ids, index + 1, deleted, callback)
		end, function(err)
			callback(deleted, err)
		end)
	end

	local function restore_history_records(records, callback)
		if #records == 0 then callback({}); return end
		local source_records = {}
		for _, record in records do table.insert(source_records, { source = record.source }) end
		observe("Restore blocks for history", function()
			return Bridge.restore_cells(zeron, url:peek(), token:peek(), session_id:peek(), source_records)
		end, function(value)
			if type(value) ~= "table" or type(value.cell_ids) ~= "table" or #value.cell_ids ~= #records then
				callback({}, "marimo returned an incomplete history restore result")
				return
			end
			local restored = {}
			for index, record in records do
				local id = value.cell_ids[index]
				if type(id) ~= "string" or id == "" then callback({}, "marimo returned an invalid restored cell ID"); return end
				table.insert(restored, { id = id, x = record.x or 80, y = record.y or 80 })
			end
			callback(restored)
		end, function(err)
			callback({}, err)
		end)
	end

	local function move_history(source_stack, destination_stack, undoing)
		if history_busy then return true end
		local action = table.remove(source_stack)
		if not action then
			notice:set(if undoing then "Nothing to undo" else "Nothing to redo")
			return true
		end
		if action.context ~= history_context() then
			table.insert(source_stack, action)
			notice:set("This history entry belongs to another notebook or scope")
			return true
		end
		if action.kind == "layout" then
			positions:set(clone_positions(if undoing then action.before else action.after))
			save_positions()
			table.insert(destination_stack, action)
			notice:set(if undoing then "Undid block movement" else "Redid block movement")
			return true
		end
		if action.kind == "edit" then
			history_busy = true
			local expected_source = if undoing then action.after else action.before
			local replacement_source = if undoing then action.before else action.after
			observe(if undoing then "Undo source edit" else "Redo source edit", function()
				return Bridge.edit(zeron, url:peek(), token:peek(), session_id:peek(), action.cell_id, expected_source, replacement_source)
			end, function()
				history_busy = false
				table.insert(destination_stack, action)
				notice:set(if undoing then "Undid the connection change" else "Redid the connection change")
				refresh_graph()
			end, function(err)
				history_busy = false
				table.insert(source_stack, action)
				notice:set("Could not update source history: " .. tostring(err))
			end)
			return true
		end
		history_busy = true
		local target_records = if undoing then action.before_records else action.after_records
		local previous_ids = action.current_ids or {}
		delete_history_ids(previous_ids, 1, {}, function(deleted, delete_error)
			if delete_error then
				history_busy = false
				table.insert(source_stack, action)
				notice:set("Could not update block history: " .. tostring(delete_error))
				return
			end
			local next_positions = clone_positions(positions:peek())
			for _, id in deleted do next_positions[id] = nil end
			positions:set(next_positions)
		restore_history_records(target_records, function(restored, restore_error)
				if restore_error then
					-- Remove any partial target restoration before rebuilding the original side.
					local partial_ids = {}
					for _, item in restored do table.insert(partial_ids, item.id) end
					delete_history_ids(partial_ids, 1, {}, function(removed_partial)
						local partial_positions = clone_positions(positions:peek())
						for _, id in removed_partial do partial_positions[id] = nil end
						positions:set(partial_positions)
						local rollback_records = if undoing then action.after_records else action.before_records
						restore_history_records(rollback_records, function(rollback, rollback_error)
							local rollback_positions = clone_positions(positions:peek())
						for _, item in rollback do rollback_positions[item.id] = { x = item.x, y = item.y } end
							positions:set(rollback_positions)
							action.current_ids = {}
							for _, item in rollback do table.insert(action.current_ids, item.id) end
							history_busy = false
							table.insert(source_stack, action)
							notice:set("History restore failed: " .. tostring(restore_error) .. (if rollback_error then "; rollback failed: " .. tostring(rollback_error) else ""))
							refresh_graph()
						end)
					end)
					return
				end
				local ids, restored_positions = {}, clone_positions(positions:peek())
				for _, item in restored do
					table.insert(ids, item.id)
					restored_positions[item.id] = { x = item.x, y = item.y }
				end
				positions:set(restored_positions)
				action.current_ids = ids
				history_busy = false
				if #ids > 0 then
					selected:set(ids[#ids]); local many = {}; for _, id in ids do many[id] = true end; selected_many:set(many)
				else
					selected:set(""); selected_many:set({})
				end
				save_positions()
				table.insert(destination_stack, action)
				notice:set(if undoing then "Undid the last Graphimo change" else "Redid the Graphimo change")
				refresh_graph()
			end)
		end)
		return true
	end

	local delete_selected
	local function move_history_for_active_scope(action, label)
		local metablock_id = active_metablock:peek()
		if metablock_id == "" then return false end
		if history_busy then return true end
		if metablock_history:peek().stale then
			notice:set("MetaBlock history is stale because its saved graph changed outside this session")
			return true
		end
		history_busy = true
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (notebook_snapshot:peek().notebook.path or "")
		observe(label, function()
			return Service.execute(zeron, path, action, { metablock_id = metablock_id, scope = active_metablock_scope:peek() })
		end, function(value)
			history_busy = false
			metablock_history:set(value.history or { undo_depth = 0, redo_depth = 0, stale = false })
			local idle_message = if action == "metablock.undo" then "Nothing to undo in this MetaBlock" else "Nothing to redo in this MetaBlock"
			notice:set(if value.changed == false then idle_message else label .. " completed")
			refresh_metablock()
		end, function(err)
			history_busy = false
			notice:set(label .. " failed: " .. tostring(err))
		end)
		return true
	end
	local function undo_canvas()
		if move_history_for_active_scope("metablock.undo", "Undo MetaBlock change") then return true end
		return move_history(undo_stack, redo_stack, true)
	end
	local function redo_canvas()
		if move_history_for_active_scope("metablock.redo", "Redo MetaBlock change") then return true end
		return move_history(redo_stack, undo_stack, false)
	end

	local function on_canvas_key(e)
		local key = string.lower(tostring(e.key or ""))
		local command = e.cmd == true or e.meta == true or e.ctrl == true
		if command and key == "z" then
			if e.shift then redo_canvas() else undo_canvas() end
			return true
		elseif command and key == "y" then
			redo_canvas()
			return true
		elseif key == "delete" or key == "backspace" then
			if #selected_ids() == 0 then return false end
			delete_selected()
			return true
		end
		return false
	end

	local function selected_set()
		return selected_many:peek()
	end

	local function on_press(e)
		local keyboard_target = canvas_keyboard:peek()
		if keyboard_target then keyboard_target:focus() end
		local x, y = Canvas.point(e, zoom:peek(), pan_x:peek(), pan_y:peek())
		local current_cells = snapshot:peek().cells or {}
		local ps = positions:peek()
		for index = #current_cells, 1, -1 do
			local cell = current_cells[index]
			local pos = ps[cell.id] or { x = 0, y = 0 }
			for _, variable in cell.defs or {} do
				if Canvas.near(x, y, pos.x + Canvas.width, pos.y + Canvas.port(cell, variable, true), 12 / zoom:peek()) then
					select_cell(cell.id, e.shift == true); drag = { kind = "wire", cell_id = cell.id, variable = variable }; return
				end
			end
			for _, variable in cell.refs or {} do
				if Canvas.near(x, y, pos.x, pos.y + Canvas.port(cell, variable, false), 12 / zoom:peek()) then select_cell(cell.id, e.shift == true); return end
			end
			local height = Canvas.height(cell, base_summaries:peek()[cell.id])
			if x >= pos.x and x <= pos.x + Canvas.width and y >= pos.y and y <= pos.y + height then
				select_cell(cell.id, e.shift == true)
				local origins = {}
				for _, moving_id in selected_ids() do
					local origin = ps[moving_id]
					if origin then origins[moving_id] = { x = origin.x or 0, y = origin.y or 0 } end
				end
				if not origins[cell.id] then origins[cell.id] = { x = pos.x, y = pos.y } end
				drag = { kind = "node", x = x, y = y, origins = origins, before_positions = clone_positions(ps) }
				return
			end
		end
		if not e.shift then
			selected_many:set({})
			selected:set("")
		end
		drag = { kind = "pan", x = e.local_x or 0, y = e.local_y or 0, px = pan_x:peek(), py = pan_y:peek() }
	end

	local function on_move(e)
		if not drag then return end
		if drag.kind == "pan" then
			pan_x:set(drag.px + (e.local_x or 0) - drag.x); pan_y:set(drag.py + (e.local_y or 0) - drag.y)
		elseif drag.kind == "node" then
			local x, y = Canvas.point(e, zoom:peek(), pan_x:peek(), pan_y:peek())
			local next = table.clone(positions:peek())
			for id, origin in drag.origins do
				local position = table.clone(next[id] or {})
				position.x, position.y = origin.x + x - drag.x, origin.y + y - drag.y
				next[id] = position
			end
			positions:set(next)
		end
	end

	local function on_release(e)
		if not drag then return end
		if drag.kind == "node" then
			local before = drag.before_positions or {}
			local after = clone_positions(positions:peek())
			local changed = false
			for id, point in after do
				local old = before[id]
				if not old or old.x ~= point.x or old.y ~= point.y then changed = true; break end
			end
			if changed then push_history({ kind = "layout", before = before, after = after }) end
			save_positions()
		elseif drag.kind == "wire" then
			local x, y = Canvas.point(e, zoom:peek(), pan_x:peek(), pan_y:peek())
			local connected = false
			for _, cell in snapshot:peek().cells or {} do
				local pos = positions:peek()[cell.id] or { x = 0, y = 0 }
				for _, ref in cell.refs or {} do
					if cell.id ~= drag.cell_id and Canvas.near(x, y, pos.x, pos.y + Canvas.port(cell, ref, false), 14 / zoom:peek()) then
						connected = true
						if active_metablock:peek() ~= "" then
							apply_metablock_intent({
								type = "connect",
								source = { element_id = drag.cell_id, port = drag.variable },
								target = { element_id = cell.id, port = ref },
							})
						else
							observe("Connect " .. drag.variable .. " to " .. ref, function()
								return Bridge.connect(zeron, url:peek(), token:peek(), session_id:peek(), drag.cell_id, drag.variable, cell.id, ref)
							end, function(value)
								if type(cell.source) == "string" and type(value.source) == "string" and cell.source ~= value.source then
									push_history({ kind = "edit", cell_id = cell.id, before = cell.source, after = value.source })
								end
								notice:set("Python source updated; marimo owns the new dependency")
								refresh_graph()
							end, function(err) notice:set("Connection refused: " .. tostring(err) .. " · Open code to edit it") end)
						end
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

	local function start_group_from_selection()
		local ids = selected_ids()
		if #ids < 2 then
			notice:set("Shift-click at least two blocks, then right-click to create a Group Block")
			return
		end
		if active_metablock:peek() ~= "" then
			notice:set("Group Blocks can be created from normal marimo cells")
			return
		end
		local name = string.gsub(group_name:peek(), "^%s*(.-)%s*$", "%1")
		if name == "" then notice:set("Enter a Group Block name"); return end
		local module = string.lower(name)
		module = string.gsub(module, "[^%w_]+", "_")
		module = string.gsub(module, "^_+", "")
		module = string.gsub(module, "_+$", "")
		if module == "" then module = "group" end
		if string.match(module, "^%d") then module = "group_" .. module end
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or snapshot:peek().notebook.path or ""
		if path == "" then notice:set("Open a project notebook before creating a Group Block"); return end
		local selected_positions = positions:peek()
		local left, top = math.huge, math.huge
		for _, id in ids do
			local point = selected_positions[id] or { x = 0, y = 0 }
			left = math.min(left, point.x or 0)
			top = math.min(top, point.y or 0)
		end
		observe("Inspect selected marimo cells", function()
			return Bridge.inspect(zeron, url:peek(), token:peek(), session_id:peek())
		end, function(graph)
			observe("Create Group Block definition", function()
				return Service.execute(zeron, path, "group.create_from_selection", {
					name = name, module = module, cells = graph.cells or {}, selected_ids = ids,
				})
			end, function(compiled)
				observe("Replace selected cells with Group Block", function()
					return Bridge.replace_group_selection(zeron, url:peek(), token:peek(), session_id:peek(), compiled)
				end, function(replaced)
					group_dialog:set(false)
					local before_records = records_for_ids(ids, graph.cells or {})
					local next_positions = table.clone(positions:peek())
					for _, id in ids do next_positions[id] = nil end
					next_positions[replaced.cell_id] = { x = left, y = top }
					positions:set(next_positions)
					push_history({
						kind = "cells",
						before_records = before_records,
						after_records = { { source = replaced.source or compiled.parent_source, x = left, y = top } },
						current_ids = { replaced.cell_id },
					})
					selected_many:set({ [replaced.cell_id] = true })
					selected:set(replaced.cell_id)
					local stack = group_scope_stack:peek()
					local active_scope = stack[#stack]
					local scope_id = if active_scope then active_scope.scope_id else "root"
					observe("Save Group Block layout", function()
						return Service.execute(zeron, path, "layout.set", { scope_id = scope_id, layout = next_positions })
					end, function()
						notice:set("Created Group Block " .. tostring(compiled.group.name) .. " with " .. tostring(#ids) .. " cells")
						refresh_graph()
					end, function(err)
						notice:set("Group Block created, but its canvas position could not be saved: " .. tostring(err))
						refresh_graph()
					end)
				end, function(err)
					if is_group_selection_rejection(err) then
						observe("Remove unused Group definition", function()
							return Service.execute(zeron, path, "group.delete", {
								group_id = compiled.group.group_id,
								expected_source_sha256 = compiled.group.source_sha256,
							})
						end, function()
							notice:set("Selected cells changed before they could be grouped. The unused Group was removed; refresh and retry.")
						end, function(cleanup_error)
							notice:set("Selected cells changed; Graphimo could not remove the unused Group definition: " .. tostring(cleanup_error))
						end)
					else
						notice:set("Could not replace selected cells: " .. tostring(err))
					end
				end)
			end, function(err)
				notice:set("Could not create Group Block: " .. tostring(err))
			end)
		end, function(err)
			notice:set("Could not inspect selected cells: " .. tostring(err))
		end)
	end

	local function block_at(at)
		local x, y = Canvas.point(at, zoom:peek(), pan_x:peek(), pan_y:peek())
		local current_cells = snapshot:peek().cells or {}
		local ps = positions:peek()
		for index = #current_cells, 1, -1 do
			local cell = current_cells[index]
			local point = ps[cell.id] or { x = 0, y = 0 }
			if x >= point.x and x <= point.x + Canvas.width and y >= point.y and y <= point.y + Canvas.height(cell, base_summaries:peek()[cell.id]) then
				return cell
			end
		end
		return nil
	end

	local function canvas_menu(at)
		if session_id:peek() == "" or active_metablock:peek() ~= "" then return {} end
		local hit = block_at(at)
		if hit and not selected_many:peek()[hit.id] then select_cell(hit.id, false) end
		if #selected_ids() < 2 then return {} end
		return {
			{ label = "Create Group Block…", on_select = function()
				group_name:set("New group")
				group_dialog:set(true)
			end },
		}
	end

	local function run_selected()
		if active_metablock:peek() ~= "" then
			notice:set("Select a notebook cell to run; MetaBlock execution materializes through the application API")
			return
		end
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
		if active_metablock:peek() ~= "" then
			notice:set("Select a notebook cell to open in marimo")
			return
		end
		local cell = selected_cell()
		if not cell then notice:set("Select a cell first"); return end
		zeron.open_url(url:peek())
		notice:set("Opened the marimo editor for cell " .. cell.id)
		log("Opened marimo editor for cell " .. cell.id)
	end

	delete_selected = function()
		local ids = selected_ids()
		if #ids == 0 then notice:set("Select a cell first"); return end
		if active_metablock:peek() ~= "" then
			local cell = selected_cell()
			if not cell then notice:set("Select a cell first"); return end
			apply_metablock_intent({ type = "delete_element", element_id = cell.id })
			selected:set("")
			return
		end
		local records, original_positions = records_for_ids(ids), clone_positions(positions:peek())
		delete_history_ids(ids, 1, {}, function(deleted, err)
			if #deleted > 0 then
				local deleted_set, deleted_records = {}, {}
				for _, id in deleted do deleted_set[id] = true end
				for _, record in records do if deleted_set[record.id] then table.insert(deleted_records, record) end end
				local next_positions = clone_positions(original_positions)
				for _, id in deleted do next_positions[id] = nil end
				positions:set(next_positions)
				push_history({ kind = "cells", before_records = deleted_records, after_records = {}, current_ids = {} })
				selected:set(""); selected_many:set({}); save_positions(); refresh_graph()
			end
			if err then notice:set("Could not delete every selected cell: " .. tostring(err)) end
		end)
	end

	local function add_control(kind)
		if session_id:peek() == "" then notice:set("Open a live marimo notebook before adding a control"); return end
		if active_metablock:peek() ~= "" then notice:set("Controls are available in normal Python scopes"); return end
		if not is_local_url(url:peek()) then notice:set("Graphimo Controls require a local Graphimo service"); return end
		output_counter:set(output_counter:peek() + 1)
		local identity = kind .. "_" .. tostring(os.time()) .. "_" .. tostring(output_counter:peek())
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (snapshot:peek().notebook.path or "")
		observe("Compile " .. kind .. " control", function()
			return Service.execute(zeron, path, "control.create", { kind = kind, identity = identity })
		end, function(compiled)
			observe("Insert " .. kind .. " control", function()
				return Bridge.add_cell(zeron, url:peek(), token:peek(), session_id:peek(), compiled.control_source)
			end, function(control_cell)
				local control_point = default_cell_position(#cells_now() + 1)
				observe("Create reactive value for " .. compiled.label, function()
					return Bridge.add_cell(zeron, url:peek(), token:peek(), session_id:peek(), compiled.value_source)
				end, function(value_cell)
					if type(control_cell) == "table" and type(control_cell.cell_id) == "string" and type(value_cell) == "table" and type(value_cell.cell_id) == "string" then
						local value_point = default_cell_position(#cells_now() + 2)
						local next_positions = clone_positions(positions:peek())
						next_positions[control_cell.cell_id] = control_point
						next_positions[value_cell.cell_id] = value_point
						positions:set(next_positions)
						push_history({ kind = "cells", before_records = {}, after_records = {
							{ source = compiled.control_source, x = control_point.x, y = control_point.y },
							{ source = compiled.value_source, x = value_point.x, y = value_point.y },
						}, current_ids = { control_cell.cell_id, value_cell.cell_id } })
						save_positions()
					end
					notice:set(compiled.label .. " added; connect its value output " .. compiled.value_name)
					refresh_graph()
				end, function(value_error)
					if type(control_cell) == "table" and type(control_cell.cell_id) == "string" then
						push_history({ kind = "cells", before_records = {}, after_records = {
							{ source = compiled.control_source, x = control_point.x, y = control_point.y },
						}, current_ids = { control_cell.cell_id } })
					end
					notice:set("Reactive value creation failed: " .. tostring(value_error) .. "; the inserted control can be undone")
				end)
			end, function(err)
				notice:set("Could not insert " .. compiled.label .. ": " .. tostring(err))
			end)
		end, function(err)
			notice:set("Could not compile " .. kind .. " control: " .. tostring(err))
		end)
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
		local defined = {}
		for _, cell in cells_now() do for _, name in cell.defs or {} do defined[name] = true end end
		local next_number = output_counter:peek()
		local out = callable.name .. "_result_" .. tostring(next_number)
		while defined[out] do next_number += 1; out = callable.name .. "_result_" .. tostring(next_number) end
		output_counter:set(next_number + 1)
		observe("Add component " .. item.id .. "::" .. callable.name, function()
			return Bridge.add_component(zeron, url:peek(), token:peek(), session_id:peek(), item.id, callable.name, out, bindings)
		end, function(value)
			if type(value) == "table" and type(value.cell_id) == "string" and type(value.source) == "string" then
				local point = default_cell_position(#cells_now() + 1)
				local next_positions = clone_positions(positions:peek())
				next_positions[value.cell_id] = point
				positions:set(next_positions)
				push_history({ kind = "cells", before_records = {}, after_records = { { source = value.source, x = point.x, y = point.y } }, current_ids = { value.cell_id } })
				save_positions()
			end
			notice:set("Inserted ordinary Python into the marimo notebook")
			refresh_graph()
		end, function(err) notice:set("Could not add component: " .. tostring(err)) end)
	end

	local function component_library_panel()
		local component_groups = ui.computed(function()
			return Library.project(components:get(), library_query:get())
		end)
		local model_methods = ui.computed(function()
			local view = metablock_projection:get()
			return view and view.method_tabs or {}
		end)
		local model_groups = ui.computed(function()
			local view = metablock_projection:get()
			return view and view.groups or {}
		end)
		local model_palette = ui.computed(function()
			local view = metablock_projection:get()
			return view and view.palette or {}
		end)
		local function copy_generated_source()
			local projection = metablock_projection:peek()
			local source = projection and projection.generated_source
			if type(source) ~= "string" or source == "" then
				notice:set("Generated Python is unavailable while the MetaBlock has errors")
				return
			end
			local ok, err = pcall(ui.clipboard.write, source)
			if ok then notice:set("Copied generated Python for revision " .. string.sub(tostring(projection.graph_revision or "unknown"), 1, 12))
			else notice:set("Could not copy generated Python: " .. tostring(err)) end
		end
		return ui.column { w = 250, h_full = true, shrink = 0, gap = "$space.2", p = "$space.2", rounded = "$radius.lg", bg = "$zeron.secondary.bg", overflow = "auto",
			ui.show(function() return active_metablock:get() == "" end, ui.column { w_full = true, gap = "$space.2",
				Text { "Controls", style = "heading" },
				ui.each(controls, function(control)
					local labels = {
						slider = "Slider", range_slider = "Range Slider", number = "Number Input",
						dropdown = "Select", checkbox = "Checkbox", text = "Text Input", run_button = "Run Trigger",
					}
					return Button {
						labels[control.kind] or control.kind,
						tone = "secondary", size = "sm", w_full = true,
						disabled = function() return session_id:get() == "" end,
						on_click = function() add_control(control.kind) end,
					}
				end),
				Text { "MetaBlocks", style = "heading" },
				Button {
					"Create PyTorch MetaBlock",
					tone = "secondary", size = "sm", w_full = true,
					disabled = function() return (snapshot:get().notebook.path or "") == "" end,
					on_click = create_metablock,
				},
				ui.show(function() return #metablocks:get() == 0 end, Text { "No saved MetaBlocks", style = "caption", tone = "secondary" }),
				ui.each(metablocks, function(item)
					return Button {
						(item.dialect_id or "MetaBlock") .. " · " .. item.metablock_id,
						tone = "secondary", size = "sm", w_full = true,
						on_click = function() open_metablock(item.metablock_id, "root") end,
					}
				end),
				ui.show(function() return #groups:get() > 0 end, ui.column { w_full = true, gap = "$space.1",
					Text { "Reusable Groups", style = "heading" },
					ui.each(groups, function(group)
						return Button {
							"Open · " .. tostring(group.name or group.group_id),
							tone = "secondary", size = "sm", w_full = true,
							disabled = function() return session_id:get() == "" end,
							on_click = function() open_group(group) end,
						}
					end),
				}),
				Text { "Components", style = "heading" },
				SearchField { value = library_query, placeholder = "Search components", name = "Search components", size = "sm", quiet = true, w_full = true },
				ui.show(function() return #component_groups:get() == 0 end, Text { function()
					return if #components:get() == 0 then "No components found in this project's components/ folder" else "No components match this search"
				end, style = "caption", tone = "secondary", wrap = true }),
				ui.each(component_groups, function(group)
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
						Button { "Add", tone = "secondary", size = "sm", disabled = function() return session_id:get() == "" or active_metablock:get() ~= "" end, on_click = function() add_selected_component(item, callable) end },
										}
									end),
								}),
							}
						end),
					}
				end),
			}),
			ui.show(function() return active_metablock:get() ~= "" end, ui.column { w_full = true, gap = "$space.2",
				Button { "← Project canvas", tone = "secondary", size = "sm", w_full = true, on_click = leave_metablock },
				Text { function()
					local view = metablock_projection:get()
					return if view then tostring(view.name or "PyTorch MetaBlock") .. " · " .. active_metablock_scope:get() else "MetaBlock"
				end, style = "heading", wrap = true },
				Button {
					"Insert generated definition",
					tone = "primary", size = "sm", w_full = true,
					disabled = function() return session_id:get() == "" or (not is_local_url(url:get())) end,
					on_click = materialize_metablock,
				},
				ui.row { w_full = true, items = "center", gap = "$space.1",
					Button {
						function() return if generated_source_open:get() then "Hide generated Python" else "Inspect generated Python" end,
						tone = "secondary", size = "sm", grow = 1,
						disabled = function() return metablock_projection:get() == nil end,
						on_click = function() generated_source_open:set(not generated_source_open:peek()) end,
					},
					Button { "Copy", tone = "secondary", size = "sm", disabled = function()
						local view = metablock_projection:get()
						return not view or type(view.generated_source) ~= "string" or view.generated_source == ""
					end, on_click = copy_generated_source },
				},
				ui.show(generated_source_open, ui.column { w_full = true, max_h = 360, min_h = 0, overflow = "auto", gap = "$space.1", p = "$space.2", rounded = "$radius.md", bg = "$zeron.primary.bg",
					Text { function()
						local view = metablock_projection:get()
						return "Generated Python · revision " .. string.sub(tostring(view and view.graph_revision or "unknown"), 1, 12)
					end, style = "caption", tone = "secondary" },
					ui.show(function()
						local view = metablock_projection:get()
						return not view or type(view.generated_source) ~= "string" or view.generated_source == ""
					end, Text { "Source is unavailable until the MetaBlock graph validates.", style = "caption", tone = "secondary", wrap = true }),
					ui.show(function()
						local view = metablock_projection:get()
						return view ~= nil and type(view.generated_source) == "string" and view.generated_source ~= ""
					end, Markdown { text = function()
						local view = metablock_projection:get()
						local source = view and view.generated_source
						if type(source) ~= "string" or source == "" then return "" end
						return "```python\n" .. source .. (string.sub(source, -1) == "\n" and "" or "\n") .. "```"
					end }),
				}),
				ui.each(model_methods, function(method)
					return Button {
						method.name, tone = if active_metablock_scope:get() == method.name then "primary" else "secondary",
						size = "sm", w_full = true,
						on_click = function() active_metablock_scope:set(method.name); selected:set(""); refresh_metablock() end,
					}
				end),
				ui.each(model_groups, function(group)
					return Button {
						"Group · " .. group.name,
						tone = if active_metablock_scope:get() == "group:" .. group.id then "primary" else "secondary",
						size = "sm", w_full = true,
						on_click = function() active_metablock_scope:set("group:" .. group.id); selected:set(""); refresh_metablock() end,
					}
				end),
				Text { "Components", style = "heading" },
				ui.each(model_palette, function(item)
					return Button {
						item.kind, tone = "secondary", size = "sm", w_full = true,
						on_click = function()
							output_counter:set(output_counter:peek() + 1)
							apply_metablock_intent({
								type = "add_element", kind = item.kind,
								id = item.kind .. "_" .. tostring(os.time()) .. "_" .. tostring(output_counter:peek()),
							})
						end,
					}
				end),
			}),
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
			Button { function() return if logs_open:get() then "Terminal −" else "Terminal +" end, tone = "secondary", size = "sm", on_click = toggle_logs },
		},
		ui.show(function() return #group_scope_stack:get() > 0 and active_metablock:get() == "" end, ui.row {
			w_full = true, items = "center", gap = "$space.1",
			Button { "Project", tone = "secondary", size = "sm", on_click = function() leave_to_group_depth(0) end },
			ui.each(group_scope_stack, function(item, index)
				return Button {
					item.name or item.container_id or "Group",
					tone = if index == #group_scope_stack:get() then "primary" else "secondary",
					size = "sm",
					on_click = function() leave_to_group_depth(index) end,
				}
			end),
		}),
		ui.row { w_full = true, gap = "$space.2", items = "center",
			Button { "Run", size = "sm", disabled = function() return selected:get() == "" or active_metablock:get() ~= "" end, on_click = run_selected },
			Button { "Open code", tone = "secondary", size = "sm", disabled = function() return selected:get() == "" or active_metablock:get() ~= "" end, on_click = open_selected },
			Button { "Delete", tone = "secondary", size = "sm", disabled = function() return selected:get() == "" end, on_click = delete_selected },
			Button { "Undo", tone = "secondary", size = "sm", disabled = function()
				if active_metablock:get() ~= "" then local history = metablock_history:get(); return history_busy or history.stale or history.undo_depth == 0 end
				return #undo_stack == 0 or history_busy
			end, on_click = undo_canvas },
			Button { "Redo", tone = "secondary", size = "sm", disabled = function()
				if active_metablock:get() ~= "" then local history = metablock_history:get(); return history_busy or history.stale or history.redo_depth == 0 end
				return #redo_stack == 0 or history_busy
			end, on_click = redo_canvas },
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
			ContextMenu {
				items = canvas_menu,
				label = "Graphimo canvas actions",
				absolute = true, left = 0, top = 0, right = 0, bottom = 0,
			ui.canvas { absolute = true, left = 0, top = 0, w_full = true, h_full = true, draw = function() end,
				ref = canvas_keyboard,
				focusable = true,
				on_key = on_canvas_key,
				on_mount = function(el)
					el:focus()
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
				Text { function() return tostring(#problem_entries:get()) .. " saved problems · " .. tostring(#log_entries:get()) .. " saved events" end, style = "caption", tone = "secondary", grow = 1 },
				Button { "Read marimo log", tone = "secondary", size = "sm", on_click = refresh_marimo_log },
				Button { "Read Graphimo events", tone = "secondary", size = "sm", on_click = read_service_events },
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
		Dialog {
			open = group_dialog,
			title = "Create Group Block",
			body = "Shift-click blocks to select them. The selected cells become one reusable Group Block; marimo keeps the data dependencies between this Group and the surrounding notebook.",
			confirm = { label = "Create Group Block", on_click = start_group_from_selection, disabled = function() return group_name:get() == "" or #selected_ids() < 2 end },
			TextField { value = group_name, label = "Name", placeholder = "Clean and scale", w_full = true, autofocus = true },
		},
	}
end)

zeron.page({ id = "canvas", title = "Graphimo", icon = "share", build = function() return Page {} end })
