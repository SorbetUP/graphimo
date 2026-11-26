from __future__ import annotations

import builtins
import io
import json
import uuid
import zipfile

import pytest


torch = pytest.importorskip("torch")
pytest.importorskip("torchlens")

from graphimo_pytorch.model_definition import RuntimeBindingMap, RuntimeModelInfo, RuntimeSite  # noqa: E402
from graphimo_pytorch.runtime import RUNTIME_REGISTRY  # noqa: E402
from graphimo_pytorch.registration import PyTorchModelDialect  # noqa: E402
from graphimo_pytorch.torchlens_adapter import (  # noqa: E402
    TorchLensIntervention,
    TorchLensRequest,
    _normalize_inputs,
    inspect_with_torchlens,
    save_inspection_artifact,
)


class _ScalarModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.projection = torch.nn.Linear(2, 2)
        self.activation = torch.nn.ReLU()

    def forward(self, value):
        return self.activation(self.projection(value)).sum()


def _runtime(model=None):
    model = model or _ScalarModel()
    runtime_id = uuid.uuid4().hex
    info = RuntimeModelInfo(
        runtime_instance_id=runtime_id,
        metablock_id="meta-test",
        graph_revision="revision-test",
        materialization_key="materialization-test",
        bindings=RuntimeBindingMap(
            (
                RuntimeSite("projection-id", "projection"),
                RuntimeSite("activation-id", "activation"),
            )
        ),
    )
    RUNTIME_REGISTRY.register(model, info)
    return model, runtime_id


def test_workbench_fixture_dtype_survives_json_integer_normalization():
    positional, keyword = _normalize_inputs({"$inputs": [[0, 1, 2]], "$dtype": "float32"})
    assert not keyword
    assert positional[0].dtype == torch.float32


def test_inspects_selected_module_and_captures_gradient_from_explicit_scalar_output():
    model, runtime_id = _runtime()
    request = TorchLensRequest(runtime_id, "activation-id", capture_gradients=True)

    result = inspect_with_torchlens(request, [[1.0, 2.0]])

    assert result.module_path == "activation"
    assert result.selected_activation["shape"] == [1, 2]
    assert result.gradients["available"] is True
    assert result.gradients["shape"] == [1, 2]
    assert result.operations
    assert result.trace["operation_count"] > 0
    assert result.non_finite["selected_activation_count"] == 0
    assert model.training is True


@pytest.mark.parametrize(
    ("intervention", "expected"),
    [
        (TorchLensIntervention("zero_ablation"), 0.0),
        (TorchLensIntervention("scale", 0.25), None),
    ],
)
def test_interventions_are_explicit_experiments_and_do_not_mutate_live_model(
    intervention, expected
):
    model, runtime_id = _runtime()
    with torch.no_grad():
        model.projection.weight.fill_(1.0)
        model.projection.bias.zero_()
    model.eval()
    inputs = [[2.0, 3.0]]
    original_parameters = [parameter.detach().clone() for parameter in model.parameters()]
    baseline = model(torch.tensor(inputs)).detach()
    request = TorchLensRequest(runtime_id, "activation-id", intervention=intervention)

    result = inspect_with_torchlens(request, inputs)

    assert result.experimental is True
    assert result.intervention["kind"] == intervention.kind
    activation = result.selected_activation
    if expected is not None:
        assert activation["maximum"] == expected
    else:
        assert activation["maximum"] == pytest.approx(1.25)
        assert activation["maximum"] < float(baseline.max())
    assert model.training is False
    assert all(
        torch.equal(before, after)
        for before, after in zip(original_parameters, model.parameters(), strict=True)
    )


def test_save_inspection_exports_trace_and_report_only_when_requested():
    _model, runtime_id = _runtime()

    class Store:
        def __init__(self):
            self.payload = None

        def put_bytes(self, payload, *, kind):
            assert kind == "pytorch-torchlens-trace"
            self.payload = payload
            return type("Ref", (), {"id": "artifact-test"})()

    store = Store()
    result = save_inspection_artifact(
        TorchLensRequest(runtime_id, "activation-id"),
        [[1.0, 2.0]],
        artifact_store=store,
    )

    assert result.artifact_ref == {"id": "artifact-test", "kind": "pytorch-torchlens-trace"}
    with zipfile.ZipFile(io.BytesIO(store.payload)) as archive:
        names = archive.namelist()
        assert "report.json" in names
        assert "artifact_metadata.json" in names
        metadata = json.loads(archive.read("artifact_metadata.json"))
        assert metadata["metablock_id"] == "meta-test"
        assert metadata["materialization_key"] == "materialization-test"
        assert "trace/ops.json" in names
        assert "trace/tensors.pt" in names
        trace_tensors = torch.load(io.BytesIO(archive.read("trace/tensors.pt")), weights_only=True)
        assert len(trace_tensors["activations"]) > 1


def test_workbench_action_runs_the_same_scoped_application_dialect():
    model, runtime_id = _runtime()
    context = type(
        "Host",
        (),
        {"current_metablock_id": "meta-test", "artifact_store": None},
    )()
    dialect = PyTorchModelDialect(context)

    result = dialect.invoke(
        {},
        "torchlens.inspect",
        {
            "runtime_instance_id": runtime_id,
            "logical_element_id": "activation-id",
            "inputs": [[1.0, 2.0]],
        },
        context,
    )

    assert result["module_path"] == "activation"
    assert result["selected_activation"]["available"] is True
    assert model.training is True

    context.current_metablock_id = "another-meta"
    with pytest.raises(ValueError, match="another MetaBlock"):
        dialect.invoke(
            {},
            "torchlens.inspect",
            {
                "runtime_instance_id": runtime_id,
                "logical_element_id": "activation-id",
                "inputs": [[1.0, 2.0]],
            },
            context,
        )


def test_broken_or_missing_torchlens_returns_a_clear_diagnostic(monkeypatch):
    model, runtime_id = _runtime()
    request = TorchLensRequest(runtime_id, "activation-id")
    original_import = builtins.__import__

    def without_torchlens(name, *args, **kwargs):
        if name == "torchlens":
            raise ImportError("deliberately unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_torchlens)
    with pytest.raises(RuntimeError, match="TorchLens is unavailable or broken"):
        inspect_with_torchlens(request, [[1.0, 2.0]])
    assert model.training is True
