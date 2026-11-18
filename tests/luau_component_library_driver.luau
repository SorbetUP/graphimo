-- Production projection contract: pure grouping and search, no Zeron UI/bridge.
local Library = require("../plugin/component_library")

local components = {
	{
		id = "zeta/plot.py",
		category = { "Visualization" },
		callables = {
			{ name = "render", signature = "(data)", description = "Build chart" },
		},
	},
	{
		id = "ml/train.py",
		category = { "Machine Learning" },
		callables = {
			{ name = "train_model", signature = "(data, epochs=10)", description = "Fit predictor" },
			{ name = "select_row", signature = "(rows[0])", description = "Pick one row" },
		},
	},
	{
		id = "ml/data.py",
		category = { "Machine Learning" },
		callables = {
			{ name = "prepare", signature = "(raw)", description = "Prepare rows" },
		},
	},
	{
		id = "root.py",
		callables = {
			{ name = "load", signature = "(path)", description = "Load input" },
		},
	},
}

local function expect_categories(groups, expected)
	assert(#groups == #expected, "unexpected number of category groups")
	for index, category in expected do
		assert(groups[index].category == category, "category ordering/group mismatch at " .. tostring(index))
	end
end

local all = Library.project(components, "")
expect_categories(all, { "Components", "Machine Learning", "Visualization" })
assert(all[1].components[1] == components[4], "root-level item metadata was not preserved")
assert(all[2].components[1] == components[3], "components in a category must sort by ID")
assert(all[2].components[2] == components[2], "all items in the category should remain grouped")
assert(all[2].components[2].callables[2].name == "select_row", "callable metadata was not preserved")

local by_id = Library.project(components, "ZETA/PLOT")
assert(#by_id == 1 and by_id[1].components[1] == components[1], "ID search must be case-insensitive")

local by_category = Library.project(components, "machine learning")
assert(#by_category == 1 and #by_category[1].components == 2, "category search must include its components")

local by_name = Library.project(components, "TRAIN_MODEL")
assert(#by_name == 1 and by_name[1].components[1] == components[2], "callable-name search failed")

local by_signature = Library.project(components, "epochs=10")
assert(#by_signature == 1 and by_signature[1].components[1] == components[2], "callable-signature search failed")

local by_description = Library.project(components, "FIT PREDICTOR")
assert(#by_description == 1 and by_description[1].components[1] == components[2], "callable-description search failed")

local literal = Library.project(components, "[")
assert(#literal == 1 and literal[1].components[1] == components[2], "search must treat query as a literal substring")

local no_matches = Library.project(components, "not-present")
assert(type(no_matches) == "table" and #no_matches == 0, "a no-match query must return an empty array")

assert(#components == 4 and #components[2].callables == 2, "projection mutated component input")
assert(components[2].category[1] == "Machine Learning", "projection mutated category metadata")
print("COMPONENT_LIBRARY_PROJECTION_OK")
