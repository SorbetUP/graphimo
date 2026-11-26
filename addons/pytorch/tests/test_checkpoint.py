from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

torch = pytest.importorskip("torch")

_PACKAGE_PATH = Path(__file__).parents[1] / "graphimo_pytorch"
_PACKAGE_NAME = "graphimo_pytorch_checkpoint_tests"
if _PACKAGE_NAME not in sys.modules:
    _package = ModuleType(_PACKAGE_NAME)
    _package.__path__ = [str(_PACKAGE_PATH)]
    _package.__package__ = _PACKAGE_NAME
    _package.__spec__ = importlib.util.spec_from_loader(_PACKAGE_NAME, loader=None, is_package=True)
    _package.__spec__.submodule_search_locations = [str(_PACKAGE_PATH)]
    sys.modules[_PACKAGE_NAME] = _package

definitions = importlib.import_module(f"{_PACKAGE_NAME}.model_definition")
runtime = importlib.import_module(f"{_PACKAGE_NAME}.runtime")
checkpoint = importlib.import_module(f"{_PACKAGE_NAME}.checkpoint")
migration = importlib.import_module(f"{_PACKAGE_NAME}.migration")
registration = importlib.import_module(f"{_PACKAGE_NAME}.registration")


def model_definition(*, revision="graph-rev-a", materialization_key="mat-key-a", out_features=2):
    return definitions.PyTorchModelDefinition(
        metablock_id="model-checkpoint",
        graph_revision=revision,
        materialization_key=materialization_key,
        model_name="CheckpointFixture",
        generated_source="class CheckpointFixture: ...\n",
        external_parameters={"hidden_size": out_features},
        bindings=definitions.RuntimeBindingMap(()),
        _factory=lambda: torch.nn.Sequential(torch.nn.Linear(2, out_features)),
        _total_parameters=2 * out_features + out_features,
    )


@pytest.fixture
def isolated_runtime(monkeypatch):
    registry = runtime.RuntimeRegistry(runtime.PyTorchEventBus())
    monkeypatch.setattr(runtime, "RUNTIME_REGISTRY", registry)
    monkeypatch.setattr(checkpoint, "RUNTIME_REGISTRY", registry)
    return registry


def test_checkpoint_saves_artifact_provenance_optimizer_and_scheduler(
    tmp_path, monkeypatch, isolated_runtime
):
    from graphimo import artifacts

    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    definition = model_definition()
    model = definition.instantiate()
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1, momentum=0.9)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1)
    loss = model(torch.ones(2, 2)).sum()
    loss.backward()
    optimizer.step()
    scheduler.step()

    reference = checkpoint.save_checkpoint(
        model,
        optimizer=optimizer,
        scheduler=scheduler,
        metadata={"epoch": 4, "note": "baseline"},
    )
    details = checkpoint.inspect_checkpoint(reference)

    assert reference.kind == "pytorch-checkpoint"
    assert details["artifact_id"] == reference.id
    assert details["metablock_id"] == "model-checkpoint"
    assert details["graph_revision"] == "graph-rev-a"
    assert details["model_name"] == "CheckpointFixture"
    assert details["metadata"] == {"epoch": 4, "note": "baseline"}
    assert details["optimizer_state_present"] is True
    assert details["scheduler_state_present"] is True
    assert artifacts.get(reference.id) == reference

    restored = checkpoint.load_checkpoint(definition, reference)
    for name, value in model.state_dict().items():
        torch.testing.assert_close(restored.state_dict()[name], value)
    assert restored.__graphimo_pytorch_checkpoint__["optimizer_state_present"] is True
    assert restored.__graphimo_pytorch_runtime__.external_parameters == {"hidden_size": 2}
    isolated_runtime.release(model)
    isolated_runtime.release(restored)


def test_strict_revision_mismatch_refuses_before_instantiating_target(
    tmp_path, monkeypatch, isolated_runtime
):
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    source = model_definition()
    model = source.instantiate()
    reference = checkpoint.save_checkpoint(model)
    target = model_definition(revision="graph-rev-b")
    count_before = len(isolated_runtime.list_for_metablock("model-checkpoint"))

    with pytest.raises(checkpoint.CheckpointError) as error:
        checkpoint.load_checkpoint(target, reference, strict=True)

    assert error.value.code == "pytorch_checkpoint_revision_mismatch"
    assert len(isolated_runtime.list_for_metablock("model-checkpoint")) == count_before
    isolated_runtime.release(model)


