// LLM API console plugin. Requests are built here (and shown, editable, before they're sent),
// relayed and recorded by the server (/api/llm/servers/<id>/send streams NDJSON events:
// request → response → line…|body → done), and parsed here for each API flavor.
(() => {
const { $, $$, el, api, toast, showError } = PG;

const TOOLS = { chat: "Chat", models: "Models", probe: "Probe", http: "Raw HTTP", history: "History", servers: "Servers" };
const FLAVOR_PATH = { chat: "/v1/chat/completions", completions: "/v1/completions", responses: "/v1/responses",
  anthropic: "/v1/messages", ollama: "/api/chat", llamacpp: "/completion" };
const EXTRA_PATHS = ["/v1/embeddings", "/tokenize", "/detokenize", "/apply-template", "/api/show", "/api/generate",
  "/api/embed", "/unload", "/logs", "/upstream/", "/slots?fail_on_no_slot=1", "/v1/messages/count_tokens",
  "/flush_cache", "/v1/rerank", "/metrics"];

// Sampling parameters: where each one goes for each flavor (missing flavor = not supported there).
const O = "options."; // Ollama nests sampling under "options"
const PARAMS = [
  { key: "temperature", label: "Temperature", type: "float", step: 0.05,
    to: { chat: "temperature", completions: "temperature", responses: "temperature", anthropic: "temperature", ollama: O + "temperature", llamacpp: "temperature" } },
  { key: "top_p", label: "Top P", type: "float", step: 0.01,
    to: { chat: "top_p", completions: "top_p", responses: "top_p", anthropic: "top_p", ollama: O + "top_p", llamacpp: "top_p" } },
  { key: "top_k", label: "Top K", type: "int",
    to: { chat: "top_k", completions: "top_k", anthropic: "top_k", ollama: O + "top_k", llamacpp: "top_k" } },
  { key: "min_p", label: "Min P", type: "float", step: 0.01,
    to: { chat: "min_p", completions: "min_p", ollama: O + "min_p", llamacpp: "min_p" } },
  { key: "max_tokens", label: "Max tokens", type: "int",
    to: { chat: "max_tokens", completions: "max_tokens", responses: "max_output_tokens", anthropic: "max_tokens", ollama: O + "num_predict", llamacpp: "n_predict" } },
  { key: "seed", label: "Seed", type: "int", to: { chat: "seed", completions: "seed", ollama: O + "seed", llamacpp: "seed" } },
  { key: "presence_penalty", label: "Presence pen.", type: "float", step: 0.1,
    to: { chat: "presence_penalty", completions: "presence_penalty", ollama: O + "presence_penalty", llamacpp: "presence_penalty" } },
  { key: "frequency_penalty", label: "Frequency pen.", type: "float", step: 0.1,
    to: { chat: "frequency_penalty", completions: "frequency_penalty", ollama: O + "frequency_penalty", llamacpp: "frequency_penalty" } },
  { key: "repeat_penalty", label: "Repeat pen.", type: "float", step: 0.05,
    title: "Sent as repetition_penalty (vLLM, SGLang, TGI) on OpenAI APIs, repeat_penalty on Ollama and llama.cpp native",
    to: { chat: "repetition_penalty", completions: "repetition_penalty", ollama: O + "repeat_penalty", llamacpp: "repeat_penalty" } },
  { key: "stop", label: "Stop (comma sep.)", type: "list",
    to: { chat: "stop", completions: "stop", anthropic: "stop_sequences", ollama: O + "stop", llamacpp: "stop" } },
  { key: "num_ctx", label: "num_ctx (Ollama)", type: "int", title: "Context size Ollama loads the model with", to: { ollama: O + "num_ctx" } },
  { key: "logprobs", label: "Top logprobs", type: "int", title: "Return token probabilities with this many alternatives (0 = just the chosen token)",
    to: { chat: "@logprobs", completions: "logprobs", ollama: "@logprobs", llamacpp: "n_probs" } },
  { key: "json", label: "JSON output", type: "bool",
    to: { chat: "@json", responses: "@json", ollama: "format", llamacpp: "@json" } },
  { key: "stream_usage", label: "Usage in stream", type: "bool", def: true, title: "stream_options.include_usage: token counts in the last chunk",
    to: { chat: "@usage", completions: "@usage" } },
  { key: "cache_prompt", label: "cache_prompt", type: "bool", title: "llama.cpp: reuse the KV cache from the previous request", to: { chat: "cache_prompt", llamacpp: "cache_prompt" } },
];

const L = {
  active: false,
  tool: "chat",
  kinds: {},
  probes: [],
  servers: [],
  suggestions: [],
  sid: "",
  models: {}, // sid -> [{id, context_length, loaded, info}]
  modelErrors: {},
  info: {}, // "sid\tmodel" -> model-info result
  messages: [], // [{role, content, reasoning?, stats?, error?, tools?}]
  busy: null, // {controller, rid, kind: "chat"|"http"}
  last: null, // the last exchange's event log for the inspector
  usedTokens: null,
  historyItem: null,
};

// ---- small helpers ---------------------------------------------------------------------
const store = (k, v) => PG.storageSet(`pg.llm.${k}`, typeof v === "string" ? v : JSON.stringify(v));
function load(k, fallback) {
  const raw = PG.storageGet(`pg.llm.${k}`);
  if (raw === null || raw === undefined) return fallback;
  try { return JSON.parse(raw); } catch { return raw; }
}
const fmt = n => (n === null || n === undefined || Number.isNaN(n)) ? "–" : Number(n).toLocaleString();
const server = () => L.servers.find(s => s.id === L.sid);
const kindOf = s => (s && L.kinds[s.kind]) || L.kinds.auto || {};
function pretty(text) {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
}
function setPath(obj, path, value) {
  const keys = path.split(".");
  let at = obj;
  for (const k of keys.slice(0, -1)) at = at[k] = (at[k] && typeof at[k] === "object") ? at[k] : {};
  at[keys[keys.length - 1]] = value;
}
function merge(target, src) {
  for (const [k, v] of Object.entries(src)) {
    if (v && typeof v === "object" && !Array.isArray(v) && target[k] && typeof target[k] === "object" && !Array.isArray(target[k])) merge(target[k], v);
    else target[k] = v;
  }
  return target;
}
async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); toast(`${what} copied`); }
  catch { toast("Could not copy", true); }
}
function shQuote(s) { return `'${String(s).replace(/'/g, `'\\''`)}'`; }
// curl for a request. Secret headers become $LLM_API_KEY so a copied command never holds a key.
function curl(method, url, headers, bodyText) {
  const parts = [`curl -sS -N -X ${method} ${shQuote(url)}`];
  for (const [k, v] of Object.entries(headers || {})) {
    const lk = k.toLowerCase();
    let value = v;
    if (lk === "authorization") value = "Bearer $LLM_API_KEY";
    else if (["x-api-key", "api-key", "proxy-authorization"].includes(lk)) value = "$LLM_API_KEY";
    if (lk === "user-agent" || lk === "accept") continue;
    parts.push(value.includes("$LLM_API_KEY") ? `-H "${k}: ${value}"` : `-H ${shQuote(`${k}: ${value}`)}`);
  }
  if (bodyText) parts.push(`--data-raw ${shQuote(bodyText)}`);
  return parts.join(" \\\n  ");
}

// ---- JSON tables -------------------------------------------------------------------------
const MAX_COLS = 12, MAX_ROWS = 500, MAX_DEPTH = 4;
const isObj = v => v !== null && typeof v === "object" && !Array.isArray(v);

function jsonScalar(v) {
  if (typeof v === "number") return el("span", { class: "llm-num-cell" }, fmt(v));
  if (v === null) return el("span", { class: "muted" }, "null");
  return document.createTextNode(String(v));
}
function jsonCell(v, depth) {
  if (v !== null && typeof v === "object") {
    if (depth >= MAX_DEPTH) return el("span", { class: "mono" }, JSON.stringify(v));
    const n = Array.isArray(v) ? plural(v.length, "item") : plural(Object.keys(v).length, "key");
    return el("details", { class: "llm-nest" }, el("summary", {}, n), jsonTable(v, depth + 1));
  }
  return jsonScalar(v);
}

function jsonTable(value, depth = 0) {
  if (Array.isArray(value)) {
    if (value.length && value.every(isObj)) {
      const rows = value.slice(0, MAX_ROWS), cols = [];
      for (const row of rows) for (const k of Object.keys(row)) if (!cols.includes(k)) cols.push(k);
      const shown = cols.slice(0, MAX_COLS), more = cols.length > shown.length;
      const head = el("tr", {}, ...shown.map(c => el("th", { title: c }, c)), more ? el("th", {}, "…") : "");
      const body = rows.map(row => el("tr", {},
        ...shown.map(c => el("td", { class: typeof row[c] === "number" ? "num" : "" }, c in row ? jsonCell(row[c], depth) : "")),
        more ? el("td", {}, jsonCell(Object.fromEntries(cols.slice(MAX_COLS).filter(c => c in row).map(c => [c, row[c]])), depth)) : ""));
      const wrap = el("div", { class: "llm-scroll" }, el("table", { class: "llm-table llm-json" }, el("thead", {}, head), el("tbody", {}, ...body)));
      if (value.length > MAX_ROWS) wrap.append(el("div", { class: "muted small" }, `${fmt(value.length - MAX_ROWS)} more rows`));
      return wrap;
    }
    if (value.every(v => v === null || typeof v !== "object")) {
      const texts = value.map(String);
      return texts.join(", ").length > 80 ? el("div", { class: "llm-lines" }, ...texts.map(t => el("div", {}, t)))
        : document.createTextNode(texts.join(", ") || "(empty)");
    }
    return el("div", { class: "llm-lines" }, ...value.map(v => el("div", {}, jsonCell(v, depth))));
  }
  if (isObj(value)) {
    const entries = Object.entries(value);
    if (!entries.length) return el("span", { class: "muted" }, "(empty)");
    return el("table", { class: "llm-kv llm-json" }, el("tbody", {}, ...entries.map(([k, v]) =>
      el("tr", {}, el("th", {}, k), el("td", {}, jsonCell(v, depth))))));
  }
  return jsonScalar(value);
}

// The useful part of a reply first (the usual list envelopes unwrapped), the other top-level keys below it.
function smartTable(value) {
  if (isObj(value)) {
    const key = ["data", "models", "results", "items", "running"].find(k => Array.isArray(value[k]) && value[k].length && value[k].every(isObj));
    if (key) {
      const rest = Object.fromEntries(Object.entries(value).filter(([k]) => k !== key && !(k === "object" && value[k] === "list")));
      return el("div", { class: "llm-json-parts" }, el("div", { class: "muted small" }, `${key} (${fmt(value[key].length)})`), jsonTable(value[key]),
        Object.keys(rest).length ? jsonTable(rest) : "");
    }
  }
  return jsonTable(value);
}

// Streamed lines as rows: time, event, the text each carries, and its full JSON (expandable).
function linesTable(lines) {
  const rows = [];
  for (const ev of lines.slice(-MAX_ROWS)) {
    const payload = linePayload(ev.data);
    if (payload === null) continue;
    let obj = null;
    try { obj = JSON.parse(payload); } catch { /* plain text */ }
    const c = obj && obj.choices && obj.choices[0], d = (c && (c.delta || c.message)) || {};
    const text = obj ? (d.content || d.reasoning_content || (obj.message && obj.message.content) || obj.content || obj.response || "") : payload;
    rows.push({ ms: Math.round(ev.t), event: obj ? (obj.type || (c && c.finish_reason) || (obj.usage ? "usage" : "chunk")) : "text",
      text: String(text), json: obj === null ? payload : obj });
  }
  return jsonTable(rows.length ? rows : [{ ms: "", event: "(no data lines)", text: "", json: "" }]);
}

// Fills `box` with a Table/Raw view of a response. `text` is a body; `lines` are streamed NDJSON/SSE events.
function showJson(box, text, { lines = null } = {}) {
  box._src = { text, lines };
  let parsed, ok = false;
  if (lines) ok = lines.length > 0;
  else if (text) { try { parsed = JSON.parse(text); ok = parsed !== null && typeof parsed === "object"; } catch { /* not JSON */ } }
  const mode = ok ? load(`view.${box.id}`, "table") : "raw";
  const seg = ok ? el("div", { class: "llm-seg", role: "group" }, ...["table", "raw"].map(m =>
    el("button", { type: "button", class: "small ghost", "aria-pressed": String(m === mode),
      onclick: () => { store(`view.${box.id}`, m); showJson(box, box._src.text, { lines: box._src.lines }); } }, m === "table" ? "Table" : "Raw"))) : "";
  let body;
  if (mode === "table") body = lines ? linesTable(lines) : smartTable(parsed);
  else {
    const raw = lines ? lines.slice(-3000).map(ev => `${String(ev.t.toFixed(0)).padStart(7)} ms  ${ev.data}`).join("\n") : pretty(text || "");
    body = el("pre", { class: "log mono small llm-tall" }, raw);
  }
  box.replaceChildren(seg, body);
}

// Same, but waits for the next frame so a fast stream doesn't rebuild the table per line.
const jsonQueued = new WeakSet();
function showJsonSoon(box, text, opts) {
  box._src = { text, lines: opts && opts.lines };
  if (jsonQueued.has(box)) return;
  jsonQueued.add(box);
  requestAnimationFrame(() => { jsonQueued.delete(box); showJson(box, box._src.text, { lines: box._src.lines }); });
}

function kvTable(obj) {
  return el("table", { class: "llm-kv" }, el("tbody", {}, ...Object.entries(obj || {}).map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, String(v))))));
}

// ---- servers -----------------------------------------------------------------------------
async function loadServers() {
  try {
    const data = await api("GET", "/api/llm/servers");
    L.servers = data.servers;
    L.suggestions = data.suggestions;
  } catch (e) { toast(e.message, true); }
  if (!L.servers.some(s => s.id === L.sid)) L.sid = (L.servers.find(s => s.id === load("sid", "")) || L.servers[0] || {}).id || "";
  renderServers();
}

function renderServers() {
  $("#llm-server").replaceChildren(...(L.servers.length ? L.servers.map(s => el("option", { value: s.id }, s.name))
    : [el("option", { value: "" }, "No servers yet — add one")]));
  $("#llm-server").value = L.sid;
  $("#llm-server-list").replaceChildren(...L.servers.map(s => {
    const status = L.modelErrors[s.id] === undefined ? "" : L.modelErrors[s.id] ? "bad" : "ok";
    return el("li", { class: s.id === L.sid ? "active" : "", tabindex: 0, title: s.base_url,
      onclick: () => selectServer(s.id), onkeydown: e => { if (e.key === "Enter") selectServer(s.id); } },
    el("div", { class: "p-title" }, el("span", { class: `p-dot ${status}`, title: L.modelErrors[s.id] || (status ? "Answered" : "Not checked yet") }),
      el("span", { class: "p-name" }, s.name)),
    el("div", { class: "p-sub" }, `${(L.kinds[s.kind] || {}).label || s.kind} · ${s.base_url.replace(/^https?:\/\//, "")}`));
  }));
  $("#llm-server-rows").replaceChildren(...(L.servers.length ? L.servers.map(s => el("tr", {},
    el("td", { title: s.name }, s.name), el("td", {}, (L.kinds[s.kind] || {}).label || s.kind),
    el("td", { class: "mono", title: s.base_url }, s.base_url),
    el("td", {}, s.api_key_saved ? "saved" : s.api_key_present ? `env ${s.api_key_env}` : s.api_key_env ? `env (unset)` : "none"),
    el("td", {}, el("div", { class: "actions" },
      el("button", { class: "small ghost", type: "button", onclick: () => { selectServer(s.id); openTool("chat"); } }, "Use"),
      el("button", { class: "small ghost", type: "button", onclick: () => editServer(s) }, "Edit"),
      el("button", { class: "small ghost danger", type: "button", onclick: () => deleteServer(s) }, "Delete")))))
    : [el("tr", {}, el("td", { colspan: 5, class: "muted" }, "No saved servers. Add one below, or scan this computer."))]));
  renderSuggestions();
}

