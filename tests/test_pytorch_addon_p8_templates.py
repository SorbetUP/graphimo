from __future__ import annotations

from dataclasses import replace

import torch
from torch import nn
from torch.nn import functional as F

from graphimo_pytorch import (
    LlamaTemplateConfig,
    compile_model,
    estimate_llama_parameters,
    llama_causal_lm_template,
    llama_decoder_layer_template,
    mlp_template,
    project_workbench,
    self_attention_template,
    validate_model_graph,
)


class _ReferenceDecoderLayer(nn.Module):
    def __init__(self, *, d: int, n_head: int, ff: int, eps: float, init_std: float, rope_base: float):
        super().__init__()
        self.input_layernorm = nn.RMSNorm(d, eps=eps)
        self.q_proj = nn.Linear(d, d, bias=False)
        self.k_proj = nn.Linear(d, d, bias=False)
        self.v_proj = nn.Linear(d, d, bias=False)
        self.o_proj = nn.Linear(d, d, bias=False)
        self.post_attention_layernorm = nn.RMSNorm(d, eps=eps)
        self.gate_proj = nn.Linear(d, ff, bias=False)
        self.up_proj = nn.Linear(d, ff, bias=False)
        self.down_proj = nn.Linear(ff, d, bias=False)
        self.n_head = n_head
        self.rope_base = rope_base
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.normal_(module.weight, mean=0.0, std=init_std)

    def _rope(self, x: torch.Tensor) -> torch.Tensor:
        sequence, width = x.shape[-2:]
        inverse = 1.0 / self.rope_base ** (
            torch.arange(0, width, 2, device=x.device, dtype=torch.float32) / width
        )
        angles = torch.outer(torch.arange(sequence, device=x.device, dtype=torch.float32), inverse)
        angles = torch.cat((angles, angles), dim=-1)
        left, right = x[..., : width // 2], x[..., width // 2 :]
        rotated = torch.cat((-right, left), dim=-1)
        return x * angles.cos().to(x.dtype) + rotated * angles.sin().to(x.dtype)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        batch, sequence, width = hidden_states.shape
        head_width = width // self.n_head
        normalized = self.input_layernorm(hidden_states)

        def split(value: torch.Tensor) -> torch.Tensor:
            return value.reshape(batch, sequence, self.n_head, head_width).transpose(1, 2)

        query = self._rope(split(self.q_proj(normalized))) * head_width**-0.5
        key = self._rope(split(self.k_proj(normalized))).transpose(-1, -2)
        value = split(self.v_proj(normalized))
        scores = query @ key
        mask = torch.triu(torch.ones((sequence, sequence), dtype=torch.bool, device=scores.device), diagonal=1)
        weights = scores.masked_fill(mask, float("-inf")).softmax(dim=-1)
        attended = (weights @ value).transpose(1, 2).flatten(2)
        hidden_states = hidden_states + self.o_proj(attended)
        normalized = self.post_attention_layernorm(hidden_states)
        hidden_states = hidden_states + self.down_proj(F.silu(self.gate_proj(normalized)) * self.up_proj(normalized))
        return hidden_states


class _ReferenceLlama(nn.Module):
    def __init__(self, config: LlamaTemplateConfig):
        super().__init__()
        self.embed_tokens = nn.Embedding(config.vocab_size, config.d)
        nn.init.normal_(self.embed_tokens.weight, mean=0.0, std=config.init_std)
        self.layers = nn.ModuleList([
            _ReferenceDecoderLayer(
                d=config.d,
                n_head=config.n_head,
                ff=config.ff,
                eps=config.rms_eps,
                init_std=config.init_std,
                rope_base=config.rope_base,
            )
            for _ in range(config.n_layers)
        ])
        self.norm = nn.RMSNorm(config.d, eps=config.rms_eps)
        self.lm_head = nn.Linear(config.d, config.vocab_size, bias=False)
        nn.init.normal_(self.lm_head.weight, mean=0.0, std=config.init_std)

    def forward(self, input_ids: torch.Tensor) -> torch.Tensor:
        hidden_states = self.embed_tokens(input_ids)
        for layer in self.layers:
            hidden_states = layer(hidden_states)
        return self.lm_head(self.norm(hidden_states))

    def loss(self, logits: torch.Tensor, input_ids: torch.Tensor) -> torch.Tensor:
        return F.cross_entropy(logits[:, :-1].flatten(0, -2), input_ids[:, 1:].flatten())

    @torch.no_grad()
    def predict(self, input_ids: torch.Tensor) -> torch.Tensor:
        return self.forward(input_ids).softmax(dim=-1)


def _scaled_config() -> LlamaTemplateConfig:
    return LlamaTemplateConfig(
        vocab_size=128, d=32, n_head=4, ff=64, n_layers=2, seq_len_for_shape_preview=8,
    )


def test_p8_template_catalog_creates_editable_graph_records() -> None:
    mlp = mlp_template(input_size=4, hidden=8, output_size=2)
    attention = self_attention_template(d_model=8, n_heads=2)
    decoder = llama_decoder_layer_template(LlamaTemplateConfig(vocab_size=16, d=8, n_head=2, ff=16, n_layers=1))

    assert {item.kind for item in mlp.entrypoints[0].elements} >= {"linear", "relu"}
    assert "self-attention" in {group.id for group in attention.groups}
    decoder_group = decoder.groups[0]
    assert {item.name for item in decoder_group.elements} >= {
        "input_layernorm", "q_proj", "k_proj", "v_proj", "o_proj", "post_attention_layernorm",
        "gate_proj", "up_proj", "down_proj",
    }


def test_p8_scaled_llama_matches_direct_reference_for_forward_loss_and_predict() -> None:
    config = _scaled_config()
    result = compile_model(llama_causal_lm_template(config))
    generated = result.definition.instantiate()
    reference = _ReferenceLlama(config)
    assert set(generated.state_dict()) == set(reference.state_dict())
    generated.load_state_dict(reference.state_dict())

    input_ids = torch.tensor([[1, 4, 7, 2, 9, 3, 6, 5], [2, 3, 1, 8, 4, 5, 7, 9]])
    expected_logits = reference(input_ids)
    actual_logits = generated(input_ids)
    torch.testing.assert_close(actual_logits, expected_logits, rtol=1e-5, atol=1e-6)
    torch.testing.assert_close(generated.loss(actual_logits, input_ids), reference.loss(expected_logits, input_ids))
    torch.testing.assert_close(generated.predict(input_ids), reference.predict(input_ids))

    assert actual_logits.shape == (2, 8, 128)
    assert result.shape_result.output_specs["method_output:logits"].shape[-1].value == 128
    assert result.bindings.for_element("q_proj")[0].module_path == "layers.0.q_proj"
    assert result.bindings.for_element("q_proj")[1].module_path == "layers.1.q_proj"
    assert "@torch.no_grad()\n    def predict(self, input_ids):" in result.generated_source
    assert "= self.forward(" in result.generated_source


def test_p8_workbench_projection_exposes_repeated_decoder_group_without_expanding_it() -> None:
    state = llama_causal_lm_template(_scaled_config())
    compiled = compile_model(state)
    projection = project_workbench(state, method_name="forward", compile_result=compiled)
    layer_card = next(card for card in projection.cards if card.element_id == "layers")

    assert layer_card.kind == "group_instance"
    assert layer_card.name == "layers"
    assert projection.group_navigation[0].name == "LlamaDecoderLayer"
    assert projection.group_navigation[0].repeat_count == 2
    assert projection.method_tabs == ("forward", "loss", "predict")
    assert projection.generated_source == compiled.generated_source
    assert "method_call" not in {item.kind for item in projection.palette}

    group_id = state.groups[0].id
    inner = project_workbench(state, active_group_id=group_id)
    assert inner.active_scope_name == "LlamaDecoderLayer"
    inner_by_name = {card.name: card for card in inner.cards}
    assert set(inner_by_name) >= {
        "input_layernorm", "q_proj", "k_proj", "v_proj", "o_proj", "post_attention_layernorm",
        "gate_proj", "up_proj", "down_proj",
    }
    assert inner_by_name["q_proj"].output_shapes[0][1].shape[-1].value == 32
    assert inner_by_name["q_proj"].input_shapes[0][1].shape[1].value == 8
    assert any(edge.tensor is not None for edge in inner.edges)

    loss_view = project_workbench(state, method_name="loss")
    predict_view = project_workbench(state, method_name="predict")
    assert {card.name for card in loss_view.cards} >= {"cross_entropy"}
    assert {card.name for card in predict_view.cards} >= {"forward_call", "probabilities"}


def test_p8_full_llama_template_estimates_without_allocating_weights() -> None:
    state = llama_causal_lm_template()
    generated = compile_model(state)
    estimate = estimate_llama_parameters()
    assert state.metadata["parameter_estimate"]["total_parameters"] == estimate.total_parameters
    assert estimate.total_parameters > 6_000_000_000
    assert estimate.parameter_bytes == estimate.total_parameters * 4
    assert any(instance.repeat == 32 for instance in state.entrypoints[0].group_instances)
    assert "range(32)" in generated.generated_source
    assert "class LlamaForCausalLM(nn.Module):" in generated.generated_source


def test_internal_method_call_rejects_recursive_method_graphs() -> None:
    state = llama_causal_lm_template(_scaled_config())
    predict = state.entrypoints[2]
    elements = tuple(
        replace(element, params={**element.params, "method": "predict"})
        if element.kind == "method_call" else element
        for element in predict.elements
    )
    recursive_predict = replace(predict, elements=elements)
    malformed = replace(state, entrypoints=(*state.entrypoints[:2], recursive_predict))

    report = validate_model_graph(malformed)
    assert any(item.code == "pytorch_method_call_recursive" for item in report.diagnostics)

