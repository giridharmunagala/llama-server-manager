"use strict";

const $ = (id) => document.getElementById(id);
const DEFAULTS = "*";
const TRUTHY = new Set(["1", "true", "on", "enabled", "yes"]);
const FALSY = new Set(["0", "false", "off", "disabled", "no"]);
const VIEWS = ["overview", "models", "router", "observability"];
const CTX_STEPS = [0, 2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144];

let token = "";
let busy = false;
let connected = false;
let currentState = "stopped";
let currentPid = null;
let workspace = null;
let polling = null;
let catalog = null;
let savedSnapshot = "";
let scope = "";
let tab = "essentials";
let textMode = false;
let textDirty = false;
let pendingApply = false;
let liveModels = [];
let newSource = "local";
let exeMode = "auto";
let detection = null;
let renderedDecision = false;
const DEFAULT_EXECUTABLE = "llama";
const invalid = new Map();
const sourceStash = new Map();
const sourceModes = new Map();

// ---------- generic helpers ----------

function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (name === "class") element.className = value;
    else if (name === "dataset") Object.assign(element.dataset, value);
    else if (name.startsWith("on")) element.addEventListener(name.slice(2), value);
    else if (name in element && !["list", "form"].includes(name)) element[name] = value;
    else if (value !== false) element.setAttribute(name, value === true ? "" : String(value));
  }
  element.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
  return element;
}

function showError(id, message) {
  $(id).textContent = message || "";
  $(id).hidden = !message;
}

let toastTimer = null;
function toast(message, kind = "ok") {
  let element = $("toast");
  if (!element) {
    element = h("div", { id: "toast", class: "toast", role: "status" });
    document.body.append(element);
  }
  element.textContent = message;
  element.dataset.kind = kind;
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), kind === "error" ? 7000 : 3500);
}

function notify(message) {
  $("notice").textContent = message;
  toast(message);
}

async function request(path, body) {
  const options = { cache: "no-store" };
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-CSRF-Token": token };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`Dashboard returned an invalid response (HTTP ${response.status}).`);
  }
  if (!response.ok) throw new Error(data.error || `Request failed (HTTP ${response.status}).`);
  return data;
}

function formatCtx(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "Model default";
  if (number < 1024) return `${number}`;
  const k = number / 1024;
  return `${Number.isInteger(k) ? k : k.toFixed(1)}K`;
}

// ---------- option model ----------

const optionByKey = new Map(OPTIONS.map((option) => [option.key, option]));
const optionIndex = new Map();
for (const option of OPTIONS) {
  optionIndex.set(option.key.toLowerCase(), { option, kind: "on" });
  for (const alias of option.aliases) optionIndex.set(alias.toLowerCase(), { option, kind: "on" });
  for (const alias of option.off || []) optionIndex.set(alias.toLowerCase(), { option, kind: "off" });
}
const lookup = (key) => optionIndex.get(key.toLowerCase());
const isBoolean = (option) => option.type === "toggle" || option.type === "bool";

function readField(options, option) {
  for (const [key, raw] of Object.entries(options)) {
    const match = lookup(key);
    if (match?.option !== option) continue;
    let value = raw;
    if (isBoolean(option)) {
      const text = raw.trim().toLowerCase();
      const truthy = TRUTHY.has(text) || !FALSY.has(text);
      value = match.kind === "on" ? truthy : !truthy;
    }
    return { present: true, key, raw, value, kind: match.kind };
  }
  return { present: false };
}

function encodeField(option, value, previous) {
  if (!isBoolean(option)) return [previous?.kind === "on" ? previous.key : option.key, String(value)];
  if (option.type === "bool") return [option.key, value ? "true" : "false"];
  if (value) return [option.key, "true"];
  if (option.off?.length) return [previous?.kind === "off" ? previous.key : option.off[0], "true"];
  return [option.key, "false"];
}

function writeField(options, option, value) {
  const previous = readField(options, option);
  const entries = [];
  let position = -1;
  for (const [key, raw] of Object.entries(options)) {
    if (lookup(key)?.option === option) {
      if (position < 0) position = entries.length;
    } else {
      entries.push([key, raw]);
    }
  }
  if (value !== undefined) entries.splice(position < 0 ? entries.length : position, 0, encodeField(option, value, previous));
  for (const key of Object.keys(options)) delete options[key];
  for (const [key, raw] of entries) options[key] = raw;
}

function scopeOptions(name = scope) {
  return name === DEFAULTS ? catalog.defaults : catalog.models[name];
}

function displayValue(option, value) {
  if (value === undefined || value === null || value === "") return option.defaultLabel || "not set";
  if (isBoolean(option)) return value ? "on" : "off";
  if (option.type === "ctx") return formatCtx(value);
  if (option.choiceLabels?.[value]) return `${value} (${option.choiceLabels[value]})`;
  return String(value);
}

function describeDefault(option) {
  if (option.defaultLabel) return option.defaultLabel;
  if (option.default === undefined) return "unset";
  return displayValue(option, option.default);
}

function fieldContext(option, name = scope) {
  const own = readField(scopeOptions(name), option);
  const inherited = name === DEFAULTS ? { present: false } : readField(catalog.defaults, option);
  const state = own.present ? "set" : inherited.present ? "inherited" : "default";
  const effective = own.present ? own.value : inherited.present ? inherited.value : option.default;
  return { own, inherited, state, effective };
}

function effective(key, name = scope) {
  return fieldContext(optionByKey.get(key), name).effective;
}

function validateValue(option, value) {
  if (value === undefined || value === "") return "";
  if (/[\r\n\0]/.test(value)) return "Line breaks are not allowed.";
  const number = Number(value);
  if (option.type === "int" || option.type === "ctx" || (option.type === "range" && option.integer)) {
    if (!/^-?\d+$/.test(value.trim())) return "Enter a whole number.";
  } else if (option.type === "float" || option.type === "range") {
    if (value.trim() === "" || Number.isNaN(number)) return "Enter a number.";
  }
  if (["int", "float", "range", "ctx"].includes(option.type)) {
    if (option.min !== undefined && number < option.min) return `Minimum is ${option.min}.`;
    if (option.max !== undefined && option.type !== "range" && number > option.max) return `Maximum is ${option.max}.`;
  }
  if (option.pattern && !option.pattern.test(value.trim())) return `Use ${option.patternHint}.`;
  if (option.type === "json") {
    try {
      const parsed = JSON.parse(value);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return "Must be a JSON object.";
    } catch {
      return "Not valid JSON.";
    }
  }
  return "";
}

function tabOf(key) {
  if (ESSENTIAL_KEYS.includes(key)) return "essentials";
  const option = optionByKey.get(key);
  return TABS.find((item) => item.groups?.includes(option.group))?.id || "advanced";
}

function isVisibleOption(option) {
  // Source keys live in the dedicated weights card for models.
  if (SOURCE_KEYS.includes(option.key)) return scope === DEFAULTS && readField(catalog.defaults, option).present;
  return true;
}

// ---------- editing ----------

function commit(option, value, { rerender = true } = {}) {
  const text = typeof value === "string" ? value.trim() : value;
  const cleared = text === undefined || text === "";
  const key = `${scope}|${option.key}`;
  const problem = cleared || isBoolean(option) ? "" : validateValue(option, text);
  if (problem) invalid.set(key, problem); else invalid.delete(key);
  if (!problem) writeField(scopeOptions(), option, cleared ? undefined : text);
  // Keep the user's invalid text visible instead of re-rendering the last valid value.
  if (rerender && !problem) refreshRow(option.key);
  else updateRowChrome(option);
  if (["flash-attn", "cache-type-k", "cache-type-v"].includes(option.key)) refreshRow("kv-cache");
  if (option.key === "parallel") refreshRow("ctx-size");
  afterEdit();
  if (option.key === "tags" && rerender && !problem) renderBody();
}

function afterEdit() {
  renderSidebar();
  renderSummary();
  renderTabs();
  renderEditorHead();
  renderOverviewModels();
  renderPreview();
  updateControls();
  updateDecisionWarnings();
}

function refreshRow(key) {
  const row = document.querySelector(`.row[data-key="${CSS.escape(key)}"]`);
  if (row) row.replaceWith(key === "kv-cache" ? renderKvRow() : renderRow(optionByKey.get(key)));
}

function badgeText(state) {
  if (state === "set") return scope === DEFAULTS ? "All models" : "Custom";
  if (state === "inherited") return "From all-models";
  return "";
}

