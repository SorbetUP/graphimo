"""Optional PyTorch MetaBlock addon; imports PyTorch lazily."""

from .model_definition import (
    GroupRuntimeIndex,
    PyTorchModelDefinition,
    RuntimeBindingMap,
    RuntimeModelInfo,
    RuntimeSite,
    definition_from_state,
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
from .checkpoint import (
    CheckpointError,
    get_migration_report,
    inspect_checkpoint,
    load_checkpoint,
    preview_checkpoint_migration,
    save_checkpoint,
)
from .migration import ShapeMismatch, WeightMigrationReport, migrate_state_dict

__all__ = [
    "ELEMENT_REGISTRY",
    "Dim",
    "CompileResult",
    "CheckpointError",
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
    "definition_from_state",
    "ShapeMismatch",
    "WeightMigrationReport",
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
    "load_checkpoint",
    "preview_checkpoint_migration",
    "inspect_checkpoint",
    "get_migration_report",
    "migrate_state_dict",
    "release_runtime_model",
    "summarize_tensor",
    "summarize_tree",
    "save_checkpoint",
    "validate_model_graph",
]
