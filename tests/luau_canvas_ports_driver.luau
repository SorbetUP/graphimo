-- Formal @node ports drive the canvas for Graphimo instances; ordinary marimo
-- cells keep lexical refs/defs as ports.
ui = {
	path = function()
		local p = {}
		function p:move() return self end
		function p:cubic() return self end
		return p
	end,
}

local Canvas = require("../plugin/canvas")
getfenv(Canvas.draw_edges).ui = ui

local instance_cell = {
	id = "inst",
	y = 10,
	refs = { "__graphimo_c_ab12cd34_split", "data" },
	defs = { "g_split_aabbccdd" },
	graphimo_instance = {
		instance_id = "aabbccdd",
		node = "split",
		component_id = "images/split.py",
		result_var = "g_split_aabbccdd",
		inputs = {
			{ name = "value", binding = { kind = "variable", expression = "data" } },
			{ name = "ratio", binding = { kind = "default", expression = nil, default = 0.5 } },
			{ name = "seed", binding = { kind = "unbound", expression = nil } },
		},
		outputs = {
			{ index = 0, name = "left" },
			{ index = 1, name = "right" },
			{ index = 2, name = "output_3" },
		},
	},
}

local inputs = Canvas.input_ports(instance_cell)
assert(#inputs == 3 and inputs[1] == "value" and inputs[3] == "seed", "formal inputs must come from graphimo_instance, not refs")
local outputs = Canvas.output_ports(instance_cell)
assert(#outputs == 3 and outputs[1] == "0" and outputs[3] == "2", "formal outputs must be zero-based index keys")
assert(Canvas.port(instance_cell, "2", true) == Canvas.port(instance_cell, "seed", false), "third output and third input share the same stacked offset")
assert(Canvas.port(instance_cell, "value", false) == 10 + 55, "first input port anchor must be stable")
assert(Canvas.port(instance_cell, "0", true) == 10 + 55, "first output port anchor must be stable")
assert(Canvas.input_label(instance_cell, "ratio") == "ratio (default)", "default binding label missing")
assert(Canvas.input_label(instance_cell, "seed") == "seed (unbound)", "unbound binding label missing")
assert(Canvas.input_label(instance_cell, "value") == "value", "variable binding must not add a marker")
assert(Canvas.output_label(instance_cell, "1") == "right", "output label must come from the instance projection")
assert(Canvas.height(instance_cell) >= 120 + (3 + 3) * 22, "card height must count formal ports")

local plain = { id = "plain", y = 0, refs = { "data" }, defs = { "out" } }
assert(#Canvas.input_ports(plain) == 1 and Canvas.input_ports(plain)[1] == "data", "plain cells keep refs as input ports")
assert(#Canvas.output_ports(plain) == 1 and Canvas.output_ports(plain)[1] == "out", "plain cells keep defs as output ports")
assert(Canvas.port(plain, "out", true) == 55 and Canvas.port(plain, "data", false) == 55, "plain cell anchors must be unchanged")

-- Edges annotate the source output index and the target input name.
local edges = {
	{ source_cell_id = "inst", target_cell_id = "plain", variable = "g_split_aabbccdd", output_index = 2, input_ref = "data" },
	{ source_cell_id = "plain", target_cell_id = "inst", variable = "data" },
}
local drawn = {}
local c = {
	stroke = function() end,
	circle = function(_, x, y) table.insert(drawn, { x = x, y = y }) end,
}
local cells = { instance_cell, plain }
Canvas.draw_edges(c, cells, edges, 1, 0, 0, "", nil)
assert(#drawn == 4, "both edge endpoints must be drawn")
assert(drawn[1].x == Canvas.width, "source endpoint must sit on the right edge of the source card")
assert(drawn[1].y == Canvas.port(instance_cell, "2", true), "source endpoint must use the annotated output_index port")
assert(drawn[2].y == Canvas.port(plain, "data", false), "target endpoint must use the annotated input_ref port")
assert(drawn[3].y == Canvas.port(plain, "out", true), "plain source edge keeps its def port")
assert(drawn[4].y == Canvas.port(instance_cell, "data", false) or drawn[4].y == Canvas.port(instance_cell, "value", false), "plain-to-instance edge lands on a formal input port")
print("CANVAS_PORTS_OK")
