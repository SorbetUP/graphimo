# graphimo-pytorch

`graphimo-pytorch` is an independently installable Graphimo MetaBlock addon.
It registers the `pytorch.model` dialect through the `graphimo.metablocks`
entry-point group and depends on PyTorch and TorchLens; Graphimo Core does not
import either dependency.

Install Graphimo Core first, then the addon:

```bash
python -m pip install -e ../..
python -m pip install -e .
```

The addon provides semantic model graphs, a static PyTorch element catalog and
shape inference, readable generated models, reusable/repeated Groups, a
standalone Workbench, process-local runtime telemetry, checkpoint/migration
support and selective TorchLens inspection. The Workbench includes an MLP,
Self Attention, Llama Decoder Layer, full-size Llama Causal LM editing fixture
and a reduced runnable Llama fixture.

```python
from graphimo_pytorch import create_template_state, definition_from_state
from graphimo_pytorch import train_pytorch_model

state = create_template_state("llama-demo", template="llama_causal_lm_small")
model_def = definition_from_state(state)
model, history, checkpoint = train_pytorch_model(model_def, token_batches, steps=20)
```

`train_pytorch_model` is an ordinary function marked with Graphimo Core's
`@node`; it does not schedule nodes. Checkpoint creation is explicit through
`persist_checkpoint=True`. The full 32-layer model is intended for canvas,
shape and code-generation review; the reduced fixture is intended for
numerical tests and interactive training.
