"""Graphimo's lightweight Python node authoring and inspection API."""

from .node import inspect_node, is_node, node
from .preview import describe_result, preview_value

__all__ = [
    "node",
    "is_node",
    "inspect_node",
    "preview_value",
    "describe_result",
]
