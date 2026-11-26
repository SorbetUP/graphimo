"""V1 PyTorch import accepts semantic state and rejects arbitrary source."""

from __future__ import annotations

import json

import pytest

from graphimo.application import GraphimoApplication
from graphimo.metablocks import GraphimoHostAPI, MetaBlockAddonManager
from graphimo_pytorch.registration import PyTorchModelDialect, registration
from graphimo_pytorch.schema import PyTorchSchemaError


def test_imports_semantic_state_json_as_a_new_metablock(tmp_path) -> None:
    source_host = GraphimoHostAPI("project", tmp_path / "notebook.py").for_metablock("source")
    dialect = PyTorchModelDialect(source_host)
    source_state = dialect.create_state({"name": "Imported model"}, source_host)
    request = {"semantic_state_json": json.dumps(source_state)}

    imported = dialect.create_state(request, source_host.for_metablock("copy"))

    assert imported["metablock_id"] == "copy"
    assert imported["name"] == "Imported model"
    assert imported["entrypoints"] == source_state["entrypoints"]
    assert imported["external_parameters"] == source_state["external_parameters"]


def test_imports_existing_pytorch_sidecar_envelope(tmp_path) -> None:
    host = GraphimoHostAPI("project", tmp_path / "notebook.py").for_metablock("source")
    dialect = PyTorchModelDialect(host)
    state = dialect.create_state({"name": "Saved model"}, host)
    envelope = {
        "format": 1,
        "metablock_id": "source",
        "dialect_id": "pytorch.model",
        "addon_version": "0.1.0",
        "api_version": 1,
        "state": state,
    }

    imported = dialect.create_state(
        {"semantic_state_json": json.dumps(envelope)}, host.for_metablock("copy")
    )

    assert imported["metablock_id"] == "copy"
    assert imported["name"] == "Saved model"


def test_semantic_import_uses_the_generic_application_create_action(tmp_path) -> None:
    source_host = GraphimoHostAPI("project", tmp_path / "notebook.py").for_metablock("source")
    source_state = PyTorchModelDialect(source_host).create_state(
        {"name": "Application import"}, source_host
    )
    host = GraphimoHostAPI("project", tmp_path / "notebook.py")

    class EntryPoint:
        name = "pytorch.model"
        dist = None

        @staticmethod
        def load():
            return registration

    manager = MetaBlockAddonManager(host, entry_points=lambda: [EntryPoint()])
    application = GraphimoApplication(host, manager)
    created = application.execute(
        "metablock.create",
        {
            "dialect_id": "pytorch.model",
            "metablock_id": "copied",
            "request": {"semantic_state_json": json.dumps(source_state)},
        },
    )

    assert created["metablock_id"] == "copied"
    assert created["projection"]["name"] == "Application import"
    assert host.read_sidecar("copied")["state"]["metablock_id"] == "copied"


@pytest.mark.parametrize(
    "source",
    [
        "class Model(torch.nn.Module): pass",
        '{"layers": [{"type": "Linear"}]}',
    ],
)
def test_arbitrary_python_or_non_graphimo_json_is_explicitly_unsupported(
    tmp_path, source: str
) -> None:
    host = GraphimoHostAPI("project", tmp_path / "notebook.py").for_metablock("copy")

    with pytest.raises(PyTorchSchemaError) as error:
        PyTorchModelDialect(host).create_state({"semantic_state_json": source}, host)

    assert error.value.code == "pytorch_import_unsupported"
    assert str(error.value)


def test_import_rejects_state_over_nested_service_limit(tmp_path) -> None:
    host = GraphimoHostAPI("project", tmp_path / "notebook.py").for_metablock("copy")

    with pytest.raises(PyTorchSchemaError, match="1 MiB import limit") as error:
        PyTorchModelDialect(host).create_state(
            {"semantic_state_json": " " * (1024 * 1024 + 1)}, host
        )

    assert error.value.code == "pytorch_import_unsupported"
