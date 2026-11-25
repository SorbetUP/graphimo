"""PyTorch addon's internal semantic element registry (P2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .diagnostics import Diagnostic
from .schema import JsonValue


@dataclass(frozen=True, slots=True)
class PortSpec:
    name: str
    variadic: bool = False
    required: bool = True


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    name: str
    type: str
    default: JsonValue
    minimum: float | int | None = None
    maximum: float | int | None = None
    choices: tuple[JsonValue, ...] | None = None
    structural: bool = True


@dataclass(frozen=True, slots=True)
class ElementKind:
    """Descriptor and pure shape/parameter semantics for one visual element."""

    kind: str
    input_ports: tuple[PortSpec, ...]
    output_ports: tuple[PortSpec, ...]
    parameters: tuple[ParameterSpec, ...] = ()
    variadic_input: bool = False
    required_parameters: tuple[str, ...] = ()

    def validate_params(self, params: Mapping[str, JsonValue]) -> tuple[Diagnostic, ...]:
        diagnostics: list[Diagnostic] = []
        declared = {parameter.name: parameter for parameter in self.parameters}
        for name in params.keys() - declared.keys():
            diagnostics.append(Diagnostic("pytorch_parameter_unknown", f"Unknown {self.kind} parameter: {name}"))
        for parameter in self.parameters:
            if parameter.name not in params:
                if parameter.name in self.required_parameters:
                    diagnostics.append(Diagnostic("pytorch_parameter_required", f"Missing required parameter {parameter.name!r}"))
                continue
            value = params[parameter.name]
            if not _matches_type(value, parameter.type):
                diagnostics.append(Diagnostic("pytorch_parameter_type_invalid", f"{parameter.name!r} must be {parameter.type}"))
                continue
            if parameter.minimum is not None and value < parameter.minimum:  # type: ignore[operator]
                diagnostics.append(Diagnostic("pytorch_parameter_range_invalid", f"{parameter.name!r} must be >= {parameter.minimum}"))
            if self.kind == "rms_norm" and parameter.name == "eps" and value <= 0:  # type: ignore[operator]
                diagnostics.append(Diagnostic("pytorch_parameter_range_invalid", "'eps' must be > 0"))
            if self.kind == "rope" and parameter.name == "base" and value <= 0:  # type: ignore[operator]
                diagnostics.append(Diagnostic("pytorch_parameter_range_invalid", "'base' must be > 0"))
            if self.kind == "dropout" and parameter.name == "p" and value >= 1:  # type: ignore[operator]
                diagnostics.append(Diagnostic("pytorch_parameter_range_invalid", "'p' must be < 1"))
            if parameter.maximum is not None and value > parameter.maximum:  # type: ignore[operator]
                diagnostics.append(Diagnostic("pytorch_parameter_range_invalid", f"{parameter.name!r} must be <= {parameter.maximum}"))
            if parameter.choices is not None and value not in parameter.choices:
                diagnostics.append(Diagnostic("pytorch_parameter_choice_invalid", f"{parameter.name!r} must be one of {parameter.choices!r}"))
        return tuple(diagnostics)

    def infer_shapes(self, inputs: tuple[object, ...], params: Mapping[str, object]) -> tuple[object, ...]:
        """Apply the addon's pure shape rule without importing PyTorch."""
        from .shapes import _infer_element

        return _infer_element(self.kind, inputs, params)


def _matches_type(value: JsonValue, expected: str) -> bool:
    return {
        "int": lambda: type(value) is int,
        "float": lambda: type(value) in (int, float),
        "bool": lambda: type(value) is bool,
        "str": lambda: isinstance(value, str),
        "int_or_symbol": lambda: type(value) is int or isinstance(value, str),
        "shape": lambda: isinstance(value, list),
    }.get(expected, lambda: False)()


def _p(name: str, type_: str, default: JsonValue = None, minimum: float | int | None = None,
       maximum: float | int | None = None, choices: tuple[JsonValue, ...] | None = None) -> ParameterSpec:
    return ParameterSpec(name, type_, default, minimum, maximum, choices)


