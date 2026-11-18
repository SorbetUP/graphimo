local Meta = require("./pytorch_meta")
local Text = require("@ds/text")
local Button = require("@ds/button")
local TextArea = require("@ds/text_area")
local ContextMenu = require("@ds/context_menu")
local Glass = require("@ds/glass")
local Tooltip = require("@ds/tooltip")

local Editor = {}

local function deep_clone(value)
	if type(value) ~= "table" then return value end
	local out = {}
	for key, child in value do out[key] = deep_clone(child) end
	return out
end

local function node_label(node)
	if node.kind == "input" then return "Input · " .. tostring((node.params or {}).name or "x") end
	if node.kind == "output" then return "Output · " .. tostring((node.params or {}).name or "output") end
	return tostring(node.label or node.op or node.kind or "Element")
end

local function next_id(nodes, definition)
	local prefix = string.lower(tostring(definition.op or definition.kind or "node"))
	prefix = string.gsub(prefix, "[^%w_]", "_")
	local used = {}
	for _, node in nodes or {} do used[node.id] = true end
	local index = 1
	while used[prefix .. "_" .. tostring(index)] do index += 1 end
	return prefix .. "_" .. tostring(index)
end

local function selected_node(data, id)
	return Meta.find((data or {}).nodes or {}, id)
end

