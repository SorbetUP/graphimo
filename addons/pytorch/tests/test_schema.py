from __future__ import annotations

import json
import importlib.util
import sys
from pathlib import Path

import pytest

_SCHEMA_PATH = Path(__file__).parents[1] / "graphimo_pytorch" / "schema.py"
_SCHEMA_SPEC = importlib.util.spec_from_file_location("graphimo_pytorch_schema_test", _SCHEMA_PATH)
assert _SCHEMA_SPEC is not None and _SCHEMA_SPEC.loader is not None
_SCHEMA_MODULE = importlib.util.module_from_spec(_SCHEMA_SPEC)
sys.modules[_SCHEMA_SPEC.name] = _SCHEMA_MODULE
_SCHEMA_SPEC.loader.exec_module(_SCHEMA_MODULE)
ElementRecord = _SCHEMA_MODULE.ElementRecord
Endpoint = _SCHEMA_MODULE.Endpoint
ExternalParameter = _SCHEMA_MODULE.ExternalParameter
MethodGraph = _SCHEMA_MODULE.MethodGraph
MethodInput = _SCHEMA_MODULE.MethodInput
MethodOutput = _SCHEMA_MODULE.MethodOutput
PyTorchMetaBlockState = _SCHEMA_MODULE.PyTorchMetaBlockState
PyTorchSchemaError = _SCHEMA_MODULE.PyTorchSchemaError
canonical_state_json = _SCHEMA_MODULE.canonical_state_json
graph_revision = _SCHEMA_MODULE.graph_revision
load_state = _SCHEMA_MODULE.load_state
save_state = _SCHEMA_MODULE.save_state
state_from_dict = _SCHEMA_MODULE.state_from_dict


def state(*, metadata=None, hidden_size: int = 16, element_metadata=None) -> PyTorchMetaBlockState:
    layer = ElementRecord(
        id="layer-1",
        kind="future.linear.v2",  # Unknown kinds must survive for newer addon versions.
        name="projection",
        params={"out_features": hidden_size},
        parameter_bindings={},
        metadata=element_metadata or {},
    )
    forward = MethodGraph(
        name="forward",
        inputs=(MethodInput("input", ("batch", 8), "float32"),),
        outputs=(MethodOutput("output", Endpoint("layer-1", "output")),),
        elements=(layer,),
        edges=(),
        group_instances=(),
    )
    return PyTorchMetaBlockState(1, "model-1", "Tiny model", (), (), (forward,), metadata or {})


def test_schema_round_trip_preserves_unknown_element_kind(tmp_path):
    original = state()
    path = tmp_path / "model.pytorch.json"

    save_state(path, original)
    restored = load_state(path)

    assert restored == original
    assert restored.entrypoints[0].elements[0].kind == "future.linear.v2"
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_canonical_revision_is_deterministic_and_excludes_layout_metadata():
    first = state(
        metadata={"purpose": "classifier", "layout": {"x": 12, "y": 18}},
        element_metadata={"renderer": {"selected": True}, "semantic_tag": "projection"},
    )
    moved = state(
        metadata={"purpose": "classifier", "layout": {"x": 800, "y": 900}},
        element_metadata={"renderer": {"selected": False}, "semantic_tag": "projection"},
    )

    assert graph_revision(first) == graph_revision(moved)
    encoded = json.loads(canonical_state_json(first))
    assert encoded["metadata"] == {"purpose": "classifier"}
    assert encoded["entrypoints"][0]["elements"][0]["metadata"] == {"semantic_tag": "projection"}
    assert graph_revision(state(hidden_size=16)) != graph_revision(state(hidden_size=32))
    assert canonical_state_json(state()) == canonical_state_json(state())


def test_unknown_schema_version_is_rejected_without_rewriting_source_file(tmp_path):
    path = tmp_path / "model.pytorch.json"
    original = '{"schema_version": 99, "metablock_id": "m"}\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(PyTorchSchemaError) as error:
        load_state(path)

    assert error.value.code == "pytorch_state_schema_unsupported"
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(name=""),
        lambda value: value.update(entrypoints=[]),
        lambda value: value.update(schema_version=2),
        lambda value: value["entrypoints"][0].update(name=""),
    ],
)
def test_invalid_state_shapes_have_stable_diagnostic_code(mutate):
    # Serialize from dataclasses first, then mutate the public JSON representation.
    value = json.loads(canonical_state_json(state()))
    mutate(value)

    with pytest.raises(PyTorchSchemaError) as error:
        state_from_dict(value)

    assert error.value.code in {"pytorch_state_invalid", "pytorch_state_schema_unsupported"}


def test_schema_rejects_invalid_parameter_defaults_and_shape_dimensions():
    with pytest.raises(PyTorchSchemaError, match="default"):
        ExternalParameter("p", "width", "int", True, True)
    with pytest.raises(PyTorchSchemaError, match="shape"):
        MethodInput("input", (0, 8), "float32")


def test_save_is_human_readable_and_atomic_file_contains_utf8(tmp_path):
    path = tmp_path / "model.pytorch.json"
    save_state(path, state())
    contents = path.read_text(encoding="utf-8")

    assert contents.endswith("\n")
    assert '\n  "entrypoints"' in contents
    assert not list(tmp_path.glob(".model.pytorch.json.*"))


def test_save_failure_keeps_previous_file_and_cleans_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "model.pytorch.json"
    path.write_text("old state\n", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(_SCHEMA_MODULE.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        save_state(path, state())

    assert path.read_text(encoding="utf-8") == "old state\n"
    assert not list(tmp_path.glob(".model.pytorch.json.*"))
