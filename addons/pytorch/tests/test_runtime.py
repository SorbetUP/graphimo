from __future__ import annotations

import gc
import importlib
import importlib.util
import sys
import weakref
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_PACKAGE_PATH = Path(__file__).parents[1] / "graphimo_pytorch"
_PACKAGE_NAME = "graphimo_pytorch_runtime_tests"
if _PACKAGE_NAME not in sys.modules:
    _package = ModuleType(_PACKAGE_NAME)
    _package.__path__ = [str(_PACKAGE_PATH)]
    _package.__package__ = _PACKAGE_NAME
    _package.__spec__ = importlib.util.spec_from_loader(_PACKAGE_NAME, loader=None, is_package=True)
    _package.__spec__.submodule_search_locations = [str(_PACKAGE_PATH)]
    sys.modules[_PACKAGE_NAME] = _package

runtime = importlib.import_module(f"{_PACKAGE_NAME}.runtime")
model_definition = importlib.import_module(f"{_PACKAGE_NAME}.model_definition")
registration_module = importlib.import_module(f"{_PACKAGE_NAME}.registration")

torch = pytest.importorskip("torch")


def runtime_info(runtime_id: str = "runtime-1", *, metablock_id: str = "model-1", sites=None):
    return SimpleNamespace(
        runtime_instance_id=runtime_id,
        metablock_id=metablock_id,
        graph_revision="revision-a",
        materialization_key="materialization-a",
        bindings=SimpleNamespace(sites=tuple(sites or ())),
    )


def site(logical_id: str = "dense", path: str = "0", group_path=()):
    return SimpleNamespace(
        logical_element_id=logical_id,
        module_path=path,
        group_instance_path=tuple(group_path),
        inspectable=True,
    )


def event_kinds(bus, metablock_id="model-1"):
    return [item["kind"] for item in bus.drain(metablock_id)["events"]]


def test_runtime_registry_is_weak_and_supports_multiple_model_instances():
    bus = runtime.PyTorchEventBus()
    registry = runtime.RuntimeRegistry(bus)
    first = torch.nn.Sequential(torch.nn.Linear(2, 2))
    second = torch.nn.Sequential(torch.nn.Linear(2, 2))
    first_info = runtime_info("runtime-a", sites=(site(),))
    second_info = runtime_info("runtime-b", sites=(site(),))
    registry.register(first, first_info)
    registry.register(second, second_info)

    assert [item.runtime_instance_id for item in registry.list_for_metablock("model-1")] == [
        "runtime-a",
        "runtime-b",
    ]
    reference = weakref.ref(first)
    del first
    gc.collect()

    assert reference() is None
    assert registry.resolve("runtime-a") is None
    assert [item.runtime_instance_id for item in registry.list_for_metablock("model-1")] == [
        "runtime-b"
    ]
    assert "runtime.released" in event_kinds(bus)


def test_activation_subscription_emits_bounded_histogram_and_removes_hook():
    bus = runtime.PyTorchEventBus()
    registry = runtime.RuntimeRegistry(bus)
    model = torch.nn.Sequential(torch.nn.Linear(2, 3))
    registry.register(model, runtime_info(sites=(site(),)))
    handle_count = len(model[0]._forward_hooks)
    registry.subscribe(
        runtime.SubscriptionSpec(
            "activation-sub", "runtime-1", "dense", detail="histogram", rate_hz=1000
        )
    )

    model(torch.tensor([[1.0, 2.0]]))
    events = bus.drain("model-1")["events"]
    activation = next(item for item in events if item["kind"] == "activation.summary")

    assert activation["schema_version"] == 1
    assert activation["runtime_instance_id"] == "runtime-1"
    assert activation["logical_element_id"] == "dense"
    assert activation["module_path"] == "0"
    assert activation["payload"]["value"]["histogram"]["counts"]
    assert len(model[0]._forward_hooks) == handle_count + 1
    assert registry.unsubscribe("runtime-1", "activation-sub")["removed"] is True
    assert len(model[0]._forward_hooks) == handle_count


def test_gradient_subscription_preserves_gradient_and_reports_output_gradient():
    bus = runtime.PyTorchEventBus()
    registry = runtime.RuntimeRegistry(bus)
    model = torch.nn.Sequential(torch.nn.Linear(2, 1, bias=False))
    registry.register(model, runtime_info(sites=(site(),)))
    registry.subscribe(
        runtime.SubscriptionSpec(
            "gradient-sub", "runtime-1", "dense", signal="gradient", rate_hz=1000
        )
    )
    inputs = torch.tensor([[2.0, 3.0]], requires_grad=True)
    output = model(inputs)
    output.sum().backward()

    gradient = next(
        item for item in bus.drain("model-1")["events"] if item["kind"] == "gradient.summary"
    )
    assert gradient["payload"]["value"]["shape"] == [1, 1]
    assert inputs.grad is not None


