import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.cell
def make_data():
    labels = [0.0, 1.0, 0.0, 1.0]
    features = [0.1, 0.8, 0.2, 0.9]
    (features, labels)
    return features, labels


@app.cell
def model(features):
    prediction = [min(1.0, max(0.0, value)) for value in features]
    prediction
    return (prediction,)


@app.cell
def criterion(prediction, labels):
    loss = sum((actual - expected) ** 2 for actual, expected in zip(prediction, labels)) / len(labels)
    loss
    return (loss,)


if __name__ == "__main__":
    app.run()
