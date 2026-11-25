"""Generic Graphimo MetaBlock adapter for the PyTorch semantic addon."""

from __future__ import annotations

import ast
import hashlib
import math
import threading
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from graphimo.metablocks import MaterializationSpec, MetaBlockContext

from .checkpoints import load_checkpoint, save_checkpoint
from .compiler import compile_model
from .diagnostics import GraphimoPytorchError
from .editing import (
    add_element,
    connect_elements,
    demote_parameter,
    disconnect_edge,
    promote_parameter,
    set_element_parameter,
)
from .inspection import (
    TorchLensIntervention,
    TorchLensRequest,
    inspect_with_torchlens,
    save_torchlens_inspection,
)
from .registry import element_kinds
from .runtime import release_runtime_model, runtime_registry
from .serialization import state_from_dict, state_to_dict
from .shapes import infer_model_shapes
from .telemetry import (
    SubscriptionSpec,
    default_subscription_rate_hz,
    event_queue,
    subscribe,
    unsubscribe,
)
from .templates import (
    LlamaTemplateConfig,
    llama_causal_lm_template,
    llama_decoder_layer_template,
    mlp_template,
    self_attention_template,
)
from .workbench_projection import project_workbench


_TEMPLATES = {
    "mlp": mlp_template,
    "self_attention": self_attention_template,
    "llama_decoder_layer": llama_decoder_layer_template,
    "llama_causal_lm": llama_causal_lm_template,
}
_CATEGORIES = {
    "input": "I/O",
    "output": "I/O",
    "embedding": "Layers",
    "linear": "Layers",
    "rms_norm": "Layers",
    "dropout": "Layers",
    "relu": "Activations",
    "gelu": "Activations",
    "silu": "Activations",
    "softmax": "Activations",
    "reshape": "Tensor transforms",
    "transpose": "Tensor transforms",
    "flatten": "Tensor transforms",
    "unflatten": "Tensor transforms",
    "split_heads": "Tensor transforms",
    "merge_heads": "Tensor transforms",
    "concatenate": "Tensor transforms",
    "add": "Math / attention",
    "multiply": "Math / attention",
    "scale": "Math / attention",
    "matmul": "Math / attention",
    "rope": "Math / attention",
    "causal_mask": "Math / attention",
    "cross_entropy": "Losses",
    "group_instance": "Structure",
}
_MAX_EVENTS_PER_METABLOCK = 2048


