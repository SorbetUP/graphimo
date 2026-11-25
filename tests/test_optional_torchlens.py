from __future__ import annotations

import importlib.util

import pytest
from graphimo_core.pytorch import ModelSpec, build_model, inspect_model
from graphimo_core.vertical_demo import run_vertical_demo


@pytest.mark.skipif(
    importlib.util.find_spec("torchlens") is None,
    reason="install the optional inspection extra to run TorchLens integration",
)
def test_targeted_torchlens_capture_validates_selected_model_module(tmp_path) -> None:
    import torch

    model = build_model(ModelSpec(4, 8, {"a": 1, "b": 1, "c": 1})).eval()
    result = inspect_model(model, torch.randn(3, 4), "backbone.1")
    assert result["module_path"] == "backbone.1"
    assert result["shape"] == [3, 8]
    assert result["operation_count"] > 0
    assert result["validated"] is True

    report = run_vertical_demo(tmp_path / "torchlens-workspace")
    assert report["torchlens_inspection"]["available"] is True
    assert report["torchlens_inspection"]["validated"] is True