function renderSuggestions(found = L.found || []) {
  const rows = [
    ...found.map(f => ({ name: `${(L.kinds[f.kind] || {}).label || f.kind} on port ${f.port}`, base_url: f.base_url, kind: f.kind, source: "found on this computer" })),
    ...L.suggestions.map(s => ({ ...s, kind: "auto" })),
  ];
  $("#llm-suggest-table").hidden = !rows.length;
  $("#llm-suggest-rows").replaceChildren(...rows.map(r => el("tr", {},
    el("td", { class: "mono", title: r.base_url }, `${r.name} · ${r.base_url}`), el("td", { class: "muted" }, r.source),
    el("td", {}, el("button", { class: "small ghost", type: "button", onclick: () => suggestServer(r) }, "Add")))));
}

async function selectServer(sid) {
  if (!sid || sid === L.sid && L.models[sid]) { renderServers(); return; }
  L.sid = sid;
  store("sid", sid);
  renderServers();
  const s = server();
  $("#llm-flavor").value = load(`flavor.${sid}`, null) || kindOf(s).flavor || "chat";
  $("#llm-model").value = load(`model.${sid}`, "") || (s && s.default_model) || "";
  updatePaths();
  await loadModels();
  modelChanged();
}

async function loadModels(force = false) {
  const sid = L.sid;
  if (!sid) return;
  if (L.models[sid] && !force) { renderModels(); return; }
  $("#llm-models-note").textContent = "Loading…";
  try {
    const data = await api("POST", `/api/llm/servers/${sid}/models`);
    L.models[sid] = data.models;
    L.modelErrors[sid] = data.models.length || !data.errors.length ? "" : data.errors.join("; ");
    if (L.sid === sid) $("#llm-models-note").textContent = `${plural(data.models.length, "model")} from ${data.sources.join(", ") || "nowhere"}`
      + (data.errors.length ? ` · ${data.errors.join("; ")}` : "");
  } catch (e) {
    L.models[sid] = [];
    L.modelErrors[sid] = e.message;
    if (L.sid === sid) $("#llm-models-note").textContent = e.message;
  }
  if (L.sid !== sid) return;
  renderServers();
  renderModels();
  // Pick a model when there's none (or a stale one): the default, else a loaded one, else the first.
  const list = L.models[sid], input = $("#llm-model");
  if (list.length && !list.some(m => m.id === input.value)) {
    const s = server();
    const pick = list.find(m => m.id === (s && s.default_model)) || list.find(m => m.loaded) || list[0];
    if (!input.value || !input.dataset.typed) { input.value = pick.id; modelChanged(); }
  }
}
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

