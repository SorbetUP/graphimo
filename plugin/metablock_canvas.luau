-- Renderer-only projection from generic addon view data onto Graphimo's canvas.
local MetaCanvas = {}

local function index_catalog(catalog)
	local result = {}
	for _, item in catalog and catalog.items or {} do
		if type(item) == "table" and type(item.kind) == "string" then
			result[item.kind] = item
		end
	end
	return result
end

local function names(ports)
	local result = {}
	for _, port in ports or {} do
		if type(port) == "table" and type(port.name) == "string" then
			table.insert(result, port.name)
		end
	end
	return result
end

local function shape_summary(values)
	local result = {}
	for _, item in values or {} do
		if type(item) == "table" and type(item[1]) == "string" and type(item[2]) == "table" then
			local shape = item[2].shape or {}
			table.insert(result, item[1] .. " " .. tostring(item[2].dtype or "tensor") .. " [" .. table.concat(shape, ", ") .. "]")
		end
	end
	return table.concat(result, "\n")
end

function MetaCanvas.project(projection, catalog, metablock_id)
	projection = if type(projection) == "table" then projection else {}
	local definitions = index_catalog(catalog)
	local group_for_instance = {}
	for _, group in projection.group_navigation or {} do
		for _, instance_id in group.instance_ids or {} do
			group_for_instance[instance_id] = group.group_id
		end
	end
	local cells, ids = {}, {}
	for index, card in projection.cards or {} do
		if type(card) == "table" and type(card.element_id) == "string" then
			local id = "metablock:" .. metablock_id .. ":" .. card.element_id
			ids[card.element_id] = id
			local descriptor = definitions[card.kind] or {}
			local refs = names(descriptor.input_ports)
			local defs = names(descriptor.output_ports)
			if card.kind == "group_instance" then
				refs, defs = {}, {}
				for _, item in card.input_shapes or {} do
					if type(item) == "table" and type(item[1]) == "string" then table.insert(refs, item[1]) end
				end
				for _, item in card.output_shapes or {} do
					if type(item) == "table" and type(item[1]) == "string" then table.insert(defs, item[1]) end
				end
			end
			if card.kind == "input" and #defs == 0 then defs = { "out" } end
			if card.kind == "output" then refs = if #refs == 0 then { "x" } else refs end
			if #refs == 0 then
				for _, shape in card.input_shapes or {} do
					if type(shape) == "table" and type(shape[1]) == "string" then table.insert(refs, shape[1]) end
				end
			end
			if #defs == 0 then
				for _, shape in card.output_shapes or {} do
					if type(shape) == "table" and type(shape[1]) == "string" then table.insert(defs, shape[1]) end
				end
			end
			local diagnostics = card.diagnostics or {}
			local preview = shape_summary(card.output_shapes)
			table.insert(cells, {
				id = id,
				name = card.name or card.kind,
				kind = "metablock_element",
				source = "MetaBlock element · " .. tostring(card.kind),
				status = if #diagnostics > 0 then "error" else "idle",
				refs = refs,
				defs = defs,
				inputs = {},
				outputs = {},
				output = if preview ~= "" then { mimetype = "text/plain", preview = preview } else nil,
				errors = diagnostics,
				meta_card = card,
				group_id = group_for_instance[card.element_id],
				x = 80 + ((index - 1) % 4) * 320,
				y = 80 + math.floor((index - 1) / 4) * 230,
			})
		end
	end
	local edges = {}
	for _, edge in projection.edges or {} do
		if type(edge) == "table" and ids[edge.source_element_id] and ids[edge.target_element_id] then
			table.insert(edges, {
				id = edge.edge_id,
				source_cell_id = ids[edge.source_element_id],
				target_cell_id = ids[edge.target_element_id],
				variable = edge.source_port,
				output_name = edge.source_port,
				input_name = edge.target_port,
				state = if #(edge.diagnostics or {}) > 0 then "error" else "valid",
			})
		end
	end
	local components = {}
	for _, item in catalog and catalog.items or {} do
		if type(item) == "table" and type(item.kind) == "string" and item.kind ~= "template" then
			local parameters = {}
			for _, parameter in item.parameters or {} do
				if type(parameter) == "table" and type(parameter.name) == "string" then
					table.insert(parameters, {
						name = parameter.name,
						required = table.find(item.required_parameters or {}, parameter.name) ~= nil,
						positional = true,
						has_default = parameter.default ~= nil,
						default = parameter.default,
						annotation = parameter.type,
					})
				end
			end
			table.insert(components, {
				id = "metablock-element:" .. item.kind,
				category = item.category or "Model",
				kind = item.kind == "template" and "metablock_template" or "metablock_element",
				description = item.description,
				meta_kind = item.id or item.kind,
				callables = {{
					name = item.title or item.kind,
					signature = "",
					description = item.description,
					parameters = parameters,
				}},
			})
		end
	end
	return {
		cells = cells,
		edges = edges,
		components = components,
		projection = projection,
		catalog = catalog,
		metablock_id = metablock_id,
	}
end

return MetaCanvas
