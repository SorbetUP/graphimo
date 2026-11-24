# Control Blocks

Control Blocks are normal Graphimo elements backed by native marimo UI values.
The application/controller generates an ordinary owned marimo cell; marimo
owns its UI state, reactive dependencies and execution. The renderer only
draws the card input and submits `set_control_value` intents.

## Available controls

The root Python palette provides Slider, Range Slider, Number, Select,
Checkbox, Switch, Text and Button. Clicking a control opens its JSON settings
with typed defaults. The resulting code remains readable, for example:

```python
# Graphimo control: slider
# Graphimo instance: a81f2c40
g_slider_a81f2c40 = mo.ui.slider(
    start=0.0,
    stop=1.0,
    step=0.01,
    value=0.2,
    debounce=True,
    label='threshold',
)
```

If the notebook does not already bind `mo`, the application adds a small
Graphimo-owned `import marimo as mo` cell. That helper is omitted from the
Workbench canvas. A connection edits only a target node's generated call and
uses the control's `.value` global expression. The canvas labels the output
`value` and does not expose the `.value` implementation detail.

## Reactive behavior

- Slider and range controls default to **On release**; set `debounce` to
  `false` for **Live** updates. Live input events are coalesced to at most one
  application update per 100 ms while dragging.
- Number uses the same visible policy and defaults to **On release**.
- Text stages edits by default and commits on change/blur; set `debounce` to
  `false` for live typing updates.
- Select, checkbox and switch update immediately.
- Button uses marimo's native `on_click` counter (`value + 1`) as an event
  signal. Core accepts only this generated callback shape for owned button
  cells.

The Workbench posts updates through marimo's
`/api/kernel/set_ui_element_value` endpoint with the UI element's runtime ID;
marimo triggers dependent cells. The local port obtains marimo's skew
protection token from the rendered notebook page and sends it only on that
native endpoint. It does not use a second scheduler.

The application validates finite numbers, configured bounds, bool/text types,
range ordering and select values (including exact scalar types) before sending
an update. Invalid input leaves notebook source and UI state unchanged.

Tests cover every native control constructor, the source marker/parser,
validation, controller wiring and a live marimo slider driving a downstream
`@node`. The live test also instantiates each remaining control primitive.
