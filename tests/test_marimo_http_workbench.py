import importlib.util
from pathlib import Path

import pytest

from graphimo.application import GraphimoApplication
from graphimo.marimo_http import MarimoHttpPort

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    importlib.util.find_spec("marimo") is None or importlib.util.find_spec("websockets") is None,
    reason="live Workbench conformance requires marimo and websockets",
)
def test_live_workbench_add_connect_run_and_inspect(tmp_path):
    notebook = tmp_path / "live_workbench.py"
    notebook.write_text(
        "import marimo\napp = marimo.App()\n"
        "@app.cell\ndef source():\n    x = 2.0\n    return (x,)\n"
        "@app.cell\ndef define_scale():\n"
        "    from graphimo import node\n"
        "    @node\n    def scale(x: float, factor: float = 1.0) -> float:\n"
        "        y = x * factor\n        return y\n"
        "    return (scale,)\n"
        "if __name__ == '__main__': app.run()\n",
        encoding="utf-8",
    )
    port = MarimoHttpPort(notebook)
    try:
        app = GraphimoApplication(port, tmp_path / "components", layout_path=tmp_path / ".graphimo.json")
        state = app.open_project(notebook, port.session_id)
        palette_node = next(item for item in state.palette.items if item.title == "scale")
        first = app.add_palette_item(palette_node.id, bindings={"x": "x", "factor": "2.0"})
        second = app.add_palette_item(palette_node.id, bindings={"x": "x", "factor": "3.0"})
        app.run_target(first["cell_id"])
        app.run_target(second["cell_id"])
        app.connect(first["cell_id"], second["cell_id"], input_name="x")
        app.run_target(second["cell_id"])
        first_result = app.inspect_node_instance(first["cell_id"])
        second_result = app.inspect_node_instance(second["cell_id"])
        assert first_result["outputs"][0]["preview"] == "4.0"
        assert second_result["outputs"][0]["preview"] == "12.0"
        refreshed = app.refresh_project()
        assert {block.id for block in refreshed.blocks} >= {first["cell_id"], second["cell_id"]}
    finally:
        port.close()


@pytest.mark.skipif(
    importlib.util.find_spec("marimo") is None or importlib.util.find_spec("websockets") is None,
    reason="live Workbench conformance requires marimo and websockets",
)
def test_live_workbench_imports_component_definition_once(tmp_path):
    notebook = tmp_path / "component_project.py"
    notebook.write_text("import marimo\napp = marimo.App()\nif __name__ == '__main__': app.run()\n", encoding="utf-8")
    components = tmp_path / "components" / "ops"
    components.mkdir(parents=True)
    (components / "scale.py").write_text(
        "from graphimo import node\n"
        "@node\n"
        "def scale(x: float, factor: float = 1.0) -> float:\n"
        "    result = x * factor\n"
        "    return result\n",
        encoding="utf-8",
    )
    port = MarimoHttpPort(notebook)
    try:
        app = GraphimoApplication(port, tmp_path / "components")
        state = app.open_project(notebook, port.session_id)
        item = next(item for item in state.palette.items if item.title == "scale")
        first = app.add_palette_item(item.id, bindings={"x": "4.0", "factor": "2.0"})
        second = app.add_palette_item(item.id, bindings={"x": "5.0", "factor": "3.0"})
        cells = port.inspect(port.session_id)["cells"]
        import_cells = [cell for cell in cells if "components.ops.scale" in cell["source"]]
        assert len(import_cells) == 1
        assert len(cells) == 4
        assert first["result_var"] != second["result_var"]
    finally:
        port.close()
