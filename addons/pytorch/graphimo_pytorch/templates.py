"""Readable, ordinary semantic graph templates for the PyTorch addon."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .diagnostics import GraphimoPytorchError
from .schema import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    GroupDefinition,
    GroupInstance,
    GroupPort,
    MethodGraph,
    MethodInput,
    MethodOutput,
    PyTorchMetaBlockState,
    TensorContract,
)


@dataclass(frozen=True, slots=True)
class LlamaTemplateConfig:
    vocab_size: int = 32000
    d: int = 4096
    n_head: int = 32
    ff: int = 11008
    n_layers: int = 32
    seq_len_for_shape_preview: int = 64
    rms_eps: float = 1e-5
    init_std: float = 0.02
    rope_base: float = 10000.0

    def __post_init__(self) -> None:
        positive_ints = (self.vocab_size, self.d, self.n_head, self.ff, self.n_layers, self.seq_len_for_shape_preview)
        if any(type(value) is not int or value < 1 for value in positive_ints):
            raise GraphimoPytorchError("pytorch_llama_config_invalid", "Llama dimensions, layer count, and preview length must be positive integers")
        if self.d % self.n_head:
            raise GraphimoPytorchError("pytorch_llama_config_invalid", "Llama width d must be divisible by n_head")
        if not math.isfinite(self.rms_eps) or self.rms_eps <= 0:
            raise GraphimoPytorchError("pytorch_llama_config_invalid", "rms_eps must be finite and positive")
        if not math.isfinite(self.init_std) or self.init_std <= 0:
            raise GraphimoPytorchError("pytorch_llama_config_invalid", "init_std must be finite and positive")
        if not math.isfinite(self.rope_base) or self.rope_base <= 0:
            raise GraphimoPytorchError("pytorch_llama_config_invalid", "rope_base must be finite and positive")


@dataclass(frozen=True, slots=True)
class ParameterEstimate:
    total_parameters: int
    trainable_parameters: int
    parameter_bytes: int
    dtype: str


def mlp_template(
    *, input_size: int = 16, hidden: int = 32, output_size: int = 4, metablock_id: str = "template-mlp"
) -> PyTorchMetaBlockState:
    """Create the catalog MLP as ordinary editable Graphimo element records."""
    if any(type(value) is not int or value < 1 for value in (input_size, hidden, output_size)):
        raise GraphimoPytorchError("pytorch_template_dimensions_invalid", "MLP dimensions must be positive integers")
    elements = (
        ElementRecord("input", "input", "input", {"method_input": "features"}),
        ElementRecord("linear-a", "linear", "linear_a", {"in_features": input_size, "out_features": hidden}),
        ElementRecord("relu", "relu", "relu"),
        ElementRecord("linear-b", "linear", "linear_b", {"in_features": hidden, "out_features": output_size}),
        ElementRecord("output", "output", "output", {"method_output": "values"}),
    )
    edges = _chain(("input", "linear-a"), ("linear-a", "relu"), ("relu", "linear-b"), ("linear-b", "output"))
    method = MethodGraph(
        "forward",
        (MethodInput("features", "features", TensorContract(("e", input_size), "float32")),),
        (MethodOutput("values", "values"),),
        elements,
        edges,
        (),
    )
    return PyTorchMetaBlockState(1, metablock_id, "MLP", (), (), (method,), {"template": "mlp"})


def self_attention_template(
    *,
    d_model: int = 32,
    n_heads: int = 4,
    causal: bool = True,
    rope: bool = True,
    normalization: bool = False,
    init_std: float = 0.02,
    metablock_id: str = "template-self-attention",
) -> PyTorchMetaBlockState:
    """Create the reusable attention Group from the documented component palette."""
    _validate_attention_dimensions(d_model, n_heads, init_std)
    group = _attention_group(
        "self-attention",
        "SelfAttention",
        d_model=d_model,
        n_heads=n_heads,
        causal=causal,
        rope=rope,
        normalization=normalization,
        init_std=init_std,
        eps=1e-5,
        residual=False,
        ff_width=None,
        rope_base=10000.0,
    )
    method = _single_group_method(group, "hidden_states", TensorContract(("e", "t", d_model), "float32"))
    return PyTorchMetaBlockState(1, metablock_id, "SelfAttention", (), (group,), (method,), {"template": "self_attention"})


def llama_decoder_layer_template(
    config: LlamaTemplateConfig | None = None,
    *,
    metablock_id: str = "template-llama-decoder-layer",
) -> PyTorchMetaBlockState:
    """Create a one-layer model whose reusable Group is the Llama decoder layer."""
    config = config or LlamaTemplateConfig()
    group = _llama_decoder_group(config)
    method = _single_group_method(group, "hidden_states", TensorContract(("e", "t", config.d), "float32"))
    return PyTorchMetaBlockState(1, metablock_id, "LlamaDecoderLayer", (), (group,), (method,), {
        "template": "llama_decoder_layer",
        "template_config": _config_json(config),
    })


def llama_causal_lm_template(
    config: LlamaTemplateConfig | None = None,
    *,
    metablock_id: str = "template-llama-causal-lm",
) -> PyTorchMetaBlockState:
    """Build the full-size-by-default causal LM graph without allocating weights."""
    config = config or LlamaTemplateConfig()
    decoder = _llama_decoder_group(config)
    input_shape = ("e", config.seq_len_for_shape_preview)
    logits_shape = ("e", config.seq_len_for_shape_preview, config.vocab_size)

    forward_elements = (
        ElementRecord("input_ids", "input", "input_ids", {"method_input": "input_ids"}),
        ElementRecord("embed_tokens", "embedding", "embed_tokens", {
            "num_embeddings": config.vocab_size, "embedding_dim": config.d, "init_std": config.init_std,
        }),
        ElementRecord("norm", "rms_norm", "norm", {"normalized_shape": config.d, "eps": config.rms_eps}),
        ElementRecord("lm_head", "linear", "lm_head", {
            "in_features": config.d, "out_features": config.vocab_size, "bias": False, "init_std": config.init_std,
        }),
        ElementRecord("logits", "output", "logits", {"method_output": "logits"}),
    )
    forward_edges = (
        _edge("input-embed", "input_ids", "out", "embed_tokens", "x"),
        _edge("embed-layers", "embed_tokens", "out", "layers", "hidden_states"),
        _edge("layers-norm", "layers", "hidden_states", "norm", "x"),
        _edge("norm-head", "norm", "out", "lm_head", "x"),
        _edge("head-logits", "lm_head", "out", "logits", "x"),
    )
    forward = MethodGraph(
        "forward",
        (MethodInput("input_ids", "input_ids", TensorContract(input_shape, "int64")),),
        (MethodOutput("logits", "logits"),),
        forward_elements,
        forward_edges,
        (GroupInstance("layers", decoder.id, "layers", config.n_layers),),
    )

    loss_elements = (
        ElementRecord("logits", "input", "logits", {"method_input": "logits"}),
        ElementRecord("input_ids", "input", "input_ids", {"method_input": "input_ids"}),
        ElementRecord("cross_entropy", "cross_entropy", "cross_entropy", {"mode": "next_token"}),
        ElementRecord("loss", "output", "loss", {"method_output": "loss"}),
    )
    loss_edges = (
        _edge("loss-logits", "logits", "out", "cross_entropy", "logits"),
        _edge("loss-target", "input_ids", "out", "cross_entropy", "target"),
        _edge("loss-output", "cross_entropy", "out", "loss", "x"),
    )
    loss = MethodGraph(
        "loss",
        (
            MethodInput("logits", "logits", TensorContract(logits_shape, "float32")),
            MethodInput("input_ids", "input_ids", TensorContract(input_shape, "int64")),
        ),
        (MethodOutput("loss", "loss"),),
        loss_elements,
        loss_edges,
        (),
    )

    predict_elements = (
        ElementRecord("input_ids", "input", "input_ids", {"method_input": "input_ids"}),
        ElementRecord("forward_call", "method_call", "forward_call", {"method": "forward", "output": "logits"}),
        ElementRecord("probabilities", "softmax", "probabilities", {"dim": -1}),
        ElementRecord("output", "output", "probabilities_output", {"method_output": "probabilities"}),
    )
    predict_edges = (
        _edge("predict-call", "input_ids", "out", "forward_call", "x"),
        _edge("predict-softmax", "forward_call", "out", "probabilities", "x"),
        _edge("predict-output", "probabilities", "out", "output", "x"),
    )
    predict = MethodGraph(
        "predict",
        (MethodInput("input_ids", "input_ids", TensorContract(input_shape, "int64")),),
        (MethodOutput("probabilities", "probabilities"),),
        predict_elements,
        predict_edges,
        (),
    )
    return PyTorchMetaBlockState(1, metablock_id, "LlamaForCausalLM", (), (decoder,), (forward, loss, predict), {
        "template": "llama_causal_lm",
        "template_config": _config_json(config),
        "parameter_estimate": _estimate_json(estimate_llama_parameters(config)),
    })


def estimate_llama_parameters(
    config: LlamaTemplateConfig | None = None,
    *,
    dtype: str = "float32",
) -> ParameterEstimate:
    """Estimate the required Llama template's parameters without instantiating it."""
    config = config or LlamaTemplateConfig()
    bytes_per_parameter = {"float16": 2, "bfloat16": 2, "float32": 4, "float64": 8}.get(dtype)
    if bytes_per_parameter is None:
        raise GraphimoPytorchError("pytorch_parameter_estimate_dtype_invalid", f"Unsupported estimate dtype {dtype!r}")
    per_decoder_layer = 4 * config.d * config.d + 3 * config.d * config.ff + 2 * config.d
    total = 2 * config.vocab_size * config.d + config.n_layers * per_decoder_layer + config.d
    return ParameterEstimate(total, total, total * bytes_per_parameter, dtype)


