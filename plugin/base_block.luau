-- Read-only projection of safe function contracts onto generated notebook cells.
local BaseBlock = {}

local function callable_for(component, name)
	local found = nil
	for _, callable in component.callables or {} do
		if callable.name == name then
			if found then return nil end
			found = callable
		end
	end
	return found
end

local function component_for(components, reference)
	local found = nil
	for _, component in components or {} do
		if component.id == reference.component_id and component.source_sha256 == reference.source_sha256 then
			if found then return nil end
			found = component
		end
	end
	return found
end

function BaseBlock.project(components, cells)
	local projected = {}
	for _, cell in cells or {} do
		local reference = cell.graphimo_component
		if type(reference) == "table"
			and type(reference.component_id) == "string"
			and type(reference.callable) == "string"
			and type(reference.source_sha256) == "string"
		then
			local component = component_for(components, reference)
			local callable = if component then callable_for(component, reference.callable) else nil
			local contract = if callable then callable.base_block else nil
			if type(contract) == "table"
				and type(contract.inputs) == "table"
				and type(contract.outputs) == "table"
				and (contract.output_mode == "none" or contract.output_mode == "single" or contract.output_mode == "tuple" or contract.output_mode == "object")
			then
				table.insert(projected, {
					cell_id = cell.id,
					component_id = reference.component_id,
					callable = reference.callable,
					inputs = contract.inputs,
					outputs = contract.outputs,
					output_mode = contract.output_mode,
				})
			end
		end
	end
	return projected
end

local function label(name, annotation, value)
	local text = if type(value) == "string" and value ~= "" then value else tostring(name or "")
	if type(annotation) == "string" and annotation ~= "" then
		return annotation .. " (" .. text .. ")"
	end
	return text
end

function BaseBlock.summary(projected_block)
	if type(projected_block) ~= "table" then return nil end
	local summary = { inputs = {}, outputs = {} }
	for _, input in projected_block.inputs or {} do
		if type(input) == "table" then
			table.insert(summary.inputs, label(input.name, input.annotation, nil))
		end
	end
	for _, output in projected_block.outputs or {} do
		if type(output) == "table" then
			table.insert(summary.outputs, label(output.name, output.annotation, output.expression))
		end
	end
	return summary
end

return BaseBlock