function renderModels() {
  const list = L.models[L.sid] || [];
  $("#llm-model-list").replaceChildren(...list.map(m => el("option", { value: m.id },
    [m.context_length ? `ctx ${fmt(m.context_length)}` : "", m.loaded ? "loaded" : "", m.info].filter(Boolean).join(" · "))));
  $("#llm-model-rows").replaceChildren(...(list.length ? list.map(m => el("tr", { class: m.id === $("#llm-model").value ? "flash" : "" },
    el("td", { class: "mono", title: m.id }, m.id),
    el("td", { title: m.context_source || "" }, m.context_length ? fmt(m.context_length) : "–"),
    el("td", {}, m.loaded === null ? "–" : m.loaded ? "yes" : "no"),
    el("td", { title: m.info }, m.info || ""),
    el("td", {}, el("div", { class: "actions" },
      el("button", { class: "small ghost", type: "button", onclick: () => { useModel(m.id); openTool("chat"); } }, "Use"),
      el("button", { class: "small ghost", type: "button", onclick: () => showModelInfo(m.id) }, "Details")))))
    : [el("tr", {}, el("td", { colspan: 5, class: "muted" }, L.sid ? "No models listed." : "Choose a server first."))]));
}

function useModel(id) {
  const input = $("#llm-model");
  input.value = id;
  delete input.dataset.typed;
  modelChanged();
  renderModels();
}

let infoTimer = null;
function modelChanged() {
  const model = $("#llm-model").value.trim();
  if (L.sid) store(`model.${L.sid}`, model);
  clearTimeout(infoTimer);
  infoTimer = setTimeout(() => loadModelInfo(model), 300);
  updatePreview();
}

async function loadModelInfo(model, force = false) {
  const sid = L.sid, key = `${sid}\t${model}`;
  if (!sid || !model) { renderCtx(); return null; }
  if (!L.info[key] || force) {
    try { L.info[key] = await api("POST", `/api/llm/servers/${sid}/model-info`, { model }); }
    catch (e) { L.info[key] = { context_length: null, context_source: e.message, details: {} }; }
  }
  if (sid === L.sid && model === $("#llm-model").value.trim()) renderCtx();
  return L.info[key];
}

async function showModelInfo(model) {
  const out = $("#llm-model-info");
  out.hidden = false;
  out.replaceChildren("Loading…");
  const info = await loadModelInfo(model, true);
  const box = el("div", { id: "llm-model-info-body" });
  out.replaceChildren(el("pre", { class: "log mono small" }, `${model}\ncontext: ${info.context_length ? fmt(info.context_length) : "unknown"} ${info.context_source ? `(${info.context_source})` : ""}`), box);
  showJson(box, JSON.stringify(info.details || {}));
}

function contextLength() {
  const model = $("#llm-model").value.trim();
  const info = L.info[`${L.sid}\t${model}`];
  if (info && info.context_length) return { n: info.context_length, src: info.context_source };
  const m = (L.models[L.sid] || []).find(x => x.id === model);
  return m && m.context_length ? { n: m.context_length, src: m.context_source } : null;
}

function renderCtx() {
  const ctx = contextLength(), used = L.usedTokens;
  const pct = ctx && used ? Math.min(100, (used / ctx.n) * 100) : 0;
  $("#llm-ctx-text").textContent = `ctx ${ctx ? fmt(ctx.n) : "?"}${used ? ` · used ${fmt(used)}${ctx ? ` (${pct.toFixed(1)}%)` : ""}` : ""}`;
  $("#llm-ctx").title = ctx ? `Context window ${fmt(ctx.n)} tokens, from ${ctx.src}` : "Context size unknown: the server didn't report it";
  const fill = $("#llm-ctx-fill");
  fill.style.width = `${pct}%`;
  fill.className = pct > 90 ? "bad" : pct > 70 ? "warn" : "";
}

function serverSpec(f) {
  const env = keyMode() === "env";
  return { id: f.elements.id.value, name: f.elements.name.value.trim(), kind: f.elements.kind.value, base_url: f.elements.base_url.value.trim(),
    // Only the active key input is sent; a blank key on edit keeps the saved one (the server decides).
    api_key: env ? "" : f.elements.api_key.value, clear_key: f.elements.clear_key.checked,
    api_key_env: env ? f.elements.api_key_env.value.trim() : "",
    auth: f.elements.auth.value, timeout: f.elements.timeout.value ? Number(f.elements.timeout.value) : null,
    verify_tls: f.elements.verify_tls.checked, headers: f.elements.headers.value, default_model: f.elements.default_model.value.trim(),
    from_endpoint: f.elements.from_endpoint.value || undefined };
}

const keyMode = () => $("#llm-key-mode").value;
function setKeyMode(mode) {
  const f = $("#llm-server-form");
  $("#llm-key-mode").value = mode;
  f.elements.api_key.hidden = mode === "env";
  f.elements.api_key_env.hidden = mode !== "env";
  $("#llm-key-hint").textContent = mode === "env" ? "Name of an environment variable PatchGoblin reads the key from." : "Leave empty for servers without a key.";
}

function showServerForm(open = true) {
  $("#llm-server-form").hidden = !open;
  if (open) $("#llm-server-form").scrollIntoView({ block: "nearest" });
}

function resetServerForm() {
  const f = $("#llm-server-form");
  f.reset();
  f.elements.id.value = f.elements.from_endpoint.value = "";
  $("#llm-server-form-title").textContent = "Add a server";
  $("#llm-clear-key-label").hidden = true;
  $("#llm-advanced").open = false;
  f.elements.api_key.placeholder = "(none)";
  f.elements.base_url.placeholder = (L.kinds[f.elements.kind.value] || {}).url || "";
  setKeyMode("key");
  kindHint();
  showError($("#llm-server-error"), "");
  showTest(null);
  showServerForm(false);
}

function newServer() {
  resetServerForm();
  showServerForm();
  $("#llm-kind").focus();
}

// Advanced opens when it holds anything but the defaults, so a saved setting is never hidden.
function advancedUsed(f) {
  const k = L.kinds[f.elements.kind.value] || {};
  const keyed = k.auth || /^https:/.test(f.elements.base_url.value) || f.elements.api_key_env.value;
  return f.elements.auth.value !== (k.auth || (keyed ? "bearer" : "none")) || !!f.elements.timeout.value
    || !f.elements.verify_tls.checked || !!f.elements.headers.value.trim() || !!f.elements.default_model.value.trim();
}

