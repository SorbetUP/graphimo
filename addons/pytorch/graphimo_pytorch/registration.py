"""Graphimo MetaBlock registration backed by the semantic PyTorch graph."""

from __future__ import annotations

import uuid
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
    GroupDefinition,
    GroupInstance,
    MethodGraph,
    MethodInput,
    MethodOutput,
    ParameterBinding,
    PyTorchMetaBlockState,
    graph_revision,
    state_from_dict,
    state_to_dict,
)
from .workbench_projection import project_model


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
        projection = project_model(semantic, scope)
        projection["graph_revision"] = graph_revision(semantic)
        from .runtime import RUNTIME_REGISTRY, _model_device

        metablock_id = context.current_metablock_id
        runtimes = RUNTIME_REGISTRY.list_for_metablock(metablock_id) if metablock_id else []
        projection["runtime_selector"] = {
            "available": bool(runtimes),
            "items": [
                {
                    "runtime_instance_id": item.runtime_instance_id,
                    "graph_revision": item.graph_revision,
                    "materialization_key": item.materialization_key,
                    "device": _model_device(model),
                }
                for item in runtimes
                if (model := RUNTIME_REGISTRY.resolve(item.runtime_instance_id)) is not None
            ],
            "reason": None if runtimes else "No active runtime is registered in this process.",
        }
        return projection

    def apply(self, state: Any, intent: dict[str, Any], context: GraphimoHostAPI) -> dict[str, Any]:
        semantic = state_from_dict(state)
        if intent.get("type") == "set_model_parameter":
            return _set_model_parameter(semantic, intent)
        return state_to_dict(_apply_graph_intent(semantic, intent))

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
        from .runtime import RUNTIME_REGISTRY, SubscriptionSpec, _model_device

        if action == "parameter_summary":
            semantic = state_from_dict(state)
            values = {
                item.id: args.get(item.id, item.default) for item in semantic.external_parameters
            }
            parameters = _count_parameters(semantic, values)
            return {"total_parameters": parameters, "trainable_parameters": parameters}
        if action == "runtime.list":
            metablock_id = context.current_metablock_id
            if not metablock_id:
                raise ValueError("runtime listing requires a scoped MetaBlock")
            return {
                "items": [
                    {
                        "runtime_instance_id": info.runtime_instance_id,
                        "metablock_id": info.metablock_id,
                        "graph_revision": info.graph_revision,
                        "materialization_key": info.materialization_key,
                        "device": _model_device(model),
                        "bindings": [
                            {
                                "logical_element_id": site.logical_element_id,
                                "module_path": site.module_path,
                                "parameter_count": _module_parameter_count(model, site.module_path),
                                "group_instance_path": [
                                    {
                                        "group_instance_id": item.group_instance_id,
                                        "repetition_index": item.repetition_index,
                                    }
                                    for item in site.group_instance_path
                                ],
                                "inspectable": site.inspectable,
                            }
                            for site in info.bindings.sites
                        ],
                    }
                    for info in RUNTIME_REGISTRY.list_for_metablock(metablock_id)
                    if (model := RUNTIME_REGISTRY.resolve(info.runtime_instance_id)) is not None
                ]
            }
        if action == "runtime.subscribe":
            _require_scoped_runtime(RUNTIME_REGISTRY, args.get("runtime_instance_id"), context)
            subscription = SubscriptionSpec(
                subscription_id=args.get("subscription_id") or uuid.uuid4().hex,
                runtime_instance_id=args.get("runtime_instance_id"),
                logical_element_id=args.get("logical_element_id"),
                repetition_index=args.get("repetition_index"),
                signal=args.get("signal", "activation"),
                rate_hz=args.get("rate_hz", 1.0),
                detail=args.get("detail", "summary"),
            )
            return RUNTIME_REGISTRY.subscribe(subscription)
        if action == "runtime.unsubscribe":
            _require_scoped_runtime(RUNTIME_REGISTRY, args.get("runtime_instance_id"), context)
            return RUNTIME_REGISTRY.unsubscribe(
                args.get("runtime_instance_id"), args.get("subscription_id")
            )
        if action == "runtime.release":
            _require_scoped_runtime(RUNTIME_REGISTRY, args.get("runtime_instance_id"), context)
            model = RUNTIME_REGISTRY.resolve(args.get("runtime_instance_id"))
            if model is None:
                return {"released": False}
            return {"released": RUNTIME_REGISTRY.release(model)}
        if action == "checkpoint.inspect":
            artifact_id = args.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise ValueError("artifact_id must be non-empty text")
            from .checkpoint import inspect_checkpoint

            return inspect_checkpoint(artifact_id)
        if action == "checkpoint.preview_migration":
            artifact_id = args.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise ValueError("artifact_id must be non-empty text")
            from .checkpoint import preview_checkpoint_migration
            from .model_definition import definition_from_state

            definition = definition_from_state(
                state_from_dict(state), external_values=args.get("external_values", {})
            )
            report = preview_checkpoint_migration(definition, artifact_id)
            return {
                "graph_revision": definition.graph_revision,
                "materialization_key": definition.materialization_key,
                "report": _migration_report_json(report),
            }
        raise ValueError(f"unsupported PyTorch MetaBlock action: {action}")

    def drain_events(
        self, cursor: str | None, limit: int, context: GraphimoHostAPI
    ) -> dict[str, Any]:
        from .runtime import RUNTIME_REGISTRY

        metablock_id = context.current_metablock_id
        if not metablock_id:
            return {"cursor": cursor or "0", "events": [], "dropped": 0}
        return RUNTIME_REGISTRY.event_bus.drain(metablock_id, cursor, limit)


