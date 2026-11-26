const $ = (selector) => document.querySelector(selector);
const modelList = $("#metablock-list");
const palette = $("#palette");
const canvas = $("#canvas");
const ctx = canvas.getContext("2d");

let projectInfo = null;
let metablockId = null;
let view = null;
let scope = "root";
let layout = {};
let selected = new Set();
let selectedEdge = null;
let connectSource = null;
let camera = { x: 0, y: 0, zoom: 1 };
let cameraTouched = false;
let pointer = null;
let drag = null;
let currentMetaBlocks = [];
let activeRuntimeId = null;
let runtimeEventCursor = "0";
let runtimeEvents = [];
let activeSubscriptions = new Map();
let runtimeRepetitionIndex = 0;
let checkpointDetails = null;

async function api(action, args = {}) {
  const response = await fetch("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, args }),
  });
  const value = await response.json();
  if (!response.ok || !value.ok) throw new Error(value.error?.message || `HTTP ${response.status}`);
  return value.result;
}

async function refreshProject() {
  const response = await fetch("/api/project", { cache: "no-store" });
  if (!response.ok) throw new Error(`Could not read project (${response.status})`);
  projectInfo = await response.json();
  $("#project-label").textContent = projectInfo.project_name;
  currentMetaBlocks = projectInfo.metablocks.items || [];
  renderMetaBlocks();
}

function renderMetaBlocks() {
  modelList.replaceChildren();
  if (!currentMetaBlocks.length) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "No saved MetaBlocks in this project.";
    modelList.append(note);
  }
  for (const item of currentMetaBlocks) {
    const button = document.createElement("button");
    button.className = `list-item${metablockId === item.metablock_id ? " active" : ""}`;
    button.textContent = `${item.dialect_id} · ${item.metablock_id}`;
    button.addEventListener("click", () => openMetaBlock(item.metablock_id));
    modelList.append(button);
  }
}

async function createModel() {
  const id = crypto.randomUUID().replaceAll("-", "");
  const name = `PyTorch Model ${currentMetaBlocks.length + 1}`;
  const created = await api("metablock.create", {
    dialect_id: "pytorch.model",
    metablock_id: id,
    request: { name },
  });
  await refreshProject();
  await openMetaBlock(id, created.projection);
}

async function openMetaBlock(id, initialProjection = null) {
  if (metablockId && metablockId !== id) await stopAllRuntimeSubscriptions();
  if (metablockId !== id) {
    activeRuntimeId = null;
    runtimeEventCursor = "0";
    runtimeEvents = [];
    checkpointDetails = null;
  }
  metablockId = id;
  scope = "root";
  selected.clear();
  selectedEdge = null;
  const result = initialProjection
    ? { projection: initialProjection, available: true }
    : await api("metablock.project", { metablock_id: id, scope });
  if (!result.available) {
    view = null;
    renderUnavailable(result);
    return;
  }
  await showProjection(result.projection);
  await refreshRuntimeSelector();
  renderInspector();
  renderMetaBlocks();
}

async function showProjection(next) {
  view = next;
  const layoutScope = getLayoutScope();
  const saved = await api("layout.get", { scope_id: layoutScope });
  layout = saved.layout || {};
  const needsInitialLayout = Object.keys(layout).length === 0;
  if (needsInitialLayout) seedDefaultLayout(view);
  for (const [index, item] of view.elements.entries()) {
    if (!layout[item.id]) {
      layout[item.id] = { x: 35 + index * 245, y: 45, width: 210, height: 120 };
    }
  }
  if (view.generated_source) $("#source code").textContent = view.generated_source;
  else $("#source code").textContent = "Generated source is unavailable while this graph has errors.";
  $("#generate-cell").disabled = !metablockId || !view.generated_source;
  if (needsInitialLayout) fitCameraToLayout();
  renderAll();
}

function seedDefaultLayout(projection) {
  const elements = projection.elements || [];
  const ids = elements.map((item) => item.id);
  const rank = Object.fromEntries(ids.map((id) => [id, 0]));
  const edges = projection.edges || [];
  for (let pass = 0; pass < ids.length; pass++) {
    let changed = false;
    for (const edge of edges) {
      if (!(edge.source.element_id in rank) || !(edge.target.element_id in rank)) continue;
      const next = rank[edge.source.element_id] + 1;
      if (next > rank[edge.target.element_id]) { rank[edge.target.element_id] = next; changed = true; }
    }
    if (!changed) break;
    if (pass === ids.length - 1) return;
  }
  const columns = new Map();
  for (const element of elements) {
    const column = rank[element.id];
    const row = columns.get(column) || 0;
    columns.set(column, row + 1);
    const visibleColumn = Math.min(column, 2);
    const overflowRow = Math.max(0, column - 2);
    layout[element.id] = {
      x: 35 + visibleColumn * 245,
      y: 45 + overflowRow * 165 + row * 155,
      width: 210,
      height: 120,
    };
  }
}

