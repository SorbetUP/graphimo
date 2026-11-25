"""Stable semantic-element to generated-module paths."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GroupRuntimeIndex:
    group_instance_id: str
    repetition_index: int


@dataclass(frozen=True, slots=True)
class RuntimeSite:
    logical_element_id: str
    module_path: str
    group_instance_path: tuple[GroupRuntimeIndex, ...]
    inspectable: bool


@dataclass(frozen=True, slots=True)
class RuntimeBindingMap:
    sites: tuple[RuntimeSite, ...]

    def for_element(self, element_id: str) -> tuple[RuntimeSite, ...]:
        return tuple(site for site in self.sites if site.logical_element_id == element_id)

