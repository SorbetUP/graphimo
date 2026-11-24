import pytest

from graphimo.instance import (
    build_instance_source,
    disable_instance_source,
    enable_instance_source,
    parse_disabled_instance_source,
    parse_instance_source,
)


def test_disabled_instance_has_exact_comment_wrapper_and_round_trips():
    source, instance = build_instance_source(
        "normalize",
        {"dataset": "g_load_a19d0c21", "mean": "0.0"},
        instance_id="4f2a7c91",
    )
    disabled = disable_instance_source(source)
    assert disabled == (
        "# graphimo:disabled-begin\n"
        "# # Graphimo node: normalize\n"
        "# # Graphimo instance: 4f2a7c91\n"
        "# g_normalize_4f2a7c91 = normalize(\n"
        "#     dataset=g_load_a19d0c21,\n"
        "#     mean=0.0,\n"
        "# )\n"
        "# graphimo:disabled-end"
    )
    assert enable_instance_source(disabled) == source
    assert parse_disabled_instance_source(disabled) == instance
    assert parse_instance_source(disabled) is None


def test_only_owned_instances_can_be_disabled():
    with pytest.raises(ValueError, match="Graphimo-owned"):
        disable_instance_source("value = user_function()")


@pytest.mark.parametrize(
    "source",
    [
        "# graphimo:disabled-begin\n# # Graphimo node: f\n# # Graphimo instance: 4f2a7c91\n# graphimo:disabled-end\n# graphimo:disabled-end",
        "# graphimo:disabled-begin\n# graphimo:disabled-begin\n# graphimo:disabled-end",
        "# graphimo:disabled-begin\n# # Graphimo node: f\nnot commented\n# graphimo:disabled-end",
    ],
)
def test_nested_duplicate_or_malformed_disabled_markers_are_rejected(source):
    with pytest.raises(ValueError):
        enable_instance_source(source)