function fitCameraToLayout() {
  const bounds = canvas.getBoundingClientRect();
  const rects = Object.values(layout);
  if (!rects.length || !bounds.width || !bounds.height) return;
  const left = Math.min(...rects.map((item) => item.x));
  const top = Math.min(...rects.map((item) => item.y));
  const right = Math.max(...rects.map((item) => item.x + item.width));
  const bottom = Math.max(...rects.map((item) => item.y + item.height));
  const zoom = Math.min(1, (bounds.width - 48) / (right - left), (bounds.height - 48) / (bottom - top));
  camera.zoom = Math.max(0.35, zoom);
  camera.x = (bounds.width - (right - left) * camera.zoom) / 2 - left * camera.zoom;
  camera.y = (bounds.height - (bottom - top) * camera.zoom) / 2 - top * camera.zoom;
}

function getLayoutScope() {
  const safe = scope.replace(/[^A-Za-z0-9_-]/g, "_");
  return `${metablockId}_${safe}`.slice(0, 128);
}

async function reloadProjection() {
  if (!metablockId) return;
  const result = await api("metablock.project", { metablock_id: metablockId, scope });
  if (result.available) await showProjection(result.projection);
  else renderUnavailable(result);
  await refreshProject();
}

function renderAll() {
  renderPalette();
  renderDiagnostics();
  renderBreadcrumbs();
  renderInspector();
  resizeCanvas();
}

function renderUnavailable(result) {
  view = null;
  $("#empty-state").classList.remove("hidden");
  $("#empty-state").textContent = `Addon unavailable: ${result.dialect_id}`;
  $("#source code").textContent = JSON.stringify(result.state ?? result.diagnostics, null, 2);
  palette.replaceChildren();
  draw();
}

function renderPalette() {
  palette.replaceChildren();
  const items = view?.palette || [];
  $("#palette-count").textContent = items.length ? `${items.length} items` : "contextual";
  if (!metablockId || !view) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "Open a PyTorch MetaBlock to see its internal components.";
    palette.append(note);
    return;
  }
  for (const item of items) {
    const button = document.createElement("button");
    button.className = "palette-item";
    button.innerHTML = `<span>${escapeHtml(item.kind)}</span><small>${escapeHtml(item.category)}</small>`;
    button.addEventListener("click", () => sendIntent({ type: "add_element", scope, kind: item.kind }));
    palette.append(button);
  }
  for (const group of view.group_templates || []) {
    const button = document.createElement("button");
    button.className = "palette-item";
    button.innerHTML = `<span>${escapeHtml(group.name)}</span><small>Group template</small>`;
    button.addEventListener("click", () => sendIntent({ type: "add_group_instance", scope, group_id: group.id }));
    palette.append(button);
  }
}

function renderDiagnostics() {
  const root = $("#diagnostic-list");
  root.replaceChildren();
  const diagnostics = view?.diagnostics || [];
  $("#diagnostic-count").textContent = diagnostics.length ? `${diagnostics.length}` : "none";
  for (const diagnostic of diagnostics) {
    const row = document.createElement("div");
    row.className = `diagnostic${diagnostic.severity === "warning" ? " warning" : ""}`;
    row.textContent = `${diagnostic.code}: ${diagnostic.message}`;
    if (diagnostic.element_id) row.addEventListener("click", () => { selected = new Set([diagnostic.element_id]); renderAll(); });
    root.append(row);
  }
}

function renderBreadcrumbs() {
  const root = $("#breadcrumbs");
  root.replaceChildren();
  if (!metablockId || !view) return;
  const home = document.createElement("button");
  home.className = "quiet";
  home.textContent = view.name;
  home.addEventListener("click", () => openMetaBlock(metablockId));
  root.append(home);
  if (scope.startsWith("group:")) {
    const group = (view.groups || []).find((item) => item.id === scope.slice(6));
    const crumb = document.createElement("span");
    crumb.textContent = `› ${group?.name || scope.slice(6)}`;
    root.append(crumb);
  } else {
    for (const method of view.method_tabs || []) {
      const tab = document.createElement("button");
      tab.className = "quiet";
      tab.textContent = method.name;
      tab.disabled = method.name === view.active_method;
      tab.addEventListener("click", () => enterScope(method.name));
      root.append(tab);
    }
  }
  const fit = document.createElement("button");
  fit.className = "quiet fit-button";
  fit.textContent = "Fit";
  fit.title = "Fit all elements in the canvas";
  fit.addEventListener("click", () => { cameraTouched = false; fitCameraToLayout(); draw(); });
  root.append(fit);
}

