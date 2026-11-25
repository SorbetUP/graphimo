"""Separately installable PyTorch MetaBlock addon.

Schema, serialization and registration imports intentionally do not import
PyTorch. Runtime modules will import torch only when their addon features are
implemented and used.
"""

from .diagnostics import Diagnostic, GraphimoPytorchError
from .revision import graph_revision
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

__all__ = [
    "Diagnostic",
    "GraphimoPytorchError",
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
    "__version__",
]
