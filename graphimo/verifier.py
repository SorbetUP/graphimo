"""Small verification harness that exercises Graphimo through its public client API."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterator

from .client import GraphimoClient, GraphimoProject


class VerificationError(AssertionError):
    """A Graphimo invariant or expected notebook result did not hold."""


class ScenarioRunError(VerificationError):
    """A scenario step failed; ``journal`` contains the replayable failure record."""

    def __init__(self, message: str, journal: dict[str, Any]):
        super().__init__(message)
        self.journal = journal


class VerifierScenario:
    """Declarative sequence of named Graphimo API actions with a reproducible seed."""

    def __init__(
        self, name: str, seed: int, *, invariants: tuple[str, ...] = ("graph_consistent",)
    ):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("scenario name must be non-empty text")
        if type(seed) is not int or seed < 0:
            raise ValueError("scenario seed must be a non-negative integer")
        self.name = name
        self.seed = seed
        self.invariants = _validate_invariants(invariants)
        self.steps: list[dict[str, Any]] = []

    def add(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        step_id: str | None = None,
        expect_error: str | None = None,
        invariants: tuple[str, ...] | None = None,
        assertions: list[dict[str, Any]] | None = None,
    ) -> VerifierScenario:
        """Append an action and return ``self`` for concise scenario declarations."""
        index = len(self.steps) + 1
        identifier = step_id or f"step_{index:03d}"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
            raise ValueError("scenario step id must contain 1-128 letters, digits, '_' or '-'")
        if any(item["id"] == identifier for item in self.steps):
            raise ValueError(f"duplicate scenario step id: {identifier}")
        if not isinstance(action, str) or not action:
            raise ValueError("scenario action must be non-empty text")
        actual_args = {} if args is None else args
        if not isinstance(actual_args, dict):
            raise ValueError("scenario action args must be an object")
        if assertions is not None and (
            not isinstance(assertions, list)
            or any(not isinstance(item, dict) for item in assertions)
        ):
            raise ValueError("scenario assertions must be an array of objects")
        if expect_error is not None and (not isinstance(expect_error, str) or not expect_error):
            raise ValueError("expected error code must be non-empty text")
        step = {
            "id": identifier,
            "action": action,
            "args": actual_args,
            "invariants": list(
                self.invariants if invariants is None else _validate_invariants(invariants)
            ),
            "assertions": assertions or [],
        }
        if expect_error is not None:
            step["expect_error"] = expect_error
        _ensure_json(step)
        self.steps.append(step)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": self.name,
            "seed": self.seed,
            "invariants": list(self.invariants),
            "steps": [dict(step) for step in self.steps],
        }

    def save(self, path: str | Path) -> Path:
        """Write the declarative scenario as bounded, portable JSON."""
        destination = Path(path)
        _write_json_atomic(destination, self.to_dict())
        return destination

    @classmethod
    def load(cls, path: str | Path) -> VerifierScenario:
        """Load and validate a saved scenario definition."""
        source = Path(path)
        if source.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("scenario definition exceeds 4 MiB")
        payload = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("unsupported Graphimo Verifier scenario format")
        scenario = cls(
            payload.get("name"),
            payload.get("seed"),
            invariants=tuple(payload.get("invariants", ())),
        )
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("scenario must contain at least one step")
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("scenario steps must be objects")
            scenario.add(
                step.get("action"),
                step.get("args", {}),
                step_id=step.get("id"),
                expect_error=step.get("expect_error"),
                invariants=tuple(step.get("invariants", ())),
                assertions=step.get("assertions", []),
            )
        return scenario

    def replay(
        self, project: VerifiedProject, *, journal_path: str | Path | None = None
    ) -> dict[str, Any]:
        """Execute steps through ``GraphimoProject.execute`` and journal every result.

        References use the service batch syntax, for example
        ``{"$ref": "add_source.result.cell_id"}``. The seed is durable scenario
        metadata; scenario authors should use it in any generated notebook source
        or model/data setup whose randomness needs reproducible control.
        """
        if not self.steps:
            raise ValueError("scenario must contain at least one step")
        started = _timestamp()
        journal: dict[str, Any] = {
            "schema_version": 1,
            "scenario": self.to_dict(),
            "seed": self.seed,
            "started_at": started,
            "operations": [],
            "completed": False,
        }
        results: dict[str, Any] = {}
        _persist_journal(journal, journal_path)
        for step in self.steps:
            event: dict[str, Any] = {
                "sequence": len(journal["operations"]) + 1,
                "id": step["id"],
                "action": step["action"],
                "started_at": _timestamp(),
            }
            try:
                resolved_args = _resolve_scenario_refs(step["args"], results)
                event["resolved_args"] = resolved_args
                try:
                    result = project.execute(step["action"], resolved_args)
                except Exception as exc:
                    error_code = getattr(exc, "code", "graphimo_action_failed")
                    if step.get("expect_error") != error_code:
                        event.update(
                            {
                                "status": "error",
                                "error": {"code": error_code, "message": str(exc)[:2000]},
                            }
                        )
                        raise ScenarioRunError(
                            f"scenario seed={self.seed} failed at step {step['id']}: {error_code}",
                            journal,
                        ) from exc
                    event.update(
                        {
                            "status": "expected_error",
                            "error": {"code": error_code, "message": str(exc)[:2000]},
                        }
                    )
                    result = None
                else:
                    if step.get("expect_error"):
                        event.update(
                            {
                                "status": "error",
                                "error": {
                                    "code": "expected_error_missing",
                                    "message": f"expected {step['expect_error']!r}, action succeeded",
                                },
                            }
                        )
                        raise ScenarioRunError(
                            f"scenario seed={self.seed} did not receive expected error at {step['id']}",
                            journal,
                        )
                    event.update({"status": "ok", "result": result})
                    results[step["id"]] = result
                checks = [self._run_invariant(project, name) for name in step["invariants"]]
                event["invariants"] = checks
                event["assertions"] = [
                    self._run_assertion(project, _resolve_scenario_refs(assertion, results))
                    for assertion in step["assertions"]
                ]
                event["finished_at"] = _timestamp()
                journal["operations"].append(event)
                _persist_journal(journal, journal_path)
            except ScenarioRunError as exc:
                if event not in journal["operations"]:
                    event["finished_at"] = _timestamp()
                    journal["operations"].append(event)
                journal["finished_at"] = _timestamp()
                _persist_journal(journal, journal_path)
                exc.journal = journal
                raise
            except Exception as exc:
                event.update(
                    {
                        "status": "assertion_failed",
                        "error": {"code": "verifier_assertion_failed", "message": str(exc)[:2000]},
                        "finished_at": _timestamp(),
                    }
                )
                journal["operations"].append(event)
                journal["finished_at"] = _timestamp()
                _persist_journal(journal, journal_path)
                raise ScenarioRunError(
                    f"scenario seed={self.seed} failed at step {step['id']}: {exc}", journal
                ) from exc
        journal["completed"] = True
        journal["finished_at"] = _timestamp()
        _persist_journal(journal, journal_path)
        return journal

    @staticmethod
    def _run_invariant(project: VerifiedProject, name: str) -> dict[str, Any]:
        if name == "graph_consistent":
            return {"name": name, "result": project.assert_graph_consistent()}
        if name == "no_errors":
            return {"name": name, "result": project.assert_no_errors()}
        if name == "formal_ports_consistent":
            return {"name": name, "result": project.assert_formal_ports_consistent()}
        if name == "binding_edges_complete":
            return {"name": name, "result": project.assert_binding_edges_complete()}
        raise VerificationError(f"unknown Graphimo Verifier invariant: {name}")

    @staticmethod
    def _run_assertion(project: VerifiedProject, assertion: dict[str, Any]) -> dict[str, Any]:
        kind = assertion.get("kind")
        if kind == "blocked_by":
            result = project.assert_blocked_by(
                assertion.get("cell_id"), assertion.get("dependency_id")
            )
        elif kind == "source_equals":
            result = project.assert_source_equals(assertion.get("cell_id"), assertion.get("equals"))
        elif kind == "output_preview":
            cell_id = assertion.get("cell_id")
            output_name = assertion.get("output_name")
            expected = assertion.get("equals")
            wait_seconds = assertion.get("wait_seconds", 0)
            if type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= 30:
                raise VerificationError("output preview wait_seconds must be between 0 and 30")
            deadline = time.monotonic() + wait_seconds
            while True:
                try:
                    result = project.assert_output(cell_id, output_name, expected)
                    break
                except VerificationError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        elif kind == "event_received":
            result = project.assert_event_received(
                assertion.get("event_kind"),
                metablock_id=assertion.get("metablock_id"),
                cursor=assertion.get("cursor", "0"),
                wait_seconds=assertion.get("wait_seconds", 5),
            )
        else:
            raise VerificationError(f"unknown Graphimo Verifier assertion: {kind!r}")
        return {"kind": kind, "result": result}


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

    def run_scenario(
        self, scenario: VerifierScenario, *, journal_path: str | Path | None = None
    ) -> dict[str, Any]:
        """Replay a declarative scenario through this project's service actions."""
        return scenario.replay(self, journal_path=journal_path)

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
            disabled = source.get("graphimo_disabled_instance")
            instance = source.get("graphimo_instance")
            preserved_output = (
                isinstance(disabled, dict) and disabled.get("result_var") == variable
            ) or (isinstance(instance, dict) and instance.get("result_var") == variable)
            if not isinstance(variable, str) or (
                variable not in source.get("defs", []) and not preserved_output
            ):
                raise VerificationError(f"edge {edge!r} has no matching source definition")
            if variable not in target.get("refs", []):
                raise VerificationError(f"edge {edge!r} has no matching target reference")
        return {"cells": len(cells), "edges": len(edges), "consistent": True}

    def assert_formal_ports_consistent(self) -> dict[str, Any]:
        """Every Graphimo instance projects a coherent formal port model."""
        snapshot = self.inspect_notebook()
        if snapshot.get("truncated"):
            raise VerificationError("notebook snapshot is truncated; port check is incomplete")
        cells = snapshot.get("cells")
        if not isinstance(cells, list):
            raise VerificationError("notebook snapshot has no cell array")
        known_states = {
            "ok",
            "idle",
            "stale",
            "blocked",
            "disabled",
            "incomplete",
            "component_unavailable",
            "error",
        }
        known_kinds = {
            "variable",
            "literal",
            "default",
            "unbound",
            "output_reference",
            "expression",
        }
        checked = 0
        for cell in cells:
            instance = cell.get("graphimo_instance") if isinstance(cell, dict) else None
            if not isinstance(instance, dict):
                continue
            checked += 1
            state = instance.get("state")
            if state not in known_states:
                raise VerificationError(
                    f"instance {instance.get('instance_id')!r} has unknown state {state!r}"
                )
            input_names = set()
            for port in instance.get("inputs", []):
                if not isinstance(port, dict) or not isinstance(port.get("name"), str):
                    raise VerificationError(
                        f"instance {instance.get('instance_id')!r} has a malformed input port"
                    )
                if port["name"] in input_names:
                    raise VerificationError(
                        f"instance {instance.get('instance_id')!r} has duplicate input {port['name']!r}"
                    )
                input_names.add(port["name"])
                binding = port.get("binding")
                if not isinstance(binding, dict) or binding.get("kind") not in known_kinds:
                    raise VerificationError(
                        f"instance {instance.get('instance_id')!r} input {port['name']!r} has invalid binding {binding!r}"
                    )
            indices = [output.get("index") for output in instance.get("outputs", [])]
            if indices != list(range(len(indices))):
                raise VerificationError(
                    f"instance {instance.get('instance_id')!r} output indices are not contiguous: {indices!r}"
                )
            bound_names = {binding.get("name") for binding in instance.get("bindings", [])}
            orphaned_names = {item.get("name") for item in instance.get("orphaned_bindings", [])}
            if bound_names & input_names and (bound_names - input_names) != orphaned_names:
                raise VerificationError(
                    f"instance {instance.get('instance_id')!r} bindings/ports disagree: "
                    f"bound={sorted(bound_names)!r} inputs={sorted(input_names)!r} orphaned={sorted(orphaned_names)!r}"
                )
        return {"instances": checked, "consistent": True}

    def assert_binding_edges_complete(self) -> dict[str, Any]:
        """Every bound input of an instance has a matching annotated edge."""
        snapshot = self.inspect_notebook()
        if snapshot.get("truncated"):
            raise VerificationError("notebook snapshot is truncated; edge check is incomplete")
        cells = snapshot.get("cells")
        edges = snapshot.get("edges")
        if not isinstance(cells, list) or not isinstance(edges, list):
            raise VerificationError("notebook snapshot must contain cell and edge arrays")
        by_id = {cell.get("id"): cell for cell in cells if isinstance(cell, dict)}
        edge_keys = {
            (edge.get("target_cell_id"), edge.get("input_ref"))
            for edge in edges
            if isinstance(edge, dict)
        }
        missing = []
        for cell in cells:
            instance = cell.get("graphimo_instance") if isinstance(cell, dict) else None
            if not isinstance(instance, dict):
                continue
            for port in instance.get("inputs", []):
                binding = port.get("binding") if isinstance(port, dict) else None
                if not isinstance(binding, dict) or binding.get("kind") in {"default", "unbound"}:
                    continue
                if (cell.get("id"), port.get("name")) not in edge_keys:
                    missing.append({"cell_id": cell.get("id"), "input": port.get("name")})
        if missing:
            raise VerificationError(f"bound inputs without annotated edges: {missing[:8]!r}")
        # Instance sources with several outputs must carry an output_index.
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source = by_id.get(edge.get("source_cell_id"))
            source_instance = source.get("graphimo_instance") if isinstance(source, dict) else None
            if isinstance(source_instance, dict) and len(source_instance.get("outputs", [])) > 1:
                if (
                    edge.get("variable") == source_instance.get("result_var")
                    and "output_index" not in edge
                ):
                    raise VerificationError(f"multi-output edge lacks output_index: {edge!r}")
        return {"edges": len(edges), "complete": True}

    def assert_no_errors(self) -> dict[str, Any]:
        """Assert that no cell in the live notebook snapshot reports an error."""
        snapshot = self.inspect_notebook()
        if snapshot.get("truncated"):
            raise VerificationError("notebook snapshot is truncated; error check is incomplete")
        cells = snapshot.get("cells")
        if not isinstance(cells, list):
            raise VerificationError("notebook snapshot has no cell array")
        errors = [
            {"cell_id": cell.get("id"), "error": error}
            for cell in cells
            if isinstance(cell, dict)
            for error in cell.get("errors", [])
        ]
        if errors:
            raise VerificationError(f"notebook contains cell errors: {errors[:8]!r}")
        return {"cells": len(cells), "errors": 0}

    def assert_blocked_by(self, cell_id: str, dependency_id: str) -> dict[str, Any]:
        """Assert a cell is transitively blocked by a specified disabled ancestor."""
        snapshot = self.inspect_notebook()
        if snapshot.get("truncated"):
            raise VerificationError("notebook snapshot is truncated; blocked state is unknown")
        blocked_by = snapshot.get("blocked_by")
        if not isinstance(blocked_by, dict):
            raise VerificationError("notebook snapshot has no blocked dependency map")
        dependencies = blocked_by.get(cell_id, [])
        if dependency_id not in dependencies:
            raise VerificationError(
                f"cell {cell_id!r} is not blocked by dependency {dependency_id!r}; got {dependencies!r}"
            )
        return {"cell_id": cell_id, "blocked_by": list(dependencies)}

    def assert_source_equals(self, cell_id: str, expected_source: str) -> dict[str, Any]:
        """Assert that one live cell's source is unchanged byte-for-byte."""
        snapshot = self.inspect_notebook()
        if snapshot.get("truncated"):
            raise VerificationError("notebook snapshot is truncated; source check is incomplete")
        cells = snapshot.get("cells")
        if not isinstance(cells, list):
            raise VerificationError("notebook snapshot has no cell array")
        cell = next(
            (item for item in cells if isinstance(item, dict) and item.get("id") == cell_id),
            None,
        )
        if cell is None:
            raise VerificationError(f"cell {cell_id!r} is absent from the notebook snapshot")
        actual = cell.get("source")
        if cell.get("source_truncated") or actual != expected_source:
            raise VerificationError(f"source for cell {cell_id!r} changed")
        return {
            "cell_id": cell_id,
            "source_sha256": hashlib.sha256(actual.encode("utf-8")).hexdigest(),
        }

    def assert_event_received(
        self,
        event_kind: str,
        *,
        metablock_id: str | None = None,
        cursor: str = "0",
        wait_seconds: float = 5,
    ) -> dict[str, Any]:
        """Wait for a bounded service event matching a kernel/addon event kind."""
        if not isinstance(event_kind, str) or not event_kind:
            raise VerificationError("event_kind must be non-empty text")
        if type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= 30:
            raise VerificationError("event wait_seconds must be between 0 and 30")
        deadline = time.monotonic() + wait_seconds
        current_cursor = cursor
        while True:
            response = self.project.events(
                cursor=current_cursor,
                limit=128,
                wait_ms=min(750, max(0, int((deadline - time.monotonic()) * 1000))),
            )
            current_cursor = response.get("cursor", current_cursor)
            for event in response.get("events", []):
                addon_event = event.get("addon_event")
                if not isinstance(addon_event, dict):
                    continue
                if addon_event.get("kind") != event_kind:
                    continue
                if metablock_id is not None and addon_event.get("metablock_id") != metablock_id:
                    continue
                return {
                    "event_kind": event_kind,
                    "metablock_id": addon_event.get("metablock_id"),
                    "sequence": event.get("sequence"),
                    "cursor": current_cursor,
                }
            if time.monotonic() >= deadline:
                raise VerificationError(
                    f"timed out waiting for event {event_kind!r} from MetaBlock {metablock_id!r}"
                )

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
        if snapshot.get("truncated"):
            raise VerificationError("notebook snapshot is truncated; output check is incomplete")
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