function renderInspector() {
  const root = $("#inspector");
  root.replaceChildren();
  const selectedIds = [...selected];
  const element = selectedIds.length === 1 ? view?.elements.find((item) => item.id === selectedIds[0]) : null;
  $("#delete-selection").disabled = !selectedIds.length && !selectedEdge;
  $("#delete-selection").textContent = selectedEdge ? "Disconnect" : "Delete";
  if (!view || !metablockId) {
    root.innerHTML = '<p class="muted">Select a MetaBlock to inspect its model.</p>';
    return;
  }
  if (selectedEdge) {
    const edge = view.edges.find((item) => item.id === selectedEdge);
    root.append(section("Connection"));
    root.append(textRow("From", edge ? `${edge.source.element_id}.${edge.source.port}` : "unknown"));
    root.append(textRow("To", edge ? `${edge.target.element_id}.${edge.target.port}` : "unknown"));
    root.append(textRow("Shape", edge?.shape ? formatShape(edge.shape) : "unresolved"));
    return;
  }
  if (!element) {
    root.append(field("MetaBlock name", view.name, async (value) => {
      await sendIntent({ type: "set_model_parameter", name: "name", value });
    }, "text"));
    for (const parameter of view.external_parameters || []) {
      root.append(field(parameter.name, parameter.default, async (value) => {
        await sendIntent({ type: "set_model_parameter", name: parameter.id, value: parseValue(parameter.type, value) });
      }, parameter.type));
    }
    root.append(textRow("Ports", `${view.outer.inputs.length} inputs · ${view.outer.outputs.length} outputs`));
    root.append(textRow("Scope", scope));
    renderRuntimeOverview(root);
    if (connectSource) root.append(connectionHint());
    return;
  }
  root.append(field("Name", element.name, async (value) => sendIntent({ type: "rename_element", scope, element_id: element.id, name: value }), "text"));
  root.append(textRow("Kind", element.kind));
  for (const direction of ["inputs", "outputs"]) {
    root.append(section(direction));
    for (const port of element[direction] || []) {
      const portName = typeof port === "string" ? port : port.name;
      const shape = direction === "outputs" && element.output_shape ? formatShape(element.output_shape) : "";
      root.append(portRow(portName, shape));
    }
  }
  for (const parameter of element.parameters || []) {
    if (element.kind === "group_instance" && parameter.name === "repeat") {
      root.append(field(parameter.name, parameter.value, async (value) => sendIntent({ type: "set_group_repeat", scope, instance_id: element.id, repeat: Number(value) }), "number", { min: parameter.minimum, step: 1 }));
    } else {
      root.append(field(parameter.name, parameter.value, async (value) => sendIntent({ type: "set_element_parameter", scope, element_id: element.id, parameter: parameter.name, value: parseValue(parameter.type, value) }), inputType(parameter.type), { min: parameter.minimum, max: parameter.maximum, step: parameter.type === "int" ? 1 : "any", choices: parameter.choices }));
    }
  }
  if (element.kind === "group_instance") {
    const open = document.createElement("button");
    open.textContent = "Open Group";
    open.addEventListener("click", () => enterScope(`group:${element.group_id}`));
    root.append(open);
  }
  root.append(section("Bindings"));
  for (const name of Object.keys(element.params || {})) {
    if (element.parameters?.some((item) => item.name === name)) continue;
    root.append(textRow(name, JSON.stringify(element.params[name])));
  }
  renderRuntimeInspector(root, element);
}

function runtimeItems() {
  return view?.runtime_selector?.items || [];
}

function selectedRuntime() {
  return runtimeItems().find((item) => item.runtime_instance_id === activeRuntimeId) || null;
}

function renderRuntimeOverview(root) {
  root.append(section("Live runtime"));
  const items = runtimeItems();
  if (!items.length) {
    root.append(textRow("Status", view?.runtime_selector?.reason || "No active runtime."));
    return;
  }
  if (items.length > 1) root.append(runtimeSelector(items));
  const runtime = selectedRuntime() || items[0];
  root.append(textRow("Runtime", runtime.runtime_instance_id));
  root.append(textRow("Device", runtime.device || "unknown"));
  root.append(textRow("Revision", runtime.graph_revision));
  const metrics = runtimeEvents.filter((item) => item.runtime_instance_id === runtime.runtime_instance_id && item.kind === "metric.scalar");
  const latestMetric = metrics.at(-1);
  if (latestMetric) root.append(textRow(`Metric ${latestMetric.payload.name}`, latestMetric.payload.value));
  renderCheckpointInspector(root);
}

function runtimeSelector(items) {
  const wrapper = document.createElement("div");
  wrapper.className = "field";
  const label = document.createElement("label");
  label.textContent = "Runtime instance";
  const select = document.createElement("select");
  for (const item of items) {
    const option = document.createElement("option");
    option.value = item.runtime_instance_id;
    option.textContent = `${item.runtime_instance_id.slice(0, 12)} · ${item.device || "unknown"}`;
    option.selected = item.runtime_instance_id === activeRuntimeId;
    select.append(option);
  }
  select.addEventListener("change", () => {
    activeRuntimeId = select.value;
    runtimeRepetitionIndex = 0;
    renderInspector();
  });
  wrapper.append(label, select);
  return wrapper;
}