class PyTorchMetaBlockDialect:
    """Own PyTorch model state, projection, edits, materialization, and actions."""

    dialect_id = "pytorch.model"

    def __init__(self, context: MetaBlockContext):
        self._context = context
        self._lock = threading.Lock()
        self._events: dict[str, deque[tuple[int, dict[str, Any]]]] = {}
        self._next_event: dict[str, int] = {}
        self._owned_models: dict[str, object] = {}

    def create_state(self, request: dict[str, Any], context: MetaBlockContext) -> dict[str, Any]:
        template_name = request.get("template", "mlp")
        template = _TEMPLATES.get(template_name)
        if template is None:
            raise GraphimoPytorchError(
                "pytorch_template_unknown", f"Unknown PyTorch template {template_name!r}"
            )
        if template_name in {"llama_decoder_layer", "llama_causal_lm"}:
            config_value = request.get("config", {})
            if not isinstance(config_value, dict):
                raise GraphimoPytorchError(
                    "pytorch_template_config_invalid", "config must be an object"
                )
            state = template(
                LlamaTemplateConfig(**config_value), metablock_id=request["metablock_id"]
            )
            return state_to_dict(state)
        options = {
            key: value for key, value in request.items() if key not in {"template", "metablock_id"}
        }
        state = template(metablock_id=request["metablock_id"], **options)
        return state_to_dict(state)

    def catalog(
        self, state: dict[str, Any], scope: dict[str, Any], context: MetaBlockContext
    ) -> dict[str, Any]:
        items = []
        for kind in element_kinds():
            parameters = (
                kind.parameters if isinstance(kind.parameters, tuple) else (kind.parameters,)
            )
            items.append(
                {
                    "id": kind.kind,
                    "title": kind.kind.replace("_", " ").title(),
                    "category": _CATEGORIES.get(kind.kind, "Other"),
                    "kind": kind.kind,
                    "input_ports": [asdict(port) for port in kind.input_ports],
                    "output_ports": [asdict(port) for port in kind.output_ports],
                    "parameters": [asdict(parameter) for parameter in parameters],
                    "required_parameters": list(kind.required_parameters),
                    "description": None,
                    "search_terms": [kind.kind],
                    "enabled": True,
                }
            )
        items.extend(
            {
                "id": f"template.{name}",
                "title": name.replace("_", " ").title(),
                "category": "Templates",
                "kind": "template",
                "description": "Create from a PyTorch template",
                "search_terms": [name, "template"],
                "enabled": True,
            }
            for name in _TEMPLATES
        )
        return {
            "dialect_id": self.dialect_id,
            "scope_id": scope.get("id", context.scope_id),
            "items": items,
        }

    def project(
        self, state: dict[str, Any], scope: dict[str, Any], context: MetaBlockContext
    ) -> dict[str, Any]:
        model = state_from_dict(state)
        active_group_id = scope.get("group_id")
        external_values = scope.get("external_values")
        compile_result = None
        diagnostics = []
        try:
            compile_result = compile_model(
                model, external_values if isinstance(external_values, dict) else None
            )
        except GraphimoPytorchError as error:
            diagnostics.append({"code": error.code, "message": str(error)})
        projection = project_workbench(
            model,
            method_name=str(scope.get("method_name", "forward")),
            active_group_id=active_group_id if isinstance(active_group_id, str) else None,
            compile_result=compile_result,
            external_values=external_values if isinstance(external_values, dict) else None,
            shape_result=infer_model_shapes(
                model, external_values if isinstance(external_values, dict) else None
            ),
        )
        result = asdict(projection)
        if diagnostics:
            result["diagnostics"] = [*result["diagnostics"], *diagnostics]
        result["dialect_id"] = self.dialect_id
        result["scope_id"] = scope.get("id", context.scope_id)
        return result

    def apply(
        self, state: dict[str, Any], intent: dict[str, Any], context: MetaBlockContext
    ) -> dict[str, Any]:
        model = state_from_dict(state)
        kind = intent.get("kind")
        if kind not in {
            "set_parameter",
            "promote_parameter",
            "demote_parameter",
            "add_element",
            "connect_elements",
            "disconnect_edge",
        }:
            raise GraphimoPytorchError(
                "pytorch_intent_unsupported", f"Unsupported PyTorch edit intent {kind!r}"
            )
        scope_id = _required_string(intent, "scope_id")
        if kind == "add_element":
            params = intent.get("params", {})
            if not isinstance(params, dict):
                raise GraphimoPytorchError(
                    "pytorch_element_parameters_invalid", "params must be an object"
                )
            model = add_element(
                model,
                scope_id,
                _required_string(intent, "element_id"),
                _required_string(intent, "element_kind"),
                _required_string(intent, "name"),
                params,
            )
            return {
                "state": state_to_dict(model),
                "diagnostics": [],
                "projection_invalidated": True,
            }
        if kind == "connect_elements":
            model = connect_elements(
                model,
                scope_id,
                _required_string(intent, "edge_id"),
                _required_string(intent, "source_element_id"),
                _required_string(intent, "source_port"),
                _required_string(intent, "target_element_id"),
                _required_string(intent, "target_port"),
            )
            return {
                "state": state_to_dict(model),
                "diagnostics": [],
                "projection_invalidated": True,
            }
        if kind == "disconnect_edge":
            model = disconnect_edge(model, scope_id, _required_string(intent, "edge_id"))
            return {
                "state": state_to_dict(model),
                "diagnostics": [],
                "projection_invalidated": True,
            }
        element_id = _required_string(intent, "element_id")
        parameter_name = _required_string(intent, "parameter_name")
        if kind == "set_parameter":
            model = set_element_parameter(
                model, scope_id, element_id, parameter_name, intent.get("value")
            )
        elif kind == "promote_parameter":
            model = promote_parameter(
                model,
                scope_id,
                element_id,
                parameter_name,
                external_parameter_id=intent.get("external_parameter_id"),
                external_name=intent.get("external_name"),
            )
        elif kind == "demote_parameter":
            values = intent.get("external_values", {})
            if not isinstance(values, dict):
                raise GraphimoPytorchError(
                    "pytorch_external_values_invalid", "external_values must be an object"
                )
            model = demote_parameter(
                model, scope_id, element_id, parameter_name, external_values=values
            )
        return {"state": state_to_dict(model), "diagnostics": [], "projection_invalidated": True}

    def build_materialization(
        self, state: dict[str, Any], bindings: dict[str, str], context: MetaBlockContext
    ) -> MaterializationSpec:
        model = state_from_dict(state)
        parameters: dict[str, str] = {}
        public_bindings: dict[str, str] = {}
        known = {item.id: item for item in model.external_parameters}
        for key, expression in bindings.items():
            if key not in known and key not in {item.name for item in model.external_parameters}:
                raise GraphimoPytorchError(
                    "pytorch_external_parameter_unknown",
                    f"Unknown external parameter binding {key!r}",
                )
            parameter = known.get(key) or next(
                item for item in model.external_parameters if item.name == key
            )
            try:
                ast.parse(expression, mode="eval")
            except SyntaxError as error:
                raise GraphimoPytorchError(
                    "pytorch_binding_expression_invalid",
                    f"Invalid Python input expression for {parameter.name!r}",
                ) from error
            parameters[parameter.id] = expression
            public_bindings[parameter.name] = expression
        unknown_keys = (
            set(bindings)
            - {item.id for item in model.external_parameters}
            - {item.name for item in model.external_parameters}
        )
        if unknown_keys:
            raise GraphimoPytorchError(
                "pytorch_external_parameter_unknown",
                f"Unknown external parameter bindings: {sorted(unknown_keys)!r}",
            )
        result_var = f"g_pytorch_{hashlib.sha256(model.metablock_id.encode()).hexdigest()[:8]}"
        state_path = str(context.sidecar_path.resolve())
        args = [
            "from graphimo_pytorch import materialize_definition as _graphimo_materialize_definition",
            f"{result_var} = _graphimo_materialize_definition(\n    state_path={state_path!r},\n    project_path={context.project_path!r},\n    external_values={{",
        ]
        args.extend(f"        {key!r}: {expression}," for key, expression in parameters.items())
        args.append("    },\n)")
        source = "\n".join(args)
        revision = compile_model(model).definition.graph_revision
        return MaterializationSpec(
            source=source,
            result_var=result_var,
            output_type="graphimo_pytorch.PyTorchModelDefinition",
            output_label=model.name,
            bindings=public_bindings,
            state_revision=revision,
        )

    def invoke(
        self, state: dict[str, Any], action: str, args: dict[str, Any], context: MetaBlockContext
    ) -> Any:
        model_state = state_from_dict(state)
        if action == "subscribe":
            runtime_id = _required_string(args, "runtime_instance_id")
            model = runtime_registry.resolve(runtime_id)
            if model is None:
                raise GraphimoPytorchError(
                    "pytorch_runtime_not_found",
                    "The selected runtime model is unavailable in this process",
                )
            detail = args.get("detail", "summary")
            rate = args.get("rate_hz", default_subscription_rate_hz(detail))
            maximum_rate = 10.0 if detail == "summary" else 2.0
            if type(rate) not in (int, float) or not math.isfinite(rate) or rate > maximum_rate:
                raise GraphimoPytorchError(
                    "pytorch_subscription_rate_invalid",
                    f"rate_hz must not exceed {maximum_rate} for {detail!r} detail",
                )
            spec = SubscriptionSpec(
                _required_string(args, "subscription_id"),
                runtime_id,
                _required_string(args, "logical_element_id"),
                args.get("repetition_index"),
                args.get("signal", "activation"),
                float(rate),
                detail,
            )
            sites = subscribe(model, spec)
            return {
                "subscription_id": spec.subscription_id,
                "sites": [asdict(site) for site in sites],
            }
        if action == "unsubscribe":
            return {"removed": unsubscribe(_required_string(args, "subscription_id"))}
        if action == "torchlens.inspect":
            intervention = args.get("intervention")
            intervention_value = None
            if intervention is not None:
                if not isinstance(intervention, dict):
                    raise GraphimoPytorchError(
                        "pytorch_torchlens_intervention_invalid", "intervention must be an object"
                    )
                intervention_value = TorchLensIntervention(
                    intervention.get("kind"), intervention.get("factor", 1.0)
                )
            capture_gradients = args.get("capture_gradients", False)
            if type(capture_gradients) is not bool:
                raise GraphimoPytorchError(
                    "pytorch_torchlens_request_invalid", "capture_gradients must be a boolean"
                )
            request = TorchLensRequest(
                _required_string(args, "runtime_instance_id"),
                _required_string(args, "logical_element_id"),
                args.get("repetition_index"),
                capture_gradients,
                intervention_value,
            )
            result = inspect_with_torchlens(
                request, _torch_inputs(args.get("inputs")), loss_fn=None
            )
            result_dict = {
                "runtime_instance_id": result.runtime_instance_id,
                "metablock_id": result.metablock_id,
                "graph_revision": result.graph_revision,
                "materialization_key": result.materialization_key,
                "logical_element_id": result.logical_element_id,
                "module_path": result.module_path,
                "repetition_index": result.repetition_index,
                "operations": [asdict(item) for item in result.operations],
                "operation_count": result.operation_count,
                "activation": dict(result.activation) if result.activation else None,
                "gradient": dict(result.gradient) if result.gradient else None,
                "nonfinite_diagnostics": list(result.nonfinite_diagnostics),
                "trace_metadata": dict(result.trace_metadata),
                "experimental": result.experimental,
                "intervention": asdict(result.intervention) if result.intervention else None,
                "diagnostics": [asdict(item) for item in result.diagnostics],
                "artifact_ref": None,
            }
            if args.get("save_trace"):
                result_dict["artifact_ref"] = asdict(
                    save_torchlens_inspection(result, context.artifact_store)
                )
            return result_dict
        if action == "checkpoint.save":
            runtime_id = _required_string(args, "runtime_instance_id")
            model = runtime_registry.resolve(runtime_id)
            if model is None:
                raise GraphimoPytorchError(
                    "pytorch_runtime_not_found",
                    "The selected runtime model is unavailable in this process",
                )
            return asdict(
                save_checkpoint(
                    model, metadata=args.get("metadata", {}), store=context.artifact_store
                )
            )
        if action == "checkpoint.load":
            artifact = _artifact_ref(args.get("artifact"))
            definition = compile_model(model_state, args.get("external_values", {})).definition
            model = load_checkpoint(
                definition,
                artifact,
                device=args.get("device"),
                strict=args.get("strict", True),
                store=context.artifact_store,
            )
            runtime_id = model.__graphimo_pytorch_runtime__.runtime_instance_id
            self._owned_models[runtime_id] = model
            return {"runtime_instance_id": runtime_id}
        if action == "runtime.release":
            runtime_id = _required_string(args, "runtime_instance_id")
            model = self._owned_models.pop(runtime_id, None)
            if model is None:
                return {"released": False}
            release_runtime_model(model)
            return {"released": True}
        if action == "runtime.list":
            return [
                asdict(item)
                for item in runtime_registry.list_for_metablock(model_state.metablock_id)
            ]
        if action == "migration.report":
            runtime_id = _required_string(args, "runtime_instance_id")
            runtime = runtime_registry.resolve(runtime_id)
            report = (
                getattr(runtime, "__graphimo_pytorch_migration_report__", None)
                if runtime is not None
                else None
            )
            if report is None:
                return None
            return asdict(report)
        raise GraphimoPytorchError(
            "pytorch_action_unsupported", f"Unsupported PyTorch action {action!r}"
        )

    def drain_events(self, cursor: int, limit: int, context: MetaBlockContext) -> dict[str, Any]:
        # A single addon handler fans the shared non-blocking runtime queue into
        # bounded per-MetaBlock cursor buffers so concurrent models do not steal
        # one another's events.
        for event in event_queue.drain():
            record = event.to_dict()
            metablock_id = str(record["metablock_id"])
            with self._lock:
                buffer = self._events.setdefault(
                    metablock_id, deque(maxlen=_MAX_EVENTS_PER_METABLOCK)
                )
                sequence = self._next_event.get(metablock_id, 0)
                buffer.append((sequence, record))
                self._next_event[metablock_id] = sequence + 1
        target_id = context.scope_id
        with self._lock:
            buffer = self._events.get(target_id, deque())
            first = buffer[0][0] if buffer else self._next_event.get(target_id, 0)
            selected = [(sequence, event) for sequence, event in buffer if sequence >= cursor][
                :limit
            ]
            next_cursor = selected[-1][0] + 1 if selected else max(cursor, first)
            return {
                "cursor": next_cursor,
                "events": [event for _, event in selected],
                "dropped": max(0, first - cursor),
            }