def _set_model_parameter(semantic: PyTorchMetaBlockState, intent: dict[str, Any]) -> dict[str, Any]:
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


def _apply_graph_intent(
    semantic: PyTorchMetaBlockState, intent: dict[str, Any]
) -> PyTorchMetaBlockState:
    scope = intent.get("scope", "forward")
    method_name = "forward" if scope == "root" else str(scope).removeprefix("method:")
    method_index = next(
        (index for index, item in enumerate(semantic.entrypoints) if item.name == method_name), None
    )
    group_id = str(scope).removeprefix("group:")
    group_index = next(
        (index for index, item in enumerate(semantic.groups) if item.id == group_id), None
    )
    if method_index is not None:
        graph = semantic.entrypoints[method_index]
        updated = _edit_graph(graph, intent)
        methods = list(semantic.entrypoints)
        methods[method_index] = updated
        return replace(semantic, entrypoints=tuple(methods))
    if group_index is not None:
        graph = semantic.groups[group_index]
        updated = _edit_graph(graph, intent)
        groups = list(semantic.groups)
        groups[group_index] = updated
        return replace(semantic, groups=tuple(groups))
    raise ValueError(f"unknown PyTorch graph scope {scope!r}")


def _edit_graph(graph: MethodGraph | GroupDefinition, intent: dict[str, Any]):
    intent_type = intent.get("type")
    if intent_type == "add_element":
        kind = intent.get("kind")
        descriptor = ELEMENT_REGISTRY.get(kind) if isinstance(kind, str) else None
        if descriptor is None or kind == "group_instance":
            raise ValueError(f"unknown PyTorch element kind {kind!r}")
        identity = intent.get("id") or uuid.uuid4().hex[:12]
        name = intent.get("name") or f"{kind}_{identity[:6]}"
        if any(item.id == identity for item in graph.elements) or any(
            item.name == name for item in (*graph.elements, *graph.group_instances)
        ):
            raise ValueError("element id and name must be unique within the scope")
        params = {
            item.name: list(item.default) if isinstance(item.default, tuple) else item.default
            for item in descriptor.parameters
        }
        if kind == "input":
            params["name"] = name
        element = ElementRecord(identity, kind, name, params, {}, {})
        return replace(graph, elements=(*graph.elements, element))

    if intent_type in {"delete_element", "rename_element", "set_element_parameter"}:
        identity = intent.get("element_id")
        element = next((item for item in graph.elements if item.id == identity), None)
        if element is None:
            raise ValueError(f"unknown PyTorch element {identity!r}")
        if intent_type == "delete_element":
            updates: dict[str, Any] = {
                "elements": tuple(item for item in graph.elements if item.id != identity),
                "edges": tuple(
                    item
                    for item in graph.edges
                    if item.source.element_id != identity and item.target.element_id != identity
                ),
            }
            if isinstance(graph, MethodGraph):
                updates["outputs"] = tuple(
                    item for item in graph.outputs if item.endpoint.element_id != identity
                )
            else:
                updates["inputs"] = tuple(
                    item for item in graph.inputs if item.endpoint.element_id != identity
                )
                updates["outputs"] = tuple(
                    item for item in graph.outputs if item.endpoint.element_id != identity
                )
            return replace(graph, **updates)
        if intent_type == "rename_element":
            name = intent.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("element name must be non-empty text")
            if any(
                item.id != identity and item.name == name.strip()
                for item in (*graph.elements, *graph.group_instances)
            ):
                raise ValueError("element name must be unique within the scope")
            elements = tuple(
                replace(item, name=name.strip()) if item.id == identity else item
                for item in graph.elements
            )
            return replace(graph, elements=elements)
        parameter_name = intent.get("parameter")
        descriptor = ELEMENT_REGISTRY.get(element.kind)
        spec = (
            next((item for item in descriptor.parameters if item.name == parameter_name), None)
            if descriptor
            else None
        )
        if spec is None:
            raise ValueError(f"unknown {element.kind} parameter {parameter_name!r}")
        params = {**element.params, parameter_name: intent.get("value")}
        diagnostics = descriptor.validate_params(params, {})
        errors = [item for item in diagnostics if item.severity == "error"]
        if errors:
            raise ValueError(errors[0].message)
        elements = tuple(
            replace(item, params=params) if item.id == identity else item for item in graph.elements
        )
        return replace(graph, elements=elements)

    if intent_type in {"connect", "disconnect"}:
        if intent_type == "disconnect":
            edge_id = intent.get("edge_id")
            edges = tuple(item for item in graph.edges if item.id != edge_id)
            if len(edges) == len(graph.edges):
                raise ValueError(f"unknown PyTorch edge {edge_id!r}")
            return replace(graph, edges=edges)
        edge_id = intent.get("edge_id") or uuid.uuid4().hex[:12]
        if any(item.id == edge_id for item in graph.edges):
            raise ValueError("edge id must be unique within the scope")
        source = intent.get("source")
        target = intent.get("target")
        if not isinstance(source, dict) or not isinstance(target, dict):
            raise ValueError("connect requires source and target endpoint objects")
        edge = EdgeRecord(
            edge_id,
            Endpoint(source.get("element_id"), source.get("port")),
            Endpoint(target.get("element_id"), target.get("port")),
        )
        if any(item.source == edge.source and item.target == edge.target for item in graph.edges):
            return graph
        return replace(graph, edges=(*graph.edges, edge))

    if intent_type in {"add_group_instance", "set_group_repeat", "delete_group_instance"}:
        identity = intent.get("instance_id")
        if intent_type == "add_group_instance":
            group_id = intent.get("group_id")
            name = intent.get("name") or f"group_{uuid.uuid4().hex[:6]}"
            identity = identity or uuid.uuid4().hex[:12]
            if any(
                item.id == identity or item.name == name
                for item in (*graph.elements, *graph.group_instances)
            ):
                raise ValueError("Group instance id and name must be unique within the scope")
            instance = GroupInstance(identity, group_id, name, intent.get("repeat", 1), {})
            return replace(graph, group_instances=(*graph.group_instances, instance))
        instance = next((item for item in graph.group_instances if item.id == identity), None)
        if instance is None:
            raise ValueError(f"unknown PyTorch Group instance {identity!r}")
        if intent_type == "set_group_repeat":
            repeat = intent.get("repeat")
            updated = replace(instance, repeat=repeat)
            instances = tuple(
                updated if item.id == identity else item for item in graph.group_instances
            )
            return replace(graph, group_instances=instances)
        updates = {
            "group_instances": tuple(item for item in graph.group_instances if item.id != identity),
            "edges": tuple(
                edge
                for edge in graph.edges
                if edge.source.element_id != identity and edge.target.element_id != identity
            ),
        }
        if isinstance(graph, MethodGraph):
            updates["outputs"] = tuple(
                output for output in graph.outputs if output.endpoint.element_id != identity
            )
        else:
            updates["inputs"] = tuple(
                item for item in graph.inputs if item.endpoint.element_id != identity
            )
            updates["outputs"] = tuple(
                item for item in graph.outputs if item.endpoint.element_id != identity
            )
        return replace(graph, **updates)

    raise ValueError(f"unsupported PyTorch MetaBlock edit intent {intent_type!r}")


