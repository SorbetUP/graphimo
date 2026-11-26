"""Canonical ``$ref`` / ``$seed`` resolution shared by the service batch API and the Verifier.

Both the service protocol (batch operations) and the scenario replay used to
carry their own copy of the resolver; they now delegate to this module so a
reference means exactly the same thing everywhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class RefResolutionError(ValueError):
    """A ``$ref`` or ``$seed`` placeholder could not be resolved."""

    code = "invalid_reference"


def resolve_references(value: Any, previous: Mapping[str, Any], *, seed: int | None = None) -> Any:
    """Resolve ``{"$ref": "step.result.field"}`` placeholders against prior results.

    ``{"$seed": true}`` resolves to the scenario/batch seed when one is provided
    (``None`` otherwise raises), so seeded scenarios can inject their durable
    seed into generated sources without hand-wiring it through ``$ref``.
    """
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            reference = value["$ref"]
            if not isinstance(reference, str):
                raise RefResolutionError("$ref must be text")
            parts = reference.split(".")
            if len(parts) < 2 or parts[0] not in previous or parts[1] != "result":
                raise RefResolutionError(f"reference does not name a prior result: {reference!r}")
            resolved = previous[parts[0]]
            for part in parts[2:]:
                if isinstance(resolved, Mapping) and part in resolved:
                    resolved = resolved[part]
                elif isinstance(resolved, list) and part.isdecimal():
                    try:
                        resolved = resolved[int(part)]
                    except IndexError as exc:
                        raise RefResolutionError(
                            f"reference index is out of range: {reference!r}"
                        ) from exc
                else:
                    raise RefResolutionError(f"reference path does not exist: {reference!r}")
            return resolved
        if set(value) == {"$seed"}:
            if value["$seed"] is not True:
                raise RefResolutionError("$seed must be true")
            if seed is None:
                raise RefResolutionError("no seed is available in this context")
            return seed
        return {key: resolve_references(item, previous, seed=seed) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_references(item, previous, seed=seed) for item in value]
    return value
