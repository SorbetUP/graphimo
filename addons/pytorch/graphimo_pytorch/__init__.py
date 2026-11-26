"""Optional PyTorch MetaBlock addon; imports PyTorch lazily."""

from .model_definition import PyTorchModelDefinition, materialize_definition
from .compiler import CompileResult, PyTorchCompilationError, compile_model
from .registry import (
    ELEMENT_REGISTRY,
    ElementKind,
    ParameterSpec,
    PortSpec,
    ValidationReport,
    get_element_kind,
    validate_model_graph,
)
from .shapes import Dim, ShapeInferenceResult, TensorSpec, infer_model_shapes

__all__ = [
    "ELEMENT_REGISTRY",
    "Dim",
    "CompileResult",
    "ElementKind",
    "ParameterSpec",
    "PortSpec",
    "PyTorchModelDefinition",
    "PyTorchCompilationError",
    "ShapeInferenceResult",
    "TensorSpec",
    "ValidationReport",
    "get_element_kind",
    "compile_model",
    "infer_model_shapes",
    "materialize_definition",
    "validate_model_graph",
]
