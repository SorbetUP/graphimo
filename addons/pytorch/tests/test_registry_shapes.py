from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_PACKAGE_PATH = Path(__file__).parents[1] / "graphimo_pytorch"
_PACKAGE_NAME = "graphimo_pytorch_p2_tests"
if _PACKAGE_NAME not in sys.modules:
    _package = ModuleType(_PACKAGE_NAME)
    _package.__path__ = [str(_PACKAGE_PATH)]
    _package.__package__ = _PACKAGE_NAME
    _package.__spec__ = importlib.util.spec_from_loader(_PACKAGE_NAME, loader=None, is_package=True)
    _package.__spec__.submodule_search_locations = [str(_PACKAGE_PATH)]
    sys.modules[_PACKAGE_NAME] = _package

_registry = importlib.import_module(f"{_PACKAGE_NAME}.registry")
_schema = importlib.import_module(f"{_PACKAGE_NAME}.schema")
_shapes = importlib.import_module(f"{_PACKAGE_NAME}.shapes")
ELEMENT_REGISTRY = _registry.ELEMENT_REGISTRY
validate_model_graph = _registry.validate_model_graph
EdgeRecord = _schema.EdgeRecord
ElementRecord = _schema.ElementRecord
Endpoint = _schema.Endpoint
GroupDefinition = _schema.GroupDefinition
GroupInstance = _schema.GroupInstance
GroupPort = _schema.GroupPort
MethodGraph = _schema.MethodGraph
MethodInput = _schema.MethodInput
MethodOutput = _schema.MethodOutput
PyTorchMetaBlockState = _schema.PyTorchMetaBlockState
Dim = _shapes.Dim
TensorSpec = _shapes.TensorSpec
infer_model_shapes = _shapes.infer_model_shapes


def element(identity, kind, name, params=None, bindings=None):
    return ElementRecord(identity, kind, name, params or {}, bindings or {}, {})


def edge(identity, source, source_port, target, target_port):
    return EdgeRecord(identity, Endpoint(source, source_port), Endpoint(target, target_port))


def state(elements, edges, outputs, *, inputs=(), groups=()):
    method = MethodGraph(
        "forward", tuple(inputs), tuple(outputs), tuple(elements), tuple(edges), ()
    )
    return PyTorchMetaBlockState(1, "model", "Model", (), tuple(groups), (method,), {})


def test_registry_contains_exact_v1_semantic_element_kinds():
    assert set(ELEMENT_REGISTRY) == {
        "input",
        "output",
        "embedding",
        "linear",
        "rms_norm",
        "dropout",
        "relu",
        "gelu",
        "silu",
        "softmax",
        "reshape",
        "transpose",
        "flatten",
        "unflatten",
        "split_heads",
        "merge_heads",
        "concatenate",
        "add",
        "multiply",
        "scale",
        "matmul",
        "rope",
        "causal_mask",
        "cross_entropy",
        "group_instance",
    }


def test_mlp_shape_inference_and_validation():
    model = state(
        [
            element(
                "x",
                "input",
                "tokens",
                {"name": "tokens", "shape": ["batch", 4], "dtype": "float32"},
            ),
            element("dense", "linear", "dense", {"in_features": 4, "out_features": 8}),
            element("activation", "gelu", "activation"),
        ],
        [
            edge("e1", "x", "output", "dense", "input"),
            edge("e2", "dense", "output", "activation", "input"),
        ],
        [MethodOutput("output", Endpoint("activation", "output"))],
        inputs=(MethodInput("tokens", ("batch", 4), "float32"),),
    )

    result = infer_model_shapes(model)

    assert result.validation.valid
    assert result.valid
    assert result.port_shapes["forward/dense/output"] == TensorSpec(
        (Dim("batch"), Dim("8", 8)), "float32"
    )
    assert result.edge_shapes["forward/e2"] == result.port_shapes["forward/activation/output"]


