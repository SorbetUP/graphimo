"""Static TensorSpec propagation for the V1 PyTorch MetaBlock dialect."""

from __future__ import annotations

from dataclasses import dataclass
from math import prod
from typing import Mapping

from .diagnostics import Diagnostic
from .schema import GroupDefinition, MethodGraph, PyTorchMetaBlockState, TensorContract
from .validation import validate_model_graph


@dataclass(frozen=True, slots=True)
class Dim:
    expr: str
    value: int | None = None


@dataclass(frozen=True, slots=True)
class TensorSpec:
    shape: tuple[Dim, ...]
    dtype: str | None
    semantic: str | None = None


@dataclass(frozen=True, slots=True)
class ShapeInferenceResult:
    output_specs: Mapping[str, TensorSpec]
    edge_specs: Mapping[str, TensorSpec]
    diagnostics: tuple[Diagnostic, ...]

    @property
    def valid(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)


def tensor_spec(contract: TensorContract) -> TensorSpec:
    return TensorSpec(tuple(_dim(token) for token in contract.shape), contract.dtype, contract.semantic)


def infer_model_shapes(
    state: PyTorchMetaBlockState,
    external_values: Mapping[str, object] | None = None,
) -> ShapeInferenceResult:
    """Validate and propagate declared shapes through methods and Groups."""
    diagnostics = list(validate_model_graph(state, external_values).diagnostics)
    groups = {group.id: group for group in state.groups}
    external = {item.id: item.default for item in state.external_parameters}
    if external_values:
        external.update(external_values)
    all_outputs: dict[str, TensorSpec] = {}
    all_edges: dict[str, TensorSpec] = {}
    visiting: set[str] = set()
    method_outputs: dict[tuple[str, str], TensorSpec] = {}
    for method in state.entrypoints:
        specs, edge_specs, found = _infer_scope(
            method, (), (), groups, external, visiting, f"method:{method.name}", method_outputs
        )
        all_outputs.update(specs)
        all_edges.update({f"{method.name}:{key}": value for key, value in edge_specs.items()})
        diagnostics.extend(found)
        for output in method.outputs:
            spec = specs.get(f"method_output:{output.id}")
            if spec is not None:
                method_outputs[(method.name, output.id)] = spec
    return ShapeInferenceResult(all_outputs, all_edges, tuple(_unique_diagnostics(diagnostics)))


