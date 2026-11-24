"use strict";

// Renderer only: this file draws application view models and submits intents.
const el = (id) => document.getElementById(id);
const ui = {
  state: null,
  selected: [],
  inspected: null,
  artifactStats: null,
  showArtifacts: false,
  zoom: 1,
  pan: { x: 40, y: 40 },
  connectFrom: null,
  drag: null,
  controlEditing: false,
  controlTimers: new Map(),
};

async function intent(action, payload = {}) {
  const response = await fetch("/api/intent", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...payload }),
  });
  const result = await response.json();
  if (!response.ok || result.error) throw new Error(result.error?.message || "Graphimo operation failed");
  if (result.state) {
    if (ui.controlEditing && action === "set_control_value") ui.state = result.state;
    else setState(result.state);
  }
  if (result.inspection) {
    ui.inspected = result.inspection;
    renderInspector();
  }
  if (result.artifact_stats) {
    ui.artifactStats = result.artifact_stats;
    ui.showArtifacts = true;
    renderInspector();
  }
  return result;
}

function tell(message, isError = false) {
  const footer = document.querySelector(".feedback");
  footer.classList.toggle("error", isError);
  el("feedback-message").textContent = message;
}

async function act(action, payload = {}) {
  try {
    return await intent(action, payload);
  } catch (error) {
    tell(error.message || String(error), true);
    return null;
  }
}

function setState(state) {
  ui.state = state;
  ui.selected = ui.selected.filter((id) => state.blocks.some((block) => block.id === id));
  el("project-name").textContent = state.project_id.split(/[\\/]/).pop() || state.project_id;
  el("breadcrumbs").textContent = (state.breadcrumbs || []).map((crumb) => crumb.title).join(" / ");
  el("canvas-empty").classList.toggle("hidden", state.blocks.length > 0);
  el("palette-search").disabled = false;
  ["refresh", "zoom-out", "zoom-in", "fit", "run-selected", "artifact-store"].forEach((id) => (el(id).disabled = false));
  renderPalette();
  renderBlocks();
  renderInspector();
}

function renderPalette() {
  const list = el("palette-list");
  const search = el("palette-search").value.trim().toLowerCase();
  const items = (ui.state?.palette?.items || []).filter((item) => {
    const haystack = [item.title, item.category, item.description, ...(item.search_terms || [])].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(search);
  });
  list.className = "palette-list";
  list.replaceChildren();
  if (!items.length) {
    list.classList.add("empty-state");
    list.textContent = ui.state ? "No nodes match this search in the current scope." : "Open a marimo project to discover its @node definitions.";
    return;
  }
  for (const item of items) {
    const button = document.createElement("button");
    button.className = "palette-item";
    button.disabled = item.enabled === false;
    button.innerHTML = `<span class="palette-icon">ƒ</span><span class="palette-meta"><span class="palette-title"></span><span class="palette-subtitle"></span></span>`;
    button.querySelector(".palette-title").textContent = item.title;
    button.querySelector(".palette-subtitle").textContent = item.description || item.category;
    button.addEventListener("click", async () => {
      const bindings = {};
      let parameters;
      if (item.kind === "control") {
        parameters = Object.fromEntries((item.input_schema || []).map((input) => [input.name, input.default]));
        const configured = window.prompt("Control settings (JSON)", JSON.stringify(parameters, null, 2));
        if (configured === null) return;
        try { parameters = JSON.parse(configured); } catch { tell("Enter valid JSON control settings.", true); return; }
      }
      for (const input of item.input_schema || []) {
        if (item.kind === "control") continue;
        if (!input.required) continue;
        const value = window.prompt(`Python expression for required input “${input.name}”`, "");
        if (value === null) return;
        bindings[input.name] = value;
      }
      const center = viewportCenter();
      const result = await act("add_palette_item", { item_id: item.id, bindings, parameters, position: center });
      if (result?.result?.cell_id) {
        ui.selected = [result.result.cell_id];
        ui.inspected = null;
        renderBlocks();
        renderInspector();
      }
    });
    list.append(button);
  }
}

function viewportCenter() {
  const rect = el("canvas-viewport").getBoundingClientRect();
  return [Math.round((rect.width / 2 - ui.pan.x) / ui.zoom), Math.round((rect.height / 2 - ui.pan.y) / ui.zoom)];
}

function statusClass(status) {
  const value = String(status || "").toLowerCase();
  if (value.includes("error") || value.includes("exception")) return "error";
  if (value.includes("run") || value.includes("queued")) return "running";
  if (value.includes("stale")) return "stale";
  if (value.includes("disabled")) return "disabled";
  if (value.includes("success") || value.includes("idle")) return "success";
  return "";
}

