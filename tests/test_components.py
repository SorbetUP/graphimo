from __future__ import annotations

from pathlib import Path

import pytest

from graphimo.application import GraphimoApplication
from graphimo.components import ComponentError, ComponentLibrary
from graphimo.metablocks import GraphimoHostAPI

NODE_SOURCE = '''"""A test component."""
from graphimo import node

@node
def normalize(value: float, scale: float = 2.0) -> float:
    """Scale a value."""
    return value * scale
'''


def test_component_write_validates_and_atomically_creates_node(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)

    created = library.write("processing/normalize.py", NODE_SOURCE)

    assert created["valid"] is True
    assert created["created"] is True
    assert created["nodes"][0]["name"] == "normalize"
    assert created["nodes"][0]["inputs"] == [
        {
            "name": "value",
            "required": True,
            "annotation": "float",
            "has_default": False,
            "positional_only": False,
        },
        {
            "name": "scale",
            "required": False,
            "annotation": "float",
            "has_default": True,
            "positional_only": False,
        },
    ]
    assert created["nodes"][0]["outputs"] == [
        {"index": 0, "declared_type": "float", "expressions": ["value * scale"]}
    ]
    assert (tmp_path / "components/processing/normalize.py").is_file()
    listed = library.list()["items"][0]
    assert listed["path"] == "processing/normalize.py"
    assert listed["callables"][0]["name"] == "normalize"
    assert (
        listed["callables"][0]["signature"]
        == "normalize(value: float, scale: float = 2.0) -> float"
    )
    assert listed["callables"][0]["parameters"][1]["default"] == 2.0
    assert listed["callables"][0]["outputs"][0]["expression"] == "value * scale"