def test_unsubscribe_before_backward_stops_gradient_event_but_preserves_training():
    bus = runtime.PyTorchEventBus()
    registry = runtime.RuntimeRegistry(bus)
    model = torch.nn.Sequential(torch.nn.Linear(2, 1, bias=False))
    registry.register(model, runtime_info(sites=(site(),)))
    registry.subscribe(
        runtime.SubscriptionSpec(
            "gradient-sub", "runtime-1", "dense", signal="gradient", rate_hz=1000
        )
    )
    inputs = torch.tensor([[2.0, 3.0]], requires_grad=True)
    output = model(inputs)
    registry.unsubscribe("runtime-1", "gradient-sub")
    output.sum().backward()

    events = bus.drain("model-1")["events"]
    assert not any(item["kind"] == "gradient.summary" for item in events)
    assert inputs.grad is not None


def test_gradient_subscription_warns_once_for_non_differentiable_output():
    class Discrete(torch.nn.Module):
        def forward(self, values):
            return values.detach().round()

    bus = runtime.PyTorchEventBus()
    registry = runtime.RuntimeRegistry(bus)
    model = torch.nn.Sequential(Discrete())
    registry.register(model, runtime_info(sites=(site(),)))
    registry.subscribe(
        runtime.SubscriptionSpec(
            "gradient-sub", "runtime-1", "dense", signal="gradient", rate_hz=1000
        )
    )
    model(torch.tensor([1.1]))
    model(torch.tensor([2.2]))

    warnings = [
        item for item in bus.drain("model-1")["events"] if item["kind"] == "runtime.warning"
    ]
    assert len(warnings) == 1
    assert warnings[0]["payload"]["code"] == "gradient_unavailable"


def test_ambiguous_repeated_site_requires_one_repetition():
    bus = runtime.PyTorchEventBus()
    registry = runtime.RuntimeRegistry(bus)
    model = torch.nn.Sequential(torch.nn.Linear(2, 2), torch.nn.Linear(2, 2))
    bindings = (
        site(group_path=(("layers", 0),)),
        site(path="1", group_path=(("layers", 1),)),
    )
    registry.register(model, runtime_info(sites=bindings))
    with pytest.raises(ValueError, match="repetition_index"):
        registry.subscribe(runtime.SubscriptionSpec("sub", "runtime-1", "dense"))

    selected = registry.subscribe(
        runtime.SubscriptionSpec("sub-1", "runtime-1", "dense", repetition_index=1)
    )
    assert selected["module_path"] == "1"


def test_tensor_summaries_handle_empty_integer_histogram_and_bounded_sample():
    empty = runtime.summarize_tensor(torch.empty(0))
    assert empty["finite_fraction"] is None
    assert empty["numel"] == 0

    integer = runtime.summarize_tensor(torch.tensor([1, 2, 3]))
    assert "mean" not in integer and "std" not in integer
    assert integer["min"] == 1 and integer["max"] == 3

    values = torch.arange(20_000, dtype=torch.float32)
    histogram = runtime.summarize_tensor(values, detail="histogram")["histogram"]
    sample = runtime.summarize_tensor(values, detail="sample")["sample"]
    assert len(histogram["counts"]) <= 128
    assert len(sample) <= runtime.MAX_SAMPLE_VALUES


def test_metrics_and_cpu_resource_helper_emit_json_safe_events(monkeypatch):
    bus = runtime.PyTorchEventBus()
    registry = runtime.RuntimeRegistry(bus)
    monkeypatch.setattr(runtime, "RUNTIME_REGISTRY", registry)
    model = torch.nn.Linear(2, 1)
    registry.register(model, runtime_info())

    runtime.emit_metric(model, "train/loss", torch.tensor(0.25), step=8)
    runtime.emit_cuda_resources(model, step=8)
    events = bus.drain("model-1")["events"]

    metric = next(item for item in events if item["kind"] == "metric.scalar")
    assert metric["step"] == 8
    assert metric["payload"] == {"name": "train/loss", "value": 0.25}
    assert not any(item["kind"] == "resource.cuda" for item in events)
    with pytest.raises(ValueError, match="exactly one scalar"):
        runtime.emit_metric(model, "invalid", torch.ones(2))


def test_event_bus_reports_eviction_and_validates_cursor():
    bus = runtime.PyTorchEventBus(max_events=2)
    info = runtime_info()
    for index in range(3):
        bus.emit(kind="metric.scalar", info=info, payload={"index": index})

    drained = bus.drain("model-1", "0")
    assert drained["dropped"] == 1
    assert [item["payload"]["index"] for item in drained["events"]] == [1, 2]
    with pytest.raises(ValueError, match="cursor"):
        bus.drain("model-1", "bad")