function rowShell({ key, label, description, state, meta, control, problem, warning, onReset, resettable }) {
  return h("div", { class: "row", dataset: { key, state }, "data-invalid": problem ? "true" : undefined },
    h("div", { class: "row-info" },
      h("div", { class: "row-title" },
        h("label", { for: `opt-${key}` }, label),
        h("span", { class: "row-badge" }, badgeText(state))),
      h("p", { class: "row-desc" }, description),
      h("p", { class: "row-meta" }, meta)),
    h("div", { class: "row-control" }, control,
      h("p", { class: "row-warning", hidden: !warning }, warning || ""),
      h("p", { class: "opt-error", role: "alert", hidden: !problem }, problem || "")),
    h("button", { type: "button", class: "reset", title: "Reset to default", "aria-label": `Reset ${label}`,
      hidden: !resettable, onclick: onReset }, "↺"));
}

function metaText(option, context) {
  const parts = [];
  if (context.state === "set" && context.inherited.present) parts.push(`overrides all-models value ${displayValue(option, context.inherited.value)}`);
  else if (context.state === "inherited") parts.push(`using all-models value ${displayValue(option, context.inherited.value)}`);
  parts.push(`default ${describeDefault(option)}`);
  if (option.key === "ctx-size") {
    const slots = Number(effective("parallel"));
    if (slots > 1) parts.push(`shared by ${slots} parallel slots`);
  }
  return h("span", {}, parts.join(" · "), " · ",
    h("code", { title: option.aliases.length ? `Also accepted: ${option.aliases.join(", ")}` : undefined }, option.key));
}

function renderRow(option) {
  const context = fieldContext(option);
  const problem = invalid.get(`${scope}|${option.key}`);
  return rowShell({
    key: option.key, label: option.label, description: option.description, state: context.state,
    meta: metaText(option, context), control: buildControl(option, context), problem,
    resettable: context.own.present || Boolean(problem), onReset: () => commit(option, undefined),
  });
}

function updateRowChrome(option) {
  const row = document.querySelector(`.row[data-key="${CSS.escape(option.key)}"]`);
  if (!row) return;
  const context = fieldContext(option);
  const problem = invalid.get(`${scope}|${option.key}`);
  row.dataset.state = context.state;
  row.toggleAttribute("data-invalid", Boolean(problem));
  row.querySelector(".row-badge").textContent = badgeText(context.state);
  row.querySelector(".reset").hidden = !context.own.present && !problem;
  row.querySelector(".opt-error").hidden = !problem;
  row.querySelector(".opt-error").textContent = problem || "";
  row.querySelector(".row-meta").replaceChildren(metaText(option, context));
  if (option.type === "ctx" && !problem) row.querySelector(".ctx-readout").textContent = formatCtx(context.effective);
}

function segmented(label, items, onPick) {
  return h("div", { class: "segmented options", role: "radiogroup", "aria-label": label },
    items.map((item) => h("button", { type: "button", role: "radio", "aria-checked": String(item.active),
      class: item.active ? (item.ghost ? "ghost-active" : "active") : "", title: item.title, onclick: () => onPick(item) }, item.label)));
}

function textInput(option, context, extra = {}) {
  const numeric = ["int", "float", "ctx", "gpu"].includes(option.type);
  const input = h(option.type === "json" ? "textarea" : "input", {
    id: `opt-${option.key}`, type: option.type === "json" ? undefined : "text", value: context.own.present ? context.own.value : "",
    class: option.type === "path" || option.type === "json" ? "mono" : numeric ? "num" : "",
    inputMode: numeric ? (option.type === "float" ? "decimal" : "numeric") : undefined,
    placeholder: context.state === "default" ? (option.placeholder || displayValue(option, context.effective)) : displayValue(option, context.effective),
    autocomplete: "off", spellcheck: false, rows: option.type === "json" ? 2 : undefined, ...extra,
  });
  input.addEventListener("input", () => commit(option, input.value, { rerender: false }));
  input.addEventListener("change", () => commit(option, input.value));
  return input;
}

function buildControl(option, context) {
  const { own, effective: value } = context;
  const ghost = !own.present;
  const id = `opt-${option.key}`;

  if (isBoolean(option)) {
    const input = h("input", { id, type: "checkbox", class: `switch${ghost ? " ghost" : ""}`, role: "switch",
      checked: Boolean(value), onchange: () => commit(option, input.checked) });
    return h("label", { class: "control-row" }, input, h("span", { class: "switch-state" }, value ? "On" : "Off"));
  }

  if (option.type === "ctx") return ctxControl(option, context);
  if (option.type === "gpu") return gpuControl(option, context);

  let control;
  if (option.type === "select" && option.choices.length <= 4) {
    control = segmented(option.label, option.choices.map((choice) => ({
      value: choice, label: option.choiceLabels?.[choice] || choice, active: String(value) === choice, ghost,
    })), (item) => commit(option, own.present && own.value === item.value ? undefined : item.value));
  } else if (option.type === "select") {
    control = h("select", { id, onchange: () => commit(option, control.value || undefined) },
      h("option", { value: "" }, `Default (${context.inherited.present ? context.inherited.value : describeDefault(option)})`),
      option.choices.map((choice) => h("option", { value: choice }, option.choiceLabels?.[choice] || choice)));
    if (own.present && !option.choices.includes(own.value)) control.append(h("option", { value: own.value }, `${own.value} (custom)`));
    control.value = own.present ? own.value : "";
  } else if (option.type === "range") {
    const numeric = Number(value);
    const slider = h("input", { type: "range", class: ghost ? "ghost" : "", min: option.min, max: option.max, step: option.step,
      value: Number.isFinite(numeric) ? numeric : option.min, "aria-label": `${option.label} slider` });
    const number = textInput(option, context, { class: "num" });
    const paint = () => slider.style.setProperty("--fill", `${((Number(slider.value) - option.min) / (option.max - option.min)) * 100}%`);
    slider.addEventListener("input", () => {
      number.value = slider.value;
      slider.classList.remove("ghost");
      paint();
      commit(option, slider.value, { rerender: false });
    });
    slider.addEventListener("change", () => commit(option, slider.value));
    number.addEventListener("input", () => {
      if (number.value.trim() !== "" && Number.isFinite(Number(number.value))) {
        slider.value = number.value;
        paint();
      }
    });
    paint();
    control = h("div", { class: "range-row" }, slider, number);
  } else {
    const list = option.choices && option.type === "combo" ? `${id}-list` : undefined;
    const input = textInput(option, context, { list });
    control = h("div", { class: "input-row" }, input,
      list ? h("datalist", { id: list }, option.choices.map((choice) => h("option", { value: choice }))) : null,
      option.unit ? h("span", { class: "unit" }, option.unit) : null);
  }
  if (!option.suggestions?.length) return control;
  return h("div", { class: "control-stack" }, control,
    h("div", { class: "chips", "aria-label": "Suggested values" },
      option.suggestions.map((suggestion) => h("button", {
        type: "button", class: `chip${own.present && String(own.value) === suggestion.value ? " active" : ""}`,
        title: `Set ${option.key} = ${suggestion.value}`, onclick: () => commit(option, suggestion.value),
      }, suggestion.label || suggestion.value))));
}

function ctxControl(option, context) {
  const current = Number(context.effective) || 0;
  const nearest = CTX_STEPS.reduce((best, step, index) =>
    Math.abs(Math.log2((step || 1024) / (current || 1024))) < Math.abs(Math.log2((CTX_STEPS[best] || 1024) / (current || 1024))) ? index : best, 0);
  const readout = h("strong", { class: "ctx-readout" }, formatCtx(context.effective));
  const slider = h("input", { type: "range", min: 0, max: CTX_STEPS.length - 1, step: 1, value: nearest,
    class: context.own.present ? "" : "ghost", "aria-label": "Context length slider" });
  const input = textInput(option, context, { class: "num", placeholder: String(current || 0) });
  const paint = () => slider.style.setProperty("--fill", `${(Number(slider.value) / (CTX_STEPS.length - 1)) * 100}%`);
  slider.addEventListener("input", () => {
    const tokens = CTX_STEPS[Number(slider.value)];
    input.value = String(tokens);
    readout.textContent = formatCtx(tokens);
    slider.classList.remove("ghost");
    paint();
    commit(option, String(tokens), { rerender: false });
  });
  slider.addEventListener("change", () => commit(option, String(CTX_STEPS[Number(slider.value)])));
  paint();
  return h("div", { class: "ctx-control" },
    h("div", { class: "ctx-top" }, readout, h("span", { class: "input-row" }, input, h("span", { class: "unit" }, "tokens"))),
    slider,
    h("div", { class: "ctx-scale", "aria-hidden": "true" }, [[0, "Model"], [3, "8K"], [5, "32K"], [7, "128K"], [8, "256K"]].map(([index, text]) =>
      h("span", { style: `left:${(index / (CTX_STEPS.length - 1)) * 100}%` }, text))));
}

