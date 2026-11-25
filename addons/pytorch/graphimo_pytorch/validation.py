"""Graph and semantic validation for the PyTorch addon dialect."""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from typing import Mapping

from .diagnostics import Diagnostic
from .registry import ELEMENT_REGISTRY, ElementKind
from .schema import GroupDefinition, GroupInstance, PyTorchMetaBlockState


@dataclass(frozen=True, slots=True)
class ValidationReport:
    diagnostics: tuple[Diagnostic, ...]

    @property
    def valid(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)


_RESERVED_MODULE_NAMES = {
    "training", "_parameters", "_buffers", "_non_persistent_buffers_set", "_backward_hooks",
    "_forward_hooks", "_forward_pre_hooks", "_state_dict_hooks", "_load_state_dict_pre_hooks",
    "_load_state_dict_post_hooks", "_modules", "forward", "register_buffer", "add_module",
}


def validate_model_graph(
    state: PyTorchMetaBlockState,
    external_values: Mapping[str, object] | None = None,
) -> ValidationReport:
    """Collect safely discoverable schema, edge, parameter, group and cycle errors."""
    external_values = external_values or {}
    diagnostics: list[Diagnostic] = []
    groups_by_id: dict[str, GroupDefinition] = {}
    group_names: dict[str, str] = {}
    for group in state.groups:
        if group.id in groups_by_id:
            diagnostics.append(Diagnostic("pytorch_duplicate_group_id", f"Duplicate Group id {group.id!r}"))
        groups_by_id[group.id] = group
        generated_name = _sanitize_name(group.name)
        if not generated_name:
            diagnostics.append(Diagnostic("pytorch_module_name_invalid", f"Invalid generated Group class name {group.name!r}"))
        if generated_name in group_names:
            diagnostics.append(Diagnostic("pytorch_module_path_conflict", f"Generated Group class name {generated_name!r} is used by {group_names[generated_name]!r} and {group.id!r}"))
        group_names[generated_name] = group.id

    method_names = [method.name for method in state.entrypoints]
    if len(method_names) != len(set(method_names)):
        diagnostics.append(Diagnostic("pytorch_duplicate_method", "Method graph names must be unique"))
    for name in method_names:
        if name not in {"forward", "loss", "predict"}:
            diagnostics.append(Diagnostic("pytorch_method_unsupported", f"Unsupported method graph name {name!r}"))

    for method in state.entrypoints:
        diagnostics.extend(_validate_scope(
            method.elements, method.edges, method.group_instances, groups_by_id,
            state, external_values, method.inputs, method.outputs,
            scope_name=f"method {method.name}",
        ))
    methods_by_name = {method.name: method for method in state.entrypoints}
    for method in state.entrypoints:
        for element in method.elements:
            if element.kind != "method_call":
                continue
            target_name = element.params.get("method")
            output_name = element.params.get("output")
            target = methods_by_name.get(str(target_name))
            if target is None:
                diagnostics.append(Diagnostic(
                    "pytorch_method_call_target_invalid",
                    f"Method call references missing method {target_name!r}",
                    element_id=element.id,
                ))
                continue
            if len(target.inputs) != 1:
                diagnostics.append(Diagnostic(
                    "pytorch_method_call_arity_unsupported",
                    "V1 method-call elements support target methods with exactly one input",
                    element_id=element.id,
                ))
            if len(target.outputs) != 1:
                diagnostics.append(Diagnostic(
                    "pytorch_method_call_output_arity_unsupported",
                    "V1 method-call elements support target methods with exactly one output",
                    element_id=element.id,
                ))
            if not any(output.id == output_name for output in target.outputs):
                diagnostics.append(Diagnostic(
                    "pytorch_method_call_output_invalid",
                    f"Method {target.name!r} has no output {output_name!r}",
                    element_id=element.id,
                ))
            connected_inputs = sum(
                edge.target.element_id == element.id and edge.target.port == "x"
                for edge in method.edges
            )
            if connected_inputs != len(target.inputs):
                diagnostics.append(Diagnostic(
                    "pytorch_method_call_arity_invalid",
                    f"Method {target.name!r} expects {len(target.inputs)} input(s), got {connected_inputs}",
                    element_id=element.id,
                ))
    method_calls = {
        method.name: [
            str(element.params.get("method"))
            for element in method.elements
            if element.kind == "method_call" and str(element.params.get("method")) in methods_by_name
        ]
        for method in state.entrypoints
    }
    complete: set[str] = set()
    active: list[str] = []
    reported_cycles: set[tuple[str, ...]] = set()

    def visit_method(name: str) -> None:
        if name in active:
            cycle = tuple(active[active.index(name):] + [name])
            if cycle not in reported_cycles:
                reported_cycles.add(cycle)
                diagnostics.append(Diagnostic(
                    "pytorch_method_call_recursive",
                    f"Recursive method calls are unsupported: {' -> '.join(cycle)}",
                ))
            return
        if name in complete:
            return
        active.append(name)
        for target_name in method_calls.get(name, ()):
            visit_method(target_name)
        active.pop()
        complete.add(name)

    for method_name in method_calls:
        visit_method(method_name)
    for group in state.groups:
        diagnostics.extend(_validate_scope(
            group.elements, group.edges, group.group_instances, groups_by_id,
            state, external_values, (), (), scope_name=f"group {group.id}",
            group_inputs=group.inputs, group_outputs=group.outputs,
        ))

    # Detect recursive group definitions independently of use sites.
    graph = {group.id: [item.group_id for item in group.group_instances] for group in state.groups}
    visiting: set[str] = set()
    visited: set[str] = set()
    reported: set[tuple[str, str]] = set()

    def visit_group(group_id: str, trail: tuple[str, ...]) -> None:
        if group_id in visiting:
            cycle_start = trail.index(group_id) if group_id in trail else 0
            cycle = trail[cycle_start:] + (group_id,)
            key = (cycle[0], "->".join(cycle))
            if key not in reported:
                reported.add(key)
                diagnostics.append(Diagnostic("pytorch_group_recursive", f"Recursive Group definitions: {' -> '.join(cycle)}"))
            return
        if group_id in visited:
            return
        visiting.add(group_id)
        for target in graph.get(group_id, ()):
            if target in graph:
                visit_group(target, trail + (target,))
        visiting.remove(group_id)
        visited.add(group_id)

    for group_id in graph:
        visit_group(group_id, (group_id,))
    return ValidationReport(tuple(diagnostics))


