from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_PACKAGE_PATH = Path(__file__).parents[1] / "graphimo_pytorch"
_PACKAGE_NAME = "graphimo_pytorch_template_tests"
if _PACKAGE_NAME not in sys.modules:
    _package = ModuleType(_PACKAGE_NAME)
    _package.__path__ = [str(_PACKAGE_PATH)]
    _package.__package__ = _PACKAGE_NAME
    _package.__spec__ = importlib.util.spec_from_loader(_PACKAGE_NAME, loader=None, is_package=True)
    _package.__spec__.submodule_search_locations = [str(_PACKAGE_PATH)]
    sys.modules[_PACKAGE_NAME] = _package

_templates = importlib.import_module(f"{_PACKAGE_NAME}.templates")
_compiler = importlib.import_module(f"{_PACKAGE_NAME}.compiler")
_registry = importlib.import_module(f"{_PACKAGE_NAME}.registry")
LlamaConfig = _templates.LlamaConfig
create_template_state = _templates.create_template_state
compile_model = _compiler.compile_model


def test_attention_and_llama_templates_use_only_the_normative_catalog():
    assert "model_call" not in _registry.ELEMENT_REGISTRY
    attention = create_template_state("attention", template="self_attention")
    llama = create_template_state("llama", template="llama_decoder_layer")
    assert {item.name for item in attention.groups} == {"SelfAttention"}
    decoder = next(item for item in llama.groups if item.name == "LlamaDecoderLayer")
    assert {item.name for item in decoder.elements} >= {
        "input_layernorm",
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        "post_attention_layernorm",
        "gate_proj",
        "up_proj",
        "down_proj",
    }
    optional = _templates.build_self_attention_group(
        LlamaConfig(vocab_size=128, d_model=32, n_heads=4, ff_size=64, n_layers=1, seq_len=8),
        causal=False,
        rope=False,
        rms_norm=True,
    )
    optional_kinds = {item.kind for item in optional.elements}
    assert "rms_norm" in optional_kinds
    assert "causal_mask" not in optional_kinds
    assert "rope" not in optional_kinds


def test_scaled_llama_matches_independent_reference_logits_loss_and_predict():
    torch = pytest.importorskip("torch")
    nn = torch.nn
    F = torch.nn.functional
    config = LlamaConfig(vocab_size=128, d_model=32, n_heads=4, ff_size=64, n_layers=2, seq_len=8)
    state = create_template_state("llama-small", template="llama_causal_lm_small", config=config)
    compiled = compile_model(state)
    assert compiled.shape_result.valid
    torch.manual_seed(712)
    generated = compiled.factory()

    class ReferenceLayer(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_layernorm = nn.RMSNorm(32, eps=1e-5)
            self.q_proj = nn.Linear(32, 32, bias=False)
            self.k_proj = nn.Linear(32, 32, bias=False)
            self.v_proj = nn.Linear(32, 32, bias=False)
            self.o_proj = nn.Linear(32, 32, bias=False)
            self.post_attention_layernorm = nn.RMSNorm(32, eps=1e-5)
            self.gate_proj = nn.Linear(32, 64, bias=False)
            self.up_proj = nn.Linear(32, 64, bias=False)
            self.down_proj = nn.Linear(64, 32, bias=False)

        def rope(self, value):
            head_dim = value.shape[-1]
            length = value.shape[-2]
            inv_freq = 1.0 / 10000.0 ** (
                torch.arange(0, head_dim, 2, device=value.device, dtype=torch.float32) / head_dim
            )
            angles = torch.outer(
                torch.arange(length, device=value.device, dtype=torch.float32), inv_freq
            )
            angles = torch.cat((angles, angles), dim=-1)
            first, second = value[..., : head_dim // 2], value[..., head_dim // 2 :]
            rotated = torch.cat((-second, first), dim=-1)
            return value * angles.cos().to(value.dtype) + rotated * angles.sin().to(value.dtype)

        def forward(self, value):
            residual = value
            value = self.input_layernorm(value)
            query = self.rope(self.q_proj(value).unflatten(-1, (4, -1)).transpose(1, 2)) * (8**-0.5)
            key = self.rope(self.k_proj(value).unflatten(-1, (4, -1)).transpose(1, 2)).transpose(
                -2, -1
            )
            values = self.v_proj(value).unflatten(-1, (4, -1)).transpose(1, 2)
            scores = query @ key
            causal = torch.triu(torch.ones(8, 8, dtype=torch.bool, device=value.device), diagonal=1)
            probabilities = torch.softmax(scores.masked_fill(causal, float("-inf")), dim=-1)
            attention = (probabilities @ values).transpose(1, 2).flatten(2)
            value = residual + self.o_proj(attention)
            residual = value
            value = self.post_attention_layernorm(value)
            value = self.down_proj(F.silu(self.gate_proj(value)) * self.up_proj(value))
            return residual + value

    class ReferenceLlama(nn.Module):
        def __init__(self):
            super().__init__()
            self.embed_tokens = nn.Embedding(128, 32)
            self.layers = nn.ModuleList([ReferenceLayer() for _ in range(2)])
            self.norm = nn.RMSNorm(32, eps=1e-5)
            self.lm_head = nn.Linear(32, 128, bias=False)

        def forward(self, input_ids):
            value = self.embed_tokens(input_ids)
            for layer in self.layers:
                value = layer(value)
            return self.lm_head(self.norm(value))

        def loss(self, logits, input_ids):
            return F.cross_entropy(logits[:, :-1].flatten(0, -2), input_ids[:, 1:].flatten())

        @torch.no_grad()
        def predict(self, input_ids):
            return torch.softmax(self.forward(input_ids), dim=-1)

    reference = ReferenceLlama()
    reference.load_state_dict(generated.state_dict())
    input_ids = torch.randint(0, 128, (2, 8))
    generated_logits = generated(input_ids)
    reference_logits = reference(input_ids)
    assert generated_logits.shape == (2, 8, 128)
    torch.testing.assert_close(generated_logits, reference_logits, rtol=1e-5, atol=1e-6)
    torch.testing.assert_close(
        generated.loss(generated_logits, input_ids), reference.loss(reference_logits, input_ids)
    )
    torch.testing.assert_close(generated.predict(input_ids), reference.predict(input_ids))
    assert generated.predict(input_ids).shape == (2, 8, 128)
    assert "@torch.no_grad()\n    def predict" in compiled.generated_source


def test_repeated_llama_group_exposes_normative_runtime_paths_and_rejects_bad_heads():
    pytest.importorskip("torch")
    config = LlamaConfig(vocab_size=128, d_model=32, n_heads=4, ff_size=64, n_layers=2, seq_len=8)
    state = create_template_state("llama-bindings", template="llama_causal_lm_small", config=config)
    compiled = compile_model(state)
    paths = [
        site.module_path for site in compiled.bindings.sites if site.logical_element_id == "q_proj"
    ]
    assert paths == ["layers.0.q_proj", "layers.1.q_proj"]
    assert "LlamaDecoderLayer() for _ in range(2)" in compiled.generated_source
    widened_heads = compile_model(state, {"n_heads": 8})
    assert "ScaleOp((32 / 8) ** -0.5)" in widened_heads.generated_source
    widened_depth = compile_model(state, {"n_layers": 3})
    assert "LlamaDecoderLayer() for _ in range(3)" in widened_depth.generated_source
    assert any(site.module_path == "layers.2.q_proj" for site in widened_depth.bindings.sites)
    projection = importlib.import_module(f"{_PACKAGE_NAME}.workbench_projection").project_model(
        state, "forward", external_values={"n_layers": 3}
    )
    assert next(item for item in projection["elements"] if item["name"] == "layers")["repeat"] == 3
    with pytest.raises(_compiler.PyTorchCompilationError) as error:
        compile_model(state, {"n_heads": 3})
    assert any("divisible" in item.message for item in error.value.diagnostics)
    with pytest.raises(_compiler.PyTorchCompilationError) as error:
        compile_model(state, {"n_layers": 0})
    assert error.value.diagnostics[0].code == "pytorch_param_invalid"


def test_full_llama_template_compiles_without_allocating_model_weights():
    state = create_template_state("llama-full", template="llama_causal_lm")
    compiled = compile_model(state)
    assert compiled.factory.__name__ == "LlamaForCausalLM"
    assert (
        "self.layers = nn.ModuleList([LlamaDecoderLayer() for _ in range(32)])"
        in compiled.generated_source
    )
    assert any(site.module_path == "layers.31.q_proj" for site in compiled.bindings.sites)


def test_training_helper_is_an_ordinary_graphimo_node_and_trains_small_llama(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    pytest.importorskip("graphimo")
    package = importlib.import_module(f"{_PACKAGE_NAME}.training")
    from graphimo import inspect_node, is_node

    assert is_node(package.train_pytorch_model)
    contract = inspect_node(package.train_pytorch_model)
    assert contract.name == "train_pytorch_model"
    assert len(contract.outputs) == 3
    monkeypatch.setenv("GRAPHIMO_ARTIFACT_STORE", str(tmp_path / "artifact-store"))
    state = create_template_state(
        "llama-train",
        template="llama_causal_lm_small",
        config=LlamaConfig(vocab_size=64, d_model=16, n_heads=2, ff_size=32, n_layers=1, seq_len=4),
    )
    definition = importlib.import_module(f"{_PACKAGE_NAME}.model_definition").definition_from_state(
        state
    )
    tokens = torch.randint(0, 64, (2, 4))
    model, summary, artifact = package.train_pytorch_model(
        definition, [tokens, tokens], steps=2, persist_checkpoint=True
    )
    assert summary["steps"] == 2
    assert len(summary["history"]) == 2
    assert artifact.kind == "pytorch-checkpoint"
    from graphimo import artifacts

    assert artifacts.get(artifact.id) == artifact
    runtime_info = model.__graphimo_pytorch_runtime__
    events = importlib.import_module(f"{_PACKAGE_NAME}.runtime").RUNTIME_REGISTRY.event_bus.drain(
        runtime_info.metablock_id, limit=512
    )["events"]
    loss_events = [
        item
        for item in events
        if item["kind"] == "metric.scalar" and item["payload"]["name"] == "train/loss"
    ]
    assert len(loss_events) == 2
