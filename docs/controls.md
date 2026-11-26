# Graphimo Control Blocks

Controls are native marimo UI elements in the normal Python scope. They do not
run through a Graphimo scheduler: the widget cell and its value cell are
ordinary marimo cells, so marimo owns reactive updates and dependent execution.

The application exposes `control.catalog` and `control.create`. Creation
validates the kind, identity, label and bounded parameters, then returns two
readable Python cell sources:

1. a widget cell such as `mo.ui.slider(...)`;
2. a dependent value cell such as `g_slider_threshold_value = g_slider_threshold.value`.

The Zeron adapter inserts both sources through the existing marimo bridge. The
second cell is the output to connect to an ordinary `@node` input. The UI uses
the same application operation exposed to service clients and agent tools.

Supported V1 controls are `slider`, `range_slider`, `number`, `dropdown`,
`checkbox`, `text` and `run_button`. Parameters are JSON values and are checked
before source generation: numeric values must be finite, ranges must be
ordered and bounded, dropdown choices are limited to 128 scalar values, and
labels and text values have explicit length limits. Python literals are
generated with `repr`; source is not assembled from shell commands.

Example:

```python
# Graphimo control: slider
# Graphimo instance: threshold_1
import marimo as g_marimo_threshold_1
g_threshold_1 = g_marimo_threshold_1.ui.slider(
    start=0.0, stop=1.0, step=0.01, value=0.2, label="threshold", show_value=True
)
g_threshold_1
```

```python
# Graphimo control value: threshold_1
g_threshold_1_value = g_threshold_1.value
g_threshold_1_value
```

Each generated module alias is unique, so adding multiple controls does not
introduce duplicate `mo` definitions across marimo cells. Values remain
normal Python values that downstream cells can consume.