def _validate_invariants(invariants: tuple[str, ...]) -> tuple[str, ...]:
    allowed = {"graph_consistent", "no_errors", "formal_ports_consistent", "binding_edges_complete"}
    if not isinstance(invariants, tuple) or any(
        not isinstance(item, str) or item not in allowed for item in invariants
    ):
        raise ValueError(f"scenario invariants must be selected from {sorted(allowed)!r}")
    return invariants


def _ensure_json(value: Any) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("scenario values must be finite JSON data") from exc
    if len(encoded) > 4 * 1024 * 1024:
        raise ValueError("scenario data exceeds 4 MiB")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _resolve_scenario_refs(value: Any, previous: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            reference = value["$ref"]
            if not isinstance(reference, str):
                raise VerificationError("scenario reference must be text")
            parts = reference.split(".")
            if len(parts) < 2 or parts[0] not in previous or parts[1] != "result":
                raise VerificationError(
                    f"scenario reference does not name a prior result: {reference!r}"
                )
            resolved = previous[parts[0]]
            for part in parts[2:]:
                if isinstance(resolved, dict) and part in resolved:
                    resolved = resolved[part]
                elif isinstance(resolved, list) and part.isdecimal():
                    try:
                        resolved = resolved[int(part)]
                    except IndexError as exc:
                        raise VerificationError(
                            f"scenario reference index is out of range: {reference!r}"
                        ) from exc
                else:
                    raise VerificationError(
                        f"scenario reference path does not exist: {reference!r}"
                    )
            return resolved
        return {key: _resolve_scenario_refs(item, previous) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_scenario_refs(item, previous) for item in value]
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    _ensure_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _persist_journal(journal: dict[str, Any], path: str | Path | None) -> None:
    if path is not None:
        _write_json_atomic(Path(path), journal)
