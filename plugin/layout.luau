-- Layout persistence is delegated to a local Python one-liner so the state
-- lives beside the marimo notebook, never in the executable notebook itself.
local Layout = {}

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
	zeron.run("python3", { "-c", code, notebook_path }, { timeout = 10 }):after(function(result)
		local ok, value = pcall(ui.json.decode, result.stdout or "{}")
		done(if ok and type(value) == "table" then value else {})
	end, function()
		done({})
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
	zeron.run("python3", { "-c", code, notebook_path, ui.json.encode(clean) }, { timeout = 10 }):after(function()
		if done then done(true) end
	end, function(err)
		if done then done(false, tostring(err)) end
	end)
end

return Layout
