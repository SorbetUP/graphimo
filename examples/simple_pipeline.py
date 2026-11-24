import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.cell
def load_data():
    import numpy as np

    data = np.array([1.0, 2.0, 3.0, 4.0])
    return (data,)


@app.cell
def define_scale():
    from graphimo import node

    @node
    def scale(x, factor=1.0):
        y = x * factor
        return y

    return (scale,)


@app.cell
def scale_by_two(data, scale):
    # Graphimo node: scale
    # Graphimo instance: 2a000001
    g_scale_2a000001 = scale(
        x=data,
        factor=2.0,
    )
    return (g_scale_2a000001,)


@app.cell
def scale_by_three(data, scale):
    # Graphimo node: scale
    # Graphimo instance: 3a000001
    g_scale_3a000001 = scale(
        x=data,
        factor=3.0,
    )
    return (g_scale_3a000001,)


@app.cell
def summarize(g_scale_2a000001, g_scale_3a000001):
    summary = {
        "scale_2_mean": float(g_scale_2a000001.mean()),
        "scale_3_mean": float(g_scale_3a000001.mean()),
    }
    return (summary,)


if __name__ == "__main__":
    app.run()