function editServer(s) {
  resetServerForm();
  const f = $("#llm-server-form");
  for (const k of ["id", "name", "kind", "base_url", "api_key_env", "auth", "timeout", "default_model"]) f.elements[k].value = s[k] ?? "";
  f.elements.verify_tls.checked = s.verify_tls !== false;
  f.elements.headers.value = Object.entries(s.headers || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
  f.elements.api_key.placeholder = s.api_key_saved ? "(saved — leave blank to keep)" : "(none)";
  setKeyMode(s.api_key_env && !s.api_key_saved ? "env" : "key");
  $("#llm-clear-key-label").hidden = !s.api_key_saved;
  $("#llm-server-form-title").textContent = `Edit ${s.name}`;
  $("#llm-advanced").open = advancedUsed(f);
  kindHint();
  showServerForm();
}

function kindHint() {
  const f = $("#llm-server-form"), k = L.kinds[f.elements.kind.value] || {};
  const port = (k.url || "").match(/:(\d+)/);
  const needsKey = k.auth || /^https:/.test(k.url || "");
  $("#llm-kind-hint").textContent = `${port ? `Default port ${port[1]}. ` : ""}${needsKey ? "This kind needs an API key." : "Local servers usually need no key."}`;
}

function kindChanged() {
  const f = $("#llm-server-form"), k = L.kinds[f.elements.kind.value] || {};
  f.elements.base_url.placeholder = k.url || "";
  const cur = f.elements.base_url.value.trim();
  if (!cur || Object.values(L.kinds).some(x => x.url === cur)) f.elements.base_url.value = k.url || "";
  if (!f.elements.id.value) f.elements.auth.value = k.auth || (/^https:/.test(k.url || "") ? "bearer" : "none");
  kindHint();
}

function showTest(content, bad = false) {
  const box = $("#llm-server-test");
  box.hidden = content === null;
  box.className = `llm-test ${bad ? "bad" : "ok"}`;
  box.replaceChildren(...(content === null ? [] : [].concat(content)));
}

async function testServer() {
  const f = $("#llm-server-form"), btn = $("#llm-server-test-btn");
  showError($("#llm-server-error"), "");
  btn.disabled = true;
  showTest("Testing…");
  try {
    const data = await api("POST", "/api/llm/servers/test", { server: serverSpec(f) });
    const n = data.models.models.length, label = (L.kinds[data.detected] || {}).label || data.detected;
    if (!n && data.models.errors.length) {
      showTest(`✗ ${data.models.errors.join("; ")}${data.reachable.length ? "" : " — nothing answered"}`, true);
      return;
    }
    const out = [`✓ ${plural(n, "model")}${data.models.sources.length ? ` (from ${data.models.sources.join(", ")})` : ""}${data.detected !== "auto" ? ` · looks like ${label}` : ""}`];
    if (data.detected !== "auto" && f.elements.kind.value === "auto") {
      out.push(" ", el("button", { type: "button", class: "ghost small", onclick: () => { f.elements.kind.value = data.detected; kindChanged(); } }, `Use kind ${label}`));
    }
    showTest(out);
  } catch (e) { showTest(`✗ ${e.message}`, true); }
  finally { btn.disabled = false; }
}

function suggestServer(r) {
  resetServerForm();
  const f = $("#llm-server-form");
  f.elements.name.value = r.name || "";
  f.elements.base_url.value = r.base_url;
  f.elements.kind.value = L.kinds[r.kind] ? r.kind : "auto";
  f.elements.auth.value = (L.kinds[f.elements.kind.value] || {}).auth || (r.endpoint ? "bearer" : "none");
  f.elements.from_endpoint.value = r.endpoint || "";
  if (r.endpoint) f.elements.api_key.placeholder = "(copied from the PatchGoblin endpoint)";
  $("#llm-advanced").open = advancedUsed(f);
  kindHint();
  showServerForm();
  f.elements.name.focus();
}

async function submitServer(e) {
  e.preventDefault();
  try {
    const data = await api("POST", "/api/llm/servers", { server: serverSpec(e.target) });
    L.servers = data.servers;
    L.suggestions = data.suggestions;
    delete L.models[data.saved];
    for (const k of Object.keys(L.info)) if (k.startsWith(`${data.saved}\t`)) delete L.info[k];
    resetServerForm();
    toast("Server saved");
    L.sid = "";
    await selectServer(data.saved);
  } catch (err) { showError($("#llm-server-error"), err.message); }
}

async function deleteServer(s) {
  if (!confirm(`Delete the server "${s.name}" (and its saved key)?`)) return;
  try {
    const data = await api("DELETE", `/api/llm/servers/${s.id}`);
    L.servers = data.servers;
    L.suggestions = data.suggestions;
    if (L.sid === s.id) { L.sid = ""; if (L.servers[0]) await selectServer(L.servers[0].id); }
    renderServers();
  } catch (err) { toast(err.message, true); }
}

async function scanLocal() {
  const btn = $("#llm-scan");
  btn.disabled = true;
  btn.textContent = "Scanning…";
  try {
    L.found = (await api("POST", "/api/llm/scan")).found;
    renderSuggestions();
    toast(L.found.length ? `Found ${plural(L.found.length, "server")}` : "No new servers found on the usual ports");
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.textContent = "Scan this computer"; }
}

// ---- building requests ---------------------------------------------------------------------
function paramValues() {
  const out = {};
  for (const p of PARAMS) {
    const input = $(`#llm-p-${p.key}`);
    if (p.type === "bool") { if (input.value) out[p.key] = input.value === "on"; continue; }
    const raw = input.value.trim();
    if (!raw) continue;
    if (p.type === "list") out[p.key] = raw.split(",").map(s => s.replace(/\\n/g, "\n")).filter(s => s.length);
    else if (p.type === "int") out[p.key] = parseInt(raw, 10);
    else out[p.key] = parseFloat(raw);
  }
  return out;
}

function thinkStyle() {
  const v = $("#llm-think-style").value;
  return v === "auto" ? (kindOf(server()).think || "enable_thinking") : v;
}

function chatMessages(flavor) {
  const sendReasoning = $("#llm-send-reasoning").checked;
  return L.messages.filter(m => !m.error && (m.content || m.reasoning)).map(m => {
    const msg = { role: m.role, content: m.content };
    if (sendReasoning && m.reasoning && m.role === "assistant") {
      if (flavor === "ollama") msg.thinking = m.reasoning;
      else msg.reasoning_content = m.reasoning;
    }
    return msg;
  });
}

// The request body for the current form, conversation and flavor.
function buildBody(pendingText = "") {
  const flavor = $("#llm-flavor").value, model = $("#llm-model").value.trim();
  const system = $("#llm-system").value.trim(), stream = $("#llm-stream").checked;
  const msgs = chatMessages(flavor);
  if (pendingText) msgs.push({ role: $("#llm-role").value, content: pendingText });
  const body = {};
  if (flavor !== "llamacpp") body.model = model;
  if (flavor === "chat" || flavor === "ollama") body.messages = [...(system ? [{ role: "system", content: system }] : []), ...msgs];
  else if (flavor === "anthropic") {
    const sys = [system, ...msgs.filter(m => m.role === "system").map(m => m.content)].filter(Boolean).join("\n\n");
    if (sys) body.system = sys;
    body.messages = msgs.filter(m => m.role !== "system").map(m => ({ role: m.role, content: m.content }));
  } else if (flavor === "responses") {
    if (system) body.instructions = system;
    body.input = msgs.map(m => ({ role: m.role, content: m.content }));
  } else {
    // Raw prompt APIs get the conversation as plain text, exactly as typed (no chat template).
    body.prompt = [system, ...msgs.map(m => m.content)].filter(Boolean).join("\n\n");
  }
  body.stream = stream;

  const values = paramValues();
  for (const p of PARAMS) {
    const to = p.to[flavor];
    const v = values[p.key] ?? (p.def !== undefined ? p.def : undefined);
    if (!to || v === undefined) continue;
    if (to === "@logprobs") { body.logprobs = true; if (v > 0) body.top_logprobs = v; }
    else if (to === "@json") {
      if (!v) continue;
      if (flavor === "responses") body.text = { format: { type: "json_object" } };
      else if (flavor === "llamacpp") body.json_schema = {};
      else body.response_format = { type: "json_object" };
    } else if (to === "@usage") { if (v && stream) body.stream_options = { include_usage: true }; }
    else if (p.type === "bool" && to === "format") { if (v) body.format = "json"; }
    else setPath(body, to, v);
  }
  if (flavor === "anthropic" && body.max_tokens === undefined) body.max_tokens = 1024; // required there

  const think = $("#llm-think").value, effort = $("#llm-effort").value;
  const budget = parseInt($("#llm-budget").value, 10) || null;
  if (flavor === "chat") {
    const style = thinkStyle();
    if (style === "reasoning") {
      const r = {};
      if (think) r.enabled = think === "on";
      if (effort) r.effort = effort;
      if (budget) r.max_tokens = budget;
      if (Object.keys(r).length) body.reasoning = r;
    } else {
      if (effort) body.reasoning_effort = effort;
      if (think && (style === "enable_thinking" || style === "thinking")) setPath(body, `chat_template_kwargs.${style}`, think === "on");
    }
  } else if (flavor === "responses") {
    if (effort || think === "on") body.reasoning = { ...(effort ? { effort } : {}), summary: "auto" };
  } else if (flavor === "anthropic") {
    if (think === "on") body.thinking = { type: "enabled", budget_tokens: budget || 1024 };
    else if (think === "off") body.thinking = { type: "disabled" };
  } else if (flavor === "ollama") {
    if (effort && think !== "off") body.think = effort === "minimal" ? "low" : effort;
    else if (think) body.think = think === "on";
  }

  const extra = $("#llm-extra").value.trim();
  if (extra) merge(body, JSON.parse(extra)); // a parse error is reported by updatePreview
  return body;
}

function currentPath() { return FLAVOR_PATH[$("#llm-flavor").value]; }

let previewTimer = null;
function updatePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => {
    const s = server();
    $("#llm-req-url").textContent = s ? `POST ${s.base_url}${currentPath()}` : "";
    $("#llm-extra-flag").textContent = $("#llm-extra").value.trim() ? "(set)" : "";
    $("#llm-system-flag").textContent = $("#llm-system").value.trim() ? "(set)" : "";
    try { JSON.parse($("#llm-extra").value.trim() || "{}"); showError($("#llm-extra-error"), ""); }
    catch (e) { showError($("#llm-extra-error"), `Extra JSON: ${e.message}`); }
    if ($("#llm-raw-edit").checked) return;
    try { $("#llm-req-body").value = JSON.stringify(buildBody($("#llm-input").value.trim()), null, 2); showError($("#llm-req-error"), ""); }
    catch (e) { showError($("#llm-req-error"), e.message); }
    saveForm();
  }, 120);
}