function gpuControl(option, context) {
  const raw = String(context.effective ?? "auto").toLowerCase();
  const mode = raw === "auto" ? "auto" : raw === "all" ? "all" : raw === "0" ? "cpu" : "layers";
  const ghost = !context.own.present;
  const input = textInput(option, context, { class: "num", placeholder: mode === "layers" ? String(context.effective) : "e.g. 40" });
  const custom = h("span", { class: "input-row gpu-layers", hidden: mode !== "layers" }, input, h("span", { class: "unit" }, "layers"));
  const items = [
    { value: "all", label: "All on GPU", title: "Fastest, needs enough VRAM" },
    { value: "auto", label: "Auto-fit", title: "Offload as much as fits" },
    { value: "cpu", label: "CPU only", title: "No GPU offload" },
    { value: "layers", label: "Layers…", title: "Choose a layer count" },
  ].map((item) => ({ ...item, active: item.value === mode, ghost }));
  return h("div", { class: "control-stack" }, segmented(option.label, items, (item) => {
    if (item.value === "layers") {
      custom.hidden = false;
      input.focus();
      return;
    }
    commit(option, { all: "all", auto: "auto", cpu: "0" }[item.value]);
  }), custom);
}

function renderKvRow() {
  const k = optionByKey.get("cache-type-k");
  const v = optionByKey.get("cache-type-v");
  const kc = fieldContext(k);
  const vc = fieldContext(v);
  const own = kc.own.present || vc.own.present;
  const state = own ? "set" : kc.state === "inherited" || vc.state === "inherited" ? "inherited" : "default";
  const same = kc.effective === vc.effective;
  const set = (value) => {
    writeField(scopeOptions(), k, value);
    writeField(scopeOptions(), v, value);
    refreshRow("kv-cache");
    refreshRow("cache-type-k");
    refreshRow("cache-type-v");
    afterEdit();
  };
  const presets = segmented("KV cache precision", KV_PRESETS.map((preset) => ({
    ...preset, title: preset.hint, active: same && kc.effective === preset.value, ghost: !own,
  })), (item) => set(item.value));
  const other = h("select", { id: "opt-kv-cache", "aria-label": "Other KV cache type", onchange: () => other.value && set(other.value) },
    h("option", { value: "" }, "Other…"), KV_TYPES.map((type) => h("option", { value: type }, type)));
  other.value = same && !KV_PRESETS.some((preset) => preset.value === kc.effective) ? kc.effective : "";
  const quantizedV = !["f16", "bf16", "f32"].includes(vc.effective);
  const warning = quantizedV && effective("flash-attn") === "off"
    ? "A quantized V cache needs Flash Attention set to on or auto." : "";
  const hint = KV_PRESETS.find((preset) => same && preset.value === kc.effective)?.hint;
  return rowShell({
    key: "kv-cache", label: "KV cache precision", state,
    description: "Memory used to remember the context. Balanced (q8_0) halves it with negligible quality loss; Compact saves more but can hurt long-context accuracy.",
    meta: h("span", {}, same ? `${hint || kc.effective} · ` : `mixed: K ${kc.effective}, V ${vc.effective} (set separately under Memory & speed) · `,
      "default f16 · ", h("code", {}, "cache-type-k + cache-type-v")),
    control: h("div", { class: "control-stack inline" }, presets, other), warning,
    resettable: own, onReset: () => set(undefined),
  });
}

function sourceMode() {
  if (sourceModes.has(scope)) return sourceModes.get(scope);
  const merged = { ...catalog.defaults, ...scopeOptions() };
  return ["hf-repo", "hf-file"].some((key) => readField(merged, optionByKey.get(key)).present) ? "hf" : "local";
}

function switchSource(mode) {
  if (mode === sourceMode()) return;
  const options = scopeOptions();
  const stash = sourceStash.get(scope) || {};
  const leaving = mode === "hf" ? ["model"] : ["hf-repo", "hf-file"];
  const entering = mode === "hf" ? ["hf-repo", "hf-file"] : ["model"];
  for (const key of leaving) {
    const field = readField(options, optionByKey.get(key));
    if (field.present) {
      stash[key] = field.value;
      writeField(options, optionByKey.get(key), undefined);
    }
    invalid.delete(`${scope}|${key}`);
  }
  for (const key of entering) {
    const option = optionByKey.get(key);
    if (stash[key] !== undefined && !readField(options, option).present) writeField(options, option, stash[key]);
    delete stash[key];
  }
  sourceStash.set(scope, stash);
  sourceModes.set(scope, mode);
  renderBody();
  afterEdit();
}

function renderSourceCard() {
  const mode = sourceMode();
  const field = (key, label, hint) => {
    const option = optionByKey.get(key);
    const context = fieldContext(option);
    const input = textInput(option, context, { class: "mono" });
    input.addEventListener("input", () => updateSourceWarning());
    return h("label", { class: "source-field" }, h("span", {}, label, hint ? h("small", {}, ` ${hint}`) : null), input);
  };
  const missing = h("p", { class: "row-warning source-warning" });
  const card = h("div", { class: "source-card", dataset: { key: "source" } },
    h("div", { class: "source-head" },
      h("div", {}, h("h3", {}, "Model weights"),
        h("p", { class: "row-desc" }, mode === "hf"
          ? "llama.cpp downloads the repo on first use and keeps it in its cache."
          : "A .gguf file on this machine. For split models, choose the first part.")),
      segmented("Model source type", [
        { value: "local", label: "Local file", active: mode === "local" },
        { value: "hf", label: "Hugging Face", active: mode === "hf" },
      ], (item) => switchSource(item.value))),
    mode === "hf"
      ? h("div", { class: "source-grid" }, field("hf-repo", "Repository", "user/model:quant"), field("hf-file", "Exact file", "optional"))
      : field("model", "File path"),
    missing, renderPurposeControl());
  function updateSourceWarning() {
    const merged = { ...catalog.defaults, ...scopeOptions() };
    const has = SOURCE_KEYS.some((key) => readField(merged, optionByKey.get(key)).present);
    missing.textContent = has ? "" : "Required: the router cannot start this model without a file or repo.";
    missing.hidden = has;
  }
  updateSourceWarning();
  return card;
}

function matchesQuery(text, query) {
  return text.some((item) => item && item.toLowerCase().includes(query));
}

function rowsFor(tabId, query) {
  const rows = [];
  const includeKv = (tabId === "essentials" || !tabId) && (!query || matchesQuery(["kv cache precision memory", "cache-type"], query));
  for (const option of OPTIONS) {
    if (!isVisibleOption(option)) continue;
    if (tabId && tabOf(option.key) !== tabId) continue;
    if (query && !matchesQuery([option.label, option.key, option.description, ...option.aliases, ...(option.off || [])], query)) continue;
    if (option.key === "flash-attn" && includeKv) rows.push(renderKvRow());
    rows.push(renderRow(option));
  }
  if (includeKv && !rows.some((row) => row.dataset.key === "kv-cache")) rows.push(renderKvRow());
  return rows;
}

function groupedRows(tabId, query) {
  const fragment = [];
  const tabInfo = TABS.find((item) => item.id === tabId);
  if (tabId === "essentials" || !tabInfo.groups) {
    const rows = tabId === "essentials" ? essentialRows(query) : rowsFor(tabId, query);
    if (rows.length) fragment.push(h("div", { class: "rows" }, rows));
    return fragment;
  }
  for (const groupId of tabInfo.groups) {
    const group = OPTION_GROUPS.find((item) => item.id === groupId);
    const rows = rowsFor(tabId, query).filter((row) => row.dataset.key === "kv-cache" || optionByKey.get(row.dataset.key)?.group === groupId);
    if (!rows.length) continue;
    fragment.push(h("section", { class: "row-group" },
      tabInfo.groups.length > 1 ? h("h3", { class: "row-group-title" }, group.title, h("span", {}, group.blurb)) : null,
      h("div", { class: "rows" }, rows)));
  }
  return fragment;
}