def test_matching_graph_revision_loads_across_materialization_keys_when_shapes_match(
    tmp_path, monkeypatch, isolated_runtime
):
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    source = model_definition()
    model = source.instantiate()
    with torch.no_grad():
        model[0].weight.fill_(5.0)
    reference = checkpoint.save_checkpoint(model)
    target = model_definition(materialization_key="mat-key-b")

    restored = checkpoint.load_checkpoint(target, reference, strict=True)
    torch.testing.assert_close(restored[0].weight, model[0].weight)
    isolated_runtime.release(restored)

    incompatible = model_definition(materialization_key="mat-key-c", out_features=3)
    target_instances = []

    def target_factory():
        target_model = torch.nn.Sequential(torch.nn.Linear(2, 3))
        with torch.no_grad():
            target_model[0].weight.fill_(99.0)
            target_model[0].bias.fill_(99.0)
        target_instances.append(target_model)
        return target_model

    incompatible = replace(incompatible, _factory=target_factory)
    with pytest.raises(checkpoint.CheckpointError) as error:
        checkpoint.load_checkpoint(incompatible, reference, strict=True)

    assert error.value.code == "pytorch_checkpoint_materialization_mismatch"
    assert torch.all(target_instances[0][0].weight == 99.0)
    assert len(isolated_runtime.list_for_metablock("model-checkpoint")) == 1
    isolated_runtime.release(target_instances[0])
    isolated_runtime.release(model)


def test_non_strict_revision_change_returns_conservative_migration_report(
    tmp_path, monkeypatch, isolated_runtime
):
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    source = model_definition()
    model = source.instantiate()
    with torch.no_grad():
        model[0].weight.fill_(7.0)
        model[0].bias.fill_(3.0)
    reference = checkpoint.save_checkpoint(model)
    target = model_definition(revision="graph-rev-b", out_features=3)

    restored = checkpoint.load_checkpoint(target, reference, strict=False)
    report = checkpoint.get_migration_report(restored)

    assert report is not None
    assert report.copied == ()
    assert {item.name for item in report.shape_mismatch} == {"0.weight", "0.bias"}
    assert restored.__graphimo_pytorch_checkpoint__["artifact_id"] == reference.id
    warning = next(
        event
        for event in isolated_runtime.event_bus.drain("model-checkpoint")["events"]
        if event["payload"].get("code") == "pytorch_checkpoint_migrated"
    )
    assert warning["payload"]["report"]["shape_mismatch"][0]["source_shape"]
    isolated_runtime.release(model)
    isolated_runtime.release(restored)


def test_migrate_state_dict_copies_only_exact_names_and_shapes():
    class WithBuffers(torch.nn.Module):
        def __init__(self, width, buffer_name):
            super().__init__()
            self.layer = torch.nn.Linear(2, width)
            self.register_buffer(buffer_name, torch.tensor([0.0, 0.0]))

    source = WithBuffers(2, "old_buffer")
    target = WithBuffers(3, "new_buffer")
    source_state = {name: value.detach().clone() for name, value in source.state_dict().items()}
    source_state["shared"] = torch.tensor([9.0, 8.0])
    target.register_buffer("shared", torch.zeros(2))

    report = migration.migrate_state_dict(source_state, target)

    assert report.copied == ("shared",)
    assert report.missing_in_source == ("new_buffer",)
    assert report.missing_in_target == ("old_buffer",)
    assert {item.name for item in report.shape_mismatch} == {"layer.weight", "layer.bias"}
    assert target.shared.tolist() == [9.0, 8.0]


@pytest.mark.parametrize("metadata", [{"bad": float("nan")}, {"bad": object()}])
def test_checkpoint_rejects_non_json_user_metadata(
    tmp_path, monkeypatch, isolated_runtime, metadata
):
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    model = model_definition().instantiate()

    with pytest.raises(TypeError, match="finite JSON values"):
        checkpoint.save_checkpoint(model, metadata=metadata)

    isolated_runtime.release(model)


def test_scheduler_state_rejects_arbitrary_python_objects(isolated_runtime):
    class UnsafeScheduler:
        def state_dict(self):
            return {"callable": lambda: None}

    model = model_definition().instantiate()
    with pytest.raises(TypeError, match="unsupported value function"):
        checkpoint.save_checkpoint(model, scheduler=UnsafeScheduler())
    isolated_runtime.release(model)


def test_addon_application_previews_migration_from_the_current_semantic_state(
    tmp_path, monkeypatch, isolated_runtime
):
    from graphimo.metablocks import GraphimoHostAPI

    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    context = GraphimoHostAPI(
        "project-checkpoint", tmp_path / "notebook.py", current_metablock_id="target-model"
    )
    dialect = registration.PyTorchModelDialect(context)
    state = dialect.create_state({}, context)
    target = importlib.import_module(f"{_PACKAGE_NAME}.model_definition").definition_from_state(
        importlib.import_module(f"{_PACKAGE_NAME}.schema").state_from_dict(state)
    )
    source = replace(target, graph_revision="prior-graph-revision", metablock_id="prior-model")
    source_model = source.instantiate()
    reference = checkpoint.save_checkpoint(source_model)

    result = dialect.invoke(
        state,
        "checkpoint.preview_migration",
        {"artifact_id": reference.id},
        context,
    )

    assert result["graph_revision"] == target.graph_revision
    assert result["report"]["copied"]
    assert result["report"]["shape_mismatch"] == []
    assert isolated_runtime.resolve(source_model.__graphimo_pytorch_runtime__.runtime_instance_id)
    assert len(isolated_runtime.list_for_metablock("prior-model")) == 1
    isolated_runtime.release(source_model)


