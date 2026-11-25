from __future__ import annotations

import ast
import json

import pytest
import torch

from graphimo.application import GraphimoApplication
from graphimo.metablocks import MaterializationSpec, MetaBlockContext, MetaBlockDialect
from graphimo.artifacts import LocalArtifactStore
from graphimo_pytorch import (
    PyTorchMetaBlockDialect,
    materialize_definition,
    mlp_template,
    state_to_dict,
)
from graphimo_pytorch.diagnostics import GraphimoPytorchError
from graphimo_pytorch.runtime import release_runtime_model, runtime_registry
from graphimo_pytorch.serialization import save_state
from graphimo_pytorch.compiler import compile_model


def _context(tmp_path, metablock_id="model-u3"):
    events = []
    state_path = tmp_path / f"{metablock_id}.pytorch.json"
    return MetaBlockContext(
        project_id="fixture.py",
        project_path="/fixture.py",
        sidecar_path=state_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        scope_id=metablock_id,
        navigation=({"id": "root", "title": "Fixture"},),
        _event_sink=events.append,
        _event_drain=lambda cursor, limit: {"cursor": cursor, "events": [], "dropped": 0},
        _sidecar_load=lambda: None,
        _sidecar_save=lambda value: value,
        _materialize=lambda cell_id, source, result_var: (cell_id, source, result_var),
        _diagnostic_sink=lambda code, message: None,
    )


def test_pytorch_dialect_implements_generic_handler_and_projects_renderer_safe_data(tmp_path):
    context = _context(tmp_path)
    dialect = PyTorchMetaBlockDialect(context)
    assert isinstance(dialect, MetaBlockDialect)

    state = dialect.create_state(
        {
            "metablock_id": "model-u3",
            "template": "mlp",
            "input_size": 8,
            "hidden": 4,
            "output_size": 2,
        },
        context,
    )
    catalog = dialect.catalog(state, {"id": "forward"}, context)
    projected = dialect.project(state, {"id": "forward"}, context)

    assert catalog["dialect_id"] == "pytorch.model"
    assert any(item["kind"] == "linear" for item in catalog["items"])
    assert projected["summary"]["metablock_id"] == "model-u3"
    assert projected["cards"]
    json.dumps(projected, allow_nan=False)


def test_pytorch_dialect_adapts_immutable_parameter_edits_and_rejects_unknown_intents(tmp_path):
    context = _context(tmp_path)
    dialect = PyTorchMetaBlockDialect(context)
    state = state_to_dict(
        mlp_template(input_size=8, hidden=4, output_size=2, metablock_id="model-edit")
    )
    changed = dialect.apply(
        state,
        {
            "kind": "promote_parameter",
            "scope_id": "forward",
            "element_id": "linear-a",
            "parameter_name": "out_features",
            "external_parameter_id": "hidden-width",
        },
        context,
    )

    updated = changed["state"]
    assert updated["external_parameters"][0]["id"] == "hidden-width"
    assert updated["external_parameters"][0]["name"] == "out_features"
    assert changed["projection_invalidated"] is True
    with pytest.raises(GraphimoPytorchError, match="Unsupported PyTorch edit intent"):
        dialect.apply(
            updated, {"kind": "create_element", "scope_id": "forward", "element_id": "new"}, context
        )


def test_pytorch_addon_apply_creates_scoped_elements_and_connects_ports(tmp_path):
    context = _context(tmp_path)
    dialect = PyTorchMetaBlockDialect(context)
    state = state_to_dict(
        mlp_template(input_size=8, hidden=4, output_size=2, metablock_id="model-edit-graph")
    )
    added = dialect.apply(
        state,
        {
            "kind": "add_element",
            "scope_id": "forward",
            "element_id": "relu-extra",
            "element_kind": "relu",
            "name": "Extra activation",
            "params": {},
        },
        context,
    )
    connected = dialect.apply(
        added["state"],
        {
            "kind": "connect_elements",
            "scope_id": "forward",
            "edge_id": "input-extra",
            "source_element_id": "input",
            "source_port": "out",
            "target_element_id": "relu-extra",
            "target_port": "x",
        },
        context,
    )
    model = connected["state"]
    assert any(item["id"] == "relu-extra" for item in model["entrypoints"][0]["elements"])
    assert any(item["id"] == "input-extra" for item in model["entrypoints"][0]["edges"])
    assert connected["projection_invalidated"] is True


