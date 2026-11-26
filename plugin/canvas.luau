local Canvas = {}
Canvas.width = 260
-- Card layout contract: a fixed-height header, then one 22-unit row per port.
-- Port dots, edges and hit-testing all read Canvas.port, so the visible anchor
-- can never diverge from the connection point (or from the rendered row).
Canvas.header_height = 64
Canvas.port_row = 22

-- Graphimo instances expose formal @node ports; ordinary marimo cells keep
-- their lexical refs/defs as ports. marimo's graph stays the execution truth.
local function instance_of(cell)
	if type(cell) == "table" and type(cell.graphimo_instance) == "table" then
		return cell.graphimo_instance
	end
	return nil
end

function Canvas.input_ports(cell)
	local instance = instance_of(cell)
	if instance and type(instance.inputs) == "table" then
		local names = {}
		for _, input in instance.inputs do
			if type(input) == "table" and type(input.name) == "string" then
				table.insert(names, input.name)
			end
		end
		return names
	end
	return cell.refs or {}
end

function Canvas.output_ports(cell)
	local instance = instance_of(cell)
	if instance and type(instance.outputs) == "table" then
		local keys = {}
		for _, output in instance.outputs do
			if type(output) == "table" and type(output.index) == "number" then
				table.insert(keys, tostring(output.index))
			end
		end
		return keys
	end
	return cell.defs or {}
end

function Canvas.output_label(cell, key)
	local instance = instance_of(cell)
	if instance and type(instance.outputs) == "table" then
		local index = tonumber(key)
		for _, output in instance.outputs do
			if type(output) == "table" and output.index == index then
				return tostring(output.name or key)
			end
		end
	end
	return tostring(key)
end

function Canvas.input_label(cell, name)
	local instance = instance_of(cell)
	if instance and type(instance.inputs) == "table" then
		for _, input in instance.inputs do
			if type(input) == "table" and input.name == name then
				local binding = input.binding
				local kind = type(binding) == "table" and binding.kind or nil
				if kind == "unbound" then return name .. " (unbound)" end
				if kind == "default" then return name .. " (default)" end
				if kind == "literal" then return name .. " (literal)" end
				return name
			end
		end
	end
	return tostring(name)
end

-- "bound" means a live connection or literal fills the port; "unbound" and
-- "default" render as an empty (hollow) dot.
function Canvas.input_bound(cell, name)
	local instance = instance_of(cell)
	if instance and type(instance.inputs) == "table" then
		for _, input in instance.inputs do
			if type(input) == "table" and input.name == name then
				local binding = input.binding
				local kind = type(binding) == "table" and binding.kind or nil
				return kind ~= nil and kind ~= "unbound" and kind ~= "default"
			end
		end
		return false
	end
	-- Ordinary cells: a ref means the port is fed by another cell.
	local refs = cell.refs or {}
	for _, ref in refs do if ref == name then return true end end
	return false
end

function Canvas.input_hint(cell, name)
	local instance = instance_of(cell)
	if instance and type(instance.inputs) == "table" then
		for _, input in instance.inputs do
			if type(input) == "table" and input.name == name then
				local binding = input.binding
				if type(binding) == "table" then
					local kind = binding.kind
					local expression = binding.expression
					if kind == "unbound" then return "unbound" end
					if kind == "default" then return "default" end
					if type(expression) == "string" and expression ~= "" then
						return string.sub(expression, 1, 18)
					end
					return tostring(kind)
				end
			end
		end
	end
	return ""
end

