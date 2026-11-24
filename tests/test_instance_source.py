import re

import pytest

from graphimo import inspect_node, node
from graphimo.instance import (
    build_instance_source,
    new_instance_id,
    output_expression,
    parse_instance_source,
    reconnect_instance_source,
    result_variable,
)


def test_canonical_instance_source_snapshot():
    source, instance = build_instance_source(
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
    assert instance.result_var == "g_normalize_4f2a7c91"


def test_generated_ids_are_distinct_and_result_variable_is_sanitized():
    first = new_instance_id()
    second = new_instance_id()
    assert re.fullmatch(r"[0-9a-f]{8}", first)
    assert re.fullmatch(r"[0-9a-f]{8}", second)
    assert first != second
    assert result_variable("train-model", "4f2a7c91") == "g_train_model_4f2a7c91"
    assert result_variable("class", "4f2a7c91") == "g_class__4f2a7c91"


def test_build_parse_round_trip_and_signature_order():
    @node
    def normalize(dataset, mean=0.0, std=1.0):
        return dataset

    spec = inspect_node(normalize)
    source, expected = build_instance_source(
        "normalize",
        {"std": "1.0", "dataset": "items", "mean": "0.0"},
        instance_id="4f2a7c91",
        node_spec=spec,
    )
    parsed = parse_instance_source(source)
    assert parsed == expected
    assert [binding.input_name for binding in parsed.bindings] == ["dataset", "mean", "std"]


def test_arbitrary_similar_python_without_markers_is_rejected():
    assert parse_instance_source("g_normalize_4f2a7c91 = normalize(value=1)") is None


def test_extra_statements_are_rejected():
    source, _ = build_instance_source("normalize", {}, instance_id="4f2a7c91")
    assert parse_instance_source(source + "\nprint('extra')") is None


def test_reconnect_changes_only_requested_keyword():
    source, _ = build_instance_source(
        "normalize",
        {"dataset": "g_load_a19d0c21", "mean": "0.0", "std": "1.0"},
        instance_id="4f2a7c91",
    )
    changed = reconnect_instance_source(source, input_name="mean", expression="config.mean")
    parsed = parse_instance_source(changed)
    assert parsed is not None
    assert [(b.input_name, b.expression) for b in parsed.bindings] == [
        ("dataset", "g_load_a19d0c21"),
        ("mean", "config.mean"),
        ("std", "1.0"),
    ]


def test_reconnect_rejects_invalid_expression_and_missing_input():
    source, _ = build_instance_source("normalize", {"mean": "0.0"}, instance_id="4f2a7c91")
    with pytest.raises(ValueError, match="valid Python"):
        reconnect_instance_source(source, input_name="mean", expression="a +")
    with pytest.raises(ValueError, match="no input"):
        reconnect_instance_source(source, input_name="std", expression="1.0")


def test_output_expression_uses_bare_var_or_tuple_index():
    _, instance = build_instance_source("normalize", {}, instance_id="4f2a7c91")
    assert output_expression(instance, 0, 1) == "g_normalize_4f2a7c91"
    assert output_expression(instance, 1, 3) == "g_normalize_4f2a7c91[1]"
    with pytest.raises(ValueError, match="outside"):
        output_expression(instance, 3, 3)


def test_output_metadata_names_are_not_replaced_by_tuple_indices():
    @node
    def calculate() -> tuple[float, int]:
        return 4.2, 3

    spec = inspect_node(calculate)
    _, instance = build_instance_source("calculate", {}, instance_id="4f2a7c91")
    assert [port.declared_type for port in spec.outputs] == ["float", "int"]
    assert output_expression(instance, 0, 2).endswith("[0]")