function saveForm() {
  const form = { params: {}, think: $("#llm-think").value, style: $("#llm-think-style").value, effort: $("#llm-effort").value,
    budget: $("#llm-budget").value, split: $("#llm-split-think").checked, sendReasoning: $("#llm-send-reasoning").checked,
    extra: $("#llm-extra").value, system: $("#llm-system").value, stream: $("#llm-stream").checked };
  for (const p of PARAMS) form.params[p.key] = $(`#llm-p-${p.key}`).value;
  store("form", form);
  store("chat", L.messages.map(({ role, content, reasoning, error }) => ({ role, content, reasoning, error })));
}

function restoreForm() {
  const form = load("form", null);
  if (form) {
    for (const p of PARAMS) if (form.params && form.params[p.key] !== undefined) $(`#llm-p-${p.key}`).value = form.params[p.key];
    $("#llm-think").value = form.think || "";
    $("#llm-think-style").value = form.style || "auto";
    $("#llm-effort").value = form.effort || "";
    $("#llm-budget").value = form.budget || "";
    $("#llm-split-think").checked = form.split !== false;
    $("#llm-send-reasoning").checked = !!form.sendReasoning;
    $("#llm-extra").value = form.extra || "";
    $("#llm-system").value = form.system || "";
    $("#llm-stream").checked = form.stream !== false;
  }
  const chat = load("chat", []);
  L.messages = Array.isArray(chat) ? chat.filter(m => m && m.role) : [];
}

// ---- parsing responses -------------------------------------------------------------------
// One parsed piece of a reply: {content, reasoning, tools, usage, timings, logprobs, finish, model}
function parsePiece(flavor, obj) {
  if (!obj || typeof obj !== "object") return {};
  if (obj.error) return { error: typeof obj.error === "string" ? obj.error : obj.error.message || JSON.stringify(obj.error) };
  if (flavor === "chat" || flavor === "completions") {
    const c = (obj.choices || [])[0] || {}, d = c.delta || c.message || {};
    const lp = c.logprobs && (c.logprobs.content || (c.logprobs.tokens && c.logprobs.tokens.map((t, i) => ({
      token: t, logprob: c.logprobs.token_logprobs[i],
      top_logprobs: Object.entries((c.logprobs.top_logprobs || [])[i] || {}).map(([token, logprob]) => ({ token, logprob })) }))));
    return { content: d.content ?? c.text, reasoning: d.reasoning_content ?? d.reasoning ?? d.thinking,
      tools: d.tool_calls, usage: obj.usage, timings: obj.timings, logprobs: lp, finish: c.finish_reason, model: obj.model };
  }
  if (flavor === "responses") {
    const t = obj.type || "";
    if (t === "response.output_text.delta") return { content: obj.delta };
    if (t.startsWith("response.reasoning") && t.endsWith(".delta")) return { reasoning: obj.delta };
    const r = t === "response.completed" ? obj.response : t ? null : obj;
    if (!r) return {};
    let content = "", reasoning = "";
    for (const item of r.output || []) {
      if (item.type === "message") content += (item.content || []).map(c => c.text || "").join("");
      if (item.type === "reasoning") reasoning += [...(item.summary || []), ...(item.content || [])].map(s => s.text || "").join("\n");
    }
    return { content: t ? undefined : content, reasoning: t ? undefined : reasoning, usage: r.usage, finish: r.status, model: r.model };
  }
  if (flavor === "anthropic") {
    const t = obj.type;
    if (t === "content_block_delta") return { content: obj.delta.text, reasoning: obj.delta.thinking, tools: obj.delta.partial_json ? [{ partial: obj.delta.partial_json }] : undefined };
    if (t === "message_start") return { usage: obj.message.usage, model: obj.message.model };
    if (t === "message_delta") return { usage: obj.usage, finish: obj.delta && obj.delta.stop_reason };
    if (t === "message") return { content: (obj.content || []).filter(b => b.type === "text").map(b => b.text).join(""),
      reasoning: (obj.content || []).filter(b => b.type === "thinking").map(b => b.thinking).join("\n"),
      tools: (obj.content || []).filter(b => b.type === "tool_use"), usage: obj.usage, finish: obj.stop_reason, model: obj.model };
    return {};
  }
  if (flavor === "ollama") {
    const m = obj.message || {};
    const timings = obj.done ? { ollama: true, prompt_n: obj.prompt_eval_count, predicted_n: obj.eval_count,
      prompt_ms: obj.prompt_eval_duration / 1e6, predicted_ms: obj.eval_duration / 1e6, load_ms: obj.load_duration / 1e6, total_ms: obj.total_duration / 1e6 } : undefined;
    return { content: m.content ?? obj.response, reasoning: m.thinking ?? obj.thinking, tools: m.tool_calls, timings,
      usage: obj.done ? { prompt_tokens: obj.prompt_eval_count, completion_tokens: obj.eval_count } : undefined,
      finish: obj.done_reason, model: obj.model, logprobs: obj.logprobs };
  }
  if (flavor === "llamacpp") {
    return { content: obj.content, timings: obj.timings, finish: obj.stop ? (obj.stop_type || "stop") : undefined,
      model: obj.model, logprobs: obj.completion_probabilities && obj.completion_probabilities.map(p => ({ token: p.token ?? p.content,
        logprob: p.logprob ?? Math.log(p.prob || (p.probs && p.probs[0] && p.probs[0].prob) || 1e-9),
        top_logprobs: (p.top_logprobs || p.probs || []).map(t => ({ token: t.token ?? t.tok_str, logprob: t.logprob ?? Math.log(t.prob || 1e-9) })) })) };
  }
  return {};
}

function applyPiece(reply, piece, t) {
  if (piece.error) reply.error = piece.error;
  if (piece.content) reply.rawContent += piece.content;
  if (piece.reasoning) reply.rawReasoning += piece.reasoning;
  if ((piece.content || piece.reasoning) && reply.stats.ttft === null) reply.stats.ttft = t;
  if (piece.tools) {
    for (const call of piece.tools) {
      const i = call.index ?? reply.tools.length;
      const have = reply.tools[i];
      if (have && call.function && have.function) {
        have.function.arguments = (have.function.arguments || "") + (call.function.arguments || "");
      } else if (have && call.partial !== undefined) have.partial += call.partial;
      else reply.tools[i] = JSON.parse(JSON.stringify(call));
    }
  }
  if (piece.usage) reply.usage = { ...(reply.usage || {}), ...Object.fromEntries(Object.entries(piece.usage).filter(([, v]) => v !== undefined && v !== null)) };
  if (piece.timings) reply.timings = piece.timings;
  if (piece.logprobs) reply.logprobs.push(...piece.logprobs);
  if (piece.finish) reply.finish = piece.finish;
  if (piece.model) reply.model = piece.model;
  if (piece.content || piece.reasoning) reply.chunks += 1;
}

// Content and reasoning for display: reasoning fields plus <think>…</think> spans (an unclosed
// <think> while streaming counts as reasoning so far).
function splitThink(reply) {
  let content = reply.rawContent, reasoning = reply.rawReasoning;
  if ($("#llm-split-think").checked) {
    const parts = [];
    content = content.replace(/<think>([\s\S]*?)(<\/think>|$)/g, (_, inner) => { parts.push(inner.trim()); return ""; });
    if (!reply.rawContent.includes("<think>") && reply.rawContent.includes("</think>")) {
      // Templates that open <think> in the prompt only send the closing tag.
      const at = reply.rawContent.indexOf("</think>");
      parts.push(reply.rawContent.slice(0, at).trim());
      content = reply.rawContent.slice(at + 8);
    }
    reasoning = [reasoning, ...parts].filter(Boolean).join("\n");
  }
  return { content: content.replace(/^\s+/, ""), reasoning };
}

// ---- the exchange (shared by Chat and Raw HTTP) ----------------------------------------------
// POSTs to the relay and calls on(event) for every NDJSON event. Resolves when the stream ends.
async function exchange(kind, req, on) {
  const controller = new AbortController();
  L.busy = { controller, rid: null, kind };
  PG.setBadge("llm", "●", "request running");
  setBusyUi(true);
  try {
    const res = await fetch(`/api/llm/servers/${encodeURIComponent(L.sid)}/send`, {
      method: "POST", signal: controller.signal,
      headers: { "X-PatchGoblin": "1", "Content-Type": "application/json" }, body: JSON.stringify(req) });
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try { msg = (await res.json()).error || msg; } catch { /* not JSON */ }
      on({ event: "error", t: 0, message: msg });
      return;
    }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.event === "request") L.busy.rid = ev.id;
        on(ev);
      }
    }
  } catch (e) {
    on(e.name === "AbortError" ? { event: "cancelled", t: null } : { event: "error", t: null, message: e.message });
  } finally {
    L.busy = null;
    PG.setBadge("llm", 0);
    setBusyUi(false);
    loadHistory();
  }
}

