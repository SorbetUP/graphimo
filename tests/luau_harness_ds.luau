-- ds component factory for the headless init harness. Each generated
-- tests/.harness/ds_<name>.luau shim returns factory("<name>"): a callable
-- table so both `Text { ... }` and `require("@ds/dialog").Dialog { ... }`
-- work.
return function(name)
	return setmetatable({}, {
		__call = function(_self, props) return { __kind = name, props = props or {} } end,
		__index = function(_t, key)
			return function(props) return { __kind = name .. "." .. tostring(key), props = props or {} } end
		end,
	})
end
