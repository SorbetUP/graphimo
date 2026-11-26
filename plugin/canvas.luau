local Canvas = {}
Canvas.width = 260

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
	local base_height = math.max(146, 120 + (#(cell.refs or {}) + #(cell.defs or {})) * 22 + media)
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

function Canvas.port(cell, variable, output)
	local list = if output then (cell.defs or {}) else (cell.refs or {})
	for index, name in list do
		if name == variable then return (cell.y or 0) + 55 + (index - 1) * 22 end
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
			local target_port = edge.target_variable or edge.variable
			local x1, y1 = screen((from.x or 0) + Canvas.width, Canvas.port(from, source_port, true))
			local x2, y2 = screen(to.x or 0, Canvas.port(to, target_port, false))
			local bend = math.max(52 * zoom, math.abs(x2 - x1) * 0.42)
			local path = ui.path():move(x1, y1):cubic(x1 + bend, y1, x2 - bend, y2, x2, y2)
			c:stroke(path, "$background", 5 * zoom, { cap = "round" })
			c:stroke(path, "$text.secondary", 1.8 * zoom, { cap = "round" })
			c:circle(x1, y1, 4 * zoom, "$accent")
			c:circle(x2, y2, 4 * zoom, "$accent")
		end
	end
	if pending then
		local from = Canvas.node(cells, pending.from_cell_id)
		if from then
			local x1, y1 = screen((from.x or 0) + Canvas.width, Canvas.port(from, pending.variable, true))
			local x2, y2 = screen(pending.x, pending.y)
			local bend = math.max(52 * zoom, math.abs(x2 - x1) * 0.42)
			c:stroke(ui.path():move(x1, y1):cubic(x1 + bend, y1, x2 - bend, y2, x2, y2), "$accent", 2 * zoom, { cap = "round" })
		end
	end
end

return Canvas
