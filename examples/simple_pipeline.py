import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.cell
def load_data():
    import numpy as np

    data = np.array([1.0, 2.0, 3.0, 4.0])
    data  # noqa: B018 - marimo expression output
    return (data,)


@app.cell
def normalize_data(data):
    processed = (data - data.mean()) / data.std()
    processed  # noqa: B018 - marimo expression output
    return (processed,)


@app.cell
def summarize(processed):
    summary = {"mean": float(processed.mean()), "std": float(processed.std())}
    summary  # noqa: B018 - marimo expression output
    return (summary,)


@app.cell
def _():
    from graphimo import node

    @node
    def scale(x, factor=1.0):
        return x * factor

    return (scale,)


@app.cell
def _(scale):
    # Graphimo node: scale
    # Graphimo instance: 4f2a7c91
    g_scale_4f2a7c91 = scale(x=2.0, factor=2.0)
    return (g_scale_4f2a7c91,)


@app.cell
def _(scale):
    # Graphimo node: scale
    # Graphimo instance: 93b7e610
    g_scale_93b7e610 = scale(x=2.0, factor=3.0)
    return (g_scale_93b7e610,)


if __name__ == "__main__":
    app.run()