def test_explicit_release_removes_hooks_and_emits_release_event():
    bus = runtime.PyTorchEventBus()
    registry = runtime.RuntimeRegistry(bus)
    model = torch.nn.Sequential(torch.nn.Linear(2, 2))
    registry.register(model, runtime_info(sites=(site(),)))
    registry.subscribe(runtime.SubscriptionSpec("sub", "runtime-1", "dense"))
    assert model[0]._forward_hooks

    assert registry.release(model) is True

    assert not model[0]._forward_hooks
    assert registry.resolve("runtime-1") is None
    assert "runtime.released" in event_kinds(bus)
    assert not hasattr(model, "__graphimo_pytorch_runtime__")


def test_model_definition_instantiation_registers_fresh_runtime_instances(monkeypatch):
    compiler = importlib.import_module(f"{_PACKAGE_NAME}.compiler")
    schema = importlib.import_module(f"{_PACKAGE_NAME}.schema")
    state = schema.PyTorchMetaBlockState(
        1,
        "materialized",
        "Materialized",
        (),
        (),
        (
            schema.MethodGraph(
                "forward",
                (schema.MethodInput("values", (2,), "float32"),),
                (schema.MethodOutput("result", schema.Endpoint("dense", "output")),),
                (
                    schema.ElementRecord(
                        "input",
                        "input",
                        "values",
                        {"name": "values", "shape": [2], "dtype": "float32"},
                        {},
                        {},
                    ),
                    schema.ElementRecord(
                        "dense", "linear", "dense", {"in_features": 2, "out_features": 1}, {}, {}
                    ),
                ),
                (
                    schema.EdgeRecord(
                        "edge",
                        schema.Endpoint("input", "output"),
                        schema.Endpoint("dense", "input"),
                    ),
                ),
                (),
            ),
        ),
        {},
    )
    compiled = compiler.compile_model(state)
    definition = model_definition.PyTorchModelDefinition(
        metablock_id=state.metablock_id,
        graph_revision="revision-test",
        materialization_key="materialization-test",
        model_name=state.name,
        generated_source=compiled.generated_source,
        external_parameters={},
        bindings=compiled.bindings,
        _factory=compiled.factory,
        _total_parameters=3,
    )
    bus = runtime.PyTorchEventBus()
    registry = runtime.RuntimeRegistry(bus)
    monkeypatch.setattr(runtime, "RUNTIME_REGISTRY", registry)

    first = definition.instantiate()
    second = definition.instantiate()

    assert (
        first.__graphimo_pytorch_runtime__.runtime_instance_id
        != second.__graphimo_pytorch_runtime__.runtime_instance_id
    )
    assert len(registry.list_for_metablock("materialized")) == 2
    assert [event["kind"] for event in bus.drain("materialized")["events"]] == [
        "runtime.created",
        "runtime.created",
    ]


def test_addon_actions_scope_runtime_listing_subscriptions_and_events(tmp_path):
    from graphimo.metablocks import GraphimoHostAPI

    host = GraphimoHostAPI("project-1", tmp_path / "notebook.py", current_metablock_id="model-1")
    dialect = registration_module.PyTorchModelDialect(host)
    model = torch.nn.Sequential(torch.nn.Linear(2, 2))
    info = runtime_info(sites=(site(),))
    runtime.RUNTIME_REGISTRY.register(model, info)

    listed = dialect.invoke({}, "runtime.list", {}, host)
    assert listed["items"][0]["runtime_instance_id"] == "runtime-1"
    assert listed["items"][0]["bindings"][0]["parameter_count"] == 6
    semantic_state = dialect.create_state({}, host)
    projection = dialect.project(semantic_state, "root", host)
    assert projection["runtime_selector"]["available"] is True
    assert projection["runtime_selector"]["items"][0]["runtime_instance_id"] == "runtime-1"
    subscribed = dialect.invoke(
        {},
        "runtime.subscribe",
        {"runtime_instance_id": "runtime-1", "logical_element_id": "dense"},
        host,
    )
    model(torch.ones(1, 2))
    events = dialect.drain_events("0", 128, host)

    assert subscribed["module_path"] == "0"
    assert any(event["kind"] == "activation.summary" for event in events["events"])
    other_scope = GraphimoHostAPI(
        "project-1", tmp_path / "notebook.py", current_metablock_id="another-model"
    )
    with pytest.raises(ValueError, match="another MetaBlock"):
        dialect.invoke(
            {},
            "runtime.subscribe",
            {"runtime_instance_id": "runtime-1", "logical_element_id": "dense"},
            other_scope,
        )
    runtime.RUNTIME_REGISTRY.release(model)
