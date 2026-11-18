-- Read-only Base Block card summary contract.
local BaseBlock = require("../plugin/base_block")

local projected = {
	cell_id = "cell-1",
	inputs = {
		{ name = "prix", annotation = "float" },
		{ name = "quantite", annotation = "int" },
		{ name = "context" },
	},
	outputs = {
		{ index = 1, name = "output_1", expression = "prix + 0.2", annotation = "float" },
		{ index = 2, name = "flower", expression = "flower", annotation = "int" },
		{ index = 3, name = "result" },
	},
	output_mode = "tuple",
}

local before_inputs = table.clone(projected.inputs)
local before_outputs = table.clone(projected.outputs)
local summary = BaseBlock.summary(projected)
assert(type(summary) == "table", "summary must return the structured label table")
assert(type(summary.inputs) == "table" and type(summary.outputs) == "table", "summary must expose input/output label arrays")
assert(#summary.inputs == 3 and #summary.outputs == 3, "summary must preserve all descriptors")
assert(summary.inputs[1] == "float (prix)", "annotated input label format mismatch")
assert(summary.inputs[2] == "int (quantite)", "input annotation order mismatch")
assert(summary.inputs[3] == "context", "input without annotation should display its name")
assert(summary.outputs[1] == "float (prix + 0.2)", "annotated expression output label mismatch")
assert(summary.outputs[2] == "int (flower)", "annotated name output label mismatch")
assert(summary.outputs[3] == "result", "output without annotation/expression should display its name")
assert(BaseBlock.summary(nil) == nil, "nil projection must preserve generic-card fallback")
assert(#projected.inputs == #before_inputs and projected.inputs[1] == before_inputs[1], "summary mutated input metadata")
assert(#projected.outputs == #before_outputs and projected.outputs[1] == before_outputs[1], "summary mutated output metadata")
print("BASE_BLOCK_SUMMARY_OK")
