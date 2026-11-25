# PyTorch addon

The PyTorch addon is a separate `graphimo-pytorch` distribution. Graphimo Core
discovers it through the `graphimo.metablocks` entry-point group; Core does not
interpret PyTorch elements or model state. The addon package is under
`addons/pytorch/graphimo_pytorch`.

## Semantic state schema

`graphimo_pytorch.schema` defines immutable V1 dataclasses and JSON helpers:

- `PyTorchMetaBlockState` holds the schema version, MetaBlock identity and name,
  external parameters, reusable groups, method entrypoints and semantic
  metadata. V1 requires a `forward` method.
- `MethodGraph` holds ordered inputs, outputs, elements, edges and Group
  instances. `loss` and `predict` are supported as additional entrypoints.
- `GroupDefinition` holds its ports, elements, edges and nested Group
  instances. `GroupInstance.repeat` must be at least one.
- `ElementRecord` preserves the addon-owned `kind`, parameters, external
  parameter bindings and JSON-safe metadata. Unknown kinds round-trip unchanged;
  support checks and materialization are the registry/compiler's responsibility.
- `ExternalParameter` supports `int`, `float`, `bool` and `str` defaults.
- `Diagnostic` carries a stable code, message, severity and optional graph
  identity fields. `PyTorchSchemaError.code` carries stable schema error codes.

The specification names `MethodInput`, `MethodOutput` and `GroupPort` without
defining their fields. This implementation uses the minimal tensor contract
`MethodInput(name, shape, dtype, semantic=None)`, plus named endpoint references
for `MethodOutput` and `GroupPort`. A shape dimension is a positive integer or a
symbolic string. This is a schema representation choice, not a product decision;
shape inference and compatibility validation belong to the later registry
milestone.

Use `load_state(path)` and `save_state(path, state)` for project sidecars.
Writes are UTF-8, indented, flushed, fsynced where supported, and atomically
replaced. A schema version other than `1` raises
`PyTorchSchemaError(code="pytorch_state_schema_unsupported")`. Invalid JSON or
invalid state raises `pytorch_state_invalid`.

`canonical_state_json(state)` emits deterministic semantic JSON, and
`graph_revision(state)` is its SHA-256 digest. Canvas/editor metadata such as
layout, renderer state, selection, zoom and pan is omitted from the serialized
semantic metadata and revision; Graphimo's presentation store owns that data.
Ordered scientific lists remain ordered, so element, edge, method and repeat
changes affect the revision.

## Current milestone boundary

The schema is the P1 foundation. The current legacy MLP dialect registration
and prototype are still present; P1 does not claim that they compile arbitrary
schema graphs. Element registry/validation and shape inference are P2, and
readable PyTorch source compilation is P3. Do not treat a state containing an
unknown element kind as materializable until the registry supports every kind
in that state.
