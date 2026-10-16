"use strict";

let telemetryData = null;
let telemetryPolling = null;
let logsPolling = null;
let logsVersion = 0;
let telemetryVersion = 0;
let telemetryRendered = "";

function metricNumber(value, suffix = "", digits = 0) {
  return value === null || value === undefined ? "-" :
    `${Number(value).toLocaleString(undefined, { maximumFractionDigits: digits })}${suffix}`;
}

function metricPercent(value) {
  return value === null || value === undefined ? "-" : metricNumber(value * 100, "%", 1);
}

function telemetryModelValue(model) {
  return model === null ? "unknown" : `model:${model}`;
}

function telemetryDate(seconds) {
  return new Date(seconds * 1000).toLocaleString();
}

function renderTelemetry() {
  const data = telemetryData;
  const totals = data.totals;
  const sample = (count) => `${count} / ${totals.requests} tasks measured`;
  const cards = [
    ["Observed requests", metricNumber(totals.requests), "Slot tasks, not an HTTP access log"],
    ["Input tokens", metricNumber(totals.input_tokens), sample(totals.input_samples)],
    ["Input evaluated", metricNumber(totals.prefill_tokens), sample(totals.prefill_samples)],
    ["Output tokens", metricNumber(totals.output_tokens), sample(totals.output_samples)],
    ["Cached input tokens", metricNumber(totals.cached_tokens), sample(totals.cache_samples)],
    ["Cache hit rate", metricPercent(totals.cache_hit_rate), "Token-weighted, known input only"],
    ["Draft tokens generated", metricNumber(totals.draft_tokens), sample(totals.draft_samples)],
    ["Draft acceptance", metricPercent(totals.draft_acceptance), totals.accepted_tokens == null ? "-" : `${metricNumber(totals.accepted_tokens)} accepted tokens`],
    ["Prefill speed", metricNumber(totals.prefill_tps, " tok/s", 1), "Evaluated tokens / prefill time"],
    ["Generation speed", metricNumber(totals.generation_tps, " tok/s", 1), "Output tokens / generation time"],
    ["Prefill latency", metricNumber(totals.prefill_ms, " ms", 1), "Mean server prefill time; not TTFT"],
    ["Time to first token", metricNumber(totals.ttft_ms, " ms", 1), "Requires client-side instrumentation"],
    ["Compute latency", metricNumber(totals.total_ms, " ms", 1), "Mean prefill + generation time"],
  ];
  $("telemetry-metrics").replaceChildren(...cards.map(([label, value, detail]) =>
    h("article", { class: "panel metric-card" }, h("span", { class: "label" }, label),
      h("strong", {}, value), h("span", { class: "muted" }, detail))));
  $("telemetry-limitations").replaceChildren(...data.limitations.map((text) => h("li", {}, text)));
  showError("telemetry-warning", data.warning);
  renderTelemetryChart();
  renderTelemetryTables();
}

function telemetryTable(headers, rows, empty) {
  if (!rows.length) return h("p", { class: "muted" }, empty);
  return h("table", {}, h("thead", {}, h("tr", {}, ...headers.map((text) => h("th", { scope: "col" }, text)))),
    h("tbody", {}, ...rows.map((cells) => h("tr", {}, ...cells.map((cell) => h("td", {}, cell))))));
}

function renderTelemetryTables() {
  $("telemetry-models").replaceChildren(telemetryTable(
    ["Model", "Requests", "Input (known)", "Output (known)", "Cache hit", "Draft acceptance", "Prefill tok/s", "Generation tok/s"],
    telemetryData.models.map((row) => [
      h("button", { class: "ghost-button model-filter", onclick: () => {
        $("telemetry-model").value = telemetryModelValue(row.model);
        telemetryVersion += 1;
        void refreshObservability();
      } }, row.model ?? "Unattributed"),
      metricNumber(row.requests), metricNumber(row.input_tokens), metricNumber(row.output_tokens),
      metricPercent(row.cache_hit_rate), metricPercent(row.draft_acceptance),
      metricNumber(row.prefill_tps, "", 1), metricNumber(row.generation_tps, "", 1),
    ]), "No observed activity in this time range. Start the router and send a request directly to llama.cpp."));
  $("telemetry-history").replaceChildren(telemetryTable(
    ["Time", "Model", "Slot / task", "Capture", "Input", "Evaluated", "Output", "Cached", "Draft / accepted", "Prefill ms", "Generation ms"],
    telemetryData.history.map((row) => [
      telemetryDate(row.started_at), row.model ?? "Unattributed", `${row.slot} / ${row.task}`,
      row.status.replaceAll("_", " "), metricNumber(row.input_tokens), metricNumber(row.prefill_tokens),
      metricNumber(row.output_tokens), metricNumber(row.cached_tokens),
      `${metricNumber(row.draft_tokens)} / ${metricNumber(row.accepted_tokens)}`,
      metricNumber(row.prefill_ms, "", 1), metricNumber(row.generation_ms, "", 1),
    ]), "No requests captured yet. Historical raw logs are not re-imported."));
}