function essentialRows(query) {
  const rows = [];
  for (const key of ESSENTIAL_KEYS) {
    if (key === "kv-cache") {
      if (!query || matchesQuery(["kv cache precision memory"], query)) rows.push(renderKvRow());
      continue;
    }
    const option = optionByKey.get(key);
    if (isDecisionModel() && ["sampling", "chat", "speculative"].includes(option.group)) continue;
    if (query && !matchesQuery([option.label, option.key, option.description], query)) continue;
    rows.push(renderRow(option));
  }
  return rows;
}

function renderBody() {
  const container = $("option-editor");
  container.replaceChildren();
  if (!catalog || !scope) return;
  renderedDecision = isDecisionModel();
  if (tab === "decision" && !renderedDecision) tab = "essentials";
  const query = $("option-search").value.trim().toLowerCase();
  $("option-editor").hidden = textMode;
  $("text-editor").hidden = !textMode;
  if (textMode) return;
  if (query) {
    $("tab-blurb").textContent = `Results for “${$("option-search").value.trim()}” across all sections`;
    let found = 0;
    for (const item of TABS) {
      if (item.decisionOnly) continue;
      const rows = item.id === "essentials" ? essentialRows(query) : rowsFor(item.id, query);
      if (!rows.length) continue;
      found += rows.length;
      container.append(h("section", { class: "row-group" },
        h("h3", { class: "row-group-title" }, item.title), h("div", { class: "rows" }, rows)));
    }
    const custom = customKeys().filter((key) => key.toLowerCase().includes(query));
    if (custom.length) container.append(renderCustomSection(query));
    if (!found && !custom.length) container.append(h("p", { class: "empty-state" }, "No settings match. Add it as a custom flag under Advanced → Other llama.cpp flags."));
    return;
  }
  const info = TABS.find((item) => item.id === tab);
  $("tab-blurb").textContent = scope === DEFAULTS && tab === "essentials"
    ? "Values set here apply to every model unless the model sets its own."
    : info.blurb;
  if (tab === "essentials" && scope !== DEFAULTS) container.append(renderSourceCard());
  if (tab === "decision") {
    container.append(renderDecisionGuide(),
      h("div", { class: "rows" }, DECISION_SETTINGS.map((key) => renderRow(optionByKey.get(key)))));
    updateDecisionWarnings();
    return;
  }
  if (tab === "essentials" && isDecisionModel()) {
    container.append(h("div", { class: "decision-summary" },
      h("p", { class: "row-desc" }, "Native decision model: use /v1/systemone. Generation sampling, reasoning and speculative decoding do not drive its probability outputs."),
      h("button", { type: "button", class: "secondary", onclick: () => selectTab("decision") }, "Decision configuration & API")));
  }
  container.append(...groupedRows(tab, ""));
  if (tab === "advanced") {
    container.append(renderCustomSection(""));
    container.append(h("div", { class: "text-switch" },
      h("div", {}, h("strong", {}, "Prefer editing text?"), h("p", { class: "row-desc" }, "Edit this section as raw key = value lines, the same format as models.ini.")),
      h("button", { type: "button", class: "secondary", onclick: () => setTextMode(true) }, "Edit as INI text")));
  }
}

function customKeys() {
  return Object.keys(scopeOptions()).filter((key) => !lookup(key));
}

function renderCustomSection(query) {
  const rows = h("div", { class: "custom-rows" });
  for (const key of customKeys()) {
    if (query && !key.toLowerCase().includes(query)) continue;
    const input = h("input", { value: scopeOptions()[key], spellcheck: false, autocomplete: "off", "aria-label": `Value for ${key}` });
    input.addEventListener("input", () => {
      if (input.value.trim() && !/[\r\n\0]/.test(input.value)) {
        scopeOptions()[key] = input.value.trim();
        afterEdit();
      }
    });
    rows.append(h("div", { class: "custom-row" }, h("code", {}, key), input,
      h("button", { type: "button", class: "ghost-button danger", "aria-label": `Remove ${key}`, onclick: () => {
        delete scopeOptions()[key];
        renderBody();
        afterEdit();
      } }, "Remove")));
  }
  const keyInput = h("input", { placeholder: "flag-name", spellcheck: false, autocomplete: "off", "aria-label": "New flag name" });
  const valueInput = h("input", { placeholder: "value (true for on/off flags)", spellcheck: false, autocomplete: "off", "aria-label": "New flag value" });
  const add = h("form", { class: "custom-row add", onsubmit: (event) => {
    event.preventDefault();
    try {
      const key = keyInput.value.trim().replace(/^-+/, "");
      const value = valueInput.value.trim();
      const canonical = key.toLowerCase().replace(/^llama_arg_/, "").replaceAll("_", "-");
      if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(key) || RESERVED_OPTIONS.has(canonical)) throw new Error(`${key || "That flag"} is invalid or controlled by the manager.`);
      if (!value || /[\r\n\0]/.test(value)) throw new Error("Enter a single-line value (use true for on/off flags).");
      if (Object.keys(scopeOptions()).some((existing) => existing.toLowerCase() === key.toLowerCase())) throw new Error(`${key} is already set.`);
      const known = lookup(key);
      if (known) {
        const option = known.option;
        writeField(scopeOptions(), option, isBoolean(option) ? (known.kind === "on") === !FALSY.has(value.toLowerCase()) : value);
        tab = tabOf(option.key);
        renderBody();
        afterEdit();
        focusRow(option.key);
        toast(`${key} has its own control in “${TABS.find((item) => item.id === tab).title}”. Set it there.`);
        return;
      }
      scopeOptions()[key] = value;
      renderBody();
      afterEdit();
    } catch (error) {
      toast(error.message, "error");
    }
  } }, keyInput, valueInput, h("button", { type: "submit", class: "secondary" }, "Add flag"));
  return h("section", { class: "row-group" },
    h("h3", { class: "row-group-title" }, "Other llama.cpp flags",
      h("span", {}, "Anything without its own control. Write the flag without leading dashes; it is passed to llama.cpp as-is.")),
    rows, add);
}

function focusRow(key) {
  requestAnimationFrame(() => {
    const row = document.querySelector(`.row[data-key="${CSS.escape(key)}"], [data-key="${CSS.escape(key)}"]`);
    if (!row) return;
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.remove("flash");
    void row.offsetWidth;
    row.classList.add("flash");
  });
}

// ---------- tabs, header, summary ----------

function tabCount(tabId) {
  const options = scopeOptions();
  if (tabId === "decision") return DECISION_SETTINGS.filter((key) => readField(options, optionByKey.get(key)).present).length;
  if (tabId === "essentials") {
    return ESSENTIAL_KEYS.filter((key) => key === "kv-cache"
      ? ["cache-type-k", "cache-type-v"].some((item) => readField(options, optionByKey.get(item)).present)
      : readField(options, optionByKey.get(key)).present).length;
  }
  const count = OPTIONS.filter((option) => !SOURCE_KEYS.includes(option.key) && tabOf(option.key) === tabId
    && readField(options, option).present).length;
  return tabId === "advanced" ? count + customKeys().length : count;
}

function renderTabs() {
  const container = $("section-tabs");
  container.replaceChildren();
  if (!catalog || !scope) return;
  if (tab === "decision" && !isDecisionModel()) tab = "essentials";
  const searching = Boolean($("option-search").value.trim());
  for (const item of TABS) {
    if (item.decisionOnly && !isDecisionModel()) continue;
    const count = tabCount(item.id);
    container.append(h("button", { type: "button", role: "tab", "aria-selected": String(!searching && !textMode && item.id === tab),
      onclick: () => selectTab(item.id) }, item.title, count ? h("b", { title: `${count} customised` }, String(count)) : null));
  }
}

function selectTab(id) {
  if (!leaveTextMode()) return;
  tab = id;
  $("option-search").value = "";
  renderTabs();
  renderBody();
}

function modelSource(name) {
  const merged = { ...catalog.defaults, ...catalog.models[name] };
  const local = ["model", "m", "LLAMA_ARG_MODEL"].map((key) => merged[key]).find(Boolean);
  const hf = ["hf-repo", "hf", "hfr", "LLAMA_ARG_HF_REPO"].map((key) => merged[key]).find(Boolean);
  if (local) return { text: local.split(/[\\/]/).pop(), full: local, kind: "local" };
  if (hf) return { text: hf, full: hf, kind: "hf" };
  return { text: "Needs a model file", kind: "missing" };
}

function liveState(name) {
  const model = liveModels.find((item) => item.id === name);
  const running = currentPid !== null && workspace?.mode === "router" && currentState === "running";
  const value = model?.status?.value || (running ? "unloaded" : "offline");
  return { value, failed: Boolean(model?.status?.failed), exit: model?.status?.exit_code, running };
}

