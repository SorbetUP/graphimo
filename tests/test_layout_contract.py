from __future__ import annotations

import json

from adapter.graphimo_adapter import merge_layout, read_layout, write_layout


def test_layout_round_trip_contains_presentation_state_only(tmp_path):
    path = tmp_path / ".graphimo.json"
    cells = {
        "stable-cell-id": {"x": 120, "y": 180, "collapsed": False},
        "other-cell": {"x": 420, "y": 180},
    }

    write_layout(path, "fixture.py", cells)
    data = json.loads(path.read_text(encoding="utf-8"))

    assert data == {
        "version": 1,
        "notebooks": {"fixture.py": {"cells": cells}},
    }
    serialized = json.dumps(data).lower()
    for forbidden in ("source", "refs", "defs", "edges", "status", "output", "execution"):
        assert forbidden not in serialized
    assert read_layout(path, "fixture.py") == cells


def test_layout_is_scoped_by_notebook_path_and_preserves_other_layouts(tmp_path):
    path = tmp_path / ".graphimo.json"
    write_layout(path, "one.py", {"cell-a": {"x": 1, "y": 2}})
    write_layout(path, "two.py", {"cell-b": {"x": 3, "y": 4}})

    assert read_layout(path, "one.py") == {"cell-a": {"x": 1, "y": 2}}
    assert read_layout(path, "two.py") == {"cell-b": {"x": 3, "y": 4}}


def test_merge_restores_matching_ids_ignores_deleted_ids_and_places_new_cells():
    current = [
        {"id": "kept", "source": "a = 1"},
        {"id": "new", "source": "b = a"},
    ]
    saved = {
        "kept": {"x": 88, "y": 144, "collapsed": True},
        "deleted": {"x": 7, "y": 9},
    }

    merged = merge_layout(current, saved)

    by_id = {cell["id"]: cell for cell in merged}
    assert by_id["kept"]["x"] == 88
    assert by_id["kept"]["y"] == 144
    assert by_id["kept"]["collapsed"] is True
    assert "deleted" not in by_id
    assert isinstance(by_id["new"]["x"], (int, float))
    assert isinstance(by_id["new"]["y"], (int, float))
    assert by_id["new"]["source"] == "b = a"


def test_missing_or_invalid_layout_falls_back_to_empty_saved_positions(tmp_path):
    path = tmp_path / ".graphimo.json"

    assert read_layout(path, "fixture.py") == {}
    path.write_text("{broken", encoding="utf-8")
    assert read_layout(path, "fixture.py") == {}
