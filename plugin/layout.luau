-- Layout persistence is delegated to a local Python one-liner so the state
-- lives beside the marimo notebook, never in the executable notebook itself.
local Layout = {}

local function run(zeron, args, timeout, done)
	local ok, request = pcall(function()
		return zeron.run("python3", args, { timeout = timeout })
	end)
	if not ok then done(nil, tostring(request)); return end
	if request == nil then
		done(nil, "Zeron did not start python3. Check Graphimo's commands permission in Settings → Plugins.")
		return
	end
	request:after(function(result) done(result, nil) end, function(err) done(nil, tostring(err)) end)
end

function Layout.load(zeron, notebook_path, done)
	local code = table.concat({
		"import json, pathlib, sys",
		"notebook = pathlib.Path(sys.argv[1]).expanduser()",
		"if not notebook.is_absolute() or notebook.suffix != '.py' or not notebook.is_file(): print('{}'); raise SystemExit(0)",
		"notebook = notebook.resolve(); path = notebook.parent / '.graphimo.json'",
		"try:",
		" data = json.loads(path.read_text(encoding='utf-8'))",
		"except (OSError, ValueError): data = {}",
		"items = data.get('notebooks', {}).get(str(notebook), {}).get('cells', {})",
		"print(json.dumps(items if isinstance(items, dict) else {}))",
	}, "\n")
	run(zeron, { "-c", code, notebook_path }, 10, function(result, err)
		if err then done({}, err); return end
		local ok, value = pcall(ui.json.decode, result.stdout or "{}")
		done(if ok and type(value) == "table" then value else {}, nil)
	end)
end

function Layout.save(zeron, notebook_path, cells, done)
	local allowed = { x = true, y = true, collapsed = true, width = true, height = true, group = true }
	local clean = {}
	for cell_id, state in cells do
		local entry = {}
		for key, value in state do if allowed[key] then entry[key] = value end end
		clean[cell_id] = entry
	end
	local code = table.concat({
		"import json, os, pathlib, sys, tempfile",
		"notebook = pathlib.Path(sys.argv[1]).expanduser()",
		"if not notebook.is_absolute() or notebook.suffix != '.py' or not notebook.is_file(): raise SystemExit('Local notebook path unavailable; layout was not written')",
		"notebook = notebook.resolve(); path = notebook.parent / '.graphimo.json'; cells = json.loads(sys.argv[2])",
		"try:",
		" data = json.loads(path.read_text(encoding='utf-8'))",
		" if data.get('version') != 1 or not isinstance(data.get('notebooks'), dict): data = {'version': 1, 'notebooks': {}}",
		"except (OSError, ValueError): data = {'version': 1, 'notebooks': {}}",
		"data['notebooks'][notebook] = {'cells': cells}",
		"path.parent.mkdir(parents=True, exist_ok=True)",
		"fd, temp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent, text=True)",
		"with os.fdopen(fd, 'w', encoding='utf-8') as stream: json.dump(data, stream, indent=2, sort_keys=True); stream.write('\\n')",
		"os.replace(temp, path)",
	}, "\n")
	run(zeron, { "-c", code, notebook_path, ui.json.encode(clean) }, 10, function(_, err)
		if done then done(err == nil, err) end
	end)
end

return Layout
