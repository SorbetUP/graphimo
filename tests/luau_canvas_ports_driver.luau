-- Formal @node ports drive the canvas for Graphimo instances; ordinary marimo
-- cells keep lexical refs/defs as ports. Port geometry is shared by the edge
-- endpoints, the drawn dots and hit-testing through Canvas.port.
local function path_mock()
	local p = { points = {} }
	function p:move(x, y) table.insert(p.points, { x = x, y = y }); return self end
	function p:line(x, y) table.insert(p.points, { x = x, y = y }); return self end
	function p:cubic(x1, y1, x2, y2, x, y) table.insert(p.points, { x = x, y = y }); return self end
	return p
end
ui = { path = path_mock }

local Canvas = require("../plugin/canvas")
getfenv(Canvas.draw_edges).ui = ui
getfenv(Canvas.draw_ports).ui = ui

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
local first_anchor = 10 + Canvas.header_height + Canvas.port_row / 2
assert(Canvas.port(instance_cell, "value", false) == first_anchor, "first input port anchor must be stable")
assert(Canvas.port(instance_cell, "0", true) == first_anchor, "first output port anchor must be stable")
assert(Canvas.input_label(instance_cell, "ratio") == "ratio (default)", "default binding label missing")
assert(Canvas.input_label(instance_cell, "seed") == "seed (unbound)", "unbound binding label missing")
assert(Canvas.input_label(instance_cell, "value") == "value", "variable binding must not add a marker")
assert(Canvas.output_label(instance_cell, "1") == "right", "output label must come from the instance projection")
assert(Canvas.input_bound(instance_cell, "value") == true and Canvas.input_bound(instance_cell, "seed") == false, "bound vs unbound ports")
assert(Canvas.height(instance_cell) >= Canvas.header_height + 3 * Canvas.port_row, "card height must count formal ports")

local plain = { id = "plain", y = 0, refs = { "data" }, defs = { "out" } }
assert(#Canvas.input_ports(plain) == 1 and Canvas.input_ports(plain)[1] == "data", "plain cells keep refs as input ports")
assert(#Canvas.output_ports(plain) == 1 and Canvas.output_ports(plain)[1] == "out", "plain cells keep defs as output ports")
assert(Canvas.port(plain, "out", true) == Canvas.header_height + Canvas.port_row / 2 and Canvas.port(plain, "data", false) == Canvas.header_height + Canvas.port_row / 2, "plain cell anchors follow the card header")

-- Edges annotate the source output index and the target input name; each edge
-- ends in an arrowhead whose tip is exactly the target port anchor.
local edges = {
	{ source_cell_id = "inst", target_cell_id = "plain", variable = "g_split_aabbccdd", output_index = 2, input_ref = "data" },
	{ source_cell_id = "plain", target_cell_id = "inst", variable = "data" },
}
local strokes = {}
local c = {
	stroke = function(_, path) table.insert(strokes, path) end,
	circle = function() end,
}
local cells = { instance_cell, plain }
Canvas.draw_edges(c, cells, edges, 1, 0, 0, "", nil)
-- 2 strokes (halo + line) + 1 arrowhead per edge
assert(#strokes == 6, "each edge draws a halo, a line and an arrowhead, got " .. #strokes)
local arrow1 = strokes[3]
local tip1 = arrow1.points[2]
assert(tip1.x == 0 and tip1.y == Canvas.port(plain, "data", false), "edge 1 arrow tip must be the annotated input_ref port on the target left edge")
local arrow2 = strokes[6]
local tip2 = arrow2.points[2]
assert(tip2.x == 0 and tip2.y == Canvas.port(instance_cell, "data", false), "edge 2 arrow tip must land on the instance input row")
print("CANVAS_PORTS_OK")

-- Port dots must share the exact Canvas.port geometry used by edges and
-- hit-testing (single geometry source).
local dots = {}
local pc = { circle = function(_, x, y) table.insert(dots, { x = x, y = y }) end }
Canvas.draw_ports(pc, cells, 2, 10, 20)
local inst_dot = nil
for _, dot in dots do
	if dot.y == 20 + Canvas.port(instance_cell, "0", true) * 2 and dot.x == 10 + Canvas.width * 2 then
		inst_dot = dot
	end
end
assert(inst_dot ~= nil, "output port dot must sit at screen(Canvas.width, Canvas.port(...))")
print("CANVAS_PORTS_GEOMETRY_OK")
