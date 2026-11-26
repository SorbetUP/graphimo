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

local function project_path_for(args)
	local saved = active_notebook:get()
	return if type(args) == "table" and type(args.path) == "string" and args.path ~= "" then args.path else (saved.root_path or saved.path or "")
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
	local path = project_path_for(args or {})
	if path == "" then return { ok = false, error = { code = "selection_required", message = "Open a local Graphimo project first" } } end
	return start_tool(function() return Service.execute(zeron, path, "notebook.inspect", {}) end)
end })

zeron.tool({ name = "list_components", run = function(args)
	local path = project_path_for(args or {})
	if path == "" then return { ok = false, error = { code = "selection_required", message = "Open a local Graphimo project first" } } end
	return start_tool(function() return Service.execute(zeron, path, "component.list", {}) end)
end })

zeron.tool({ name = "add_component", run = function(args)
	local path = project_path_for(args or {})
	if path == "" then return { ok = false, error = { code = "selection_required", message = "Open a local Graphimo project first" } } end
	return start_tool(function() return Service.execute(zeron, path, "component.add", {
		path = args.component_id, callable = args.callable, output_name = args.output_name, bindings = args.bindings or {},
	}) end)
end })

zeron.tool({ name = "connect", run = function(args)
	local path = project_path_for(args or {})
	if path == "" then return { ok = false, error = { code = "selection_required", message = "Open a local Graphimo project first" } } end
	return start_tool(function() return Service.execute(zeron, path, "notebook.connect", {
		from_cell_id = args.from_cell_id, variable = args.variable, output_index = args.output_index, to_cell_id = args.to_cell_id, input_ref = args.input_ref,
	}) end)
end })

zeron.tool({ name = "run", run = function(args)
	local path = project_path_for(args or {})
	if path == "" then return { ok = false, error = { code = "selection_required", message = "Open a local Graphimo project first" } } end
	return start_tool(function() return Service.execute(zeron, path, "notebook.run", { cell_id = args.cell_id }) end)
end })

zeron.tool({ name = "open_code", run = function(args)
	local url, _, session = config_for(args or {})
	if args.session_id then session = args.session_id end
	if url == "" then return { ok = false, error = { code = "selection_required", message = "Open a notebook in Graphimo first" } } end
	zeron.open_url(string.gsub(url, "/+$", ""))
	return { ok = true, opened = true, session_id = session, cell_id = args.cell_id }
end })