function renderRuntimeInspector(root, element) {
  root.append(section("Live runtime"));
  const items = runtimeItems();
  if (!items.length) {
    root.append(textRow("Status", view?.runtime_selector?.reason || "No active runtime."));
    return;
  }
  if (items.length > 1) root.append(runtimeSelector(items));
  const runtime = selectedRuntime() || items[0];
  const bindings = (runtime.bindings || []).filter((item) => item.logical_element_id === element.id && item.inspectable !== false);
  if (!bindings.length) {
    root.append(textRow("Status", "This visual element has no inspectable runtime module."));
    return;
  }
  const repetitionValues = [...new Set(bindings.map((item) => item.group_instance_path?.at(-1)?.repetition_index).filter((item) => Number.isInteger(item)))].sort((a, b) => a - b);
  if (repetitionValues.length > 1) {
    const wrapper = document.createElement("div");
    wrapper.className = "field";
    const label = document.createElement("label"); label.textContent = "Repetition";
    const select = document.createElement("select");
    for (const value of repetitionValues) {
      const option = document.createElement("option"); option.value = String(value); option.textContent = String(value); option.selected = value === runtimeRepetitionIndex; select.append(option);
    }
    select.addEventListener("change", () => { runtimeRepetitionIndex = Number(select.value); renderInspector(); });
    wrapper.append(label, select); root.append(wrapper);
  }
  const binding = repetitionValues.length > 1
    ? bindings.find((item) => item.group_instance_path?.at(-1)?.repetition_index === runtimeRepetitionIndex) || bindings[0]
    : bindings[0];
  root.append(textRow("Runtime", runtime.runtime_instance_id));
  root.append(textRow("Module path", binding.module_path));
  root.append(textRow("Parameters", binding.parameter_count ?? 0));
  if (runtime.graph_revision !== view.graph_revision) {
    root.append(textRow("Revision status", "This runtime uses an older graph revision."));
  }
  const matchingEvents = runtimeEvents.filter((item) => item.runtime_instance_id === runtime.runtime_instance_id && item.logical_element_id === element.id);
  const activation = [...matchingEvents].reverse().find((item) => item.kind === "activation.summary");
  const gradient = [...matchingEvents].reverse().find((item) => item.kind === "gradient.summary");
  if (activation?.payload?.value?.shape) root.append(textRow("Actual output", formatShape(activation.payload.value)));
  if (activation) root.append(textRow("Activation", JSON.stringify(activation.payload.value)));
  if (gradient) root.append(textRow("Gradient", JSON.stringify(gradient.payload.value)));
  for (const [signal, detail, label] of [
    ["activation", "summary", "Subscribe activation summary"],
    ["gradient", "summary", "Subscribe gradient summary"],
    ["activation", "histogram", "Show activation histogram"],
    ["activation", "sample", "Show bounded activation sample"],
  ]) {
    const button = document.createElement("button");
    button.textContent = label;
    button.addEventListener("click", () => subscribeRuntime(element, runtime, binding, signal, detail).catch(showError));
    root.append(button);
  }
  const stop = document.createElement("button");
  stop.className = "quiet";
  stop.textContent = "Stop live inspection";
  stop.addEventListener("click", () => stopRuntimeSubscriptions(element, runtime).catch(showError));
  root.append(stop);
  renderCheckpointInspector(root);
}

