"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..", "static");
const options = fs.readFileSync(path.join(root, "options.js"), "utf8");
const app = fs.readFileSync(path.join(root, "app.js"), "utf8");
const decision = fs.readFileSync(path.join(root, "decision.js"), "utf8");

function context() {
  const ctx = vm.createContext({
    h: (tag, props, ...children) => ({ tag, props, children: children.flat() }),
    $: () => ({ value: "http://127.0.0.1:8080/v1" }),
  });
  vm.runInContext(options, ctx);
  vm.runInContext(`
    const DEFAULTS = "*";
    const TRUTHY = new Set(["1", "true", "on", "enabled", "yes"]);
    const FALSY = new Set(["0", "false", "off", "disabled", "no"]);
    const optionByKey = new Map(OPTIONS.map(option => [option.key, option]));
    const optionIndex = new Map();
    for (const option of OPTIONS) {
      optionIndex.set(option.key.toLowerCase(), {option, kind: "on"});
      for (const key of option.aliases) optionIndex.set(key.toLowerCase(), {option, kind: "on"});
      for (const key of option.off || []) optionIndex.set(key.toLowerCase(), {option, kind: "off"});
    }
    const lookup = key => optionIndex.get(key.toLowerCase());
    const isBoolean = option => option.type === "toggle" || option.type === "bool";
    let scope = "clef";
    let catalog = {defaults: {}, models: {clef: {"hf-repo": "ggml-org/Clef-Flash-GGUF:Q4_K_M", tags: "decision-model"}}};
    let liveModels = [];
    const effective = key => catalog.models[scope][key] || catalog.defaults[key] || optionByKey.get(key).default;
  `, ctx);
  vm.runInContext(app.slice(app.indexOf("function readField("), app.indexOf("function scopeOptions(")), ctx);
  vm.runInContext(decision, ctx);
  return ctx;
}

test("decision purpose uses explicit tags or native output modalities, not repository-name guesses", () => {
  const ctx = context();
  assert.equal(ctx.isDecisionModel("clef"), true);
  assert.equal(ctx.isDecisionModel("*"), false);
  vm.runInContext(`catalog.models.clef.tags = "";`, ctx);
  assert.equal(ctx.isDecisionModel("clef"), false);
  vm.runInContext(`liveModels = [{id:"clef", architecture:{output_modalities:["decisions"]}}];`, ctx);
  assert.equal(ctx.isDecisionModel("clef"), true);
  vm.runInContext(`liveModels = [{id:"clef", architecture:{output_modalities:["text", "decisions"]}}];`, ctx);
  assert.equal(ctx.isDecisionModel("clef"), true);
  vm.runInContext(`liveModels = [{id:"clef"}];`, ctx);
  assert.equal(ctx.isDecisionModel("clef"), false);
});

test("purpose edits preserve unrelated tags and do not change model or generation flags", () => {
  const ctx = context();
  const settings = {"hf-repo": "example/model", temp: "0.8", tags: "vision,private"};
  ctx.setModelPurpose(settings, true);
  assert.equal(settings.tags, "vision,private,decision-model");
  assert.equal(settings.temp, "0.8");
  assert.equal(settings["hf-repo"], "example/model");
  ctx.setModelPurpose(settings, false);
  assert.equal(settings.tags, "vision,private");
  ctx.setModelPurpose(settings, true);
  ctx.setModelPurpose(settings, true);
  assert.equal(settings.tags, "vision,private,decision-model");
});

test("purpose supports environment aliases and overrides shared decision tags", () => {
  const ctx = context();
  const settings = {LLAMA_ARG_TAGS: "decision-model,vision"};
  ctx.setModelPurpose(settings, false);
  assert.equal(settings.LLAMA_ARG_TAGS, "vision");
  ctx.setModelPurpose(settings, true);
  assert.equal(settings.LLAMA_ARG_TAGS, "vision,decision-model");
  const blank = {};
  ctx.setModelPurpose(blank, false, {tags: "decision-model"});
  assert.equal(blank.tags, "text-generation");
  ctx.setModelPurpose(blank, true, {tags: "decision-model"});
  assert.equal(blank.tags, "text-generation,decision-model");
  vm.runInContext(`catalog.defaults.tags = "decision-model"; catalog.models.clef = {LLAMA_ARG_TAGS:"vision"};`, ctx);
  assert.equal(ctx.isDecisionModel("clef"), false);
  const ownAlias = {LLAMA_ARG_TAGS: "private"};
  ctx.setModelPurpose(ownAlias, true, {tags: "shared"});
  assert.equal(ownAlias.LLAMA_ARG_TAGS, "private,decision-model");
});

test("4K starter sets matching context and batches, preserves custom flags, overrides negated vision flag", () => {
  const ctx = context();
  const settings = {"hf-repo": "ggml-org/Clef-Flash-GGUF:Q4_K_M", "no-mmproj": "1", threads: "8"};
  ctx.applyDecisionStarter(settings);
  assert.equal(settings["ctx-size"], "4096");
  assert.equal(settings["batch-size"], "4096");
  assert.equal(settings["ubatch-size"], "4096");
  assert.equal(settings.parallel, "1");
  assert.equal(settings["cache-type-k"], "f16");
  assert.equal(settings["cache-type-v"], "f16");
  assert.equal(settings["mmproj-auto"], "true");
  assert.equal(settings["spec-type"], "none");
  assert.equal(settings["no-mmproj"], undefined);
  assert.equal(settings.threads, "8");
});

test("direct System One example includes the router model ID and all question types, without chat or streaming fields", () => {
  const ctx = context();
  assert.equal(ctx.decisionEndpoint(), "http://127.0.0.1:8080/v1/systemone");
  const example = ctx.decisionExample("clef-flash");
  assert.equal(example.model, "clef-flash");
  assert.equal(example.questions.route.type, "choice");
  assert.equal(example.questions.urgent.type, "noul");
  assert.equal(example.questions.priority.type, "score");
  assert.equal(example.questions.priority.criteria.length, 4);
  assert.equal(example.messages, undefined);
  assert.equal(example.stream, undefined);
});

test("decision guide exposes endpoint, batch warning, structured request and compatibility details", () => {
  const ctx = context();
  vm.runInContext(`catalog.models.clef["ctx-size"] = "8192";`, ctx);
  const guide = JSON.stringify(ctx.renderDecisionGuide());
  assert.ok(guide.includes("/v1/systemone"));
  assert.ok(guide.includes("Clef"));
  assert.ok(guide.includes("PR #29831"));
  assert.ok(guide.includes("full-context request would exceed the batch limits"));
  assert.ok(guide.includes("output_tokens = 0"));
  assert.ok(guide.includes("decision-example"));
  vm.runInContext(`applyDecisionStarter(catalog.models.clef);`, ctx);
  const adjusted = ctx.renderDecisionGuide();
  const warning = adjusted.children.find(child => child?.props?.id === "decision-batch-warning");
  assert.equal(warning.props.hidden, true);
});

test("Clef full-prompt batch warning updates as batch limits change", () => {
  const ctx = context();
  vm.runInContext(`applyDecisionStarter(catalog.models.clef);`, ctx);
  assert.equal(ctx.decisionBatchWarning(), "");
  vm.runInContext(`catalog.models.clef["batch-size"] = "2048";`, ctx);
  assert.ok(ctx.decisionBatchWarning().includes("clamp"));
  vm.runInContext(`catalog.models.clef["ubatch-size"] = "1024";`, ctx);
  assert.ok(ctx.decisionBatchWarning().includes("full-context"));
});
