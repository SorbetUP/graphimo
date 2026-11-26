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

The P2 port convention is: ordinary unary elements use `input` and `output`;
`add`, `multiply` and `matmul` use `left` and `right`; `cross_entropy` uses
`logits` and `target`; `concatenate` uses variadic `input`; a Group instance
uses the named ports declared by its referenced Group. A visual `input` element
matches its `name` parameter to a `MethodInput` of the same name. These names
fill in the port identity that the schema's conceptual definitions leave open.

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

P1 supplies the semantic schema. P2 adds the exact V1 element catalog,
structured validation, symbolic tensor shapes and shape propagation without
creating a model. `validate_model_graph(state, external_values)` reports
structural, port, cycle, parameter and binding diagnostics. The normative error
codes include `pytorch_unknown_element_kind`, `pytorch_unknown_port`,
`pytorch_edge_invalid`, `pytorch_cycle_invalid`,
`pytorch_group_cycle_invalid`, `pytorch_param_invalid`,
`pytorch_external_param_missing`, `pytorch_shape_incompatible` and
`pytorch_shape_divisibility_error`. Each diagnostic carries the MetaBlock ID;
method, element, Group and edge identities are attached when known.

`infer_model_shapes(state, external_values)` returns `ShapeInferenceResult`:
`port_shapes` and `edge_shapes` map scoped IDs to `TensorSpec`, with validation
and shape diagnostics included. It uses bounded exact symbolic matching,
simple products/quotients, broadcast rules and concrete shape constraints. It
does not construct a PyTorch model or execute user code. P3 is the compiler and
readable generated source milestone. Until that compiler supports every kind
in a state, unknown kinds must remain non-materializable.