def test_component_instance_source_uses_validated_ports_and_readable_python(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write("processing/normalize.py", NODE_SOURCE)

    instance = library.instance_source(
        "processing/normalize.py",
        "normalize",
        "normalized",
        {"value": {"variable": "data"}, "scale": {"literal": 2.5}},
    )

    assert instance["source_sha256"] in instance["library_source"]
    assert "components.processing.normalize" in instance["library_source"]
    assert (
        instance["runtime_symbol"] + " = _graphimo_module.normalize" in instance["library_source"]
    )
    assert instance["runtime_symbol"] + "(" in instance["source"]
    assert "# Graphimo component: processing/normalize.py" in instance["source"]
    assert "value=data," in instance["source"]
    assert "scale=__import__('json').loads('2.5')," in instance["source"]
    assert instance["canonical_instance"] is True
    assert instance["component_id"] == "processing/normalize.py"
    assert instance["callable"] == "normalize"
    assert instance["result_var"].startswith("g_normalize_")
    compile(instance["source"], "<component-instance>", "exec")


def test_component_instance_source_allows_unbound_required_inputs_as_formal_ports(
    tmp_path: Path,
) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write("normalize.py", NODE_SOURCE)

    instance = library.instance_source("normalize.py", "normalize", "normalized", {})

    assert instance["canonical_instance"] is True
    assert "value=" not in instance["source"]
    assert "scale=" not in instance["source"]
    compile(instance["source"], "<component-instance>", "exec")


def test_same_callable_name_in_two_components_gets_distinct_runtime_symbols(
    tmp_path: Path,
) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write("images.py", NODE_SOURCE)
    library.write("tables.py", NODE_SOURCE)

    images = library.instance_source("images.py", "normalize", "a", {})
    tables = library.instance_source("tables.py", "normalize", "b", {})

    assert images["runtime_symbol"] != tables["runtime_symbol"]
    assert images["library_marker"] != tables["library_marker"]
    assert images["library_marker"] == "# Graphimo node library: images.py:normalize"
    assert tables["library_marker"] == "# Graphimo node library: tables.py:normalize"
    assert images["runtime_symbol"] + " = " in images["library_source"]
    assert tables["runtime_symbol"] + " = " in tables["library_source"]
    compile(images["library_source"], "<library>", "exec")
    compile(tables["library_source"], "<library>", "exec")

    updates = library.library_updates()
    assert set(updates) == {
        "# Graphimo node library: images.py:normalize",
        "# Graphimo node library: tables.py:normalize",
    }
    for marker, source in updates.items():
        assert source.startswith(marker + "\n")
        assert "# Graphimo node source sha256: " in source
        compile(source, "<library>", "exec")


@pytest.mark.parametrize(
    "bindings, message",
    [
        ({"value": {"variable": "data"}, "typo": {"variable": "scale"}}, "unknown component input"),
        ({"value": {"variable": "bad-name"}}, "Python identifier"),
    ],
)
def test_component_instance_source_rejects_invalid_bindings(
    tmp_path: Path, bindings: dict[str, object], message: str
) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write("normalize.py", NODE_SOURCE)

    with pytest.raises(ComponentError, match=message):
        library.instance_source("normalize.py", "normalize", "result", bindings)


def test_component_instance_source_respects_positional_only_parameters(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write(
        "positional.py",
        "from graphimo import node\n\n"
        "@node\n"
        "def combine(left, /, right=2):\n"
        "    return left + right\n",
    )

    result = library.instance_source(
        "positional.py",
        "combine",
        "combined",
        {"left": {"variable": "data"}, "right": {"literal": 3}},
    )

    assert "combine(data, right=__import__('json').loads('3'))" in result["source"]


def test_component_instance_source_rejects_omitted_preceding_positional_default(
    tmp_path: Path,
) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write(
        "positional.py",
        "from graphimo import node\n\n"
        "@node\n"
        "def combine(scale=2, value=3, /):\n"
        "    return scale * value\n",
    )

    with pytest.raises(ComponentError, match="bind positional-only inputs"):
        library.instance_source("positional.py", "combine", "combined", {"value": {"literal": 3}})


def test_invalid_component_update_preserves_previous_valid_file(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write("normalize.py", NODE_SOURCE)
    target = tmp_path / "components/normalize.py"

    result = library.write("normalize.py", "def broken(:\n    pass\n")

    assert result["valid"] is False
    assert result["diagnostics"][0]["code"] == "component_validation_failed"
    assert target.read_text(encoding="utf-8").startswith('"""A test component."""')


def test_component_requires_public_graphimo_node(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)

    result = library.write("helper.py", "def helper(value):\n    return value\n")

    assert result["valid"] is False
    assert result["diagnostics"][0]["code"] == "component_node_missing"
    assert not (tmp_path / "components/helper.py").exists()


def test_component_packages_can_export_nodes_from_init_file(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)

    created = library.write("math_nodes/__init__.py", NODE_SOURCE)

    assert created["valid"] is True
    assert created["nodes"][0]["name"] == "normalize"
    assert library.list()["items"][0]["path"] == "math_nodes/__init__.py"
    instance = library.instance_source(
        "math_nodes/__init__.py",
        "normalize",
        "normalized",
        {"value": {"variable": "data"}},
    )
    assert "import_module('components.math_nodes')" in instance["library_source"]


def test_component_paths_cannot_escape_or_follow_symlinks(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    with pytest.raises(ComponentError, match="traversal"):
        library.write("../outside.py", NODE_SOURCE)

    outside = tmp_path / "outside"
    outside.mkdir()
    library.root.mkdir()
    (library.root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ComponentError, match="symbolic link"):
        library.write("linked/escape.py", NODE_SOURCE)


def test_component_delete_moves_source_to_recoverable_project_trash(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write("normalize.py", NODE_SOURCE)

    deleted = library.delete("normalize.py")

    assert deleted["deleted"] is True
    assert not (tmp_path / "components/normalize.py").exists()
    assert (
        Path(deleted["trash_path"])
        .read_text(encoding="utf-8")
        .startswith('"""A test component."""')
    )


def test_graphimo_application_routes_component_operations(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    application = GraphimoApplication(GraphimoHostAPI("project", project))

    written = application.execute(
        "component.write", {"path": "normalize.py", "source": NODE_SOURCE}
    )
    validated = application.execute("component.validate", {"path": "normalize.py"})
    catalog = application.execute("component.list", {})

    assert written["valid"] is True
    assert validated["valid"] is True
    assert catalog["items"][0]["nodes"] == ["normalize"]
    assert application.execute("component.delete", {"path": "normalize.py"})["deleted"] is True


def test_application_routes_live_notebook_intents_to_host(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    calls: list[tuple[str, dict[str, object]]] = []
    host = GraphimoHostAPI(
        "project",
        project,
        invoke_notebook=lambda action, args: calls.append((action, args)) or {"ok": True},
    )
    application = GraphimoApplication(host)

    assert application.execute("notebook.run", {"cell_id": "cell-1"}) == {"ok": True}
    assert calls == [("run", {"cell_id": "cell-1"})]


def test_application_add_component_uses_the_attached_marimo_host(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write("normalize.py", NODE_SOURCE)
    inserted: list[dict[str, object]] = []
    host = GraphimoHostAPI(
        "project",
        project,
        invoke_notebook=lambda action, args: inserted.append({"action": action, **args})
        or {"cell_id": "cell-new"},
    )
    application = GraphimoApplication(host)

    result = application.execute(
        "component.add",
        {
            "path": "normalize.py",
            "callable": "normalize",
            "output_name": "normalized",
            "bindings": {"value": {"variable": "data"}},
        },
    )

    assert result["cell_id"] == "cell-new"
    assert result["runtime_symbol"] + "(" in result["source"]
    assert "value=data," in result["source"]
    assert result["canonical_instance"] is True
    assert [item["action"] for item in inserted] == [
        "inspect",
        "add_cell",
        "run",
        "add_cell",
    ]
    assert inserted[1]["source"] == result["library_source"]
    assert inserted[3]["source"] == result["source"]


def test_notebook_intent_requires_attached_kernel(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    application = GraphimoApplication(GraphimoHostAPI("project", project))

    with pytest.raises(ValueError, match="no live marimo kernel"):
        application.execute("notebook.inspect", {})


def test_component_write_rejects_module_level_calls_before_import(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    sentinel = tmp_path / "must-survive.txt"
    sentinel.write_text("safe", encoding="utf-8")
    source = f"""from pathlib import Path
from graphimo import node

Path({str(sentinel)!r}).unlink()

@node
def harmless(value):
    return value
"""

    result = ComponentLibrary(project).write("unsafe.py", source)

    assert result["valid"] is False
    assert result["diagnostics"][0]["code"] == "component_top_level_execution"
    assert "module-level Expr" in result["diagnostics"][0]["message"]
    assert sentinel.read_text(encoding="utf-8") == "safe"
    assert not (tmp_path / "components/unsafe.py").exists()


def test_component_write_never_imports_component_or_its_dependencies(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    sentinel = tmp_path / "import-was-executed.txt"
    (tmp_path / "tripwire.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
        "def identity(value):\n    return value\n",
        encoding="utf-8",
    )
    source = """import tripwire
from graphimo import node

@node
def identity(value: int) -> int:
    return tripwire.identity(value)
"""

    result = ComponentLibrary(project).write("safe.py", source)

    assert result["valid"] is True
    assert result["nodes"] == [
        {
            "name": "identity",
            "inputs": [
                {
                    "name": "value",
                    "required": True,
                    "annotation": "int",
                    "has_default": False,
                    "positional_only": False,
                }
            ],
            "outputs": [
                {
                    "index": 0,
                    "declared_type": "int",
                    "expressions": ["tripwire.identity(value)"],
                }
            ],
            "diagnostic": None,
        }
    ]
    assert not sentinel.exists()


def test_component_list_discovers_aliased_node_without_importing_module(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    components = tmp_path / "components"
    components.mkdir()
    (components / "aliased.py").write_text(
        "from graphimo import node as _node\n"
        "@_node\n"
        "def normalize(value: float, scale: float = 2.0) -> float:\n"
        "    return value * scale\n",
        encoding="utf-8",
    )

    listed = ComponentLibrary(project).list()["items"]

    assert [item["id"] for item in listed] == ["aliased.py"]
    assert listed[0]["callables"][0]["name"] == "normalize"


def test_component_write_rejects_async_nodes_and_generators(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)

    async_result = library.write(
        "async_node.py",
        "from graphimo import node\n\n@node\nasync def fetch(value: int) -> int:\n    return value\n",
    )
    assert async_result["valid"] is False
    assert "async" in async_result["diagnostics"][0]["message"]

    generator_result = library.write(
        "gen_node.py",
        "from graphimo import node\n\n@node\ndef batches(value: int):\n    for i in range(value):\n        yield i\n",
    )
    assert generator_result["valid"] is False
    assert "yield" in generator_result["diagnostics"][0]["message"]


def test_component_list_marks_invalid_state_and_skips_it_in_refresh(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write("good.py", NODE_SOURCE)
    # A hand-written file that would never pass component.write validation.
    library.root.mkdir(parents=True, exist_ok=True)
    (library.root / "handmade.py").write_text(
        "from graphimo import node\n\nprint('side effect')\n\n@node\ndef broken(value: int) -> int:\n    return value\n",
        encoding="utf-8",
    )

    items = {item["path"]: item for item in library.list()["items"]}
    assert items["good.py"]["state"] == "valid"
    assert items["handmade.py"]["state"] == "invalid"
    assert items["handmade.py"]["diagnostics"][0]["code"] == "component_top_level_execution"

    updates = library.library_updates()
    assert "# Graphimo node library: good.py:normalize" in updates
    assert not any("handmade.py" in marker for marker in updates)


def test_component_list_uses_package_digest_for_init_files(tmp_path: Path) -> None:
    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write("pkg/__init__.py", NODE_SOURCE)
    (library.root / "pkg" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")

    item = library.list()["items"][0]
    assert item["path"] == "pkg/__init__.py"
    # The digest covers every file of the package, not just __init__.py.
    from graphimo.components import _component_digest

    assert item["sha256"] == _component_digest(
        library.root / "pkg" / "__init__.py", Path("pkg/__init__.py")
    )
    assert item["source_sha256"] == item["sha256"]


def test_literal_limits_are_consistent_at_the_boundary(tmp_path: Path) -> None:
    from graphimo.instance import MAX_LITERAL_JSON_BYTES

    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write("normalize.py", NODE_SOURCE)

    ok_size = MAX_LITERAL_JSON_BYTES - 3  # room for the two JSON quotes
    big = "x" * ok_size
    result = library.instance_source(
        "normalize.py", "normalize", "out", {"value": {"literal": big}}
    )
    assert result["canonical_instance"] is True

    too_big = "x" * (MAX_LITERAL_JSON_BYTES + 1)
    with pytest.raises(ComponentError, match="literal exceeds"):
        library.instance_source("normalize.py", "normalize", "out", {"value": {"literal": too_big}})


def test_runtime_symbol_uses_128_bits_and_detects_collisions(tmp_path: Path) -> None:
    from graphimo import runtime_symbol

    symbol = runtime_symbol("images/normalize.py", "normalize")
    digest_part = symbol.removeprefix("__graphimo_c_").removesuffix("_normalize")
    assert len(digest_part) == 32
    assert len(symbol) > 40

    project = tmp_path / "notebook.py"
    project.write_text("# marimo project\n", encoding="utf-8")
    library = ComponentLibrary(project)
    library.write("a.py", NODE_SOURCE)
    library.write("b.py", NODE_SOURCE)
    updates = library.library_updates()
    symbols = [
        line
        for source in updates.values()
        for line in source.splitlines()
        if line.startswith("__graphimo_c_")
    ]
    assert len(symbols) == 2
    assert symbols[0] != symbols[1]
