from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from graphimo.metablocks import MetaDialectRegistration
from graphimo_pytorch import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    ExternalParameter,
    GraphimoPytorchError,
    GroupDefinition,
    GroupInstance,
    GroupPort,
    MethodGraph,
    MethodInput,
    MethodOutput,
    ParameterBinding,
    PyTorchMetaBlockState,
    TensorContract,
    canonical_state_json,
    graph_revision,
    load_state,
    save_state,
    state_from_dict,
    state_to_dict,
)
from graphimo_pytorch.registration import registration


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "addons" / "pytorch"


def _sample_state(*, width: int = 32, repeat: int = 2) -> PyTorchMetaBlockState:
    external = ExternalParameter("p-width", "width", "int", width, True)
    elements = (
        ElementRecord("input", "input", "x", {"method_input": "in-x"}),
        ElementRecord(
            "linear",
            "unknown-future-kind",
            "linear_a",
            {"in_features": 16, "out_features": width},
            {"out_features": ParameterBinding("p-width")},
            {"opaque": {"future": [1, "raw"]}},
        ),
        ElementRecord("output", "output", "y", {"method_output": "out-y"}),
    )
    edges = (
        EdgeRecord("edge-in", Endpoint("input", "out"), Endpoint("linear", "x")),
        EdgeRecord("edge-out", Endpoint("linear", "out"), Endpoint("output", "x")),
    )
    group = GroupDefinition(
        "decoder-layer",
        "Decoder Layer",
        (GroupPort("group-in", "x"),),
        (GroupPort("group-out", "y"),),
        (),
        (),
        (GroupInstance("repeat-layer", "decoder-layer", "layer", repeat, {"width": width}),),
    )
    forward = MethodGraph(
        "forward",
        (MethodInput("in-x", "x", TensorContract(("batch", 16), "float32", "features")),),
        (MethodOutput("out-y", "y"),),
        elements,
        edges,
        (GroupInstance("use-decoder", "decoder-layer", "layers", repeat, {}),),
    )
    return PyTorchMetaBlockState(
        1, "model-7ab2c1f0", "TinyModel", (external,), (group,), (forward,), {"seed": 7}
    )


def test_p1_schema_roundtrip_keeps_unknown_element_kind_and_nested_group_state(
    tmp_path: Path,
) -> None:
    state = _sample_state()
    path = tmp_path / ".graphimo" / "metablocks" / "model-7ab2c1f0.pytorch.json"

    save_state(path, state)
    loaded = load_state(path)

    assert loaded == state
    assert loaded.entrypoints[0].elements[1].kind == "unknown-future-kind"
    assert loaded.entrypoints[0].elements[1].metadata == {"opaque": {"future": [1, "raw"]}}
    assert loaded.groups[0].group_instances[0].repeat == 2
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_canonical_state_and_graph_revision_are_deterministic_and_semantic() -> None:
    first = _sample_state()
    same = _sample_state()
    changed_width = _sample_state(width=64)
    changed_repeat = _sample_state(repeat=3)

    assert canonical_state_json(first) == canonical_state_json(same)
    assert graph_revision(first) == graph_revision(same)
    assert graph_revision(first) != graph_revision(changed_width)
    assert graph_revision(first) != graph_revision(changed_repeat)

    reordered = state_to_dict(first)
    reordered["metadata"] = {"seed": 7}
    reordered["entrypoints"][0]["elements"][1]["params"] = {
        "out_features": 32,
        "in_features": 16,
    }
    equivalent = state_from_dict(reordered)
    assert graph_revision(equivalent) == graph_revision(first)


def test_unknown_schema_version_fails_without_modifying_file(tmp_path: Path) -> None:
    path = tmp_path / "future.json"
    original = '{"schema_version":99,"metablock_id":"m","name":"future"}\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(GraphimoPytorchError) as error:
        load_state(path)

    assert error.value.code == "pytorch_state_schema_unsupported"
    assert path.read_text(encoding="utf-8") == original


def test_registration_matches_core_entry_point_contract() -> None:
    assert isinstance(registration, MetaDialectRegistration)
    assert registration.dialect_id == "pytorch.model"
    assert registration.api_version == 1
    assert registration.addon_version == "0.1.0"
    assert callable(registration.factory)


def test_repeat_type_and_non_json_semantics_are_rejected() -> None:
    with pytest.raises(GraphimoPytorchError) as error:
        GroupInstance("bad", "g", "g", 0, {})
    assert error.value.code == "pytorch_group_repeat_invalid"

    malformed = state_to_dict(_sample_state())
    malformed["entrypoints"][0]["elements"][0]["params"] = {"bad": float("nan")}
    with pytest.raises(GraphimoPytorchError) as error:
        state_from_dict(malformed)
    assert error.value.code == "pytorch_state_invalid"


def test_registration_import_does_not_import_torch() -> None:
    guard = "\n".join(
        (
            "import builtins",
            "original = builtins.__import__",
            "def guarded(name, *args, **kwargs):",
            "    if name.split('.')[0] in {'torch', 'torchlens'}: raise AssertionError(name)",
            "    return original(name, *args, **kwargs)",
            "builtins.__import__ = guarded",
            "import graphimo_pytorch.registration",
            "print('isolated')",
        )
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'core'}:{ADDON}"
    result = subprocess.run(
        [sys.executable, "-c", guard],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated"


def test_addon_wheel_declares_entry_point_and_contains_schema(tmp_path: Path) -> None:
    source = tmp_path / "addon-source"
    shutil.copytree(
        ADDON,
        source,
        ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__", "*.pyc"),
    )
    output = tmp_path / "wheelhouse"
    output.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(output),
            str(source),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    wheel = next(output.glob("graphimo_pytorch-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        files = set(archive.namelist())
        metadata = archive.read(
            next(name for name in files if name.endswith("entry_points.txt"))
        ).decode()
    assert "graphimo_pytorch/schema.py" in files
    assert "graphimo_pytorch/templates.py" in files
    assert "graphimo.metablocks" in metadata
    assert "graphimo_pytorch.registration:registration" in metadata