function liveLabel(live) {
  if (live.failed) return `Failed (exit ${live.exit})`;
  return { loaded: "Loaded", loading: "Loading…", unloaded: "Not loaded", offline: "Router stopped" }[live.value] || live.value;
}

function isModified(name) {
  const saved = JSON.parse(savedSnapshot || "{}");
  if (name === DEFAULTS) return JSON.stringify(saved.defaults) !== JSON.stringify(catalog.defaults);
  return JSON.stringify(saved.models?.[name]) !== JSON.stringify(catalog.models[name])
    || saved.enabled?.includes(name) !== catalog.enabled.includes(name);
}

function facts(name) {
  const list = [];
  const decision = isDecisionModel(name);
  if (decision) list.push({ key: "decision-api", text: "Decision · System One" });
  const ctx = effective("ctx-size", name);
  list.push({ key: "ctx-size", text: `${formatCtx(ctx)}${Number(ctx) > 0 ? " context" : " context"}` });
  const gpu = String(effective("n-gpu-layers", name)).toLowerCase();
  list.push({ key: "n-gpu-layers", text: gpu === "0" ? "CPU only" : gpu === "all" ? "All on GPU" : gpu === "auto" ? "GPU auto-fit" : `${gpu} GPU layers` });
  const k = effective("cache-type-k", name);
  const v = effective("cache-type-v", name);
  if (k !== "f16" || v !== "f16") list.push({ key: "kv-cache", text: k === v ? `KV ${k}` : `KV ${k}/${v}` });
  const slots = effective("parallel", name);
  if (slots !== "-1") list.push({ key: "parallel", text: `${slots} slot${slots === "1" ? "" : "s"}` });
  const spec = effective("spec-type", name);
  if (!decision && spec && spec !== "none") list.push({ key: "spec-type", text: `⚡ ${spec}` });
  const temp = fieldContext(optionByKey.get("temp"), name);
  if (!decision && temp.state !== "default") list.push({ key: "temp", text: `temp ${temp.effective}` });
  if (effective("mmproj-auto", name) === false) list.push({ key: "mmproj-auto", text: "text-only" });
  const reasoning = fieldContext(optionByKey.get("reasoning"), name);
  if (!decision && reasoning.state !== "default") list.push({ key: "reasoning", text: `reasoning ${reasoning.effective}` });
  return list;
}

function renderSummary() {
  const container = $("summary-chips");
  container.replaceChildren();
  if (!catalog || !scope) return;
  const items = scope === DEFAULTS
    ? Object.keys(catalog.defaults).map((key) => {
      const option = lookup(key)?.option;
      return { key: option?.key, text: `${option?.label || key}: ${option ? displayValue(option, readField(catalog.defaults, option).value) : catalog.defaults[key]}` };
    })
    : facts(scope);
  if (!items.length) {
    container.append(h("span", { class: "muted" }, "Nothing shared yet. Set a value here to apply it to every model."));
    return;
  }
  for (const item of items) {
    container.append(h("button", { type: "button", class: "fact", disabled: !item.key, onclick: () => {
      const key = item.key === "kv-cache" ? "kv-cache" : item.key;
      tab = key === "decision-api" ? "decision" : key === "kv-cache" ? "essentials" : tabOf(key);
      $("option-search").value = "";
      if (!leaveTextMode()) return;
      renderTabs();
      renderBody();
      if (key !== "decision-api") focusRow(key);
    } }, item.text));
  }
}

function renderEditorHead() {
  if (!catalog || !scope) return;
  const isModel = scope !== DEFAULTS;
  $("editor-kicker").textContent = isModel ? (isDecisionModel() ? "DECISION MODEL" : "MODEL") : "SHARED DEFAULTS";
  $("editor-name").textContent = isModel ? scope : "All models";
  if (isModel) {
    const source = modelSource(scope);
    $("editor-sub").replaceChildren(source.kind === "missing"
      ? "Choose a model file or Hugging Face repo below to finish setting it up."
      : h("span", {}, "Request it as ", h("code", {}, scope),
        isDecisionModel() ? [" via ", h("code", {}, "/v1/systemone")] : null,
        " · ", source.kind === "hf" ? "🤗 " : "", source.full));
  } else {
    $("editor-sub").textContent = "Settings every model inherits. A model's own value always wins.";
  }
  $("enabled-wrap").hidden = !isModel;
  $("model-enabled").checked = isModel && catalog.enabled.includes(scope);
  $("more-menu").hidden = !isModel;
  const live = isModel ? liveState(scope) : null;
  $("editor-live").hidden = !isModel;
  $("load-model").hidden = !isModel || !live?.running || live.value === "loaded" || live.value === "loading";
  $("unload-model").hidden = !isModel || !live?.running || !(live.value === "loaded" || live.value === "loading");
  if (live) {
    $("editor-live").textContent = liveLabel(live);
    $("editor-live").dataset.value = live.failed ? "failed" : live.value;
  }
}

// ---------- sidebar & overview ----------

function renderSidebar() {
  if (!catalog) return;
  const defaultsCount = Object.keys(catalog.defaults).length;
  $("defaults-entry").replaceChildren(h("button", {
    type: "button", class: `scope-item defaults${scope === DEFAULTS ? " selected" : ""}`, "aria-pressed": String(scope === DEFAULTS),
    onclick: () => selectScope(DEFAULTS),
  }, h("span", { class: "scope-icon", "aria-hidden": "true" }, "✱"),
  h("span", { class: "scope-text" }, h("strong", {}, "Shared defaults"), h("small", {}, `${defaultsCount} setting${defaultsCount === 1 ? "" : "s"} for every model`)),
  isModified(DEFAULTS) ? h("span", { class: "dirty-dot", title: "Unsaved changes" }) : null));

  const list = $("model-sidebar");
  list.replaceChildren();
  const names = Object.keys(catalog.models);
  $("model-count").textContent = String(names.length);
  $("nav-model-count").textContent = names.length ? String(names.length) : "";
  for (const name of names) {
    const source = modelSource(name);
    const live = liveState(name);
    const enabled = catalog.enabled.includes(name);
    list.append(h("li", { role: "option", "aria-selected": String(scope === name), class: `model-item${scope === name ? " selected" : ""}${enabled ? "" : " excluded"}` },
      h("button", { type: "button", class: "scope-item", onclick: () => selectScope(name) },
        h("span", { class: "state-dot", dataset: { model: name, value: live.failed ? "failed" : live.value }, title: liveLabel(live) }),
        h("span", { class: "scope-text" },
          h("strong", {}, name),
          h("small", { class: `source-${source.kind}`, title: source.full || source.text }, source.kind === "hf" ? `🤗 ${source.text}` : source.text)),
        enabled ? null : h("span", { class: "off-tag", title: "Not available in the router" }, "off"),
        isModified(name) ? h("span", { class: "dirty-dot", title: "Unsaved changes" }) : null)));
  }
  if (!names.length) list.append(h("li", { class: "empty-state small" }, "No models yet. Click “New model” to add one."));
}

function renderOverviewModels() {
  if (!catalog) return;
  const container = $("overview-models");
  container.replaceChildren();
  const names = Object.keys(catalog.models);
  if (!names.length) {
    container.append(h("div", { class: "empty-state" }, h("p", {}, "No models yet."),
      h("button", { type: "button", class: "primary", onclick: () => openNewModel() }, "+ Add your first model")));
  }
  const dirty = isDirty();
  for (const name of names) {
    const live = liveState(name);
    const enabled = catalog.enabled.includes(name);
    const source = modelSource(name);
    const actions = [h("button", { type: "button", class: "ghost-button", onclick: () => { selectScope(name); go("models"); } }, "Configure")];
    if (live.running && enabled && !dirty && !pendingApply) {
      const loaded = live.value === "loaded" || live.value === "loading";
      actions.unshift(h("button", { type: "button", class: "secondary", onclick: () => routerAction(loaded ? "unload" : "load", name) }, loaded ? "Unload" : "Load"));
    }
    container.append(h("article", { class: `tile${enabled ? "" : " excluded"}`, dataset: { value: live.failed ? "failed" : live.value } },
      h("div", { class: "tile-top" },
        h("span", { class: "tile-state" }, h("span", { class: "state-dot", dataset: { value: live.failed ? "failed" : live.value } }), enabled ? liveLabel(live) : "Not in router"),
        isModified(name) ? h("span", { class: "dirty-dot", title: "Unsaved changes" }) : null),
      h("h3", {}, name),
      h("p", { class: `tile-source source-${source.kind}`, title: source.full || "" }, source.kind === "hf" ? `🤗 ${source.text}` : source.text),
      h("div", { class: "tile-facts" }, facts(name).slice(0, 4).map((fact) => h("span", {}, fact.text))),
      h("div", { class: "tile-actions" }, actions)));
  }
  const hint = $("setup-hint");
  const steps = [];
  if (detection && !detection.resolved) {
    steps.push(["router", detection.automatic ? "Install llama.cpp or tell us where it is" : "Fix the llama.cpp path"]);
  }
  if (!names.length) steps.push(["new", "Add a model"]);
  hint.hidden = !steps.length;
  hint.replaceChildren(...(steps.length ? [h("strong", {}, "Getting started: "), ...steps.flatMap(([target, text], index) => [
    index ? " then " : "",
    h("button", { type: "button", class: "link-button", onclick: () => (target === "new" ? openNewModel() : go("router")) }, text),
  ])] : []));
}