async function stopExchange() {
  if (!L.busy) return;
  const { rid, controller } = L.busy;
  if (rid) { try { await api("POST", `/api/llm/cancel/${rid}`); } catch { /* aborting below anyway */ } }
  controller.abort();
}

function setBusyUi(on) {
  $("#llm-send").disabled = $("#llm-regen").disabled = on;
  $("#llm-stop").hidden = !on || (L.busy && L.busy.kind !== "chat");
  $("#llm-http-stop").hidden = !on || (L.busy && L.busy.kind !== "http");
  $("#llm-http-form").querySelector("button[type=submit]").disabled = on;
}

// The payload of one stream line ("data: {...}" for SSE, the line itself for NDJSON), or null.
function linePayload(line) {
  if (line.startsWith("data:")) {
    const data = line.slice(5).trim();
    return data === "[DONE]" ? null : data;
  }
  if (line.startsWith("event:") || line.startsWith(":") || line.startsWith("id:") || line.startsWith("retry:")) return null;
  return line;
}

// ---- chat ---------------------------------------------------------------------------------
function newReply() {
  return { role: "assistant", rawContent: "", rawReasoning: "", tools: [], usage: null, timings: null, logprobs: [],
    finish: "", model: "", chunks: 0, error: "", status: 0,
    stats: { ttfb: null, ttft: null, total: null, lines: 0 } };
}

async function send(regenerate = false) {
  if (L.busy) return;
  if (!L.sid) { toast("Add or choose a server first", true); openTool("servers"); return; }
  let bodyText;
  const text = $("#llm-input").value.trim();
  if (regenerate) {
    while (L.messages.length && L.messages[L.messages.length - 1].role === "assistant") L.messages.pop();
  } else if (text) {
    L.messages.push({ role: $("#llm-role").value, content: text });
    $("#llm-input").value = "";
  }
  if ($("#llm-raw-edit").checked) {
    bodyText = $("#llm-req-body").value;
    try { JSON.parse(bodyText); } catch (e) { showError($("#llm-req-error"), `Body isn't valid JSON: ${e.message}`); return; }
  } else {
    try { bodyText = JSON.stringify(buildBody(), null, 2); }
    catch (e) { showError($("#llm-req-error"), e.message); return; }
    $("#llm-req-body").value = bodyText;
  }
  const flavor = $("#llm-flavor").value;
  const reply = newReply();
  L.messages.push(reply);
  L.last = { lines: [], response: null, request: null, reply, flavor };
  $("#llm-stream-log").textContent = "";
  $("#llm-logprobs").replaceChildren();
  renderMessages();
  let firstBody = "";
  await exchange("chat", { method: "POST", path: currentPath(), body: bodyText, label: $("#llm-model").value.trim() }, ev => {
    if (ev.event === "request") { L.last.request = ev; }
    else if (ev.event === "response") {
      L.last.response = ev;
      reply.status = ev.status;
      reply.stats.ttfb = ev.ms;
      renderResponseMeta();
    } else if (ev.event === "line") {
      reply.stats.lines += 1;
      logLine(ev);
      const payload = linePayload(ev.data);
      if (payload === null) return;
      let obj;
      try { obj = JSON.parse(payload); } catch { if (reply.status >= 400) reply.error = (reply.error ? reply.error + "\n" : "") + payload; return; }
      applyPiece(reply, parsePiece(flavor, obj), ev.t);
      renderReply(reply);
    } else if (ev.event === "body") {
      firstBody = ev.data;
      showJson($("#llm-resp-body"), ev.data);
      let obj = null;
      try { obj = JSON.parse(ev.data); } catch { /* not JSON */ }
      if (obj) applyPiece(reply, parsePiece(flavor, obj), ev.t);
      if (reply.status >= 400 && !reply.error) reply.error = obj ? (obj.error && (obj.error.message || obj.error)) || ev.data : ev.data || `HTTP ${reply.status}`;
      else if (!obj && reply.status < 400) reply.error = "The reply isn't JSON: " + ev.data.slice(0, 500);
      if (reply.stats.ttft === null && (reply.rawContent || reply.rawReasoning)) reply.stats.ttft = ev.t;
    } else if (ev.event === "error") {
      reply.error = ev.message;
    } else if (ev.event === "cancelled") {
      reply.error = reply.error || "Stopped.";
      reply.stopped = true;
    } else if (ev.event === "done") {
      reply.stats.total = ev.t;
      if (reply.status >= 400 && !reply.error) reply.error = `HTTP ${reply.status}`;
    }
  });
  if (!firstBody && L.last.lines.length) $("#llm-resp-body").replaceChildren("(streamed: see the Stream tab)");
  finishReply(reply);
}

function finishReply(reply) {
  const { content, reasoning } = splitThink(reply);
  reply.content = content;
  reply.reasoning = reasoning;
  if (typeof reply.error !== "string") reply.error = JSON.stringify(reply.error);
  const u = reply.usage || {}, tm = reply.timings || {};
  const prompt = u.prompt_tokens ?? u.input_tokens ?? tm.prompt_n ?? null;
  const completion = u.completion_tokens ?? u.output_tokens ?? tm.predicted_n ?? null;
  if (prompt !== null || completion !== null) L.usedTokens = (prompt || 0) + (completion || 0);
  if (reply.stopped && !reply.content && !reply.reasoning) reply.error = "Stopped before any output.";
  renderMessages();
  renderStats(reply);
  renderLogprobs(reply);
  renderCtx();
  saveForm();
  updatePreview();
}

function renderResponseMeta() {
  const r = L.last.response, q = L.last.request;
  $("#llm-resp-meta").textContent = `${q.method} ${q.url}\n\nHTTP ${r.status} ${r.reason}   (first byte after ${r.ms} ms)\n`
    + Object.entries(r.headers).map(([k, v]) => `${k}: ${v}`).join("\n");
  if (!L.last.lines.length) $("#llm-resp-body").replaceChildren();
}

let logQueued = false;
function logLine(ev) {
  L.last.lines.push(ev);
  if (logQueued) return;
  logQueued = true;
  requestAnimationFrame(() => {
    logQueued = false;
    const log = $("#llm-stream-log"), lines = L.last ? L.last.lines : [];
    const shown = lines.slice(-1500);
    log.textContent = (lines.length > shown.length ? `… ${lines.length - shown.length} earlier lines hidden\n` : "")
      + shown.map(l => `${String(l.t.toFixed(0)).padStart(7)} ms  ${l.data}`).join("\n");
    $("#llm-stream-count").textContent = `${plural(lines.length, "line")}`;
    if ($("#llm-stream-follow").checked) log.scrollTop = log.scrollHeight;
  });
}

let replyQueued = false;
function renderReply(reply) {
  if (replyQueued) return;
  replyQueued = true;
  requestAnimationFrame(() => {
    replyQueued = false;
    const node = $("#llm-messages .llm-msg:last-child");
    if (!node) return;
    const { content, reasoning } = splitThink(reply);
    node.replaceWith(messageNode({ ...reply, content, reasoning, live: true }, L.messages.length - 1));
    const box = $("#llm-messages");
    box.scrollTop = box.scrollHeight;
  });
}

function statLine(m) {
  const s = m.stats;
  if (!s) return null;
  const u = m.usage || {}, tm = m.timings || {};
  const completion = u.completion_tokens ?? u.output_tokens ?? tm.predicted_n;
  const genMs = s.total !== null && s.ttft !== null ? s.total - s.ttft : null;
  const tps = tm.predicted_per_second || (tm.ollama && tm.predicted_n && tm.predicted_ms ? tm.predicted_n / (tm.predicted_ms / 1000) : null)
    || (completion && genMs > 0 ? completion / (genMs / 1000) : null);
  return [s.ttft !== null ? `TTFT ${s.ttft.toFixed(0)} ms` : null, s.total !== null ? `total ${(s.total / 1000).toFixed(2)} s` : null,
    completion !== undefined && completion !== null ? `${fmt(completion)} tok` : null, tps ? `${tps.toFixed(1)} tok/s` : null,
    m.finish ? `finish: ${m.finish}` : null, m.status && m.status !== 200 ? `HTTP ${m.status}` : null].filter(Boolean).join(" · ");
}