def test_attention_matmul_shape_inference_with_concrete_head_dimensions():
    model = state(
        [
            element("q", "input", "q", {"name": "q", "shape": [2, 6, 12], "dtype": "float32"}),
            element("k", "input", "k", {"name": "k", "shape": [2, 6, 12], "dtype": "float32"}),
            element("q_heads", "split_heads", "q_heads", {"n_heads": 3}),
            element("k_heads", "split_heads", "k_heads", {"n_heads": 3}),
            element("k_transpose", "transpose", "k_transpose", {"dim0": -2, "dim1": -1}),
            element("scores", "matmul", "scores"),
        ],
        [
            edge("q1", "q", "output", "q_heads", "input"),
            edge("k1", "k", "output", "k_heads", "input"),
            edge("k2", "k_heads", "output", "k_transpose", "input"),
            edge("q3", "q_heads", "output", "scores", "left"),
            edge("k3", "k_transpose", "output", "scores", "right"),
        ],
        [MethodOutput("scores", Endpoint("scores", "output"))],
        inputs=(MethodInput("q", (2, 6, 12), "float32"), MethodInput("k", (2, 6, 12), "float32")),
    )

    result = infer_model_shapes(model)

    assert result.valid, result.diagnostics
    assert result.port_shapes["forward/scores/output"].shape == (
        Dim("2", 2),
        Dim("3", 3),
        Dim("6", 6),
        Dim("6", 6),
    )


def test_validation_collects_unknown_kind_cycles_ports_and_bad_names():
    model = state(
        [element("a", "not_registered", "bad name"), element("b", "relu", "b")],
        [edge("ab", "a", "missing", "b", "input"), edge("ba", "b", "output", "a", "input")],
        [],
    )

    report = validate_model_graph(model)
    codes = {item.code for item in report.diagnostics}

    assert not report.valid
    assert {
        "pytorch_unknown_element_kind",
        "pytorch_cycle_invalid",
        "pytorch_unknown_port",
        "pytorch_module_name_invalid",
    } <= codes


def test_validation_rejects_multiple_inputs_and_unresolved_parameter_binding():
    model = state(
        [
            element("a", "input", "a", {"name": "a", "shape": [4], "dtype": "float32"}),
            element("b", "input", "b", {"name": "b", "shape": [4], "dtype": "float32"}),
            element("sum", "add", "sum", bindings={"alpha": _binding("missing")}),
        ],
        [edge("a1", "a", "output", "sum", "left"), edge("a2", "b", "output", "sum", "left")],
        [],
    )

    codes = {item.code for item in validate_model_graph(model).diagnostics}
    assert "pytorch_input_multiple_edges" in codes
    assert "pytorch_external_param_missing" in codes


def _binding(identity):
    return _schema.ParameterBinding(identity)


def test_shape_rules_report_attention_divisibility_and_matmul_mismatch():
    model = state(
        [
            element("x", "input", "x", {"name": "x", "shape": [1, 5, 10], "dtype": "float32"}),
            element("heads", "split_heads", "heads", {"n_heads": 3}),
        ],
        [edge("e", "x", "output", "heads", "input")],
        [MethodOutput("out", Endpoint("heads", "output"))],
        inputs=(MethodInput("x", (1, 5, 10), "float32"),),
    )

    result = infer_model_shapes(model)
    assert any(
        item.code == "pytorch_shape_divisibility_error" and "divisible" in item.message
        for item in result.diagnostics
    )


def test_group_shape_is_propagated_through_repeated_instances():
    definition = GroupDefinition(
        "dense-group",
        "DenseGroup",
        (GroupPort("x", Endpoint("dense", "input")),),
        (GroupPort("y", Endpoint("dense", "output")),),
        (element("dense", "linear", "dense", {"in_features": 4, "out_features": 4}),),
        (),
        (),
    )
    model = state(
        [element("x", "input", "x", {"name": "x", "shape": [2, 4], "dtype": "float32"})],
        [edge("call", "x", "output", "layer", "x")],
        [MethodOutput("y", Endpoint("layer", "y"))],
        inputs=(MethodInput("x", (2, 4), "float32"),),
        groups=(definition,),
    )
    model_method = model.entrypoints[0]
    model = PyTorchMetaBlockState(
        1,
        "model",
        "Model",
        (),
        (definition,),
        (
            MethodGraph(
                model_method.name,
                model_method.inputs,
                model_method.outputs,
                model_method.elements,
                model_method.edges,
                (GroupInstance("layer", "dense-group", "layer", 3, {}),),
            ),
        ),
        {},
    )

    result = infer_model_shapes(model)

    assert result.valid, result.diagnostics
    assert result.port_shapes["forward/layer/y"].shape == (Dim("2", 2), Dim("4", 4))


def test_required_v1_element_shape_rules():
    vector = TensorSpec((Dim("e", 2), Dim("t", 3), Dim("d", 8)), "float32")
    attention = TensorSpec((Dim("e", 2), Dim("h", 2), Dim("t", 3), Dim("dh", 4)), "float32")
    cases = [
        (
            "embedding",
            {"input": (TensorSpec((Dim("e", 2), Dim("t", 3)), "int64"),)},
            {"num_embeddings": 16, "embedding_dim": 8},
            (2, 3, 8),
        ),
        ("rms_norm", {"input": (vector,)}, {"normalized_shape": 8, "eps": 1e-5}, (2, 3, 8)),
        ("dropout", {"input": (vector,)}, {"p": 0.1}, (2, 3, 8)),
        ("relu", {"input": (vector,)}, {}, (2, 3, 8)),
        ("gelu", {"input": (vector,)}, {}, (2, 3, 8)),
        ("silu", {"input": (vector,)}, {}, (2, 3, 8)),
        ("softmax", {"input": (vector,)}, {"dim": -1}, (2, 3, 8)),
        ("reshape", {"input": (vector,)}, {"target_shape": [6, -1]}, (6, 8)),
        ("transpose", {"input": (vector,)}, {"dim0": 0, "dim1": 2}, (8, 3, 2)),
        ("flatten", {"input": (vector,)}, {"start_dim": 1, "end_dim": -1}, (2, 24)),
        (
            "unflatten",
            {"input": (TensorSpec((Dim("d", 8),), "float32"),)},
            {"dim": 0, "sizes": [2, 4]},
            (2, 4),
        ),
        ("split_heads", {"input": (vector,)}, {"n_heads": 2}, (2, 2, 3, 4)),
        ("merge_heads", {"input": (attention,)}, {}, (2, 3, 8)),
        (
            "concatenate",
            {
                "input": (
                    TensorSpec((Dim("e", 2), Dim("a", 3))),
                    TensorSpec((Dim("e", 2), Dim("b", 5))),
                )
            },
            {"dim": -1},
            (2, 8),
        ),
        (
            "add",
            {"left": (vector,), "right": (TensorSpec((Dim("d", 8),), "float32"),)},
            {},
            (2, 3, 8),
        ),
        (
            "multiply",
            {"left": (vector,), "right": (TensorSpec((Dim("d", 8),), "float32"),)},
            {},
            (2, 3, 8),
        ),
        ("scale", {"input": (vector,)}, {"factor": 0.5}, (2, 3, 8)),
        (
            "matmul",
            {
                "left": (attention,),
                "right": (
                    TensorSpec((Dim("e", 2), Dim("h", 2), Dim("dh", 4), Dim("t", 3)), "float32"),
                ),
            },
            {},
            (2, 2, 3, 3),
        ),
        ("rope", {"input": (vector,)}, {"base": 10000.0}, (2, 3, 8)),
        (
            "causal_mask",
            {
                "input": (
                    TensorSpec((Dim("e", 2), Dim("h", 2), Dim("t", 3), Dim("t", 3)), "float32"),
                )
            },
            {},
            (2, 2, 3, 3),
        ),
        (
            "cross_entropy",
            {
                "logits": (TensorSpec((Dim("n", 6), Dim("vocab", 16)), "float32"),),
                "target": (TensorSpec((Dim("n", 6),), "int64"),),
            },
            {"mode": "generic"},
            (),
        ),
    ]
    for kind, inputs, params, expected in cases:
        result = ELEMENT_REGISTRY[kind].infer_shapes(inputs, params, {})
        assert tuple(item.value for item in result[0].shape) == expected, kind


def test_group_definition_recursion_has_normative_diagnostic_code():
    first = GroupDefinition(
        "first",
        "First",
        (),
        (),
        (),
        (),
        (GroupInstance("to-second", "second", "to_second", 1, {}),),
    )
    second = GroupDefinition(
        "second", "Second", (), (), (), (), (GroupInstance("to-first", "first", "to_first", 1, {}),)
    )
    model = state([], [], [], groups=(first, second))

    report = validate_model_graph(model)

    assert any(item.code == "pytorch_group_cycle_invalid" for item in report.diagnostics)