function iniText() {
  const section = (name, options) => [`[${name}]`, ...Object.entries(options).map(([key, value]) => `${key} = ${value}`)].join("\n");
  return ["version = 1", section("*", catalog.defaults), ...catalog.enabled.map((name) => section(name, catalog.models[name]))].join("\n\n");
}

function renderPreview() {
  if (catalog && $("ini-preview").closest("details").open) $("ini-preview").textContent = iniText();
}

function renderAll() {
  if (catalog && scope !== DEFAULTS && !Object.hasOwn(catalog.models, scope)) scope = Object.keys(catalog.models)[0] || DEFAULTS;
  renderSidebar();
  renderEditorHead();
  renderSummary();
  renderTabs();
  if (textMode) $("raw-options").value = optionsText(scopeOptions());
  renderBody();
  renderOverviewModels();
  renderPreview();
  updateControls();
}

// ---------- text mode ----------

function optionsText(options) {
  return Object.entries(options).map(([key, value]) => `${key} = ${value}`).join("\n");
}

function parseOptions(text) {
  const options = Object.create(null);
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim() || /^[#;]/.test(line.trim())) continue;
    const split = line.indexOf("=");
    if (split < 1) throw new Error(`Use one key = value per line: "${line.trim()}"`);
    const key = line.slice(0, split).trim();
    if (Object.hasOwn(options, key)) throw new Error(`Duplicate option: ${key}`);
    const option = lookup(key)?.option;
    const clash = option && Object.keys(options).find((existing) => lookup(existing)?.option === option);
    if (clash) throw new Error(`${clash} and ${key} set the same option (${option.key}); keep one.`);
    const value = line.slice(split + 1).trim();
    if (!value) throw new Error(`Option ${key} needs a value (use true for flags).`);
    options[key] = value;
  }
  return { ...options };
}

function captureText() {
  if (!textMode || !textDirty) return;
  const parsed = parseOptions($("raw-options").value);
  if (scope === DEFAULTS) catalog.defaults = parsed;
  else catalog.models[scope] = parsed;
  for (const key of [...invalid.keys()]) if (key.startsWith(`${scope}|`)) invalid.delete(key);
  sourceModes.delete(scope);
  textDirty = false;
}

function leaveTextMode() {
  try {
    captureText();
  } catch (error) {
    toast(error.message, "error");
    return false;
  }
  if (textMode) {
    textMode = false;
    renderAll();
  }
  return true;
}

function setTextMode(on) {
  if (!on) {
    leaveTextMode();
    return;
  }
  textMode = true;
  textDirty = false;
  $("raw-options").value = optionsText(scopeOptions());
  renderTabs();
  renderBody();
  $("raw-options").focus();
}

// ---------- catalog actions ----------

function selectScope(name) {
  if (!leaveTextMode()) return;
  scope = name;
  $("option-search").value = "";
  renderAll();
  $("more-menu").open = false;
}

function setEnabled(name, enabled) {
  catalog.enabled = enabled ? [...new Set([...catalog.enabled, name])] : catalog.enabled.filter((id) => id !== name);
  afterEdit();
}

function isDirty() {
  return Boolean(catalog) && (textDirty || JSON.stringify(catalog) !== savedSnapshot);
}

function validateModelName(name, except) {
  if (!name || name === "*" || name === "DEFAULT" || /[[\]\r\n\0]/.test(name)) throw new Error("Enter a valid model ID (no brackets or line breaks).");
  if (name !== except && Object.hasOwn(catalog.models, name)) throw new Error(`A model named ${name} already exists.`);
}

function updateControls() {
  const active = currentPid !== null;
  $("start").disabled = !connected || !token || !workspace || busy || active;
  $("stop").disabled = !connected || busy || !active || currentState === "stopping";
  $("start").hidden = active;
  $("stop").hidden = !active;
  $("save").disabled = !connected || !token || busy || active;
  $("executable").disabled = !connected || busy || active;
  $("port").disabled = !connected || busy || active;
  for (const button of document.querySelectorAll("[data-exe-mode]")) button.disabled = !connected || busy || active;
  const unavailable = !connected || busy || !catalog || !token;
  $("catalog-fieldset").disabled = unavailable;
  for (const element of $("import-form").elements) element.disabled = unavailable;
  for (const element of [$("max-loaded"), $("autoload"), ...document.querySelectorAll("#router-capacity .step")]) {
    element.disabled = unavailable || active;
  }
  const dirty = isDirty();
  const isModel = scope !== DEFAULTS && catalog && Object.hasOwn(catalog.models, scope);
  for (const id of ["delete-model", "clone-model", "rename-model"]) $(id).disabled = unavailable || !isModel;
  const routerUp = active && workspace?.mode === "router";
  $("apply-catalog").disabled = unavailable || !routerUp || dirty || !pendingApply;
  $("apply-catalog").hidden = !routerUp || dirty || !pendingApply;
  $("save-catalog").disabled = unavailable || !dirty;
  $("save-catalog").hidden = !dirty;
  $("discard-catalog").disabled = unavailable || !dirty;
  $("discard-catalog").hidden = !dirty;
  for (const id of ["load-model", "unload-model"]) {
    $(id).disabled = unavailable || !routerUp || dirty || pendingApply || !isModel || !catalog.enabled.includes(scope);
  }
  const state = !catalog ? "loading" : dirty ? "dirty" : pendingApply ? "pending" : "clean";
  const changed = catalog && dirty ? changedSections() : 0;
  const messages = {
    loading: "Loading presets...",
    dirty: invalid.size
      ? `${invalid.size} value${invalid.size === 1 ? " needs" : "s need"} fixing before you can save`
      : `Unsaved changes${changed ? ` in ${changed} section${changed === 1 ? "" : "s"}` : ""}`,
    pending: routerUp ? "Saved. Apply to reload the running router when clients are idle." : "Saved. Takes effect when the router starts.",
    clean: "All changes saved.",
  };
  $("catalog-status").textContent = messages[state];
  $("catalog-status").dataset.state = state;
  $("save-message").textContent = messages[state];
  const bar = document.querySelector(".save-bar");
  bar.dataset.state = state;
  bar.classList.toggle("visible", state === "dirty" || (state === "pending" && routerUp));
}

function changedSections() {
  const saved = JSON.parse(savedSnapshot || "{}");
  let count = isModified(DEFAULTS) ? 1 : 0;
  const names = new Set([...Object.keys(saved.models || {}), ...Object.keys(catalog.models)]);
  for (const name of names) {
    if (!Object.hasOwn(catalog.models, name) || !Object.hasOwn(saved.models || {}, name) || isModified(name)) count += 1;
  }
  if (saved.max_loaded !== catalog.max_loaded || saved.autoload !== catalog.autoload) count += 1;
  return count;
}

// ---------- views ----------

function go(view) {
  if (location.hash !== `#${view}`) location.hash = view;
  else renderView();
}

