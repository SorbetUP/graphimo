from __future__ import annotations

from graphimo_pytorch import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    MethodGraph,
    MethodInput,
    MethodOutput,
    PyTorchMetaBlockState,
    TensorContract,
    element_kinds,
    infer_model_shapes,
    validate_model_graph,
)


def _state(elements: tuple[ElementRecord, ...], edges: tuple[EdgeRecord, ...], *, shape=("e", 16)) -> PyTorchMetaBlockState:
    method = MethodGraph(
        "forward",
        (MethodInput("x", "x", TensorContract(tuple(shape), "float32", "features")),),
        (MethodOutput("y", "y"),),
        elements,
        edges,
        (),
    )
    return PyTorchMetaBlockState(1, "model", "MLP", (), (), (method,), {})


def _mlp(*, first_out: int = 32, second_in: int = 32) -> PyTorchMetaBlockState:
    elements = (
        ElementRecord("in", "input", "x", {"method_input": "x"}),
        ElementRecord("fc1", "linear", "fc1", {"in_features": 16, "out_features": first_out}),
        ElementRecord("act", "relu", "relu"),
        ElementRecord("fc2", "linear", "fc2", {"in_features": second_in, "out_features": 4}),
        ElementRecord("out", "output", "y", {"method_output": "y"}),
    )
    edges = (
        EdgeRecord("e1", Endpoint("in", "out"), Endpoint("fc1", "x")),
        EdgeRecord("e2", Endpoint("fc1", "out"), Endpoint("act", "x")),
        EdgeRecord("e3", Endpoint("act", "out"), Endpoint("fc2", "x")),
        EdgeRecord("e4", Endpoint("fc2", "out"), Endpoint("out", "x")),
    )
    return _state(elements, edges)


def test_p2_registers_exact_v1_semantic_catalog() -> None:
    assert {item.kind for item in element_kinds()} == {
        "input", "output", "embedding", "linear", "rms_norm", "dropout", "relu", "gelu", "silu", "softmax",
        "reshape", "transpose", "flatten", "unflatten", "split_heads", "merge_heads", "concatenate", "add",
        "multiply", "scale", "matmul", "rope", "causal_mask", "cross_entropy", "group_instance",
    }


def test_p2_infers_mlp_linear_and_activation_shapes() -> None:
    state = _mlp()
    report = validate_model_graph(state)
    assert report.valid, report.diagnostics
    result = infer_model_shapes(state)
    assert result.valid, result.diagnostics
    assert tuple(dim.expr for dim in result.output_specs["fc1.out"].shape) == ("e", "32")
    assert tuple(dim.expr for dim in result.output_specs["act.out"].shape) == ("e", "32")
    assert tuple(dim.expr for dim in result.output_specs["fc2.out"].shape) == ("e", "4")
    assert result.output_specs["method_output:y"].shape[-1].value == 4
    assert result.edge_specs["forward:e4"].shape[-1].value == 4


def test_p2_reports_linear_shape_mismatch_with_element_identity() -> None:
    result = infer_model_shapes(_mlp(second_in=99))
    assert not result.valid
    assert any(item.code == "pytorch_shape_incompatible" and item.element_id == "fc2" for item in result.diagnostics)


def test_p2_infers_embedding_and_attention_shapes() -> None:
    elements = (
        ElementRecord("tokens", "input", "input_ids", {"method_input": "tokens"}),
        ElementRecord("embed", "embedding", "embedding", {"num_embeddings": 128, "embedding_dim": 32}),
        ElementRecord("split", "split_heads", "heads", {"n_heads": 4}),
        ElementRecord("transpose", "transpose", "keys_t", {"dim0": -1, "dim1": -2}),
        ElementRecord("scores", "matmul", "scores"),
        ElementRecord("mask", "causal_mask", "masked_scores"),
        ElementRecord("out", "output", "logits", {"method_output": "logits"}),
    )
    edges = (
        EdgeRecord("a", Endpoint("tokens", "out"), Endpoint("embed", "x")),
        EdgeRecord("b", Endpoint("embed", "out"), Endpoint("split", "x")),
        EdgeRecord("c", Endpoint("split", "out"), Endpoint("scores", "a")),
        EdgeRecord("d", Endpoint("split", "out"), Endpoint("transpose", "x")),
        EdgeRecord("e", Endpoint("transpose", "out"), Endpoint("scores", "b")),
        EdgeRecord("f", Endpoint("scores", "out"), Endpoint("mask", "x")),
        EdgeRecord("g", Endpoint("mask", "out"), Endpoint("out", "x")),
    )
    method = MethodGraph("forward", (MethodInput("tokens", "tokens", TensorContract(("e", "t"), "int64", "token_ids")),),
                         (MethodOutput("logits", "logits"),), elements, edges, ())
    result = infer_model_shapes(PyTorchMetaBlockState(1, "attention", "Attention", (), (), (method,), {}))
    assert result.valid, result.diagnostics
    assert tuple(dim.expr for dim in result.output_specs["split.out"].shape) == ("e", "h", "t", "32/h")
    assert tuple(dim.expr for dim in result.output_specs["scores.out"].shape) == ("e", "h", "t", "t")


