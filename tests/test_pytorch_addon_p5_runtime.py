from __future__ import annotations

import torch
import pytest

from graphimo_pytorch import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    MethodGraph,
    MethodInput,
    MethodOutput,
    PyTorchMetaBlockState,
    SubscriptionSpec,
    TensorContract,
    emit_cuda_resources,
    emit_metric,
    event_queue,
    release_runtime_model,
    runtime_registry,
    subscribe,
    subscription_manager,
    summarize_tensor,
    unsubscribe,
)
from graphimo_pytorch.compiler import compile_model


def _model():
    elements = (
        ElementRecord("input", "input", "input", {"method_input": "x"}),
        ElementRecord("linear", "linear", "projection", {"in_features": 4, "out_features": 3}),
        ElementRecord("relu", "relu", "activation"),
        ElementRecord("output", "output", "output", {"method_output": "y"}),
    )
    edges = (
        EdgeRecord("e1", Endpoint("input", "out"), Endpoint("linear", "x")),
        EdgeRecord("e2", Endpoint("linear", "out"), Endpoint("relu", "x")),
        EdgeRecord("e3", Endpoint("relu", "out"), Endpoint("output", "x")),
    )
    method = MethodGraph("forward", (MethodInput("x", "x", TensorContract(("e", 4), "float32")),),
                         (MethodOutput("y", "y"),), elements, edges, ())
    definition = compile_model(PyTorchMetaBlockState(1, "live-model", "Live Model", (), (), (method,), {})).definition
    return definition, definition.instantiate()


def _spec(model, identifier: str, signal: str, *, detail: str = "summary", rate_hz: float = 100.0):
    runtime_id = model.__graphimo_pytorch_runtime__.runtime_instance_id
    return SubscriptionSpec(identifier, runtime_id, "linear", None, signal, rate_hz, detail)


def _drain():
    event_queue.drain(2048)


def test_p5_activation_subscription_emits_bounded_event_and_unsubscribes() -> None:
    _drain()
    _, model = _model()
    spec = _spec(model, "activation-sub", "activation")
    sites = subscribe(model, spec)
    assert sites[0].module_path == "projection"
    output = model(torch.randn(2, 4))
    assert output.shape == (2, 3)
    events = event_queue.drain()
    activation = next(event for event in events if event.kind == "activation.summary")
    assert activation.logical_element_id == "linear"
    assert activation.module_path == "projection"
    assert activation.payload["summary"]["shape"] == [2, 3]
    assert unsubscribe(spec.subscription_id)
    _drain()
    model(torch.randn(2, 4))
    assert not any(event.kind == "activation.summary" for event in event_queue.drain())


def test_p5_gradient_subscription_observes_output_gradient_without_modifying_it() -> None:
    _drain()
    _, model = _model()
    spec = _spec(model, "gradient-sub", "gradient")
    subscribe(model, spec)
    expected = model(torch.randn(2, 4))
    expected.sum().backward()
    events = event_queue.drain()
    gradient = next(event for event in events if event.kind == "gradient.summary")
    assert gradient.payload["output_shape"] == [2, 3]
    assert gradient.payload["shape"] == [2, 3]
    unsubscribe(spec.subscription_id)


def test_p5_tensor_summary_is_json_safe_and_bounded_for_empty_nan_histogram_and_sample() -> None:
    summary = summarize_tensor(torch.tensor([1.0, float("nan"), 3.0]))
    assert summary["finite_fraction"] == pytest.approx(2 / 3)
    assert summary["mean"] is None
    histogram = summarize_tensor(torch.arange(100_000, dtype=torch.float32), detail="histogram", bins=12)
    assert len(histogram["histogram"]["counts"]) == 12
    sample = summarize_tensor(torch.arange(20_000), detail="sample")
    assert len(sample["sample"]) == 4096
    empty = summarize_tensor(torch.empty((0, 4)))
    assert empty["numel"] == 0
    assert "mean" not in empty
    assert not any(isinstance(value, torch.Tensor) for value in summary.values())


def test_p5_metrics_and_cpu_resources_use_runtime_provenance() -> None:
    _drain()
    _, model = _model()
    metric = emit_metric(model, "train/loss", torch.tensor(0.25), step=12)
    assert metric.kind == "metric.scalar"
    assert metric.step == 12
    assert metric.payload == {"name": "train/loss", "value": 0.25}
    assert emit_cuda_resources(model) is None
    assert any(event.kind == "metric.scalar" for event in event_queue.drain())


def test_p5_rate_limit_duplicate_and_release_cleanup() -> None:
    _drain()
    definition, model = _model()
    spec = _spec(model, "limited", "activation", rate_hz=0.01)
    subscribe(model, spec)
    model(torch.randn(2, 4))
    model(torch.randn(2, 4))
    activations = [event for event in event_queue.drain() if event.kind == "activation.summary"]
    assert len(activations) == 1
    assert unsubscribe(spec.subscription_id)

    second = definition.instantiate()
    runtime_id = second.__graphimo_pytorch_runtime__.runtime_instance_id
    spec = _spec(second, "release-sub", "activation")
    subscribe(second, spec)
    release_runtime_model(second)
    assert not any(item.runtime_instance_id == runtime_id for item in runtime_registry.list_for_metablock("live-model"))
    assert not any(item.subscription_id == "release-sub" for item in subscription_manager.active())


def test_p5_gradient_subscription_emits_one_warning_for_non_grad_outputs() -> None:
    _drain()
    _, model = _model()
    spec = _spec(model, "no-grad", "gradient")
    subscribe(model, spec)
    with torch.no_grad():
        model(torch.randn(2, 4))
        model(torch.randn(2, 4))
    warnings = [event for event in event_queue.drain() if event.payload.get("code") == "gradient_unavailable"]
    assert len(warnings) == 1
    assert unsubscribe(spec.subscription_id)

