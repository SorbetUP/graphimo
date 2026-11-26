-- Card height follows the fixed layout contract: a header of
-- Canvas.header_height, then one Canvas.port_row per port row, then the
-- preview strip. Port anchors never move when the preview changes.
local Canvas = require("../plugin/canvas")

local cell = {
	refs = { "input" },
	defs = { "output" },
	y = 40,
}
local base = Canvas.height(cell)
assert(base == Canvas.header_height + Canvas.port_row + 14, "base height = header + one port row + bottom padding")

local many = { refs = { "a", "b", "c", "d" }, defs = { "out" }, y = 0 }
assert(Canvas.height(many) == Canvas.header_height + 4 * Canvas.port_row + 14, "each extra port adds one row")

local with_preview = { refs = { "a" }, defs = { "out" }, y = 0, output = { preview = "[1, 2]" } }
assert(Canvas.height(with_preview) > Canvas.height({ refs = { "a" }, defs = { "out" }, y = 0 }), "a preview strip adds height")

local input_anchor = Canvas.port(cell, "input", false)
local output_anchor = Canvas.port(cell, "output", true)
assert(input_anchor == 40 + Canvas.header_height + Canvas.port_row / 2, "input anchor = cell top + header + half a row")
assert(output_anchor == input_anchor, "first input and first output share the first port row")
local cell_with_preview = table.clone(cell)
cell_with_preview.output = { preview = "x" }
assert(Canvas.port(cell_with_preview, "input", false) == input_anchor, "preview must not move port anchors")
assert(Canvas.port(cell_with_preview, "output", true) == output_anchor, "preview must not move port anchors")
print("BASE_BLOCK_HEIGHT_OK")
