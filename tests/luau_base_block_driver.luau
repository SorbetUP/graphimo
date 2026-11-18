-- Pure Base Block projection contract: ordered descriptors and exact references.
local BaseBlock = require("../plugin/base_block")

local inputs = {
	{ name = "prix", required = true, positional = true },
	{ name = "quantite", required = true, positional = true },
	{ name = "stock", required = true, positional = true },
}
local outputs = {
	{ index = 1, name = "output_1", expression = "prix + 0.2", annotation = "float" },
	{ index = 2, name = "output_2", expression = "quantite + stock", annotation = "int" },
	{ index = 3, name = "flower", expression = "flower", annotation = "int" },
}
local components = {
	{
		id = "calculs/infos.py",
		source_sha256 = "abc123",
		callables = {
			{
				name = "obtenir_infos",
				base_block = { inputs = inputs, outputs = outputs, output_mode = "tuple" },
			},
		},
	},
	{
		id = "math.py",
		source_sha256 = "xyz789",
		callables = {
			{ name = "scale", base_block = { inputs = {}, outputs = { { index = 1, name = "result" } }, output_mode = "single" } },
		},
	},
}
local cells = {
	{ id = "instance-a", graphimo_component = { component_id = "calculs/infos.py", callable = "obtenir_infos", source_sha256 = "abc123" } },
	{ id = "instance-b", graphimo_component = { component_id = "calculs/infos.py", callable = "obtenir_infos", source_sha256 = "abc123" } },
	{ id = "stale", graphimo_component = { component_id = "calculs/infos.py", callable = "obtenir_infos", source_sha256 = "oldhash" } },
	{ id = "wrong-callable", graphimo_component = { component_id = "calculs/infos.py", callable = "scale", source_sha256 = "abc123" } },
	{ id = "wrong-component", graphimo_component = { component_id = "missing.py", callable = "obtenir_infos", source_sha256 = "abc123" } },
	{ id = "legacy" },
	{ id = "single", graphimo_component = { component_id = "math.py", callable = "scale", source_sha256 = "xyz789" } },
}

local projected = BaseBlock.project(components, cells)
assert(#projected == 3, "only exact current component references should project")
assert(projected[1].cell_id == "instance-a" and projected[2].cell_id == "instance-b", "projection must preserve cell order and duplicate instances")
assert(projected[1].component_id == "calculs/infos.py" and projected[1].callable == "obtenir_infos", "component/callable identity mismatch")
assert(projected[1].output_mode == "tuple" and #projected[1].inputs == 3 and #projected[1].outputs == 3, "Base Block descriptors were not preserved")
assert(projected[1].inputs[1].name == "prix" and projected[1].inputs[3].name == "stock", "input signature order changed")
assert(projected[1].outputs[1].name == "output_1" and projected[1].outputs[3].name == "flower", "return order changed")
assert(projected[3].cell_id == "single" and projected[3].output_mode == "single", "later matching cell missing")
assert(#components == 2 and #cells == 7 and cells[3].graphimo_component.source_sha256 == "oldhash", "projection mutated input")
print("BASE_BLOCK_PROJECTION_OK")