def _required_string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise GraphimoPytorchError(
            "pytorch_action_argument_invalid", f"{key} must be a non-empty string"
        )
    return result


def _artifact_ref(value: Any):
    from graphimo.artifacts import ArtifactRef

    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise GraphimoPytorchError(
            "pytorch_checkpoint_artifact_invalid", "artifact must contain its Graphimo artifact id"
        )
    return ArtifactRef(value["id"], str(value.get("kind", "pytorch-checkpoint")))


def _torch_inputs(value: Any) -> Any:
    import torch

    if isinstance(value, dict):
        return {key: _torch_inputs(item) for key, item in value.items()}
    if isinstance(value, list):
        if all(type(item) in {int, float, bool} for item in value):
            return torch.as_tensor(value)
        return tuple(_torch_inputs(item) for item in value)
    if type(value) in {int, float, bool}:
        return torch.as_tensor(value)
    return value


def materialize_definition(
    *,
    state_path: str | Path,
    project_path: str | Path | None = None,
    external_values: dict[str, Any] | None = None,
):
    """Load persisted addon state and create its definition in the marimo kernel."""
    from .serialization import load_state

    state = load_state(Path(state_path))
    if project_path is not None:
        from .telemetry import configure_project_event_relay

        configure_project_event_relay(str(project_path), state.metablock_id)
    return compile_model(state, external_values or {}).definition
