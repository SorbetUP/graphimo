from __future__ import annotations

from pathlib import Path

import pytest
import torch

from graphimo.artifacts import configure_default_store, materialize
from graphimo_pytorch import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    ExternalParameter,
    GraphimoPytorchError,
    MethodGraph,
    MethodInput,
    MethodOutput,
    ParameterBinding,
    PyTorchMetaBlockState,
    TensorContract,
    compile_model,
    load_checkpoint,
    save_checkpoint,
)


def _state(*, name: str = "Checkpoint MLP", width: int = 8, external: bool = False) -> PyTorchMetaBlockState:
    external_parameters = (ExternalParameter("width", "width", "int", width, True),) if external else ()
    params = {"in_features": 4} if external else {"in_features": 4, "out_features": width}
    bindings = {"out_features": ParameterBinding("width")} if external else {}
    elements = (
        ElementRecord("input", "input", "input", {"method_input": "x"}),
        ElementRecord("linear", "linear", "projection", params, bindings),
        ElementRecord("output", "output", "output", {"method_output": "y"}),
    )
    method = MethodGraph(
        "forward", (MethodInput("x", "x", TensorContract(("e", 4), "float32")),),
        (MethodOutput("y", "y"),), elements,
        (EdgeRecord("a", Endpoint("input", "out"), Endpoint("linear", "x")),
         EdgeRecord("b", Endpoint("linear", "out"), Endpoint("output", "x"))), (),
    )
    return PyTorchMetaBlockState(1, "checkpoint-model", name, external_parameters, (), (method,), {})


@pytest.fixture
def artifacts(tmp_path: Path):
    return configure_default_store(tmp_path / "artifacts")


def test_p6_saves_exact_state_optimizer_provenance_and_loads_exact_revision(artifacts, tmp_path: Path) -> None:
    definition = compile_model(_state()).definition
    model = definition.instantiate()
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.01)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1)
    model(torch.randn(2, 4)).sum().backward()
    optimizer.step()
    scheduler.step()
    before = {key: value.detach().clone() for key, value in model.state_dict().items()}

    artifact = save_checkpoint(model, optimizer=optimizer, scheduler=scheduler, metadata={"epoch": 3, "tag": "smoke"})
    assert artifact.kind == "pytorch-checkpoint"
    restored = load_checkpoint(definition, artifact, strict=True)
    assert restored.__graphimo_pytorch_runtime__.graph_revision == definition.graph_revision
    assert all(torch.equal(before[key], restored.state_dict()[key]) for key in before)

    checkpoint_file = materialize(artifact, tmp_path / "checkpoint.pt")
    payload = torch.load(checkpoint_file, map_location="cpu", weights_only=True)
    assert payload["optimizer_state_dict"]
    assert payload["scheduler_state_dict"]
    assert payload["metadata"] == {"epoch": 3, "tag": "smoke"}
    assert "model" not in payload
    assert payload["metablock_id"] == definition.metablock_id
    assert payload["materialization_key"] == definition.materialization_key


def test_p6_strict_revision_and_materialization_mismatches_refuse_before_loading(artifacts) -> None:
    definition = compile_model(_state()).definition
    artifact = save_checkpoint(definition.instantiate())
    changed_revision = compile_model(_state(width=9)).definition
    with pytest.raises(GraphimoPytorchError) as error:
        load_checkpoint(changed_revision, artifact, strict=True)
    assert error.value.code == "pytorch_checkpoint_revision_mismatch"

    external_state = _state(external=True)
    external_default = compile_model(external_state).definition
    artifact = save_checkpoint(external_default.instantiate())
    external_changed = compile_model(external_state, {"width": 12}).definition
    assert external_default.graph_revision == external_changed.graph_revision
    with pytest.raises(GraphimoPytorchError) as error:
        load_checkpoint(external_changed, artifact, strict=True)
    assert error.value.code == "pytorch_checkpoint_materialization_mismatch"


def test_p6_non_strict_load_reports_only_exact_name_and_shape_migration(artifacts) -> None:
    original = compile_model(_state()).definition
    artifact = save_checkpoint(original.instantiate())
    renamed = compile_model(_state(name="Checkpoint MLP fork")).definition
    migrated = load_checkpoint(renamed, artifact, strict=False)
    report = migrated.__graphimo_pytorch_migration_report__
    assert set(report.copied) == {"projection.weight", "projection.bias"}
    assert report.shape_mismatch == ()
    assert report.missing_in_source == ()
    assert report.missing_in_target == ()

    wider = compile_model(_state(width=12)).definition
    migrated = load_checkpoint(wider, artifact, strict=False)
    report = migrated.__graphimo_pytorch_migration_report__
    assert {item.name for item in report.shape_mismatch} == {"projection.weight", "projection.bias"}
    assert report.copied == ()


def test_p6_requires_provenance_and_json_metadata(artifacts) -> None:
    plain_model = torch.nn.Linear(2, 2)
    with pytest.raises(GraphimoPytorchError) as error:
        save_checkpoint(plain_model)
    assert error.value.code == "pytorch_runtime_not_registered"

    model = compile_model(_state()).definition.instantiate()
    with pytest.raises(GraphimoPytorchError) as error:
        save_checkpoint(model, metadata={"not_json": object()})
    assert error.value.code == "pytorch_checkpoint_metadata_invalid"