registration = MetaDialectRegistration(
    dialect_id="pytorch.model",
    addon_version="0.1.0",
    api_version=1,
    factory=PyTorchModelDialect,
)


def _require_scoped_runtime(registry: Any, runtime_id: Any, context: GraphimoHostAPI) -> Any:
    if not isinstance(runtime_id, str) or not runtime_id:
        raise ValueError("runtime_instance_id must be non-empty text")
    info = registry.info(runtime_id)
    if info is None:
        raise ValueError("runtime model is no longer available")
    if info.metablock_id != context.current_metablock_id:
        raise ValueError("runtime instance belongs to another MetaBlock")
    return info


def _module_parameter_count(model: Any, module_path: str) -> int:
    try:
        module = model.get_submodule(module_path)
        return sum(parameter.numel() for parameter in module.parameters())
    except (AttributeError, KeyError):
        return 0


def _migration_report_json(report: Any) -> dict[str, Any]:
    return {
        "copied": list(report.copied),
        "missing_in_source": list(report.missing_in_source),
        "missing_in_target": list(report.missing_in_target),
        "shape_mismatch": [
            {
                "name": item.name,
                "source_shape": list(item.source_shape),
                "target_shape": list(item.target_shape),
            }
            for item in report.shape_mismatch
        ],
    }