function renderTelemetryChart() {
  if (!telemetryData) return;
  const field = $("telemetry-plot").value;
  const series = telemetryData.series;
  if (!series.length) {
    $("telemetry-chart").replaceChildren(h("p", { class: "muted" }, "No activity to plot in this range."));
    $("telemetry-chart-labels").replaceChildren();
    return;
  }
  const step = telemetryData.bucket_seconds;
  const points = new Map(series.map((row) => [row.timestamp, row[field]]));
  const dense = [];
  for (let stamp = series[0].timestamp; stamp <= series.at(-1).timestamp; stamp += step) {
    dense.push({ stamp, value: points.has(stamp) ? points.get(stamp) : field === "requests" ? 0 : null });
  }
  if (!dense.some((point) => point.value !== null)) {
    $("telemetry-chart").replaceChildren(h("p", { class: "muted",
      title: "This metric is unavailable in the selected range." }, "-"));
    $("telemetry-chart-labels").replaceChildren();
    return;
  }
  const max = Math.max(0, ...dense.map((point) => point.value ?? 0));
  const percentage = ["cache_hit_rate", "draft_acceptance"].includes(field);
  const bars = dense.map(({ stamp, value }) => {
    const formatted = percentage ? metricPercent(value) : metricNumber(value, "", 1);
    const title = `${telemetryDate(stamp)} - ${telemetryDate(stamp + step)}: ${formatted}`;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 20 100");
    svg.setAttribute("aria-hidden", "true");
    const rect = document.createElementNS(svg.namespaceURI, "rect");
    const height = value === null ? 2 : Math.max(2, value / (max || 1) * 100);
    for (const [name, item] of Object.entries({ x: 1, y: 100 - height, width: 18, height, rx: 2 })) {
      rect.setAttribute(name, item);
    }
    svg.append(rect);
    return h("button", { class: value === null ? "trend-bar missing" : "trend-bar",
      title, "aria-label": title, onclick: () => { $("telemetry-chart-detail").textContent = title; } }, svg);
  });
  $("telemetry-chart").replaceChildren(...bars);
  $("telemetry-chart-labels").replaceChildren(
    h("span", {}, telemetryDate(dense[0].stamp)),
    h("span", {}, `Peak ${percentage ? metricPercent(max) : metricNumber(max, "", 1)} / ${Math.round(step / 60)} min bucket`),
    h("span", {}, telemetryDate(dense.at(-1).stamp + step)),
    h("p", { id: "telemetry-chart-detail", role: "status" }, "Select a bar for details."));
}

async function refreshObservability() {
  if (telemetryPolling) return telemetryPolling;
  telemetryPolling = (async () => {
    let retry;
    do {
      const version = telemetryVersion;
      const query = new URLSearchParams({ range: $("telemetry-range").value });
      const selected = $("telemetry-model").value;
      if (selected !== "all") query.set("model", selected === "unknown" ? "" : selected.slice(6));
      try {
        const data = await request(`/api/observability?${query}`);
        retry = version !== telemetryVersion;
        if (retry) continue;
        telemetryData = data;
        const choices = new Set(data.model_choices);
        if (selected !== "all") choices.add(selected === "unknown" ? null : selected.slice(6));
        const signature = JSON.stringify([data, selected, query.toString()]);
        if (signature !== telemetryRendered) {
          $("telemetry-model").replaceChildren(h("option", { value: "all" }, "All models"),
            ...Array.from(choices).map((model) => h("option", { value: telemetryModelValue(model) }, model ?? "Unattributed")));
          $("telemetry-model").value = selected;
          renderTelemetry();
          telemetryRendered = signature;
        }
        showError("telemetry-error", "");
        $("telemetry-updated").textContent = `Updated ${new Date().toLocaleTimeString()} · ${data.totals.in_progress || 0} in progress · ${data.totals.interrupted || 0} interrupted · ${data.totals.unmeasured || 0} unmeasured`;
      } catch (error) {
        retry = version !== telemetryVersion;
        if (!retry) {
          showError("telemetry-error", `Cannot load observability: ${error.message}`);
          $("telemetry-updated").textContent = "Update failed. Displayed data may be stale.";
        }
      }
    } while (retry);
  })();
  try { await telemetryPolling; } finally { telemetryPolling = null; }
}

function logsVisible() {
  return !$("view-diagnostics").hidden && !document.hidden;
}

async function refreshLogs() {
  if (!logsVisible()) return;
  if (logsPolling) return logsPolling;
  const version = logsVersion;
  logsPolling = (async () => {
    try {
      const logs = await request("/api/logs");
      if (!logsVisible() || version !== logsVersion) return;
      const output = logs.lines.slice(-500).join("\n") || "No server output yet.";
      if ($("logs").textContent !== output) {
        $("logs").textContent = output;
        if ($("follow-logs").checked) $("logs").scrollTop = $("logs").scrollHeight;
      }
      showError("logs-error", "");
    } catch (error) {
      if (logsVisible() && version === logsVersion) {
        showError("logs-error", `Cannot load server output: ${error.message}`);
      }
    }
  })();
  try { await logsPolling; } finally { logsPolling = null; }
}

function initializeObservability() {
  for (const id of ["telemetry-model", "telemetry-range"]) {
    $(id).addEventListener("change", () => { telemetryVersion += 1; void refreshObservability(); });
  }
  $("telemetry-plot").addEventListener("change", renderTelemetryChart);
  $("refresh-observability").addEventListener("click", () => { void refreshObservability(); });
  $("refresh-logs").addEventListener("click", () => { void refreshLogs(); });
  $("follow-logs").addEventListener("change", () => {
    logsVersion += 1;
    if ($("follow-logs").checked) void refreshLogs();
  });
  document.addEventListener("visibilitychange", () => {
    logsVersion += 1;
  });
}
