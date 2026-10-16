"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const app = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
const pickerCode = app.slice(app.indexOf("async function browseModelFile("), app.indexOf("function setNewSource("));

test("Browse fills the selected absolute path and restores the button", async () => {
  let resolve;
  const browse = new Function("request", `${pickerCode}; return browseModelFile;`)((route, body) => {
    assert.equal(route, "/api/pick-model");
    assert.deepEqual(body, {});
    return new Promise((done) => { resolve = done; });
  });
  const button = { disabled: false, textContent: "Browse..." };
  let selected = null;
  const pending = browse(button, (value) => { selected = value; }, assert.fail);
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, "Picking...");
  resolve({ path: "C:\\models\\model with spaces.gguf" });
  await pending;
  assert.equal(selected, "C:\\models\\model with spaces.gguf");
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "Browse...");
});

test("Cancelling leaves the existing path untouched", async () => {
  const browse = new Function("request", `${pickerCode}; return browseModelFile;`)(async () => ({ path: null }));
  const button = { disabled: false, textContent: "Browse..." };
  await browse(button, assert.fail, assert.fail);
  assert.equal(button.disabled, false);
});

test("Picker errors are displayed and the button remains usable", async () => {
  const browse = new Function("request", `${pickerCode}; return browseModelFile;`)(async () => {
    throw new Error("Cannot open the file picker.");
  });
  const button = { disabled: false, textContent: "Browse..." };
  let message = "";
  await browse(button, assert.fail, (value) => { message = value; });
  assert.equal(message, "Cannot open the file picker.");
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "Browse...");
});