function renderView() {
  const view = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
  for (const name of VIEWS) $(`view-${name}`).hidden = name !== view;
  for (const link of document.querySelectorAll(".tabs a")) {
    link.classList.toggle("active", link.dataset.view === view);
    if (link.dataset.view === view) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  if (view === "observability") void refreshObservability();
  window.scrollTo({ top: 0 });
}

// ---------- status & polling ----------

function renderStatus(status) {
  currentState = status.state;
  currentPid = status.pid;
  pendingApply = status.pending_apply;
  $("state").textContent = status.state === "running" ? "Router online" : status.state;
  document.querySelector(".status-pill").dataset.state = status.state;
  $("health").textContent = status.health_detail;
  $("pid").textContent = status.pid ? `PID ${status.pid}` : "Not running";
  showError("server-error", status.error);
  updateControls();
}

function renderWorkspace(data) {
  applySettings(data.settings);
  if (data.api_url) $("api-url").value = data.api_url;
  if (data.catalog) {
    catalog = structuredClone(data.catalog);
    savedSnapshot = JSON.stringify(catalog);
    pendingApply = data.pending_apply;
    textDirty = false;
    invalid.clear();
    sourceStash.clear();
    sourceModes.clear();
    if (!scope) scope = Object.keys(catalog.models)[0] || DEFAULTS;
    $("preset-source").value = catalog.source;
    $("max-loaded").value = catalog.max_loaded;
    $("autoload").checked = catalog.autoload;
    renderAll();
  }
}

function updateModelStates() {
  if (!catalog) return;
  for (const dot of document.querySelectorAll(".model-sidebar .state-dot")) {
    const live = liveState(dot.dataset.model);
    dot.dataset.value = live.failed ? "failed" : live.value;
    dot.title = liveLabel(live);
  }
  renderEditorHead();
  renderOverviewModels();
  if (renderedDecision !== isDecisionModel()) {
    renderSummary();
    renderTabs();
    renderBody();
  }
}

async function refresh() {
  if (polling) return polling;
  polling = (async () => {
    try {
      const status = await request("/api/status");
      connected = true;
      showError("connection-error", "");
      renderStatus(status);
      if (status.mode === "router" && status.state === "running") {
        try {
          liveModels = (await request("/api/models")).data;
          $("router-note").textContent = "Live state from llama.cpp. Models load and swap automatically on request.";
        } catch (error) {
          liveModels = [];
          $("router-note").textContent = `Router status unavailable: ${error.message}`;
        }
      } else {
        liveModels = [];
        $("router-note").textContent = "Start the router to see which models are loaded.";
      }
      updateModelStates();
      await Promise.all([
        $("logs-dialog").open ? refreshLogs() : Promise.resolve(),
        !$("view-observability").hidden ? refreshObservability() : Promise.resolve(),
      ]);
    } catch (error) {
      connected = false;
      $("state").textContent = "Disconnected";
      document.querySelector(".status-pill").dataset.state = "disconnected";
      showError("connection-error", `Cannot reach dashboard: ${error.message}`);
      updateControls();
    }
  })();
  try {
    await polling;
  } finally {
    polling = null;
  }
}

async function act(action) {
  busy = true;
  $("notice").textContent = "";
  showError("action-error", "");
  updateControls();
  try {
    await action();
  } catch (error) {
    showError("action-error", error.message);
    toast(error.message, "error");
  } finally {
    await refresh();
    busy = false;
    updateControls();
  }
}

function isCommandName(value) {
  return Boolean(value) && !/[\\/:]/.test(value) && value !== "." && value !== "..";
}

function executableValue() {
  return exeMode === "auto" ? DEFAULT_EXECUTABLE : $("executable").value.trim().replace(/^"(.*)"$/, "$1");
}

function setExeMode(mode) {
  exeMode = mode;
  for (const button of document.querySelectorAll("[data-exe-mode]")) {
    const active = button.dataset.exeMode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-checked", String(active));
  }
  $("executable-field").hidden = mode !== "custom";
  if (mode === "custom") $("executable").focus();
}

function renderDetection() {
  const box = $("detect-status");
  if (!detection) {
    box.dataset.state = "checking";
    $("detect-title").textContent = "Looking for llama.cpp…";
    $("detect-detail").textContent = "";
    return;
  }
  box.dataset.state = detection.resolved ? "found" : "missing";
  if (detection.resolved) {
    $("detect-title").textContent = detection.automatic ? `Found llama.cpp: runs as “${detection.command}”` : `Using your custom path: runs as “${detection.command}”`;
    $("detect-detail").textContent = detection.resolved;
  } else if (detection.automatic) {
    $("detect-title").textContent = "llama.cpp was not found (looked for llama and llama-server)";
    $("detect-detail").replaceChildren("Install llama.cpp, e.g. ", h("code", {}, navigator.platform.startsWith("Win") ? "winget install llama.cpp" : "brew install llama.cpp"),
      ", then click Check again. Or choose Custom path below.");
  } else {
    $("detect-title").textContent = "The custom path does not exist";
    $("detect-detail").textContent = detection.configured;
  }
  renderOverviewModels();
}

async function detect() {
  try {
    detection = await request("/api/detect");
  } catch (error) {
    detection = null;
    $("detect-title").textContent = `Cannot check for llama.cpp: ${error.message}`;
    return;
  }
  renderDetection();
}

function applySettings(settings) {
  workspace = settings;
  const executable = workspace.executable || DEFAULT_EXECUTABLE;
  if (["llama", "llama-server"].includes(executable.toLowerCase())) {
    $("executable").value = "";
    setExeMode("auto");
  } else {
    $("executable").value = executable;
    exeMode = "custom";
    setExeMode("custom");
  }
  $("port").value = workspace.port;
  $("active-port").textContent = workspace.port;
  $("api-url").value = `http://127.0.0.1:${workspace.port}/v1`;
  $("router-link").href = `http://127.0.0.1:${workspace.port}/`;
}

async function saveRouterSettings() {
  const executable = executableValue();
  if (!executable) throw new Error("Enter the path to llama (or llama-server), or switch back to Use PATH.");
  const data = await request("/api/workspace", {
    mode: "router",
    executable,
    port: Number($("port").value),
  });
  applySettings(data.settings);
  await detect();
  renderOverviewModels();
}

function saveCatalog() {
  return act(async () => {
    captureText();
    if (invalid.size) {
      const [key, message] = invalid.entries().next().value;
      const [where, option] = key.split("|");
      if (where !== scope) selectScope(where);
      tab = tabOf(option);
      renderTabs();
      renderBody();
      go("models");
      focusRow(SOURCE_KEYS.includes(option) ? "source" : option);
      throw new Error(`Fix ${optionByKey.get(option)?.label || option} in ${where === DEFAULTS ? "shared defaults" : where}: ${message}`);
    }
    renderWorkspace({ ...await request("/api/catalog", catalog), api_url: $("api-url").value });
    if (textMode) $("raw-options").value = optionsText(scopeOptions());
    notify(currentPid === null ? "Saved. Changes take effect when the router starts." : "Saved. Apply when it is safe to reload the router.");
  });
}

function routerAction(action, model) {
  return act(async () => {
    await request("/api/router", { action, model });
    notify(`${action === "load" ? "Loading" : "Unloading"} ${model}…`);
  });
}

// ---------- new model dialog ----------

function setNewSource(mode) {
  newSource = mode;
  for (const button of document.querySelectorAll("#add-model-form [data-source]")) {
    const active = button.dataset.source === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-checked", String(active));
  }
  $("new-source-label").textContent = mode === "hf" ? "Hugging Face repo (user/model:quant)" : "Path to .gguf file";
  $("new-source").placeholder = mode === "hf" ? "unsloth/Qwen3-8B-GGUF:Q4_K_M" : "C:\\models\\my-model-Q4_K_M.gguf";
}

function openNewModel() {
  if (!catalog) return;
  $("new-model").value = "";
  $("new-source").value = "";
  $("new-purpose").value = "text";
  $("new-decision-hint").hidden = true;
  showError("new-model-error", "");
  $("new-template").replaceChildren(h("option", { value: "" }, "Blank: only the shared defaults"),
    ...Object.keys(catalog.models).map((name) => h("option", { value: name }, `Copy settings from ${name}`)));
  setNewSource("local");
  $("new-model-dialog").showModal();
  $("new-model").focus();
}

// ---------- event wiring ----------

window.addEventListener("hashchange", renderView);

$("start").addEventListener("click", () => {
  void act(async () => {
    if (isDirty()) throw new Error("Save your model changes before starting the router.");
    if (workspace.mode !== "router"
        || workspace.executable !== executableValue()
        || workspace.port !== Number($("port").value)) {
      await saveRouterSettings();
    }
    try {
      renderStatus(await request("/api/start", {}));
    } catch (error) {
      if (/not found|does not exist/.test(error.message)) {
        await detect();
        go("router");
      }
      throw error;
    }
    notify("Router starting. Models load on their first request.");
  });
});

$("stop").addEventListener("click", () => {
  void act(async () => {
    renderStatus(await request("/api/stop", {}));
    notify("Router stopped.");
  });
});

$("settings").addEventListener("submit", (event) => {
  event.preventDefault();
  void act(async () => {
    await saveRouterSettings();
    notify("Installation settings saved.");
  });
});

$("import-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if ((isDirty() || Object.keys(catalog.models).length)
      && !confirm("Replace the managed model list with this INI? Unsaved edits will be lost. Your original INI is not modified.")) return;
  void act(async () => {
    scope = "";
    renderWorkspace({ ...await request("/api/import", { source: $("preset-source").value }), api_url: $("api-url").value });
    notify("INI imported. Apply saved changes if the router is running.");
  });
});