def _infer_scope(scope: MethodGraph | None, group_inputs: tuple[object, ...], group_outputs: tuple[object, ...],
                 groups: Mapping[str, GroupDefinition], external: Mapping[str, object], visiting: set[str],
                 scope_key: str,
                 method_outputs: Mapping[tuple[str, str], TensorSpec] | None = None,
                 ) -> tuple[dict[str, TensorSpec], dict[str, TensorSpec], list[Diagnostic]]:
    if scope is None:
        return {}, {}, []
    diagnostics: list[Diagnostic] = []
    elements = {item.id: item for item in scope.elements}
    instances = {item.id: item for item in scope.group_instances}
    edge_specs: dict[str, TensorSpec] = {}
    outputs: dict[str, TensorSpec] = {}
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in [*elements, *instances]}
    incoming: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for edge in scope.edges:
        incoming.setdefault((edge.target.element_id, edge.target.port), []).append((edge.source.element_id, edge.source.port))
        if edge.source.element_id in adjacency and edge.target.element_id in adjacency:
            adjacency[edge.source.element_id].append(edge.target.element_id)
    order = _topological_order(adjacency)
    if order is None:
        return {}, {}, [Diagnostic("pytorch_shape_graph_cycle", f"Cannot infer shapes for cyclic scope {scope_key}")]

    method_inputs = {item.id: tensor_spec(item.tensor) for item in scope.inputs}
    group_input_specs = {item.name: tensor_spec(item.tensor) for item in group_inputs if getattr(item, "tensor", None) is not None}
    group_input_ids = {item.id: item.name for item in group_inputs}
    # Group-boundary inputs are pseudo-sources rather than runtime element nodes.
    for port_id, name in group_input_ids.items():
        if name in group_input_specs:
            outputs[f"{port_id}.out"] = group_input_specs[name]

    instance_inputs = {item.id: item for item in scope.group_instances}
    for node_id in order:
        if node_id in elements:
            element = elements[node_id]
            if element.kind == "input":
                declared_id = element.params.get("method_input")
                spec = method_inputs.get(declared_id) if isinstance(declared_id, str) else None
                if spec is None:
                    diagnostics.append(Diagnostic("pytorch_shape_input_unresolved", "Input element does not resolve to a method tensor declaration", element_id=node_id))
                else:
                    outputs[f"{node_id}.out"] = spec
                continue
            if element.kind == "output":
                sources = incoming.get((node_id, "x"), ())
                spec = _source_spec(sources, outputs)
                if spec is not None:
                    output_id = element.params.get("method_output")
                    outputs[f"{node_id}.result"] = spec
                    outputs[f"method_output:{output_id}"] = spec
                continue
            specs_by_port: dict[str, list[TensorSpec]] = {}
            for edge in scope.edges:
                if edge.target.element_id == node_id:
                    spec = outputs.get(f"{edge.source.element_id}.{edge.source.port}")
                    if spec is not None:
                        specs_by_port.setdefault(edge.target.port, []).append(spec)
                        edge_specs[edge.id] = spec
            ordered_inputs = _ordered_inputs(element.kind, specs_by_port)
            params = _resolved_params(element, external)
            if element.kind == "method_call":
                method_name = params.get("method")
                output_name = params.get("output")
                called = (method_outputs or {}).get((str(method_name), str(output_name)))
                if called is None:
                    diagnostics.append(Diagnostic(
                        "pytorch_method_call_output_unresolved",
                        f"Method call {method_name!r} output {output_name!r} has no previously inferred shape",
                        element_id=node_id,
                    ))
                elif len(ordered_inputs) != 1:
                    diagnostics.append(Diagnostic(
                        "pytorch_method_call_inputs_invalid",
                        "V1 method calls require exactly one tensor input",
                        element_id=node_id,
                    ))
                else:
                    outputs[f"{node_id}.out"] = called
                continue
            try:
                inferred = _infer_element(element.kind, ordered_inputs, params)
                for index, spec in enumerate(inferred):
                    port = "out" if index == 0 else f"out{index}"
                    outputs[f"{node_id}.{port}"] = spec
            except _ShapeError as error:
                diagnostics.append(Diagnostic(error.code, error.message, element_id=node_id))
        else:
            instance = instance_inputs[node_id]
            definition = groups.get(instance.group_id)
            if definition is None:
                continue
            if definition.id in visiting:
                diagnostics.append(Diagnostic("pytorch_group_recursive", f"Recursive Group {definition.id!r} cannot be shape-inferred", element_id=node_id))
                continue
            provided: dict[str, TensorSpec] = {}
            for edge in scope.edges:
                if edge.target.element_id == node_id:
                    spec = outputs.get(f"{edge.source.element_id}.{edge.source.port}")
                    if spec is not None:
                        provided[edge.target.port] = spec
                        edge_specs[edge.id] = spec
            nested_method = MethodGraph(f"group:{definition.id}", (), (), definition.elements, definition.edges, definition.group_instances)
            visiting.add(definition.id)
            nested: dict[str, TensorSpec] = {}
            nested_edges: dict[str, TensorSpec] = {}
            nested_diagnostics: list[Diagnostic] = []
            repetition_inputs = provided
            for _ in range(instance.repeat):
                nested_group_inputs = tuple(
                    type("InputBoundary", (), {"id": p.id, "name": p.name, "tensor": _contract_from_spec(repetition_inputs[p.name]) if p.name in repetition_inputs else p.tensor})()
                    for p in definition.inputs
                )
                nested, nested_edges, repetition_diagnostics = _infer_scope(
                    nested_method, nested_group_inputs, definition.outputs, groups, external, visiting,
                    f"group:{definition.id}", method_outputs,
                )
                nested_diagnostics.extend(repetition_diagnostics)
                output_by_name = {port.name: port for port in definition.outputs}
                repetition_inputs = {}
                for index, input_port in enumerate(definition.inputs):
                    output_port = output_by_name.get(input_port.name)
                    if output_port is None and index < len(definition.outputs):
                        output_port = definition.outputs[index]
                    if output_port is not None and f"group_output:{output_port.id}" in nested:
                        repetition_inputs[input_port.name] = nested[f"group_output:{output_port.id}"]
            visiting.remove(definition.id)
            diagnostics.extend(nested_diagnostics)
            # Preserve one logical Group-scope shape projection for its editor.
            # The repeated instances share the same declared internal graph, so
            # the last repetition has the same semantic shape as its peers.
            outputs.update({
                f"group:{definition.id}:{key}": value
                for key, value in nested.items()
                if not key.startswith("group_output:")
            })
            edge_specs.update({
                f"group:{definition.id}:{key}": value for key, value in nested_edges.items()
            })
            outputs.update({f"{node_id}.{key}": value for key, value in nested.items() if key.startswith("group_output:")})
            for port in definition.outputs:
                value = nested.get(f"group_output:{port.id}")
                if value:
                    outputs[f"{node_id}.{port.name}"] = value
            edge_specs.update({f"{node_id}:{key}": value for key, value in nested_edges.items()})

    for edge in scope.edges:
        value = outputs.get(f"{edge.source.element_id}.{edge.source.port}")
        if value is not None:
            edge_specs[edge.id] = value
    for port in group_outputs:
        sources = incoming.get((port.id, "x"), ())
        spec = _source_spec(sources, outputs)
        if spec is not None:
            outputs[f"group_output:{port.id}"] = spec
    return outputs, edge_specs, diagnostics


def _contract_from_spec(spec: TensorSpec) -> TensorContract:
    return TensorContract(tuple(dim.value if dim.value is not None else dim.expr for dim in spec.shape), spec.dtype or "float32", spec.semantic)


def _source_spec(sources: object, outputs: Mapping[str, TensorSpec]) -> TensorSpec | None:
    for element_id, port in sources:  # type: ignore[union-attr]
        return outputs.get(f"{element_id}.{port}")
    return None


def _resolved_params(element: object, external: Mapping[str, object]) -> dict[str, object]:
    values = dict(element.params)
    for name, binding in element.parameter_bindings.items():
        if binding.external_parameter_id in external:
            values[name] = external[binding.external_parameter_id]
    return values


def _ordered_inputs(kind: str, specs: Mapping[str, list[TensorSpec]]) -> tuple[TensorSpec, ...]:
    if kind == "concatenate":
        def variadic_order(port: str) -> tuple[int, str]:
            if port.startswith("inputs[") and port.endswith("]"):
                try:
                    return (int(port[7:-1]), port)
                except ValueError:
                    pass
            return (0, port)

        return tuple(spec for port in sorted(specs, key=variadic_order) for spec in specs[port])
    order = ("a", "b") if kind in {"add", "multiply", "matmul"} else ("logits", "target") if kind == "cross_entropy" else ("x",)
    return tuple(specs[name][0] for name in order if specs.get(name))


class _ShapeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _dim(token: object) -> Dim:
    if type(token) is int:
        if token < 0:
            raise _ShapeError("pytorch_shape_dimension_invalid", f"Shape dimension must be non-negative, got {token}")
        return Dim(str(token), token)
    if isinstance(token, str) and token:
        return Dim(token, None)
    raise _ShapeError("pytorch_shape_dimension_invalid", f"Invalid shape token {token!r}")


def _infer_element(kind: str, inputs: tuple[TensorSpec, ...], params: Mapping[str, object]) -> tuple[TensorSpec, ...]:
    if kind == "group_instance":
        return inputs
    if not inputs:
        raise _ShapeError("pytorch_shape_input_missing", f"{kind} requires an input tensor")
    first = inputs[0]
    shape = first.shape
    if kind in {"relu", "gelu", "silu", "dropout", "softmax", "rms_norm", "scale", "causal_mask", "rope"}:
        if kind == "rms_norm":
            _require_last(shape, params.get("normalized_shape"), kind)
        if kind == "rope" and shape and shape[-1].value is not None and shape[-1].value % 2:
            raise _ShapeError("pytorch_rope_dimension_odd", "RoPE requires an even final dimension")
        if kind == "causal_mask" and len(shape) >= 2:
            _equal_dim(shape[-1], shape[-2], "Causal mask expects square attention scores")
        if kind == "softmax" and shape:
            _normalize_axis(params.get("dim", -1), len(shape))
        return (first,)
    if kind == "embedding":
        if first.dtype and not any(value in first.dtype.lower() for value in ("int", "long")):
            raise _ShapeError("pytorch_embedding_dtype_invalid", "Embedding indices must use an integer dtype")
        dim = int(params["embedding_dim"])
        return (TensorSpec(shape + (Dim(str(dim), dim),), "float32", first.semantic),)
    if kind == "linear":
        _require_last(shape, params.get("in_features"), kind)
        value = int(params["out_features"])
        return (TensorSpec(shape[:-1] + (Dim(str(value), value),), first.dtype, first.semantic),)
    if kind == "reshape":
        target = tuple(params["target_shape"])
        inferred = [Dim("-1", None) if value == -1 else _dim(value) for value in target]
        negative = [index for index, value in enumerate(target) if value == -1]
        if any(type(value) is int and value < -1 for value in target) or len(negative) > 1:
            raise _ShapeError("pytorch_reshape_invalid", "Reshape permits at most one inferred -1 dimension")
        if negative:
            if all(dim.value is not None for dim in shape) and all(dim.value is not None for i, dim in enumerate(inferred) if i != negative[0]):
                total = prod(dim.value for dim in shape if dim.value is not None)
                known = prod(dim.value for i, dim in enumerate(inferred) if i != negative[0] and dim.value is not None)
                if known == 0 or total % known:
                    raise _ShapeError("pytorch_reshape_incompatible", "Reshape target does not preserve element count")
                inferred[negative[0]] = Dim(str(total // known), total // known)
            else:
                inferred[negative[0]] = Dim("-1", None)
        elif all(dim.value is not None for dim in shape + tuple(inferred)):
            if prod(dim.value for dim in shape if dim.value is not None) != prod(dim.value for dim in inferred if dim.value is not None):
                raise _ShapeError("pytorch_reshape_incompatible", "Reshape target does not preserve element count")
        return (TensorSpec(tuple(inferred), first.dtype, first.semantic),)
    if kind == "transpose":
        result = list(shape)
        dim0 = _normalize_axis(params["dim0"], len(shape))
        dim1 = _normalize_axis(params["dim1"], len(shape))
        result[dim0], result[dim1] = result[dim1], result[dim0]
        return (TensorSpec(tuple(result), first.dtype, first.semantic),)
    if kind == "flatten":
        start = _normalize_axis(params.get("start_dim", 0), len(shape))
        end = _normalize_axis(params.get("end_dim", -1), len(shape))
        if start > end:
            raise _ShapeError("pytorch_flatten_invalid", "flatten start_dim must be <= end_dim")
        selected = shape[start:end + 1]
        if all(dim.value is not None for dim in selected):
            flattened = prod(dim.value for dim in selected if dim.value is not None)
            merged = Dim(str(flattened), flattened)
        else:
            merged = Dim("*".join(dim.expr for dim in selected), None)
        return (TensorSpec(shape[:start] + (merged,) + shape[end + 1:], first.dtype, first.semantic),)
    if kind == "unflatten":
        dim = _normalize_axis(params["dim"], len(shape))
        sizes = tuple(_dim(value) for value in params["sizes"])
        old = shape[dim]
        if old.value is not None and all(item.value is not None for item in sizes):
            if prod(item.value for item in sizes if item.value is not None) != old.value:
                raise _ShapeError("pytorch_unflatten_incompatible", "Unflatten sizes must multiply to the selected dimension")
        return (TensorSpec(shape[:dim] + sizes + shape[dim + 1:], first.dtype, first.semantic),)
    if kind == "split_heads":
        if len(shape) != 3:
            raise _ShapeError("pytorch_split_heads_rank", "split_heads expects canonical (e, t, d) input")
        heads = int(params["n_heads"])
        dim = shape[2]
        if dim.value is not None and dim.value % heads:
            raise _ShapeError("pytorch_split_heads_divisibility", f"Dimension {dim.value} is not divisible by n_heads={heads}")
        dh = Dim(f"{dim.expr}/h", dim.value // heads if dim.value is not None else None)
        return (TensorSpec((shape[0], Dim("h", heads), shape[1], dh), first.dtype, first.semantic),)
    if kind == "merge_heads":
        if len(shape) != 4:
            raise _ShapeError("pytorch_merge_heads_rank", "merge_heads expects canonical (e, h, t, dh) input")
        h, dh = shape[1], shape[3]
        value = h.value * dh.value if h.value is not None and dh.value is not None else None
        return (TensorSpec((shape[0], shape[2], Dim(f"{h.expr}*{dh.expr}", value)), first.dtype, first.semantic),)
    if kind in {"add", "multiply"}:
        if len(inputs) != 2:
            raise _ShapeError("pytorch_shape_input_missing", f"{kind} requires two inputs")
        return (_binary_broadcast(inputs[0], inputs[1]),)
    if kind == "concatenate":
        if len(inputs) < 1:
            raise _ShapeError("pytorch_shape_input_missing", "concatenate needs at least one input")
        axis = _normalize_axis(params.get("dim", 0), len(first.shape))
        shape = list(first.shape)
        total = 0
        expressions: list[str] = []
        for spec in inputs:
            if len(spec.shape) != len(shape):
                raise _ShapeError("pytorch_concatenate_rank", "concatenate inputs must have the same rank")
            for index, (left, right) in enumerate(zip(shape, spec.shape)):
                if index != axis:
                    _equal_dim(left, right, "concatenate dimensions must match outside the concatenation axis")
            dim = spec.shape[axis]
            expressions.append(dim.expr)
            if dim.value is None:
                total = -1
            elif total >= 0:
                total += dim.value
        shape[axis] = Dim("+".join(expressions), total if total >= 0 else None)
        return (TensorSpec(tuple(shape), first.dtype, first.semantic),)
    if kind == "matmul":
        if len(inputs) != 2:
            raise _ShapeError("pytorch_shape_input_missing", "matmul requires two inputs")
        return (_matmul(inputs[0], inputs[1]),)
    if kind == "cross_entropy":
        if len(inputs) != 2:
            raise _ShapeError("pytorch_shape_input_missing", "cross_entropy requires logits and targets")
        if params.get("mode") == "next_token":
            if len(inputs[0].shape) < 2 or len(inputs[1].shape) < 2:
                raise _ShapeError("pytorch_cross_entropy_shape", "next_token cross entropy expects batched sequence tensors")
            _equal_dim(inputs[0].shape[0], inputs[1].shape[0], "logits and input_ids batch dimensions must match")
            _equal_dim(inputs[0].shape[1], inputs[1].shape[1], "logits and input_ids sequence dimensions must match")
        return (TensorSpec((), "float32", "loss"),)
    raise _ShapeError("pytorch_shape_rule_missing", f"No shape rule for {kind!r}")


def _binary_broadcast(left: TensorSpec, right: TensorSpec) -> TensorSpec:
    result: list[Dim] = []
    for a, b in zip(reversed(left.shape), reversed(right.shape)):
        if a.value == 1:
            result.append(b)
        elif b.value == 1:
            result.append(a)
        elif a.value is not None and b.value is not None:
            if a.value != b.value:
                raise _ShapeError("pytorch_broadcast_incompatible", f"Cannot broadcast dimensions {a.expr} and {b.expr}")
            result.append(a)
        elif a.expr == b.expr:
            result.append(a)
        else:
            result.append(Dim(f"broadcast({a.expr},{b.expr})", None))
    longer = left.shape if len(left.shape) > len(right.shape) else right.shape
    result.extend(reversed(longer[:abs(len(left.shape) - len(right.shape))]))
    return TensorSpec(tuple(reversed(result)), left.dtype or right.dtype, left.semantic or right.semantic)


def _matmul(left: TensorSpec, right: TensorSpec) -> TensorSpec:
    if not left.shape or not right.shape:
        raise _ShapeError("pytorch_matmul_rank", "matmul inputs must have rank >= 1")
    left_vector, right_vector = len(left.shape) == 1, len(right.shape) == 1
    a = (Dim("1", 1),) + left.shape if left_vector else left.shape
    b = right.shape + (Dim("1", 1),) if right_vector else right.shape
    _equal_dim(a[-1], b[-2], "matmul inner dimensions are incompatible")
    batch = _binary_broadcast(TensorSpec(a[:-2], None), TensorSpec(b[:-2], None)).shape
    result = batch + (a[-2], b[-1])
    if left_vector:
        result = result[:-2] + (result[-1],)
    if right_vector:
        result = result[:-1]
    return TensorSpec(result, left.dtype or right.dtype, left.semantic or right.semantic)


def _equal_dim(left: Dim, right: Dim, message: str) -> None:
    if left.value is not None and right.value is not None:
        if left.value != right.value:
            raise _ShapeError("pytorch_shape_incompatible", f"{message}: {left.expr} != {right.expr}")
    # Distinct unresolved symbols remain unknown in V1; only concrete conflicts
    # prove incompatibility and block materialization.


def _require_last(shape: tuple[Dim, ...], expected: object, kind: str) -> None:
    if not shape:
        raise _ShapeError("pytorch_shape_rank", f"{kind} requires at least one dimension")
    if type(expected) is int:
        actual = shape[-1]
        if actual.value is not None and actual.value != expected:
            raise _ShapeError("pytorch_shape_incompatible", f"{kind} expected final dimension {expected}, got {actual.expr}")


def _normalize_axis(value: object, rank: int) -> int:
    if type(value) is not int:
        raise _ShapeError("pytorch_axis_invalid", f"Dimension index must be an integer, got {value!r}")
    axis = value + rank if value < 0 else value
    if axis < 0 or axis >= rank:
        raise _ShapeError("pytorch_axis_out_of_range", f"Dimension index {value} is out of range for rank {rank}")
    return axis


def _topological_order(adjacency: Mapping[str, list[str]]) -> list[str] | None:
    indegree = {node: 0 for node in adjacency}
    for targets in adjacency.values():
        for target in targets:
            if target in indegree:
                indegree[target] += 1
    ready = [node for node, count in indegree.items() if count == 0]
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for target in adjacency.get(node, ()):
            if target in indegree:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
    return order if len(order) == len(adjacency) else None


def _unique_diagnostics(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    seen: set[tuple[str, str, str | None]] = set()
    for item in diagnostics:
        key = (item.code, item.message, item.element_id)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result

