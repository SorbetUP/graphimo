from __future__ import annotations

import gc

import pytest
import torch

from graphimo_pytorch import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    ExternalParameter,
    GraphimoPytorchError,
    GroupDefinition,
    GroupInstance,
    GroupPort,
    MethodGraph,
    MethodInput,
    MethodOutput,
    ParameterBinding,
    PyTorchMetaBlockState,
    TensorContract,
    compile_model,
    runtime_registry,
)


def _mlp_state(*, width: int = 8, external: bool = False) -> PyTorchMetaBlockState:
    params = (ExternalParameter("width-param", "width", "int", width, True),) if external else ()
    binding = {"out_features": ParameterBinding("width-param")} if external else {}
    chosen_width = 8 if external else width
    elements = (
        ElementRecord("input", "input", "input", {"method_input": "features"}),
        ElementRecord("linear", "linear", "projection",
                      {"in_features": 4} if external else {"in_features": 4, "out_features": chosen_width}, binding),
        ElementRecord("relu", "relu", "activation"),
        ElementRecord("output", "output", "logits", {"method_output": "logits"}),
    )
    edges = (
        EdgeRecord("e1", Endpoint("input", "out"), Endpoint("linear", "x")),
        EdgeRecord("e2", Endpoint("linear", "out"), Endpoint("relu", "x")),
        EdgeRecord("e3", Endpoint("relu", "out"), Endpoint("output", "x")),
    )
    method = MethodGraph("forward", (MethodInput("features", "features", TensorContract(("e", 4), "float32")),),
                         (MethodOutput("logits", "logits"),), elements, edges, ())
    return PyTorchMetaBlockState(1, "mlp-model", "Small MLP", params, (), (method,), {})


def test_p3_compiles_readable_mlp_and_instantiates_definition() -> None:
    result = compile_model(_mlp_state())
    model = result.definition.instantiate()
    value = model(torch.randn(3, 4))

    assert value.shape == (3, 8)
    assert "class Small_MLP(nn.Module):" in result.generated_source
    assert "self.projection = nn.Linear(4, 8, bias=True)" in result.generated_source
    assert "# graphimo-graph-revision:" in result.generated_source
    assert result.bindings.for_element("linear")[0].module_path == "projection"
    runtime = model.__graphimo_pytorch_runtime__
    assert runtime_registry.resolve(runtime.runtime_instance_id) is model
    assert runtime.graph_revision == result.definition.graph_revision


def test_p3_external_values_change_materialization_without_changing_graph_revision() -> None:
    state = _mlp_state(external=True)
    first = compile_model(state)
    wider = compile_model(state, {"width-param": 12})

    assert first.definition.graph_revision == wider.definition.graph_revision
    assert first.definition.materialization_key != wider.definition.materialization_key
    assert wider.definition.instantiate()(torch.randn(2, 4)).shape == (2, 12)


def test_p3_compiles_repeated_group_with_stable_module_paths() -> None:
    group = GroupDefinition(
        "projection-group", "ProjectionGroup",
        (GroupPort("group-in", "x", TensorContract(("e", 4), "float32")),),
        (GroupPort("group-out", "y", TensorContract(("e", 4), "float32")),),
        (ElementRecord("inner-linear", "linear", "projection", {"in_features": 4, "out_features": 4}),),
        (EdgeRecord("group-input", Endpoint("group-in", "out"), Endpoint("inner-linear", "x")),
         EdgeRecord("group-output", Endpoint("inner-linear", "out"), Endpoint("group-out", "x"))),
        (),
    )
    elements = (
        ElementRecord("input", "input", "input", {"method_input": "features"}),
        ElementRecord("output", "output", "output", {"method_output": "values"}),
    )
    method = MethodGraph(
        "forward", (MethodInput("features", "features", TensorContract(("e", 4), "float32")),),
        (MethodOutput("values", "values"),), elements,
        (EdgeRecord("outer-in", Endpoint("input", "out"), Endpoint("layers", "x")),
         EdgeRecord("outer-out", Endpoint("layers", "y"), Endpoint("output", "x"))),
        (GroupInstance("layers", "projection-group", "layers", 2, {}),),
    )
    state = PyTorchMetaBlockState(1, "repeat-model", "Repeated Model", (), (group,), (method,), {})
    result = compile_model(state)
    model = result.definition.instantiate()
    assert model(torch.randn(5, 4)).shape == (5, 4)
    sites = result.bindings.for_element("inner-linear")
    assert [site.module_path for site in sites] == ["layers.0.projection", "layers.1.projection"]
    assert [site.group_instance_path[0].repetition_index for site in sites] == [0, 1]
    assert "nn.ModuleList" in result.generated_source


def test_p3_rejects_invalid_graph_before_source_compilation() -> None:
    state = _mlp_state()
    method = state.entrypoints[0]
    broken = MethodGraph(method.name, method.inputs, method.outputs,
                         tuple(ElementRecord(item.id, item.kind, item.name, item.params, item.parameter_bindings, item.metadata)
                               for item in method.elements),
                         method.edges[:-1], method.group_instances)
    state = PyTorchMetaBlockState(state.schema_version, state.metablock_id, state.name, state.external_parameters,
                                  state.groups, (broken,), state.metadata)
    with pytest.raises(GraphimoPytorchError) as error:
        compile_model(state)
    assert error.value.code == "pytorch_compile_invalid"


def test_p3_runtime_registry_uses_weak_references_and_release() -> None:
    result = compile_model(_mlp_state())
    model = result.definition.instantiate()
    runtime_id = model.__graphimo_pytorch_runtime__.runtime_instance_id
    assert runtime_registry.resolve(runtime_id) is model
    del model
    gc.collect()
    assert runtime_registry.resolve(runtime_id) is None


def test_p3_compiles_addressable_attention_helpers_and_mask() -> None:
    elements = (
        ElementRecord("input", "input", "input_ids", {"method_input": "tokens"}),
        ElementRecord("embed", "embedding", "token_embedding", {"num_embeddings": 32, "embedding_dim": 16}),
        ElementRecord("split", "split_heads", "heads", {"n_heads": 4}),
        ElementRecord("transpose", "transpose", "keys_transposed", {"dim0": -1, "dim1": -2}),
        ElementRecord("matmul", "matmul", "attention_scores"),
        ElementRecord("mask", "causal_mask", "causal_scores"),
        ElementRecord("out", "output", "scores", {"method_output": "scores"}),
    )
    edges = (
        EdgeRecord("e1", Endpoint("input", "out"), Endpoint("embed", "x")),
        EdgeRecord("e2", Endpoint("embed", "out"), Endpoint("split", "x")),
        EdgeRecord("e3", Endpoint("split", "out"), Endpoint("matmul", "a")),
        EdgeRecord("e4", Endpoint("split", "out"), Endpoint("transpose", "x")),
        EdgeRecord("e5", Endpoint("transpose", "out"), Endpoint("matmul", "b")),
        EdgeRecord("e6", Endpoint("matmul", "out"), Endpoint("mask", "x")),
        EdgeRecord("e7", Endpoint("mask", "out"), Endpoint("out", "x")),
    )
    method = MethodGraph("forward", (MethodInput("tokens", "tokens", TensorContract(("e", "t"), "int64")),),
                         (MethodOutput("scores", "scores"),), elements, edges, ())
    state = PyTorchMetaBlockState(1, "attention", "Tiny Attention", (), (), (method,), {})
    result = compile_model(state)
    scores = result.definition.instantiate()(torch.randint(0, 32, (2, 5)))
    assert scores.shape == (2, 4, 5, 5)
    assert torch.isneginf(scores[..., 0, 1]).all()
    assert result.bindings.for_element("matmul")[0].inspectable
    assert "MatMulOp()" in result.generated_source


def test_p3_compiles_symbolic_reshape_against_input_dimensions() -> None:
    elements = (
        ElementRecord("input", "input", "input", {"method_input": "x"}),
        ElementRecord("reshape", "reshape", "reshape", {"target_shape": ["e", -1]}),
        ElementRecord("out", "output", "output", {"method_output": "y"}),
    )
    edges = (EdgeRecord("in", Endpoint("input", "out"), Endpoint("reshape", "x")),
             EdgeRecord("out", Endpoint("reshape", "out"), Endpoint("out", "x")))
    method = MethodGraph("forward", (MethodInput("x", "x", TensorContract(("e", 16), "float32")),),
                         (MethodOutput("y", "y"),), elements, edges, ())
    definition = compile_model(PyTorchMetaBlockState(1, "reshape", "Reshape", (), (), (method,), {})).definition
    assert definition.instantiate()(torch.randn(3, 16)).shape == (3, 16)

