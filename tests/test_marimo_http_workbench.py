import importlib.util
from pathlib import Path

import pytest

from graphimo.application import GraphimoApplication
from graphimo.marimo_http import MarimoHttpPort, MarimoSessionPool
from graphimo.groups import GroupPort, create_group_file

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
        app = GraphimoApplication(
            port, tmp_path / "components", layout_path=tmp_path / ".graphimo.json"
        )
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
        before_disable = {
            cell["id"]: cell["source"] for cell in port.inspect(port.session_id)["cells"]
        }
        app.disable_instance(first["cell_id"])
        disabled_state = app.refresh_project()
        disabled_block = next(
            block for block in disabled_state.blocks if block.id == first["cell_id"]
        )
        stale_block = next(
            block for block in disabled_state.blocks if block.id == second["cell_id"]
        )
        assert disabled_block.state == "disabled"
        assert stale_block.state == "stale"
        assert any(
            edge.source_block_id == first["cell_id"]
            and edge.target_block_id == second["cell_id"]
            and edge.state == "stale"
            for edge in disabled_state.edges
        )
        app.enable_instance(first["cell_id"])
        after_enable = {
            cell["id"]: cell["source"] for cell in port.inspect(port.session_id)["cells"]
        }
        assert after_enable[first["cell_id"]] == before_disable[first["cell_id"]]
        assert after_enable[second["cell_id"]] == before_disable[second["cell_id"]]
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
    notebook.write_text(
        "import marimo\napp = marimo.App()\nif __name__ == '__main__': app.run()\n",
        encoding="utf-8",
    )
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


@pytest.mark.skipif(
    importlib.util.find_spec("marimo") is None or importlib.util.find_spec("websockets") is None,
    reason="live Workbench conformance requires marimo and websockets",
)
def test_live_control_block_updates_marimo_and_reactive_node(tmp_path):
    notebook = tmp_path / "live_control.py"
    notebook.write_text(
        "import marimo\napp = marimo.App()\n"
        "@app.cell\ndef define_node():\n"
        "    from graphimo import node\n"
        "    @node\n    def scale(x: float, factor: float) -> float:\n"
        "        return x * factor\n"
        "    return (scale,)\n"
        "if __name__ == '__main__': app.run()\n",
        encoding="utf-8",
    )
    port = MarimoHttpPort(notebook)
    try:
        app = GraphimoApplication(port, tmp_path / "components")
        app.open_project(notebook, port.session_id)
        slider = app.add_control(
            "slider",
            {
                "label": "factor",
                "start": 0.0,
                "stop": 2.0,
                "step": 0.1,
                "value": 0.5,
                "debounce": False,
            },
        )
        item = next(item for item in app.refresh_project().palette.items if item.title == "scale")
        node = app.add_palette_item(
            item.id, bindings={"x": "4.0", "factor": f"{slider['result_var']}.value"}
        )
        app.run_target(node["cell_id"])
        before = app.inspect_node_instance(node["cell_id"])
        assert before["outputs"][0]["preview"] == "2.0"
        control_state = port.inspect_control(port.session_id, slider["result_var"])
        assert control_state["value"] == 0.5
        assert control_state["object_id"]

        app.set_control_value(slider["cell_id"], 1.5)
        after = app.inspect_node_instance(node["cell_id"])
        refreshed = app.refresh_project()
        live_slider = next(block for block in refreshed.blocks if block.id == slider["cell_id"])
        assert after["outputs"][0]["preview"] == "6.0"
        assert live_slider.outputs[0].preview == "1.5"
        extra_controls = {}
        for control_type in (
            "range_slider",
            "number",
            "select",
            "checkbox",
            "switch",
            "text",
            "button",
        ):
            extra = app.add_control(control_type, {})
            extra_state = port.inspect_control(port.session_id, extra["result_var"])
            assert "object_id" in extra_state
            extra_controls[control_type] = extra
        values = {
            "range_slider": [0.2, 0.8],
            "number": 3.0,
            "select": "B",
            "checkbox": True,
            "switch": True,
            "text": "changed",
            "button": 1,
        }
        for control_type, next_value in values.items():
            control = extra_controls[control_type]
            app.set_control_value(control["cell_id"], next_value)
            updated = port.inspect_control(port.session_id, control["result_var"])
            assert updated["value"] == next_value
    finally:
        port.close()


@pytest.mark.skipif(
    importlib.util.find_spec("marimo") is None or importlib.util.find_spec("websockets") is None,
    reason="live Workbench conformance requires marimo and websockets",
)
def test_live_workbench_runs_group_inspects_named_outputs_and_navigates(tmp_path):
    notebook = tmp_path / "group_project.py"
    notebook.write_text(
        "import marimo\napp = marimo.App()\nif __name__ == '__main__': app.run()\n",
        encoding="utf-8",
    )
    components = tmp_path / "components"
    group_path = components / "pipeline.py"
    create_group_file(
        group_path,
        group_id="pipeline",
        name="Pipeline",
        inputs=[GroupPort("dataset", "dataset", "list")],
        outputs=[GroupPort("count", "size", "int")],
    )
    group_source = group_path.read_text(encoding="utf-8").replace(
        "# Add normal Graphimo/marimo cells below.\n\n",
        "@app.cell\ndef compute(dataset):\n    size = len(dataset)\n    return (size,)\n\n",
    )
    group_path.write_text(group_source, encoding="utf-8")
    port = MarimoSessionPool(notebook)
    try:
        app = GraphimoApplication(port, components, layout_path=tmp_path / ".graphimo.json")
        app.open_project(notebook, port.session_id)
        created = app.add_group_instance("pipeline.py", {"dataset": "[1, 2, 3]"})
        app.run_target(created["cell_id"])

        inspection = app.open_inspector(created["cell_id"])
        assert inspection["outputs"][0]["name"] == "count"
        assert inspection["outputs"][0]["preview"] == "3"

        entered = app.enter_group("pipeline.py")
        assert entered.active_scope_id == "pipeline"
        assert len(entered.blocks) == 1
        assert entered.blocks[0].title == "compute"
        assert app.leave_scope().active_scope_id == "root"
    finally:
        port.close()
