-- Ready-made block library: validated component files plus insertable pipelines.
-- A "block" is a component file written under components/ via component.write.
-- A "pipeline" is an ordered list of @node instances that the plugin inserts,
-- wires with notebook.connect (output_index for multi-output nodes) and runs.
local Presets = {}

local LFW_SKLEARN = [[
from graphimo import node


@node
def load_lfw(min_faces_per_person: int = 70, resize: float = 0.4):
    """Download LFW faces and return a stratified train/test split."""
    from sklearn.datasets import fetch_lfw_people
    from sklearn.model_selection import train_test_split

    lfw = fetch_lfw_people(min_faces_per_person=min_faces_per_person, resize=resize)
    X_train, X_test, y_train, y_test = train_test_split(
        lfw.data, lfw.target, test_size=0.25, random_state=42, stratify=lfw.target
    )
    return X_train, X_test, y_train, y_test


@node
def reduce_pca(X_train, X_test, n_components: int = 100):
    """Fit PCA on the training split and project both splits."""
    from sklearn.decomposition import PCA

    pca = PCA(n_components=n_components, whiten=True, random_state=42).fit(X_train)
    return pca.transform(X_train), pca.transform(X_test)


@node
def train_classifier(X_train_pca, y_train, max_iter: int = 2000):
    """Train a multinomial logistic regression on PCA features."""
    from sklearn.linear_model import LogisticRegression

    return LogisticRegression(max_iter=max_iter).fit(X_train_pca, y_train)


@node
def evaluate(clf, X_test_pca, y_test):
    """Return accuracy and the raw predictions for inspection."""
    from sklearn.metrics import accuracy_score

    y_pred = clf.predict(X_test_pca)
    return accuracy_score(y_test, y_pred), y_pred
]]

local DIGITS_TORCH = [[
from graphimo import node


@node
def load_digits():
    """Bundled sklearn digits as standardized torch tensors (no download)."""
    import torch
    from sklearn.datasets import load_digits

    digits = load_digits()
    X = torch.tensor(digits.data, dtype=torch.float32)
    y = torch.tensor(digits.target, dtype=torch.long)
    X = (X - X.mean(dim=0)) / (X.std(dim=0) + 1e-8)
    return X, y


@node
def train_mlp(X, y, hidden: int = 64, epochs: int = 40, lr: float = 0.01):
    """Train a small MLP classifier; return the model and the final loss."""
    import torch

    model = torch.nn.Sequential(
        torch.nn.Linear(X.shape[1], hidden),
        torch.nn.ReLU(),
        torch.nn.Linear(hidden, int(y.max()) + 1),
    )
    loss_fn = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    loss = None
    for _ in range(epochs):
        optimizer.zero_grad()
        loss = loss_fn(model(X), y)
        loss.backward()
        optimizer.step()
    return model, float(loss)


@node
def evaluate_mlp(model, X, y):
    """Return accuracy of the model on the given tensors."""
    import torch

    with torch.no_grad():
        predictions = model(X).argmax(dim=1)
    return float((predictions == y).float().mean())
]]

local TEXT_STATS = [[
from collections import Counter

from graphimo import node


@node
def tokenize(text: str):
    """Lowercase whitespace tokens plus total/unique counts."""
    tokens = text.lower().split()
    return tokens, len(tokens), len(set(tokens))


@node
def top_tokens(tokens, k: int = 5):
    """Return the k most frequent tokens as (token, count) pairs."""
    return Counter(tokens).most_common(k)


@node
def summary(text: str):
    """One-call text summary: counts and top tokens."""
    tokens = text.lower().split()
    return len(tokens), len(set(tokens)), Counter(tokens).most_common(5)
]]

Presets.blocks = {
	{
		path = "ml/lfw_sklearn.py",
		name = "LFW · sklearn",
		description = "load_lfw → reduce_pca → train_classifier → evaluate",
		source = LFW_SKLEARN,
	},
	{
		path = "ml/digits_torch.py",
		name = "Digits · torch MLP",
		description = "load_digits → train_mlp → evaluate_mlp",
		source = DIGITS_TORCH,
	},
	{
		path = "ml/text_stats.py",
		name = "Text stats",
		description = "tokenize → top_tokens → summary (pure Python)",
		source = TEXT_STATS,
	},
}

-- connections: { from = <1-based step index>, output_index = <int>, input = "<node input>" }
Presets.pipelines = {
	{
		id = "lfw_classifier",
		name = "LFW face recognition (sklearn)",
		description = "PCA + logistic regression on LFW; last block outputs accuracy",
		block = "ml/lfw_sklearn.py",
		source = LFW_SKLEARN,
		steps = {
			{ callable = "load_lfw", connections = {} },
			{ callable = "reduce_pca", connections = {
				{ from = 1, output_index = 0, input = "X_train" },
				{ from = 1, output_index = 1, input = "X_test" },
			} },
			{ callable = "train_classifier", connections = {
				{ from = 2, output_index = 0, input = "X_train_pca" },
				{ from = 1, output_index = 2, input = "y_train" },
			} },
			{ callable = "evaluate", connections = {
				{ from = 3, output_index = 0, input = "clf" },
				{ from = 2, output_index = 1, input = "X_test_pca" },
				{ from = 1, output_index = 3, input = "y_test" },
			} },
		},
	},
	{
		id = "digits_mlp",
		name = "Digits MLP (torch, offline)",
		description = "Small MLP on the bundled digits dataset; no download needed",
		block = "ml/digits_torch.py",
		source = DIGITS_TORCH,
		steps = {
			{ callable = "load_digits", connections = {} },
			{ callable = "train_mlp", connections = {
				{ from = 1, output_index = 0, input = "X" },
				{ from = 1, output_index = 1, input = "y" },
			} },
			{ callable = "evaluate_mlp", connections = {
				{ from = 2, output_index = 0, input = "model" },
				{ from = 1, output_index = 0, input = "X" },
				{ from = 1, output_index = 1, input = "y" },
			} },
		},
	},
}

function Presets.block_by_path(path)
	for _, block in Presets.blocks do
		if block.path == path then return block end
	end
	return nil
end

return Presets
