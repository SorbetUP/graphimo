-- Card height must account for the summary rows without moving port anchors.
local Canvas = require("../plugin/canvas")

local cell = {
	refs = { "input" },
	defs = { "output" },
	y = 40,
}
local generic_height = Canvas.height(cell)
assert(Canvas.height(cell, nil) == generic_height, "nil summary must preserve generic card height")
assert(Canvas.height(cell, { inputs = {}, outputs = {} }) == generic_height, "empty summary must preserve generic card height")

local input_port_before = Canvas.port(cell, "input", false)
local output_port_before = Canvas.port(cell, "output", true)
local short = { inputs = { "x" }, outputs = { "y" } }
local short_height = Canvas.height(cell, short)
assert(short_height == generic_height + 40, "one short In and Out row should reserve 20 units each")

local long_label = string.rep("a", 60)
local long = { inputs = { long_label }, outputs = {} }
local long_height = Canvas.height(cell, long)
local rendered_input = "In: " .. long_label
local expected_lines = math.ceil(#rendered_input / 24)
assert(long_height == generic_height + expected_lines * 20, "wrapped summary height must reserve each 24-byte line")
assert(long_height > short_height, "long wrapped labels must increase card height monotonically")

-- Summary layout is below the existing port-label row and cannot move anchors.
local cell_with_summary = table.clone(cell)
cell_with_summary.summary = long
assert(Canvas.port(cell_with_summary, "input", false) == input_port_before, "input anchor moved when summary was present")
assert(Canvas.port(cell_with_summary, "output", true) == output_port_before, "output anchor moved when summary was present")
print("BASE_BLOCK_HEIGHT_OK")