def _llama_decoder_group(config: LlamaTemplateConfig) -> GroupDefinition:
    return _attention_group(
        "llama-decoder-layer",
        "LlamaDecoderLayer",
        d_model=config.d,
        n_heads=config.n_head,
        causal=True,
        rope=True,
        normalization=True,
        init_std=config.init_std,
        eps=config.rms_eps,
        residual=True,
        ff_width=config.ff,
        rope_base=config.rope_base,
    )


def _attention_group(
    group_id: str,
    group_name: str,
    *,
    d_model: int,
    n_heads: int,
    causal: bool,
    rope: bool,
    normalization: bool,
    init_std: float,
    eps: float,
    residual: bool,
    ff_width: int | None,
    rope_base: float,
) -> GroupDefinition:
    elements: list[ElementRecord] = []
    edges: list[EdgeRecord] = []
    group_input = GroupPort("hidden_states", "hidden_states", TensorContract(("e", "t", d_model), "float32"))
    group_output = GroupPort("hidden_states_out", "hidden_states", TensorContract(("e", "t", d_model), "float32"))
    source = group_input.id
    if normalization:
        elements.append(ElementRecord("input_layernorm", "rms_norm", "input_layernorm", {"normalized_shape": d_model, "eps": eps}))
        edges.append(_edge("group-input-norm", source, "out", "input_layernorm", "x"))
        source = "input_layernorm"
    attention_output = _append_attention_core(
        elements,
        edges,
        source,
        d_model=d_model,
        n_heads=n_heads,
        causal=causal,
        rope=rope,
        init_std=init_std,
        rope_base=rope_base,
        edge_prefix="attention",
    )
    if residual:
        elements.append(ElementRecord("attention_residual", "add", "attention_residual"))
        edges.append(_edge("residual-input", group_input.id, "out", "attention_residual", "a"))
        edges.append(_edge("residual-attention", attention_output, "out", "attention_residual", "b"))
        elements.append(ElementRecord("post_attention_layernorm", "rms_norm", "post_attention_layernorm", {
            "normalized_shape": d_model, "eps": eps,
        }))
        edges.append(_edge("residual-norm", "attention_residual", "out", "post_attention_layernorm", "x"))
        assert ff_width is not None
        elements.extend((
            ElementRecord("gate_proj", "linear", "gate_proj", {"in_features": d_model, "out_features": ff_width, "bias": False, "init_std": init_std}),
            ElementRecord("gate_silu", "silu", "gate_silu"),
            ElementRecord("up_proj", "linear", "up_proj", {"in_features": d_model, "out_features": ff_width, "bias": False, "init_std": init_std}),
            ElementRecord("gate_up_multiply", "multiply", "gate_up_multiply"),
            ElementRecord("down_proj", "linear", "down_proj", {"in_features": ff_width, "out_features": d_model, "bias": False, "init_std": init_std}),
            ElementRecord("mlp_residual", "add", "mlp_residual"),
        ))
        edges.extend((
            _edge("norm-gate", "post_attention_layernorm", "out", "gate_proj", "x"),
            _edge("gate-silu", "gate_proj", "out", "gate_silu", "x"),
            _edge("norm-up", "post_attention_layernorm", "out", "up_proj", "x"),
            _edge("silu-multiply", "gate_silu", "out", "gate_up_multiply", "a"),
            _edge("up-multiply", "up_proj", "out", "gate_up_multiply", "b"),
            _edge("multiply-down", "gate_up_multiply", "out", "down_proj", "x"),
            _edge("residual-mlp", "attention_residual", "out", "mlp_residual", "a"),
            _edge("down-mlp-residual", "down_proj", "out", "mlp_residual", "b"),
            _edge("group-output", "mlp_residual", "out", group_output.id, "x"),
        ))
    elif normalization:
        edges.append(_edge("group-output", attention_output, "out", group_output.id, "x"))
    else:
        edges.append(_edge("group-output", attention_output, "out", group_output.id, "x"))
    return GroupDefinition(group_id, group_name, (group_input,), (group_output,), tuple(elements), tuple(edges), ())


