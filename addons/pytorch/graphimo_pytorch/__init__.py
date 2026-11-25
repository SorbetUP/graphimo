"""Optional PyTorch MetaBlock addon; imports PyTorch lazily."""

from .model_definition import PyTorchModelDefinition, materialize_definition

__all__ = ["PyTorchModelDefinition", "materialize_definition"]
