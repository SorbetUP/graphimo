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

The current slice validates the addon package boundary, lazy entry-point
discovery, generic sidecar persistence, a basic multi-head MLP definition and
marimo materialization of a `PyTorchModelDefinition`. It does not yet provide
the complete visual PyTorch graph/compiler, reusable internal Groups,
parameter promotion, live subscription/TorchLens actions or checkpoint
migration workflow specified for the finished addon.
