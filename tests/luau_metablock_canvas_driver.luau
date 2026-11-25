-- Addon projection renders through the existing Graphimo canvas data contract.
local MetaCanvas = require("../plugin/metablock_canvas")

local view = MetaCanvas.project({
	method_tabs = { "forward", "loss" },
	cards = {
		{ element_id = "input-x", kind = "input", name = "x", input_shapes = {}, output_shapes = {{ "out", { shape = { 2, 8 }, dtype = "float32" } }}, diagnostics = {} },
		{ element_id = "linear-a", kind = "linear", name = "projection", input_shapes = {{ "x", { shape = { 2, 8 }, dtype = "float32" } }}, output_shapes = {{ "out", { shape = { 2, 4 }, dtype = "float32" } }}, diagnostics = {} },
	},
	edges = {{ edge_id = "edge-x", source_element_id = "input-x", source_port = "out", target_element_id = "linear-a", target_port = "x", diagnostics = {} }},
}, {
	items = {
		{ kind = "input", title = "Input", output_ports = {{ name = "out" }} },
		{ kind = "linear", title = "Linear", input_ports = {{ name = "x" }}, output_ports = {{ name = "out" }}, parameters = {{ name = "out_features", type = "int", default = 4 }}, required_parameters = { "out_features" } },
	},
}, "model-a")

assert(#view.cells == 2 and view.cells[2].id == "metablock:model-a:linear-a", "projection cards must map to stable canvas block IDs")
assert(view.cells[2].refs[1] == "x" and view.cells[2].defs[1] == "out", "catalog port schemas must project onto canvas ports")
assert(view.edges[1].source_cell_id == view.cells[1].id and view.edges[1].target_cell_id == view.cells[2].id, "addon edges must map to canvas edges")
assert(view.edges[1].output_name == "out" and view.edges[1].input_name == "x", "port names must remain exact")
assert(#view.components == 2 and view.components[2].meta_kind == "linear", "contextual addon palette metadata must be preserved")
assert(view.components[2].callables[1].parameters[1].required, "required parameter metadata must be exposed to the renderer")
print("METABLOCK_CANVAS_PROJECTION_OK")
