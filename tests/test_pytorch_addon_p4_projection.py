from __future__ import annotations

from graphimo_pytorch import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    GroupDefinition,
    MethodGraph,
    MethodInput,
    MethodOutput,
    PyTorchMetaBlockState,
    TensorContract,
    compile_model,
    demote_parameter,
    project_workbench,
    promote_parameter,
    set_element_parameter,
)


def _state() -> PyTorchMetaBlockState:
    elements = (
        ElementRecord("input", "input", "features", {"method_input": "x"}),
        ElementRecord("linear", "linear", "projection", {"in_features": 4, "out_features": 8, "bias": False}),
        ElementRecord("relu", "relu", "activation"),
        ElementRecord("output", "output", "values", {"method_output": "y"}),
    )
    edges = (
        EdgeRecord("a", Endpoint("input", "out"), Endpoint("linear", "x")),
        EdgeRecord("b", Endpoint("linear", "out"), Endpoint("relu", "x")),
        EdgeRecord("c", Endpoint("relu", "out"), Endpoint("output", "x")),
    )
    method = MethodGraph("forward", (MethodInput("x", "x", TensorContract(("e", 4), "float32")),),
                         (MethodOutput("y", "y"),), elements, edges, ())
    group = GroupDefinition("block", "Reusable Block", (), (), (), (), ())
    return PyTorchMetaBlockState(1, "projected", "Projected MLP", (), (group,), (method,), {})


def test_p4_projection_contains_contextual_palette_shapes_inspectors_and_source() -> None:
    state = _state()
    compiled = compile_model(state)
    model = compiled.definition.instantiate()
    projection = project_workbench(state, compile_result=compiled)

    assert projection.summary.name == "Projected MLP"
    assert projection.method_tabs == ("forward",)
    assert {item.kind for item in projection.palette} >= {"input", "output", "linear", "relu", "split_heads", "cross_entropy"}
    linear = next(item for item in projection.cards if item.element_id == "linear")
    assert linear.output_shapes[0][1].shape[-1].value == 8
    assert next(item for item in linear.parameters if item.name == "out_features").structural
    assert next(item for item in projection.edges if item.edge_id == "b").tensor is not None
    assert projection.generated_source == compiled.generated_source
    assert len(projection.runtime_instances) == 1
    assert model.__graphimo_pytorch_runtime__.runtime_instance_id == projection.runtime_instances[0].runtime_instance_id
    assert projection.group_navigation[0].name == "Reusable Block"


def test_p4_scope_projection_navigates_into_group_without_a_second_canvas_model() -> None:
    state = _state()
    projection = project_workbench(state, active_group_id="block")
    assert projection.active_scope_id == "block"
    assert projection.active_scope_name == "Reusable Block"
    assert projection.cards == ()
    assert projection.palette


def test_p4_parameter_edit_promote_and_demote_are_immutable_semantic_operations() -> None:
    state = _state()
    changed = set_element_parameter(state, "forward", "linear", "out_features", 12)
    assert state.entrypoints[0].elements[1].params["out_features"] == 8
    assert changed.entrypoints[0].elements[1].params["out_features"] == 12

    promoted = promote_parameter(changed, "forward", "linear", "out_features",
                                external_parameter_id="p-width", external_name="width")
    linear = promoted.entrypoints[0].elements[1]
    assert "out_features" not in linear.params
    assert linear.parameter_bindings["out_features"].external_parameter_id == "p-width"
    assert promoted.external_parameters[0].default == 12

    updated = set_element_parameter(promoted, "forward", "linear", "out_features", 16)
    assert updated.external_parameters[0].default == 16
    demoted = demote_parameter(updated, "forward", "linear", "out_features")
    linear = demoted.entrypoints[0].elements[1]
    assert linear.params["out_features"] == 16
    assert not linear.parameter_bindings
    assert not demoted.external_parameters

