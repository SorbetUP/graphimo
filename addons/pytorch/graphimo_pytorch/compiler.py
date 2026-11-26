"""Deterministic trusted-source compiler for PyTorch MetaBlock graphs."""

from __future__ import annotations

import hashlib
import json
import keyword
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .model_definition import GroupRuntimeIndex, RuntimeBindingMap, RuntimeSite
from .registry import get_element_kind
from .schema import (
    Diagnostic,
    ElementRecord,
    GroupDefinition,
    GroupInstance,
    MethodGraph,
    PyTorchMetaBlockState,
    graph_revision,
)
from .shapes import ShapeInferenceResult, infer_model_shapes

JsonScalar = None | bool | int | float | str
COMPILER_VERSION = "1"
_IDENTIFIER = re.compile(r"\W")


@dataclass(frozen=True)
class CompileResult:
    generated_source: str
    factory: Callable[[], Any]
    bindings: RuntimeBindingMap
    shape_result: ShapeInferenceResult
    diagnostics: tuple[Diagnostic, ...]
    materialization_key: str


class PyTorchCompilationError(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...]):
        self.diagnostics = diagnostics
        self.code = diagnostics[0].code if diagnostics else "pytorch_codegen_failed"
        super().__init__("; ".join(item.message for item in diagnostics))