def test_p2_checks_head_divisibility_and_matmul_compatibility() -> None:
    split = _state(
        (ElementRecord("in", "input", "x", {"method_input": "x"}),
         ElementRecord("split", "split_heads", "heads", {"n_heads": 3}),
         ElementRecord("out", "output", "y", {"method_output": "y"})),
        (EdgeRecord("a", Endpoint("in", "out"), Endpoint("split", "x")),
         EdgeRecord("b", Endpoint("split", "out"), Endpoint("out", "x"))),
        shape=("e", "t", 10),
    )
    result = infer_model_shapes(split)
    assert any(item.code == "pytorch_split_heads_divisibility" for item in result.diagnostics)

    elements = (
        ElementRecord("left", "input", "left", {"method_input": "x"}),
        ElementRecord("right", "input", "right", {"method_input": "z"}),
        ElementRecord("mm", "matmul", "mm"),
        ElementRecord("out", "output", "y", {"method_output": "y"}),
    )
    edges = (
        EdgeRecord("a", Endpoint("left", "out"), Endpoint("mm", "a")),
        EdgeRecord("b", Endpoint("right", "out"), Endpoint("mm", "b")),
        EdgeRecord("c", Endpoint("mm", "out"), Endpoint("out", "x")),
    )
    method = MethodGraph("forward", (MethodInput("x", "left", TensorContract((2, 8), "float32")),
                                      MethodInput("z", "right", TensorContract((7, 4), "float32"))),
                         (MethodOutput("y", "y"),), elements, edges, ())
    broken = PyTorchMetaBlockState(1, "matmul", "Matmul", (), (), (method,), {})
    result = infer_model_shapes(broken)
    assert any(item.code == "pytorch_shape_incompatible" and item.element_id == "mm" for item in result.diagnostics)


def test_p2_validates_unknown_ports_cycles_and_invalid_params() -> None:
    state = _state(
        (ElementRecord("in", "input", "x", {"method_input": "x"}),
         ElementRecord("fc", "linear", "fc", {"in_features": 0, "out_features": 8}),
         ElementRecord("relu", "relu", "relu"),
         ElementRecord("out", "output", "y", {"method_output": "y"})),
        (EdgeRecord("a", Endpoint("in", "unknown"), Endpoint("fc", "x")),
         EdgeRecord("b", Endpoint("fc", "out"), Endpoint("relu", "x")),
         EdgeRecord("c", Endpoint("relu", "out"), Endpoint("fc", "x")),
         EdgeRecord("d", Endpoint("relu", "out"), Endpoint("out", "x"))),
    )
    report = validate_model_graph(state)
    codes = {item.code for item in report.diagnostics}
    assert {"pytorch_edge_source_invalid", "pytorch_graph_cycle", "pytorch_parameter_range_invalid"} <= codes


def test_p2_add_broadcast_and_cross_entropy_scalar_shapes() -> None:
    elements = (
        ElementRecord("a", "input", "a", {"method_input": "a"}),
        ElementRecord("b", "input", "b", {"method_input": "b"}),
        ElementRecord("sum", "add", "sum"),
        ElementRecord("loss", "cross_entropy", "loss", {"mode": "generic"}),
        ElementRecord("out", "output", "out", {"method_output": "out"}),
    )
    edges = (
        EdgeRecord("a1", Endpoint("a", "out"), Endpoint("sum", "a")),
        EdgeRecord("b1", Endpoint("b", "out"), Endpoint("sum", "b")),
        EdgeRecord("a2", Endpoint("sum", "out"), Endpoint("loss", "logits")),
        EdgeRecord("b2", Endpoint("b", "out"), Endpoint("loss", "target")),
        EdgeRecord("c", Endpoint("loss", "out"), Endpoint("out", "x")),
    )
    method = MethodGraph("forward", (MethodInput("a", "a", TensorContract(("e", 8), "float32")),
                                      MethodInput("b", "b", TensorContract((1, 8), "float32"))),
                         (MethodOutput("out", "loss"),), elements, edges, ())
    result = infer_model_shapes(PyTorchMetaBlockState(1, "broadcast", "Broadcast", (), (), (method,), {}))
    assert result.valid, result.diagnostics
    assert tuple(dim.expr for dim in result.output_specs["sum.out"].shape) == ("e", "8")
    assert result.output_specs["loss.out"].shape == ()


