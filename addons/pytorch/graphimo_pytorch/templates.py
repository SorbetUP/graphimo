"""Reusable attention and Llama semantic graph templates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schema import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    ExternalParameter,
    GroupDefinition,
    GroupInstance,
    GroupPort,
    MethodGraph,
    MethodInput,
    MethodOutput,
    ParameterBinding,
    PyTorchMetaBlockState,
)


@dataclass(frozen=True)
class LlamaConfig:
    vocab_size: int = 32000
    d_model: int = 4096
    n_heads: int = 32
    ff_size: int = 11008
    n_layers: int = 32
    seq_len: int = 64
    rms_eps: float = 1e-5
    init_std: float = 0.02
    rope_base: float = 10000.0

    def __post_init__(self) -> None:
        for name in ("vocab_size", "d_model", "n_heads", "ff_size", "n_layers", "seq_len"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.d_model % self.n_heads:
            raise ValueError("d_model must be divisible by n_heads")
        if self.d_model // self.n_heads % 2:
            raise ValueError("head dimension must be even for RoPE")
        if self.rms_eps <= 0 or self.init_std < 0 or self.rope_base <= 0:
            raise ValueError("rms_eps and rope_base must be positive; init_std cannot be negative")


class _GraphBuilder:
    def __init__(self) -> None:
        self.elements: list[ElementRecord] = []
        self.edges: list[EdgeRecord] = []

    def element(self, identity: str, kind: str, name: str, params: dict[str, Any] | None = None,
                bindings: dict[str, str] | None = None, metadata: dict[str, Any] | None = None) -> str:
        self.elements.append(
            ElementRecord(
                identity,
                kind,
                name,
                params or {},
                {key: ParameterBinding(value) for key, value in (bindings or {}).items()},
                metadata or {},
            )
        )
        return identity

    def edge(self, identity: str, source: str, target: str,
             source_port: str = "output", target_port: str = "input") -> None:
        self.edges.append(EdgeRecord(identity, Endpoint(source, source_port), Endpoint(target, target_port)))

    def branch(self, identity: str, source: str, target: str,
               source_port: str = "output", target_port: str = "input") -> None:
        self.edge(identity, source, target, source_port, target_port)


def _llama_parameters(config: LlamaConfig) -> tuple[ExternalParameter, ...]:
    return (
        ExternalParameter("vocab_size", "vocab_size", "int", config.vocab_size, True),
        ExternalParameter("d_model", "d_model", "int", config.d_model, True),
        ExternalParameter("n_heads", "n_heads", "int", config.n_heads, True),
        ExternalParameter("ff_size", "ff_size", "int", config.ff_size, True),
        ExternalParameter("n_layers", "n_layers", "int", config.n_layers, True),
        ExternalParameter("seq_len", "seq_len", "int", config.seq_len, True),
        ExternalParameter("rms_eps", "rms_eps", "float", config.rms_eps, False),
        ExternalParameter("init_std", "init_std", "float", config.init_std, False),
        ExternalParameter("rope_base", "rope_base", "float", config.rope_base, False),
    )


def build_llama_decoder_group(config: LlamaConfig | None = None) -> GroupDefinition:
    """One editable Llama layer graph, instantiated once per logical repetition."""
    config = config or LlamaConfig()
    graph = _GraphBuilder()
    graph.element("x_alias", "output", "x", {"name": "x"})
    graph.element("input_layernorm", "rms_norm", "input_layernorm", {"normalized_shape": config.d_model, "eps": config.rms_eps}, {"normalized_shape": "d_model", "eps": "rms_eps"})
    for name in ("q_proj", "k_proj", "v_proj"):
        graph.element(name, "linear", name, {"in_features": config.d_model, "out_features": config.d_model, "bias": False, "init_std": config.init_std}, {"in_features": "d_model", "out_features": "d_model", "init_std": "init_std"})
        graph.element(f"{name}_split", "split_heads", f"{name}_split", {"n_heads": config.n_heads}, {"n_heads": "n_heads"})
    graph.element("q_rope", "rope", "q_rope", {"base": config.rope_base}, {"base": "rope_base"})
    graph.element("k_rope", "rope", "k_rope", {"base": config.rope_base}, {"base": "rope_base"})
    graph.element("q_scale", "scale", "q_scale", {"factor": (config.d_model // config.n_heads) ** -0.5})
    graph.element("k_transpose", "transpose", "k_transpose", {"dim0": -2, "dim1": -1})
    graph.element("qk_matmul", "matmul", "qk_matmul")
    graph.element("causal_mask", "causal_mask", "causal_mask")
    graph.element("attention_softmax", "softmax", "attention_softmax", {"dim": -1})
    graph.element("attention_value", "matmul", "attention_value")
    graph.element("merge_heads", "merge_heads", "merge_heads")
    graph.element("o_proj", "linear", "o_proj", {"in_features": config.d_model, "out_features": config.d_model, "bias": False, "init_std": config.init_std}, {"in_features": "d_model", "out_features": "d_model", "init_std": "init_std"})
    graph.element("attention_residual", "add", "attention_residual")
    graph.element("post_attention_layernorm", "rms_norm", "post_attention_layernorm", {"normalized_shape": config.d_model, "eps": config.rms_eps}, {"normalized_shape": "d_model", "eps": "rms_eps"})
    graph.element("gate_proj", "linear", "gate_proj", {"in_features": config.d_model, "out_features": config.ff_size, "bias": False, "init_std": config.init_std}, {"in_features": "d_model", "out_features": "ff_size", "init_std": "init_std"})
    graph.element("up_proj", "linear", "up_proj", {"in_features": config.d_model, "out_features": config.ff_size, "bias": False, "init_std": config.init_std}, {"in_features": "d_model", "out_features": "ff_size", "init_std": "init_std"})
    graph.element("gate_silu", "silu", "gate_silu")
    graph.element("gate_product", "multiply", "gate_product")
    graph.element("down_proj", "linear", "down_proj", {"in_features": config.ff_size, "out_features": config.d_model, "bias": False, "init_std": config.init_std}, {"in_features": "ff_size", "out_features": "d_model", "init_std": "init_std"})
    graph.element("mlp_residual", "add", "mlp_residual")
    graph.edge("x_norm", "x_alias", "input_layernorm")
    for name in ("q_proj", "k_proj", "v_proj"):
        graph.edge(f"norm_{name}", "input_layernorm", name)
        graph.edge(f"{name}_split_edge", name, f"{name}_split")
    graph.edge("q_to_rope", "q_proj_split", "q_rope")
    graph.edge("k_to_rope", "k_proj_split", "k_rope")
    graph.edge("q_to_scale", "q_rope", "q_scale")
    graph.edge("k_to_transpose", "k_rope", "k_transpose")
    graph.edge("q_to_scores", "q_scale", "qk_matmul", target_port="left")
    graph.edge("k_to_scores", "k_transpose", "qk_matmul", target_port="right")
    graph.edge("scores_to_mask", "qk_matmul", "causal_mask")
    graph.edge("mask_to_softmax", "causal_mask", "attention_softmax")
    graph.edge("weights_to_value", "attention_softmax", "attention_value", target_port="left")
    graph.edge("v_to_value", "v_proj_split", "attention_value", target_port="right")
    graph.edge("value_to_merge", "attention_value", "merge_heads")
    graph.edge("merge_to_o", "merge_heads", "o_proj")
    graph.edge("o_to_residual", "o_proj", "attention_residual", target_port="right")
    graph.edge("x_to_residual", "x_alias", "attention_residual", target_port="left")
    graph.edge("residual_to_norm", "attention_residual", "post_attention_layernorm")
    graph.edge("norm_to_gate", "post_attention_layernorm", "gate_proj")
    graph.edge("norm_to_up", "post_attention_layernorm", "up_proj")
    graph.edge("gate_to_silu", "gate_proj", "gate_silu")
    graph.edge("silu_to_product", "gate_silu", "gate_product", target_port="left")
    graph.edge("up_to_product", "up_proj", "gate_product", target_port="right")
    graph.edge("product_to_down", "gate_product", "down_proj")
    graph.edge("down_to_residual", "down_proj", "mlp_residual", target_port="right")
    graph.edge("attention_to_mlp_residual", "attention_residual", "mlp_residual", target_port="left")
    return GroupDefinition(
        "llama_decoder_layer",
        "LlamaDecoderLayer",
        (GroupPort("x", Endpoint("x_alias", "input")),),
        (GroupPort("output", Endpoint("mlp_residual", "output")),),
        tuple(graph.elements),
        tuple(graph.edges),
        (),
    )


def build_self_attention_group(
    config: LlamaConfig | None = None, *, causal: bool = True, rope: bool = True, rms_norm: bool = False
) -> GroupDefinition:
    """Reusable Q/K/V attention Graph template with optional norm, RoPE and causal masking."""
    config = config or LlamaConfig(d_model=32, n_heads=4, ff_size=64, n_layers=1, seq_len=8, vocab_size=128)
    graph = _GraphBuilder()
    graph.element("x_alias", "output", "x", {"name": "x"})
    source = "x_alias"
    if rms_norm:
        graph.element("input_layernorm", "rms_norm", "input_layernorm", {"normalized_shape": config.d_model, "eps": config.rms_eps}, {"normalized_shape": "d_model", "eps": "rms_eps"})
        graph.edge("x_norm", "x_alias", "input_layernorm")
        source = "input_layernorm"
    for name in ("q_proj", "k_proj", "v_proj"):
        graph.element(name, "linear", name, {"in_features": config.d_model, "out_features": config.d_model, "bias": False, "init_std": config.init_std}, {"in_features": "d_model", "out_features": "d_model", "init_std": "init_std"})
        graph.element(f"{name}_split", "split_heads", f"{name}_split", {"n_heads": config.n_heads}, {"n_heads": "n_heads"})
        graph.edge(f"{source}_{name}", source, name)
        graph.edge(f"{name}_split_edge", name, f"{name}_split")
    if rope:
        for name in ("q", "k"):
            graph.element(f"{name}_rope", "rope", f"{name}_rope", {"base": config.rope_base}, {"base": "rope_base"})
            graph.edge(f"{name}_to_rope", f"{name}_proj_split", f"{name}_rope")
    q = "q_rope" if rope else "q_proj_split"
    k = "k_rope" if rope else "k_proj_split"
    graph.element("q_scale", "scale", "q_scale", {"factor": (config.d_model // config.n_heads) ** -0.5})
    graph.element("k_transpose", "transpose", "k_transpose", {"dim0": -2, "dim1": -1})
    graph.element("qk_matmul", "matmul", "qk_matmul")
    graph.edge("q_to_scale", q, "q_scale")
    graph.edge("k_to_transpose", k, "k_transpose")
    graph.edge("q_scores", "q_scale", "qk_matmul", target_port="left")
    graph.edge("k_scores", "k_transpose", "qk_matmul", target_port="right")
    score = "qk_matmul"
    if causal:
        graph.element("causal_mask", "causal_mask", "causal_mask")
        graph.edge("scores_mask", score, "causal_mask")
        score = "causal_mask"
    graph.element("attention_softmax", "softmax", "attention_softmax", {"dim": -1})
    graph.element("attention_value", "matmul", "attention_value")
    graph.element("merge_heads", "merge_heads", "merge_heads")
    graph.element("o_proj", "linear", "o_proj", {"in_features": config.d_model, "out_features": config.d_model, "bias": False, "init_std": config.init_std}, {"in_features": "d_model", "out_features": "d_model", "init_std": "init_std"})
    graph.edge("scores_softmax", score, "attention_softmax")
    graph.edge("weights_value", "attention_softmax", "attention_value", target_port="left")
    graph.edge("v_value", "v_proj_split", "attention_value", target_port="right")
    graph.edge("value_merge", "attention_value", "merge_heads")
    graph.edge("merge_output", "merge_heads", "o_proj")
    return GroupDefinition(
        "self_attention",
        "SelfAttention",
        (GroupPort("x", Endpoint("x_alias", "input")),),
        (GroupPort("output", Endpoint("o_proj", "output")),),
        tuple(graph.elements),
        tuple(graph.edges),
        (),
    )


def build_llama_state(
    metablock_id: str,
    name: str = "LlamaForCausalLM",
    config: LlamaConfig | None = None,
) -> PyTorchMetaBlockState:
    config = config or LlamaConfig()
    group = build_llama_decoder_group(config)
    external = _llama_parameters(config)
    forward = _GraphBuilder()
    forward.element("input_ids_input", "input", "input_ids", {"name": "input_ids", "shape": ["batch", "seq_len"], "dtype": "int64", "semantic": "token_ids"})
    forward.element("embed_tokens", "embedding", "embed_tokens", {"num_embeddings": config.vocab_size, "embedding_dim": config.d_model, "init_std": config.init_std}, {"num_embeddings": "vocab_size", "embedding_dim": "d_model", "init_std": "init_std"})
    forward.edge("tokens_embedding", "input_ids_input", "embed_tokens")
    forward.edge("embedding_layers", "embed_tokens", "layers_instance", target_port="x")
    forward.edge("layers_norm", "layers_instance", "norm")
    forward.element("norm", "rms_norm", "norm", {"normalized_shape": config.d_model, "eps": config.rms_eps}, {"normalized_shape": "d_model", "eps": "rms_eps"})
    forward.edge("norm_head", "norm", "lm_head")
    forward.element("lm_head", "linear", "lm_head", {"in_features": config.d_model, "out_features": config.vocab_size, "bias": False, "init_std": config.init_std}, {"in_features": "d_model", "out_features": "vocab_size", "init_std": "init_std"})
    forward_method = MethodGraph(
        "forward",
        (MethodInput("input_ids", ("batch", "seq_len"), "int64", "token_ids"),),
        (MethodOutput("logits", Endpoint("lm_head", "output")),),
        tuple(forward.elements), tuple(forward.edges),
        (GroupInstance("layers_instance", group.id, "layers", config.n_layers, {}),),
    )

    loss = _GraphBuilder()
    loss.element("logits_input", "input", "logits", {"name": "logits", "shape": ["batch", "seq_len", "vocab_size"], "dtype": "float32"})
    loss.element("input_ids_input", "input", "input_ids", {"name": "input_ids", "shape": ["batch", "seq_len"], "dtype": "int64", "semantic": "token_ids"})
    loss.element("loss", "cross_entropy", "loss", {"mode": "next_token"})
    loss.edge("logits_loss", "logits_input", "loss", target_port="logits")
    loss.edge("tokens_loss", "input_ids_input", "loss", target_port="target")
    loss_method = MethodGraph(
        "loss",
        (MethodInput("logits", ("batch", "seq_len", "vocab_size"), "float32"),
         MethodInput("input_ids", ("batch", "seq_len"), "int64", "token_ids")),
        (MethodOutput("loss", Endpoint("loss", "output")),),
        tuple(loss.elements), tuple(loss.edges), (),
    )

    predict = _GraphBuilder()
    predict.element("input_ids_input", "input", "input_ids", {"name": "input_ids", "shape": ["batch", "seq_len"], "dtype": "int64", "semantic": "token_ids"})
    predict.element("forward_logits", "output", "forward_logits", {}, metadata={"method_call": "forward", "arguments": ["input_ids"], "output_shape": ["batch", "seq_len", "vocab_size"], "dtype": "float32"})
    predict.element("probabilities", "softmax", "probabilities", {"dim": -1})
    predict.edge("tokens_to_forward", "input_ids_input", "forward_logits")
    predict.edge("forward_to_probabilities", "forward_logits", "probabilities")
    predict_method = MethodGraph(
        "predict",
        (MethodInput("input_ids", ("batch", "seq_len"), "int64", "token_ids"),),
        (MethodOutput("probabilities", Endpoint("probabilities", "output")),),
        tuple(predict.elements), tuple(predict.edges), (),
    )
    return PyTorchMetaBlockState(
        1,
        metablock_id,
        name,
        external,
        (group,),
        (forward_method, loss_method, predict_method),
        {"template": "llama_causal_lm", "generated_class_name": "LlamaForCausalLM", "method_returns": {"forward": "direct", "loss": "direct", "predict": "direct"}, "no_grad_methods": ["predict"]},
    )


def create_template_state(
    metablock_id: str, *, name: str = "PyTorch Model", template: str = "multihead_mlp", config: LlamaConfig | None = None
) -> PyTorchMetaBlockState:
    if template in {"llama_causal_lm", "llama_causal_lm_small"}:
        if template.endswith("_small") and config is None:
            config = LlamaConfig(vocab_size=128, d_model=32, n_heads=4, ff_size=64, n_layers=2, seq_len=8)
        return build_llama_state(metablock_id, name or "LlamaForCausalLM", config)
    if template == "llama_decoder_layer":
        config = config or LlamaConfig(vocab_size=128, d_model=32, n_heads=4, ff_size=64, n_layers=1, seq_len=8)
        group = build_llama_decoder_group(config)
        method_graph = _GraphBuilder()
        method_graph.element("input_x", "input", "x", {"name": "x", "shape": ["batch", "seq_len", "d_model"], "dtype": "float32"})
        method_graph.edge("x_to_layer", "input_x", "layer_instance", target_port="x")
        method = MethodGraph("forward", (MethodInput("x", ("batch", "seq_len", "d_model"), "float32"),), (MethodOutput("output", Endpoint("layer_instance", "output")),), tuple(method_graph.elements), tuple(method_graph.edges), (GroupInstance("layer_instance", group.id, "layer", 1, {}),))
        return PyTorchMetaBlockState(1, metablock_id, name or "LlamaDecoderLayer", _llama_parameters(config), (group,), (method,), {"template": "llama_decoder_layer"})
    if template == "self_attention":
        config = config or LlamaConfig(vocab_size=128, d_model=32, n_heads=4, ff_size=64, n_layers=1, seq_len=8)
        group = build_self_attention_group(config)
        method_graph = _GraphBuilder()
        method_graph.element("input_x", "input", "x", {"name": "x", "shape": ["batch", "seq_len", "d_model"], "dtype": "float32"})
        method_graph.edge("x_to_attention", "input_x", "attention_instance", target_port="x")
        method = MethodGraph("forward", (MethodInput("x", ("batch", "seq_len", "d_model"), "float32"),), (MethodOutput("output", Endpoint("attention_instance", "output")),), tuple(method_graph.elements), tuple(method_graph.edges), (GroupInstance("attention_instance", group.id, "attention", 1, {}),))
        return PyTorchMetaBlockState(1, metablock_id, name or "SelfAttention", _llama_parameters(config), (group,), (method,), {"template": "self_attention"})
    raise ValueError(f"unknown PyTorch model template {template!r}")
