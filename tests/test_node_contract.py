from __future__ import annotations

import inspect

from graphimo import describe_result, inspect_node, is_node, node, preview_value


@node
def _single_output(x: int) -> float:
    c = x / 2
    return c


@node
def _tuple_output(a: float, b: int, label: str = "ok") -> tuple[float, int, str]:
    return a, b, label


@node
def _product_output(prix: float, nbr: int) -> float:
    return prix * nbr


def _calculate(value: int) -> int:
    return value + 1


@node
def _call_output(x: int) -> int:
    return _calculate(x)


@node
def _dict_output(a: int, b: int):
    return {"a": a, "b": b}


@node
def _conditional_output(flag: bool) -> float:
    if flag:
        return c  # noqa: F821 - expression metadata is inspected without execution
    return d  # noqa: F821 - expression metadata is inspected without execution


@node
def _inconsistent_output(flag: bool):
    if flag:
        return 1
    return 1, 2


@node
def _tuple_annotation_mismatch() -> tuple[int, str]:
    return 1, 2, 3


@node
def _nested_returns_are_ignored() -> int:
    def inner():
        return "ignored"

    return 1


def test_node_marker_preserves_function_identity_and_signature() -> None:
    def f(value: int = 3) -> int:
        """description"""
        return value

    signature_before = inspect.signature(f)
    annotations_before = dict(f.__annotations__)
    decorated = node(f)
    assert decorated is f
    assert is_node(f)
    assert f(4) == 4
    assert f.__name__ == "f"
    assert f.__doc__ == "description"
    assert f.__annotations__ == annotations_before
    assert inspect.signature(f) == signature_before


def test_inspect_node_extracts_single_and_tuple_outputs() -> None:
    single = inspect_node(_single_output)
    assert single.name == "_single_output"
    assert single.inputs[0].annotation == "int"
    assert single.outputs[0].declared_type == "float"
    assert single.outputs[0].expressions == ("c",)

    multiple = inspect_node(_tuple_output)
    assert [port.declared_type for port in multiple.outputs] == ["float", "int", "str"]
    assert [port.expressions for port in multiple.outputs] == [("a",), ("b",), ("label",)]
    assert multiple.inputs[-1].default == "ok"


def test_inspect_node_supports_expressions_dicts_and_return_paths() -> None:
    assert inspect_node(_product_output).outputs[0].expressions == ("prix * nbr",)
    assert inspect_node(_call_output).outputs[0].expressions == ("_calculate(x)",)
    # The single output expression is preserved verbatim for source-level wiring.
    assert inspect_node(_dict_output).outputs[0].expressions == ('{"a": a, "b": b}',)
    conditional = inspect_node(_conditional_output)
    assert conditional.diagnostic is None
    assert conditional.outputs[0].expressions == ("c", "d")


def test_inspect_node_reports_static_diagnostics() -> None:
    assert inspect_node(_inconsistent_output).diagnostic == "node_output_arity_inconsistent"
    assert (
        inspect_node(_tuple_annotation_mismatch).diagnostic
        == "node_return_annotation_arity_mismatch"
    )
    assert inspect_node(_nested_returns_are_ignored).outputs[0].expressions == ("1",)


def test_preview_value_is_bounded_and_survives_bad_repr() -> None:
    assert preview_value(4.2) == {"runtime_type": "float", "preview": "4.2"}
    many = preview_value(list(range(100)), max_chars=50, max_items=3)
    assert many["runtime_type"] == "list"
    assert len(many["preview"]) <= 50
    assert "len=100" in many["preview"]

    class BadRepr:
        def __repr__(self) -> str:
            raise RuntimeError("cannot represent")

    assert "repr unavailable" in preview_value(BadRepr())["preview"]


def test_describe_result_adds_declared_and_expression_metadata() -> None:
    spec = inspect_node(_single_output)
    assert describe_result(4.2, spec) == [
        {
            "index": 0,
            "declared_type": "float",
            "runtime_type": "float",
            "preview": "4.2",
            "expressions": ["c"],
            "active_expression": None,
        }
    ]
    tuple_spec = inspect_node(_tuple_output)
    assert len(describe_result((1.2, 3, "ok"), tuple_spec)) == 3
    try:
        describe_result((1,), tuple_spec)
    except ValueError as error:
        assert "node_output_arity_mismatch" in str(error)
    else:
        raise AssertionError("expected tuple output arity diagnostic")
