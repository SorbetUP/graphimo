"""Host-independent PyTorch MetaBlock projections for Graphimo panels/canvas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .bindings import RuntimeBindingMap
from .compiler import CompileResult
from .diagnostics import Diagnostic
from .registry import ELEMENT_REGISTRY, ParameterSpec
from .revision import graph_revision
from .runtime import RuntimeModelInfo, runtime_registry
from .schema import GroupInstance, PyTorchMetaBlockState
from .shapes import ShapeInferenceResult, TensorSpec, infer_model_shapes, tensor_spec


@dataclass(frozen=True, slots=True)
class PaletteItem:
    kind: str
    category: str
    input_ports: tuple[str, ...]
    output_ports: tuple[str, ...]
    parameters: tuple[ParameterSpec, ...]


@dataclass(frozen=True, slots=True)
class ParameterInspectorItem:
    name: str
    type: str
    value: object
    minimum: float | int | None
    maximum: float | int | None
    choices: tuple[object, ...] | None
    structural: bool
    external_parameter_id: str | None = None


@dataclass(frozen=True, slots=True)
class ElementCard:
    element_id: str
    kind: str
    name: str
    input_shapes: tuple[tuple[str, TensorSpec], ...]
    output_shapes: tuple[tuple[str, TensorSpec], ...]
    parameters: tuple[ParameterInspectorItem, ...]
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class EdgeProjection:
    edge_id: str
    source_element_id: str
    source_port: str
    target_element_id: str
    target_port: str
    tensor: TensorSpec | None
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class GroupNavigationItem:
    group_id: str
    name: str
    repeat_count: int
    instance_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MetaBlockOuterSummary:
    metablock_id: str
    name: str
    graph_revision: str
    input_parameters: tuple[tuple[str, str, object], ...]
    entrypoints: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PyTorchWorkbenchProjection:
    summary: MetaBlockOuterSummary
    active_scope_id: str | None
    active_scope_name: str
    method_tabs: tuple[str, ...]
    palette: tuple[PaletteItem, ...]
    cards: tuple[ElementCard, ...]
    edges: tuple[EdgeProjection, ...]
    diagnostics: tuple[Diagnostic, ...]
    group_navigation: tuple[GroupNavigationItem, ...]
    generated_source: str | None
    bindings: RuntimeBindingMap | None
    runtime_instances: tuple[RuntimeModelInfo, ...]


_CATEGORIES = {
    "input": "I/O", "output": "I/O",
    "embedding": "Layers", "linear": "Layers", "rms_norm": "Layers", "dropout": "Layers",
    "relu": "Activations", "gelu": "Activations", "silu": "Activations", "softmax": "Activations",
    "reshape": "Tensor transforms", "transpose": "Tensor transforms", "flatten": "Tensor transforms",
    "unflatten": "Tensor transforms", "split_heads": "Tensor transforms", "merge_heads": "Tensor transforms",
    "concatenate": "Tensor transforms", "add": "Math / attention", "multiply": "Math / attention",
    "scale": "Math / attention", "matmul": "Math / attention", "rope": "Math / attention",
    "causal_mask": "Math / attention", "cross_entropy": "Losses", "group_instance": "Structure",
}


def project_workbench(
    state: PyTorchMetaBlockState,
    *,
    method_name: str = "forward",
    active_group_id: str | None = None,
    compile_result: CompileResult | None = None,
    external_values: Mapping[str, object] | None = None,
    shape_result: ShapeInferenceResult | None = None,
) -> PyTorchWorkbenchProjection:
    """Project semantic data to renderer-safe view models; never inspects nn.Module."""
    shape_result = shape_result or infer_model_shapes(state, external_values)
    groups = {group.id: group for group in state.groups}
    if active_group_id is None:
        scope = next((item for item in state.entrypoints if item.name == method_name), None)
        scope_name = method_name
        scope_elements = scope.elements if scope else ()
        scope_edges = scope.edges if scope else ()
        scope_instances = scope.group_instances if scope else ()
    else:
        scope = groups.get(active_group_id)
        scope_name = scope.name if scope else active_group_id
        scope_elements = scope.elements if scope else ()
        scope_edges = scope.edges if scope else ()
        scope_instances = scope.group_instances if scope else ()
    shape_prefix = f"group:{active_group_id}:" if active_group_id is not None else ""

    ext_values = {item.id: item.default for item in state.external_parameters}
    ext_values.update(external_values or {})
    external_by_id = {item.id: item for item in state.external_parameters}
    diagnostics_by_element: dict[str, list[Diagnostic]] = {}
    for diagnostic in shape_result.diagnostics:
        if diagnostic.element_id:
            diagnostics_by_element.setdefault(diagnostic.element_id, []).append(diagnostic)

    cards: list[ElementCard] = []
    if active_group_id is not None and scope is not None:
        for port in scope.inputs:
            spec = tensor_spec(port.tensor) if port.tensor is not None else None
            cards.append(ElementCard(port.id, "input", port.name, (), (("out", spec),) if spec else (), (), ()))
        for port in scope.outputs:
            spec = tensor_spec(port.tensor) if port.tensor is not None else None
            cards.append(ElementCard(port.id, "output", port.name, (("x", spec),) if spec else (), (), (), ()))
    for element in scope_elements:
        specs = ELEMENT_REGISTRY.get(element.kind)
        if specs is None:
            continue
        incoming = tuple(
            (edge.target.port, shape_result.edge_specs.get(_edge_shape_key(shape_result, edge.id, shape_prefix)))
            for edge in scope_edges if edge.target.element_id == element.id
        )
        incoming_shapes = tuple((name, tensor) for name, tensor in incoming if tensor is not None)
        output_shapes = tuple(
            (port.name, shape_result.output_specs[shape_prefix + key])
            for port in specs.output_ports
            if (key := f"{element.id}.{port.name}") is not None
            and shape_prefix + key in shape_result.output_specs
        )
        parameters = []
        for descriptor in specs.parameters:
            binding = element.parameter_bindings.get(descriptor.name)
            external_id = binding.external_parameter_id if binding else None
            if binding:
                external = external_by_id.get(binding.external_parameter_id)
                value = ext_values.get(binding.external_parameter_id, external.default if external else descriptor.default)
            else:
                value = element.params.get(descriptor.name, descriptor.default)
            parameters.append(ParameterInspectorItem(
                descriptor.name, descriptor.type, value, descriptor.minimum, descriptor.maximum,
                descriptor.choices, descriptor.structural, external_id,
            ))
        cards.append(ElementCard(
            element.id, element.kind, element.name, incoming_shapes, output_shapes,
            tuple(parameters), tuple(diagnostics_by_element.get(element.id, ())),
        ))

    # Group instances are cards in the enclosing scope and expose their concrete repetition count.
    for instance in scope_instances:
        definition = groups.get(instance.group_id)
        input_shapes = tuple(
            (edge.target.port, tensor)
            for edge in scope_edges if edge.target.element_id == instance.id
            if (tensor := shape_result.edge_specs.get(_edge_shape_key(shape_result, edge.id, shape_prefix))) is not None
        )
        output_shapes = tuple(
            (port.name, shape_result.output_specs[shape_prefix + f"{instance.id}.{port.name}"])
            for port in (definition.outputs if definition else ())
            if shape_prefix + f"{instance.id}.{port.name}" in shape_result.output_specs
        )
        cards.append(ElementCard(
            instance.id, "group_instance", instance.name, input_shapes, output_shapes, (),
            tuple(diagnostics_by_element.get(instance.id, ())),
        ))

    edge_views = []
    for edge in scope_edges:
        tensor = shape_result.edge_specs.get(_edge_shape_key(shape_result, edge.id, shape_prefix))
        relevant = tuple(item for item in shape_result.diagnostics if item.element_id in {edge.source.element_id, edge.target.element_id})
        edge_views.append(EdgeProjection(edge.id, edge.source.element_id, edge.source.port,
                                         edge.target.element_id, edge.target.port, tensor, relevant))

    usage: dict[str, list[GroupInstance]] = {group.id: [] for group in state.groups}
    for method in state.entrypoints:
        for instance in method.group_instances:
            usage.setdefault(instance.group_id, []).append(instance)
    for group in state.groups:
        for instance in group.group_instances:
            usage.setdefault(instance.group_id, []).append(instance)
    group_navigation = tuple(
        GroupNavigationItem(group.id, group.name,
                            max((instance.repeat for instance in usage.get(group.id, ())), default=1),
                            tuple(instance.id for instance in usage.get(group.id, ())))
        for group in state.groups
    )
    summary = MetaBlockOuterSummary(
        state.metablock_id, state.name, graph_revision(state),
        tuple((item.id, item.name, ext_values.get(item.id, item.default)) for item in state.external_parameters),
        tuple(item.name for item in state.entrypoints),
    )
    palette = tuple(
        PaletteItem(kind.kind, _CATEGORIES[kind.kind], tuple(port.name for port in kind.input_ports),
                    tuple(port.name for port in kind.output_ports), kind.parameters)
        for kind in ELEMENT_REGISTRY.values() if kind.kind != "method_call"
        if kind.kind != "group_instance" or bool(state.groups)
    )
    return PyTorchWorkbenchProjection(
        summary, active_group_id, scope_name,
        tuple(item.name for item in state.entrypoints), palette, tuple(cards), tuple(edge_views),
        shape_result.diagnostics, group_navigation,
        compile_result.generated_source if compile_result else None,
        compile_result.bindings if compile_result else None,
        tuple(runtime_registry.list_for_metablock(state.metablock_id)),
    )


def _edge_shape_key(result: ShapeInferenceResult, edge_id: str, prefix: str = "") -> str | None:
    suffix = f":{edge_id}"
    return next((
        key for key in result.edge_specs
        if key.endswith(suffix) and (prefix in key if prefix else "group:" not in key)
    ), None)

