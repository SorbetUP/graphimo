local Projection = require("../../plugin/metablock_projection")

local view, projection = Projection.to_canvas("model_1", "forward", "/tmp/project.py", {
	available = true,
	projection = {
		name = "Tiny",
		dialect_id = "pytorch.model",
		elements = {
			{
				id = "linear_1",
				kind = "linear",
				name = "Dense",
				inputs = { { name = "x" } },
				outputs = { { name = "y" } },
				output_shape = { shape = { "batch", "8" }, dtype = "float32" },
			},
		},
		edges = {
			{
				id = "edge_1",
				source = { element_id = "linear_1", port = "y" },
				target = { element_id = "linear_2", port = "x" },
			},
		},
	},
})

assert(projection.name == "Tiny")
assert(view.notebook.metablock_id == "model_1")
assert(view.cells[1].id == "linear_1")
assert(view.cells[1].refs[1] == "x")
assert(view.cells[1].defs[1] == "y")
assert(view.cells[1].output.preview == "Output [batch, 8] · float32")
assert(view.edges[1].source_variable == "y")
assert(view.edges[1].target_variable == "x")
assert(Projection.scope_layout_id("model_1", "group:abc") ~= Projection.scope_layout_id("model_1", "method:group_abc"))

local parent_cells = Projection.annotate_parent_cells({
	{ id = "cell_1", source = "g_pytorch_model_1 = materialize_definition(metablock_id='model_1')", defs = { "g_pytorch_model_1" } },
	{ id = "cell_2", source = "print('ordinary')", defs = {} },
})
assert(parent_cells[1].graphimo_metablock_id == "model_1")
assert(parent_cells[1].name == "MetaBlock · model_1")
assert(parent_cells[1].defs[1] == "g_pytorch_model_1")
assert(parent_cells[2].graphimo_metablock_id == nil)

local unavailable = Projection.to_canvas("model_2", "root", "/tmp/project.py", {
	available = false,
})
assert(#unavailable.cells == 0)

print("metablock-projection-ok")