def _validate_scope(
    elements: tuple[object, ...], edges: tuple[object, ...], instances: tuple[GroupInstance, ...],
    groups: Mapping[str, GroupDefinition], state: PyTorchMetaBlockState,
    external_values: Mapping[str, object], inputs: tuple[object, ...], outputs: tuple[object, ...],
    *, scope_name: str, group_inputs: tuple[object, ...] = (), group_outputs: tuple[object, ...] = (),
) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    elements_by_id = {item.id: item for item in elements}
    instances_by_id = {item.id: item for item in instances}
    ids = [*(item.id for item in elements), *(item.id for item in instances)]
    if len(ids) != len(set(ids)):
        result.append(Diagnostic("pytorch_duplicate_element_id", f"Duplicate element/instance id in {scope_name}"))
    edge_ids = [item.id for item in edges]
    if len(edge_ids) != len(set(edge_ids)):
        result.append(Diagnostic("pytorch_duplicate_edge_id", f"Duplicate edge id in {scope_name}"))

    external_by_id = {item.id: item for item in state.external_parameters}
    for element in elements:
        kind = ELEMENT_REGISTRY.get(element.kind)
        if kind is None:
            result.append(Diagnostic("pytorch_element_unknown", f"Unknown element kind {element.kind!r}", element_id=element.id))
            continue
        generated_name = _sanitize_name(element.name)
        if not generated_name or generated_name in _RESERVED_MODULE_NAMES:
            result.append(Diagnostic("pytorch_module_name_invalid", f"Invalid generated Python module name {element.name!r}", element_id=element.id))
        params = dict(element.params)
        for parameter, binding in element.parameter_bindings.items():
            if parameter in params:
                result.append(Diagnostic("pytorch_parameter_binding_conflict", f"Parameter {parameter!r} has both a literal and external binding", element_id=element.id))
            external = external_by_id.get(binding.external_parameter_id)
            if external is None:
                result.append(Diagnostic("pytorch_external_parameter_unresolved", f"External parameter {binding.external_parameter_id!r} does not exist", element_id=element.id))
                continue
            value = external_values.get(external.id, external.default)
            if not _matches_parameter_type(value, external.type):
                result.append(Diagnostic("pytorch_external_parameter_type_invalid", f"Value for {external.name!r} must be {external.type}", element_id=element.id))
                continue
            params[parameter] = value  # type: ignore[assignment]
        for diagnostic in kind.validate_params(params):
            result.append(Diagnostic(diagnostic.code, f"{element.name}: {diagnostic.message}", diagnostic.severity, element.id))
        # Semantic module paths are unique within a generated class.
    names: dict[str, str] = {}
    for element in elements:
        if element.kind in ELEMENT_REGISTRY:
            generated_name = _sanitize_name(element.name)
            previous = names.get(generated_name)
            if previous is not None:
                result.append(Diagnostic("pytorch_module_path_conflict", f"Generated module name {generated_name!r} is used by {previous!r} and {element.id!r}", element_id=element.id))
            names[generated_name] = element.id

    for instance in instances:
        if instance.group_id not in groups:
            result.append(Diagnostic("pytorch_group_reference_invalid", f"Group instance {instance.id!r} references missing Group {instance.group_id!r}", element_id=instance.id))
        if type(instance.repeat) is not int or instance.repeat < 1:
            result.append(Diagnostic("pytorch_group_repeat_invalid", "Group repetition must be an integer >= 1", element_id=instance.id))
        generated_name = _sanitize_name(instance.name)
        if not generated_name or generated_name in _RESERVED_MODULE_NAMES:
            result.append(Diagnostic("pytorch_module_name_invalid", f"Invalid generated Python module name {instance.name!r}", element_id=instance.id))
        previous = names.get(generated_name)
        if previous is not None:
            result.append(Diagnostic("pytorch_module_path_conflict", f"Generated module name {generated_name!r} is used by {previous!r} and {instance.id!r}", element_id=instance.id))
        names[generated_name] = instance.id

    input_ids = {item.id for item in inputs}
    output_ids = {item.id for item in outputs}
    seen_method_inputs: set[str] = set()
    seen_method_outputs: set[str] = set()
    for element in elements:
        if element.kind == "input" and inputs:
            method_input = element.params.get("method_input")
            if method_input not in input_ids:
                result.append(Diagnostic("pytorch_method_input_unresolved", "Input element must reference a declared method_input", element_id=element.id))
            elif method_input in seen_method_inputs:
                result.append(Diagnostic("pytorch_method_input_duplicate", f"Method input {method_input!r} has multiple input elements", element_id=element.id))
            else:
                seen_method_inputs.add(method_input)
        if element.kind == "output" and outputs:
            method_output = element.params.get("method_output")
            if method_output not in output_ids:
                result.append(Diagnostic("pytorch_method_output_unresolved", "Output element must reference a declared method_output", element_id=element.id))
            elif method_output in seen_method_outputs:
                result.append(Diagnostic("pytorch_method_output_duplicate", f"Method output {method_output!r} has multiple output elements", element_id=element.id))
            else:
                seen_method_outputs.add(method_output)
    for missing in input_ids - seen_method_inputs:
        result.append(Diagnostic("pytorch_method_input_unresolved", f"Declared method input {missing!r} has no input element"))
    for missing in output_ids - seen_method_outputs:
        result.append(Diagnostic("pytorch_method_output_unresolved", f"Declared method output {missing!r} has no output element"))

    in_counts: dict[tuple[str, str], int] = {}
    adjacency: dict[str, list[str]] = {item: [] for item in ids}
    for edge in edges:
        source = _port_kind(edge.source.element_id, edge.source.port, "output", elements_by_id, instances_by_id, groups, group_inputs, group_outputs)
        target = _port_kind(edge.target.element_id, edge.target.port, "input", elements_by_id, instances_by_id, groups, group_inputs, group_outputs)
        if not source:
            result.append(Diagnostic("pytorch_edge_source_invalid", f"Missing source endpoint or output port {edge.source.element_id}.{edge.source.port}"))
        if not target:
            result.append(Diagnostic("pytorch_edge_target_invalid", f"Missing target endpoint or input port {edge.target.element_id}.{edge.target.port}"))
        if source is not None and target is not None:
            key = (edge.target.element_id, edge.target.port)
            in_counts[key] = in_counts.get(key, 0) + 1
            if in_counts[key] > 1 and not _is_variadic_target(edge.target.element_id, elements_by_id, instances_by_id, groups):
                result.append(Diagnostic("pytorch_input_multiple_edges", f"Input {edge.target.element_id}.{edge.target.port} has multiple incoming edges"))
            if edge.source.element_id in adjacency and edge.target.element_id in adjacency:
                adjacency[edge.source.element_id].append(edge.target.element_id)

    for element in elements:
        kind = ELEMENT_REGISTRY.get(element.kind)
        if kind is None:
            continue
        for port in kind.input_ports:
            if port.required and not port.variadic and in_counts.get((element.id, port.name), 0) == 0:
                result.append(Diagnostic("pytorch_input_unconnected", f"Required input {element.id}.{port.name} is unconnected", element_id=element.id))
    for instance in instances:
        definition = groups.get(instance.group_id)
        if definition:
            for port in definition.inputs:
                if in_counts.get((instance.id, port.name), 0) == 0:
                    result.append(Diagnostic("pytorch_input_unconnected", f"Required Group input {instance.id}.{port.name} is unconnected", element_id=instance.id))

    for port in group_inputs:
        if in_counts.get((port.id, "x"), 0):
            result.append(Diagnostic("pytorch_group_input_edge_invalid", f"Group input {port.id!r} cannot be an edge target"))
    for port in group_outputs:
        if in_counts.get((port.id, "x"), 0) != 1:
            result.append(Diagnostic("pytorch_group_output_unresolved", f"Group output {port.id!r} must have one incoming edge"))

    # DFS gives a deterministic diagnostic for every independent back edge.
    states: dict[str, int] = {}
    reported_cycles: set[tuple[str, str]] = set()
    trail: list[str] = []

    def visit(node: str) -> None:
        states[node] = 1
        trail.append(node)
        for target in adjacency.get(node, ()):
            if states.get(target) == 1:
                start = trail.index(target)
                cycle = tuple(trail[start:] + [target])
                key = (cycle[0], "->".join(cycle))
                if key not in reported_cycles:
                    reported_cycles.add(key)
                    result.append(Diagnostic("pytorch_graph_cycle", f"Directed cycle in {scope_name}: {' -> '.join(cycle)}", element_id=target))
            elif states.get(target, 0) == 0:
                visit(target)
        trail.pop()
        states[node] = 2

    for node in adjacency:
        if states.get(node, 0) == 0:
            visit(node)
    return result


