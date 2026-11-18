-- Minimal Zeron shim for exercising the production Graphimo layout module.
local mode, notebook_path, decoded_json = ...
local layout = require("../plugin/layout")

ui = {
	json = {
		encode = function(value)
			local function encode(item)
				if type(item) == "string" then
					return '"' .. string.gsub(string.gsub(item, "\\", "\\\\"), '"', '\\"') .. '"'
				end
				if type(item) == "number" or type(item) == "boolean" then return tostring(item) end
				if type(item) ~= "table" then error("unsupported JSON value " .. type(item)) end
				local entries = {}
				for key, nested in item do
					table.insert(entries, encode(tostring(key)) .. ":" .. encode(nested))
				end
				return "{" .. table.concat(entries, ",") .. "}"
			end
			return encode(value)
		end,
		decode = function(value)
			assert(value == decoded_json, "layout loader did not decode the Python-produced sidecar value")
			return { ["stable-cell-id"] = { x = 120, y = 180, collapsed = false } }
		end,
	},
}
getfenv(layout.save).ui = ui
getfenv(layout.load).ui = ui

local Promise = {}
Promise.__index = Promise
function Promise:after(success, failure)
	if self.error then if failure then failure(self.error) end
	elseif success then success(self.value) end
	return nil
end

local zeron = {}
function zeron.run(_program, args)
	assert(args[1] == "-c", "layout operation must use python -c")
	print("GRAPHIMO_SCRIPT_BEGIN\n" .. args[2] .. "\nGRAPHIMO_SCRIPT_END")
	print("GRAPHIMO_NOTEBOOK:" .. args[3])
	if mode == "save" then
		print("GRAPHIMO_CELLS:" .. args[4])
		return setmetatable({ value = { status = 0, stdout = "" } }, Promise)
	end
	return setmetatable({ value = { status = 0, stdout = decoded_json } }, Promise)
end

local cells = { ["stable-cell-id"] = { x = 120, y = 180, collapsed = false } }
if mode == "save" then
	layout.save(zeron, notebook_path, cells, function(ok, err)
		assert(ok, tostring(err))
		print("GRAPHIMO_SAVE_DONE")
	end)
elseif mode == "load" then
	layout.load(zeron, notebook_path, function(value, err)
		assert(not err, tostring(err))
		local cell = value["stable-cell-id"]
		assert(cell and cell.x == 120 and cell.y == 180 and cell.collapsed == false, "layout callback did not restore saved cell metadata")
		print("GRAPHIMO_LOAD_DONE")
	end)
else
	error("unknown mode: " .. tostring(mode))
end