function renderCheckpointInspector(root) {
  root.append(section("Checkpoint"));
  const row = document.createElement("div");
  row.className = "field";
  const label = document.createElement("label"); label.textContent = "Inspect artifact ID";
  const input = document.createElement("input"); input.type = "text"; input.placeholder = "Graphimo Artifact ID";
  if (checkpointDetails) input.value = checkpointDetails.artifact_id;
  const inspect = document.createElement("button"); inspect.textContent = "Inspect checkpoint";
  inspect.addEventListener("click", async () => {
    try {
      checkpointDetails = await api("metablock.invoke", {
        metablock_id: metablockId,
        action: "checkpoint.inspect",
        args: { artifact_id: input.value.trim() },
      });
      renderInspector();
    } catch (error) { showError(error); }
  });
  row.append(label, input, inspect); root.append(row);
  if (!checkpointDetails) return;
  const details = checkpointDetails;
  for (const [labelText, value] of [
    ["Artifact ID", details.artifact_id],
    ["MetaBlock ID", details.metablock_id],
    ["Graph revision", details.graph_revision],
    ["Materialization key", details.materialization_key],
    ["Model", details.model_name || "unknown"],
    ["PyTorch", details.pytorch_version],
    ["Addon", details.addon_version],
    ["Saved step/epoch", details.metadata?.step ?? details.metadata?.epoch ?? "not supplied"],
    ["Optimizer state", details.optimizer_state_present ? "yes" : "no"],
    ["Scheduler state", details.scheduler_state_present ? "yes" : "no"],
    ["Compatibility", details.graph_revision === view.graph_revision ? "same graph revision" : "migration required"],
  ]) root.append(textRow(labelText, value));
  if (details.graph_revision !== view.graph_revision) {
    const preview = document.createElement("button");
    preview.textContent = "Preview exact-shape migration";
    preview.addEventListener("click", async () => {
      try {
        const externalValues = Object.fromEntries(
          (view.external_parameters || []).map((item) => [item.id, item.default]),
        );
        details.migration_preview = await api("metablock.invoke", {
          metablock_id: metablockId,
          action: "checkpoint.preview_migration",
          args: { artifact_id: details.artifact_id, external_values: externalValues },
        });
        renderInspector();
      } catch (error) { showError(error); }
    });
    root.append(preview);
    if (details.migration_preview?.report) {
      const report = details.migration_preview.report;
      root.append(textRow("Copied exact matches", report.copied.join(", ") || "none"));
      root.append(textRow("Missing in source", report.missing_in_source.join(", ") || "none"));
      root.append(textRow("Missing in target", report.missing_in_target.join(", ") || "none"));
      for (const mismatch of report.shape_mismatch) {
        root.append(textRow(
          `Shape mismatch ${mismatch.name}`,
          `${mismatch.source_shape.join(" × ")} → ${mismatch.target_shape.join(" × ")}`,
        ));
      }
      root.append(textRow(
        "Migration",
        "Preview only. Use graphimo_pytorch.load_checkpoint(..., strict=False) explicitly to create the migrated model. Optimizer state is not migrated.",
      ));
    }
  }
}

async function subscribeRuntime(element, runtime, binding, signal, detail) {
  const key = `${runtime.runtime_instance_id}:${element.id}:${signal}:${detail}`;
  if (activeSubscriptions.has(key)) return;
  const subscriptionId = crypto.randomUUID();
  const repetitionIndex = binding.group_instance_path?.at(-1)?.repetition_index;
  const result = await api("metablock.invoke", {
    metablock_id: metablockId,
    action: "runtime.subscribe",
    args: {
      subscription_id: subscriptionId,
      runtime_instance_id: runtime.runtime_instance_id,
      logical_element_id: element.id,
      ...(Number.isInteger(repetitionIndex) ? { repetition_index: repetitionIndex } : {}),
      signal,
      detail,
      rate_hz: 1,
    },
  });
  activeSubscriptions.set(key, { runtime_instance_id: runtime.runtime_instance_id, subscription_id: result.subscription_id });
  renderInspector();
}

async function stopRuntimeSubscriptions(element, runtime) {
  for (const [key, item] of [...activeSubscriptions]) {
    if (item.runtime_instance_id !== runtime.runtime_instance_id || !key.startsWith(`${runtime.runtime_instance_id}:${element.id}:`)) continue;
    await api("metablock.invoke", { metablock_id: metablockId, action: "runtime.unsubscribe", args: item });
    activeSubscriptions.delete(key);
  }
}

async function stopAllRuntimeSubscriptions() {
  for (const [key, item] of [...activeSubscriptions]) {
    await api("metablock.invoke", { metablock_id: metablockId, action: "runtime.unsubscribe", args: item });
    activeSubscriptions.delete(key);
  }
}

async function refreshRuntimeSelector() {
  if (!metablockId || !view) return;
  const result = await api("metablock.invoke", { metablock_id: metablockId, action: "runtime.list", args: {} });
  const items = result.items || [];
  view.runtime_selector = {
    available: items.length > 0,
    items,
    reason: items.length ? null : "No active runtime is registered in this process.",
  };
  if (!items.some((item) => item.runtime_instance_id === activeRuntimeId)) activeRuntimeId = items[0]?.runtime_instance_id || null;
}

async function pollRuntimeEvents() {
  if (!metablockId || !view) return;
  try {
    await refreshRuntimeSelector();
    const response = await api("metablock.events", { metablock_id: metablockId, cursor: runtimeEventCursor, limit: 128 });
    runtimeEventCursor = response.cursor || runtimeEventCursor;
    runtimeEvents.push(...(response.events || []));
    runtimeEvents = runtimeEvents.slice(-256);
    renderInspector();
  } catch (error) {
    showError(error);
  }
}

function field(labelText, value, commit, type = "text", attributes = {}) {
  const wrapper = document.createElement("div");
  wrapper.className = "field";
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = attributes.choices?.length ? document.createElement("select") : document.createElement("input");
  if (input.tagName === "SELECT") {
    for (const choice of attributes.choices) {
      const option = document.createElement("option"); option.value = String(choice); option.textContent = String(choice); option.selected = choice === value; input.append(option);
    }
  } else {
    input.type = type === "bool" ? "checkbox" : ["int", "float", "number", "optional_float"].includes(type) ? "number" : "text";
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = type === "shape" && Array.isArray(value) ? JSON.stringify(value) : value ?? "";
  }
  for (const [key, item] of Object.entries(attributes)) if (item != null && key !== "choices") input.setAttribute(key, String(item));
  input.addEventListener("change", async () => {
    try { await commit(input.type === "checkbox" ? input.checked : input.value); }
    catch (error) { showError(error); }
  });
  wrapper.append(label, input);
  return wrapper;
}

