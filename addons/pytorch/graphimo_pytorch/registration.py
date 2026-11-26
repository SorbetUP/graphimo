"""Graphimo MetaBlock registration backed by the semantic PyTorch graph."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from graphimo.metablocks import GraphimoHostAPI, MetaDialectRegistration, state_revision

from .model_definition import _count_parameters
from .prototype import ModelSpec
from .registry import ELEMENT_REGISTRY
from .schema import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    ExternalParameter,
    MethodGraph,
    MethodInput,
    MethodOutput,
    ParameterBinding,
    PyTorchMetaBlockState,
    graph_revision,
    state_from_dict,
    state_to_dict,
)
from .shapes import infer_model_shapes


def _head_parameter_id(name: str) -> str:
    return f"head_size__{name}"


def _create_state(request: dict[str, Any], context: GraphimoHostAPI) -> dict[str, Any]:
    metablock_id = context.current_metablock_id
    if not metablock_id:
        raise ValueError("PyTorch MetaBlock creation requires a scoped MetaBlock ID")
    default_spec = {"input_size": 4, "hidden_size": 8, "head_sizes": {"output": 1}}
    validated = ModelSpec(**request.get("model_spec", default_spec))
    external = [
        ExternalParameter("input_size", "input_size", "int", validated.input_size, True),
        ExternalParameter("hidden_size", "hidden_size", "int", validated.hidden_size, True),
    ]
    for name, size in sorted(validated.head_sizes.items()):
        external.append(
            ExternalParameter(_head_parameter_id(name), f"{name}_size", "int", size, True)
        )
    elements = [
        ElementRecord(
            "input",
            "input",
            "input",
            {"name": "input", "shape": ["batch", "input_size"], "dtype": "float32"},
            {},
            {},
        ),
        ElementRecord(
            "backbone",
            "linear",
            "backbone",
            {
                "in_features": validated.input_size,
                "out_features": validated.hidden_size,
                "bias": True,
                "init_std": None,
            },
            {
                "in_features": ParameterBinding("input_size"),
                "out_features": ParameterBinding("hidden_size"),
            },
            {},
        ),
        ElementRecord("activation", "gelu", "activation", {}, {}, {}),
    ]
    edges = [
        EdgeRecord("input_to_backbone", Endpoint("input", "output"), Endpoint("backbone", "input")),
        EdgeRecord(
            "backbone_to_activation",
            Endpoint("backbone", "output"),
            Endpoint("activation", "input"),
        ),
    ]
    outputs: list[MethodOutput] = []
    for name, size in sorted(validated.head_sizes.items()):
        element_id = f"head_{name}"
        elements.append(
            ElementRecord(
                element_id,
                "linear",
                element_id,
                {
                    "in_features": validated.hidden_size,
                    "out_features": size,
                    "bias": True,
                    "init_std": None,
                },
                {
                    "in_features": ParameterBinding("hidden_size"),
                    "out_features": ParameterBinding(_head_parameter_id(name)),
                },
                {},
            )
        )
        edges.append(
            EdgeRecord(
                f"activation_to_{element_id}",
                Endpoint("activation", "output"),
                Endpoint(element_id, "input"),
            )
        )
        outputs.append(MethodOutput(name, Endpoint(element_id, "output")))
    state = PyTorchMetaBlockState(
        schema_version=1,
        metablock_id=metablock_id,
        name=request.get("name", "PyTorch Model"),
        external_parameters=tuple(external),
        groups=(),
        entrypoints=(
            MethodGraph(
                "forward",
                (MethodInput("input", ("batch", "input_size"), "float32"),),
                tuple(outputs),
                tuple(elements),
                tuple(edges),
                (),
            ),
        ),
        metadata={"template": "multihead_mlp"},
    )
    return state_to_dict(state)


class PyTorchModelDialect:
    dialect_id = "pytorch.model"

    def __init__(self, host: GraphimoHostAPI):
        self.host = host

    def create_state(self, request: dict[str, Any], context: GraphimoHostAPI) -> dict[str, Any]:
        return _create_state(request, context)

    def catalog(self, state: Any, scope: str, context: GraphimoHostAPI) -> dict[str, Any]:
        if scope == "root":
            return {"scope": scope, "items": ["pytorch.model"]}
        return {"scope": scope, "items": list(ELEMENT_REGISTRY)}

    def project(self, state: Any, scope: str, context: GraphimoHostAPI) -> dict[str, Any]:
        semantic = state_from_dict(state)
        inferred = infer_model_shapes(semantic)
        method = next((item for item in semantic.entrypoints if item.name == "forward"), None)
        elements = []
        edges = []
        if method is not None:
            for element in method.elements:
                descriptor = ELEMENT_REGISTRY.get(element.kind)
                elements.append(
                    {
                        "id": element.id,
                        "kind": element.kind,
                        "name": element.name,
                        "params": element.params,
                        "inputs": [port.name for port in descriptor.input_ports]
                        if descriptor
                        else [],
                        "outputs": [port.name for port in descriptor.output_ports]
                        if descriptor
                        else [],
                        "output_shape": _shape_payload(
                            inferred.port_shapes.get(f"forward/{element.id}/output")
                        ),
                    }
                )
            edges = [
                {
                    "id": edge.id,
                    "source": {"element_id": edge.source.element_id, "port": edge.source.port},
                    "target": {"element_id": edge.target.element_id, "port": edge.target.port},
                    "shape": _shape_payload(inferred.edge_shapes.get(f"forward/{edge.id}")),
                }
                for edge in method.edges
            ]
        return {
            "scope": scope,
            "name": semantic.name,
            "dialect_id": self.dialect_id,
            "elements": elements,
            "edges": edges,
            "external_parameters": [
                {
                    "id": item.id,
                    "name": item.name,
                    "type": item.type,
                    "default": item.default,
                    "structural": item.structural,
                }
                for item in semantic.external_parameters
            ],
            "diagnostics": [
                {"code": item.code, "message": item.message, "severity": item.severity}
                for item in inferred.diagnostics
            ],
            "generated_source": None,
        }

    def apply(self, state: Any, intent: dict[str, Any], context: GraphimoHostAPI) -> dict[str, Any]:
        if intent.get("type") != "set_model_parameter":
            raise ValueError("unsupported PyTorch MetaBlock edit intent")
        semantic = state_from_dict(state)
        field = intent.get("name")
        value = intent.get("value")
        if field == "name" and isinstance(value, str) and value.strip():
            return state_to_dict(replace(semantic, name=value.strip()))
        for index, parameter in enumerate(semantic.external_parameters):
            if parameter.name == field or parameter.id == field:
                updated = replace(parameter, default=value)
                parameters = list(semantic.external_parameters)
                parameters[index] = updated
                return state_to_dict(replace(semantic, external_parameters=tuple(parameters)))
        raise ValueError(f"unknown PyTorch MetaBlock parameter {field!r}")

    def build_materialization(
        self, state: Any, bindings: dict[str, Any], context: GraphimoHostAPI
    ) -> dict[str, Any]:
        metablock_id = context.current_metablock_id
        if not metablock_id:
            raise ValueError("MetaBlock materialization context has no current MetaBlock ID")
        record = context.read_sidecar(metablock_id)
        if record is None:
            raise FileNotFoundError(f"MetaBlock '{metablock_id}' sidecar is missing")
        semantic = state_from_dict(state)
        revision = graph_revision(semantic)
        if graph_revision(state_from_dict(record.get("state"))) != revision:
            raise ValueError("MetaBlock state changed before materialization")
        state_path = context.sidecar_path(metablock_id, "pytorch.model").resolve()
        source = (
            "from graphimo_pytorch import materialize_definition\n\n"
            f"g_pytorch_{metablock_id} = materialize_definition(\n"
            f"    state_path={str(state_path)!r},\n"
            f"    metablock_id={metablock_id!r},\n"
            f"    revision={revision!r},\n"
            f"    external_values={bindings!r},\n"
            ")\n"
        )
        return {
            "source": source,
            "result_name": f"g_pytorch_{metablock_id}",
            "output_type": "PyTorchModelDefinition",
            "bindings": bindings,
            "revision": state_revision(state),
        }

    def invoke(
        self, state: Any, action: str, args: dict[str, Any], context: GraphimoHostAPI
    ) -> Any:
        if action == "parameter_summary":
            semantic = state_from_dict(state)
            values = {
                item.id: args.get(item.id, item.default) for item in semantic.external_parameters
            }
            parameters = _count_parameters(semantic, values)
            return {"total_parameters": parameters, "trainable_parameters": parameters}
        raise ValueError(f"unsupported PyTorch MetaBlock action: {action}")

    def drain_events(
        self, cursor: str | None, limit: int, context: GraphimoHostAPI
    ) -> dict[str, Any]:
        return {"cursor": cursor or "0", "events": [], "dropped": 0}


def _shape_payload(spec):
    if spec is None:
        return None
    return {
        "shape": [item.expr for item in spec.shape],
        "dtype": spec.dtype,
        "semantic": spec.semantic,
    }


registration = MetaDialectRegistration(
    dialect_id="pytorch.model",
    addon_version="0.1.0",
    api_version=1,
    factory=PyTorchModelDialect,
)