local function service_project_path(args)
	local path = project_path_for(args)
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
			{ id = "addons", action = "addon.list", args = {} },
		})
	end)
	if type(request) ~= "table" or type(request.after) ~= "function" then return request end
	return request:after(function(batch)
		local result = { project_path = path, graphimo = batch }
		if url == "" or session == "" then return result end
		return Service.execute(zeron, path, "notebook.inspect", {}):after(function(notebook)
			if type(notebook) == "table" and notebook.ok == false then return notebook end
			result.notebook = notebook
			if not is_local_url(url) then return result end
			return Service.telemetry_config(zeron, path, url, "", session):after(function(config)
				return Bridge.configure_telemetry(zeron, url, "", session, config):after(function()
					return result
				end)
			end)
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
	return Service.execute(zeron, path, "notebook.inspect", {}):after(function(graph)
		if type(graph) ~= "table" or graph.ok == false then return graph end
		return Service.execute(zeron, path, "group.create_from_selection", {
			name = args.name,
			module = args.module,
			cells = graph.cells or {},
			selected_ids = args.selected_ids,
		}):after(function(compiled)
			if type(compiled) ~= "table" or compiled.ok == false then return compiled end
			return Service.execute(zeron, path, "notebook.replace_group_selection", {
				selected_ids = compiled.selected_ids,
				source_hashes = compiled.source_hashes,
				parent_source = compiled.parent_source,
			}):after(function(replaced)
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
	local addon_dialects = ui.use_state({})
	local active_metablock = ui.use_state("")
	local active_metablock_scope = ui.use_state("root")
	local metablock_projection = ui.use_state(nil)
	local metablock_history = ui.use_state({ undo_depth = 0, redo_depth = 0, stale = false })
	local metablock_revision = ui.use_state("")
	local metablock_parameter_dialog = ui.use_state(false)
	local metablock_parameter_target = ui.use_state({})
	local metablock_parameter_value = ui.use_state("")
	local metablock_event_cursor = ui.use_state("0")
	local metablock_event_entries = ui.use_state({})
	local metablock_events_open = ui.use_state(false)
	local checkpoint_artifact_id = ui.use_state("")
	local checkpoint_details = ui.use_state(nil)
	local checkpoint_busy = ui.use_state(false)
	local checkpoint_panel_open = ui.use_state(false)
	local checkpoint_request_sequence = 0
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
	local inspector_open = ui.use_state(false)
	local show_technical = ui.use_state(false)
	local literal_target = ui.use_state("")
	local literal_value = ui.use_state("")
	local canvas = ui.ref()
	local canvas_keyboard = ui.ref()
	local drag = nil
	local undo_stack, redo_stack = {}, {}
	local history_busy = false

	local function cells_now()
		return snapshot:get().cells or {}
	end

	-- Cell roles drive the canvas projection: technical cells stay reachable
	-- in developer mode but do not clutter the main pipeline view.
	local function cell_role(cell)
		if type(cell) ~= "table" then return "ordinary_python" end
		if type(cell.graphimo_instance) == "table" then return "node_instance" end
		local source = type(cell.source) == "string" and cell.source or ""
		if string.find(source, "# Graphimo node library:", 1, true) then return "library" end
		if string.find(source, "# Graphimo control:", 1, true) then return "control" end
		if string.find(source, "# Graphimo group:", 1, true) then return "group" end
		return "ordinary_python"
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

	local function visible_positioned_cells()
		local visible = visible_cell_ids:peek()
		local positioned = {}
		for _, cell in positioned_cells() do
			if visible[cell.id] then table.insert(positioned, cell) end
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
			local project_path = project_root_path:peek() ~= "" and project_root_path:peek() or (active_notebook:peek().root_path or active_notebook:peek().path or "")
			return Service.execute(zeron, project_path, "notebook.inspect", {})
		end, function(value)
			local metablock_labels = {}
			for _, item in metablocks:get() or {} do
				metablock_labels[item.metablock_id] = item.dialect_id or "MetaBlock"
			end
			value.cells = MetaBlockProjection.annotate_parent_cells(value.cells, metablock_labels)
			notebook_snapshot:set(value)
			snapshot:set(value)
			local ids = {}
			for _, cell in value.cells or {} do
				if show_technical:peek() or cell_role(cell) ~= "library" then table.insert(ids, cell.id) end
			end
			visible_cell_ids:set(ids)
			if value.notebook.session_id then session_id:set(value.notebook.session_id) end
			local path = value.notebook.path or ""
			local project_path = project_root_path:peek() ~= "" and project_root_path:peek() or path
			local telemetry_session = value.notebook.session_id or session_id:peek()
			if is_local_url(url:peek()) and telemetry_session ~= "" then
				observe("Configure kernel telemetry", function()
					return Service.telemetry_config(zeron, project_path, url:peek(), token:peek(), telemetry_session)
				end, function(config)
					observe("Attach kernel telemetry", function()
						return Bridge.configure_telemetry(zeron, url:peek(), token:peek(), telemetry_session, config)
					end, function()
						log("Attached asynchronous kernel telemetry for marimo session " .. telemetry_session)
					end, function(err)
						log("Could not attach kernel telemetry: " .. tostring(err), "error")
					end)
				end, function(err)
					log("Could not obtain ephemeral telemetry credentials: " .. tostring(err), "error")
				end)
			end
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
					return Service.execute(zeron, project_path, "component.list", {})
				end, function(component_result)
					local available = component_result.components or component_result.items or {}
					components:set(available)
					observe("Check component sources", function()
						return Service.execute(zeron, project_path, "component.refresh_instances", {})
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
						{ id = "addons", action = "addon.list", args = {} },
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
					local dialects = {}
					for _, addon in (results.addons or {}).addons or {} do
						local caps = addon.capabilities or {}
						local has_metablock = false
						for _, cap in caps do if cap == "metablock" then has_metablock = true end end
						if has_metablock then
							for _, dialect_id in addon.dialects or {} do
								table.insert(dialects, { dialect_id = dialect_id, label = addon.id .. " / " .. dialect_id })
							end
						end
					end
					addon_dialects:set(dialects)
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
		observe("Inspect MetaBlock", function()
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
			metablock_revision:set((results.projection or {}).revision or "")
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
		checkpoint_request_sequence += 1
		if active_metablock:peek() == "" then notebook_snapshot:set(snapshot:peek()) end
		active_metablock:set(metablock_id)
		active_metablock_scope:set(scope or "root")
		metablock_history:set({ undo_depth = 0, redo_depth = 0, stale = false })
		metablock_event_cursor:set("0")
		metablock_event_entries:set({})
		metablock_events_open:set(false)
		checkpoint_artifact_id:set("")
		checkpoint_details:set(nil)
		checkpoint_busy:set(false)
		checkpoint_panel_open:set(false)
		generated_source_open:set(false)
		selected:set("")
		selected_many:set({})
		refresh_metablock()
	end

	local function leave_metablock()
		checkpoint_request_sequence += 1
		active_metablock:set("")
		active_metablock_scope:set("root")
		metablock_projection:set(nil)
		metablock_history:set({ undo_depth = 0, redo_depth = 0, stale = false })
		metablock_event_cursor:set("0")
		metablock_event_entries:set({})
		metablock_events_open:set(false)
		checkpoint_artifact_id:set("")
		checkpoint_details:set(nil)
		checkpoint_busy:set(false)
		checkpoint_panel_open:set(false)
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

	local function apply_metablock_intent(intent, on_success)
		local metablock_id = active_metablock:peek()
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (notebook_snapshot:peek().notebook.path or "")
		if metablock_id == "" or path == "" then return end
		intent.scope = active_metablock_scope:peek()
		local expected = metablock_revision:peek()
		if expected == "" then expected = nil end
		observe("Update MetaBlock", function()
			return Service.execute(zeron, path, "metablock.apply", {
				metablock_id = metablock_id,
				intent = intent,
				scope = active_metablock_scope:peek(),
				expected_revision = expected,
			})
		end, function(value)
			if type(value) == "table" and type(value.history) == "table" then metablock_history:set(value.history) end
			refresh_metablock()
			if on_success then on_success(value) end
		end, function(err)
			notice:set("MetaBlock update failed: " .. tostring(err))
		end)
	end

	local function read_metablock_events()
		local metablock_id = active_metablock:peek()
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (notebook_snapshot:peek().notebook.path or "")
		if metablock_id == "" or path == "" then return end
		observe("Read MetaBlock events", function()
			return Service.execute(zeron, path, "metablock.events", {
				metablock_id = metablock_id, cursor = metablock_event_cursor:peek(), limit = 128,
			})
		end, function(result)
			if active_metablock:peek() ~= metablock_id then return end
			metablock_event_cursor:set(result.cursor or metablock_event_cursor:peek())
			local next_events = table.clone(metablock_event_entries:peek())
			for _, event in result.events or {} do table.insert(next_events, event) end
			while #next_events > 50 do table.remove(next_events, 1) end
			metablock_event_entries:set(next_events)
			local count = #(result.events or {})
			if count == 0 then notice:set("No new MetaBlock events are available in the Graphimo service")
			else notice:set("Read " .. tostring(count) .. " MetaBlock event(s)") end
		end, function(err)
			if active_metablock:peek() ~= metablock_id then return end
			notice:set("Could not read MetaBlock events: " .. tostring(err))
		end)
	end

	local function inspect_checkpoint()
		local metablock_id = active_metablock:peek()
		local artifact_id = string.gsub(checkpoint_artifact_id:peek(), "^%s*(.-)%s*$", "%1")
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (notebook_snapshot:peek().notebook.path or "")
		if metablock_id == "" or path == "" then return end
		if artifact_id == "" or #artifact_id > 256 then notice:set("Enter a valid Graphimo Artifact ID"); return end
		checkpoint_request_sequence += 1
		local request_sequence = checkpoint_request_sequence
		checkpoint_busy:set(true)
		observe("Inspect checkpoint", function()
			return Service.execute(zeron, path, "metablock.invoke", {
				metablock_id = metablock_id,
				action = "checkpoint.inspect",
				args = { artifact_id = artifact_id },
			})
		end, function(value)
			if checkpoint_request_sequence ~= request_sequence then return end
			checkpoint_busy:set(false)
			if active_metablock:peek() ~= metablock_id then return end
			checkpoint_details:set(value)
		end, function(err)
			if checkpoint_request_sequence ~= request_sequence then return end
			checkpoint_busy:set(false)
			if active_metablock:peek() ~= metablock_id then return end
			notice:set("Could not inspect checkpoint: " .. tostring(err))
		end)
	end

	local function preview_checkpoint_migration()
		local details = checkpoint_details:peek()
		local view = metablock_projection:peek()
		local metablock_id = active_metablock:peek()
		local expected_revision = view and view.graph_revision
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (notebook_snapshot:peek().notebook.path or "")
		if not details or not view or metablock_id == "" or path == "" then return end
		checkpoint_request_sequence += 1
		local request_sequence = checkpoint_request_sequence
		local external_values = {}
		for _, parameter in view.external_parameters or {} do external_values[parameter.id] = parameter.default end
		checkpoint_busy:set(true)
		observe("Preview checkpoint migration", function()
			return Service.execute(zeron, path, "metablock.invoke", {
				metablock_id = metablock_id,
				action = "checkpoint.preview_migration",
				args = { artifact_id = details.artifact_id, external_values = external_values },
			})
		end, function(value)
			if checkpoint_request_sequence ~= request_sequence then return end
			checkpoint_busy:set(false)
			if active_metablock:peek() ~= metablock_id then return end
			local updated = table.clone(details)
			updated.migration_preview = value
			updated.migration_preview_revision = expected_revision
			checkpoint_details:set(updated)
		end, function(err)
			if checkpoint_request_sequence ~= request_sequence then return end
			checkpoint_busy:set(false)
			if active_metablock:peek() ~= metablock_id then return end
			notice:set("Could not preview checkpoint migration: " .. tostring(err))
		end)
	end

	local function restore_checkpoint(allow_migration)
		local details = checkpoint_details:peek()
		local metablock_id = active_metablock:peek()
		local artifact_id = string.gsub(checkpoint_artifact_id:peek(), "^%s*(.-)%s*$", "%1")
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (notebook_snapshot:peek().notebook.path or "")
		if metablock_id == "" or path == "" then return end
		if artifact_id == "" or #artifact_id > 256 then notice:set("Enter a valid Graphimo Artifact ID"); return end
		local view = metablock_projection:peek()
		local runtime_id = nil
		local selector = view and view.runtime_selector
		if type(selector) == "table" and type(selector.items) == "table" and #selector.items > 0 then
			runtime_id = selector.items[1].runtime_instance_id
		end
		local args = { artifact_id = artifact_id, restore_training_state = true, strict = not allow_migration }
		if type(runtime_id) == "string" and runtime_id ~= "" then args.runtime_instance_id = runtime_id end
		checkpoint_request_sequence += 1
		local request_sequence = checkpoint_request_sequence
		checkpoint_busy:set(true)
		observe("Restore checkpoint", function()
			return Service.execute(zeron, path, "metablock.invoke", {
				metablock_id = metablock_id,
				action = "checkpoint.restore",
				args = args,
			})
		end, function(value)
			if checkpoint_request_sequence ~= request_sequence then return end
			checkpoint_busy:set(false)
			if active_metablock:peek() ~= metablock_id then return end
			local updated = table.clone(details or {})
			updated.last_restore = value
			checkpoint_details:set(updated)
			notice:set("Checkpoint restored (runtime " .. tostring(value and value.runtime_instance_id or "new") .. ")")
			refresh_metablock()
		end, function(err)
			if checkpoint_request_sequence ~= request_sequence then return end
			checkpoint_busy:set(false)
			if active_metablock:peek() ~= metablock_id then return end
			notice:set("Could not restore checkpoint: " .. tostring(err))
		end)
	end

	local addon_ui_states = {}

	local function addon_ui_state(key, default)
		local state = addon_ui_states[key]
		if state == nil then
			state = ui.use_state(default)
			addon_ui_states[key] = state
		end
		return state
	end

	local function invoke_addon_ui_action(panel, action)
		local metablock_id = active_metablock:peek()
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (notebook_snapshot:peek().notebook.path or "")
		if metablock_id == "" or path == "" then return end
		local args = {}
		for _, field in panel.fields or {} do
			local key = tostring(panel.id) .. "/" .. tostring(field.id)
			local state = addon_ui_states[key]
			local value = state and state:peek() or field.default
			if field.type == "integer" or field.type == "number" then
				local number = tonumber(value)
				if number == nil then notice:set("Field " .. tostring(field.label or field.id) .. " expects a number"); return end
				if field.type == "integer" and number ~= math.floor(number) then
					notice:set("Field " .. tostring(field.label or field.id) .. " expects an integer"); return
				end
				value = number
			elseif field.type == "boolean" then
				value = value == true
			end
			args[tostring(field.id)] = value
		end
		observe(tostring(action.label or action.id), function()
			return Service.execute(zeron, path, "metablock.invoke", {
				metablock_id = metablock_id,
				action = action.invoke,
				args = args,
			})
		end, function()
			notice:set(tostring(action.label or action.id) .. " done")
			refresh_metablock()
		end, function(err)
			notice:set(tostring(action.label or action.id) .. " failed: " .. tostring(err))
		end)
	end

	local function render_addon_ui_panels()
		local view = metablock_projection:get()
		local contribution = view and view.ui
		-- ui.show requires a ui node, never nil: render an empty column when the
		-- active MetaBlock declares no declarative UI.
		if type(contribution) ~= "table" then return ui.column {} end
		return ui.column { w_full = true, gap = "$space.2",
			ui.each(contribution.panels or {}, function(panel)
				return ui.column { w_full = true, gap = "$space.1", p = "$space.2", rounded = "$radius.md", bg = "$zeron.secondary.bg",
					Text { tostring(panel.title or panel.id), style = "label" },
					ui.each(panel.fields or {}, function(field)
						local key = tostring(panel.id) .. "/" .. tostring(field.id)
						local default = field.default
						if default == nil and field.type == "boolean" then default = false end
						if default == nil then default = "" end
						local state = addon_ui_state(key, default)
						if field.type == "boolean" then
							return Button {
								tostring(field.label or field.id) .. ": " .. (state:peek() == true and "on" or "off"),
								tone = "secondary", size = "sm", w_full = true,
								on_click = function() state:set(state:peek() ~= true) end,
							}
						end
						return TextField { value = state, label = tostring(field.label or field.id), placeholder = tostring(field.default or ""), w_full = true }
					end),
					ui.each(panel.actions or {}, function(action)
						return Button {
							tostring(action.label or action.id),
							tone = "secondary", size = "sm", w_full = true,
							on_click = function() invoke_addon_ui_action(panel, action) end,
						}
					end),
				}
			end),
		}
	end

	local function format_checkpoint_migration_report(report)
		if type(report) ~= "table" then return "Migration report unavailable" end
		local function shape_text(shape)
			if type(shape) ~= "table" then return "?" end
			local dimensions = {}
			for _, dimension in shape do table.insert(dimensions, tostring(dimension)) end
			return "(" .. table.concat(dimensions, " x ") .. ")"
		end
		local sections = {}
		local function add_names(label, values)
			values = if type(values) == "table" then values else {}
			local lines = { label .. " (" .. tostring(#values) .. ")" }
			for index = 1, math.min(#values, 6) do
				local name = tostring(values[index])
				if #name > 140 then name = string.sub(name, 1, 137) .. "..." end
				table.insert(lines, "  · " .. name)
			end
			if #values > 6 then table.insert(lines, "  · and " .. tostring(#values - 6) .. " more") end
			table.insert(sections, table.concat(lines, "\n"))
		end
		add_names("Copied", report.copied)
		add_names("Missing in checkpoint", report.missing_in_source)
		add_names("Unused checkpoint weights", report.missing_in_target)
		local mismatches = if type(report.shape_mismatch) == "table" then report.shape_mismatch else {}
		local mismatch_lines = { "Shape mismatches (" .. tostring(#mismatches) .. ")" }
		for index = 1, math.min(#mismatches, 6) do
			local mismatch = mismatches[index]
			local name = tostring(mismatch.name or "unknown")
			if #name > 100 then name = string.sub(name, 1, 97) .. "..." end
			table.insert(mismatch_lines, "  · " .. name .. " " .. shape_text(mismatch.source_shape) .. " -> " .. shape_text(mismatch.target_shape))
		end
		if #mismatches > 6 then table.insert(mismatch_lines, "  · and " .. tostring(#mismatches - 6) .. " more") end
		table.insert(sections, table.concat(mismatch_lines, "\n"))
		return table.concat(sections, "\n")
	end

	local function format_checkpoint_metadata(metadata)
		if type(metadata) ~= "table" or next(metadata) == nil then return "Saved metadata: none" end
		local ok, encoded = pcall(ui.json.encode, metadata)
		if not ok or type(encoded) ~= "string" then return "Saved metadata: unavailable" end
		if #encoded > 1000 then encoded = string.sub(encoded, 1, 997) .. "..." end
		return "Saved metadata: " .. encoded
	end

	local function open_metablock_parameter_editor(target)
		local value = target.value
		if type(value) == "table" then
			local ok, encoded = pcall(ui.json.encode, value)
			value = if ok then encoded else ""
		elseif value == nil then
			value = ""
		else
			value = tostring(value)
		end
		metablock_parameter_target:set(target)
		metablock_parameter_value:set(value)
		metablock_parameter_dialog:set(true)
	end

	local function parse_metablock_parameter_value(raw, parameter)
		local kind = parameter.type
		if kind == "int" or kind == "integer" then
			local number = tonumber(raw)
			if not number or number % 1 ~= 0 then error("Enter a whole number for " .. parameter.name) end
			return number
		elseif kind == "float" or kind == "number" or kind == "optional_float" then
			if raw == "" and kind == "optional_float" then return nil end
			local number = tonumber(raw)
			if not number then error("Enter a number for " .. parameter.name) end
			return number
		elseif kind == "optional_str" then
			if raw == "" then return nil end
			return raw
		elseif kind == "bool" or kind == "boolean" then
			local normalized = string.lower(string.gsub(raw, "^%s*(.-)%s*$", "%1"))
			if normalized == "true" then return true end
			if normalized == "false" then return false end
			error("Enter true or false for " .. parameter.name)
		elseif kind == "shape" or kind == "json" then
			local ok, decoded = pcall(ui.json.decode, raw)
			if not ok then error("Enter valid JSON for " .. parameter.name) end
			return decoded
		end
		return raw
	end

	local function submit_metablock_parameter()
		local target = metablock_parameter_target:peek()
		local ok, value = pcall(parse_metablock_parameter_value, metablock_parameter_value:peek(), target)
		if not ok then notice:set(tostring(value)); return end
		if type(value) == "number" and target.minimum ~= nil and value < target.minimum then
			notice:set(tostring(target.name) .. " must be at least " .. tostring(target.minimum))
			return
		end
		if type(value) == "number" and target.maximum ~= nil and value > target.maximum then
			notice:set(tostring(target.name) .. " must be at most " .. tostring(target.maximum))
			return
		end
		if target.choices then
			local found = false
			for _, choice in target.choices do if choice == value then found = true end end
			if not found then
				notice:set("Choose one of the supported values: " .. table.concat(target.choices, ", "))
				return
			end
		end
		local intent
		if target.external then
			intent = { type = "set_model_parameter", name = target.id, value = value }
		elseif target.group_repeat then
			intent = { type = "set_group_repeat", scope = active_metablock_scope:peek(), instance_id = target.element_id, ["repeat"] = value }
		elseif target.rename_element then
			intent = { type = "rename_element", scope = active_metablock_scope:peek(), element_id = target.element_id, name = value }
		else
			intent = {
				type = "set_element_parameter",
				scope = active_metablock_scope:peek(),
				element_id = target.element_id,
				parameter = target.name,
				value = value,
			}
		end
		apply_metablock_intent(intent, function() metablock_parameter_dialog:set(false) end)
	end

	local function selected_metablock_element()
		local view = metablock_projection:get()
		for _, element in view and view.elements or {} do
			if element.id == selected:get() then return element end
		end
		return nil
	end

	local function create_metablock(dialect_id, label)
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (snapshot:peek().notebook.path or "")
		if path == "" then notice:set("Open a local notebook before creating a MetaBlock"); return end
		if type(dialect_id) ~= "string" or dialect_id == "" then
			notice:set("No MetaBlock addon is installed (addon.list reports no 'metablock' capability)")
			return
		end
		output_counter:set(output_counter:peek() + 1)
		local metablock_id = "block_" .. tostring(os.time()) .. "_" .. tostring(output_counter:peek())
		observe("Create MetaBlock (" .. dialect_id .. ")", function()
			return Service.execute(zeron, path, "metablock.create", {
				dialect_id = dialect_id,
				metablock_id = metablock_id,
				request = { name = label or dialect_id },
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
			notice:set("Could not create MetaBlock: " .. tostring(err))
		end)
	end

	local function import_metablock_state()
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (snapshot:peek().notebook.path or "")
		if path == "" then notice:set("Open a local notebook before importing a MetaBlock"); return end
		ui.open_files({
			title = "Import Graphimo MetaBlock state",
			filters = { { "Graphimo MetaBlock state or source", { "json", "py" } } },
		}):after(function(files)
			local file = files and files[1]
			if not file then return end
			ui.read_file(file):after(function(source)
				if type(source) ~= "string" then
					notice:set("Could not read the selected MetaBlock file")
					return
				end
				if #source > 1024 * 1024 then
					notice:set("Selected MetaBlock state exceeds the 1 MiB import limit")
					return
				end
				local dialects = addon_dialects:peek()
				if #dialects == 0 then
					notice:set("No MetaBlock addon is installed; cannot import a state file")
					return
				end
				local dialect = dialects[1]
				output_counter:set(output_counter:peek() + 1)
				local metablock_id = "block_" .. tostring(os.time()) .. "_" .. tostring(output_counter:peek())
				observe("Import Graphimo MetaBlock state", function()
					return Service.execute(zeron, path, "metablock.create", {
						dialect_id = dialect.dialect_id,
						metablock_id = metablock_id,
						request = { semantic_state_json = source },
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
					notice:set("Could not import MetaBlock state: " .. tostring(err))
				end)
			end, function(err)
				notice:set("Could not read the selected MetaBlock file: " .. tostring(err))
			end)
		end, function(err)
			notice:set("Could not choose a MetaBlock file: " .. tostring(err))
		end)
	end

	local function materialize_metablock()
		local metablock_id = active_metablock:peek()
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (notebook_snapshot:peek().notebook.path or "")
		if metablock_id == "" or path == "" or session_id:peek() == "" then
			notice:set("Open a live local marimo notebook before inserting a MetaBlock definition")
			return
		end
		observe("Materialize MetaBlock definition", function()
			return Service.execute(zeron, path, "metablock.materialize", {
				metablock_id = metablock_id,
				bindings = {},
			})
		end, function(definition)
			-- The service inserts and runs the generated cell itself; "current"
			-- means the cell really exists in the notebook.
			if definition.materialization_state ~= "current" or type(definition.cell_id) ~= "string" then
				notice:set("MetaBlock materialization did not produce a live cell (no kernel attached?)")
				return
			end
			log("Materialized " .. metablock_id .. " into cell " .. definition.cell_id)
			leave_metablock()
		end, function(err)
			notice:set("Could not materialize MetaBlock: " .. tostring(err))
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
		local function input_labels(value)
			local labels = {}
			if type(value) == "table" then
				for _, name in Canvas.input_ports(value) do table.insert(labels, Canvas.input_label(value, name)) end
			end
			return labels
		end
		local function output_labels(value)
			local labels = {}
			if type(value) == "table" then
				for _, key in Canvas.output_ports(value) do table.insert(labels, Canvas.output_label(value, key)) end
			end
			return labels
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
				Text { function()
					local value = cell()
					local role = cell_role(value)
					if role == "ordinary_python" or role == "node_instance" then return "" end
					return role
				end, style = "caption", tone = "secondary" },
				Text { function()
					local value = cell()
					local dependency = value and value.graphimo_dependency_state
					if dependency and dependency.state == "disabled" then return "disabled" end
					if dependency and dependency.state == "blocked" then return "stale / blocked" end
					if dependency and dependency.state == "component_unavailable" then return "component unavailable" end
					return value and value.status or "unknown"
				end, style = "caption", tone = function()
					local value = cell()
					local dependency = value and value.graphimo_dependency_state
					if dependency and dependency.state == "blocked" then return "danger" end
					if dependency and dependency.state == "disabled" then return "secondary" end
					return if value and (value.status == "success" or value.status == "idle") then "success" elseif value and (value.status == "error" or value.status == "exception" or value.status == "marimo-error") then "danger" else "secondary"
				end },
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
					local dependency = value.graphimo_dependency_state
					local stale = dependency and dependency.state == "blocked"
					if preview == "" then return (if stale then "STALE · " else "") .. tostring(mime) end
					return (if stale then "STALE · " else "") .. tostring(mime) .. " · " .. tostring(preview)
				end
				if value.output and value.output ~= "None" then return tostring(value.output) end
				return value.errors and value.errors[1] and value.errors[1].message or ""
			end, style = "caption", tone = "secondary", wrap = true },
			ui.row { w_full = true, grow = 1, items = "stretch", gap = "$space.2",
				ui.column { w = 82, gap = "$space.1", justify = "start",
					Text { function() local value = cell(); return ports(input_labels(value), "● ", "") end, style = "caption", tone = "secondary", wrap = true },
				},
				ui.div { grow = 1 },
				ui.column { w = 82, gap = "$space.1", items = "end",
					Text { function() local value = cell(); return ports(output_labels(value), "", " ●") end, style = "caption", tone = "primary", wrap = true },
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
			local path = project_root_path:peek() ~= "" and project_root_path:peek() or (snapshot:peek().notebook.path or "")
			return Service.execute(zeron, path, "notebook.delete", { cell_id = id })
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
		local path = project_root_path:peek() ~= "" and project_root_path:peek() or (snapshot:peek().notebook.path or "")
		return Service.execute(zeron, path, "notebook.restore_cells", { records = source_records })
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
				local path = project_root_path:peek() ~= "" and project_root_path:peek() or (snapshot:peek().notebook.path or "")
				return Service.execute(zeron, path, "notebook.edit_guarded", {
					cell_id = action.cell_id,
					expected_source = expected_source,
					source = replacement_source,
				})
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
			for _, key in Canvas.output_ports(cell) do
				if Canvas.near(x, y, pos.x + Canvas.width, pos.y + Canvas.port(cell, key, true), 12 / zoom:peek()) then
					select_cell(cell.id, e.shift == true)
					local instance = if type(cell.graphimo_instance) == "table" then cell.graphimo_instance else nil
					drag = {
						kind = "wire",
						cell_id = cell.id,
						port = key,
						variable = if instance then instance.result_var else key,
						output_index = if instance then tonumber(key) else nil,
					}
					return
				end
			end
			for _, name in Canvas.input_ports(cell) do
				if Canvas.near(x, y, pos.x, pos.y + Canvas.port(cell, name, false), 12 / zoom:peek()) then select_cell(cell.id, e.shift == true); return end
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
				for _, name in Canvas.input_ports(cell) do
					if cell.id ~= drag.cell_id and Canvas.near(x, y, pos.x, pos.y + Canvas.port(cell, name, false), 14 / zoom:peek()) then
						connected = true
						if active_metablock:peek() ~= "" then
							apply_metablock_intent({
								type = "connect",
								source = { element_id = drag.cell_id, port = drag.port },
								target = { element_id = cell.id, port = name },
							})
						else
							observe("Connect " .. tostring(drag.variable) .. " to " .. name, function()
								local project_path = project_root_path:peek() ~= "" and project_root_path:peek() or (snapshot:peek().notebook.path or "")
								return Service.execute(zeron, project_path, "notebook.connect", {
									from_cell_id = drag.cell_id, variable = drag.variable, output_index = drag.output_index, to_cell_id = cell.id, input_ref = name,
								})
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
			return Service.execute(zeron, path, "notebook.inspect", {})
		end, function(graph)
			observe("Create Group Block definition", function()
				return Service.execute(zeron, path, "group.create_from_selection", {
					name = name, module = module, cells = graph.cells or {}, selected_ids = ids,
				})
			end, function(compiled)
				observe("Replace selected cells with Group Block", function()
					return Service.execute(zeron, path, "notebook.replace_group_selection", {
						selected_ids = compiled.selected_ids,
						source_hashes = compiled.source_hashes,
						parent_source = compiled.parent_source,
					})
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
		local ids = selected_ids()
		local items = {}
		local single = if #ids == 1 then Canvas.node(snapshot:peek().cells or {}, ids[1]) else nil
		if single and type(single.graphimo_instance) == "table" then
			table.insert(items, { label = "Inspect node…", on_select = function() inspector_open:set(true) end })
		end
		if #ids >= 2 then
			table.insert(items, { label = "Create Group Block…", on_select = function()
				group_name:set("New group")
				group_dialog:set(true)
			end })
		end
		return items
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
			local project_path = project_root_path:peek() ~= "" and project_root_path:peek() or (snapshot:peek().notebook.path or "")
			return Service.execute(zeron, project_path, "notebook.run", { cell_id = cell.id })
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

	local function inspector_instance()
		local cell = selected_cell()
		if cell and type(cell.graphimo_instance) == "table" then return cell, cell.graphimo_instance end
		return nil, nil
	end

	local function inspector_project_path()
		return project_root_path:peek() ~= "" and project_root_path:peek() or (snapshot:peek().notebook.path or "")
	end

	local function decode_inspector_literal(raw, annotation)
		-- Strict JSON decoding: a syntax error must never silently become a string.
		local ok, decoded = pcall(ui.json.decode, raw)
		if not ok then return nil, "invalid JSON: " .. tostring(decoded) end
		local ann = tostring(annotation or "")
		if ann == "int" then
			if type(decoded) ~= "number" or decoded ~= math.floor(decoded) then
				return nil, "input " .. " expects an integer (annotation int)"
			end
		elseif ann == "float" or ann == "number" then
			if type(decoded) ~= "number" then return nil, "expected a JSON number (annotation " .. ann .. ")" end
		elseif ann == "bool" or ann == "boolean" then
			if type(decoded) ~= "boolean" then return nil, "expected true or false (annotation bool)" end
		elseif ann == "str" or ann == "string" then
			if type(decoded) ~= "string" then return nil, "expected a JSON string (annotation str)" end
		end
		return decoded, nil
	end

	local function inspector_set_literal()
		local _cell, instance = inspector_instance()
		local name = literal_target:peek()
		local raw = literal_value:peek()
		if not instance or name == "" or raw == "" then
			notice:set("Choose an input and enter a JSON literal first")
			return
		end
		local annotation = nil
		for _, port in instance.inputs or {} do
			if type(port) == "table" and port.name == name then annotation = port.annotation end
		end
		local value, decode_error = decode_inspector_literal(raw, annotation)
		if decode_error then
			notice:set("Literal refused: " .. decode_error)
			return
		end
		observe("Set literal on " .. name, function()
			return Service.execute(zeron, inspector_project_path(), "notebook.set_literal", {
				instance_id = instance.instance_id, input_name = name, value = value,
			})
		end, function()
			notice:set("Literal set on input " .. name)
			refresh_graph()
		end, function(err)
			notice:set("Literal refused: " .. tostring(err))
		end)
	end

	local function inspector_disconnect_input()
		local cell, instance = inspector_instance()
		local name = literal_target:peek()
		if not cell or not instance or name == "" then
			notice:set("Choose an input first")
			return
		end
		observe("Disconnect input " .. name, function()
			return Service.execute(zeron, inspector_project_path(), "notebook.disconnect", {
				cell_id = cell.id, input_name = name,
			})
		end, function()
			notice:set("Input " .. name .. " disconnected (default applies if any)")
			refresh_graph()
		end, function(err)
			notice:set("Disconnect refused: " .. tostring(err))
		end)
	end

	local function inspector_toggle_disabled()
		local cell, instance = inspector_instance()
		if not cell or not instance then return end
		local action = instance.disabled and "notebook.enable_instance" or "notebook.disable_instance"
		observe(action, function()
			return Service.execute(zeron, inspector_project_path(), action, { cell_id = cell.id })
		end, function()
			notice:set(if instance.disabled then "Instance re-enabled" else "Instance disabled; dependents become stale/blocked")
			refresh_graph()
		end, function(err)
			notice:set("Instance toggle refused: " .. tostring(err))
		end)
	end

	local inspector_inputs = ui.computed(function()
		local _cell, instance = inspector_instance()
		return instance and instance.inputs or {}
	end)
	local inspector_outputs = ui.computed(function()
		local _cell, instance = inspector_instance()
		return instance and instance.outputs or {}
	end)

	local function inspector_panel()
		return ui.column { w_full = true, max_h = 340, overflow = "auto", gap = "$space.2", p = "$space.2", rounded = "$radius.md", bg = "$zeron.secondary.bg",
			ui.row { w_full = true, items = "center", gap = "$space.2",
				Text { function()
					local _cell, instance = inspector_instance()
					return if instance then tostring(instance.node) .. "  ·  " .. tostring(instance.component_id or "legacy instance") else "Select a Graphimo node to inspect it"
				end, style = "label", grow = 1 },
				Text { function() local _cell, instance = inspector_instance(); return instance and tostring(instance.state or "") or "" end, style = "caption", tone = "secondary" },
				Button { "Close", tone = "secondary", size = "sm", on_click = function() inspector_open:set(false) end },
			},
			ui.show(function() return select(2, inspector_instance()) ~= nil end, ui.column { w_full = true, gap = "$space.2",
				Text { "Inputs", style = "label" },
				ui.each(inspector_inputs, function(input)
					if type(input) ~= "table" then return Text { "" } end
					local binding = type(input.binding) == "table" and input.binding or {}
					local kind = tostring(binding.kind or "?")
					local detail = "unbound"
					if kind == "variable" or kind == "output_reference" or kind == "expression" then
						detail = tostring(binding.expression or "")
					elseif kind == "literal" then
						detail = "literal " .. tostring(binding.expression or "")
					elseif kind == "default" then
						detail = "default " .. tostring(binding.default)
					end
					local name_text = tostring(input.name or "?")
					if type(input.annotation) == "string" and input.annotation ~= "" then
						name_text = name_text .. " (" .. input.annotation .. ")"
					end
					return ui.row { w_full = true, items = "center", gap = "$space.1",
						Text { name_text .. " · " .. kind .. " · " .. detail, style = "caption", grow = 1, wrap = true },
						Button { "Edit", tone = "secondary", size = "sm", on_click = function() literal_target:set(tostring(input.name or "")); literal_value:set("") end },
					}
				end),
				ui.row { w_full = true, items = "center", gap = "$space.1",
					Text { function() return "Input: " .. (literal_target:peek() ~= "" and literal_target:peek() or "(choose Edit)") end, style = "caption", tone = "secondary" },
					TextField { value = literal_value, label = "Literal (JSON)", placeholder = "0.5", w_full = true, grow = 1 },
					Button { "Set literal", size = "sm", on_click = inspector_set_literal },
					Button { "Disconnect", tone = "secondary", size = "sm", on_click = inspector_disconnect_input },
				},
				Text { "Outputs", style = "label" },
				ui.each(inspector_outputs, function(output)
					if type(output) ~= "table" then return Text { "" } end
					local state = "not computed"
					if output.available then state = "available" elseif output.stale then state = "stale" end
					local preview = ""
					if type(output.runtime_preview) == "table" then preview = tostring(output.runtime_preview.preview or "") end
					local label = tostring(output.index or 0) .. " · " .. tostring(output.name or "output") .. " · " .. tostring(output.declared_type or "?") .. " · " .. state
					if preview ~= "" then label = label .. " · " .. preview end
					return Text { label, style = "caption", tone = "secondary", wrap = true }
				end),
				ui.row { w_full = true, items = "center", gap = "$space.1",
					Button { "Run", size = "sm", on_click = run_selected },
					Button { function()
						local _cell, instance = inspector_instance()
						if instance and instance.disabled then return "Enable" end
						return "Disable"
					end, tone = "secondary", size = "sm", on_click = inspector_toggle_disabled },
				},
			}),
		}
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
		observe("Add " .. kind .. " control", function()
			return Service.execute(zeron, path, "control.add", { kind = kind, identity = identity })
		end, function(control)
			local control_point = default_cell_position(#cells_now() + 1)
			local value_point = default_cell_position(#cells_now() + 2)
			local next_positions = clone_positions(positions:peek())
			next_positions[control.control_cell_id] = control_point
			next_positions[control.value_cell_id] = value_point
			positions:set(next_positions)
			push_history({ kind = "cells", before_records = {}, after_records = {
				{ source = control.control_source, x = control_point.x, y = control_point.y },
				{ source = control.value_source, x = value_point.x, y = value_point.y },
			}, current_ids = { control.control_cell_id, control.value_cell_id } })
			save_positions()
			notice:set(control.label .. " added; connect its value output " .. control.value_name)
			refresh_graph()
		end, function(err)
			notice:set("Could not add " .. kind .. " control: " .. tostring(err))
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
		-- Never invent implicit variable bindings: unbound inputs stay formal
		-- ports and are connected explicitly from the canvas or by an agent.
		local bindings = {}
		local defined = {}
		for _, cell in cells_now() do for _, name in cell.defs or {} do defined[name] = true end end
		local next_number = output_counter:peek()
		local out = callable.name .. "_result_" .. tostring(next_number)
		while defined[out] do next_number += 1; out = callable.name .. "_result_" .. tostring(next_number) end
		output_counter:set(next_number + 1)
		observe("Add component " .. item.id .. "::" .. callable.name, function()
			return Service.execute(zeron, project_root_path:peek() ~= "" and project_root_path:peek() or (snapshot:peek().notebook.path or ""), "component.add", {
				path = item.id, callable = callable.name, output_name = out, bindings = bindings,
			})
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
		local model_parameters = ui.computed(function()
			local view = metablock_projection:get()
			return view and view.external_parameters or {}
		end)
		local selected_element_parameters = ui.computed(function()
			local element = selected_metablock_element()
			return element and element.parameters or {}
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
		local function display_parameter_value(value)
			if type(value) == "table" then
				local ok, encoded = pcall(ui.json.encode, value)
				return if ok then encoded else "complex value"
			end
			return tostring(value)
		end
		local function display_meta_event(event)
			local ok, payload = pcall(ui.json.encode, event.payload or {})
			if not ok then payload = tostring(event.payload) end
			local target = event.logical_element_id or event.module_path or event.runtime_instance_id or "runtime"
			local step = if event.step ~= nil then " · step " .. tostring(event.step) else ""
			return string.sub(tostring(event.kind or "event") .. " · " .. tostring(target) .. step .. "\n" .. tostring(payload), 1, 1800)
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
				ui.show(function() return #addon_dialects:get() == 0 end, Text { "No MetaBlock addon installed (addon.list)", style = "caption", tone = "secondary" }),
				ui.each(addon_dialects, function(dialect)
					return Button {
						"Create " .. dialect.label,
						tone = "secondary", size = "sm", w_full = true,
						disabled = function() return (snapshot:get().notebook.path or "") == "" end,
						on_click = function() create_metablock(dialect.dialect_id, dialect.label) end,
					}
				end),
				Button {
					"Import MetaBlock state",
					tone = "secondary", size = "sm", w_full = true,
					disabled = function() return (snapshot:get().notebook.path or "") == "" end,
					on_click = import_metablock_state,
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
					return if view then tostring(view.name or "MetaBlock") .. " · " .. active_metablock_scope:get() else "MetaBlock"
				end, style = "heading", wrap = true },
				Text { function()
					local view = metablock_projection:get()
					local selector = view and view.runtime_selector
					local items = selector and selector.items or {}
					if #items > 0 then
						return "Runtime visibility: " .. tostring(#items) .. " active runtime(s) in the Graphimo service process"
					end
					return "Runtime visibility: none in the Graphimo service. A model running in the separate marimo kernel is not visible here yet."
				end, style = "caption", tone = "secondary", wrap = true },
				Button {
					"Insert generated definition",
					tone = "primary", size = "sm", w_full = true,
					disabled = function() return session_id:get() == "" or (not is_local_url(url:get())) end,
					on_click = materialize_metablock,
				},
				ui.show(function()
					local view = metablock_projection:get()
					return view ~= nil and type(view.ui) == "table"
				end, render_addon_ui_panels()),
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
				ui.show(function()
					local view = metablock_projection:get()
					return view ~= nil and active_metablock_scope:get() == "root" and selected:get() == ""
				end, ui.column { w_full = true, gap = "$space.1",
					Text { "Model parameters", style = "heading" },
					Button {
						function() local view = metablock_projection:get(); return "Rename · " .. tostring(view and view.name or "MetaBlock") end,
						tone = "secondary", size = "sm", w_full = true,
						on_click = function()
							local view = metablock_projection:peek()
						open_metablock_parameter_editor({ external = true, id = "name", name = "Name", type = "str", value = view and view.name or "MetaBlock" })
						end,
					},
					ui.each(model_parameters, function(parameter)
						return Button {
							"Edit " .. tostring(parameter.name) .. " · " .. display_parameter_value(parameter.default),
							tone = "secondary", size = "sm", w_full = true,
							on_click = function()
								open_metablock_parameter_editor({
									external = true, id = parameter.id, name = parameter.name,
									type = parameter.type, value = parameter.default,
								})
							end,
						}
					end),
				}),
				ui.show(function() return selected_metablock_element() ~= nil end, ui.column { w_full = true, gap = "$space.1",
					Text { function()
						local element = selected_metablock_element()
						return element and (element.name or element.id) or "Selection"
					end, style = "heading", wrap = true },
					Text { function()
						local element = selected_metablock_element()
						return element and tostring(element.kind or "component") or ""
					end, style = "caption", tone = "secondary" },
					Button {
						function() local element = selected_metablock_element(); return "Rename · " .. tostring(element and (element.name or element.id) or "element") end,
						tone = "secondary", size = "sm", w_full = true,
						on_click = function()
							local element = selected_metablock_element()
						open_metablock_parameter_editor({ rename_element = true, element_id = element.id, name = "Element name", type = "str", value = element.name })
						end,
					},
					Text { function()
						local element = selected_metablock_element()
						local shape = element and element.output_shape
						if type(shape) ~= "table" then return "Output shape unresolved" end
						local dimensions = {}
						for _, size in shape.shape or {} do table.insert(dimensions, tostring(size)) end
						return (shape.dtype or "?") .. " [" .. table.concat(dimensions, " × ") .. "]"
					end, style = "caption", tone = "secondary", wrap = true },
					ui.each(selected_element_parameters, function(parameter)
						return Button {
							"Edit " .. tostring(parameter.name) .. " · " .. display_parameter_value(parameter.value),
							tone = "secondary", size = "sm", w_full = true,
							on_click = function()
								open_metablock_parameter_editor({
									element_id = selected:peek(), name = parameter.name,
									type = parameter.type, value = parameter.value,
									choices = parameter.choices, minimum = parameter.minimum,
									maximum = parameter.maximum,
								})
							end,
						}
					end),
					ui.show(function()
						local element = selected_metablock_element()
						return element ~= nil and element.kind == "group_instance"
					end, Button {
						function()
							local element = selected_metablock_element()
							return "Edit repetitions · " .. tostring(element and element["repeat"] or 1)
					end,
						tone = "secondary", size = "sm", w_full = true,
						on_click = function()
							local element = selected_metablock_element()
						open_metablock_parameter_editor({
								group_repeat = true, element_id = element.id, name = "repetitions",
								type = "int", value = element["repeat"],
							})
						end,
					}),
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
				Button {
					function() return if checkpoint_panel_open:get() then "Checkpoints −" else "Checkpoints +" end,
					tone = "secondary", size = "sm", w_full = true,
					on_click = function() checkpoint_panel_open:set(not checkpoint_panel_open:peek()) end,
				},
				ui.show(checkpoint_panel_open, ui.column { w_full = true, gap = "$space.1",
					Text { "Inspect checkpoint metadata or preview exact-shape migration. Nothing is loaded or changed.", style = "caption", tone = "secondary", wrap = true },
					TextField { value = checkpoint_artifact_id, label = "Artifact ID", placeholder = "Graphimo Artifact ID", w_full = true },
					Button { "Inspect checkpoint", tone = "secondary", size = "sm", w_full = true, disabled = function() return checkpoint_busy:get() end, on_click = inspect_checkpoint },
					ui.show(function() return checkpoint_details:get() ~= nil end, ui.column { w_full = true, gap = "$space.1",
						Text { function()
							local details = checkpoint_details:get()
							return "Artifact " .. tostring(details and details.artifact_id or "") .. " · " .. tostring(details and details.kind or "checkpoint")
						end, style = "caption", tone = "secondary", wrap = true },
						Text { function()
							local details = checkpoint_details:get()
							return "Model: " .. tostring(details and details.model_name or "unknown") .. " · MetaBlock: " .. tostring(details and details.metablock_id or "unknown")
						end, style = "caption", tone = "secondary", wrap = true },
						Text { function()
							local details = checkpoint_details:get()
							return "Graph revision: " .. tostring(details and details.graph_revision or "unknown")
						end, style = "caption", tone = "secondary", wrap = true },
						Text { function()
							local details = checkpoint_details:get()
							return "Materialization: " .. tostring(details and details.materialization_key or "unknown")
						end, style = "caption", tone = "secondary", wrap = true },
						Text { function()
							local details = checkpoint_details:get()
							return "checkpoint " .. tostring(details and details.pytorch_version or "unknown") .. " · addon " .. tostring(details and details.addon_version or "unknown")
						end, style = "caption", tone = "secondary", wrap = true },
						Text { function()
							local details = checkpoint_details:get()
							local optimizer = details and details.optimizer_state_present and "present" or "absent"
							local scheduler = details and details.scheduler_state_present and "present" or "absent"
							return "Optimizer state: " .. optimizer .. " · scheduler state: " .. scheduler
						end, style = "caption", tone = "secondary", wrap = true },
						Text { function()
							local details = checkpoint_details:get()
							return format_checkpoint_metadata(details and details.metadata)
						end, style = "caption", tone = "secondary", wrap = true },
						Text { function()
							local details = checkpoint_details:get()
							local current = metablock_projection:get()
							return if details and current and details.graph_revision == current.graph_revision then "Compatibility: same graph revision" else "Compatibility: migration preview may be required"
						end, style = "caption", tone = "secondary", wrap = true },
						ui.show(function()
							local details = checkpoint_details:get()
							local current = metablock_projection:get()
							return details ~= nil and current ~= nil and details.graph_revision ~= current.graph_revision
						end, Button { "Preview exact-shape migration", tone = "secondary", size = "sm", w_full = true, disabled = function() return checkpoint_busy:get() end, on_click = preview_checkpoint_migration }),
						ui.show(function()
							local details = checkpoint_details:get()
							local current = metablock_projection:get()
							return details ~= nil and current ~= nil and type(details.migration_preview) == "table" and details.migration_preview_revision == current.graph_revision
						end, ui.column { w_full = true, gap = "$space.1",
							Text { function()
								local details = checkpoint_details:get()
								local report = details and details.migration_preview and details.migration_preview.report or nil
								return format_checkpoint_migration_report(report)
							end, style = "caption", tone = "secondary", wrap = true },
							Button { "Apply migration (strict off)", tone = "secondary", size = "sm", w_full = true, disabled = function() return checkpoint_busy:get() end, on_click = function() restore_checkpoint(true) end },
						}),
						ui.show(function() return checkpoint_details:get() ~= nil end, ui.column { w_full = true, gap = "$space.1",
							Button { "Restore checkpoint into live runtime", tone = "primary", size = "sm", w_full = true, disabled = function() return checkpoint_busy:get() end, on_click = function() restore_checkpoint(false) end },
							Text { function()
								local details = checkpoint_details:get()
								local restore = details and details.last_restore or nil
								if type(restore) ~= "table" then return "Not restored yet" end
								return "Last restore: runtime " .. tostring(restore.runtime_instance_id or "?") .. " · migrated " .. tostring(restore.migrated or false) .. " · revision match " .. tostring(restore.revision_match or false)
							end, style = "caption", tone = "secondary", wrap = true },
						}),
						ui.show(function()
							local details = checkpoint_details:get()
							local current = metablock_projection:get()
							return details ~= nil and current ~= nil and type(details.migration_preview) == "table" and details.migration_preview_revision ~= current.graph_revision
						end, Text { "The model graph changed after this preview. Run the migration preview again before relying on it.", style = "caption", tone = "secondary", wrap = true }),
					}),
				}),
				ui.row { w_full = true, items = "center", gap = "$space.1",
					Button {
						function() return if metablock_events_open:get() then "Runtime events −" else "Runtime events +" end,
						tone = "secondary", size = "sm", grow = 1,
						on_click = function() metablock_events_open:set(not metablock_events_open:peek()) end,
					},
					Button { "Read", tone = "secondary", size = "sm", on_click = read_metablock_events },
				},
				ui.show(metablock_events_open, ui.column { w_full = true, gap = "$space.1",
					Text { "Events delivered to the Graphimo service. Events in another marimo process are not bridged yet.", style = "caption", tone = "secondary", wrap = true },
					ui.show(function() return #metablock_event_entries:get() == 0 end, Text { "No events loaded", style = "caption", tone = "secondary" }),
					ui.each(metablock_event_entries, function(event)
						return Text { display_meta_event(event), style = "caption", tone = "secondary", wrap = true }
					end),
				}),
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
			Button { function() return if inspector_open:get() then "Inspector −" else "Inspector +" end, tone = "secondary", size = "sm", disabled = function() return selected:get() == "" end, on_click = function() inspector_open:set(not inspector_open:peek()) end },
			Button { function() return if show_technical:get() then "Technical −" else "Technical +" end, tone = "secondary", size = "sm", on_click = function() show_technical:set(not show_technical:peek()); refresh_graph() end },
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
				Canvas.draw_edges(c, visible_positioned_cells(), snapshot:get().edges or {}, zoom:get(), pan_x:get(), pan_y:get(), selected:get())
				Canvas.draw_ports(c, visible_positioned_cells(), zoom:get(), pan_x:get(), pan_y:get())
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
			ui.show(function() return inspector_open:get() end, inspector_panel()),
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
		Dialog {
			open = metablock_parameter_dialog,
			title = "Edit MetaBlock parameter",
			body = "The value is validated and saved through Graphimo Core and the addon.",
			confirm = {
				label = "Apply parameter",
				on_click = submit_metablock_parameter,
				disabled = function() return metablock_parameter_target:get().name == nil end,
			},
			Text {
				function()
					local target = metablock_parameter_target:get()
					return "Parameter: " .. tostring(target.name or "unknown")
				end,
				style = "caption", tone = "secondary",
			},
			TextField {
				value = metablock_parameter_value,
				label = "Value",
				placeholder = "Enter a value",
				w_full = true,
				autofocus = true,
			},
			Text {
				function()
					local target = metablock_parameter_target:get()
					local details = {}
					if target.minimum ~= nil then table.insert(details, "minimum " .. tostring(target.minimum)) end
					if target.maximum ~= nil then table.insert(details, "maximum " .. tostring(target.maximum)) end
					if target.choices then
						local choices = {}
						for _, choice in target.choices do table.insert(choices, tostring(choice)) end
						table.insert(details, "choices: " .. table.concat(choices, ", "))
					end
					return table.concat(details, " · ")
				end,
				style = "caption", tone = "secondary", wrap = true,
			},
		},
	}
end)

zeron.page({ id = "canvas", title = "Graphimo", icon = "share", build = function() return Page {} end })