function section(label) { const el = document.createElement("div"); el.className = "section-label"; el.textContent = label; return el; }
function textRow(label, value) { const el = document.createElement("div"); el.className = "port-row"; el.textContent = `${label}: ${value}`; return el; }
function portRow(label, shape) { const el = document.createElement("div"); el.className = "port-row"; el.innerHTML = `<span>${escapeHtml(label)}</span><span class="shape">${escapeHtml(shape)}</span>`; return el; }
function connectionHint() { const el = document.createElement("div"); el.className = "connection-hint"; el.textContent = `Connecting from ${connectSource.element_id}.${connectSource.port}; choose an input port on the canvas. Escape cancels.`; return el; }
function inputType(type) { return type === "bool" ? "bool" : type; }
function parseValue(type, value) { if (type === "int") return Number.parseInt(value, 10); if (type === "float" || type === "number") return Number(value); if (type === "optional_float") return value === "" ? null : Number(value); if (type === "shape") return JSON.parse(value); return value; }
function formatShape(spec) { return `${spec.dtype || "?"} [${(spec.shape || []).join(" × ")}]`; }

function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]); }

async function sendIntent(intent, refresh = true) {
  try {
    const previousIds = new Set((view?.elements || []).map((item) => item.id));
    await api("metablock.apply", { metablock_id: metablockId, intent });
    if (refresh) {
      await reloadProjection();
      if (["add_element", "add_group_instance"].includes(intent.type)) {
        const inserted = intent.id || intent.instance_id || view?.elements.find((item) => !previousIds.has(item.id))?.id;
        if (inserted && view?.elements.some((item) => item.id === inserted)) {
          selected = new Set([inserted]);
          selectedEdge = null;
          renderAll();
        }
      }
    }
  } catch (error) {
    showError(error);
  }
}

async function enterScope(nextScope) {
  scope = nextScope.startsWith("group:") || nextScope.startsWith("method:") ? nextScope : `method:${nextScope}`;
  selected.clear();
  selectedEdge = null;
  connectSource = null;
  camera = { x: 0, y: 0, zoom: 1 };
  cameraTouched = false;
  await reloadProjection();
}

async function persistLayout() {
  if (metablockId && view) await api("layout.set", { scope_id: getLayoutScope(), layout });
}

function resizeCanvas() {
  const bounds = canvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(bounds.width * scale));
  canvas.height = Math.max(1, Math.floor(bounds.height * scale));
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  if (!cameraTouched && view?.elements?.length) fitCameraToLayout();
  draw();
}

function draw() {
  const bounds = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, bounds.width, bounds.height);
  const hasElements = Boolean(view?.elements?.length);
  $("#empty-state").classList.toggle("hidden", hasElements || !metablockId);
  if (!hasElements) return;
  ctx.save();
  ctx.translate(camera.x, camera.y);
  ctx.scale(camera.zoom, camera.zoom);
  const locations = new Map();
  for (const element of view.elements) {
    const rect = layout[element.id] || { x: 20, y: 20, width: 210, height: 120 };
    locations.set(element.id, rect);
  }
  for (const edge of view.edges || []) {
    const from = locations.get(edge.source.element_id);
    const to = locations.get(edge.target.element_id);
    if (!from || !to) continue;
    const sourceElement = view.elements.find((item) => item.id === edge.source.element_id);
    const targetElement = view.elements.find((item) => item.id === edge.target.element_id);
    const start = portPosition(sourceElement, from, edge.source.port, "output");
    const end = portPosition(targetElement, to, edge.target.port, "input");
    const sx = start.x; const sy = start.y; const tx = end.x; const ty = end.y;
    ctx.beginPath();
    ctx.moveTo(sx, sy);
    ctx.bezierCurveTo(sx + 48, sy, tx - 48, ty, tx, ty);
    ctx.strokeStyle = selectedEdge === edge.id ? "#ffd166" : "#627da9";
    ctx.lineWidth = selectedEdge === edge.id ? 3 : 2;
    ctx.stroke();
    if (edge.shape) {
      ctx.fillStyle = "#aab8cc";
      ctx.font = "10px ui-monospace, monospace";
      ctx.fillText(formatShape(edge.shape), (sx + tx) / 2 - 28, (sy + ty) / 2 - 8);
    }
  }
  for (const element of view.elements) drawElement(element, locations.get(element.id));
  if (pointer?.mode === "connect" && pointer.current) {
    const from = locations.get(connectSource?.element_id);
    const element = view.elements.find((item) => item.id === connectSource?.element_id);
    if (from && element) {
      const start = portPosition(element, from, connectSource.port, "output");
      ctx.beginPath(); ctx.moveTo(start.x, start.y);
      ctx.lineTo(pointer.current.x, pointer.current.y); ctx.strokeStyle = "#81a9ff"; ctx.stroke();
    }
  }
  ctx.restore();
}

