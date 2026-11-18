-- Pure projection of discovered component metadata for the Components panel.
local Library = {}

local function category_for(item)
	local category = item.category
	if type(category) == "string" and category ~= "" then return category end
	if type(category) == "table" then
		local parts = {}
		for _, part in category do
			if type(part) == "string" and part ~= "" then table.insert(parts, part) end
		end
		if #parts > 0 then return table.concat(parts, " / ") end
	end
	return "Components"
end

local function contains(value, query)
	return type(value) == "string" and string.find(string.lower(value), query, 1, true) ~= nil
end

local function matches(item, category, query)
	if query == "" or contains(item.id, query) or contains(category, query) then return true end
	for _, callable in item.callables or {} do
		if contains(callable.name, query) or contains(callable.signature, query) or contains(callable.description, query) then
			return true
		end
	end
	return false
end

local function less(a, b)
	local lower_a, lower_b = string.lower(a), string.lower(b)
	if lower_a == lower_b then return a < b end
	return lower_a < lower_b
end

function Library.project(components, query)
	local search = if type(query) == "string" then (string.match(query, "^%s*(.-)%s*$") or "") else ""
	search = string.lower(search)
	local grouped = {}
	for _, item in components or {} do
		local category = category_for(item)
		if matches(item, category, search) then
			grouped[category] = grouped[category] or {}
			table.insert(grouped[category], item)
		end
	end

	local categories = {}
	for category in grouped do table.insert(categories, category) end
	table.sort(categories, less)

	local result = {}
	for _, category in categories do
		local items = grouped[category]
		table.sort(items, function(a, b) return less(tostring(a.id or ""), tostring(b.id or "")) end)
		table.insert(result, { category = category, components = items })
	end
	return result
end

return Library
