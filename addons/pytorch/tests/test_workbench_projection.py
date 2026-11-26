from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_PACKAGE_PATH = Path(__file__).parents[1] / "graphimo_pytorch"
_PACKAGE_NAME = "graphimo_pytorch_workbench_tests"
if _PACKAGE_NAME not in sys.modules:
    package = ModuleType(_PACKAGE_NAME)
    package.__path__ = [str(_PACKAGE_PATH)]
    package.__package__ = _PACKAGE_NAME
    package.__spec__ = importlib.util.spec_from_loader(_PACKAGE_NAME, loader=None, is_package=True)
    package.__spec__.submodule_search_locations = [str(_PACKAGE_PATH)]
    sys.modules[_PACKAGE_NAME] = package

schema = importlib.import_module(f"{_PACKAGE_NAME}.schema")
projection = importlib.import_module(f"{_PACKAGE_NAME}.workbench_projection")
registration = importlib.import_module(f"{_PACKAGE_NAME}.registration")
ElementRecord = schema.ElementRecord
Endpoint = schema.Endpoint
MethodGraph = schema.MethodGraph
MethodInput = schema.MethodInput
MethodOutput = schema.MethodOutput
PyTorchMetaBlockState = schema.PyTorchMetaBlockState


def mlp_state():
    elements = (
        ElementRecord(
            "tokens",
            "input",
            "tokens",
            {"name": "tokens", "shape": ["batch", 4], "dtype": "float32"},
            {},
            {},
        ),
        ElementRecord("dense", "linear", "dense", {"in_features": 4, "out_features": 8}, {}, {}),
        ElementRecord("gelu", "gelu", "gelu", {}, {}, {}),
    )
    method = MethodGraph(
        "forward",
        (MethodInput("tokens", ("batch", 4), "float32"),),
        (MethodOutput("output", Endpoint("gelu", "output")),),
        elements,
        (
            schema.EdgeRecord("e1", Endpoint("tokens", "output"), Endpoint("dense", "input")),
            schema.EdgeRecord("e2", Endpoint("dense", "output"), Endpoint("gelu", "input")),
        ),
        (),
    )
    return PyTorchMetaBlockState(1, "model-1", "Tiny", (), (), (method,), {})


def test_projection_exposes_palette_inspectors_shapes_methods_and_source():
    pytest.importorskip("torch")

    view = projection.project_model(mlp_state(), "root")

    assert view["dialect_id"] == "pytorch.model"
    assert view["active_method"] == "forward"
    assert {item["kind"] for item in view["palette"]} >= {"linear", "gelu", "split_heads"}
    dense = next(item for item in view["elements"] if item["id"] == "dense")
    assert dense["output_shape"]["shape"] == ["batch", "8"]
    assert {item["name"] for item in dense["parameters"]} >= {"in_features", "out_features"}
    assert view["edges"][0]["shape"] is not None
    assert "class Tiny(nn.Module)" in view["generated_source"]
    assert view["runtime_selector"]["available"] is False


def test_projection_keeps_invalid_graph_diagnostics_and_omits_source():
    state = mlp_state()
    method = state.entrypoints[0]
    broken = PyTorchMetaBlockState(
        state.schema_version,
        state.metablock_id,
        state.name,
        state.external_parameters,
        state.groups,
        (MethodGraph(method.name, method.inputs, method.outputs, method.elements, (), ()),),
        state.metadata,
    )

    view = projection.project_model(broken, "root")

    assert view["generated_source"] is None
    assert any(item["code"] == "pytorch_input_unconnected" for item in view["diagnostics"])


def test_dialect_apply_supports_element_parameter_edge_and_delete_intents():
    dialect = registration.PyTorchModelDialect(None)
    state = schema.state_to_dict(mlp_state())
    state = dialect.apply(
        state,
        {
            "type": "set_element_parameter",
            "element_id": "dense",
            "parameter": "out_features",
            "value": 12,
        },
        None,
    )
    assert (
        next(item for item in state["entrypoints"][0]["elements"] if item["id"] == "dense")[
            "params"
        ]["out_features"]
        == 12
    )

    state = dialect.apply(
        state,
        {"type": "add_element", "kind": "relu", "id": "relu_new", "name": "relu_new"},
        None,
    )
    state = dialect.apply(
        state,
        {
            "type": "connect",
            "edge_id": "e3",
            "source": {"element_id": "gelu", "port": "output"},
            "target": {"element_id": "relu_new", "port": "input"},
        },
        None,
    )
    state = dialect.apply(
        state,
        {
            "type": "connect",
            "edge_id": "e4_duplicate",
            "source": {"element_id": "gelu", "port": "output"},
            "target": {"element_id": "relu_new", "port": "input"},
        },
        None,
    )
    assert (
        len(
            [
                item
                for item in state["entrypoints"][0]["edges"]
                if item["target"]["element_id"] == "relu_new"
            ]
        )
        == 1
    )
    state = dialect.apply(state, {"type": "disconnect", "edge_id": "e3"}, None)
    state = dialect.apply(state, {"type": "delete_element", "element_id": "relu_new"}, None)

    assert all(item["id"] != "relu_new" for item in state["entrypoints"][0]["elements"])
    assert all(item["id"] != "e3" for item in state["entrypoints"][0]["edges"])


