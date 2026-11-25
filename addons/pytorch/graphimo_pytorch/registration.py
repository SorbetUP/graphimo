"""Generic Graphimo MetaBlock registration for the PyTorch addon."""

from __future__ import annotations

from typing import Any

from graphimo.metablocks import GraphimoHostAPI, MetaDialectRegistration, state_revision

from .prototype import ModelSpec


class PyTorchModelDialect:
    dialect_id = "pytorch.model"

    def __init__(self, host: GraphimoHostAPI):
        self.host = host

    def create_state(self, request: dict[str, Any], context: GraphimoHostAPI) -> dict[str, Any]:
        spec = request.get(
            "model_spec", {"input_size": 4, "hidden_size": 8, "head_sizes": {"output": 1}}
        )
        validated = ModelSpec(**spec)
        return {
            "schema_version": 1,
            "name": request.get("name", "PyTorch Model"),
            "model_spec": {
                "input_size": validated.input_size,
                "hidden_size": validated.hidden_size,
                "head_sizes": dict(sorted(validated.head_sizes.items())),
            },
        }

    def catalog(self, state: Any, scope: str, context: GraphimoHostAPI) -> dict[str, Any]:
        if scope == "root":
            return {"scope": scope, "items": ["pytorch.model"]}
        return {
            "scope": scope,
            "items": ["linear", "relu", "activation", "tensor.add", "tensor.multiply"],
        }

    def project(self, state: Any, scope: str, context: GraphimoHostAPI) -> dict[str, Any]:
        spec = ModelSpec(**state["model_spec"])
        return {
            "scope": scope,
            "name": state["name"],
            "elements": [
                {"id": "model", "kind": "model", "name": state["name"]},
                {
                    "id": "backbone_linear",
                    "kind": "linear",
                    "name": "backbone",
                    "output_shape": [None, spec.hidden_size],
                },
                *[
                    {
                        "id": f"head_{name}",
                        "kind": "linear",
                        "name": name,
                        "output_shape": [None, size],
                    }
                    for name, size in sorted(spec.head_sizes.items())
                ],
            ],
            "edges": [],
            "model_spec": state["model_spec"],
        }

    def apply(self, state: Any, intent: dict[str, Any], context: GraphimoHostAPI) -> dict[str, Any]:
        if intent.get("type") != "set_model_parameter":
            raise ValueError("unsupported PyTorch MetaBlock edit intent")
        field = intent.get("name")
        value = intent.get("value")
        if field == "name" and isinstance(value, str) and value.strip():
            return {**state, "name": value.strip()}
        if field not in {"input_size", "hidden_size", "head_sizes"}:
            raise ValueError("unsupported PyTorch model parameter")
        model_spec = {**state["model_spec"], field: value}
        validated = ModelSpec(**model_spec)
        return {
            **state,
            "model_spec": {
                "input_size": validated.input_size,
                "hidden_size": validated.hidden_size,
                "head_sizes": dict(sorted(validated.head_sizes.items())),
            },
        }

    def build_materialization(
        self, state: Any, bindings: dict[str, Any], context: GraphimoHostAPI
    ) -> dict[str, Any]:
        metablock_id = context.current_metablock_id
        if not metablock_id:
            raise ValueError("MetaBlock materialization context has no current MetaBlock ID")
        record = context.read_sidecar(metablock_id)
        if record is None:
            raise FileNotFoundError(f"MetaBlock '{metablock_id}' sidecar is missing")
        revision = state_revision(state)
        if state_revision(record.get("state")) != revision:
            raise ValueError("MetaBlock state changed before materialization")
        state_path = context.sidecar_path(metablock_id, "pytorch.model").resolve()
        source = (
            "from graphimo_pytorch import materialize_definition\n\n"
            f"g_pytorch_{metablock_id} = materialize_definition(\n"
            f"    state_path={str(state_path)!r},\n"
            f"    metablock_id={metablock_id!r},\n"
            f"    external_values={bindings!r},\n"
            ")\n"
        )
        return {
            "source": source,
            "result_name": f"g_pytorch_{metablock_id}",
            "output_type": "PyTorchModelDefinition",
            "bindings": bindings,
            "revision": revision,
        }

    def invoke(
        self, state: Any, action: str, args: dict[str, Any], context: GraphimoHostAPI
    ) -> Any:
        if action == "parameter_summary":
            spec = ModelSpec(**state["model_spec"])
            parameters = spec.input_size * spec.hidden_size + spec.hidden_size
            parameters += sum(spec.hidden_size * size + size for size in spec.head_sizes.values())
            return {"total_parameters": parameters, "trainable_parameters": parameters}
        raise ValueError(f"unsupported PyTorch MetaBlock action: {action}")

    def drain_events(
        self, cursor: str | None, limit: int, context: GraphimoHostAPI
    ) -> dict[str, Any]:
        return {"cursor": cursor or "0", "events": [], "dropped": 0}


registration = MetaDialectRegistration(
    dialect_id="pytorch.model",
    addon_version="0.1.0",
    api_version=1,
    factory=PyTorchModelDialect,
)