Editor.View = ui.component(function(props)
	local data = props.data
	local selected = props.selected
	local params_text = props.params_text
	local dirty = props.dirty
	local pending = props.pending
	local canvas = ui.ref()
	local drag = nil

	local function mutate(run)
		local next = deep_clone(data:peek() or {})
		next.nodes = next.nodes or {}
		next.edges = next.edges or {}
		run(next)
		data:set(next)
		dirty:set(true)
	end

	local function choose(id)
		selected:set(id or "")
		local node = selected_node(data:peek(), id)
		params_text:set(ui.json.encode((node and node.params) or {}))
	end

	local function add_element(definition, x, y)
		mutate(function(next)
			local id = next_id(next.nodes, definition)
			table.insert(next.nodes, Meta.new_node(definition, id, x, y))
			choose(id)
		end)
	end

	local function delete_selected()
		local id = selected:peek()
		if id == "" then return end
		mutate(function(next)
			local keep = {}
			for _, node in next.nodes do if node.id ~= id then table.insert(keep, node) end end
			next.nodes = keep
			local edges = {}
			for _, edge in next.edges do if edge.from ~= id and edge.to ~= id then table.insert(edges, edge) end end
			next.edges = edges
		end)
		choose("")
	end

	local function apply_params()
		local id = selected:peek()
		if id == "" then return end
		local ok, decoded = pcall(ui.json.decode, params_text:peek())
		if not ok or type(decoded) ~= "table" then
			if props.on_notice then props.on_notice("Parameters must be a JSON object") end
			return
		end
		mutate(function(next)
			local node = Meta.find(next.nodes, id)
			if node then node.params = decoded end
		end)
	end

	local function card(node)
		local style = Glass.style({
			absolute = true,
			left = node.x or 0,
			top = node.y or 0,
			w = Meta.node_width,
			h = Meta.node_height,
			rounded = "$radius.md",
			pointer_events = "none",
		}, "island")
		style.border_color = function() return if selected:get() == node.id then "$accent" else "$border.strong" end
		style.bg = "$zeron.secondary.bg"
		table.insert(style, ui.column {
			w_full = true, h_full = true, p = "$space.2", gap = "$space.1",
			ui.row {
				w_full = true, items = "center",
				if Meta.has_input(node) then Text { "●", style = "caption", tone = "secondary" } else nil,
				Text { node_label(node), style = "label", grow = 1, wrap = false },
				if Meta.has_output(node) then Text { "●", style = "caption", tone = "primary" } else nil,
			},
			Text {
				function()
					local p = node.params or {}
					local text = ui.json.encode(p)
					if #text > 54 then text = string.sub(text, 1, 53) .. "…" end
					return text
				end,
				style = "caption", tone = "secondary",
				tooltip = Tooltip { function() return ui.json.encode(node.params or {}) end },
			},
		})
		return ui.div(style)
	end

	local function pointer(e)
		local x = e.local_x or 0
		local y = e.local_y or 0
		return x, y
	end

	local function on_press(e)
		local x, y = pointer(e)
		local nodes = (data:peek() or {}).nodes or {}
		for index = #nodes, 1, -1 do
			local node = nodes[index]
			local nx, ny = node.x or 0, node.y or 0
			if Meta.has_output(node) and (x - (nx + Meta.node_width)) ^ 2 + (y - (ny + Meta.node_height / 2)) ^ 2 <= 12 ^ 2 then
				choose(node.id)
				drag = { kind = "wire", from = node.id }
				pending:set({ from = node.id, x = x, y = y })
				return
			end
			if Meta.has_input(node) and (x - nx) ^ 2 + (y - (ny + Meta.node_height / 2)) ^ 2 <= 12 ^ 2 then
				choose(node.id)
				return
			end
			if x >= nx and x <= nx + Meta.node_width and y >= ny and y <= ny + Meta.node_height then
				choose(node.id)
				drag = { kind = "node", id = node.id, x = x, y = y, nx = nx, ny = ny }
				return
			end
		end
		choose("")
	end

	local function on_move(e)
		if not drag then return end
		local x, y = pointer(e)
		if drag.kind == "wire" then
			pending:set({ from = drag.from, x = x, y = y })
		elseif drag.kind == "node" then
			mutate(function(next)
				local node = Meta.find(next.nodes, drag.id)
				if node then node.x, node.y = drag.nx + x - drag.x, drag.ny + y - drag.y end
			end)
		end
	end

	local function on_release(e)
		if not drag then return end
		local x, y = pointer(e)
		if drag.kind == "wire" then
			local target = nil
			for _, node in (data:peek() or {}).nodes or {} do
				if node.id ~= drag.from and Meta.has_input(node) then
					local nx, ny = node.x or 0, node.y or 0
					if (x - nx) ^ 2 + (y - (ny + Meta.node_height / 2)) ^ 2 <= 14 ^ 2 then target = node; break end
				end
			end
			if target then
				mutate(function(next)
					local duplicate = false
					for _, edge in next.edges do if edge.from == drag.from and edge.to == target.id then duplicate = true end end
					if not duplicate then table.insert(next.edges, { from = drag.from, to = target.id }) end
				end)
			end
		end
		pending:set(nil)
		drag = nil
	end

	local function menu(at)
		return Meta.menu_entries(function(definition)
			add_element(definition, at.local_x or 120, at.local_y or 120)
		end)
	end

	local function inspector()
		return ui.column {
			w = 250, h_full = true, shrink = 0, min_h = 0, gap = "$space.2", p = "$space.2",
			bg = "$zeron.secondary.bg", rounded = "$radius.md",
			Text { "Element", style = "heading" },
			Text {
				function()
					local node = selected_node(data:get(), selected:get())
					return if node then node_label(node) else "Select an element"
				end,
				style = "label", tone = "secondary", wrap = true,
			},
			ui.show(function() return selected:get() ~= "" end, ui.column {
				w_full = true, grow = 1, min_h = 0, gap = "$space.2",
				Text { "Parameters (JSON)", style = "caption", tone = "secondary" },
				TextArea { value = params_text, rows = 10, max_rows = 28, w_full = true, grow = 1, name = "Element parameters" },
				ui.row {
					w_full = true, gap = "$space.2",
					Button { "Apply", size = "sm", on_click = apply_params },
					Button { "Delete", size = "sm", tone = "danger", on_click = delete_selected },
				},
			}),
		}
	end

	return ui.column {
		w = props.width or 760, h_full = true, min_h = 0, shrink = 0, gap = "$space.2", p = "$space.2",
		bg = "$zeron.secondary.bg", rounded = "$radius.lg",
		ui.row {
			w_full = true, items = "center", gap = "$space.2",
			Text { function() return tostring((data:get() or {}).name or "PyTorch Model") end, style = "heading", grow = 1 },
			Text { "Elements are definition-only; PyTorch executes the generated nn.Module.", style = "caption", tone = "secondary" },
			Button {
				function() return if dirty:get() then "Save *" else "Save" end,
				size = "sm",
				on_click = function() if props.on_save then props.on_save(data:peek()) end end,
			},
			Button { "Close", size = "sm", tone = "secondary", on_click = function() if props.on_close then props.on_close() end end },
		},
		ui.row {
			w_full = true, grow = 1, min_h = 0, gap = "$space.2",
			ui.div {
				ref = canvas, grow = 1, min_w = 360, min_h = 0, h_full = true, relative = true,
				rounded = "$radius.md", overflow = "hidden", bg = "$background",
				ui.canvas {
					absolute = true, left = 0, top = 0, w_full = true, h_full = true,
					draw = function(c)
						Meta.draw_edges(c, (data:get() or {}).nodes or {}, (data:get() or {}).edges or {}, pending:get())
					end,
					pointer_events = "none",
				},
				ui.div {
					absolute = true, left = 0, top = 0, right = 0, bottom = 0, pointer_events = "none",
					ui.each(function() return (data:get() or {}).nodes or {} end, card, { key = function(node) return node.id end, w_full = true, h_full = true, relative = true }),
				},
				ContextMenu {
					items = menu,
					label = "Add PyTorch element",
					absolute = true, left = 0, top = 0, right = 0, bottom = 0,
					ui.canvas {
						absolute = true, left = 0, top = 0, w_full = true, h_full = true, draw = function() end,
						on_press = on_press,
						on_pointer_move = on_move,
						on_release = on_release,
					},
				},
			},
			inspector(),
		},
	}
end)

return Editor
