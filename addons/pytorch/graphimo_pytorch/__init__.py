"""Separately installable PyTorch MetaBlock addon.

Schema, serialization and registration imports intentionally do not import
PyTorch. Runtime modules will import torch only when their addon features are
implemented and used.
"""

from .diagnostics import Diagnostic, GraphimoPytorchError
from .bindings import GroupRuntimeIndex, RuntimeBindingMap, RuntimeSite
from .compiler import CompileResult, compile_model
from .definition import PyTorchModelDefinition
from .editing import demote_parameter, promote_parameter, set_element_parameter
from .revision import graph_revision
from .registry import ELEMENT_REGISTRY, ElementKind, ParameterSpec, PortSpec, element_kinds, get_element_kind
from .schema import (
    EdgeRecord,
    ElementRecord,
    Endpoint,
    ExternalParameter,
    GroupDefinition,
    GroupInstance,
    GroupPort,
    MethodGraph,
    MethodInput,
    MethodOutput,
    ParameterBinding,
    PyTorchMetaBlockState,
    TensorContract,
)
from .serialization import canonical_state_json, load_state, save_state, state_from_dict, state_to_dict
from .version import __version__
from .runtime import RuntimeModelInfo, RuntimeRegistry, release_runtime_model, runtime_registry
from .workbench_projection import PyTorchWorkbenchProjection, project_workbench
from .shapes import Dim, ShapeInferenceResult, TensorSpec, infer_model_shapes, tensor_spec
from .validation import ValidationReport, validate_model_graph

__all__ = [
    "Diagnostic",
    "GraphimoPytorchError",
    "CompileResult",
    "compile_model",
    "PyTorchModelDefinition",
    "RuntimeSite",
    "GroupRuntimeIndex",
    "RuntimeBindingMap",
    "RuntimeModelInfo",
    "RuntimeRegistry",
    "runtime_registry",
    "release_runtime_model",
    "PyTorchWorkbenchProjection",
    "project_workbench",
    "set_element_parameter",
    "promote_parameter",
    "demote_parameter",
    "PyTorchMetaBlockState",
    "ElementRecord",
    "Endpoint",
    "EdgeRecord",
    "TensorContract",
    "MethodInput",
    "MethodOutput",
    "MethodGraph",
    "GroupPort",
    "GroupDefinition",
    "GroupInstance",
    "ExternalParameter",
    "ParameterBinding",
    "canonical_state_json",
    "load_state",
    "save_state",
    "state_from_dict",
    "state_to_dict",
    "graph_revision",
    "ELEMENT_REGISTRY",
    "ElementKind",
    "ParameterSpec",
    "PortSpec",
    "element_kinds",
    "get_element_kind",
    "Dim",
    "TensorSpec",
    "ShapeInferenceResult",
    "infer_model_shapes",
    "tensor_spec",
    "ValidationReport",
    "validate_model_graph",
    "__version__",
]