function renderBlocks() {
  if (!ui.state) return;
  const container = el("blocks");
  container.replaceChildren();
  for (const block of ui.state.blocks) {
    const card = document.createElement("article");
    card.className = `block-card ${ui.selected.includes(block.id) ? "selected" : ""} ${statusClass(block.state) === "error" ? "error" : ""}`;
    card.dataset.blockId = block.id;
    card.style.left = `${block.x}px`;
    card.style.top = `${block.y}px`;
    const head = document.createElement("div");
    head.className = "block-head";
    head.innerHTML = `<span class="block-title-wrap"><i class="state-dot ${statusClass(block.state)}"></i><span class="block-title"></span></span><span class="block-actions"><button data-action="run" title="Run this block">▶</button><button data-action="inspect" title="Inspect outputs">ⓘ</button><button data-action="more" title="Block actions">⋯</button></span>`;
    head.querySelector(".block-title").textContent = block.title;
    if (block.kind === "control") {
      head.querySelector('[data-action="run"]').hidden = true;
      head.querySelector('[data-action="inspect"]').hidden = true;
    }
    head.querySelector('[data-action="run"]').addEventListener("click", (event) => {
      event.stopPropagation();
      act("run_target", { cell_id: block.id });
    });
    head.querySelector('[data-action="inspect"]').addEventListener("click", (event) => {
      event.stopPropagation();
      inspectBlock(block.id);
    });
    head.querySelector('[data-action="more"]').addEventListener("click", (event) => {
      event.stopPropagation();
      showBlockMenu(block.id, event.currentTarget);
    });
    head.addEventListener("pointerdown", (event) => startBlockDrag(event, block));
    card.append(head);
    const body = document.createElement("div");
    body.className = "block-body";
    if (block.kind === "control") renderControlWidget(block, body);
    for (const input of block.inputs || []) {
      const row = document.createElement("div");
      row.className = "port-row";
      row.innerHTML = `<button class="port-anchor input-anchor" title="Input ${escapeHtml(input.name)}"></button><span class="port-name"></span><span class="port-type"></span>`;
      row.querySelector(".port-name").textContent = input.name;
      row.querySelector(".port-type").textContent = input.declared_type || "";
      row.querySelector(".input-anchor").dataset.port = input.name;
      row.querySelector(".input-anchor").addEventListener("click", (event) => {
        event.stopPropagation();
        finishConnection(block.id, input.name);
      });
      body.append(row);
    }
    for (const output of block.outputs || []) {
      const row = document.createElement("div");
      row.className = "port-row output";
      row.innerHTML = `<span class="port-name"></span><span class="port-type"></span><span class="port-preview"></span><button class="port-anchor output-anchor" title="Connect output"></button>`;
      row.querySelector(".port-name").textContent = output.name;
      row.querySelector(".port-type").textContent = output.runtime_type || output.declared_type || "?";
      row.querySelector(".port-preview").textContent = output.preview || output.expression || "";
      row.querySelector(".output-anchor").addEventListener("click", (event) => {
        event.stopPropagation();
        ui.connectFrom = { cellId: block.id, outputIndex: output.id.split(":").pop() };
        event.currentTarget.classList.add("connecting");
        tell(`Choose an input for ${output.name}.`);
      });
      body.append(row);
    }
    const summary = (block.outputs || []).find((output) => output.preview);
    if (summary) {
      const text = document.createElement("div");
      text.className = "block-summary";
      text.textContent = `${summary.runtime_type || summary.declared_type || "Output"} · ${summary.preview}`;
      body.append(text);
    }
    card.append(body);
    card.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      selectBlock(block.id, event.shiftKey);
    });
    container.append(card);
  }
  applyWorldTransform();
  requestAnimationFrame(renderEdges);
  const selectedBlock = ui.state.blocks.find((item) => item.id === ui.selected.at(-1));
  el("run-selected").disabled = !selectedBlock || selectedBlock.kind === "control";
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function renderEdges() {
  if (!ui.state) return;
  const svg = el("edges");
  svg.replaceChildren();
  for (const edge of ui.state.edges || []) {
    const source = document.querySelector(`[data-block-id="${CSS.escape(edge.source_block_id)}"]`);
    const target = document.querySelector(`[data-block-id="${CSS.escape(edge.target_block_id)}"]`);
    if (!source || !target) continue;
    const sourceRect = source.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const worldRect = el("canvas-world").getBoundingClientRect();
    const x1 = (sourceRect.right - worldRect.left) / ui.zoom;
    const y1 = (sourceRect.top + sourceRect.height / 2 - worldRect.top) / ui.zoom;
    const x2 = (targetRect.left - worldRect.left) / ui.zoom;
    const y2 = (targetRect.top + targetRect.height / 2 - worldRect.top) / ui.zoom;
    const bend = Math.max(40, Math.abs(x2 - x1) * .45);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`);
    path.setAttribute("class", `edge-path ${edge.state === "stale" ? "stale" : ""}`);
    svg.append(path);
  }
}

function applyWorldTransform() {
  el("canvas-world").style.transform = `translate(${ui.pan.x}px, ${ui.pan.y}px) scale(${ui.zoom})`;
  el("zoom-label").textContent = `${Math.round(ui.zoom * 100)}%`;
}

function selectBlock(id, toggle) {
  if (toggle) ui.selected = ui.selected.includes(id) ? ui.selected.filter((item) => item !== id) : [...ui.selected, id];
  else ui.selected = [id];
  ui.inspected = null;
  ui.showArtifacts = false;
  act("select", { ids: ui.selected });
  renderBlocks();
  renderInspector();
}

async function finishConnection(targetCellId, inputName) {
  if (!ui.connectFrom) return;
  const source = ui.connectFrom;
  ui.connectFrom = null;
  const result = await act("connect", {
    source_cell_id: source.cellId,
    target_cell_id: targetCellId,
    input_name: inputName,
    output_index: Number(source.outputIndex),
  });
  document.querySelectorAll(".port-anchor.connecting").forEach((item) => item.classList.remove("connecting"));
  if (result) tell("Connection updated. Marimo owns the resulting dependency graph.");
}

async function inspectBlock(cellId) {
  const result = await act("inspect", { cell_id: cellId });
  if (result?.inspection) {
    ui.inspected = result.inspection;
    ui.selected = [cellId];
    renderBlocks();
    renderInspector();
  }
}

function renderInspector() {
  const panel = el("inspector-content");
  panel.replaceChildren();
  const selected = ui.state?.blocks?.find((block) => block.id === ui.selected.at(-1));
  el("inspector-title").textContent = selected?.title || "Inspector";
  el("status-pill").textContent = selected?.state || "Idle";
  el("status-pill").classList.toggle("error", statusClass(selected?.state) === "error");
  if (ui.showArtifacts && ui.artifactStats) {
    const stats = ui.artifactStats;
    el("inspector-title").textContent = "ArtifactStore";
    el("status-pill").textContent = `${stats.artifacts} artifacts`;
    el("status-pill").classList.remove("error");
    panel.className = "inspector-content";
    panel.replaceChildren(sectionLabel("Storage usage"));
    for (const [label, value] of [
      ["Logical bytes", stats.logical_bytes],
      ["Physical stored bytes", stats.physical_stored_bytes],
      ["Deduplicated bytes", stats.deduplicated_bytes],
      ["Unique blobs", stats.blobs],
    ]) {
      const row = document.createElement("div");
      row.className = "detail-row";
      row.innerHTML = "<strong></strong><code></code>";
      row.querySelector("strong").textContent = label;
      row.querySelector("code").textContent = Number(value).toLocaleString();
      panel.append(row);
    }
    panel.append(sectionLabel("Persistence"));
    const note = document.createElement("div");
    note.className = "output-preview";
    note.textContent = "Artifacts are stored only when a Graphimo operation explicitly persists them.";
    panel.append(note);
    return;
  }
  if (!selected) {
    panel.className = "inspector-content empty-state";
    panel.textContent = "Select a block to inspect its inputs and outputs.";
    return;
  }
  panel.className = "inspector-content";
  const inputs = selected.inputs || [];
  if (inputs.length) {
    panel.append(sectionLabel("Inputs"));
    for (const input of inputs) {
      const row = document.createElement("div");
      row.className = "detail-row";
      row.innerHTML = `<strong></strong><code class="input-expression"></code><input aria-label="Literal value" placeholder="JSON literal" /><span class="detail-actions"><button class="apply-literal">Apply literal</button><button class="disconnect">Disconnect</button></span>`;
      row.querySelector("strong").textContent = `${input.name}${input.declared_type ? ` · ${input.declared_type}` : ""}`;
      row.querySelector(".input-expression").textContent = input.expression || (input.connected ? "Connected" : input.preview || "Unbound");
      const field = row.querySelector("input");
      field.value = input.expression && !input.connected ? input.expression : "";
      const apply = row.querySelector(".apply-literal");
      apply.textContent = input.connected ? "Replace connection with literal" : "Apply literal";
      apply.addEventListener("click", async () => {
        if (input.connected && !window.confirm(`Replace the connection to “${input.name}” with a literal?`)) return;
        let value;
        try { value = JSON.parse(field.value); } catch { tell("Enter a valid JSON literal, such as 0.5, true, or \"text\".", true); return; }
        await act("set_literal", { cell_id: selected.id, input_name: input.name, value });
      });
      row.querySelector(".disconnect").addEventListener("click", () => act("disconnect", { target_cell_id: selected.id, input_name: input.name }));
      panel.append(row);
    }
  }
  if (selected.outputs?.length) {
    panel.append(sectionLabel("Outputs"));
    for (const output of selected.outputs) {
      const row = document.createElement("div");
      row.className = "detail-row";
      row.innerHTML = `<strong></strong><code></code><div class="output-preview"></div>`;
      row.querySelector("strong").textContent = `${output.runtime_type || output.declared_type || "?"} · ${output.name}`;
      row.querySelector("code").textContent = output.expression || "Multiple return paths";
      row.querySelector(".output-preview").textContent = output.preview || "No runtime value yet";
      panel.append(row);
    }
  }
  const diagnostics = (ui.state.diagnostics || []).filter((item) => item.block_id === selected.id);
  if (diagnostics.length) {
    panel.append(sectionLabel("Diagnostics"));
    for (const diagnostic of diagnostics) {
      const row = document.createElement("div");
      row.className = "output-preview";
      row.textContent = `${diagnostic.code}: ${diagnostic.message}`;
      panel.append(row);
    }
  }
  const controls = document.createElement("div");
  controls.className = "detail-section";
  controls.append(sectionLabel("Block"));
  const run = document.createElement("button");
  run.className = "primary";
  run.textContent = "Run this block";
  run.addEventListener("click", () => act("run_target", { cell_id: selected.id }));
  controls.append(run);
  panel.append(controls);
}

function sectionLabel(text) {
  const label = document.createElement("div");
  label.className = "detail-label";
  label.textContent = text;
  return label;
}

function showBlockMenu(id) {
  const block = ui.state?.blocks.find((item) => item.id === id);
  const actions = [...(block?.kind === "control" ? [] : ["duplicate"]), "delete", ...(block?.can_disable ? ["disable"] : []), ...(block?.can_enable ? ["enable"] : [])];
  const choice = window.prompt(`Block action: ${actions.join(", ")}`, actions[0]);
  if (choice === "duplicate") act("duplicate_elements", { ids: [id] });
  else if (choice === "delete") act("delete_elements", { ids: [id] });
  else if (choice === "disable" && block?.can_disable) act("disable_instance", { cell_id: id });
  else if (choice === "enable" && block?.can_enable) act("enable_instance", { cell_id: id });
}

function renderControlWidget(block, parent) {
  const type = block.body.control_type;
  const params = block.body.parameters || {};
  const value = block.body.value;
  const widget = document.createElement("div");
  widget.className = "control-widget";
  widget.addEventListener("pointerdown", (event) => event.stopPropagation());
  const live = block.body.execution_policy === "live";
  const submit = async (nextValue) => {
    await act("set_control_value", { cell_id: block.id, value: nextValue });
  };
  const schedule = (nextValue) => {
    ui.controlEditing = true;
    const existing = ui.controlTimers.get(block.id);
    if (existing) clearTimeout(existing.timer);
    const timer = setTimeout(() => {
      const pending = ui.controlTimers.get(block.id);
      ui.controlTimers.delete(block.id);
      if (pending) submit(pending.value);
    }, 100);
    ui.controlTimers.set(block.id, { timer, value: nextValue });
  };
  const finish = async (nextValue) => {
    const pending = ui.controlTimers.get(block.id);
    if (pending) clearTimeout(pending.timer);
    ui.controlTimers.delete(block.id);
    ui.controlEditing = false;
    await submit(nextValue);
  };
  const rangeInput = (current, label, onInput, onCommit) => {
    const input = document.createElement("input");
    input.type = "range";
    input.min = params.start ?? 0;
    input.max = params.stop ?? 1;
    input.step = params.step ?? "any";
    input.value = current ?? 0;
    input.setAttribute("aria-label", label);
    const output = document.createElement("output");
    output.textContent = String(current ?? 0);
    input.addEventListener("input", () => {
      output.textContent = input.value;
      onInput(Number(input.value));
    });
    input.addEventListener("change", () => onCommit(Number(input.value)));
    widget.append(input, output);
  };
  if (type === "slider") {
    rangeInput(value, params.label, (next) => live && schedule(next), finish);
  } else if (type === "range_slider") {
    const values = [...(Array.isArray(value) ? value : params.value)];
    for (let index = 0; index < 2; index += 1) {
      rangeInput(values[index], `${params.label} ${index === 0 ? "minimum" : "maximum"}`,
        (next) => { values[index] = index === 0 ? Math.min(next, values[1]) : Math.max(next, values[0]); if (live) schedule(values); },
        (next) => { values[index] = index === 0 ? Math.min(next, values[1]) : Math.max(next, values[0]); finish(values); });
    }
  } else if (type === "number") {
    const input = document.createElement("input");
    input.type = "number";
    if (params.start != null) input.min = params.start;
    if (params.stop != null) input.max = params.stop;
    if (params.step != null) input.step = params.step;
    input.value = value ?? "";
    input.setAttribute("aria-label", params.label);
    input.addEventListener("input", () => { if (live && input.value !== "") schedule(Number(input.value)); });
    input.addEventListener("change", () => { if (input.value !== "") finish(Number(input.value)); });
    widget.append(input);
  } else if (type === "select") {
    const select = document.createElement("select");
    select.setAttribute("aria-label", params.label);
    for (const [index, option] of params.options.entries()) {
      const item = document.createElement("option");
      item.value = String(index);
      item.textContent = String(option);
      item.selected = typeof option === typeof value && option === value;
      select.append(item);
    }
    select.addEventListener("change", () => finish(params.options[Number(select.value)]));
    widget.append(select);
  } else if (type === "checkbox" || type === "switch") {
    const label = document.createElement("label");
    if (type === "switch") label.className = "toggle-control";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
    input.addEventListener("change", () => finish(input.checked));
    label.append(input, document.createTextNode(params.label || type));
    widget.append(label);
  } else if (type === "text") {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value ?? "";
    input.placeholder = params.placeholder || "";
    input.setAttribute("aria-label", params.label);
    input.addEventListener("input", () => { if (live) schedule(input.value); });
    input.addEventListener("change", () => { if (!live) finish(input.value); });
    input.addEventListener("blur", () => finish(input.value));
    widget.append(input);
  } else if (type === "button") {
    const button = document.createElement("button");
    button.textContent = params.label || "Trigger";
    button.addEventListener("click", () => finish((typeof value === "number" ? value : 0) + 1));
    widget.append(button);
  }
  if (["slider", "range_slider", "number"].includes(type)) {
    const policy = document.createElement("span");
    policy.className = "control-policy";
    policy.textContent = live ? "Live" : "On release";
    widget.append(policy);
  }
  parent.append(widget);
}

function startBlockDrag(event, block) {
  if (event.button !== 0 || event.target.closest("button")) return;
  if (!ui.selected.includes(block.id)) ui.selected = [block.id];
  ui.drag = { startX: event.clientX, startY: event.clientY, ids: [...ui.selected], positions: new Map() };
  for (const id of ui.drag.ids) {
    const model = ui.state.blocks.find((item) => item.id === id);
    if (model) ui.drag.positions.set(id, [model.x, model.y]);
  }
  event.currentTarget.setPointerCapture(event.pointerId);
  event.currentTarget.onpointermove = (moveEvent) => {
    if (!ui.drag) return;
    const dx = (moveEvent.clientX - ui.drag.startX) / ui.zoom;
    const dy = (moveEvent.clientY - ui.drag.startY) / ui.zoom;
    for (const id of ui.drag.ids) {
      const card = document.querySelector(`[data-block-id="${CSS.escape(id)}"]`);
      const origin = ui.drag.positions.get(id);
      if (card && origin) { card.style.left = `${origin[0] + dx}px`; card.style.top = `${origin[1] + dy}px`; }
    }
    requestAnimationFrame(renderEdges);
  };
  event.currentTarget.onpointerup = async (upEvent) => {
    if (!ui.drag) return;
    const dx = (upEvent.clientX - ui.drag.startX) / ui.zoom;
    const dy = (upEvent.clientY - ui.drag.startY) / ui.zoom;
    const positions = {};
    for (const id of ui.drag.ids) {
      const origin = ui.drag.positions.get(id);
      if (origin) positions[id] = [origin[0] + dx, origin[1] + dy];
    }
    ui.drag = null;
    event.currentTarget.onpointermove = null;
    event.currentTarget.onpointerup = null;
    await act("move_elements", { positions });
  };
}

function setupCanvas() {
  const viewport = el("canvas-viewport");
  let panStart = null;
  viewport.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".block-card")) return;
    if (event.button !== 0 && event.button !== 1) return;
    if (event.target.closest(".canvas-toolbar")) return;
    panStart = { x: event.clientX, y: event.clientY, pan: { ...ui.pan } };
    viewport.setPointerCapture(event.pointerId);
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!panStart) return;
    ui.pan = { x: panStart.pan.x + event.clientX - panStart.x, y: panStart.pan.y + event.clientY - panStart.y };
    applyWorldTransform();
    renderEdges();
  });
  viewport.addEventListener("pointerup", () => { panStart = null; });
  viewport.addEventListener("wheel", (event) => {
    event.preventDefault();
    const rect = viewport.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    zoomAt(event.deltaY < 0 ? 1.1 : 1 / 1.1, px, py);
  }, { passive: false });
  viewport.addEventListener("click", (event) => {
    if (event.target.closest(".block-card") || panStart || ui.connectFrom) return;
    ui.selected = [];
    ui.inspected = null;
    act("select", { ids: [] });
    renderInspector();
    renderBlocks();
  });
}

function zoomAt(factor, x, y) {
  const next = Math.max(.35, Math.min(1.8, ui.zoom * factor));
  const ratio = next / ui.zoom;
  ui.pan = { x: x - (x - ui.pan.x) * ratio, y: y - (y - ui.pan.y) * ratio };
  ui.zoom = next;
  applyWorldTransform();
  renderEdges();
}

function fitGraph() {
  const blocks = ui.state?.blocks || [];
  if (!blocks.length) return;
  const minX = Math.min(...blocks.map((block) => block.x));
  const minY = Math.min(...blocks.map((block) => block.y));
  const maxX = Math.max(...blocks.map((block) => block.x + block.width));
  const maxY = Math.max(...blocks.map((block) => block.y + block.height));
  const rect = el("canvas-viewport").getBoundingClientRect();
  ui.zoom = Math.max(.35, Math.min(1.5, Math.min((rect.width - 100) / (maxX - minX + 80), (rect.height - 100) / (maxY - minY + 80))));
  ui.pan = { x: 50 - minX * ui.zoom, y: 50 - minY * ui.zoom };
  applyWorldTransform();
  renderEdges();
}

el("open-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const path = el("project-path").value.trim();
  const result = await act("open_project", { path });
  if (result?.state) {
    ui.selected = [];
    ui.inspected = null;
    ui.showArtifacts = false;
    ui.pan = { x: 40, y: 40 };
    ui.zoom = 1;
    renderBlocks();
    fitGraph();
    tell("Project opened. Marimo is active behind the Workbench.");
  }
});
el("refresh").addEventListener("click", () => act("refresh_project"));
el("artifact-store").addEventListener("click", () => {
  if (ui.showArtifacts) {
    ui.showArtifacts = false;
    renderInspector();
  } else {
    act("inspect_artifact_store");
  }
});
el("palette-search").addEventListener("input", renderPalette);
el("zoom-in").addEventListener("click", () => zoomAt(1.12, el("canvas-viewport").clientWidth / 2, el("canvas-viewport").clientHeight / 2));
el("zoom-out").addEventListener("click", () => zoomAt(1 / 1.12, el("canvas-viewport").clientWidth / 2, el("canvas-viewport").clientHeight / 2));
el("fit").addEventListener("click", fitGraph);
el("run-selected").addEventListener("click", () => {
  if (ui.selected.length === 1) act("run_target", { cell_id: ui.selected[0] });
  else tell("Select one block to run it through marimo.", true);
});
el("dismiss-feedback").addEventListener("click", () => tell("Workbench ready."));
window.addEventListener("resize", () => requestAnimationFrame(renderEdges));
window.addEventListener("keydown", (event) => {
  if (event.target.matches("input")) return;
  if (event.key === "Escape") { ui.connectFrom = null; document.querySelectorAll(".connecting").forEach((item) => item.classList.remove("connecting")); }
  if ((event.key === "Delete" || event.key === "Backspace") && ui.selected.length) act("delete_elements", { ids: ui.selected });
  if (event.key.toLowerCase() === "f") fitGraph();
});

setupCanvas();
