import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.cell
def load_data():
    import numpy as np

    data = np.array([1.0, 2.0, 3.0, 4.0])
    data
    return (data,)


@app.cell
def normalize_data(data):
    processed = (data - data.mean()) / data.std()
    processed
    return (processed,)


@app.cell
def summarize(processed):
    summary = {"mean": float(processed.mean()), "std": float(processed.std())}
    summary
    return (summary,)


if __name__ == "__main__":
    app.run()