_ONE_IN_ONE_OUT = (PortSpec("x"),), (PortSpec("out"),)
_TWO_IN_ONE_OUT = (PortSpec("a"), PortSpec("b")), (PortSpec("out"),)


def _kind(kind: str, parameters: tuple[ParameterSpec, ...] = (), *, inputs: tuple[PortSpec, ...] | None = None,
          variadic: bool = False, required: tuple[str, ...] = ()) -> ElementKind:
    default_inputs, outputs = _ONE_IN_ONE_OUT
    return ElementKind(kind, inputs or default_inputs, outputs, parameters, variadic, required)


_KINDS = (
    ElementKind("input", (), (PortSpec("out"),), (_p("name", "str", "input"), _p("shape", "shape", []), _p("dtype", "str", "float32"), _p("semantic", "str", None), _p("method_input", "str", None))),
    ElementKind("output", (PortSpec("x"),), (), (_p("name", "str", "output"), _p("method_output", "str", None))),
    _kind("embedding", (_p("num_embeddings", "int", None, 1), _p("embedding_dim", "int", None, 1), _p("init_std", "float", None, 0)), required=("num_embeddings", "embedding_dim")),
    _kind("linear", (_p("in_features", "int", None, 1), _p("out_features", "int", None, 1), _p("bias", "bool", True), _p("init_std", "float", None, 0)), required=("in_features", "out_features")),
    _kind("rms_norm", (_p("normalized_shape", "int", None, 1), _p("eps", "float", 1e-5, 0)), required=("normalized_shape",)),
    _kind("dropout", (_p("p", "float", 0.5, 0, 1))),
    _kind("relu"), _kind("gelu"), _kind("silu"),
    _kind("softmax", (_p("dim", "int", -1),)),
    _kind("reshape", (_p("target_shape", "shape", None),), required=("target_shape",)),
    _kind("transpose", (_p("dim0", "int", None), _p("dim1", "int", None)), required=("dim0", "dim1")),
    _kind("flatten", (_p("start_dim", "int", 0), _p("end_dim", "int", -1))),
    _kind("unflatten", (_p("dim", "int", None), _p("sizes", "shape", None)), required=("dim", "sizes")),
    _kind("split_heads", (_p("n_heads", "int", None, 1), _p("layout", "str", "e_t_d_to_e_h_t_dh", choices=("e_t_d_to_e_h_t_dh",))), required=("n_heads",)),
    _kind("merge_heads"),
    _kind("concatenate", (_p("dim", "int", 0),), inputs=(PortSpec("inputs", variadic=True),), variadic=True),
    ElementKind("add", *_TWO_IN_ONE_OUT), ElementKind("multiply", *_TWO_IN_ONE_OUT),
    _kind("scale", (_p("factor", "float", None),), required=("factor",)),
    ElementKind("matmul", *_TWO_IN_ONE_OUT),
    _kind("rope", (_p("base", "float", 10000.0, 0),)),
    _kind("causal_mask"),
    _kind("cross_entropy", (_p("mode", "str", "generic", choices=("generic", "next_token")),), inputs=(PortSpec("logits"), PortSpec("target"))),
    _kind("method_call", (_p("method", "str", None), _p("output", "str", None)), inputs=(PortSpec("x"),), required=("method", "output")),
    _kind("group_instance", (_p("group_id", "str", None), _p("repeat", "int", 1, 1))),
)

ELEMENT_REGISTRY: Mapping[str, ElementKind] = {item.kind: item for item in _KINDS}
_INTERNAL_KINDS = frozenset({"method_call"})


def get_element_kind(kind: str) -> ElementKind | None:
    return ELEMENT_REGISTRY.get(kind)


def element_kinds() -> tuple[ElementKind, ...]:
    """Return only user-insertable V1 elements, excluding compiler semantics."""
    return tuple(item for item in ELEMENT_REGISTRY.values() if item.kind not in _INTERNAL_KINDS)