function Canvas.height(cell)
	local ports = math.max(1, #Canvas.input_ports(cell), #Canvas.output_ports(cell))
	local preview = 0
	if type(cell.output) == "table" and (cell.output.preview or cell.output.image_data_url) then preview = 58 end
	if cell.errors and #cell.errors > 0 then preview = math.max(preview, 40) end
	return Canvas.header_height + ports * Canvas.port_row + preview + 14
end

function Canvas.node(cells, id)
	for _, cell in cells do if cell.id == id then return cell end end
	return nil
end

function Canvas.point(e, zoom, x, y)
	return ((e.local_x or 0) - x) / zoom, ((e.local_y or 0) - y) / zoom
end

function Canvas.near(x, y, a, b, radius)
	return (x - a) ^ 2 + (y - b) ^ 2 <= radius ^ 2
end

function Canvas.port(cell, key, output)
	local list = if output then Canvas.output_ports(cell) else Canvas.input_ports(cell)
	for index, name in list do
		if name == key then return (cell.y or 0) + Canvas.header_height + (index - 1) * Canvas.port_row + Canvas.port_row / 2 end
	end
	return (cell.y or 0) + Canvas.header_height + Canvas.port_row / 2
end

function Canvas.draw_grid(c, w, h, zoom, pan_x, pan_y, visible)
	if not visible then return end
	local step = 30 * zoom
	while step < 20 do step *= 2 end
	local x0, y0 = pan_x % step, pan_y % step
	for x = x0, w, step do
		for y = y0, h, step do c:circle(x, y, 1, "$border.strong") end
	end
end

-- Port dots share Canvas.port geometry with edges and hit-testing, so the
-- visible anchor can never diverge from the connection point.
function Canvas.draw_ports(c, cells, zoom, pan_x, pan_y)
	local function screen(x, y)
		return pan_x + x * zoom, pan_y + y * zoom
	end
	for _, cell in cells do
		for _, name in Canvas.input_ports(cell) do
			local x, y = screen(cell.x or 0, Canvas.port(cell, name, false))
			if Canvas.input_bound(cell, name) then
				c:circle(x, y, 4.5 * zoom, "$accent")
			else
				-- Hollow dot: ring in tertiary ink over the canvas background.
				c:circle(x, y, 4.5 * zoom, "$text.tertiary")
				c:circle(x, y, 2.8 * zoom, "$background")
			end
		end
		for _, key in Canvas.output_ports(cell) do
			local x, y = screen((cell.x or 0) + Canvas.width, Canvas.port(cell, key, true))
			c:circle(x, y, 4.5 * zoom, "$accent")
			c:circle(x, y, 2.8 * zoom, "$background")
		end
	end
end

-- Cubic bezier through (x1,y1)-(x2,y2); returns a point at t along it.
local function bezier(x1, y1, x2, y2, bend, t)
	local u = 1 - t
	local ax, ay = x1 + bend, y1
	local bx, by = x2 - bend, y2
	return u * u * u * x1 + 3 * u * u * t * ax + 3 * u * t * t * bx + t * t * t * x2,
		u * u * u * y1 + 3 * u * u * t * ay + 3 * u * t * t * by + t * t * t * y2
end

function Canvas.draw_edges(c, cells, edges, zoom, pan_x, pan_y, selected, pending)
	local function screen(x, y)
		return pan_x + x * zoom, pan_y + y * zoom
	end
	local index = {}
	for _, cell in cells do index[cell.id] = cell end
	for _, edge in edges do
		local from = index[edge.source_cell_id]
		local to = index[edge.target_cell_id]
		if from and to then
			local source_port = edge.source_variable or edge.variable
			if type(edge.output_index) == "number" then source_port = tostring(edge.output_index) end
			local target_port = edge.input_ref or edge.target_variable or edge.variable
			local x1, y1 = screen((from.x or 0) + Canvas.width, Canvas.port(from, source_port, true))
			local x2, y2 = screen(to.x or 0, Canvas.port(to, target_port, false))
			local bend = math.max(52 * zoom, math.abs(x2 - x1) * 0.42)
			local path = ui.path():move(x1, y1):cubic(x1 + bend, y1, x2 - bend, y2, x2, y2)
			local stale = edge.status == "stale"
			local edge_color = if edge == selected then "$accent" elseif stale then "$text.tertiary" else "$text.secondary"
			c:stroke(path, "$background", 5 * zoom, { cap = "round" })
			c:stroke(path, edge_color, 1.8 * zoom, { cap = "round" })
			-- Arrowhead at the target port.
			local ax, ay = bezier(x1, y1, x2, y2, bend, 0.82)
			local bx, by = bezier(x1, y1, x2, y2, bend, 0.9)
			local angle = math.atan2(by - ay, bx - ax)
			local size = 6.5 * zoom
			local arrow = ui.path()
				:move(x2 - size * math.cos(angle - 0.5), y2 - size * math.sin(angle - 0.5))
				:line(x2, y2)
				:line(x2 - size * math.cos(angle + 0.5), y2 - size * math.sin(angle + 0.5))
			c:stroke(arrow, edge_color, 1.8 * zoom, { cap = "round", join = "round" })
		end
	end
	if pending then
		local from = index[pending.from_cell_id]
		if from then
			local x1, y1 = screen((from.x or 0) + Canvas.width, Canvas.port(from, pending.port or pending.variable, true))
			local x2, y2 = screen(pending.x, pending.y)
			local bend = math.max(52 * zoom, math.abs(x2 - x1) * 0.42)
			c:stroke(ui.path():move(x1, y1):cubic(x1 + bend, y1, x2 - bend, y2, x2, y2), "$accent", 2 * zoom, { cap = "round" })
		end
	end
end

return Canvas
