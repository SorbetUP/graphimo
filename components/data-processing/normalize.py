"""Small reusable numerical components for notebook examples."""

from graphimo import node as _node


@_node
def normalize(values, mean=0.0, scale=1.0):
    """Center values and divide by a scale."""
    return (values - mean) / scale


def helper(values):
    """A visible library helper that is not executable as a Graphimo node."""
    return len(values)
