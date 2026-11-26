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
        {"name": "value", "required": True, "annotation": "float", "has_default": False},
        {"name": "scale", "required": False, "annotation": "float", "has_default": True},
    ]
    assert created["nodes"][0]["outputs"] == [
        {"index": 0, "declared_type": "float", "expressions": ["value * scale"]}
    ]
    assert (tmp_path / "components/processing/normalize.py").is_file()
    assert library.list()["items"][0]["path"] == "processing/normalize.py"


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
