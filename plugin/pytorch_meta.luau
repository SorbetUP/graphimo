local Meta = {}

Meta.node_width = 170
Meta.node_height = 72

Meta.catalog = {
	{ kind = "input", label = "Input", category = "Structure", defaults = { name = "x" } },
	{ kind = "output", label = "Output", category = "Structure", defaults = { name = "output" } },
	{ kind = "module", op = "Conv1d", label = "Conv1d", category = "Layers", defaults = { in_channels = 1, out_channels = 16, kernel_size = 3, stride = 1, padding = 0, bias = true } },
	{ kind = "module", op = "Conv2d", label = "Conv2d", category = "Layers", defaults = { in_channels = 3, out_channels = 16, kernel_size = 3, stride = 1, padding = 0, bias = true } },
	{ kind = "module", op = "Conv3d", label = "Conv3d", category = "Layers", defaults = { in_channels = 1, out_channels = 8, kernel_size = 3, stride = 1, padding = 0, bias = true } },
	{ kind = "module", op = "Linear", label = "Linear", category = "Layers", defaults = { in_features = 128, out_features = 10, bias = true } },
	{ kind = "module", op = "ReLU", label = "ReLU", category = "Activations", defaults = { inplace = false } },
	{ kind = "module", op = "GELU", label = "GELU", category = "Activations", defaults = {} },
	{ kind = "module", op = "BatchNorm1d", label = "BatchNorm1d", category = "Normalization", defaults = { num_features = 128 } },
	{ kind = "module", op = "BatchNorm2d", label = "BatchNorm2d", category = "Normalization", defaults = { num_features = 16 } },
	{ kind = "module", op = "LayerNorm", label = "LayerNorm", category = "Normalization", defaults = { normalized_shape = 128 } },
	{ kind = "module", op = "Dropout", label = "Dropout", category = "Regularization", defaults = { p = 0.5 } },
	{ kind = "module", op = "Flatten", label = "Flatten", category = "Shape", defaults = { start_dim = 1, end_dim = -1 } },
	{ kind = "module", op = "MaxPool2d", label = "MaxPool2d", category = "Pooling", defaults = { kernel_size = 2, stride = 2, padding = 0 } },
	{ kind = "module", op = "AdaptiveAvgPool2d", label = "AdaptiveAvgPool2d", category = "Pooling", defaults = { output_size = { 1, 1 } } },
	{ kind = "module", op = "Identity", label = "Identity", category = "Layers", defaults = {} },
	{ kind = "merge", op = "Add", label = "Add", category = "Merge", defaults = {} },
	{ kind = "merge", op = "Concat", label = "Concat", category = "Merge", defaults = { dim = 1 } },
	{ kind = "custom", op = "Custom", label = "Custom nn.Module", category = "Custom", defaults = { module = "", class_name = "", kwargs = {} } },
}

local function clone(value)
	if type(value) ~= "table" then return value end
	local out = {}
	for key, child in value do out[key] = clone(child) end
	return out
end

function Meta.new_node(definition, id, x, y)
	return {
		id = id,
		kind = definition.kind,
		op = definition.op,
		label = definition.label,
		params = clone(definition.defaults or {}),
		x = x,
		y = y,
	}
end

function Meta.find(nodes, id)
	for _, node in nodes or {} do if node.id == id then return node end end
	return nil
end

function Meta.has_input(node)
	return node ~= nil and node.kind ~= "input"
end

function Meta.has_output(node)
	return node ~= nil and node.kind ~= "output"
end

function Meta.draw_edges(c, nodes, edges, pending)
	for _, edge in edges or {} do
		local from, to = Meta.find(nodes, edge.from), Meta.find(nodes, edge.to)
		if from and to then
			local x1, y1 = (from.x or 0) + Meta.node_width, (from.y or 0) + Meta.node_height / 2
			local x2, y2 = to.x or 0, (to.y or 0) + Meta.node_height / 2
			local bend = math.max(45, math.abs(x2 - x1) * 0.4)
			local path = ui.path():move(x1, y1):cubic(x1 + bend, y1, x2 - bend, y2, x2, y2)
			c:stroke(path, "$background", 5, { cap = "round" })
			c:stroke(path, "$text.secondary", 1.6, { cap = "round" })
		end
	end
	if pending then
		local from = Meta.find(nodes, pending.from)
		if from then
			local x1, y1 = (from.x or 0) + Meta.node_width, (from.y or 0) + Meta.node_height / 2
			local x2, y2 = pending.x, pending.y
			local bend = math.max(45, math.abs(x2 - x1) * 0.4)
			c:stroke(ui.path():move(x1, y1):cubic(x1 + bend, y1, x2 - bend, y2, x2, y2), "$accent", 2, { cap = "round" })
		end
	end
end

function Meta.menu_entries(add)
	local categories, order = {}, {}
	for _, definition in Meta.catalog do
		if not categories[definition.category] then categories[definition.category] = {}; table.insert(order, definition.category) end
		local captured = definition
		table.insert(categories[definition.category], { label = definition.label, on_select = function() add(captured) end })
	end
	local result = {}
	for _, category in order do table.insert(result, { label = category, submenu = categories[category] }) end
	return result
end

return Meta