function messageNode(m, i) {
  const del = el("button", { class: "ghost small", type: "button", title: "Delete this message",
    onclick: () => { L.messages.splice(i, 1); renderMessages(); saveForm(); updatePreview(); } }, "×");
  const copyBtn = el("button", { class: "ghost small", type: "button", title: "Copy", onclick: () => copy(m.content || "", "Message") }, "⧉");
  return el("div", { class: `llm-msg ${m.role}${m.error ? " err" : ""}` },
    el("div", { class: "llm-msg-head" }, el("span", { class: "llm-role" }, m.role), m.model ? el("span", { class: "muted small mono" }, m.model) : null,
      el("span", { class: "muted small" }, statLine(m) || ""), el("span", { class: "llm-msg-actions" }, m.live ? null : copyBtn, m.live ? null : del)),
    m.reasoning ? el("details", { class: "llm-reasoning", open: m.live && !m.content ? true : undefined },
      el("summary", {}, `Thinking · ${fmt(m.reasoning.length)} chars`), el("div", { class: "llm-text" }, m.reasoning)) : null,
    m.content ? el("div", { class: "llm-text" }, m.content) : m.live && !m.reasoning && !m.error ? el("div", { class: "muted small" }, "waiting…") : null,
    (m.tools || []).length ? el("pre", { class: "log mono small" }, JSON.stringify(m.tools, null, 2)) : null,
    m.error ? el("div", { class: "error small" }, m.error) : null);
}

function renderMessages() {
  const box = $("#llm-messages");
  box.replaceChildren(...(L.messages.length ? L.messages.map((m, i) => messageNode(m, i))
    : [el("div", { class: "muted small llm-empty" }, "Ask something below. The exact request is shown on the right before you send it.")]));
  box.scrollTop = box.scrollHeight;
}

function renderStats(reply) {
  const s = reply.stats, u = reply.usage || {}, tm = reply.timings || {};
  const rows = [
    ["HTTP status", reply.status || "–"], ["Model (reported)", reply.model || "–"], ["Finish reason", reply.finish || "–"],
    ["Time to first byte", s.ttfb !== null ? `${s.ttfb} ms` : "–"], ["Time to first token", s.ttft !== null ? `${s.ttft.toFixed(0)} ms` : "–"],
    ["Total time", s.total !== null ? `${s.total.toFixed(0)} ms` : "–"], ["Stream lines / content chunks", `${s.lines} / ${reply.chunks}`],
    ["Prompt tokens", fmt(u.prompt_tokens ?? u.input_tokens ?? tm.prompt_n)],
    ["Completion tokens", fmt(u.completion_tokens ?? u.output_tokens ?? tm.predicted_n)],
    ["Reasoning tokens", fmt(u.completion_tokens_details && u.completion_tokens_details.reasoning_tokens)],
    ["Cached prompt tokens", fmt((u.prompt_tokens_details && u.prompt_tokens_details.cached_tokens) ?? u.cache_read_input_tokens ?? tm.cache_n)],
    ["Server prompt speed", tm.prompt_per_second ? `${tm.prompt_per_second.toFixed(1)} tok/s` : tm.ollama && tm.prompt_n && tm.prompt_ms ? `${(tm.prompt_n / (tm.prompt_ms / 1000)).toFixed(1)} tok/s` : "–"],
    ["Server generation speed", tm.predicted_per_second ? `${tm.predicted_per_second.toFixed(1)} tok/s` : tm.ollama && tm.predicted_n && tm.predicted_ms ? `${(tm.predicted_n / (tm.predicted_ms / 1000)).toFixed(1)} tok/s` : "–"],
    ["Model load time (Ollama)", tm.load_ms ? `${tm.load_ms.toFixed(0)} ms` : "–"],
    ["Content / reasoning chars", `${fmt(reply.content.length)} / ${fmt(reply.reasoning.length)}`],
  ];
  $("#llm-stats").replaceChildren(...rows.map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", { class: "mono" }, String(v)))),
    el("tr", {}, el("th", {}, "Raw usage / timings"), el("td", {}, el("pre", { class: "log mono small" }, JSON.stringify({ usage: reply.usage, timings: reply.timings }, null, 2)))));
}

function renderLogprobs(reply) {
  const box = $("#llm-logprobs");
  if (!reply.logprobs.length) { box.replaceChildren(el("span", { class: "muted small" }, "No logprobs in the last reply.")); return; }
  box.replaceChildren(...reply.logprobs.slice(0, 4000).map(lp => {
    const p = Math.exp(lp.logprob ?? 0);
    const alts = (lp.top_logprobs || []).map(t => `${JSON.stringify(t.token)}  ${(Math.exp(t.logprob) * 100).toFixed(1)}%`).join("\n");
    return el("span", { class: "llm-tok", style: `--p:${(1 - p).toFixed(3)}`, title: `${JSON.stringify(lp.token)}  ${(p * 100).toFixed(1)}%${alts ? `\n\n${alts}` : ""}` }, lp.token);
  }));
}

