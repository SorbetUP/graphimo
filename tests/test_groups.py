from __future__ import annotations

import pytest

from graphimo.groups import (
    GroupPort,
    GroupResult,
    build_group_instance_source,
    create_group_file,
    load_group_contract,
    parse_group_instance_source,
    run_group,
)


def _group(tmp_path):
    path = tmp_path / "components" / "bodypart_pipeline.py"
    create_group_file(
        path,
        group_id="bodypart-pipeline",
        name="Bodypart pipeline",
        inputs=[GroupPort("dataset", "dataset", "list")],
        outputs=[GroupPort("model", "model", "str"), GroupPort("metrics", "metrics", "dict")],
    )
    source = path.read_text(encoding="utf-8")
    source = source.replace(
        "# Add normal Graphimo/marimo cells below.\n\n",
        "@app.cell\n"
        "def train(dataset):\n"
        "    model = f'model:{len(dataset)}'\n"
        "    metrics = {'count': len(dataset)}\n"
        "    return model, metrics\n\n",
    )
    path.write_text(source, encoding="utf-8")
    return path


def test_group_contract_file_has_managed_marimo_input_and_output_cells(tmp_path):
    path = _group(tmp_path)

    contract = load_group_contract(path)

    assert contract.id == "bodypart-pipeline"
    assert contract.name == "Bodypart pipeline"
    assert [(port.name, port.definition) for port in contract.inputs] == [("dataset", "dataset")]
    assert [port.name for port in contract.outputs] == ["model", "metrics"]


def test_run_group_returns_opaque_named_outputs_and_isolates_repeated_runs(tmp_path):
    path = _group(tmp_path)

    first = run_group(path, inputs={"dataset": [1, 2, 3]})
    second = run_group(path, inputs={"dataset": [1]})

    assert isinstance(first, GroupResult)
    assert first.model == "model:3"
    assert first["metrics"] == {"count": 3}
    assert second.model == "model:1"
    assert first.model != second.model


def test_run_group_validates_contract_input_and_output_names(tmp_path):
    path = _group(tmp_path)

    with pytest.raises(ValueError, match="Missing required Group input"):
        run_group(path, inputs={})
    with pytest.raises(ValueError, match="Unknown Group input"):
        run_group(path, inputs={"other": 1})
    with pytest.raises(ValueError, match="Unknown Group output"):
        run_group(path, inputs={"dataset": []}, outputs=("unknown",))


def test_group_contract_rejects_drift_in_managed_input_cell(tmp_path):
    path = _group(tmp_path)
    source = path.read_text(encoding="utf-8").replace(
        "    dataset = None", "    dataset = None\n    surprise = 1"
    )
    path.write_text(source, encoding="utf-8")

    with pytest.raises(ValueError, match="input cell definitions do not match"):
        load_group_contract(path)


def test_group_instance_source_keeps_reference_and_named_outputs(tmp_path):
    path = _group(tmp_path)
    contract = load_group_contract(path)

    source, instance = build_group_instance_source(
        contract, {"dataset": "source.data"}, instance_id="ab12cd34"
    )
    parsed = parse_group_instance_source(source)

    assert instance.result_var == "g_Bodypart_pipeline_ab12cd34"
    assert parsed is not None
    assert parsed.group_id == "bodypart-pipeline"
    assert parsed.path == str(path.resolve())
    assert parsed.bindings[0].expression == "source.data"
    assert parsed.outputs == ("model", "metrics")
    assert "g_Bodypart_pipeline_ab12cd34.model" in source or "graphimo.groups.run_group" in source