def test_pytorch_materialization_recreates_definition_inside_consumer_process(tmp_path):
    context = _context(tmp_path, "model-materialize")
    state_obj = mlp_template(
        input_size=8, hidden=4, output_size=2, metablock_id="model-materialize"
    )
    state = state_to_dict(state_obj)
    save_state(context.sidecar_path, state_obj)
    dialect = PyTorchMetaBlockDialect(context)

    spec = dialect.build_materialization(state, {}, context)
    assert isinstance(spec, MaterializationSpec)
    assert spec.output_type == "graphimo_pytorch.PyTorchModelDefinition"
    assert spec.state_revision
    ast.parse(spec.source)

    definition = materialize_definition(state_path=context.sidecar_path)
    assert definition.metablock_id == "model-materialize"
    model = definition.instantiate()
    try:
        assert tuple(model(torch.ones(2, 8)).shape) == (2, 2)
    finally:
        release_runtime_model(model)


def test_generic_application_loads_the_real_pytorch_handler(project, host, monkeypatch):
    host.marimo.snapshot["notebook"]["path"] = str(project.notebook)
    host.marimo.sessions[0]["path"] = str(project.notebook)
    app = GraphimoApplication(host.marimo, project.components, layout_path=project.layout)
    app.open_project(project.notebook)
    monkeypatch.setattr(
        "graphimo.metablock_application.load_metablock_registration",
        lambda _dialect: __import__(
            "graphimo_pytorch.registration", fromlist=["registration"]
        ).registration,
    )

    state = app.create_metablock("pytorch.model", {"template": "mlp", "input_size": 8}, "model-app")
    projection = app.project_metablock("model-app", {"id": "forward"})
    assert state["metablock_id"] == "model-app"
    assert projection["dialect_id"] == "pytorch.model"
    assert app.build_metablock_materialization("model-app", {}).result_var.startswith("g_pytorch_")
    inserted = app.materialize_metablock("model-app", {})
    source = host.marimo.added[-1][1]
    assert inserted["cell_id"] == "inserted-cell-1"
    assert "# Graphimo metablock: pytorch.model" in source
    assert "# Graphimo instance: model-app" in source
    assert "materialize_definition" in source


def test_pytorch_subscriptions_and_torchlens_use_generic_invoke_and_event_methods(tmp_path):
    context = _context(tmp_path, "model-runtime")
    dialect = PyTorchMetaBlockDialect(context)
    state = mlp_template(input_size=8, hidden=4, output_size=2, metablock_id="model-runtime")
    model = compile_model(state).definition.instantiate()
    runtime_id = model.__graphimo_pytorch_runtime__.runtime_instance_id
    try:
        subscription = dialect.invoke(
            state_to_dict(state),
            "subscribe",
            {
                "subscription_id": "subscription-u3",
                "runtime_instance_id": runtime_id,
                "logical_element_id": "linear-a",
                "signal": "activation",
                "detail": "summary",
            },
            context,
        )
        assert subscription["subscription_id"] == "subscription-u3"
        model(torch.ones(1, 8))
        events = dialect.drain_events(0, 20, context)
        assert any(item["kind"] == "activation.summary" for item in events["events"])

        inspected = dialect.invoke(
            state_to_dict(state),
            "torchlens.inspect",
            {
                "runtime_instance_id": runtime_id,
                "logical_element_id": "linear-a",
                "inputs": [[0.5] * 8],
            },
            context,
        )
        assert inspected["runtime_instance_id"] == runtime_id
        assert inspected["logical_element_id"] == "linear-a"
        assert inspected["activation"] is not None
        assert "_trace" not in inspected

        checkpoint = dialect.invoke(
            state_to_dict(state),
            "checkpoint.save",
            {
                "runtime_instance_id": runtime_id,
                "metadata": {"epoch": 1},
            },
            context,
        )
        assert checkpoint["kind"] == "pytorch-checkpoint"
        restored = dialect.invoke(
            state_to_dict(state),
            "checkpoint.load",
            {
                "artifact": checkpoint,
            },
            context,
        )
        restored_model = runtime_registry.resolve(restored["runtime_instance_id"])
        assert restored_model is not None
        assert dialect.invoke(
            state_to_dict(state),
            "runtime.release",
            {"runtime_instance_id": restored["runtime_instance_id"]},
            context,
        ) == {"released": True}
        assert runtime_registry.resolve(restored["runtime_instance_id"]) is None
    finally:
        dialect.invoke(
            state_to_dict(state), "unsubscribe", {"subscription_id": "subscription-u3"}, context
        )
        release_runtime_model(model)