function drawElement(element, rect) {
  const selectedHere = selected.has(element.id);
  const inputs = element.inputs || [];
  const outputs = element.outputs || [];
  const height = Math.max(rect.height, 82 + Math.max(inputs.length, outputs.length) * 18);
  rect.height = height;
  ctx.fillStyle = selectedHere ? "#273957" : "#1c2431";
  ctx.strokeStyle = selectedHere ? "#81a9ff" : "#45536a";
  ctx.lineWidth = selectedHere ? 2 : 1;
  roundRect(ctx, rect.x, rect.y, rect.width, height, 8);
  ctx.fill(); ctx.stroke();
  ctx.fillStyle = "#e9edf5"; ctx.font = "600 13px system-ui"; ctx.fillText(element.name, rect.x + 12, rect.y + 21);
  ctx.fillStyle = "#9ba6b8"; ctx.font = "11px system-ui"; ctx.fillText(element.kind, rect.x + 12, rect.y + 40);
  if (element.output_shape) ctx.fillText(formatShape(element.output_shape), rect.x + 12, rect.y + height - 8);
  ctx.font = "10px system-ui";
  inputs.forEach((port, index) => {
    const name = typeof port === "string" ? port : port.name;
    const y = rect.y + 58 + index * 18;
    ctx.beginPath(); ctx.arc(rect.x, y, 5, 0, Math.PI * 2); ctx.fillStyle = "#83c2ff"; ctx.fill();
    ctx.fillStyle = "#c0cad8"; ctx.fillText(name, rect.x + 10, y + 3);
  });
  outputs.forEach((port, index) => {
    const name = typeof port === "string" ? port : port.name;
    const y = rect.y + 58 + index * 18;
    ctx.beginPath(); ctx.arc(rect.x + rect.width, y, 5, 0, Math.PI * 2); ctx.fillStyle = "#72d6a1"; ctx.fill();
    ctx.fillStyle = "#c0cad8"; ctx.textAlign = "right"; ctx.fillText(name, rect.x + rect.width - 10, y + 3); ctx.textAlign = "left";
  });
}

function roundRect(context, x, y, width, height, radius) {
  context.beginPath(); context.roundRect(x, y, width, height, radius);
}

function portPosition(element, rect, portName, direction) {
  const ports = element?.[direction === "input" ? "inputs" : "outputs"] || [];
  const index = ports.findIndex((item) => (typeof item === "string" ? item : item.name) === portName);
  return { x: direction === "input" ? rect.x : rect.x + rect.width, y: rect.y + 58 + Math.max(0, index) * 18 };
}

function worldPoint(event) {
  const bounds = canvas.getBoundingClientRect();
  return { x: (event.clientX - bounds.left - camera.x) / camera.zoom, y: (event.clientY - bounds.top - camera.y) / camera.zoom };
}

function hitPort(point) {
  for (const element of view?.elements || []) {
    const rect = layout[element.id]; if (!rect) continue;
    for (const [direction, ports] of [["input", element.inputs || []], ["output", element.outputs || []]]) {
      for (let index = 0; index < ports.length; index++) {
        const port = ports[index];
        const x = direction === "input" ? rect.x : rect.x + rect.width;
        const y = rect.y + 58 + index * 18;
        if (Math.hypot(point.x - x, point.y - y) <= 10) return { element, port: typeof port === "string" ? port : port.name, direction };
      }
    }
  }
  return null;
}

function hitElement(point) {
  return [...(view?.elements || [])].reverse().find((element) => {
    const rect = layout[element.id]; return rect && point.x >= rect.x && point.x <= rect.x + rect.width && point.y >= rect.y && point.y <= rect.y + rect.height;
  });
}

function hitEdge(point) {
  for (const edge of view?.edges || []) {
    const from = layout[edge.source.element_id]; const to = layout[edge.target.element_id];
    const sourceElement = view.elements.find((item) => item.id === edge.source.element_id);
    const targetElement = view.elements.find((item) => item.id === edge.target.element_id);
    if (!from || !to || !sourceElement || !targetElement) continue;
    const start = portPosition(sourceElement, from, edge.source.port, "output");
    const end = portPosition(targetElement, to, edge.target.port, "input");
    for (let step = 0; step <= 16; step++) {
      const t = step / 16; const u = 1 - t;
      const x = u ** 3 * start.x + 3 * u ** 2 * t * (start.x + 48) + 3 * u * t ** 2 * (end.x - 48) + t ** 3 * end.x;
      const y = u ** 3 * start.y + 3 * u ** 2 * t * start.y + 3 * u * t ** 2 * end.y + t ** 3 * end.y;
      if (Math.hypot(point.x - x, point.y - y) <= 8) return edge;
    }
  }
  return null;
}

