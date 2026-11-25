# Python nodes and instance source

Graphimo's host-independent Python API lives in the `graphimo` package.

## Define and inspect a node

`@graphimo.node` marks a function without wrapping it or registering a scheduler. `graphimo.inspect_node` returns ordered input and output descriptors. It reads source with `inspect.getsource`, parses the AST, and never evaluates annotations. Unsupported source is reported through `NodeSpec.diagnostic`.

```python
from graphimo import node


@node
def normalize(dataset, mean: float = 0.0, std: float = 1.0) -> float:
    normalized = (dataset - mean) / std
    return normalized
```

`preview_value` and `describe_result` provide bounded, JSON-safe runtime inspection. They do not persist results. Multiple outputs require a tuple with the exact statically declared arity.

## Create a marimo cell body

`build_instance_source` creates only the canonical Graphimo marker comments and direct keyword call. The host inserts this body into a marimo cell; it does not generate marimo's cell wrapper.

```python
from graphimo import build_instance_source

source, spec = build_instance_source(
    "normalize",
    {"dataset": "g_load_a19d0c21", "mean": "0.0", "std": "1.0"},
)
```

The generated body has a random eight-character lowercase hexadecimal instance ID and a result global named `g_<node>_<id>`. `parse_instance_source` recognizes only a single marked direct call, and `reconnect_instance_source` changes one existing keyword value before regenerating canonical source. Expressions must parse in Python's expression mode and are size-bounded.

Use `output_expression(spec, output_index, output_count)` to create a downstream reference: a single output uses the result global; multiple outputs use a zero-based tuple index. Port labels remain the `OutputPort` metadata from `inspect_node`.

These APIs generate source and describe contracts. Marimo remains responsible for dependency analysis and reactive execution.
