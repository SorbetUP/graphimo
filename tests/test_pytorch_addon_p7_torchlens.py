from __future__ import annotations

import json

import pytest
import torch

from graphimo.artifacts import LocalArtifactStore
from graphimo_pytorch import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    GraphimoPytorchError,
    MethodGraph,
    MethodInput,
    MethodOutput,
    PyTorchMetaBlockState,
    TensorContract,
    TorchLensIntervention,
    TorchLensRequest,
    compile_model,
    inspect_with_torchlens,
    save_torchlens_inspection,
)


def _compiled_model():
    elements = (
        ElementRecord("input", "input", "input", {"method_input": "features"}),
        ElementRecord("projection", "linear", "projection", {"in_features": 4, "out_features": 3}),
        ElementRecord("output", "output", "output", {"method_output": "scores"}),
    )
    edges = (
        EdgeRecord("in", Endpoint("input", "out"), Endpoint("projection", "x")),
        EdgeRecord("out", Endpoint("projection", "out"), Endpoint("output", "x")),
    )
    method = MethodGraph(
        "forward",
        (MethodInput("features", "features", TensorContract(("e", 4), "float32")),),
        (MethodOutput("scores", "scores"),),
        elements,
        edges,
        (),
    )
    state = PyTorchMetaBlockState(1, "torchlens-test", "TorchLens test", (), (), (method,), {})
    return compile_model(state).definition.instantiate()


def _request(model, *, intervention=None, gradients=False):
    runtime = model.__graphimo_pytorch_runtime__
    return TorchLensRequest(runtime.runtime_instance_id, "projection", capture_gradients=gradients,
                            intervention=intervention)


def test_p7_torchlens_captures_explicit_activation_and_loss_gradients() -> None:
    pytest.importorskip("torchlens")
    model = _compiled_model()
    parameter = next(model.parameters())
    parameter.grad = torch.full_like(parameter, 3.0)
    original_grad = parameter.grad.clone()
    result = inspect_with_torchlens(_request(model, gradients=True), torch.ones(2, 4), loss_fn=lambda output: output.square().mean())

    assert result.module_path == "projection"
    assert result.activation is not None and result.activation["shape"] == [2, 3]
    assert result.gradient is not None and result.gradient["shape"] == [2, 3]
    assert result.operations and result.operation_count >= len(result.operations)
    assert result.trace_metadata["explicit_input"] is True
    assert result.trace_metadata["torchlens_version"] == "2.34.1"
    assert result.experimental is False
    assert torch.equal(parameter.grad, original_grad)


@pytest.mark.parametrize("intervention", [TorchLensIntervention("zero"), TorchLensIntervention("scale", 2.0)])
def test_p7_interventions_are_experimental_and_do_not_mutate_model_weights(intervention) -> None:
    pytest.importorskip("torchlens")
    model = _compiled_model()
    inputs = torch.full((2, 4), 0.5)
    before = {name: value.detach().clone() for name, value in model.state_dict().items()}
    baseline = inspect_with_torchlens(_request(model), inputs)
    result = inspect_with_torchlens(_request(model, intervention=intervention), inputs)

    assert result.experimental is True
    assert result.intervention == intervention
    assert result.activation is not None and baseline.activation is not None
    if intervention.kind == "zero":
        assert result.activation.get("max", 0) == 0
    else:
        assert result.activation["mean"] == pytest.approx(2 * baseline.activation["mean"])
    assert all(torch.equal(before[name], value) for name, value in model.state_dict().items())


def test_p7_full_trace_is_saved_only_after_explicit_export(tmp_path) -> None:
    pytest.importorskip("torchlens")
    model = _compiled_model()
    result = inspect_with_torchlens(_request(model), torch.ones(2, 4))
    store = LocalArtifactStore(tmp_path / "store")

    assert result.artifact_ref is None
    artifact = save_torchlens_inspection(result, store)
    assert artifact.kind == "pytorch-torchlens-trace"
    exported = store.materialize(artifact, tmp_path / "exported")
    report = json.loads((exported / "inspection.json").read_text(encoding="utf-8"))
    assert report["metablock_id"] == model.__graphimo_pytorch_runtime__.metablock_id
    assert report["graph_revision"] == result.graph_revision
    assert report["materialization_key"] == result.materialization_key
    assert report["runtime_instance_id"] == result.runtime_instance_id
    assert report["logical_element_id"] == "projection"
    assert report["module_path"] == "projection"
    assert report["torchlens_version"] == "2.34.1"


def test_p7_gradients_without_loss_fn_are_reported_without_implicit_backward() -> None:
    pytest.importorskip("torchlens")
    model = _compiled_model()
    result = inspect_with_torchlens(_request(model, gradients=True), torch.ones(2, 4))

    assert result.gradient is None
    assert any(item.code == "pytorch_torchlens_loss_fn_required" for item in result.diagnostics)


def test_p7_scale_requires_finite_factor() -> None:
    with pytest.raises(GraphimoPytorchError, match="finite"):
        TorchLensIntervention("scale", float("nan"))
