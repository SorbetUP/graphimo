"""Symbolic tensor shapes and static PyTorch graph shape propagation."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace as replace_dataclass
from typing import Any, Mapping

from .registry import ValidationReport, get_element_kind, validate_model_graph
from .schema import (
    Diagnostic,
    ElementRecord,
    GroupDefinition,
    MethodGraph,
    PyTorchMetaBlockState,
)

JsonScalar = None | bool | int | float | str
_BINARY_EXPR = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*|\d+)\s*([*/])\s*([A-Za-z_][A-Za-z0-9_]*|\d+)$")


@dataclass(frozen=True)
class Dim:
    expr: str
    value: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.expr, str) or not self.expr:
            raise ValueError("dimension expression must be non-empty text")
        if self.value is not None and (type(self.value) is not int or self.value < 0):
            raise ValueError("dimension value must be a non-negative integer or None")


@dataclass(frozen=True)
class TensorSpec:
    shape: tuple[Dim, ...]
    dtype: str | None = None
    semantic: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.shape, tuple) or any(
            not isinstance(item, Dim) for item in self.shape
        ):
            raise ValueError("TensorSpec shape must be a tuple of Dim values")


@dataclass(frozen=True)
class ShapeInferenceResult:
    port_shapes: Mapping[str, TensorSpec]
    edge_shapes: Mapping[str, TensorSpec]
    diagnostics: tuple[Diagnostic, ...]
    validation: ValidationReport

    @property
    def valid(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)


def _dim(value: int | str, external_values: Mapping[str, JsonScalar]) -> Dim:
    if type(value) is int:
        if value == -1:
            return Dim("-1", None)
        if value < 0:
            raise ValueError(f"negative tensor dimension {value}")
        return Dim(str(value), value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid tensor dimension {value!r}")
    expression = value.strip()
    if expression in external_values and type(external_values[expression]) is int:
        number = external_values[expression]
        return Dim(expression, number)
        if expression.isdecimal():
            return Dim(expression, int(expression))
    match = _BINARY_EXPR.fullmatch(expression)
    if match:
        left, operator, right = match.groups()
        left_value = _resolve_atom(left, external_values)
        right_value = _resolve_atom(right, external_values)
        if left_value is not None and right_value is not None:
            if operator == "*":
                return Dim(expression, left_value * right_value)
            if right_value and left_value % right_value == 0:
                return Dim(expression, left_value // right_value)
        return Dim(expression)
    return Dim(expression)


def _resolve_atom(atom: str, values: Mapping[str, JsonScalar]) -> int | None:
    if atom.isdecimal():
        return int(atom)
    value = values.get(atom)
    return value if type(value) is int else None


def _same_dimension(left: Dim, right: Dim) -> bool:
    return left.expr == right.expr or (
        left.value is not None and right.value is not None and left.value == right.value
    )


def _broadcast(left: tuple[Dim, ...], right: tuple[Dim, ...]) -> tuple[Dim, ...]:
    result: list[Dim] = []
    width = max(len(left), len(right))
    padded_left = (Dim("1", 1),) * (width - len(left)) + left
    padded_right = (Dim("1", 1),) * (width - len(right)) + right
    for a, b in zip(padded_left, padded_right, strict=True):
        if _same_dimension(a, b):
            result.append(a if a.value is not None else b)
        elif a.value == 1:
            result.append(b)
        elif b.value == 1:
            result.append(a)
        else:
            raise ValueError(f"cannot broadcast dimensions {a.expr} and {b.expr}")
    return tuple(result)


def _product(values: tuple[Dim, ...]) -> Dim:
    if all(item.value is not None for item in values):
        concrete = math.prod(item.value for item in values if item.value is not None)
        return Dim(str(concrete), concrete)
    return Dim("*".join(item.expr for item in values))


def _normalize_axis(axis: int, rank: int) -> int:
    normalized = axis + rank if axis < 0 else axis
    if normalized < 0 or normalized >= rank:
        raise ValueError(f"dimension {axis} is out of range for rank {rank}")
    return normalized


def _expect_count(inputs: Mapping[str, tuple[TensorSpec, ...]], ports: tuple[str, ...]) -> None:
    absent = [port for port in ports if not inputs.get(port)]
    if absent:
        raise ValueError(f"missing tensor input(s): {', '.join(absent)}")


def _infer_element(
    element: ElementRecord,
    inputs: Mapping[str, tuple[TensorSpec, ...]],
    external_values: Mapping[str, JsonScalar],
    method_inputs: Mapping[str, TensorSpec],
) -> tuple[TensorSpec, ...]:
    kind = element.kind
    params = element.params

    def dim(value):
        return _dim(value, external_values)

    if kind == "input":
        name = params.get("name", element.name)
        if name in method_inputs:
            return (method_inputs[name],)
        shape = tuple(dim(item) for item in params["shape"])
        return (TensorSpec(shape, params.get("dtype", "float32"), params.get("semantic")),)
    if kind == "output":
        _expect_count(inputs, ("input",))
        if element.metadata.get("method_call") == "forward":
            return (
                TensorSpec(
                    tuple(dim(value) for value in element.metadata.get("output_shape", ())),
                    element.metadata.get("dtype", "float32"),
                ),
            )
        return inputs["input"][:1]
    if kind == "group_instance":
        return ()
    if kind == "embedding":
        _expect_count(inputs, ("input",))
        source = inputs["input"][0]
        if source.dtype not in {"int32", "int64"}:
            raise ValueError("embedding input must have an integer dtype")
        return (
            TensorSpec(
                source.shape + (dim(params.get("embedding_dim", 1)),), "float32", source.semantic
            ),
        )
    if kind == "linear":
        _expect_count(inputs, ("input",))
        source = inputs["input"][0]
        if not source.shape:
            raise ValueError("linear input must have at least one dimension")
        expected = dim(params.get("in_features", 1))
        if not _same_dimension(source.shape[-1], expected):
            raise ValueError(
                f"linear expects last dimension {expected.expr}, got {source.shape[-1].expr}"
            )
        return (
            TensorSpec(
                source.shape[:-1] + (dim(params.get("out_features", 1)),),
                source.dtype,
                source.semantic,
            ),
        )
    if kind == "rms_norm":
        _expect_count(inputs, ("input",))
        source = inputs["input"][0]
        if not source.shape or not _same_dimension(
            source.shape[-1], dim(params.get("normalized_shape", 1))
        ):
            raise ValueError("rms_norm normalized_shape does not match the input's last dimension")
        return (source,)
    if kind in {"dropout", "relu", "gelu", "silu", "softmax", "scale", "rope", "causal_mask"}:
        _expect_count(inputs, ("input",))
        source = inputs["input"][0]
        if kind == "softmax":
            _normalize_axis(params.get("dim", -1), len(source.shape))
        if (
            kind == "rope"
            and source.shape
            and source.shape[-1].value is not None
            and source.shape[-1].value % 2
        ):
            raise ValueError("rope requires an even last dimension")
        if kind == "causal_mask" and (
            len(source.shape) < 2 or not _same_dimension(source.shape[-1], source.shape[-2])
        ):
            raise ValueError(
                "causal_mask expects square attention scores in the last two dimensions"
            )
        return (source,)
    if kind == "reshape":
        _expect_count(inputs, ("input",))
        source = inputs["input"][0]
        target = [dim(item) for item in params.get("target_shape", ())]
        inferred = [index for index, item in enumerate(target) if item.expr == "-1"]
        if len(inferred) > 1:
            raise ValueError("reshape supports at most one inferred -1 dimension")
        source_product = _product(source.shape)
        known_product = _product(tuple(item for item in target if item.expr != "-1"))
        if source_product.value is not None and known_product.value is not None:
            if inferred:
                if known_product.value == 0 or source_product.value % known_product.value:
                    raise ValueError(
                        "reshape element count is not divisible by the specified dimensions"
                    )
                index = inferred[0]
                target[index] = Dim(
                    str(source_product.value // known_product.value),
                    source_product.value // known_product.value,
                )
            elif source_product.value != known_product.value:
                raise ValueError("reshape must preserve the number of elements")
        elif inferred:
            target[inferred[0]] = Dim("-1")
        return (TensorSpec(tuple(target), source.dtype, source.semantic),)
    if kind == "transpose":
        _expect_count(inputs, ("input",))
        source = inputs["input"][0]
        axis0 = _normalize_axis(params.get("dim0", 0), len(source.shape))
        axis1 = _normalize_axis(params.get("dim1", 1), len(source.shape))
        result = list(source.shape)
        result[axis0], result[axis1] = result[axis1], result[axis0]
        return (TensorSpec(tuple(result), source.dtype, source.semantic),)
    if kind == "flatten":
        _expect_count(inputs, ("input",))
        source = inputs["input"][0]
        if not source.shape:
            return (source,)
        start = _normalize_axis(params.get("start_dim", 0), len(source.shape))
        end = _normalize_axis(params.get("end_dim", -1), len(source.shape))
        if end < start:
            raise ValueError("flatten end_dim must be >= start_dim")
        return (
            TensorSpec(
                source.shape[:start]
                + (_product(source.shape[start : end + 1]),)
                + source.shape[end + 1 :],
                source.dtype,
                source.semantic,
            ),
        )
    if kind == "unflatten":
        _expect_count(inputs, ("input",))
        source = inputs["input"][0]
        axis = _normalize_axis(params.get("dim", -1), len(source.shape))
        sizes = tuple(dim(item) for item in params.get("sizes", ()))
        original = source.shape[axis]
        requested = _product(sizes)
        if (
            original.value is not None
            and requested.value is not None
            and original.value != requested.value
        ):
            raise ValueError("unflatten size product must equal the selected dimension")
        return (
            TensorSpec(
                source.shape[:axis] + sizes + source.shape[axis + 1 :],
                source.dtype,
                source.semantic,
            ),
        )
    if kind == "split_heads":
        _expect_count(inputs, ("input",))
        source = inputs["input"][0]
        if len(source.shape) != 3:
            raise ValueError("split_heads expects input shape (e, t, d)")
        heads = params.get("n_heads", 1)
        width = source.shape[-1]
        if width.value is not None and width.value % heads:
            raise ValueError(f"last dimension {width.value} must be divisible by n_heads {heads}")
        head_dim = Dim(
            f"{width.expr}/{heads}", width.value // heads if width.value is not None else None
        )
        return (
            TensorSpec(
                (source.shape[0], Dim(str(heads), heads), source.shape[1], head_dim),
                source.dtype,
                source.semantic,
            ),
        )
    if kind == "merge_heads":
        _expect_count(inputs, ("input",))
        source = inputs["input"][0]
        if len(source.shape) != 4:
            raise ValueError("merge_heads expects input shape (e, h, t, dh)")
        merged = _product((source.shape[1], source.shape[3]))
        return (
            TensorSpec((source.shape[0], source.shape[2], merged), source.dtype, source.semantic),
        )
    if kind in {"add", "multiply"}:
        _expect_count(inputs, ("left", "right"))
        left, right = inputs["left"][0], inputs["right"][0]
        return (TensorSpec(_broadcast(left.shape, right.shape), left.dtype or right.dtype),)
    if kind == "concatenate":
        tensors = inputs.get("input", ())
        if len(tensors) < 2:
            raise ValueError("concatenate requires at least two input tensors")
        first = tensors[0]
        axis = _normalize_axis(params.get("dim", -1), len(first.shape))
        shape = list(first.shape)
        values = [first.shape[axis]]
        for tensor in tensors[1:]:
            if len(tensor.shape) != len(shape):
                raise ValueError("concatenate inputs must have equal rank")
            for index, (a, b) in enumerate(zip(first.shape, tensor.shape, strict=True)):
                if index != axis and not _same_dimension(a, b):
                    raise ValueError(f"concatenate dimension {index} is incompatible")
            values.append(tensor.shape[axis])
        if all(item.value is not None for item in values):
            total = sum(item.value for item in values if item.value is not None)
            shape[axis] = Dim(str(total), total)
        else:
            shape[axis] = Dim("+".join(item.expr for item in values))
        return (TensorSpec(tuple(shape), first.dtype),)
    if kind == "matmul":
        _expect_count(inputs, ("left", "right"))
        left, right = inputs["left"][0], inputs["right"][0]
        if not left.shape or not right.shape:
            raise ValueError("matmul inputs must have rank at least one")
        if len(left.shape) == 1:
            left_batch, left_m, left_k = (), None, left.shape[0]
        else:
            left_batch, left_m, left_k = left.shape[:-2], left.shape[-2], left.shape[-1]
        if len(right.shape) == 1:
            right_batch, right_k, right_n = (), right.shape[0], None
        else:
            right_batch, right_k, right_n = right.shape[:-2], right.shape[-2], right.shape[-1]
        if not _same_dimension(left_k, right_k):
            raise ValueError(
                f"matmul inner dimensions {left_k.expr} and {right_k.expr} are incompatible"
            )
        batch = _broadcast(left_batch, right_batch)
        result = list(batch)
        if left_m is not None:
            result.append(left_m)
        if right_n is not None:
            result.append(right_n)
        return (TensorSpec(tuple(result), left.dtype or right.dtype),)
    if kind == "cross_entropy":
        _expect_count(inputs, ("logits", "target"))
        logits, targets = inputs["logits"][0], inputs["target"][0]
        if params.get("mode", "generic") == "next_token":
            if len(logits.shape) < 2 or len(targets.shape) < 2:
                raise ValueError(
                    "next_token cross_entropy expects batched sequence logits and token IDs"
                )
        elif not logits.shape or len(targets.shape) not in {
            len(logits.shape),
            len(logits.shape) - 1,
        }:
            raise ValueError("generic cross_entropy logits/target ranks are incompatible")
        return (TensorSpec((), "float32", "loss"),)
    raise ValueError(f"shape rule is not implemented for element kind {kind!r}")


def infer_model_shapes(
    state: PyTorchMetaBlockState,
    external_values: Mapping[str, JsonScalar] | None = None,
) -> ShapeInferenceResult:
    """Validate and infer output/edge TensorSpecs without constructing a model."""
    values = dict(external_values or {})
    validation = validate_model_graph(state, values)
    diagnostics = list(validation.diagnostics)
    port_shapes: dict[str, TensorSpec] = {}
    edge_shapes: dict[str, TensorSpec] = {}
    groups = {group.id: group for group in state.groups}
    shape_values = dict(values)
    for parameter in state.external_parameters:
        shape_values[parameter.name] = shape_values.get(parameter.id, parameter.default)

    def infer_graph(graph: MethodGraph | GroupDefinition, scope: str, group_inputs=None):
        group_inputs = group_inputs or {}
        element_map = {element.id: element for element in graph.elements}
        instance_map = {instance.id: instance for instance in graph.group_instances}
        nodes = set(element_map) | set(instance_map)
        outgoing: dict[str, set[str]] = {node: set() for node in nodes}
        indegree = {node: 0 for node in nodes}
        edges_by_target: dict[tuple[str, str], list[Any]] = {}
        for edge in graph.edges:
            if edge.source.element_id not in nodes or edge.target.element_id not in nodes:
                continue
            if edge.target.element_id not in outgoing[edge.source.element_id]:
                outgoing[edge.source.element_id].add(edge.target.element_id)
                indegree[edge.target.element_id] += 1
            edges_by_target.setdefault((edge.target.element_id, edge.target.port), []).append(edge)
        ready = sorted(node for node, count in indegree.items() if count == 0)
        order: list[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for neighbor in sorted(outgoing[current]):
                indegree[neighbor] -= 1
                if indegree[neighbor] == 0:
                    ready.append(neighbor)
                    ready.sort()
        if len(order) != len(nodes):
            return {}, {}
        produced: dict[tuple[str, str], TensorSpec] = {}
        per_node: dict[str, dict[str, tuple[TensorSpec, ...]]] = {}
        failed_nodes: set[str] = set()
        method_inputs = {
            item.name: TensorSpec(
                tuple(_dim(part, shape_values) for part in item.shape), item.dtype, item.semantic
            )
            for item in (graph.inputs if isinstance(graph, MethodGraph) else ())
        }
        for node in order:
            if node in element_map:
                element = element_map[node]
                if any(
                    edge.source.element_id in failed_nodes
                    for edge in graph.edges
                    if edge.target.element_id == node
                ):
                    failed_nodes.add(node)
                    continue
                inputs: dict[str, list[TensorSpec]] = {}
                for edge in graph.edges:
                    if edge.target.element_id != node:
                        continue
                    value = produced.get((edge.source.element_id, edge.source.port))
                    if value is not None:
                        inputs.setdefault(edge.target.port, []).append(value)
                for (target_id, port), spec in group_inputs.items():
                    if target_id == node:
                        inputs.setdefault(port, []).append(spec)
                try:
                    resolved_params = dict(element.params)
                    for parameter_name, binding in element.parameter_bindings.items():
                        if binding.external_parameter_id in shape_values:
                            resolved_params[parameter_name] = shape_values[
                                binding.external_parameter_id
                            ]
                    resolved_element = replace_dataclass(element, params=resolved_params)
                    outputs = _infer_element(
                        resolved_element,
                        {key: tuple(items) for key, items in inputs.items()},
                        shape_values,
                        method_inputs,
                    )
                    descriptor = get_element_kind(element.kind)
                    output_names = (
                        tuple(port.name for port in descriptor.output_ports)
                        if descriptor
                        else ("output",)
                    )
                    for port, spec in zip(output_names, outputs):
                        produced[(node, port)] = spec
                        port_shapes[f"{scope}/{node}/{port}"] = spec
                except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
                    failed_nodes.add(node)
                    diagnostics.append(
                        Diagnostic(
                            (
                                "pytorch_shape_divisibility_error"
                                if "divisible" in str(exc)
                                else "pytorch_shape_incompatible"
                            ),
                            str(exc),
                            method_name=scope,
                            element_id=node,
                        )
                    )
            else:
                instance = instance_map[node]
                definition = groups.get(instance.group_id)
                if definition is None:
                    continue
                provided = {}
                for port in definition.inputs:
                    for edge in graph.edges:
                        if edge.target.element_id == node and edge.target.port == port.name:
                            spec = produced.get((edge.source.element_id, edge.source.port))
                            if spec is not None:
                                provided[(port.endpoint.element_id, port.endpoint.port)] = spec
                group_outputs = {}
                for repeat_index in range(instance.repeat):
                    group_outputs, _ = infer_graph(
                        definition,
                        f"{scope}/{instance.name}/repeat-{repeat_index + 1}",
                        provided,
                    )
                    output_by_name = {
                        port.name: group_outputs.get((port.endpoint.element_id, port.endpoint.port))
                        for port in definition.outputs
                    }
                    if any(value is None for value in output_by_name.values()):
                        failed_nodes.add(node)
                        break
                    if repeat_index + 1 < instance.repeat:
                        if len(definition.inputs) == len(definition.outputs) == 1:
                            output_by_name[definition.inputs[0].name] = next(
                                iter(output_by_name.values())
                            )
                        provided = {
                            (port.endpoint.element_id, port.endpoint.port): output_by_name[
                                port.name
                            ]
                            for port in definition.inputs
                            if output_by_name.get(port.name) is not None
                        }
                for port in definition.outputs:
                    spec = group_outputs.get((port.endpoint.element_id, port.endpoint.port))
                    if spec is not None:
                        produced[(node, port.name)] = spec
                        port_shapes[f"{scope}/{node}/{port.name}"] = spec
        for edge in graph.edges:
            spec = produced.get((edge.source.element_id, edge.source.port))
            if spec is not None:
                edge_shapes[f"{scope}/{edge.id}"] = spec
        return produced, per_node

    for method in state.entrypoints:
        produced, _ = infer_graph(method, method.name)
        for output in method.outputs:
            spec = produced.get((output.endpoint.element_id, output.endpoint.port))
            if spec is None:
                diagnostics.append(
                    Diagnostic(
                        "pytorch_shape_output_unresolved",
                        f"could not infer method output {output.name!r}",
                        method_name=method.name,
                    )
                )
    diagnostics = _dedupe_repeated_shape_diagnostics(diagnostics)
    diagnostics = [
        item
        if item.metablock_id is not None
        else replace_dataclass(item, metablock_id=state.metablock_id)
        for item in diagnostics
    ]
    return ShapeInferenceResult(port_shapes, edge_shapes, tuple(diagnostics), validation)


def _dedupe_repeated_shape_diagnostics(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """Collapse identical errors from repeated instances of one visual Group."""
    unique: list[Diagnostic] = []
    seen: set[tuple[Any, ...]] = set()
    repeat_marker = re.compile(r"/repeat-\d+")
    for item in diagnostics:
        owner = repeat_marker.sub("/repeat-*", item.method_name or "")
        key = (item.code, item.message, item.severity, owner, item.element_id, item.edge_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique
