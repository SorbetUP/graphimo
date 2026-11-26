"""PyTorch addon element catalog and semantic graph validation."""

from __future__ import annotations

import keyword
import math
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from .schema import (
    Diagnostic,
    ElementRecord,
    GroupDefinition,
    MethodGraph,
    PyTorchMetaBlockState,
)

JsonScalar = None | bool | int | float | str


@dataclass(frozen=True)
class PortSpec:
    name: str
    variadic: bool = False
    required: bool = True


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    type: str
    default: Any
    minimum: float | int | None = None
    maximum: float | int | None = None
    choices: tuple[Any, ...] | None = None
    structural: bool = True


@dataclass(frozen=True)
class ElementPresentation:
    title: str
    subtitle: str
    input_ports: tuple[str, ...]
    output_ports: tuple[str, ...]
    inferred: Mapping[str, Any]


@dataclass(frozen=True)
class ValidationReport:
    diagnostics: tuple[Diagnostic, ...]

    @property
    def valid(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)


ShapeFunction = Callable[
    [Mapping[str, Any], Mapping[str, Any], Mapping[str, JsonScalar]], tuple[Any, ...]
]


@dataclass(frozen=True)
class ElementKind:
    kind: str
    input_ports: tuple[PortSpec, ...]
    output_ports: tuple[PortSpec, ...]
    parameters: tuple[ParameterSpec, ...]
    shape_rule: ShapeFunction | None = None

    def validate_params(
        self, params: Mapping[str, Any], external_values: Mapping[str, JsonScalar]
    ) -> tuple[Diagnostic, ...]:
        return _validate_parameters(self, params, external_values)

    def infer_shapes(self, inputs, params, external_values):
        if self.shape_rule is None:
            from .shapes import _infer_element

            element = ElementRecord("shape-probe", self.kind, self.kind, dict(params), {}, {})
            normalized = {
                key: value if isinstance(value, tuple) else (value,)
                for key, value in inputs.items()
            }
            return _infer_element(element, normalized, external_values, {})
        return self.shape_rule(inputs, params, external_values)

    def build_module(self, params, external_values):
        """P2 descriptors do not build runtime modules; codegen is P3."""
        return None

    def emit_forward(self, ctx, element, input_exprs):
        """P2 descriptors do not emit source; codegen is P3."""
        return None

    def presentation(self, element: ElementRecord, inferred) -> ElementPresentation:
        return ElementPresentation(
            title=element.name,
            subtitle=self.kind,
            input_ports=tuple(port.name for port in self.input_ports),
            output_ports=tuple(port.name for port in self.output_ports),
            inferred=inferred,
        )


def _p(
    name: str,
    value_type: str,
    default: Any,
    minimum=None,
    maximum=None,
    choices=None,
    structural=True,
) -> ParameterSpec:
    return ParameterSpec(name, value_type, default, minimum, maximum, choices, structural)


def _kind(
    name: str,
    inputs: tuple[PortSpec, ...] = (PortSpec("input"),),
    outputs: tuple[PortSpec, ...] = (PortSpec("output"),),
    params: tuple[ParameterSpec, ...] = (),
) -> ElementKind:
    return ElementKind(name, inputs, outputs, params)


_NO_INPUT: tuple[PortSpec, ...] = ()
_CATALOG = (
    _kind(
        "input",
        _NO_INPUT,
        (PortSpec("output"),),
        (
            _p("name", "str", "input"),
            _p("shape", "shape", ()),
            _p("dtype", "str", "float32"),
            _p("semantic", "optional_str", None),
        ),
    ),
    _kind("output", (PortSpec("input"),), (PortSpec("output"),), (_p("name", "str", "output"),)),
    _kind(
        "embedding",
        params=(
            _p("num_embeddings", "int", 1, 1),
            _p("embedding_dim", "int", 1, 1),
            _p("init_std", "optional_float", None, 0),
        ),
    ),
    _kind(
        "linear",
        params=(
            _p("in_features", "int", 1, 1),
            _p("out_features", "int", 1, 1),
            _p("bias", "bool", True),
            _p("init_std", "optional_float", None, 0),
        ),
    ),
    _kind("rms_norm", params=(_p("normalized_shape", "int", 1, 1), _p("eps", "float", 1e-5, 0))),
    _kind("dropout", params=(_p("p", "float", 0.5, 0, 1),)),
    _kind("relu"),
    _kind("gelu"),
    _kind("silu"),
    _kind("softmax", params=(_p("dim", "int", -1),)),
    _kind("reshape", params=(_p("target_shape", "shape", ()),)),
    _kind("transpose", params=(_p("dim0", "int", 0), _p("dim1", "int", 1))),
    _kind("flatten", params=(_p("start_dim", "int", 0), _p("end_dim", "int", -1))),
    _kind("unflatten", params=(_p("dim", "int", -1), _p("sizes", "shape", ()))),
    _kind(
        "split_heads",
        params=(
            _p("n_heads", "int", 1, 1),
            _p("layout", "str", "e_t_d_to_e_h_t_dh", choices=("e_t_d_to_e_h_t_dh",)),
        ),
    ),
    _kind("merge_heads"),
    _kind("concatenate", (PortSpec("input", variadic=True),), params=(_p("dim", "int", -1),)),
    _kind("add", (PortSpec("left"), PortSpec("right"))),
    _kind("multiply", (PortSpec("left"), PortSpec("right"))),
    _kind("scale", params=(_p("factor", "number", 1.0),)),
    _kind("matmul", (PortSpec("left"), PortSpec("right"))),
    _kind("rope", params=(_p("base", "float", 10000.0, 0),)),
    _kind("causal_mask"),
    _kind(
        "cross_entropy",
        (PortSpec("logits"), PortSpec("target")),
        params=(_p("mode", "str", "generic", choices=("generic", "next_token")),),
    ),
    _kind("group_instance", _NO_INPUT, (PortSpec("output"),)),
)

ELEMENT_REGISTRY: Mapping[str, ElementKind] = {item.kind: item for item in _CATALOG}


def get_element_kind(kind: str) -> ElementKind | None:
    return ELEMENT_REGISTRY.get(kind)


def _validate_parameters(
    descriptor: ElementKind, params: Mapping[str, Any], external_values: Mapping[str, JsonScalar]
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    declared = {spec.name: spec for spec in descriptor.parameters}
    for name, spec in declared.items():
        if name not in params:
            value = spec.default
        else:
            value = params[name]
        if name in params and name in external_values:
            value = external_values[name]
        if name not in params and spec.default is None and spec.type.startswith("optional_"):
            continue
        expected = {
            "int": (int,),
            "float": (int, float),
            "number": (int, float),
            "bool": (bool,),
            "str": (str,),
            "optional_float": (int, float, type(None)),
            "optional_str": (str, type(None)),
            "shape": (list, tuple),
        }.get(spec.type, ())
        valid = any(type(value) is item for item in expected)
        if spec.type == "shape":
            valid = isinstance(value, (list, tuple)) and all(
                (type(dimension) is int and dimension > 0)
                or (isinstance(dimension, str) and bool(dimension.strip()))
                for dimension in value
            )
            if descriptor.kind == "reshape" and isinstance(value, (list, tuple)):
                valid = (
                    all(
                        (type(dimension) is int and (dimension > 0 or dimension == -1))
                        or (isinstance(dimension, str) and bool(dimension.strip()))
                        for dimension in value
                    )
                    and sum(dimension == -1 for dimension in value) <= 1
                )
        if not valid:
            diagnostics.append(
                Diagnostic(
                    "pytorch_param_invalid",
                    f"{name} must have type {spec.type}",
                    element_id=None,
                )
            )
            continue
        if isinstance(value, float) and not math.isfinite(value):
            diagnostics.append(Diagnostic("pytorch_param_invalid", f"{name} must be finite"))
            continue
        if spec.minimum is not None and value is not None and value < spec.minimum:
            diagnostics.append(
                Diagnostic("pytorch_param_invalid", f"{name} must be >= {spec.minimum}")
            )
        if spec.maximum is not None and value is not None:
            if value >= spec.maximum and descriptor.kind == "dropout" and name == "p":
                diagnostics.append(Diagnostic("pytorch_param_invalid", "dropout p must be < 1"))
            elif value > spec.maximum:
                diagnostics.append(
                    Diagnostic("pytorch_param_invalid", f"{name} must be <= {spec.maximum}")
                )
        if (
            descriptor.kind in {"rms_norm", "rope"}
            and name in {"eps", "base"}
            and value is not None
            and value <= 0
        ):
            diagnostics.append(Diagnostic("pytorch_param_invalid", f"{name} must be > 0"))
        if spec.choices is not None and value not in spec.choices:
            diagnostics.append(
                Diagnostic("pytorch_param_invalid", f"{name} must be one of {spec.choices}")
            )
    for name in params.keys() - declared.keys():
        diagnostics.append(Diagnostic("pytorch_param_invalid", f"unknown parameter {name!r}"))
    return tuple(diagnostics)


def _elements_for_graph(graph: MethodGraph | GroupDefinition):
    elements = {element.id: element for element in graph.elements}
    groups = {item.id: item for item in graph.group_instances}
    return elements, groups


def _all_groups(state: PyTorchMetaBlockState) -> dict[str, GroupDefinition]:
    return {group.id: group for group in state.groups}


def validate_model_graph(
    state: PyTorchMetaBlockState,
    external_values: Mapping[str, JsonScalar] | None = None,
) -> ValidationReport:
    """Collect structural and parameter diagnostics for every method and group."""
    supplied_values = dict(external_values or {})
    diagnostics: list[Diagnostic] = []
    groups = _all_groups(state)
    declared_external = {parameter.id: parameter for parameter in state.external_parameters}
    accepted_external_keys = declared_external.keys() | {
        parameter.name for parameter in state.external_parameters
    }
    for parameter_id in supplied_values.keys() - accepted_external_keys:
        diagnostics.append(
            Diagnostic(
                "pytorch_external_param_missing", f"unknown external parameter {parameter_id!r}"
            )
        )
    for parameter in state.external_parameters:
        if parameter.id not in supplied_values and parameter.name not in supplied_values:
            continue
        actual = supplied_values.get(parameter.id, supplied_values.get(parameter.name))
        allowed = {"int": int, "float": (int, float), "bool": bool, "str": str}[parameter.type]
        if type(actual) not in (allowed if isinstance(allowed, tuple) else (allowed,)):
            diagnostics.append(
                Diagnostic(
                    "pytorch_param_invalid",
                    f"external parameter {parameter.name!r} has invalid type",
                )
            )
    values = {
        parameter.id: supplied_values.get(
            parameter.id, supplied_values.get(parameter.name, parameter.default)
        )
        for parameter in state.external_parameters
    }
    group_visiting: set[str] = set()
    checked_groups: set[str] = set()

    def validate_graph(graph: MethodGraph | GroupDefinition, owner: str) -> None:
        elements, instances = _elements_for_graph(graph)
        if len(elements) != len(graph.elements):
            diagnostics.append(
                Diagnostic("pytorch_state_invalid", f"duplicate element id in {owner}")
            )
        overlap = elements.keys() & instances.keys()
        if overlap:
            diagnostics.append(
                Diagnostic(
                    "pytorch_edge_invalid",
                    f"element and Group instance IDs overlap in {owner}: {sorted(overlap)}",
                )
            )
        edge_ids: set[str] = set()
        incoming: dict[tuple[str, str], list[Any]] = {}
        adjacency: dict[str, set[str]] = {identity: set() for identity in [*elements, *instances]}
        for element in graph.elements:
            if not element.name.isidentifier() or keyword.iskeyword(element.name):
                diagnostics.append(
                    Diagnostic(
                        "pytorch_module_name_invalid",
                        f"invalid Python module name {element.name!r}",
                        element_id=element.id,
                    )
                )
        if len({item.name for item in graph.elements}) != len(graph.elements):
            diagnostics.append(
                Diagnostic("pytorch_module_path_conflict", f"conflicting module names in {owner}")
            )
        if len(
            {item.name for item in graph.elements} | {item.name for item in graph.group_instances}
        ) < (len(graph.elements) + len(graph.group_instances)):
            diagnostics.append(
                Diagnostic("pytorch_module_path_conflict", f"conflicting module names in {owner}")
            )
        for instance in graph.group_instances:
            if instance.repeat < 1:
                diagnostics.append(
                    Diagnostic(
                        "pytorch_group_repeat_invalid",
                        "Group repeat must be >= 1",
                        group_id=instance.group_id,
                    )
                )
            if instance.group_id not in groups:
                diagnostics.append(
                    Diagnostic(
                        "pytorch_group_reference_invalid",
                        f"unknown Group {instance.group_id!r}",
                        group_id=instance.group_id,
                    )
                )
            if not instance.name.isidentifier() or keyword.iskeyword(instance.name):
                diagnostics.append(
                    Diagnostic(
                        "pytorch_module_name_invalid",
                        f"invalid Python Group name {instance.name!r}",
                        group_id=instance.group_id,
                    )
                )
        for edge in graph.edges:
            if edge.id in edge_ids:
                diagnostics.append(
                    Diagnostic(
                        "pytorch_edge_invalid",
                        f"duplicate edge id {edge.id!r}",
                        edge_id=edge.id,
                    )
                )
            edge_ids.add(edge.id)
            source = edge.source.element_id
            target = edge.target.element_id
            if source not in elements and source not in instances:
                diagnostics.append(
                    Diagnostic(
                        "pytorch_edge_invalid",
                        f"edge source {source!r} does not exist",
                        edge_id=edge.id,
                    )
                )
                continue
            if target not in elements and target not in instances:
                diagnostics.append(
                    Diagnostic(
                        "pytorch_edge_invalid",
                        f"edge target {target!r} does not exist",
                        edge_id=edge.id,
                    )
                )
                continue
            source_group = groups.get(instances[source].group_id) if source in instances else None
            target_group = groups.get(instances[target].group_id) if target in instances else None
            source_ports = (
                tuple(port.name for port in source_group.outputs)
                if source_group
                else tuple(
                    port.name
                    for port in (
                        get_element_kind(elements[source].kind).output_ports
                        if get_element_kind(elements[source].kind)
                        else ()
                    )
                )
            )
            target_ports = (
                tuple(port.name for port in target_group.inputs)
                if target_group
                else tuple(
                    port.name
                    for port in (
                        get_element_kind(elements[target].kind).input_ports
                        if get_element_kind(elements[target].kind)
                        else ()
                    )
                )
            )
            if edge.source.port not in source_ports:
                diagnostics.append(
                    Diagnostic(
                        "pytorch_unknown_port",
                        f"unknown output port {edge.source.port!r}",
                        element_id=source,
                        edge_id=edge.id,
                    )
                )
            if edge.target.port not in target_ports:
                diagnostics.append(
                    Diagnostic(
                        "pytorch_unknown_port",
                        f"unknown input port {edge.target.port!r}",
                        element_id=target,
                        edge_id=edge.id,
                    )
                )
            key = (target, edge.target.port)
            incoming.setdefault(key, []).append(edge)
            adjacency[source].add(target)
        if isinstance(graph, GroupDefinition):
            for port in graph.inputs:
                incoming.setdefault((port.endpoint.element_id, port.endpoint.port), []).append(port)
        for (node_id, port_name), edges in incoming.items():
            descriptor = get_element_kind(elements[node_id].kind) if node_id in elements else None
            if descriptor:
                port = next(
                    (item for item in descriptor.input_ports if item.name == port_name), None
                )
            else:
                instance = instances.get(node_id)
                definition = groups.get(instance.group_id) if instance else None
                port = (
                    PortSpec(port_name)
                    if definition and any(item.name == port_name for item in definition.inputs)
                    else None
                )
            if len(edges) > 1 and not (port and port.variadic):
                diagnostics.append(
                    Diagnostic(
                        "pytorch_input_multiple_edges",
                        f"input port {port_name!r} has multiple incoming edges",
                        element_id=node_id,
                    )
                )
        for element in graph.elements:
            descriptor = get_element_kind(element.kind)
            if descriptor is None:
                diagnostics.append(
                    Diagnostic(
                        "pytorch_unknown_element_kind",
                        f"unknown element kind {element.kind!r}",
                        element_id=element.id,
                    )
                )
                continue
            resolved_params = dict(element.params)
            for parameter_name, binding in element.parameter_bindings.items():
                if binding.external_parameter_id in values:
                    resolved_params[parameter_name] = values[binding.external_parameter_id]
            for item in descriptor.validate_params(resolved_params, values):
                diagnostics.append(
                    Diagnostic(
                        item.code,
                        item.message,
                        item.severity,
                        method_name=owner,
                        element_id=element.id,
                    )
                )
            for port in descriptor.input_ports:
                count = len(incoming.get((element.id, port.name), ()))
                if port.required and count == 0 and element.kind != "input":
                    diagnostics.append(
                        Diagnostic(
                            "pytorch_input_unconnected",
                            f"required input {port.name!r} is unconnected",
                            method_name=owner,
                            element_id=element.id,
                        )
                    )
            for _, binding in element.parameter_bindings.items():
                if binding.external_parameter_id not in declared_external:
                    diagnostics.append(
                        Diagnostic(
                            "pytorch_external_param_missing",
                            f"unknown external parameter binding {binding.external_parameter_id!r}",
                            element_id=element.id,
                        )
                    )
                elif binding.external_parameter_id not in values:
                    diagnostics.append(
                        Diagnostic(
                            "pytorch_external_param_missing",
                            f"external parameter {binding.external_parameter_id!r} has no resolved value",
                            element_id=element.id,
                        )
                    )
        for instance in graph.group_instances:
            for name in instance.params.keys():
                # Group parameter compatibility is checked after its definition is resolved.
                if not isinstance(name, str):
                    diagnostics.append(
                        Diagnostic(
                            "pytorch_group_parameter_invalid",
                            "Group parameter names must be strings",
                            group_id=instance.group_id,
                        )
                    )
            definition = groups.get(instance.group_id)
            if definition:
                for port in definition.inputs:
                    if not incoming.get((instance.id, port.name)):
                        diagnostics.append(
                            Diagnostic(
                                "pytorch_input_unconnected",
                                f"required Group input {port.name!r} is unconnected",
                                group_id=instance.group_id,
                            )
                        )
        if isinstance(graph, GroupDefinition):
            for label, ports in (("input", graph.inputs), ("output", graph.outputs)):
                port_names = [port.name for port in ports]
                if len(port_names) != len(set(port_names)):
                    diagnostics.append(
                        Diagnostic(
                            "pytorch_edge_invalid",
                            f"duplicate Group {label} port name in {owner}",
                            group_id=graph.id,
                        )
                    )
                if label == "input":
                    for port_name in port_names:
                        if not port_name.isidentifier() or keyword.iskeyword(port_name):
                            diagnostics.append(
                                Diagnostic(
                                    "pytorch_module_name_invalid",
                                    f"invalid generated Group input name {port_name!r}",
                                    group_id=graph.id,
                                )
                            )
                for port in ports:
                    endpoint = port.endpoint
                    if endpoint.element_id not in elements and endpoint.element_id not in instances:
                        diagnostics.append(
                            Diagnostic(
                                "pytorch_edge_invalid",
                                f"Group {label} port references missing element {endpoint.element_id!r}",
                                group_id=graph.id,
                            )
                        )
                        continue
                    descriptor = (
                        get_element_kind(elements[endpoint.element_id].kind)
                        if endpoint.element_id in elements
                        else None
                    )
                    instance = instances.get(endpoint.element_id)
                    nested = groups.get(instance.group_id) if instance else None
                    if label == "input":
                        available = (
                            tuple(item.name for item in descriptor.input_ports)
                            if descriptor
                            else tuple(item.name for item in nested.inputs)
                            if nested
                            else ()
                        )
                    else:
                        available = (
                            tuple(item.name for item in descriptor.output_ports)
                            if descriptor
                            else tuple(item.name for item in nested.outputs)
                            if nested
                            else ()
                        )
                    if endpoint.port not in available:
                        diagnostics.append(
                            Diagnostic(
                                "pytorch_unknown_port",
                                f"Group {label} port references unknown port {endpoint.port!r}",
                                group_id=graph.id,
                            )
                        )
        cycle = _find_cycle(adjacency)
        if cycle:
            diagnostics.append(
                Diagnostic(
                    "pytorch_cycle_invalid",
                    f"directed dataflow cycle in {owner}: {' -> '.join(cycle)}",
                )
            )
        for output in getattr(graph, "outputs", ()):
            if (
                output.endpoint.element_id not in elements
                and output.endpoint.element_id not in instances
            ):
                diagnostics.append(
                    Diagnostic(
                        "pytorch_edge_invalid",
                        f"method output {output.name!r} references a missing element",
                        method_name=owner,
                    )
                )
            else:
                endpoint = output.endpoint
                if endpoint.element_id in elements:
                    descriptor = get_element_kind(elements[endpoint.element_id].kind)
                    allowed_ports = (
                        tuple(port.name for port in descriptor.output_ports) if descriptor else ()
                    )
                else:
                    instance = instances.get(endpoint.element_id)
                    definition = groups.get(instance.group_id) if instance else None
                    allowed_ports = (
                        tuple(port.name for port in definition.outputs) if definition else ()
                    )
                if endpoint.port not in allowed_ports:
                    diagnostics.append(
                        Diagnostic(
                            "pytorch_unknown_port",
                            f"unknown method output port {endpoint.port!r}",
                            method_name=owner,
                        )
                    )
        for instance in graph.group_instances:
            if instance.group_id in groups:
                validate_group(instance.group_id)

    def validate_group(group_id: str) -> None:
        if group_id in group_visiting:
            diagnostics.append(
                Diagnostic(
                    "pytorch_group_cycle_invalid",
                    f"recursive Group definition involving {group_id!r}",
                    group_id=group_id,
                )
            )
            return
        if group_id in checked_groups:
            return
        group_visiting.add(group_id)
        validate_graph(groups[group_id], group_id)
        group_visiting.remove(group_id)
        checked_groups.add(group_id)

    group_names = [group.name for group in state.groups]
    if len(group_names) != len(set(group_names)):
        diagnostics.append(
            Diagnostic("pytorch_module_path_conflict", "conflicting top-level Group module names")
        )
    for group in state.groups:
        if not group.name.isidentifier() or keyword.iskeyword(group.name):
            diagnostics.append(
                Diagnostic(
                    "pytorch_module_name_invalid",
                    f"invalid Python Group module name {group.name!r}",
                    group_id=group.id,
                )
            )
    supported_methods = {"forward", "loss", "predict"}
    for method in state.entrypoints:
        for value in [
            *(item.name for item in method.inputs),
            *(item.name for item in method.outputs),
        ]:
            if not value.isidentifier() or keyword.iskeyword(value):
                diagnostics.append(
                    Diagnostic(
                        "pytorch_module_name_invalid",
                        f"invalid generated method port name {value!r}",
                        method_name=method.name,
                    )
                )
        if len({item.name for item in method.inputs}) != len(method.inputs):
            diagnostics.append(
                Diagnostic(
                    "pytorch_edge_invalid", "duplicate method input name", method_name=method.name
                )
            )
        if len({item.name for item in method.outputs}) != len(method.outputs):
            diagnostics.append(
                Diagnostic(
                    "pytorch_edge_invalid", "duplicate method output name", method_name=method.name
                )
            )
        if method.name not in supported_methods:
            diagnostics.append(
                Diagnostic(
                    "pytorch_method_unsupported",
                    f"unsupported method graph {method.name!r}",
                    method_name=method.name,
                )
            )
        validate_graph(method, method.name)
    for group in state.groups:
        validate_group(group.id)
    return ValidationReport(
        tuple(
            item
            if item.metablock_id is not None
            else replace(item, metablock_id=state.metablock_id)
            for item in diagnostics
        )
    )


def _find_cycle(adjacency: Mapping[str, set[str]]) -> tuple[str, ...] | None:
    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []

    def visit(node: str) -> tuple[str, ...] | None:
        if node in visiting:
            start = path.index(node)
            return tuple(path[start:] + [node])
        if node in visited:
            return None
        visiting.add(node)
        path.append(node)
        for neighbor in sorted(adjacency.get(node, ())):
            found = visit(neighbor)
            if found:
                return found
        path.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    for node in sorted(adjacency):
        found = visit(node)
        if found:
            return found
    return None