def test_p2_tensor_transform_and_rope_rules() -> None:
    elements = (
        ElementRecord("in", "input", "x", {"method_input": "x"}),
        ElementRecord("reshape", "reshape", "reshape", {"target_shape": [2, -1]}),
        ElementRecord("transpose", "transpose", "transpose", {"dim0": 0, "dim1": 1}),
        ElementRecord("flatten", "flatten", "flatten", {"start_dim": 0, "end_dim": 1}),
        ElementRecord("unflatten", "unflatten", "unflatten", {"dim": 0, "sizes": [4, 8]}),
        ElementRecord("rope", "rope", "rope"),
        ElementRecord("out", "output", "y", {"method_output": "y"}),
    )
    edges = tuple(
        EdgeRecord(f"e{index}", Endpoint(source, "out"), Endpoint(target, "x"))
        for index, (source, target) in enumerate((
            ("in", "reshape"), ("reshape", "transpose"), ("transpose", "flatten"),
            ("flatten", "unflatten"), ("unflatten", "rope"), ("rope", "out"),
        ))
    )
    state = _state(elements, edges, shape=(2, 16))
    result = infer_model_shapes(state)
    assert result.valid, result.diagnostics
    assert result.output_specs["reshape.out"].shape[1].value == 16
    assert tuple(dim.expr for dim in result.output_specs["unflatten.out"].shape) == ("4", "8")
    assert result.output_specs["rope.out"].shape == result.output_specs["unflatten.out"].shape

    odd_rope = _state(
        (ElementRecord("in", "input", "x", {"method_input": "x"}),
         ElementRecord("rope", "rope", "rope"),
         ElementRecord("out", "output", "y", {"method_output": "y"})),
        (EdgeRecord("a", Endpoint("in", "out"), Endpoint("rope", "x")),
         EdgeRecord("b", Endpoint("rope", "out"), Endpoint("out", "x"))),
        shape=("e", 15),
    )
    assert any(item.code == "pytorch_rope_dimension_odd" for item in infer_model_shapes(odd_rope).diagnostics)


def test_p2_group_recursion_and_invalid_external_binding_are_reported() -> None:
    from graphimo_pytorch import GroupDefinition, GroupInstance, ParameterBinding

    group_a = GroupDefinition("a", "A", (), (), (), (), (GroupInstance("ab", "b", "b", 1, {}),))
    group_b = GroupDefinition("b", "B", (), (), (), (), (GroupInstance("ba", "a", "a", 1, {}),))
    state = _mlp()
    forward = state.entrypoints[0]
    elements = tuple(
        ElementRecord(element.id, element.kind, element.name, element.params,
                      {"out_features": ParameterBinding("missing")} if element.id == "fc1" else element.parameter_bindings,
                      element.metadata)
        for element in forward.elements
    )
    state = PyTorchMetaBlockState(
        1, "cycle", "Cycle", (), (group_a, group_b),
        (MethodGraph(forward.name, forward.inputs, forward.outputs, elements, forward.edges, forward.group_instances),), {},
    )
    codes = {item.code for item in validate_model_graph(state).diagnostics}
    assert "pytorch_group_recursive" in codes
    assert "pytorch_external_parameter_unresolved" in codes


def test_p2_propagates_shapes_through_repeated_group_instances() -> None:
    from graphimo_pytorch import GroupDefinition, GroupInstance, GroupPort

    group = GroupDefinition(
        "identity", "IdentityGroup",
        (GroupPort("group-x", "x", TensorContract(("e", 16), "float32")),),
        (GroupPort("group-y", "y", TensorContract(("e", 16), "float32")),),
        (ElementRecord("relu", "relu", "relu"),),
        (EdgeRecord("group-in", Endpoint("group-x", "out"), Endpoint("relu", "x")),
         EdgeRecord("group-out", Endpoint("relu", "out"), Endpoint("group-y", "x"))),
        (),
    )
    elements = (
        ElementRecord("in", "input", "x", {"method_input": "x"}),
        ElementRecord("out", "output", "y", {"method_output": "y"}),
    )
    method = MethodGraph(
        "forward", (MethodInput("x", "x", TensorContract(("e", 16), "float32")),),
        (MethodOutput("y", "y"),), elements,
        (EdgeRecord("outer-in", Endpoint("in", "out"), Endpoint("layers", "x")),
         EdgeRecord("outer-out", Endpoint("layers", "y"), Endpoint("out", "x"))),
        (GroupInstance("layers", "identity", "layers", 3, {}),),
    )
    result = infer_model_shapes(PyTorchMetaBlockState(1, "group-model", "GroupModel", (), (group,), (method,), {}))
    assert result.valid, result.diagnostics
    assert tuple(dim.expr for dim in result.output_specs["layers.y"].shape) == ("e", "16")


