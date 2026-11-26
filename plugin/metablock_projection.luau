-- Renderer adapter from a dialect-owned MetaBlock projection to canvas cards.
local Projection = {}

function Projection.annotate_parent_cells(cells)
	local result = {}
	for _, original in cells or {} do
		local cell = table.clone(original)
		local source = if type(cell.source) == "string" then cell.source else ""
		if string.find(source, "materialize_definition", 1, true) then
			local metablock_id = string.match(source, "metablock_id%s*=%s*['\"]([%w_-]+)['\"]")
			if metablock_id then
				cell.graphimo_metablock_id = metablock_id
				cell.name = "PyTorch Model · " .. metablock_id
				cell.source = "Graphimo PyTorch MetaBlock definition"
			end
		end
		table.insert(result, cell)
	end
	return result
end

local function port_names(ports)
	local result = {}
	for _, port in ports or {} do
		if type(port) == "table" and type(port.name) == "string" then
			table.insert(result, port.name)
		end
	end
	return result
end

local function output_summary(shape)
	if type(shape) ~= "table" then return nil end
	local dimensions = {}
	for _, part in shape.shape or {} do table.insert(dimensions, tostring(part)) end
	local text = if #dimensions > 0 then "[" .. table.concat(dimensions, ", ") .. "]" else "shape unknown"
	if type(shape.dtype) == "string" and shape.dtype ~= "" then text ..= " · " .. shape.dtype end
	if type(shape.semantic) == "string" and shape.semantic ~= "" then text ..= " · " .. shape.semantic end
	return text
end

function Projection.scope_layout_id(metablock_id, scope)
	local identity = tostring(metablock_id) .. "\0" .. tostring(scope or "root")
	local hash = 5381
	for index = 1, #identity do
		hash = (hash * 33 + string.byte(identity, index)) % 4294967296
	end
	local safe_id = string.gsub(tostring(metablock_id), "[^A-Za-z0-9_-]", "_")
	return "mb_" .. string.sub(safe_id, 1, 64) .. "_" .. string.format("%.0f", hash)
end

function Projection.to_canvas(metablock_id, scope, project_path, value)
	local projection = if type(value) == "table" then value.projection else nil
	if type(projection) ~= "table" then
		return {
			notebook = { path = project_path, kind = "metablock", metablock_id = metablock_id },
			cells = {},
			edges = {},
		}, nil
	end
	local diagnostics = projection.diagnostics or {}
	local cells = {}
	for _, element in projection.elements or {} do
		local has_error = false
		for _, diagnostic in diagnostics do
			if diagnostic.severity == "error" and diagnostic.element_id == element.id then has_error = true end
		end
		local shape = output_summary(element.output_shape)
		table.insert(cells, {
			id = element.id,
			name = element.name or element.id,
			source = element.kind or "PyTorch component",
			status = if has_error then "error" else "idle",
			refs = port_names(element.inputs),
			defs = port_names(element.outputs),
			output = if shape then { mimetype = "text/plain", preview = "Output " .. shape } else nil,
			graphimo_metablock_element = true,
			kind = element.kind,
		})
	end
	local edges = {}
	for _, edge in projection.edges or {} do
		if type(edge.source) == "table" and type(edge.target) == "table" then
			table.insert(edges, {
				id = edge.id,
				source_cell_id = edge.source.element_id,
				target_cell_id = edge.target.element_id,
				source_variable = edge.source.port,
				target_variable = edge.target.port,
			})
		end
	end
	return {
		notebook = {
			path = project_path,
			kind = "metablock",
			metablock_id = metablock_id,
			name = projection.name,
			dialect_id = projection.dialect_id,
			scope = scope,
		},
		cells = cells,
		edges = edges,
	}, projection
end

return Projection
