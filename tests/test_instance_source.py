from __future__ import annotations

import re

import pytest

from graphimo import (
    Binding,
    build_instance_source,
    new_instance_id,
    output_expression,
    parse_instance_source,
    reconnect_instance_source,
    result_variable,
)


def test_build_instance_source_matches_canonical_snapshot() -> None:
    source, spec = build_instance_source(
        "normalize",
        {"dataset": "g_load_a19d0c21", "mean": "0.0", "std": "1.0"},
        instance_id="4f2a7c91",
    )
    assert source == (
        "# Graphimo node: normalize\n"
        "# Graphimo instance: 4f2a7c91\n"
        "g_normalize_4f2a7c91 = normalize(\n"
        "    dataset=g_load_a19d0c21,\n"
        "    mean=0.0,\n"
        "    std=1.0,\n"
        ")"
    )
    assert spec.instance_id == "4f2a7c91"
    assert spec.result_var == "g_normalize_4f2a7c91"
    assert spec.bindings == (
        Binding("dataset", "g_load_a19d0c21"),
        Binding("mean", "0.0"),
        Binding("std", "1.0"),
    )


def test_ids_and_result_variables_are_valid_and_unique() -> None:
    ids = {new_instance_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(re.fullmatch(r"[0-9a-f]{8}", value) for value in ids)
    assert result_variable("123 normalize", "aabbccdd") == "g_node_123_normalize_aabbccdd"
    assert result_variable("class", "aabbccdd") == "g_class_aabbccdd"
    first, first_spec = build_instance_source("normalize", {})
    second, second_spec = build_instance_source("normalize", {})
    assert first != second
    assert first_spec.instance_id != second_spec.instance_id


def test_build_parse_round_trip_and_safe_rejection() -> None:
    source, _ = build_instance_source(
        "scale", {"x": "g_source_aabbccdd[0]", "factor": "0.5"}, instance_id="0123abcd"
    )
    parsed = parse_instance_source(source)
    assert parsed is not None
    assert parsed.bindings == (Binding("x", "g_source_aabbccdd[0]"), Binding("factor", "0.5"))

    assert parse_instance_source("g_scale_0123abcd = scale(x=1)") is None
    assert parse_instance_source(source + "\nprint('extra statement')") is None
    assert parse_instance_source(source.replace("g_scale_0123abcd", "other")) is None
    assert parse_instance_source(source.replace("scale(\n", "scale(*values,\n")) is None


def test_reconnect_changes_only_the_requested_binding() -> None:
    source, _ = build_instance_source(
        "combine", {"left": "left_source", "right": "right_source"}, instance_id="12345678"
    )
    changed = reconnect_instance_source(source, input_name="right", expression="new_right[1]")
    parsed = parse_instance_source(changed)
    assert parsed is not None
    assert parsed.bindings == (Binding("left", "left_source"), Binding("right", "new_right[1]"))

    with pytest.raises(ValueError, match="no input named"):
        reconnect_instance_source(source, input_name="missing", expression="x")
    with pytest.raises(ValueError, match="valid Python"):
        reconnect_instance_source(source, input_name="right", expression="not valid !")
    with pytest.raises(ValueError, match="canonical"):
        reconnect_instance_source("result = combine()", input_name="right", expression="x")


def test_output_expression_uses_result_or_tuple_index() -> None:
    _, instance = build_instance_source("split", {}, instance_id="f00dbabe")
    assert output_expression(instance, 0, 1) == "g_split_f00dbabe"
    assert output_expression(instance, 2, 3) == "g_split_f00dbabe[2]"
    with pytest.raises(IndexError):
        output_expression(instance, 3, 3)
