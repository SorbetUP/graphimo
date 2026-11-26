"""Small verification harness that exercises Graphimo through its public client API."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .client import GraphimoClient, GraphimoProject


class VerificationError(AssertionError):
    """A Graphimo invariant or expected notebook result did not hold."""


class GraphimoVerifier:
    """Open projects and verify them through the same service API as agents/UI."""

    def __init__(self, client: GraphimoClient):
        self.client = client

    @classmethod
    def connect_local(
        cls,
        *,
        manifest_path: str | Path | None = None,
        auto_start: bool = True,
        timeout: float = 10.0,
    ) -> GraphimoVerifier:
        """Connect to the authenticated loopback service, optionally starting it."""
        return cls(
            GraphimoClient.connect_local(
                manifest_path=manifest_path, auto_start=auto_start, timeout=timeout
            )
        )

    @contextmanager
    def project(self, path: str | Path) -> Iterator[VerifiedProject]:
        """Open one project for assertions and close its service session on exit."""
        project = VerifiedProject(self.client.open_project(path))
        try:
            yield project
        finally:
            project.close()

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> GraphimoVerifier:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


class VerifiedProject:
    """Assertions layered over a regular :class:`GraphimoProject` handle."""

    def __init__(self, project: GraphimoProject):
        self.project = project

    def execute(self, action: str, args: dict[str, Any] | None = None) -> Any:
        """Execute an ordinary named Graphimo application action."""
        return self.project.execute(action, args)

    def batch(
        self, operations: list[dict[str, Any]], *, continue_on_error: bool = False
    ) -> dict[str, Any]:
        """Run the service's ordered, reference-aware application batch."""
        return self.project.batch(operations, continue_on_error=continue_on_error)

    def inspect_notebook(self) -> dict[str, Any]:
        """Return the bounded live snapshot produced by the attached marimo kernel."""
        return self.project.inspect_notebook()

    def assert_graph_consistent(self) -> dict[str, Any]:
        """Check that every live edge joins existing cells and matching definitions."""
        snapshot = self.inspect_notebook()
        if snapshot.get("truncated"):
            raise VerificationError("notebook snapshot is truncated; graph consistency is unknown")
        cells = snapshot.get("cells")
        edges = snapshot.get("edges")
        if not isinstance(cells, list) or not isinstance(edges, list):
            raise VerificationError("notebook snapshot must contain cell and edge arrays")
        by_id: dict[str, dict[str, Any]] = {}
        for cell in cells:
            if not isinstance(cell, dict) or not isinstance(cell.get("id"), str):
                raise VerificationError("notebook snapshot contains a cell without a string id")
            cell_id = cell["id"]
            if cell_id in by_id:
                raise VerificationError(f"notebook snapshot contains duplicate cell id {cell_id!r}")
            by_id[cell_id] = cell

        for edge in edges:
            if not isinstance(edge, dict):
                raise VerificationError("notebook snapshot contains a malformed edge")
            source_id = edge.get("source_cell_id")
            target_id = edge.get("target_cell_id")
            variable = edge.get("variable")
            source = by_id.get(source_id) if isinstance(source_id, str) else None
            target = by_id.get(target_id) if isinstance(target_id, str) else None
            if source is None or target is None:
                raise VerificationError(f"edge {edge!r} points to a cell missing from the snapshot")
            if not isinstance(variable, str) or variable not in source.get("defs", []):
                raise VerificationError(f"edge {edge!r} has no matching source definition")
            if variable not in target.get("refs", []):
                raise VerificationError(f"edge {edge!r} has no matching target reference")
        return {"cells": len(cells), "edges": len(edges), "consistent": True}

    def assert_output(
        self,
        cell_id: str,
        output_name: str,
        expected_preview: str,
    ) -> dict[str, Any]:
        """Assert an inspected definition preview exactly matches expected text.

        Graphimo previews are intentionally bounded and JSON-safe. This compares
        the displayed preview representation, not the live Python object.
        """
        snapshot = self.inspect_notebook()
        cells = snapshot.get("cells")
        if not isinstance(cells, list):
            raise VerificationError("notebook snapshot has no cell array")
        cell = next((item for item in cells if item.get("id") == cell_id), None)
        if cell is None:
            raise VerificationError(f"cell {cell_id!r} is absent from the notebook snapshot")
        output = next(
            (
                item
                for item in cell.get("outputs", [])
                if isinstance(item, dict) and item.get("name") == output_name
            ),
            None,
        )
        if output is None:
            raise VerificationError(
                f"cell {cell_id!r} has no inspected output named {output_name!r}"
            )
        actual = output.get("preview")
        if actual != expected_preview:
            raise VerificationError(
                f"output {output_name!r} preview mismatch: expected {expected_preview!r}, got {actual!r}"
            )
        return {"cell_id": cell_id, "output_name": output_name, "preview": actual}

    def close(self) -> bool:
        return self.project.close()

    def __enter__(self) -> VerifiedProject:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()
