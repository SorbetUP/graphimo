"""Unit guards for the static kernel snippets sent to the marimo kernel."""

from __future__ import annotations

import json
import textwrap

from graphimo.service.kernel_action_sources import metablock_action_source, notebook_action_source

NOTEBOOK_ACTIONS = [
    "inspect",
    "add_cell",
    "restore_cells",
    "connect",
    "run",
    "delete",
    "edit_guarded",
    "replace_group_selection",
    "disable_instance",
    "enable_instance",
    "replace_library",
    "set_literal",
    "disconnect",
    "refresh_component_hashes",
    "capture_output",
]

INSTANCE_ACTIONS = ["disable_instance", "enable_instance", "set_literal", "disconnect"]


def _payload() -> str:
    return json.dumps(
        {
            "cell_id": "abc",
            "instance_id": "deadbeef",
            "input_name": "values",
            "value": [1, 2],
            "_graphimo_project_path": "/tmp/project/nb.py",
        }
    )


def _compiles(source: str) -> bool:
    wrapped = "async def __cell__():\n" + textwrap.indent(source, "    ")
    compile(wrapped, "<kernel-action>", "exec")
    return True


def test_every_notebook_action_source_compiles() -> None:
    for action in NOTEBOOK_ACTIONS:
        assert _compiles(notebook_action_source(action, _payload())), action


def test_metablock_action_source_compiles() -> None:
    assert _compiles(metablock_action_source(_payload()))


def test_instance_actions_accept_cell_id_or_instance_id() -> None:
    for action in INSTANCE_ACTIONS:
        source = notebook_action_source(action, _payload())
        assert "payload.get('cell_id')" in source, action
        assert "payload.get('instance_id')" in source, action


def test_run_action_guards_empty_graph_and_missing_cell() -> None:
    source = notebook_action_source("run", _payload())
    assert "except KeyError" in source, "graph may be empty before the first run"
    assert "cell_not_found" in source


def test_capture_output_resolves_multi_output_index_zero() -> None:
    source = notebook_action_source("capture_output", _payload())
    # output_index == 0 must still select element 0 of a multi-output node
    # instead of being treated as falsy and capturing the whole tuple.
    assert "output_count" in source
    assert "output_count > 1" in source
    assert "value = value[output_index]" in source


def test_connect_action_guards_empty_graph() -> None:
    source = notebook_action_source("connect", _payload())
    assert "except KeyError" in source
