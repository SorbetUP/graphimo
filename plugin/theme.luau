-- The Graphimo page's visual system in one place: geometry, the library
-- category model and status colors. Pure data + pure helpers (no ui/ds
-- requires) so the headless harness can load it unchanged.
local Theme = {}

-- Layout geometry.
Theme.topbar_h = 52
Theme.library_w = 300
Theme.inspector_w = 320
Theme.canvas_inset = 10 -- floating pills offset inside the canvas well

-- Left-rail library categories. `icon` is a Logo.roles key.
Theme.categories = {
	{ id = "nodes", label = "Nodes", icon = "node_instance" },
	{ id = "controls", label = "Controls", icon = "control" },
	{ id = "metablocks", label = "MetaBlocks", icon = "metablock" },
	{ id = "presets", label = "Presets", icon = "preset" },
	{ id = "artifacts", label = "Artifacts", icon = "library" },
}

-- Right-rail tabs.
Theme.rail_tabs = {
	{ id = "inspect", label = "Inspect" },
	{ id = "logs", label = "Logs" },
}

-- Status label -> dot color token for canvas cards.
Theme.status_color = {
	success = "$success",
	warning = "$warning",
	danger = "$warning",
	info = "$accent",
	neutral = "$text.tertiary",
}

function Theme.category(id)
	for _, category in Theme.categories do
		if category.id == id then return category end
	end
	return Theme.categories[1]
end

-- Basename that works for / and \ paths without a fragile char class.
function Theme.basename(path)
	path = tostring(path or "")
	local name = string.match(path, "[^/\\]+$")
	return name ~= "" and name or path
end

return Theme
