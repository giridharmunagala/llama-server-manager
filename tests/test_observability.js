"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function render(decision) {
  const elements = new Map();
  const $ = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: "", children: [],
      replaceChildren(...children) { this.children = children; },
    });
    return elements.get(id);
  };
  const h = (tag, attributes, ...children) => ({ tag, attributes, children });
  const context = vm.createContext({
    $, h, liveModels: [], isDecisionModel: (name) => decision && name === "model",
    showError() {},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../static/observability.js"), "utf8"), context);
  $("telemetry-model").value = "model:model";
  context.data = {
    totals: { requests: 4, completed: 4, calls_per_second: 2, calls_per_minute: 120, aggregate_tps: 400,
      aggregate_input_tps: 800, aggregate_prefill_tps: 600 },
    models: [{ model: "model", requests: 4, calls_per_second: 2, calls_per_minute: 120, aggregate_tps: 400 }],
    history: [], series: [], limitations: [],
  };
  vm.runInContext("telemetryData = data; renderTelemetry()", context);
  return (id) => JSON.stringify($(id).children);
}

test("Decision observability shows call rates and latency without token metrics", () => {
  const content = render(true);
  assert.match(content("telemetry-metrics"), /Completed calls \/ sec/);
  assert.match(content("telemetry-metrics"), /120 RPM/);
  assert.match(content("telemetry-metrics"), /Compute latency/);
  for (const id of ["telemetry-metrics", "telemetry-models", "telemetry-history", "telemetry-plot"]) {
    assert.doesNotMatch(content(id), /tok\/s|tokens|Generation|Draft/);
  }
});

test("Text observability distinguishes combined throughput from per-task speed", () => {
  const content = render(false);
  assert.match(content("telemetry-metrics"), /Aggregate output throughput/);
  assert.match(content("telemetry-metrics"), /400 tok\/s/);
  assert.match(content("telemetry-metrics"), /Aggregate input throughput/);
  assert.match(content("telemetry-metrics"), /800 tok\/s/);
  assert.match(content("telemetry-metrics"), /600 tok\/s/);
  assert.match(content("telemetry-plot"), /aggregate_input_tps/);
  assert.match(content("telemetry-plot"), /aggregate_prefill_tps/);
  assert.match(content("telemetry-metrics"), /Per-task generation speed/);
  assert.match(content("telemetry-models"), /Aggregate tok\/s \(60s\)/);
  assert.match(content("telemetry-plot"), /aggregate_tps/);
});