def _matches_parameter_type(value: object, declared: str) -> bool:
    return {"int": lambda: type(value) is int, "float": lambda: type(value) in (int, float),
            "bool": lambda: type(value) is bool, "str": lambda: isinstance(value, str)}[declared]()


def _sanitize_name(value: str) -> str:
    cleaned = re.sub(r"\W+", "_", value).strip("_")
    if not cleaned:
        return ""
    if cleaned[0].isdigit():
        cleaned = "module_" + cleaned
    if keyword.iskeyword(cleaned):
        cleaned += "_"
    return cleaned


def _port_kind(element_id: str, port: str, direction: str, elements: Mapping[str, object],
               instances: Mapping[str, GroupInstance], groups: Mapping[str, GroupDefinition],
               group_inputs: tuple[object, ...], group_outputs: tuple[object, ...]) -> bool | None:
    if element_id in elements:
        kind = ELEMENT_REGISTRY.get(elements[element_id].kind)
        if kind is None:
            return None
        declared = kind.output_ports if direction == "output" else kind.input_ports
        return any(item.name == port or item.variadic and port.startswith(item.name + "[") for item in declared)
    if element_id in instances:
        group = groups.get(instances[element_id].group_id)
        if group is None:
            return None
        declared_names = [item.name for item in (group.outputs if direction == "output" else group.inputs)]
        return port in declared_names
    if direction == "output" and any(item.id == element_id for item in group_inputs):
        return port == "out"
    if direction == "input" and any(item.id == element_id for item in group_outputs):
        return port == "x"
    return None


def _is_variadic_target(element_id: str, elements: Mapping[str, object], instances: Mapping[str, GroupInstance],
                        groups: Mapping[str, GroupDefinition]) -> bool:
    element = elements.get(element_id)
    if element is not None:
        kind: ElementKind | None = ELEMENT_REGISTRY.get(element.kind)
        return bool(kind and kind.variadic_input)
    instance = instances.get(element_id)
    group = groups.get(instance.group_id) if instance else None
    return bool(group and any(port.name == "inputs" for port in group.inputs))

