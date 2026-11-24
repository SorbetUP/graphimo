from graphimo import describe_result, inspect_node, node, preview_value


def test_scalar_preview_is_json_safe():
    assert preview_value(1.5) == {"runtime_type": "float", "preview": "1.5"}


def test_large_container_preview_respects_item_limit():
    preview = preview_value([1, 2, 3, 4], max_items=2)
    assert preview["runtime_type"] == "list"
    assert preview["preview"] == "len=4 [1, 2, ...]"


def test_preview_handles_repr_raising():
    class BrokenRepr:
        def __repr__(self):
            raise RuntimeError("no repr")

    assert preview_value(BrokenRepr()) == {
        "runtime_type": "BrokenRepr",
        "preview": "<BrokenRepr repr unavailable>",
    }


def test_describe_result_checks_runtime_output_arity():
    @node
    def pair(value):
        return value, value

    spec = inspect_node(pair)
    try:
        describe_result((1,), spec)
    except ValueError as exc:
        assert str(exc) == "node_output_arity_mismatch"
    else:
        raise AssertionError("expected output arity mismatch")


def test_preview_bounds_string_and_container_samples():
    assert preview_value("abcdefgh", max_chars=6)["preview"] == "abc..."
    assert preview_value([1, 2, 3], max_items=1)["preview"] == "len=3 [1, ...]"