$("new-model-button").addEventListener("click", openNewModel);
for (const button of document.querySelectorAll("[data-exe-mode]")) {
  button.addEventListener("click", () => setExeMode(button.dataset.exeMode));
}
$("detect-refresh").addEventListener("click", () => {
  detection = null;
  renderDetection();
  void detect();
});

function renderThemeToggle() {
  const dark = document.documentElement.dataset.theme === "dark";
  $("theme-toggle").textContent = dark ? "☀" : "☾";
  $("theme-toggle").setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
}
$("theme-toggle").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("theme", theme);
  } catch {
    // The choice simply won't persist.
  }
  renderThemeToggle();
});
renderThemeToggle();
$("cancel-new-model").addEventListener("click", () => $("new-model-dialog").close());
$("new-purpose").addEventListener("change", () => {
  $("new-decision-hint").hidden = $("new-purpose").value !== "decision";
});
$("new-template").addEventListener("change", () => {
  const name = $("new-template").value;
  if (name) $("new-purpose").value = isDecisionModel(name) ? "decision" : "text";
  $("new-decision-hint").hidden = $("new-purpose").value !== "decision";
});
for (const button of document.querySelectorAll("#add-model-form [data-source]")) {
  button.addEventListener("click", () => setNewSource(button.dataset.source));
}
$("add-model-form").addEventListener("submit", (event) => {
  event.preventDefault();
  try {
    if (!leaveTextMode()) return;
    const name = $("new-model").value.trim();
    validateModelName(name);
    const source = $("new-source").value.trim().replace(/^"(.*)"$/, "$1");
    if (!source || /[\r\n\0]/.test(source)) throw new Error(newSource === "hf" ? "Enter a Hugging Face repo." : "Enter the path to a .gguf file.");
    const options = {};
    const template = $("new-template").value;
    if (template) {
      for (const [key, value] of Object.entries(catalog.models[template])) {
        if (!SOURCE_KEYS.includes(lookup(key)?.option.key)) options[key] = value;
      }
    }
    writeField(options, optionByKey.get(newSource === "hf" ? "hf-repo" : "model"), source);
    const decision = $("new-purpose").value === "decision";
    setModelPurpose(options, decision, catalog.defaults);
    if (decision && !template) applyDecisionStarter(options);
    catalog.models = { ...catalog.models, [name]: options };
    catalog.enabled.push(name);
    $("new-model-dialog").close();
    tab = "essentials";
    selectScope(name);
    go("models");
    toast(`${name} added. Review its essentials, then save.`);
  } catch (error) {
    showError("new-model-error", error.message);
  }
});

$("clone-model").addEventListener("click", () => {
  $("more-menu").open = false;
  const name = prompt(`Name for the copy of ${scope}:`, `${scope}-copy`);
  if (name === null) return;
  try {
    if (!leaveTextMode()) return;
    validateModelName(name.trim());
    catalog.models = { ...catalog.models, [name.trim()]: structuredClone(catalog.models[scope]) };
    if (catalog.enabled.includes(scope)) catalog.enabled.push(name.trim());
    selectScope(name.trim());
    toast(`Duplicated as ${name.trim()}.`);
  } catch (error) {
    toast(error.message, "error");
  }
});

$("rename-model").addEventListener("click", () => {
  $("more-menu").open = false;
  const name = prompt(`New ID for ${scope}. Clients must use the new name.`, scope);
  if (name === null || name.trim() === scope) return;
  try {
    if (!leaveTextMode()) return;
    const clean = name.trim();
    validateModelName(clean);
    catalog.models = Object.fromEntries(Object.entries(catalog.models).map(([key, value]) => [key === scope ? clean : key, value]));
    catalog.enabled = catalog.enabled.map((item) => (item === scope ? clean : item));
    for (const map of [sourceStash, sourceModes]) {
      if (map.has(scope)) map.set(clean, map.get(scope));
      map.delete(scope);
    }
    for (const [key, message] of [...invalid]) {
      if (key.startsWith(`${scope}|`)) {
        invalid.delete(key);
        invalid.set(`${clean}|${key.slice(scope.length + 1)}`, message);
      }
    }
    selectScope(clean);
    toast(`Renamed to ${clean}.`);
  } catch (error) {
    toast(error.message, "error");
  }
});

$("delete-model").addEventListener("click", () => {
  $("more-menu").open = false;
  if (scope === DEFAULTS || !confirm(`Remove ${scope}? Only its settings are removed; model files stay on disk.`)) return;
  delete catalog.models[scope];
  catalog.enabled = catalog.enabled.filter((name) => name !== scope);
  for (const key of [...invalid.keys()]) if (key.startsWith(`${scope}|`)) invalid.delete(key);
  textDirty = false;
  textMode = false;
  scope = Object.keys(catalog.models)[0] || DEFAULTS;
  renderAll();
});

$("model-enabled").addEventListener("change", () => setEnabled(scope, $("model-enabled").checked));
$("load-model").addEventListener("click", () => void routerAction("load", scope));
$("unload-model").addEventListener("click", () => void routerAction("unload", scope));

$("max-loaded").addEventListener("input", () => {
  const value = Number($("max-loaded").value);
  if (Number.isInteger(value) && value >= 1 && value <= 32) catalog.max_loaded = value;
  updateControls();
});
for (const button of document.querySelectorAll("#router-capacity .step")) {
  button.addEventListener("click", () => {
    const value = Math.min(32, Math.max(1, Number($("max-loaded").value || 1) + Number(button.dataset.step)));
    $("max-loaded").value = value;
    catalog.max_loaded = value;
    updateControls();
  });
}
$("autoload").addEventListener("change", () => {
  catalog.autoload = $("autoload").checked;
  updateControls();
});

$("option-search").addEventListener("input", () => {
  if (textMode && !leaveTextMode()) return;
  renderTabs();
  renderBody();
});
$("close-text").addEventListener("click", () => setTextMode(false));
$("raw-options").addEventListener("input", () => {
  textDirty = true;
  updateControls();
});
$("ini-preview").closest("details").addEventListener("toggle", () => {
  try {
    captureText();
  } catch (error) {
    toast(error.message, "error");
  }
  renderPreview();
});

$("save-catalog").addEventListener("click", () => void saveCatalog());
$("discard-catalog").addEventListener("click", () => {
  if (!confirm("Discard all unsaved changes?")) return;
  void act(async () => {
    textMode = false;
    renderWorkspace({ ...await request("/api/workspace"), api_url: $("api-url").value });
    toast("Changes discarded.");
  });
});
$("apply-catalog").addEventListener("click", () => {
  if (!confirm("Reload saved presets now? llama.cpp may unload changed models. Make sure clients are ready for a reload.")) return;
  void act(async () => {
    await request("/api/router", { action: "apply", model: null });
    pendingApply = false;
    notify("Applied. Changed models reload on their next request.");
  });
});

document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault();
    if (!$("save-catalog").disabled) void saveCatalog();
  }
});
document.addEventListener("click", (event) => {
  if (!event.target.closest("#more-menu")) $("more-menu").open = false;
});
window.addEventListener("beforeunload", (event) => {
  if (isDirty()) event.preventDefault();
});

async function copyInput(input, button) {
  try {
    await navigator.clipboard.writeText(input.value);
    const label = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = label; }, 1200);
  } catch {
    input.select();
    showError("action-error", "Clipboard access was denied. The value is selected for manual copying.");
  }
}

for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", () => copyInput($(button.dataset.copy), button));
}

async function initialize() {
  initializeObservability();
  renderView();
  updateControls();
  try {
    const [initial, data] = await Promise.all([request("/api/config"), request("/api/workspace")]);
    token = initial.csrf_token;
    if (!data.settings.executable) data.settings.executable = initial.config.executable;
    renderWorkspace(data);
    void detect();
  } catch (error) {
    showError("action-error", `Cannot load settings. Reload this page after resolving: ${error.message}`);
  }
  async function poll() {
    await refresh();
    setTimeout(poll, 2000);
  }
  await poll();
}

void initialize();
