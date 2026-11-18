local Canvas = {}
Canvas.width = 300
Canvas.header_height = 70
Canvas.port_gap = 27

function Canvas.height(cell, block)
	if type(block) == "table" then
		local rows = math.max(#(block.inputs or {}), #(block.outputs or {}), 1)
		local rich = if block.kind == "display" or block.kind == "annotation" then 96 else 0
		return math.max(128, Canvas.header_height + rows * Canvas.port_gap + 28 + rich)
	end
	local rows = math.max(#(cell.refs or {}), #(cell.defs or {}), 1)
	local media = if type(cell.output) == "table" and cell.output.image_data_url then 72 else 0
	return math.max(132, Canvas.header_height + rows * Canvas.port_gap + 30 + media)
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

local function block_port_index(block, variable, output)
	if type(block) ~= "table" then return nil end
	local list = if output then (block.outputs or {}) else (block.inputs or {})
	for index, port in list do
		if output then
			if port.variable == variable or port.port_key == variable then return index end
		else
			local binding = port.binding
			if port.port_key == variable or port.name == variable
				or (type(binding) == "table" and binding.kind == "variable" and binding.variable == variable)
			then return index end
		end
	end
	return nil
end

function Canvas.port(cell, variable, output, block)
	local index = block_port_index(block, variable, output)
	if not index then
		local list = if output then (cell.defs or {}) else (cell.refs or {})
		for i, name in list do if name == variable then index = i; break end end
	end
	index = index or 1
	return (cell.y or 0) + Canvas.header_height + (index - 1) * Canvas.port_gap
end

function Canvas.input_ports_for_variable(block, variable)
	local ports = {}
	if type(block) ~= "table" then return ports end
	for _, port in block.inputs or {} do
		local binding = port.binding
		if type(binding) == "table" and binding.kind == "variable" and binding.variable == variable then
			table.insert(ports, port.port_key or port.name)
		end
	end
	return ports
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

local function stroke_edge(c, x1, y1, x2, y2, zoom)
	local bend = math.max(52 * zoom, math.abs(x2 - x1) * 0.42)
	local path = ui.path():move(x1, y1):cubic(x1 + bend, y1, x2 - bend, y2, x2, y2)
	c:stroke(path, "$background", 5 * zoom, { cap = "round" })
	c:stroke(path, "$text.secondary", 1.8 * zoom, { cap = "round" })
	c:circle(x1, y1, 4 * zoom, "$accent")
	c:circle(x2, y2, 4 * zoom, "$accent")
end

function Canvas.draw_edges(c, cells, edges, zoom, pan_x, pan_y, selected, pending, blocks)
	local function screen(x, y)
		return pan_x + x * zoom, pan_y + y * zoom
	end
	blocks = blocks or {}
	for _, edge in edges do
		local from = Canvas.node(cells, edge.source_cell_id)
		local to = Canvas.node(cells, edge.target_cell_id)
		if from and to then
			local from_block, to_block = blocks[from.id], blocks[to.id]
			local x1, y1 = screen((from.x or 0) + Canvas.width, Canvas.port(from, edge.variable, true, from_block))
			local target_keys = Canvas.input_ports_for_variable(to_block, edge.variable)
			if #target_keys == 0 then target_keys = { edge.variable } end
			for _, target_key in target_keys do
				local x2, y2 = screen(to.x or 0, Canvas.port(to, target_key, false, to_block))
				stroke_edge(c, x1, y1, x2, y2, zoom)
			end
		end
	end
	if pending then
		local from = Canvas.node(cells, pending.from_cell_id)
		if from then
			local block = blocks[from.id]
			local x1, y1 = screen((from.x or 0) + Canvas.width, Canvas.port(from, pending.variable, true, block))
			local x2, y2 = screen(pending.x, pending.y)
			local bend = math.max(52 * zoom, math.abs(x2 - x1) * 0.42)
			c:stroke(ui.path():move(x1, y1):cubic(x1 + bend, y1, x2 - bend, y2, x2, y2), "$accent", 2 * zoom, { cap = "round" })
		end
	end
end

return Canvas