canvas.addEventListener("pointerdown", (event) => {
  if (!view) return;
  const point = worldPoint(event);
  const port = hitPort(point);
  if (port?.direction === "output") {
    selectedEdge = null;
    connectSource = { element_id: port.element.id, port: port.port };
    pointer = { mode: "connect", current: point };
    renderInspector(); draw(); return;
  }
  if (port?.direction === "input" && connectSource) {
    const intent = { type: "connect", scope, source: connectSource, target: { element_id: port.element.id, port: port.port } };
    connectSource = null; pointer = null; sendIntent(intent); return;
  }
  const edge = hitEdge(point);
  if (edge) {
    selected.clear(); selectedEdge = edge.id; pointer = null; renderInspector(); draw(); return;
  }
  if (event.button === 1 || event.button === 2 || (!hitElement(point) && event.shiftKey)) {
    pointer = { mode: "pan", lastX: event.clientX, lastY: event.clientY };
    canvas.setPointerCapture(event.pointerId); return;
  }
  const element = hitElement(point);
  if (element) {
    selectedEdge = null;
    if (event.metaKey || event.ctrlKey || event.shiftKey) selected.has(element.id) ? selected.delete(element.id) : selected.add(element.id);
    else if (!selected.has(element.id)) selected = new Set([element.id]);
    const rect = layout[element.id];
    pointer = { mode: "move", elementId: element.id, startX: point.x, startY: point.y, positions: Object.fromEntries([...selected].map((id) => [id, { ...layout[id] }])) };
    canvas.setPointerCapture(event.pointerId);
    if (event.detail > 1 && element.kind === "group_instance") enterScope(`group:${element.group_id}`);
    renderInspector(); draw(); return;
  }
  selectedEdge = null;
  if (!event.metaKey && !event.ctrlKey && !event.shiftKey) selected.clear();
  renderInspector(); draw();
});
canvas.addEventListener("pointermove", (event) => {
  if (!pointer) return;
  const point = worldPoint(event);
  if (pointer.mode === "move") {
    const dx = point.x - pointer.startX; const dy = point.y - pointer.startY;
    for (const [id, rect] of Object.entries(pointer.positions)) layout[id] = { ...rect, x: rect.x + dx, y: rect.y + dy };
  } else if (pointer.mode === "pan") {
    cameraTouched = true;
    camera.x += event.clientX - pointer.lastX; camera.y += event.clientY - pointer.lastY;
    pointer.lastX = event.clientX; pointer.lastY = event.clientY;
  } else if (pointer.mode === "connect") pointer.current = point;
  draw();
});
canvas.addEventListener("pointerup", async () => {
  if (pointer?.mode === "move") await persistLayout();
  pointer = null;
});
canvas.addEventListener("wheel", (event) => {
  event.preventDefault();
  cameraTouched = true;
  const oldZoom = camera.zoom;
  const nextZoom = Math.max(0.35, Math.min(2, oldZoom * (event.deltaY < 0 ? 1.08 : 0.92)));
  const bounds = canvas.getBoundingClientRect();
  const px = event.clientX - bounds.left; const py = event.clientY - bounds.top;
  camera.x = px - (px - camera.x) * (nextZoom / oldZoom);
  camera.y = py - (py - camera.y) * (nextZoom / oldZoom);
  camera.zoom = nextZoom; draw();
}, { passive: false });
canvas.addEventListener("contextmenu", (event) => event.preventDefault());
window.addEventListener("keydown", (event) => { if (event.key === "Escape") { connectSource = null; pointer = null; renderInspector(); draw(); } });
window.addEventListener("resize", resizeCanvas);

$("#refresh").addEventListener("click", () => refreshProject().catch(showError));
$("#new-model").addEventListener("click", () => createModel().catch(showError));
$("#delete-selection").addEventListener("click", async () => {
  if (selectedEdge) {
    const edgeId = selectedEdge;
    selectedEdge = null;
    await sendIntent({ type: "disconnect", scope, edge_id: edgeId });
    return;
  }
  for (const id of [...selected]) await sendIntent({ type: "delete_element", scope, element_id: id }, false);
  selected.clear(); await reloadProjection();
});
$("#generate-cell").addEventListener("click", async () => {
  try {
    const result = await api("metablock.materialize", { metablock_id: metablockId, bindings: {} });
    $("#source code").textContent = `${result.source}\n# Standalone Workbench preview only: no live marimo session is attached.`;
  } catch (error) { showError(error); }
});

function showError(error) {
  const row = document.createElement("div"); row.className = "diagnostic"; row.textContent = error.message || String(error);
  $("#diagnostic-list").prepend(row);
}

refreshProject().then(resizeCanvas).catch(showError);
window.setInterval(() => { void pollRuntimeEvents(); }, 1000);