def _identifier(value: str, fallback: str = "GraphimoModel") -> str:
    value = _IDENTIFIER.sub("_", value.strip())
    value = re.sub(r"_+", "_", value).strip("_") or fallback
    if value[0].isdigit() or keyword.iskeyword(value):
        value = f"Graphimo_{value}"
    return value


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _comment(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ")


def _resolved_values(
    state: PyTorchMetaBlockState, external_values: Mapping[str, JsonScalar] | None
) -> tuple[dict[str, JsonScalar], dict[str, JsonScalar]]:
    supplied = dict(external_values or {})
    by_id = {
        item.id: supplied.get(item.id, supplied.get(item.name, item.default))
        for item in state.external_parameters
    }
    by_name = {item.name: by_id[item.id] for item in state.external_parameters}
    return by_id, by_name


def _resolved_params(element: ElementRecord, by_id: Mapping[str, JsonScalar]) -> dict[str, Any]:
    descriptor = get_element_kind(element.kind)
    params = {item.name: item.default for item in descriptor.parameters} if descriptor else {}
    params.update(element.params)
    for name, binding in element.parameter_bindings.items():
        if binding.external_parameter_id in by_id:
            params[name] = by_id[binding.external_parameter_id]
    return params


def _parameter_literal(value: Any) -> str:
    if isinstance(value, tuple):
        return repr(tuple(value))
    if isinstance(value, list):
        return repr(tuple(value))
    return repr(value)


def _class_name(name: str, fallback: str) -> str:
    fragments = [part for part in re.split(r"[^A-Za-z0-9]+", name) if part]
    result = "".join(part[:1].upper() + part[1:] for part in fragments) or fallback
    if result[0].isdigit() or keyword.iskeyword(result):
        result = f"Graphimo{result}"
    return result


_HELPERS = """
class ReshapeOp(nn.Module):
    def __init__(self, shape):
        super().__init__()
        self.shape = tuple(shape)

    def forward(self, x):
        return x.reshape(self.shape)


class TransposeOp(nn.Module):
    def __init__(self, dim0, dim1):
        super().__init__()
        self.dim0, self.dim1 = dim0, dim1

    def forward(self, x):
        return x.transpose(self.dim0, self.dim1)


class SplitHeadsOp(nn.Module):
    def __init__(self, n_heads):
        super().__init__()
        self.n_heads = n_heads

    def forward(self, x):
        return x.unflatten(-1, (self.n_heads, -1)).transpose(1, 2)


class MergeHeadsOp(nn.Module):
    def forward(self, x):
        return x.transpose(1, 2).flatten(2)


class AddOp(nn.Module):
    def forward(self, left, right):
        return left + right


class MultiplyOp(nn.Module):
    def forward(self, left, right):
        return left * right


class ScaleOp(nn.Module):
    def __init__(self, factor):
        super().__init__()
        self.factor = factor

    def forward(self, x):
        return x * self.factor


class MatMulOp(nn.Module):
    def forward(self, left, right):
        return torch.matmul(left, right)


class RoPEOp(nn.Module):
    def __init__(self, base=10000.0):
        super().__init__()
        self.base = base

    def forward(self, x):
        d, t = x.shape[-1], x.shape[-2]
        inv_freq = 1.0 / self.base ** (torch.arange(0, d, 2, device=x.device, dtype=torch.float32) / d)
        ang = torch.outer(torch.arange(t, device=x.device, dtype=torch.float32), inv_freq)
        ang = torch.cat((ang, ang), dim=-1)
        x1, x2 = x[..., : d // 2], x[..., d // 2 :]
        return x * ang.cos().to(x.dtype) + torch.cat((-x2, x1), dim=-1) * ang.sin().to(x.dtype)


class CausalMaskOp(nn.Module):
    def forward(self, scores):
        length = scores.shape[-1]
        mask = torch.triu(torch.ones(length, length, dtype=torch.bool, device=scores.device), diagonal=1)
        return scores.masked_fill(mask, float("-inf"))


class ConcatenateOp(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def forward(self, *values):
        return torch.cat(values, dim=self.dim)


class CrossEntropyOp(nn.Module):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode

    def forward(self, logits, target):
        if self.mode == "next_token":
            return F.cross_entropy(logits[:, :-1].flatten(0, -2), target[:, 1:].flatten())
        return F.cross_entropy(logits, target)
""".strip()


def _module_expression(element: ElementRecord, by_id: Mapping[str, JsonScalar]) -> str:
    params = _resolved_params(element, by_id)
    kind = element.kind
    if kind in {"input", "output"}:
        return "nn.Identity()"
    if kind == "embedding":
        return f"nn.Embedding({params['num_embeddings']}, {params['embedding_dim']})"
    if kind == "linear":
        return f"nn.Linear({params['in_features']}, {params['out_features']}, bias={params.get('bias', True)!r})"
    if kind == "rms_norm":
        return f"nn.RMSNorm({params['normalized_shape']}, eps={params.get('eps', 1e-5)!r})"
    if kind == "dropout":
        return f"nn.Dropout({params.get('p', 0.5)!r})"
    if kind == "relu":
        return "nn.ReLU()"
    if kind == "gelu":
        return "nn.GELU()"
    if kind == "silu":
        return "nn.SiLU()"
    if kind == "softmax":
        return f"nn.Softmax(dim={params.get('dim', -1)})"
    if kind == "reshape":
        return f"ReshapeOp({_parameter_literal(params['target_shape'])})"
    if kind == "transpose":
        return f"TransposeOp({params['dim0']}, {params['dim1']})"
    if kind == "flatten":
        return f"nn.Flatten(start_dim={params['start_dim']}, end_dim={params['end_dim']})"
    if kind == "unflatten":
        return f"nn.Unflatten({params['dim']}, {_parameter_literal(params['sizes'])})"
    if kind == "split_heads":
        return f"SplitHeadsOp({params['n_heads']})"
    if kind == "merge_heads":
        return "MergeHeadsOp()"
    if kind == "add":
        return "AddOp()"
    if kind == "multiply":
        return "MultiplyOp()"
    if kind == "scale":
        if element.name == "q_scale" and "d_model" in by_id and "n_heads" in by_id:
            return f"ScaleOp(({by_id['d_model']} / {by_id['n_heads']}) ** -0.5)"
        return f"ScaleOp({params['factor']!r})"
    if kind == "matmul":
        return "MatMulOp()"
    if kind == "rope":
        return f"RoPEOp({params['base']!r})"
    if kind == "causal_mask":
        return "CausalMaskOp()"
    if kind == "concatenate":
        return f"ConcatenateOp({params['dim']})"
    if kind == "cross_entropy":
        return f"CrossEntropyOp({_quote(params['mode'])})"
    raise ValueError(f"cannot compile unsupported element kind {kind!r}")


def _topological_nodes(
    graph: MethodGraph | GroupDefinition,
) -> tuple[list[str], dict[str, ElementRecord], dict[str, GroupInstance]]:
    elements = {item.id: item for item in graph.elements}
    instances = {item.id: item for item in graph.group_instances}
    node_order = [item.id for item in graph.elements] + [item.id for item in graph.group_instances]
    outgoing = {node: set() for node in node_order}
    indegree = dict.fromkeys(node_order, 0)
    for edge in graph.edges:
        source, target = edge.source.element_id, edge.target.element_id
        if source in outgoing and target in outgoing and target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1
    ready = [node for node in node_order if indegree[node] == 0]
    order = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for child in node_order:
            if child in outgoing[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        ready.sort(key=node_order.index)
    if len(order) != len(node_order):
        raise ValueError("cannot generate source for a cyclic dataflow graph")
    return order, elements, instances


def _node_variable(identity: str, port: str, semantic_name: str | None = None) -> str:
    suffix = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:8]
    return (
        f"v_{_identifier(semantic_name or identity, 'node')}_{suffix}_{_identifier(port, 'output')}"
    )


def _emit_graph(
    graph: MethodGraph | GroupDefinition,
    *,
    method_name: str,
    input_names: tuple[str, ...],
    prefix: str,
    by_id: Mapping[str, JsonScalar],
    groups: Mapping[str, GroupDefinition],
    bindings: list[RuntimeSite],
    group_path: tuple[GroupRuntimeIndex, ...] = (),
    base_indent: int = 8,
    register_sites: bool = True,
    llama_group: bool = False,
    llama_model: bool = False,
    direct_return: bool = False,
) -> list[str]:
    indent = " " * base_indent
    order, elements, instances = _topological_nodes(graph)
    values: dict[tuple[str, str], str] = {}
    injected: dict[tuple[str, str], str] = {}
    if isinstance(graph, GroupDefinition):
        for port in graph.inputs:
            variable = _identifier(port.name, "input")
            injected[(port.endpoint.element_id, port.endpoint.port)] = variable
    lines: list[str] = []
    for identity in order:
        if identity in elements:
            element = elements[identity]
            descriptor = get_element_kind(element.kind)
            port_names = tuple(item.name for item in descriptor.input_ports) if descriptor else ()
            input_exprs: dict[str, list[str]] = {name: [] for name in port_names}
            for edge in graph.edges:
                if edge.target.element_id == identity:
                    expr = values.get((edge.source.element_id, edge.source.port))
                    if expr is not None:
                        input_exprs.setdefault(edge.target.port, []).append(expr)
            for (target_id, target_port), expr in injected.items():
                if target_id == identity:
                    input_exprs.setdefault(target_port, []).append(expr)
            output_port = (
                descriptor.output_ports[0].name
                if descriptor and descriptor.output_ports
                else "output"
            )
            output_var = _node_variable(identity, output_port, element.name)
            module_key = f"{prefix}__{element.name}" if prefix else element.name
            if element.kind == "input":
                name = element.params.get("name", element.name)
                method_call = element.metadata.get("method_call")
                if method_call == "forward":
                    call_args = element.metadata.get("arguments", [])
                    if not isinstance(call_args, list) or any(item not in input_names for item in call_args):
                        raise ValueError("forward method-call metadata must reference method inputs")
                    lines.append(
                        f"{indent}{output_var} = self.forward({', '.join(_identifier(item, 'input') for item in call_args)})"
                    )
                elif name not in input_names:
                    raise ValueError(
                        f"input element {element.id!r} has no matching method input {name!r}"
                    )
                elif not (llama_group or llama_model):
                    lines.append(
                        f"{indent}{output_var} = self.layers[{_quote(module_key)}]({_identifier(name, 'input')})"
                    )
                elif not method_call:
                    lines.append(f"{indent}{output_var} = {_identifier(name, 'input')}")
            else:
                ports = port_names
                args = []
                for port in ports:
                    expressions = input_exprs.get(port, [])
                    if descriptor and any(
                        item.name == port and item.variadic for item in descriptor.input_ports
                    ):
                        args.extend(expressions)
                    elif expressions:
                        args.append(expressions[0])
                access = (
                    f"self.{_identifier(element.name)}"
                    if llama_group
                    else _llama_element_access(element, module_key)
                    if llama_model
                    else None
                )
                if element.metadata.get("method_call") == "forward":
                    call_args = element.metadata.get("arguments", [])
                    if not isinstance(call_args, list) or any(item not in input_names for item in call_args):
                        raise ValueError("forward method-call metadata must reference method inputs")
                    lines.append(
                        f"{indent}{output_var} = self.forward({', '.join(_identifier(item, 'input') for item in call_args)})"
                    )
                else:
                    lines.append(
                        f"{indent}{output_var} = {(access or f'self.layers[{_quote(module_key)}]')}({', '.join(args)})"
                    )
            values[(identity, output_port)] = output_var
            module_path = (
                f"{prefix}.{element.name}"
                if llama_group and prefix
                else element.name
                if llama_model and element.name in {"embed_tokens", "norm", "lm_head"}
                else f"operations.{module_key}"
                if llama_model
                else f"layers.{module_key}"
            )
            if register_sites and not element.metadata.get("method_call") and not (llama_group and element.kind == "input"):
                bindings.append(RuntimeSite(element.id, module_path, group_path, True))
        else:
            instance = instances[identity]
            definition = groups[instance.group_id]
            repeat_count = _effective_group_repeat(instance, by_id, llama_model)
            args = []
            for port in definition.inputs:
                edge = next(
                    (
                        item
                        for item in graph.edges
                        if item.target.element_id == identity and item.target.port == port.name
                    ),
                    None,
                )
                expression = (
                    values.get((edge.source.element_id, edge.source.port)) if edge else None
                )
                if expression is None:
                    raise ValueError(
                        f"Group instance {instance.name!r} has an unresolved input {port.name!r}"
                    )
                args.append(expression)
            group_key = f"{prefix}__{instance.name}" if prefix else instance.name
            outputs = []
            for port in definition.outputs:
                variable = _node_variable(identity, port.name, f"{instance.name}_{port.name}")
                outputs.append(variable)
            llama_repeated = llama_model and instance.name == "layers"
            if repeat_count == 1:
                result_var = _node_variable(identity, "result", f"{instance.name}_result")
                call = "self.layers" if llama_repeated else f"self.groups[{_quote(group_key)}]"
                lines.append(f"{indent}{result_var} = {call}({', '.join(args)})")
                unpack = ", ".join(outputs)
                if len(outputs) == 1:
                    unpack += ","
                lines.append(f"{indent}{unpack} = {result_var}")
            else:
                current_args = args
                for index in range(repeat_count):
                    result_var = _node_variable(
                        identity, f"repeat_{index}", f"{instance.name}_repeat_{index}"
                    )
                    call = "self.layers" if llama_repeated else f"self.groups[{_quote(group_key)}]"
                    lines.append(f"{indent}{result_var} = {call}[{index}]({', '.join(current_args)})")
                    current_args = [f"{result_var}[0]"] if len(definition.outputs) == 1 else []
                    if len(definition.outputs) > 1:
                        current_args = [
                            f"{result_var}[{out_index}]"
                            for out_index in range(len(definition.outputs))
                        ]
                for index, port in enumerate(definition.outputs):
                    variable = _node_variable(identity, port.name, f"{instance.name}_{port.name}")
                    expression = current_args[index] if len(current_args) > 1 else current_args[0]
                    lines.append(f"{indent}{variable} = {expression}")
                    values[(identity, port.name)] = variable
            if repeat_count == 1:
                for port, variable in zip(definition.outputs, outputs, strict=True):
                    values[(identity, port.name)] = variable
            if register_sites:
                for index in range(repeat_count):
                    root_path = (
                        f"layers.{index}"
                        if llama_model and instance.name == "layers"
                        else f"groups.{group_key}.{index}"
                        if repeat_count > 1
                        else f"groups.{group_key}"
                    )
                    direct_group = definition.name == "LlamaDecoderLayer"
                    _add_runtime_sites(
                        definition,
                        root_path,
                        group_path + (GroupRuntimeIndex(instance.id, index),),
                        groups,
                        bindings,
                        llama_group=direct_group,
                    )
    if isinstance(graph, GroupDefinition):
        expressions = [
            values.get((port.endpoint.element_id, port.endpoint.port)) for port in graph.outputs
        ]
        if any(item is None for item in expressions):
            raise ValueError("Group output endpoint has no generated tensor")
        lines.append(f"{indent}return ({', '.join(expressions)},)")
    else:
        expressions = [
            values.get((port.endpoint.element_id, port.endpoint.port)) for port in graph.outputs
        ]
        if any(item is None for item in expressions):
            raise ValueError(f"method {method_name!r} output endpoint has no generated tensor")
        dict_items = ", ".join(
            f"{_quote(port.name)}: {expr}"
            for port, expr in zip(graph.outputs, expressions, strict=True)
        )
        if direct_return and len(expressions) == 1:
            lines.append(f"{indent}return {expressions[0]}")
        else:
            lines.append(f"{indent}return {{{dict_items}}}")
    return lines


def _llama_element_access(element: ElementRecord, module_key: str) -> str | None:
    if element.name in {"embed_tokens", "norm", "lm_head"}:
        return f"self.{element.name}"
    return f"self.operations[{_quote(module_key)}]"


def _effective_group_repeat(
    instance: GroupInstance, by_id: Mapping[str, JsonScalar], llama_model: bool
) -> int:
    if llama_model and instance.name == "layers":
        return int(by_id.get("n_layers", instance.repeat))
    return instance.repeat


def _add_runtime_sites(
    definition: GroupDefinition,
    root_path: str,
    group_path: tuple[GroupRuntimeIndex, ...],
    groups: Mapping[str, GroupDefinition],
    bindings: list[RuntimeSite],
    llama_group: bool = False,
) -> None:
    for element in definition.elements:
        if llama_group and element.kind == "input":
            continue
        bindings.append(
            RuntimeSite(
                element.id,
                f"{root_path}.{element.name}"
                if llama_group
                else f"{root_path}.layers.{element.name}",
                group_path,
                True,
            )
        )
    for instance in definition.group_instances:
        nested = groups[instance.group_id]
        for index in range(instance.repeat):
            nested_path = f"{root_path}.groups.{instance.name}"
            if instance.repeat > 1:
                nested_path += f".{index}"
            _add_runtime_sites(
                nested,
                nested_path,
                group_path + (GroupRuntimeIndex(instance.id, index),),
                groups,
                bindings,
                llama_group=False,
            )


def _emit_group_class(
    group: GroupDefinition,
    groups: Mapping[str, GroupDefinition],
    by_id: Mapping[str, JsonScalar],
    bindings: list[RuntimeSite],
    emitted: set[str],
    llama_template: bool = False,
) -> list[str]:
    if group.id in emitted:
        return []
    lines: list[str] = []
    for instance in group.group_instances:
        nested = groups.get(instance.group_id)
        if nested:
            lines.extend(_emit_group_class(nested, groups, by_id, bindings, emitted, llama_template))
    emitted.add(group.id)
    class_name = _class_name(group.name, "Group")
    args = ", ".join(_identifier(port.name, "input") for port in group.inputs)
    direct = llama_template and group.name == "LlamaDecoderLayer"
    lines.extend([f"class {class_name}(nn.Module):", "    def __init__(self):", "        super().__init__()"])
    if not direct:
        lines.extend(["        self.layers = nn.ModuleDict()", "        self.groups = nn.ModuleDict()"])
    for element in group.elements:
        if element.kind == "input" and direct:
            continue
        if direct:
            lines.append(f"        self.{_identifier(element.name)} = {_module_expression(element, by_id)}")
            params = _resolved_params(element, by_id)
            if element.kind in {"embedding", "linear"} and params.get("init_std") is not None:
                lines.append(f"        nn.init.normal_(self.{_identifier(element.name)}.weight, mean=0.0, std={params['init_std']!r})")
        else:
            lines.append(f"        self.layers[{_quote(element.name)}] = {_module_expression(element, by_id)}")
    for instance in group.group_instances:
        nested = groups[instance.group_id]
        nested_name = _class_name(nested.name, "Group")
        if instance.repeat == 1:
            expression = f"{nested_name}()"
        else:
            expression = f"nn.ModuleList([{nested_name}() for _ in range({instance.repeat})])"
        lines.append(f"        self.groups[{_quote(instance.name)}] = {expression}")
    lines.extend(["", f"    def forward(self{', ' + args if args else ''}):"])
    lines.extend(
        _emit_graph(
            group,
            method_name=group.name,
            input_names=tuple(port.name for port in group.inputs),
            prefix="",
            by_id=by_id,
            groups=groups,
            bindings=bindings,
            base_indent=8,
            register_sites=False,
            llama_group=direct,
        )
    )
    lines.append("")
    return lines


def _generate_source(
    state: PyTorchMetaBlockState,
    by_id: Mapping[str, JsonScalar],
    materialization_key: str,
    revision: str,
) -> tuple[str, RuntimeBindingMap]:
    groups = {item.id: item for item in state.groups}
    bindings: list[RuntimeSite] = []
    lines = [
        "# Generated by graphimo-pytorch",
        f"# graphimo-metablock-id: {_comment(state.metablock_id)}",
        f"# graphimo-graph-revision: {revision}",
        f"# graphimo-materialization-key: {materialization_key}",
        "import torch",
        "import torch.nn as nn",
        "import torch.nn.functional as F",
        "",
        _HELPERS,
        "",
    ]
    emitted: set[str] = set()
    llama_template = state.metadata.get("template") in {"llama_causal_lm", "llama_decoder_layer"}
    for group in state.groups:
        lines.extend(_emit_group_class(group, groups, by_id, bindings, emitted, llama_template))
    model_class = str(state.metadata.get("generated_class_name") or _class_name(state.name, "GraphimoModel"))
    llama_model = state.metadata.get("template") == "llama_causal_lm"
    lines.extend(
        [
            f"class {model_class}(nn.Module):",
            "    def __init__(self):",
            "        super().__init__()",
        ]
    )
    if llama_model:
        lines.append("        self.operations = nn.ModuleDict()")
    else:
        lines.extend(["        self.layers = nn.ModuleDict()", "        self.groups = nn.ModuleDict()"])
    for method in state.entrypoints:
        prefix = method.name
        for element in method.elements:
            key = f"{prefix}__{element.name}"
            expression = _module_expression(element, by_id)
            if llama_model and element.name in {"embed_tokens", "norm", "lm_head"}:
                module_target = f"self.{element.name}"
            elif llama_model:
                module_target = f"self.operations[{_quote(key)}]"
            else:
                module_target = f"self.layers[{_quote(key)}]"
            lines.append(f"        {module_target} = {expression}")
            params = _resolved_params(element, by_id)
            if element.kind in {"embedding", "linear"} and params.get("init_std") is not None:
                lines.append(
                    f"        nn.init.normal_({module_target}.weight, mean=0.0, std={params['init_std']!r})"
                )
        for instance in method.group_instances:
            definition = groups[instance.group_id]
            group_class = _class_name(definition.name, "Group")
            repeat_count = _effective_group_repeat(instance, by_id, llama_model)
            expression = (
                f"{group_class}()"
                if repeat_count == 1
                else f"nn.ModuleList([{group_class}() for _ in range({repeat_count})])"
            )
            group_key = f"{method.name}__{instance.name}"
            if llama_model and instance.name == "layers":
                lines.append(f"        self.layers = {expression}")
            elif not llama_model:
                lines.append(f"        self.groups[{_quote(group_key)}] = {expression}")
    lines.append("")
    for method in state.entrypoints:
        args = ", ".join(_identifier(item.name, "input") for item in method.inputs)
        if method.name in state.metadata.get("no_grad_methods", []):
            lines.append("    @torch.no_grad()")
        lines.append(f"    def {method.name}(self{', ' + args if args else ''}):")
        lines.extend(
            _emit_graph(
                method,
                method_name=method.name,
                input_names=tuple(item.name for item in method.inputs),
                prefix=method.name,
                by_id=by_id,
                groups=groups,
                bindings=bindings,
                base_indent=8,
                llama_model=llama_model,
                direct_return=state.metadata.get("method_returns", {}).get(method.name) == "direct",
            )
        )
        lines.append("")
    source = "\n".join(lines).rstrip() + "\n"
    return source, RuntimeBindingMap(tuple(bindings))


def compile_model(
    state: PyTorchMetaBlockState,
    external_values: Mapping[str, JsonScalar] | None = None,
    *,
    addon_compiler_version: str = COMPILER_VERSION,
) -> CompileResult:
    """Validate, infer, generate and instantiate only addon-owned source."""
    by_id, by_name = _resolved_values(state, external_values)
    if state.metadata.get("template") == "llama_causal_lm":
        layer_count = by_id.get("n_layers")
        if type(layer_count) is not int or layer_count < 1:
            diagnostic = Diagnostic(
                "pytorch_param_invalid",
                "Llama n_layers must be a positive integer",
                metablock_id=state.metablock_id,
            )
            raise PyTorchCompilationError((diagnostic,))
    shape_result = infer_model_shapes(state, by_id)
    errors = tuple(item for item in shape_result.diagnostics if item.severity == "error")
    if errors:
        raise PyTorchCompilationError(errors)
    revision = graph_revision(state)
    key_payload = {
        "graph_revision": revision,
        "external_parameters": by_name,
        "compiler_version": addon_compiler_version,
    }
    materialization_key = hashlib.sha256(
        json.dumps(key_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    try:
        source, bindings = _generate_source(state, by_id, materialization_key, revision)
        namespace: dict[str, Any] = {"__name__": "_graphimo_pytorch_generated"}
        exec(
            compile(source, f"<graphimo-pytorch:{state.metablock_id}>", "exec"),
            namespace,
            namespace,
        )
        model_class_name = str(
            state.metadata.get("generated_class_name") or _class_name(state.name, "GraphimoModel")
        )
        model_class = namespace[model_class_name]
        import torch

        del torch
    except Exception as exc:  # noqa: BLE001 - generated-source defects become structured addon diagnostics
        diagnostic = Diagnostic(
            "pytorch_codegen_failed",
            f"generated PyTorch source failed: {exc}\n{source if 'source' in locals() else ''}",
            metablock_id=state.metablock_id,
        )
        raise PyTorchCompilationError((diagnostic,)) from exc
    return CompileResult(
        generated_source=source,
        factory=model_class,
        bindings=bindings,
        shape_result=shape_result,
        diagnostics=(),
        materialization_key=materialization_key,
    )
