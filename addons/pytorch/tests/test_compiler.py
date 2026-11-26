from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_PACKAGE_PATH = Path(__file__).parents[1] / "graphimo_pytorch"
_PACKAGE_NAME = "graphimo_pytorch_compiler_tests"
if _PACKAGE_NAME not in sys.modules:
    _package = ModuleType(_PACKAGE_NAME)
    _package.__path__ = [str(_PACKAGE_PATH)]
    _package.__package__ = _PACKAGE_NAME
    _package.__spec__ = importlib.util.spec_from_loader(_PACKAGE_NAME, loader=None, is_package=True)
    _package.__spec__.submodule_search_locations = [str(_PACKAGE_PATH)]
    sys.modules[_PACKAGE_NAME] = _package

_schema = importlib.import_module(f"{_PACKAGE_NAME}.schema")
_compiler = importlib.import_module(f"{_PACKAGE_NAME}.compiler")
EdgeRecord = _schema.EdgeRecord
ElementRecord = _schema.ElementRecord
Endpoint = _schema.Endpoint
ExternalParameter = _schema.ExternalParameter
GroupDefinition = _schema.GroupDefinition
GroupInstance = _schema.GroupInstance
GroupPort = _schema.GroupPort
MethodGraph = _schema.MethodGraph
MethodInput = _schema.MethodInput
MethodOutput = _schema.MethodOutput
ParameterBinding = _schema.ParameterBinding
PyTorchMetaBlockState = _schema.PyTorchMetaBlockState
compile_model = _compiler.compile_model
PyTorchCompilationError = _compiler.PyTorchCompilationError


def element(identity, kind, name, params=None, bindings=None):
    return ElementRecord(identity, kind, name, params or {}, bindings or {}, {})


def edge(identity, source, source_port, target, target_port):
    return EdgeRecord(identity, Endpoint(source, source_port), Endpoint(target, target_port))


def mlp_state(*, grouped=False):
    elements = (
        element(
            "x", "input", "tokens", {"name": "tokens", "shape": ["batch", 4], "dtype": "float32"}
        ),
        element("hidden", "linear", "hidden", {"in_features": 4, "out_features": 8}),
        element("activation", "gelu", "activation"),
        element("head", "linear", "head", {"in_features": 8, "out_features": 1}),
    )
    method = MethodGraph(
        "forward",
        (MethodInput("tokens", ("batch", 4), "float32"),),
        (MethodOutput("output", Endpoint("head", "output")),),
        elements,
        (
            edge("e1", "x", "output", "hidden", "input"),
            edge("e2", "hidden", "output", "activation", "input"),
            edge("e3", "activation", "output", "head", "input"),
        ),
        (),
    )
    return PyTorchMetaBlockState(1, "model-1", "Tiny Model", (), (), (method,), {})


def test_compiler_generates_readable_executable_mlp_and_runtime_sites():
    pytest.importorskip("torch")
    import torch

    result = compile_model(mlp_state())
    model = result.factory()
    output = model(torch.ones(3, 4))

    assert tuple(output["output"].shape) == (3, 1)
    assert "# graphimo-metablock-id: model-1" in result.generated_source
    assert "class TinyModel(nn.Module)" in result.generated_source
    assert "nn.Linear(4, 8" in result.generated_source
    assert "torchlens" not in result.generated_source
    assert {site.logical_element_id for site in result.bindings.sites} == {
        "x",
        "hidden",
        "activation",
        "head",
    }
    assert result.shape_result.valid


def test_compiler_resolves_external_structural_parameters_and_changes_key():
    pytest.importorskip("torch")
    source = mlp_state()
    parameters = (ExternalParameter("width", "hidden_size", "int", 8, True),)
    method = source.entrypoints[0]
    elements = (
        method.elements[0],
        element(
            "hidden",
            "linear",
            "hidden",
            {"in_features": 4, "out_features": 8},
            {"out_features": ParameterBinding("width")},
        ),
        method.elements[2],
        element(
            "head",
            "linear",
            "head",
            {"in_features": 8, "out_features": 1},
            {"in_features": ParameterBinding("width")},
        ),
    )
    state = PyTorchMetaBlockState(
        1,
        "model-1",
        "Tiny Model",
        parameters,
        (),
        (MethodGraph(method.name, method.inputs, method.outputs, elements, method.edges, ()),),
        {},
    )

    default = compile_model(state)
    widened = compile_model(state, {"hidden_size": 12})

    assert default.materialization_key != widened.materialization_key
    assert "nn.Linear(4, 12" in widened.generated_source
    assert "nn.Linear(12, 1" in widened.generated_source


def test_compiler_emits_repeated_group_as_module_list_and_runs():
    pytest.importorskip("torch")
    import torch

    definition = GroupDefinition(
        "projection",
        "Projection",
        (GroupPort("x", Endpoint("dense", "input")),),
        (GroupPort("y", Endpoint("dense", "output")),),
        (element("dense", "linear", "dense", {"in_features": 4, "out_features": 4}),),
        (),
        (),
    )
    method = MethodGraph(
        "forward",
        (MethodInput("tokens", ("batch", 4), "float32"),),
        (MethodOutput("output", Endpoint("projection_call", "y")),),
        (
            element(
                "x",
                "input",
                "tokens",
                {"name": "tokens", "shape": ["batch", 4], "dtype": "float32"},
            ),
        ),
        (edge("call", "x", "output", "projection_call", "x"),),
        (GroupInstance("projection_call", "projection", "projection_call", 2, {}),),
    )
    state = PyTorchMetaBlockState(1, "grouped", "Grouped", (), (definition,), (method,), {})

    result = compile_model(state)
    model = result.factory()

    assert tuple(model(torch.ones(2, 4))["output"].shape) == (2, 4)
    assert "nn.ModuleList([Projection() for _ in range(2)])" in result.generated_source
    assert any(
        site.logical_element_id == "dense" and site.module_path.endswith(".0.layers.dense")
        for site in result.bindings.sites
    )
    assert any(
        site.logical_element_id == "dense" and site.module_path.endswith(".1.layers.dense")
        for site in result.bindings.sites
    )


def test_compile_refuses_invalid_graph_and_reports_normative_shape_code():
    bad = mlp_state()
    method = bad.entrypoints[0]
    broken = PyTorchMetaBlockState(
        1,
        bad.metablock_id,
        bad.name,
        bad.external_parameters,
        bad.groups,
        (
            MethodGraph(
                method.name, method.inputs, method.outputs, method.elements, method.edges[:-1], ()
            ),
        ),
        bad.metadata,
    )

    with pytest.raises(PyTorchCompilationError) as error:
        compile_model(broken)

    assert error.value.diagnostics
    assert not any(item.code == "pytorch_codegen_failed" for item in error.value.diagnostics)
