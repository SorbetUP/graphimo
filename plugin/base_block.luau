-- Graphimo's visual contract for function-backed marimo cells.
-- A Base Block is the marimo cell itself. This module only enriches that cell
-- with signature/return metadata from a referenced component; it never creates
-- a second execution model.
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

local function runtime_value(cell, variable)
	if type(variable) ~= "string" or variable == "" or type(cell.values) ~= "table" then return nil end
	local value = cell.values[variable]
	return if type(value) == "table" then value else nil
end

local function binding_for(reference, name)
	if type(reference.inputs) ~= "table" then return nil end
	local binding = reference.inputs[name]
	return if type(binding) == "table" then binding else nil
end

local function output_variable(reference, index, cell, count)
	if type(reference.outputs) == "table" and type(reference.outputs[index]) == "string" then
		return reference.outputs[index]
	end
	-- Backwards-compatible fallback for cells generated before Graphimo began
	-- recording destructured output bindings.
	if count == 1 and #(cell.defs or {}) == 1 then return cell.defs[1] end
	return nil
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
				local inputs, outputs = {}, {}
				for _, input in contract.inputs do
					if type(input) == "table" and type(input.name) == "string" then
						local port = table.clone(input)
						port.binding = binding_for(reference, input.name)
						port.port_key = input.name
						table.insert(inputs, port)
					end
				end
				for index, output in contract.outputs do
					if type(output) == "table" then
						local port = table.clone(output)
						port.variable = output_variable(reference, index, cell, #contract.outputs)
						port.runtime = runtime_value(cell, port.variable)
						port.port_key = tostring(index)
						table.insert(outputs, port)
					end
				end
				table.insert(projected, {
					cell_id = cell.id,
					component_id = reference.component_id,
					callable = reference.callable,
					inputs = inputs,
					outputs = outputs,
					output_mode = contract.output_mode,
				})
			end
		end
	end
	return projected
end

function BaseBlock.by_cell(components, cells)
	local result = {}
	for _, block in BaseBlock.project(components, cells) do result[block.cell_id] = block end
	return result
end

local function simple_value(value)
	if value == nil then return "None" end
	if type(value) == "string" then return value end
	return tostring(value)
end

function BaseBlock.input_text(input)
	local name = tostring(input.name or "")
	local annotation = if type(input.annotation) == "string" and input.annotation ~= "" then input.annotation else "Any"
	local shown = nil
	local binding = input.binding
	if type(binding) == "table" and binding.kind == "literal" then
		shown = simple_value(binding.value)
	elseif input.has_default == true and input.default ~= nil then
		shown = simple_value(input.default)
	elseif input.has_default == true and input.default == nil then
		shown = "None"
	end
	if shown ~= nil then
		return {
			short = name .. " (" .. shown .. ")",
			full = name .. " (" .. shown .. " | " .. annotation .. ")",
		}
	end
	return { short = name .. " (" .. annotation .. ")", full = name .. " (" .. annotation .. ")" }
end

function BaseBlock.output_text(output)
	local annotation = if type(output.annotation) == "string" and output.annotation ~= "" then output.annotation else nil
	local runtime = output.runtime
	local type_name = annotation
	if not type_name and type(runtime) == "table" and type(runtime.type) == "string" then type_name = runtime.type end
	type_name = type_name or "Any"
	local expression = if type(output.expression) == "string" and output.expression ~= "" then output.expression else tostring(output.name or "result")
	local value = nil
	if type(runtime) == "table" and runtime.has_value == true then value = simple_value(runtime.value) end
	local body = if value ~= nil then value .. " | " .. expression else expression
	return { short = type_name .. " (" .. body .. ")", full = type_name .. " (" .. body .. ")" }
end

-- Kept for old tests/consumers while the UI migrates to the port model.
function BaseBlock.summary(projected_block)
	if type(projected_block) ~= "table" then return nil end
	local summary = { inputs = {}, outputs = {} }
	for _, input in projected_block.inputs or {} do table.insert(summary.inputs, BaseBlock.input_text(input).short) end
	for _, output in projected_block.outputs or {} do table.insert(summary.outputs, BaseBlock.output_text(output).short) end
	return summary
end

return BaseBlock
