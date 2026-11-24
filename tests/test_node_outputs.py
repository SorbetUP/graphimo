from graphimo import inspect_node, node

c = 1.0
d = 2.0


def test_tuple_return_annotation_defines_output_types():
    @node
    def calculate(prix: float, nbr: int) -> tuple[float, int, str]:
        total = prix * nbr
        return total, nbr, str(total)

    spec = inspect_node(calculate)
    assert [port.declared_type for port in spec.outputs] == ["float", "int", "str"]
    assert [port.expressions for port in spec.outputs] == [("total",), ("nbr",), ("str(total)",)]


def test_dict_return_is_one_output():
    @node
    def calculate(x: int) -> dict:
        return {"value": x}

    spec = inspect_node(calculate)
    assert len(spec.outputs) == 1
    assert spec.outputs[0].expressions == ('{"value": x}',)


def test_alternative_return_paths_are_stable_and_source_ordered():
    @node
    def choose(flag: bool) -> tuple[int, int]:
        if flag:
            return 1, 2
        return 3, 4

    spec = inspect_node(choose)
    assert [port.expressions for port in spec.outputs] == [("1", "3"), ("2", "4")]


def test_single_output_alternative_paths_keep_source_order():
    @node
    def choose(flag: bool) -> float:
        if flag:
            return c
        return d

    assert inspect_node(choose).outputs[0].expressions == ("c", "d")


def test_nested_functions_do_not_contribute_return_paths():
    @node
    def outer(value):
        def helper():
            return 1, 2, 3

        return value

    assert inspect_node(outer).outputs[0].expressions == ("value",)


def test_inconsistent_return_arity_is_diagnosed():
    @node
    def inconsistent(flag):
        if flag:
            return 1, 2
        return 3

    spec = inspect_node(inconsistent)
    assert spec.diagnostic == "node_output_arity_inconsistent"
    assert spec.outputs == ()


def test_tuple_annotation_arity_mismatch_is_diagnosed():
    @node
    def inconsistent() -> tuple[int, str]:
        return 1, 2, 3

    spec = inspect_node(inconsistent)
    assert spec.diagnostic == "node_return_annotation_arity_mismatch"
    assert len(spec.outputs) == 3
    assert all(port.declared_type is None for port in spec.outputs)
