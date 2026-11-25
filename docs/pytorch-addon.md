# PyTorch MetaBlock addon

`graphimo-pytorch` lives at `addons/pytorch` and builds as its own Python
distribution. It registers in `graphimo.metablocks`; Graphimo Core does not
import Torch or TorchLens. Package metadata follows the addon contract:
Python 3.11+ (the current Graphimo Core floor), `torch>=2.6`, and
`torchlens==2.34.1`, with no CUDA wheel pinned.

## P1 delivered

- `graphimo_pytorch.registration.registration` declares dialect
  `pytorch.model`, API version 1 and a factory callable.
- The semantic schema includes MetaBlock state, external parameters, method
  graphs, element/edge records, reusable groups and repeated group instances.
- `save_state` writes human-readable sorted UTF-8 JSON atomically;
  `load_state` validates V1; `canonical_state_json` and `graph_revision`
  produce stable SHA-256 revisions from semantic fields.
- Unknown element kinds keep their JSON schema fields and remain serializable.
  The later registry/compiler milestone will diagnose them as unmaterializable.
- Schema and registration imports do not import `torch` or `torchlens`.

The first schema milestone does not claim an interactive PyTorch editor or
model materializer. Core §31 says the stable host handler API is not frozen;
the factory remains a private registration marker until that interface is
specified. P1 does not add handler methods to Core.

## Open normative conflict

Core §32 marks the exported PyTorch model value as a human decision among an
instantiated module, class/factory or explicit handle. This addon contract
§7 fixes the value as `PyTorchModelDefinition`. Those instructions conflict.
No parent model port or materializer dependent on that choice is implemented.

For implementation order, use P1 through P8 in
`Graphimo_PYTORCH_PLUGIN_IMPLEMENTATION.md` after resolving only the
decisions each stage depends on. The addon remains independent from the
Graphimo Core scheduler and contains all Torch-specific semantics.
