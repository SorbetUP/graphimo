from graphimo import inspect_node, is_node, node


def calc(value):
    return value


def test_node_marks_callable_without_wrapping_it():
    @node
    def scale(value: float, factor: float = 2.0) -> float:
        """Scale a value."""
        return value * factor

    assert is_node(scale)
    assert scale(3.0) == 6.0
    spec = inspect_node(scale)
    assert spec.name == "scale"
    assert spec.inputs[0].name == "value"
    assert spec.inputs[0].required is True
    assert spec.inputs[0].annotation == "float"
    assert spec.inputs[1].required is False
    assert spec.inputs[1].default == 2.0
    assert spec.outputs[0].declared_type == "float"
    assert spec.outputs[0].expressions == ("value * factor",)
    assert scale.__doc__ == "Scale a value."
    assert scale.__annotations__ == {"value": float, "factor": float, "return": float}


def test_inspection_does_not_execute_source():
    executions = []

    @node
    def tracked(value):
        executions.append(value)
        return value

    spec = inspect_node(tracked)
    assert spec.outputs[0].expressions == ("value",)
    assert executions == []


def test_call_expression_is_preserved():
    @node
    def calculate(x):
        return calc(x)

    assert inspect_node(calculate).outputs[0].expressions == ("calc(x)",)


def test_none_return_expression_is_an_output():
    @node
    def no_value():
        return None

    spec = inspect_node(no_value)
    assert len(spec.outputs) == 1
    assert spec.outputs[0].expressions == ("None",)