def test_invalid_element_parameter_is_rejected_without_state_change():
    dialect = registration.PyTorchModelDialect(None)
    state = schema.state_to_dict(mlp_state())

    with pytest.raises(ValueError, match=">= 1"):
        dialect.apply(
            state,
            {
                "type": "set_element_parameter",
                "element_id": "dense",
                "parameter": "out_features",
                "value": 0,
            },
            None,
        )

    assert (
        next(item for item in state["entrypoints"][0]["elements"] if item["id"] == "dense")[
            "params"
        ]["out_features"]
        == 8
    )


def test_projection_exposes_group_instance_repeat_and_callsite_shapes():
    pytest.importorskip("torch")
    group = schema.GroupDefinition(
        "projection",
        "Projection",
        (schema.GroupPort("x", Endpoint("dense", "input")),),
        (schema.GroupPort("y", Endpoint("dense", "output")),),
        (ElementRecord("dense", "linear", "dense", {"in_features": 4, "out_features": 4}, {}, {}),),
        (),
        (),
    )
    method = schema.MethodGraph(
        "forward",
        (MethodInput("tokens", ("batch", 4), "float32"),),
        (MethodOutput("output", Endpoint("projection_call", "y")),),
        (
            ElementRecord(
                "tokens",
                "input",
                "tokens",
                {"name": "tokens", "shape": ["batch", 4], "dtype": "float32"},
                {},
                {},
            ),
        ),
        (
            schema.EdgeRecord(
                "call", Endpoint("tokens", "output"), Endpoint("projection_call", "x")
            ),
        ),
        (schema.GroupInstance("projection_call", "projection", "projection_call", 2, {}),),
    )
    state = PyTorchMetaBlockState(1, "model-1", "Tiny", (), (group,), (method,), {})

    outer = projection.project_model(state, "root")
    group_view = projection.project_model(state, "group:projection")
    card = next(item for item in outer["elements"] if item["id"] == "projection_call")

    assert card["repeat"] == 2
    assert card["inputs"][0]["name"] == "x"
    dense = next(item for item in group_view["elements"] if item["id"] == "dense")
    assert dense["output_shape"]["shape"] == ["batch", "4"]
    assert group_view["generated_source"], [
        (item["code"], item["message"]) for item in group_view["diagnostics"]
    ]


def test_pytorch_projection_and_edits_flow_through_graphimo_application(tmp_path: Path):
    pytest.importorskip("torch")
    from graphimo.application import GraphimoApplication
    from graphimo.metablocks import GraphimoHostAPI, MetaBlockAddonManager

    registration_value = registration.registration

    class EntryPoint:
        name = "pytorch"

        def load(self):
            return registration_value

    project_path = tmp_path / "notebook.py"
    project_path.write_text("# local project\n", encoding="utf-8")
    host = GraphimoHostAPI("project", project_path)
    manager = MetaBlockAddonManager(host, entry_points=lambda: [EntryPoint()])
    application = GraphimoApplication(host, manager)

    created = application.execute(
        "metablock.create",
        {"dialect_id": "pytorch.model", "metablock_id": "torch_model", "request": {"name": "Tiny"}},
    )
    projected = application.execute(
        "metablock.project", {"metablock_id": "torch_model", "scope": "root"}
    )
    added = application.execute(
        "metablock.apply",
        {
            "metablock_id": "torch_model",
            "intent": {"type": "add_element", "scope": "forward", "kind": "relu", "id": "relu_1"},
        },
    )

    assert created["projection"]["generated_source"]
    assert projected["projection"]["dialect_id"] == "pytorch.model"
    assert any(item["id"] == "relu_1" for item in added["projection"]["elements"])
    assert any(
        item["code"] == "pytorch_input_unconnected" for item in added["projection"]["diagnostics"]
    )
