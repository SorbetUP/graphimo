local Canvas = {}
Canvas.width = 260

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

function Canvas.height(cell, summary)
	local media = if type(cell.output) == "table" and cell.output.image_data_url then 68 else 0
	local summary_height = 0
	if type(summary) == "table" then
		local function add_row(prefix, values)
			if #values == 0 then return end
			local text = prefix .. table.concat(values, " · ")
			-- Use UTF-8 byte length and a conservative 24-byte wrapped line budget.
			summary_height += math.max(1, math.ceil(#text / 24)) * 20
		end
		add_row("In: ", summary.inputs or {})
		add_row("Out: ", summary.outputs or {})
	end
	local base_height = math.max(146, 120 + (#Canvas.input_ports(cell) + #Canvas.output_ports(cell)) * 22 + media)
	return base_height + summary_height
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
		if name == key then return (cell.y or 0) + 55 + (index - 1) * 22 end
	end
	return (cell.y or 0) + 55
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

function Canvas.draw_edges(c, cells, edges, zoom, pan_x, pan_y, selected, pending)
	local function screen(x, y)
		return pan_x + x * zoom, pan_y + y * zoom
	end
	for _, edge in edges do
		local from = Canvas.node(cells, edge.source_cell_id)
		local to = Canvas.node(cells, edge.target_cell_id)
		if from and to then
			local source_port = edge.source_variable or edge.variable
			if type(edge.output_index) == "number" then source_port = tostring(edge.output_index) end
			local target_port = edge.input_ref or edge.target_variable or edge.variable
			local x1, y1 = screen((from.x or 0) + Canvas.width, Canvas.port(from, source_port, true))
			local x2, y2 = screen(to.x or 0, Canvas.port(to, target_port, false))
			local bend = math.max(52 * zoom, math.abs(x2 - x1) * 0.42)
			local path = ui.path():move(x1, y1):cubic(x1 + bend, y1, x2 - bend, y2, x2, y2)
			local edge_color = if edge.status == "stale" then "$text.tertiary" else "$text.secondary"
			c:stroke(path, "$background", 5 * zoom, { cap = "round" })
			c:stroke(path, edge_color, 1.8 * zoom, { cap = "round" })
			c:circle(x1, y1, 4 * zoom, "$accent")
			c:circle(x2, y2, 4 * zoom, "$accent")
		end
	end
	if pending then
		local from = Canvas.node(cells, pending.from_cell_id)
		if from then
			local x1, y1 = screen((from.x or 0) + Canvas.width, Canvas.port(from, pending.port or pending.variable, true))
			local x2, y2 = screen(pending.x, pending.y)
			local bend = math.max(52 * zoom, math.abs(x2 - x1) * 0.42)
			c:stroke(ui.path():move(x1, y1):cubic(x1 + bend, y1, x2 - bend, y2, x2, y2), "$accent", 2 * zoom, { cap = "round" })
		end
	end
end

return Canvas
