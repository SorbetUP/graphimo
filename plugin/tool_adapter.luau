-- Zeron tool results can finish through Promise:after callbacks. Await those
-- callbacks here and deliver only the resolved JSON value to the host.
local Adapter = {}

function Adapter.dispatch(run, args, call, answer)
	local ok, result = pcall(run, args, call)
	if not ok then
		answer(false, result)
		return
	end
	local valid, after = pcall(function() return result and result.after end)
	if valid and type(after) == "function" then
		local registered, err = pcall(function()
			after(result, function(value) answer(true, value) end, function(reason) answer(false, reason) end)
		end)
		if not registered then answer(false, err) end
		return
	end
	answer(true, result)
end

return Adapter