async function countTokens() {
  if (!L.sid) return;
  const btn = $("#llm-count");
  btn.disabled = true;
  try {
    let body = {};
    try { body = buildBody($("#llm-input").value.trim()); } catch { /* fall back to raw text */ }
    const messages = body.messages || (body.input ? body.input : null);
    const text = body.prompt || [$("#llm-system").value, ...(messages || []).map(m => m.content)].join("\n");
    const r = await api("POST", `/api/llm/servers/${L.sid}/tokenize`, { model: $("#llm-model").value.trim(), text, messages });
    L.usedTokens = r.count;
    renderCtx();
    toast(`${fmt(r.count)} tokens (${r.templated ? "with chat template" : "raw text"}, via ${r.via})`);
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
}

function addMessage() {
  const text = $("#llm-input").value.trim();
  if (!text) return;
  L.messages.push({ role: $("#llm-role").value, content: text });
  $("#llm-input").value = "";
  renderMessages();
  saveForm();
  updatePreview();
}

function selectTab(name) {
  for (const b of $$("#llm-view .llm-tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const p of $$("#llm-view [data-pane]")) p.hidden = p.dataset.pane !== name;
  store("tab", name);
}

// ---- probe --------------------------------------------------------------------------------
async function runProbe() {
  if (!L.sid) return;
  const btn = $("#llm-probe-run");
  btn.disabled = true;
  $("#llm-probe-note").textContent = "Probing…";
  try {
    const data = await api("POST", `/api/llm/servers/${L.sid}/probe`);
    const s = server(), label = (L.kinds[data.detected] || {}).label || data.detected;
    $("#llm-probe-note").replaceChildren(`Looks like: ${label}. `,
      s && s.kind !== data.detected && data.detected !== "auto"
        ? el("button", { class: "ghost small", type: "button", onclick: () => setKind(data.detected) }, `Set kind to ${label}`) : "");
    $("#llm-probe-rows").replaceChildren(...data.results.map(r => el("tr", { class: r.status >= 200 && r.status < 300 ? "" : "muted",
      onclick: () => {
        $("#llm-probe-head").hidden = $("#llm-probe-body").hidden = false;
        $("#llm-probe-head").textContent = `GET ${r.path} → ${r.status || r.error}`;
        showJson($("#llm-probe-body"), r.text);
      } },
      el("td", { class: "mono" }, r.path), el("td", {}, r.status ? String(r.status) : r.error ? "error" : "–"),
      el("td", {}, `${r.ms} ms`), el("td", { class: "mono", title: r.error || r.type }, r.error || r.type))));
  } catch (e) { $("#llm-probe-note").textContent = e.message; }
  finally { btn.disabled = false; }
}

async function setKind(kind) {
  const s = server();
  try {
    const data = await api("POST", "/api/llm/servers", { server: { ...s, kind, headers: s.headers } });
    L.servers = data.servers;
    delete L.models[s.id];
    renderServers();
    $("#llm-flavor").value = (L.kinds[kind] || {}).flavor || "chat";
    store(`flavor.${s.id}`, $("#llm-flavor").value);
    await loadModels(true);
    toast("Kind saved");
  } catch (e) { toast(e.message, true); }
}

// ---- raw HTTP -------------------------------------------------------------------------------
function updatePaths() {
  const paths = [...new Set([...Object.values(FLAVOR_PATH), ...L.probes, ...EXTRA_PATHS])];
  $("#llm-paths").replaceChildren(...paths.map(p => el("option", { value: p })));
}

async function sendHttp(e) {
  e.preventDefault();
  if (L.busy || !L.sid) { if (!L.sid) toast("Choose a server first", true); return; }
  const f = e.target, method = f.elements.method.value, out = $("#llm-http-out"), hdr = $("#llm-http-headers");
  const bodyText = ["GET", "HEAD", "DELETE", "OPTIONS"].includes(method) && !$("#llm-http-body").value.trim() ? null : $("#llm-http-body").value;
  out.replaceChildren();
  hdr.hidden = true;
  $("#llm-http-meta").textContent = "Sending…";
  const lines = [];
  let resp = null;
  await exchange("http", { method, path: f.elements.path.value.trim(), body: bodyText, label: "raw" }, ev => {
    if (ev.event === "response") {
      resp = ev;
      hdr.hidden = false;
      hdr.replaceChildren(el("pre", { class: "log mono small" }, `HTTP ${ev.status} ${ev.reason}`), kvTable(ev.headers));
    } else if (ev.event === "line") {
      lines.push(ev);
      showJsonSoon(out, "", { lines });
    } else if (ev.event === "body") {
      showJson(out, ev.data);
    } else if (ev.event === "error") {
      out.append(el("pre", { class: "log mono small" }, ev.message));
    } else if (ev.event === "done") {
      if (lines.length) showJson(out, "", { lines });
      $("#llm-http-meta").textContent = `HTTP ${ev.status || "–"} · first byte ${resp ? resp.ms : "–"} ms · total ${ev.t} ms · ${fmt(ev.bytes)} bytes${ev.lines ? ` · ${ev.lines} lines` : ""}`;
    } else if (ev.event === "cancelled") $("#llm-http-meta").textContent = "Stopped.";
  });
}

// ---- history --------------------------------------------------------------------------------
async function loadHistory() {
  let rows = [];
  try { rows = (await api("GET", "/api/llm/history")).history; } catch { return; }
  $("#llm-history-count").textContent = rows.length ? `(${rows.length})` : "";
  if (L.tool !== "history") return;
  $("#llm-history-rows").replaceChildren(...(rows.length ? rows.map(r => el("tr", { class: r.status >= 400 || r.error ? "bad" : "", onclick: () => showHistory(r.id) },
    el("td", {}, r.at.slice(11)), el("td", { title: r.server_name }, r.server_name),
    el("td", { class: "mono", title: `${r.method} ${r.path}` }, `${r.method} ${r.path}${r.label && r.label !== "raw" ? ` · ${r.label}` : ""}`),
    el("td", {}, r.error ? "error" : r.cancelled ? "stopped" : String(r.status)),
    el("td", {}, `${r.ms_ttfb ?? "–"} / ${r.ms_total ?? "–"} ms`)))
    : [el("tr", {}, el("td", { colspan: 5, class: "muted" }, "No requests yet."))]));
}

async function showHistory(rid) {
  let r;
  try { r = await api("GET", `/api/llm/history/${rid}`); } catch (e) { toast(e.message, true); return; }
  L.historyItem = r;
  $("#llm-history-detail").hidden = false;
  $("#llm-hd-title").textContent = `${r.method} ${r.path} → ${r.error || r.status} (${r.ms_total} ms)`;
  $("#llm-hd-request").textContent = `${r.method} ${r.url}\n` + Object.entries(r.request_headers).map(([k, v]) => `${k}: ${v}`).join("\n")
    + (r.request_body ? `\n\n${pretty(r.request_body)}` : "");
  $("#llm-hd-response-head").textContent = `HTTP ${r.status}${r.error ? `\nError: ${r.error}` : ""}`;
  $("#llm-hd-response-headers").replaceChildren(kvTable(r.response_headers));
  const lines = r.lines ? r.response.split("\n").filter(Boolean).map(data => ({ t: 0, data })) : null;
  showJson($("#llm-hd-response"), r.response, { lines });
  $("#llm-history-detail").scrollIntoView({ block: "nearest" });
}

// ---- tools & setup ------------------------------------------------------------------------
function openTool(tool) {
  if (!TOOLS[tool]) tool = "chat";
  L.tool = tool;
  store("tool", tool);
  for (const li of $$("#llm-nav li")) li.classList.toggle("active", li.dataset.tool === tool);
  for (const t of Object.keys(TOOLS)) $(`#llm-${t}-panel`).hidden = t !== tool;
  if (tool === "models") loadModels();
  if (tool === "history") loadHistory();
  if (tool === "servers") loadServers();
  if (tool === "chat") updatePreview();
}

function buildParams() {
  $("#llm-params").replaceChildren(...PARAMS.map(p => el("label", { title: p.title || "" }, p.label,
    p.type === "bool"
      ? el("select", { id: `llm-p-${p.key}` }, el("option", { value: "" }, p.def ? "default (on)" : "not sent"), el("option", { value: "on" }, "on"), el("option", { value: "off" }, "off"))
      : el("input", { id: `llm-p-${p.key}`, type: p.type === "list" ? "text" : "number", step: p.step || 1, placeholder: "–" }))));
}

PG.register({
  id: "llm",
  async setup() {
    buildParams();
    restoreForm();
    for (const li of $$("#llm-nav li")) {
      li.onclick = () => openTool(li.dataset.tool);
      li.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTool(li.dataset.tool); } };
    }
    $("#llm-server").onchange = e => selectServer(e.target.value);
    $("#llm-model").addEventListener("input", e => { e.target.dataset.typed = "1"; });
    $("#llm-model").addEventListener("change", modelChanged);
    $("#llm-model").addEventListener("focus", () => loadModels()); // the list fills in if it wasn't loaded yet
    $("#llm-models-refresh").onclick = async () => { await loadModels(true); loadModelInfo($("#llm-model").value.trim(), true); };
    $("#llm-models-load").onclick = () => loadModels(true);
    $("#llm-flavor").onchange = () => { if (L.sid) store(`flavor.${L.sid}`, $("#llm-flavor").value); updatePreview(); };
    for (const node of $$("#llm-chat-panel input, #llm-chat-panel select, #llm-chat-panel textarea")) {
      if (node.id !== "llm-req-body") node.addEventListener("input", updatePreview);
    }
    $("#llm-raw-edit").onchange = e => { $("#llm-req-body").readOnly = !e.target.checked; updatePreview(); };
    $("#llm-send").onclick = () => send();
    $("#llm-regen").onclick = () => send(true);
    $("#llm-stop").onclick = stopExchange;
    $("#llm-http-stop").onclick = stopExchange;
    $("#llm-add").onclick = addMessage;
    $("#llm-count").onclick = countTokens;
    $("#llm-clear").onclick = () => { L.messages = []; L.usedTokens = null; renderMessages(); renderCtx(); saveForm(); updatePreview(); };
    $("#llm-input").addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } });
    $("#llm-copy-curl").onclick = () => {
      const s = server();
      if (!s) return;
      const headers = { "Content-Type": "application/json", ...(s.headers || {}) };
      if (s.api_key_present) headers[s.auth === "x-api-key" ? "x-api-key" : "Authorization"] = "secret";
      if (s.kind === "anthropic" || s.auth === "x-api-key") headers["anthropic-version"] = "2023-06-01";
      copy(curl("POST", s.base_url + currentPath(), headers, $("#llm-req-body").value), "curl command");
    };
    for (const b of $$("#llm-view .llm-tabs button")) b.onclick = () => selectTab(b.dataset.tab);
    $("#llm-probe-run").onclick = runProbe;
    $("#llm-http-form").onsubmit = sendHttp;
    $("#llm-history-reload").onclick = loadHistory;
    $("#llm-history-clear").onclick = async () => { await api("DELETE", "/api/llm/history"); $("#llm-history-detail").hidden = true; loadHistory(); };
    $("#llm-hd-curl").onclick = () => { const r = L.historyItem; if (r) copy(curl(r.method, r.url, r.request_headers, r.request_body), "curl command"); };
    $("#llm-hd-copy").onclick = () => { if (L.historyItem) copy(L.historyItem.response, "Response"); };
    $("#llm-hd-http").onclick = () => {
      const r = L.historyItem, f = $("#llm-http-form");
      if (!r) return;
      if (r.server !== L.sid) selectServer(r.server);
      f.elements.method.value = r.method;
      f.elements.path.value = r.path;
      $("#llm-http-body").value = r.request_body ? pretty(r.request_body) : "";
      openTool("http");
    };
    $("#llm-server-form").onsubmit = submitServer;
    $("#llm-server-cancel").onclick = resetServerForm;
    $("#llm-server-new").onclick = newServer;
    $("#llm-server-test-btn").onclick = testServer;
    $("#llm-key-mode").onchange = e => setKeyMode(e.target.value);
    const addServer = () => { openTool("servers"); newServer(); };
    $("#llm-server-add").onclick = addServer;
    $("#llm-server-add").onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); addServer(); } };
    $("#llm-scan").onclick = scanLocal;
    $("#llm-kind").onchange = kindChanged;
    selectTab(load("tab", "request"));
    try {
      const meta = await api("GET", "/api/llm/meta");
      L.kinds = meta.kinds;
      L.probes = meta.probes;
    } catch (e) { toast(e.message, true); }
    $("#llm-kind").replaceChildren(...Object.entries(L.kinds).map(([id, k]) => el("option", { value: id }, k.label)));
    $("#llm-url-presets").replaceChildren(...[...new Set(Object.values(L.kinds).map(k => k.url))].map(u => el("option", { value: u })));
    $("#llm-server-form").elements.auth.value = "none";
    kindHint();
    updatePaths();
    renderMessages();
    await loadServers();
    const tool = load("tool", "chat");
    L.tool = TOOLS[tool] ? tool : "chat";
    L.ready = true;
    if (L.active) openTool(L.tool);
    if (L.sid) await selectServer(L.sid);
    loadHistory();
  },
  activate() {
    L.active = true;
    if (!L.ready) return; // setup finishes the job
    openTool(L.tool);
    // Servers may have changed elsewhere (another tab, a restart): refresh, then fill the model list.
    loadServers().then(() => { if (L.sid && !L.models[L.sid]) selectServer(L.sid); });
  },
  deactivate() {
    L.active = false;
  },
});
})();
