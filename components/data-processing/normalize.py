"""Small reusable numerical components for notebook examples."""


def normalize(values, mean=0.0, scale=1.0):
    """Center values and divide by a scale."""
    return (values - mean) / scale
