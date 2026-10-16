"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const read = (name) => fs.readFileSync(path.join(__dirname, "..", "static", name), "utf8");
const app = read("app.js");

function dashboard(request = async () => ({ lines: ["server output"] })) {
  const elements = new Map();
  const $ = (id) => {
    if (!elements.has(id)) elements.set(id, {
      hidden: false, checked: false, textContent: "", scrollTop: 0, scrollHeight: 100,
      listeners: {},
      addEventListener(name, listener) { this.listeners[name] = listener; },
    });
    return elements.get(id);
  };
  const document = {
    hidden: false, listeners: {},
    addEventListener(name, listener) { this.listeners[name] = listener; },
    querySelectorAll() { return []; },
  };
  const context = vm.createContext({
    $, request, document, location: { hash: "#diagnostics" }, window: { scrollTo() {} },
    showError(id, message) { $(id).textContent = message; },
    renderTelemetryChart() {}, updateControls() {}, updateModelStates() {}, renderStatus() {},
    connected: false, polling: null, liveModels: [],
  });
  vm.runInContext(read("observability.js"), context);
  vm.runInContext(app.match(/const VIEWS = .*;/)[0], context);
  vm.runInContext(app.slice(app.indexOf("function renderView()"), app.indexOf("// ---------- status & polling")), context);
  vm.runInContext(app.slice(app.indexOf("async function refresh()"), app.indexOf("async function act(")), context);
  context.initializeObservability();
  context.renderView();
  return { context, $, document };
}

test("Opening Diagnostics and regular polling do not fetch logs by default", async () => {
  const routes = [];
  const { context, $ } = dashboard(async (route) => {
    routes.push(route);
    return { state: "stopped" };
  });
  for (let i = 0; i < 10; i += 1) await context.refresh();
  assert.equal(routes.filter((route) => route === "/api/logs").length, 0);
  assert.equal($("view-diagnostics").hidden, false);
  assert.equal($("follow-logs").checked, false);
});

test("Manual refresh displays a bounded snapshot without enabling live updates", async () => {
  const lines = Array.from({ length: 750 }, (_, i) => `line ${i}`);
  const { context, $ } = dashboard(async () => ({ lines }));
  await $("refresh-logs").listeners.click();
  await context.refreshLogs();
  assert.equal($("logs").textContent.split("\n").length, 500);
  assert.equal($("logs").textContent.split("\n")[0], "line 250");
  assert.equal($("logs").scrollTop, 0);
  assert.equal($("follow-logs").checked, false);
  await context.refreshLogs();
  assert.equal($("logs").textContent.split("\n").length, 500);
});

test("Live updates follow output, and leaving Diagnostics switches them off", async () => {
  const routes = [];
  const { context, $ } = dashboard(async (route) => {
    routes.push(route);
    return route === "/api/logs" ? { lines: ["latest"] } : { state: "stopped" };
  });
  $("follow-logs").checked = true;
  $("follow-logs").listeners.change();
  await context.refreshLogs();
  assert.equal($("logs").textContent, "latest");
  assert.equal($("logs").scrollTop, 100);
  await context.refresh();
  assert.equal(routes.filter((route) => route === "/api/logs").length, 2);
  context.location.hash = "#overview";
  context.renderView();
  assert.equal($("follow-logs").checked, false);
  await context.refresh();
  context.location.hash = "#diagnostics";
  context.renderView();
  await context.refresh();
  assert.equal(routes.filter((route) => route === "/api/logs").length, 2);
});

test("Background tabs neither fetch nor render logs, including stale responses", async () => {
  let resolve;
  let calls = 0;
  const { context, $, document } = dashboard(() => {
    calls += 1;
    return new Promise((done) => { resolve = done; });
  });
  const pending = context.refreshLogs();
  const duplicate = context.refreshLogs();
  assert.equal(calls, 1);
  document.hidden = true;
  document.listeners.visibilitychange();
  await context.refreshLogs();
  assert.equal(calls, 1);
  document.hidden = false;
  document.listeners.visibilitychange();
  resolve({ lines: ["stale"] });
  await Promise.all([pending, duplicate]);
  assert.equal($("logs").textContent, "");
});

for (const action of ["pause", "navigate"]) {
  test(`An in-flight response is not rendered after ${action}`, async () => {
    let resolve;
    const { context, $ } = dashboard(() => new Promise((done) => { resolve = done; }));
    $("follow-logs").checked = true;
    const pending = context.refreshLogs();
    if (action === "pause") {
      $("follow-logs").checked = false;
      $("follow-logs").listeners.change();
    } else {
      context.location.hash = "#models";
      context.renderView();
    }
    resolve({ lines: ["stale"] });
    await pending;
    assert.equal($("logs").textContent, "");
  });
}

test("Log errors are explicit and an empty successful response clears them", async () => {
  let failed = true;
  const { context, $ } = dashboard(async () => {
    if (failed) throw new Error("disk unavailable");
    return { lines: [] };
  });
  await context.refreshLogs();
  assert.match($("logs-error").textContent, /Cannot load server output: disk unavailable/);
  failed = false;
  await context.refreshLogs();
  assert.equal($("logs").textContent, "No server output yet.");
  assert.equal($("logs-error").textContent, "");
});
