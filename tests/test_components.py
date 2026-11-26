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
    assert "normalize(" in instance["source"]
    assert "value=data," in instance["source"]
    assert "scale=__import__('json').loads('2.5')," in instance["source"]
    assert instance["canonical_instance"] is True
    assert instance["result_var"].startswith("g_normalize_")
    compile(instance["source"], "<component-instance>", "exec")


@pytest.mark.parametrize(
    "bindings, message",
    [
        ({}, "missing required"),
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
    assert "normalize(" in result["source"]
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