def _append_attention_core(
    elements: list[ElementRecord],
    edges: list[EdgeRecord],
    source: str,
    *,
    d_model: int,
    n_heads: int,
    causal: bool,
    rope: bool,
    init_std: float,
    rope_base: float,
    edge_prefix: str,
) -> str:
    for projection in ("q_proj", "k_proj", "v_proj"):
        elements.append(ElementRecord(projection, "linear", projection, {
            "in_features": d_model, "out_features": d_model, "bias": False, "init_std": init_std,
        }))
        edges.append(_edge(f"{edge_prefix}-{projection}-in", source, "out", projection, "x"))
        split = f"{projection}_split"
        elements.append(ElementRecord(split, "split_heads", split, {"n_heads": n_heads}))
        edges.append(_edge(f"{edge_prefix}-{projection}-split", projection, "out", split, "x"))
        if rope and projection in {"q_proj", "k_proj"}:
            rope_id = f"{projection}_rope"
            elements.append(ElementRecord(rope_id, "rope", rope_id, {"base": rope_base}))
            edges.append(_edge(f"{edge_prefix}-{projection}-rope", split, "out", rope_id, "x"))
            projection_source = rope_id
        else:
            projection_source = split
        if projection == "q_proj":
            scale_id = "q_scale"
            elements.append(ElementRecord(scale_id, "scale", scale_id, {"factor": (d_model // n_heads) ** -0.5}))
            edges.append(_edge(f"{edge_prefix}-q-scale", projection_source, "out", scale_id, "x"))
        elif projection == "k_proj":
            elements.append(ElementRecord("k_transpose", "transpose", "k_transpose", {"dim0": -1, "dim1": -2}))
            edges.append(_edge(f"{edge_prefix}-k-transpose", projection_source, "out", "k_transpose", "x"))

    elements.append(ElementRecord("attention_scores", "matmul", "attention_scores"))
    edges.extend((
        _edge(f"{edge_prefix}-qk-left", "q_scale", "out", "attention_scores", "a"),
        _edge(f"{edge_prefix}-qk-right", "k_transpose", "out", "attention_scores", "b"),
    ))
    attention_scores = "attention_scores"
    if causal:
        elements.append(ElementRecord("causal_scores", "causal_mask", "causal_scores"))
        edges.append(_edge(f"{edge_prefix}-causal-mask", attention_scores, "out", "causal_scores", "x"))
        attention_scores = "causal_scores"
    elements.extend((
        ElementRecord("attention_softmax", "softmax", "attention_softmax", {"dim": -1}),
        ElementRecord("attention_values", "matmul", "attention_values"),
        ElementRecord("merge_heads", "merge_heads", "merge_heads"),
        ElementRecord("o_proj", "linear", "o_proj", {"in_features": d_model, "out_features": d_model, "bias": False, "init_std": init_std}),
    ))
    edges.extend((
        _edge(f"{edge_prefix}-softmax", attention_scores, "out", "attention_softmax", "x"),
        _edge(f"{edge_prefix}-weights-values", "attention_softmax", "out", "attention_values", "a"),
        _edge(f"{edge_prefix}-values-right", "v_proj_split", "out", "attention_values", "b"),
        _edge(f"{edge_prefix}-merge", "attention_values", "out", "merge_heads", "x"),
        _edge(f"{edge_prefix}-o-proj", "merge_heads", "out", "o_proj", "x"),
    ))
    return "o_proj"


def _single_group_method(group: GroupDefinition, input_name: str, contract: TensorContract) -> MethodGraph:
    elements = (
        ElementRecord("input", "input", input_name, {"method_input": input_name}),
        ElementRecord("output", "output", "output", {"method_output": "output"}),
    )
    edges = (
        _edge("method-group-in", "input", "out", "group", group.inputs[0].name),
        _edge("method-group-out", "group", group.outputs[0].name, "output", "x"),
    )
    return MethodGraph(
        "forward",
        (MethodInput(input_name, input_name, contract),),
        (MethodOutput("output", "output"),),
        elements,
        edges,
        (GroupInstance("group", group.id, "group", 1),),
    )


def _validate_attention_dimensions(d_model: int, n_heads: int, init_std: float) -> None:
    if type(d_model) is not int or d_model < 1 or type(n_heads) is not int or n_heads < 1:
        raise GraphimoPytorchError("pytorch_template_dimensions_invalid", "Attention dimensions must be positive integers")
    if d_model % n_heads:
        raise GraphimoPytorchError("pytorch_template_dimensions_invalid", "d_model must be divisible by n_heads")
    if not math.isfinite(init_std) or init_std <= 0:
        raise GraphimoPytorchError("pytorch_template_dimensions_invalid", "init_std must be finite and positive")


def _config_json(config: LlamaTemplateConfig) -> dict[str, int | float]:
    return {
        "vocab_size": config.vocab_size,
        "d": config.d,
        "n_head": config.n_head,
        "ff": config.ff,
        "n_layers": config.n_layers,
        "seq_len_for_shape_preview": config.seq_len_for_shape_preview,
        "rms_eps": config.rms_eps,
        "init_std": config.init_std,
        "rope_base": config.rope_base,
    }


def _estimate_json(estimate: ParameterEstimate) -> dict[str, int | str]:
    return {
        "total_parameters": estimate.total_parameters,
        "trainable_parameters": estimate.trainable_parameters,
        "parameter_bytes": estimate.parameter_bytes,
        "dtype": estimate.dtype,
    }


def _edge(edge_id: str, source_id: str, source_port: str, target_id: str, target_port: str) -> EdgeRecord:
    return EdgeRecord(edge_id, Endpoint(source_id, source_port), Endpoint(target_id, target_port))


def _chain(*pairs: tuple[str, str]) -> tuple[EdgeRecord, ...]:
    return tuple(_edge(f"chain-{index}", source, "out", target, "x") for index, (source, target) in enumerate(pairs))
