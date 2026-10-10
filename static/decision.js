"use strict";

const DECISION_TAG = "decision-model";
const DECISION_SETTINGS = ["ctx-size", "batch-size", "ubatch-size", "parallel",
  "mmproj-auto", "mmproj", "mmproj-offload", "image-min-tokens", "image-max-tokens"];
const DECISION_STARTER = { "ctx-size": "4096", "batch-size": "4096", "ubatch-size": "4096",
  parallel: "1", "cache-type-k": "f16", "cache-type-v": "f16", "mmproj-auto": "true", "spec-type": "none" };

function modelTags(options, defaults = {}) {
  const own = readField(options, optionByKey.get("tags"));
  const field = own.present ? own : readField(defaults, optionByKey.get("tags"));
  return field.present ? field.value.split(",").map((tag) => tag.trim()).filter(Boolean) : [];
}

function isDecisionModel(name = scope) {
  if (name === DEFAULTS || !catalog?.models[name]) return false;
  const modalities = liveModels.find((model) => model.id === name)?.architecture?.output_modalities;
  return modalities?.includes("decisions") === true ||
    modelTags(catalog.models[name], catalog.defaults).includes(DECISION_TAG);
}

function setModelPurpose(options, decision, defaults = {}) {
  const tags = modelTags(options, defaults).filter((tag) => tag !== DECISION_TAG);
  if (decision) tags.push(DECISION_TAG);
  // An explicit empty tags value is invalid in INI. Preserve an override when
  // shared defaults would otherwise reintroduce the decision tag.
  writeField(options, optionByKey.get("tags"), tags.join(",") ||
    (modelTags(defaults).includes(DECISION_TAG) ? "text-generation" : undefined));
}

function applyDecisionStarter(options) {
  for (const [key, value] of Object.entries(DECISION_STARTER)) {
    writeField(options, optionByKey.get(key), value);
  }
}

function renderPurposeControl() {
  const select = h("select", { id: "model-purpose", onchange: () => {
    setModelPurpose(scopeOptions(), select.value === "decision", catalog.defaults);
    renderAll();
  } }, h("option", { value: "text" }, "Text generation / chat"),
  h("option", { value: "decision" }, "Decision model (System One)"));
  const detected = liveModels.find((model) => model.id === scope)?.architecture?.output_modalities?.includes("decisions");
  select.value = isDecisionModel() ? "decision" : "text";
  select.disabled = Boolean(detected);
  return h("div", { class: "model-purpose" },
    h("label", { class: "source-field" }, h("span", {}, "Model purpose"), select),
    h("p", { class: "row-desc" }, detected
      ? "Native decision capability reported by llama.cpp. The GGUF determines the endpoint."
      : "Decision marks this preset with the informational decision-model tag. llama.cpp detects actual capabilities from the GGUF; this does not convert a chat model."));
}

function decisionEndpoint() {
  return `${$("api-url").value.replace(/\/v1\/?$/, "")}/v1/systemone`;
}

function decisionExample(name) {
  return {
    model: name,
    state: "Customer message: I was charged twice for my order.",
    questions: {
      route: { type: "choice", instructions: "Which team should handle this?",
        criteria: { billing: "Payments, charges and refunds", technical: "Bugs, errors and login problems" } },
      urgent: { type: "noul", instructions: "Does this require urgent attention?" },
      priority: { type: "score", instructions: "How urgent is this?",
        criteria: ["Can wait", "This week", "Today", "Right now"] },
    },
  };
}

function decisionBatchWarning() {
  const batch = Number(effective("batch-size"));
  const micro = Number(effective("ubatch-size"));
  const ctx = Number(effective("ctx-size"));
  return micro > batch ? "Physical batch size exceeds logical batch size; llama.cpp may clamp it."
    : ctx > Math.min(batch, micro) ? "For Clef, a full-context request would exceed the batch limits. Increase both batch sizes or send a shorter state, including image and question tokens."
    : "";
}

function updateDecisionWarnings() {
  if (!isDecisionModel()) return;
  const warning = $("decision-batch-warning");
  if (warning) {
    warning.textContent = decisionBatchWarning();
    warning.hidden = !warning.textContent;
  }
  const flags = $("decision-flags-warning");
  if (flags) {
    const problems = [];
    if (effective("spec-type") !== "none") problems.push("Disable speculative decoding under Speed-up for decision-only models.");
    if (effective("embedding")) problems.push("Disable embedding-only mode under Advanced for System One.");
    if (effective("rerank")) problems.push("Disable reranking under Advanced for System One.");
    flags.textContent = problems.join(" ");
    flags.hidden = !flags.textContent;
  }
}

function renderDecisionGuide() {
  const endpoint = h("input", { id: "decision-api-url", readOnly: true, value: decisionEndpoint() });
  const example = h("textarea", { id: "decision-example", readOnly: true, rows: 19,
    class: "mono", value: JSON.stringify(decisionExample(scope), null, 2), "aria-label": "System One request JSON" });
  const warning = decisionBatchWarning();
  return h("section", { class: "decision-guide" },
    h("h3", {}, "Typed decisions, not generated text"),
    h("p", { class: "row-desc" }, "Send state + questions directly to llama.cpp. No manager proxy. Choice returns option probabilities; score returns an expected level; noul returns a yes probability."),
    h("label", { class: "source-field" }, h("span", {}, "POST endpoint"),
      h("div", { class: "value-field" }, endpoint,
        h("button", { type: "button", class: "copy", onclick: (event) => copyInput(endpoint, event.currentTarget) }, "Copy"))),
    h("p", { class: "row-desc" }, "Requires a recent llama.cpp build with /v1/systemone and Clef support (PR #29831). Clef only serves decisions; chat completions and streaming are not supported. No embedding, pooling or reranking flag is needed."),
    h("div", { class: "decision-batch-note" },
      h("strong", {}, "Clef: the entire prompt must fit in one physical batch."),
      h("p", {}, "Count state, question/schema and image tokens. Context, logical batch and physical batch sizes of 4096 are a conservative starting point. Increase together for larger requests; this needs more memory. Other decision architectures can have different batching requirements."),
      h("button", { type: "button", class: "secondary", onclick: () => {
        if (!confirm("Set context, both batch sizes to 4096, one slot, f16 KV cache, automatic vision projector and no speculative decoding? Other settings stay unchanged.")) return;
        applyDecisionStarter(scopeOptions());
        renderAll();
      } }, "Use 4K decision starter")),
    h("p", { id: "decision-batch-warning", class: "row-warning", hidden: !warning }, warning),
    h("p", { id: "decision-flags-warning", class: "row-warning", hidden: true }),
    h("p", { class: "row-desc" }, "For image decisions, leave the automatic projector on with a Hugging Face repo, or set a local mmproj file. Send base64 data URLs in images. Text-only decisions do not require a projector."),
    h("div", { class: "decision-example-head" }, h("h4", {}, "Example request body"),
      h("button", { type: "button", class: "secondary small", onclick: (event) => copyInput(example, event.currentTarget) }, "Copy JSON")),
    example,
    h("p", { class: "row-desc" }, "Clef decides its questions jointly. Output is structured probabilities, not completion tokens; System One reports output_tokens = 0. Log-only observability can have missing generation/draft metrics."),
    h("a", { href: "https://huggingface.co/blog/ggml-org/decision-models-in-llamacpp",
      target: "_blank", rel: "noopener noreferrer" }, "About decision models"),
    " · ",
    h("a", { href: "https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1systemone-typesafe-compatible-system-one-api",
      target: "_blank", rel: "noopener noreferrer" }, "System One API reference"));
}
