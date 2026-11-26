"""Optional PyTorch MetaBlock addon; imports PyTorch lazily."""

from .model_definition import (
    GroupRuntimeIndex,
    PyTorchModelDefinition,
    RuntimeBindingMap,
    RuntimeModelInfo,
    RuntimeSite,
    materialize_definition,
)
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
from .runtime import (
    PyTorchEventBus,
    PyTorchEvent,
    RuntimeRegistry,
    SubscriptionSpec,
    emit_cuda_resources,
    emit_metric,
    release_runtime_model,
    summarize_tensor,
    summarize_tree,
)

__all__ = [
    "ELEMENT_REGISTRY",
    "Dim",
    "CompileResult",
    "ElementKind",
    "ParameterSpec",
    "PortSpec",
    "PyTorchModelDefinition",
    "PyTorchEventBus",
    "PyTorchEvent",
    "GroupRuntimeIndex",
    "RuntimeBindingMap",
    "RuntimeModelInfo",
    "RuntimeSite",
    "RuntimeRegistry",
    "SubscriptionSpec",
    "PyTorchCompilationError",
    "ShapeInferenceResult",
    "TensorSpec",
    "ValidationReport",
    "get_element_kind",
    "compile_model",
    "infer_model_shapes",
    "emit_cuda_resources",
    "emit_metric",
    "materialize_definition",
    "release_runtime_model",
    "summarize_tensor",
    "summarize_tree",
    "validate_model_graph",
]