def test_restore_checkpoint_into_new_runtime_returns_json_metadata_only(
    tmp_path, monkeypatch, isolated_runtime
):
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    definition = model_definition()
    model = definition.instantiate()
    with torch.no_grad():
        for parameter in model.parameters():
            parameter.add_(1.0)
    reference = checkpoint.save_checkpoint(model, metadata={"training_steps": 7})

    # Keep restored models alive: the runtime registry only holds weakrefs and
    # the kernel keeps models in cell globals.
    kept: list = []
    original_instantiate = definitions.PyTorchModelDefinition.instantiate

    def keep_alive(self, **kwargs):
        restored_model = original_instantiate(self, **kwargs)
        kept.append(restored_model)
        return restored_model

    monkeypatch.setattr(definitions.PyTorchModelDefinition, "instantiate", keep_alive)
    result = checkpoint.restore_checkpoint(definition, reference)

    assert result["artifact_id"] == reference.id
    assert result["graph_revision"] == "graph-rev-a"
    assert result["revision_match"] is True
    assert result["migrated"] is False
    assert result["migration_report"] is None
    assert result["training_steps"] == 7
    assert isinstance(result["runtime_instance_id"], str)
    # The registry is weak (the kernel keeps models alive via cell globals);
    # keep a strong reference here so the restored runtime stays resolvable.
    assert isolated_runtime.resolve(result["runtime_instance_id"]) is not None
    import json

    json.dumps(result, allow_nan=False)
    assert "model" not in result
    # Weight equality is covered by the existing-runtime restore below, where
    # the test holds a strong reference to the model.


def test_restore_checkpoint_into_existing_runtime_and_training_state(
    tmp_path, monkeypatch, isolated_runtime
):
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    definition = model_definition()
    model = definition.instantiate()
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1, momentum=0.9)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1)
    model(torch.ones(2, 2)).sum().backward()
    optimizer.step()
    scheduler.step()
    reference = checkpoint.save_checkpoint(
        model, optimizer=optimizer, scheduler=scheduler, metadata={"training_steps": 3}
    )
    info = runtime._runtime_info(model)

    # Drift the weights after saving so the restore is observable.
    with torch.no_grad():
        for parameter in model.parameters():
            parameter.add_(5.0)
    fresh_optimizer = torch.optim.SGD(model.parameters(), lr=0.1, momentum=0.9)
    fresh_scheduler = torch.optim.lr_scheduler.StepLR(fresh_optimizer, step_size=1)
    model.__graphimo_pytorch_training_state__ = {
        "optimizer": fresh_optimizer,
        "scheduler": fresh_scheduler,
    }

    result = checkpoint.restore_checkpoint(
        definition,
        reference,
        runtime_instance_id=info.runtime_instance_id,
        restore_training_state=True,
    )

    assert result["runtime_instance_id"] == info.runtime_instance_id
    assert result["optimizer_state_restored"] is True
    assert result["scheduler_state_restored"] is True
    assert result["training_steps"] == 3
    # Weights were restored into the live model, and the optimizer state matches.
    saved = checkpoint.load_checkpoint(definition, reference)
    assert torch.allclose(next(model.parameters()), next(saved.parameters()))
    assert fresh_optimizer.state_dict()["param_groups"] == optimizer.state_dict()["param_groups"]
    assert fresh_scheduler.state_dict() == scheduler.state_dict()


def test_restore_checkpoint_rejects_revision_mismatch_and_unknown_runtime(
    tmp_path, monkeypatch, isolated_runtime
):
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    definition = model_definition()
    model = definition.instantiate()
    reference = checkpoint.save_checkpoint(model)

    other = model_definition(revision="graph-rev-b", materialization_key="mat-key-b")
    with pytest.raises(checkpoint.CheckpointError, match="does not match") as mismatch:
        checkpoint.restore_checkpoint(other, reference)
    assert mismatch.value.code == "pytorch_checkpoint_revision_mismatch"
    with pytest.raises(checkpoint.CheckpointError, match="not registered") as missing:
        checkpoint.restore_checkpoint(definition, reference, runtime_instance_id="nope")
    assert missing.value.code == "pytorch_checkpoint_runtime_missing"


def test_restore_optimizer_state_returns_recorded_step_count(
    tmp_path, monkeypatch, isolated_runtime
):
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    definition = model_definition()
    model = definition.instantiate()
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    model(torch.ones(2, 2)).sum().backward()
    optimizer.step()
    reference = checkpoint.save_checkpoint(
        model, optimizer=optimizer, metadata={"training_steps": 11}
    )
    fresh = torch.optim.SGD(model.parameters(), lr=0.5)
    assert checkpoint.restore_optimizer_state(fresh, reference) == 11
    # The recorded optimizer state (lr=0.1) replaced the constructor hyperparams.
    assert fresh.state_dict()["param_groups"][0]["lr"] == 0.1
    bare = checkpoint.save_checkpoint(model)
    with pytest.raises(checkpoint.CheckpointError, match="no optimizer state") as missing:
        checkpoint.restore_optimizer_state(fresh, bare)
    assert missing.value.code == "pytorch_checkpoint_optimizer_missing"
