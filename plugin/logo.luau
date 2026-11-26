-- The Graphimo mark, as SVG code for ui.svg { code = Logo.svg, tint = "currentColor" }.
-- Keep in sync with graphimo.svg (the manifest icon, drawn on the plugin's tile).
local Logo = {}

Logo.svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2.75" y="2.75" width="7" height="7" rx="2"/><rect x="14.25" y="8.5" width="7" height="7" rx="2"/><rect x="2.75" y="14.25" width="7" height="7" rx="2"/><path d="M9.75 6.25h4.5a2 2 0 0 1 2 2v0M9.75 17.75h4.5a2 2 0 0 0 2-2v0"/></svg>'

-- Small per-role glyphs for block cards, same 24-unit grid and stroke.
Logo.roles = {
	node_instance = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="7" y="7" width="10" height="10" rx="2.5"/><path d="M2.5 12h4.5M17 12h4.5M12 2.5V7M12 17v4.5"/></svg>',
	library = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5z"/><path d="M4 20.5V5.5M20 18v3H6.5"/></svg>',
	control = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 8h9M17 8h3M4 16h3M11 16h9"/><circle cx="15" cy="8" r="2"/><circle cx="9" cy="16" r="2"/></svg>',
	group = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-dasharray="3.5 3" stroke-linecap="round"><rect x="3.5" y="3.5" width="17" height="17" rx="3.5"/></svg>',
	metablock = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 2.75 20.25 7v10L12 21.25 3.75 17V7z"/><path d="M3.75 7 12 11.25 20.25 7M12 11.25v10"/></svg>',
	ordinary_python = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5z"/><path d="M13.5 3v5.5H19M9 13l-2 2 2 2M15 13l2 2-2 2"/></svg>',
}

return Logo
