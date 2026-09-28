"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const STATUS_LABEL = {
  unplanned: "Unplanned", planning: "Planning…", drafted: "Drafted", planned: "Planned", queued: "Queued",
  running: "Running…", done: "Done", failed: "Failed",
};
const COLUMN_OF = {
  unplanned: "unplanned", planning: "unplanned", drafted: "drafted", planned: "planned",
  queued: "queue", running: "queue", done: "finished", failed: "finished",
};
// Not locked by the AI and not finished; mirrors LOCKED in app.py.
const ACTIONABLE = new Set(["unplanned", "drafted", "planned", "queued", "failed"]);
const BUSY = new Set(["planning", "running"]);

const state = {
  projects: [],
  pid: localStorageGet("pg.pid"),
  tab: validTab(localStorageGet("pg.tab")),
  tasks: [],
  openTid: null,
  formStamp: null, // server values of the editable fields when the drawer form was filled
  questionStamp: null, // questions shown in the drawer, so polling doesn't wipe typed answers
  dirty: false,
  selected: new Set(), // task ids ticked for batch actions; always within the current tab
  lastSelected: null, // anchor for shift-click range selection
};

function validTab(col) { return Object.values(COLUMN_OF).includes(col) ? col : "unplanned"; }
function localStorageGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function localStorageSet(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } }

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

async function api(method, url, body) {
  const opts = { method, headers: { "X-PatchGoblin": "1" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  return data;
}

function toast(message, isError = false) {
  const t = $("#toast");
  t.textContent = message;
  t.classList.toggle("error", isError);
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, isError ? 6000 : 2500);
}

function showError(node, message) {
  node.textContent = message || "";
  node.hidden = !message;
}

function relTime(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

// Timestamps have one-second resolution, so compare field contents to detect server changes.
const formStamp = t => JSON.stringify([t.title, t.description, t.provider, t.plan_model || "",
  t.code_model || "", t.plan]);

const currentProject = () => state.projects.find(p => p.id === state.pid);
const openTask = () => state.tasks.find(t => t.id === state.openTid);

/* ---------------- model dropdowns ---------------- */

const MODELS = JSON.parse(document.body.dataset.models || "{}");
let PROVIDERS = JSON.parse(document.body.dataset.providers || "[]");
const CUSTOM_MODEL = "\u0000custom";
const CLI = new Set(["claude", "codex"]);

// Endpoint model lists are fetched from the API once per session and merged into MODELS.
const modelFetch = new Map(); // provider -> Promise
const modelLoading = new Set();
const modelErrors = {};

const isEndpoint = id => !!id && !CLI.has(id) && PROVIDERS.some(p => p.id === id);

function providerName(id) {
  const p = PROVIDERS.find(x => x.id === id);
  return p ? p.name : `${id} (missing)`;
}

// Selects a provider, keeping a removed endpoint visible as "(missing)" instead of silently switching.
function setProviderValue(select, id) {
  for (const opt of $$("option.missing", select)) opt.remove();
  if (id && ![...select.options].some(o => o.value === id)) {
    select.append(el("option", { value: id, class: "missing" }, `${id} (missing)`));
  }
  select.value = id;
}

function renderProviderSelects() {
  for (const select of $$(".provider-select")) {
    const value = select.value;
    const blank = [...select.options].find(o => o.value === "");
    select.replaceChildren(...(blank ? [el("option", { value: "" }, blank.textContent)] : []),
      ...PROVIDERS.map(p => el("option", { value: p.id }, p.name)));
    setProviderValue(select, value);
  }
}

function ensureModels(provider, refresh = false) {
  if (!isEndpoint(provider)) return Promise.resolve();
  if (!refresh && modelFetch.has(provider)) return modelFetch.get(provider);
  modelLoading.add(provider);
  const job = api("GET", `/api/endpoints/${encodeURIComponent(provider)}/models${refresh ? "?refresh=1" : ""}`)
    .then(data => {
      MODELS[provider] = [...new Set([...(MODELS[provider] || []), ...data.models])];
      modelErrors[provider] = data.error || "";
    })
    .catch(e => { modelErrors[provider] = e.message; modelFetch.delete(provider); })
    .finally(() => modelLoading.delete(provider));
  modelFetch.set(provider, job);
  return job;
}

// Fills the select now, then again once the endpoint's live model list arrives.
async function fillModelSelectLive(select, provider, current, stillValid = () => true, blankLabel = undefined) {
  fillModelSelect(select, provider, current, blankLabel);
  if (!isEndpoint(provider) || (modelFetch.has(provider) && !modelLoading.has(provider))) return;
  const job = ensureModels(provider);
  fillModelSelect(select, provider, current);
  await job;
  if (stillValid()) fillModelSelect(select, provider, select.dataset.value);
}

// The blank option's text says what blank falls back to ("Global default", "Project default"…);
// it is remembered on the select so later refills keep it.
function fillModelSelect(select, provider, current = "", blankLabel = select.dataset.blank || "default") {
  select.dataset.blank = blankLabel;
  const models = [...(MODELS[provider] || [])];
  if (current && !models.includes(current)) models.push(current);
  select.replaceChildren(el("option", { value: "" }, blankLabel),
    ...models.map(m => el("option", { value: m }, m)),
    modelLoading.has(provider) ? el("option", { value: "", disabled: true }, "Loading models…") : null,
    el("option", { value: CUSTOM_MODEL }, "Custom…"));
  select.value = current;
  select.dataset.value = current;
  select.title = modelErrors[provider] ? `Couldn't list models: ${modelErrors[provider]}` : "";
}

// Resolves the select's new value, asking for a name when "Custom…" is picked.
// Returns null (and restores the previous choice) if the prompt is cancelled.
function pickModel(select, provider) {
  if (select.value !== CUSTOM_MODEL) {
    select.dataset.value = select.value;
    return select.value;
  }
  const name = (prompt("Model name:", select.dataset.value) || "").trim();
  if (!name) { select.value = select.dataset.value; return null; }
  fillModelSelect(select, provider, name);
  return name;
}

// A model belongs to its provider, so drop it when switching to a provider that doesn't list it.
const modelFor = (provider, model) => ((MODELS[provider] || []).includes(model) ? model : "");

/* ---------------- projects ---------------- */

async function loadProjects() {
  const data = await api("GET", "/api/projects");
  state.projects = data.projects;
  if (!currentProject()) state.pid = state.projects[0] ? state.projects[0].id : null;
  renderProjects();
  await selectProject(state.pid);
}

function renderProjects() {
  const list = $("#project-list");
  list.replaceChildren(...state.projects.map(p => el("li", {
    class: p.id === state.pid ? "active" : "",
    onclick: () => selectProject(p.id),
  }, el("div", { class: "p-title" }, p.name),
     el("div", { class: "p-sub" }, p.location === "ssh" ? `ssh · ${p.ssh_target}` : "local"))));
  const has = state.projects.length > 0;
  $("#empty-state").hidden = has;
  $("#project-view").hidden = !has;
}

async function selectProject(pid) {
  if (pid !== state.pid) closeDrawer();
  if (pid !== state.pid) clearSelection(false);
  state.pid = pid;
  if (pid) localStorageSet("pg.pid", pid);
  renderProjects();
  const p = currentProject();
  if (!p) { closeChat(); return; }
  $("#p-name").textContent = p.name;
  $("#p-where").textContent = p.location === "ssh"
    ? `${p.ssh_target}${p.ssh_port ? ":" + p.ssh_port : ""}:${p.path}` : p.path;
  renderProjectModel();
  $("#p-plan-limit").value = p.plan_limit || "";
  $("#p-rewrite-titles").checked = p.rewrite_titles !== false;
  $("#p-auto-sync").checked = p.auto_sync === true;
  state.tasks = [];
  renderBoard();
  loadChat();
  await loadTasks();
}

function renderProjectModel() {
  const p = currentProject();
  if (!p) return;
  const provider = p.provider || "claude";
  setProviderValue($("#p-provider"), provider);
  $("#p-model-refresh").hidden = !isEndpoint(provider);
  const stillValid = () => currentProject() === p && (p.provider || "claude") === provider;
  fillModelSelectLive($("#p-plan-model"), provider, p.plan_model || "", stillValid, "Global default");
  fillModelSelectLive($("#p-code-model"), provider, p.code_model || "", stillValid, "Global default");
  fillModelSelectLive($("#c-model"), provider, p.chat_model || "", stillValid, "Planning model");
  fillBatchModel($("#batch-plan-model"), provider);
  fillBatchModel($("#batch-code-model"), provider);
  renderChatWhere();
}

// Refills the project's model selects from MODELS without fetching (after a refresh or provider change).
function refillProjectModels(p, provider) {
  fillModelSelect($("#p-plan-model"), provider, p.plan_model || "");
  fillModelSelect($("#p-code-model"), provider, p.code_model || "");
  fillModelSelect($("#c-model"), provider, p.chat_model || "");
}

async function updateProject(fields) {
  const p = currentProject();
  try {
    const updated = await api("PATCH", `/api/projects/${p.id}`, fields);
    Object.assign(p, updated);
    toast("Project updated");
  } catch (e) { toast(e.message, true); }
}

/* ---------------- tasks & board ---------------- */

async function loadTasks() {
  const pid = state.pid;
  if (!pid) return;
  try {
    const data = await api("GET", `/api/projects/${pid}/tasks`);
    if (pid !== state.pid) return;
    state.tasks = data.tasks;
    showError($("#p-error"), "");
  } catch (e) {
    if (pid === state.pid) showError($("#p-error"), `Could not load tasks: ${e.message}`);
    return;
  }
  renderBoard();
  if (state.openTid !== null) renderDrawer(false);
}

function sortTasks(col, tasks) {
  const by = (f, dir = 1) => (a, b) => ((a[f] || "") < (b[f] || "") ? -dir : (a[f] || "") > (b[f] || "") ? dir : 0);
  if (col === "queue") {
    return tasks.sort((a, b) => (a.status === "running" ? -1 : b.status === "running" ? 1 : by("queued_at")(a, b)));
  }
  if (col === "finished") return tasks.sort(by("finished_at", -1));
  return tasks.sort((a, b) => a.id - b.id);
}

function renderBoard() {
  for (const column of $$(".column")) {
    const col = column.dataset.col;
    const tasks = sortTasks(col, state.tasks.filter(t => COLUMN_OF[t.status] === col));
    const active = col === state.tab;
    $(".cards", column).replaceChildren(...(tasks.length ? tasks.map(renderCard)
      : [el("div", { class: "muted empty-col" }, "No tasks here")]));
    column.hidden = !active;

    // The badge counts only tasks the user can act on; the tooltip gives the full breakdown.
    const tab = $(`.queue-tab[data-col="${col}"]`);
    const actionable = tasks.filter(t => ACTIONABLE.has(t.status)).length;
    const byStatus = {};
    for (const t of tasks) byStatus[t.status] = (byStatus[t.status] || 0) + 1;
    const parts = [`${actionable} actionable`, ...Object.entries(byStatus)
      .filter(([s]) => !ACTIONABLE.has(s))
      .map(([s, n]) => `${n} ${STATUS_LABEL[s].replace("…", "").toLowerCase()}`)];
    const summary = `${tab.dataset.label}: ${parts.join(", ")} (${tasks.length} total)`;
    const count = $(".count", tab);
    count.textContent = actionable || "";
    count.classList.toggle("empty", !actionable);
    $(".activity", tab).hidden = !tasks.some(t => BUSY.has(t.status));
    tab.title = summary;
    tab.setAttribute("aria-label", summary);
    tab.setAttribute("aria-selected", String(active));
    tab.classList.toggle("active", active);
    tab.tabIndex = active ? 0 : -1;
  }
  renderBatchBar();
}

function selectTab(col, focus = false) {
  if (validTab(col) !== state.tab) clearSelection(false);
  state.tab = validTab(col);
  localStorageSet("pg.tab", state.tab);
  renderBoard();
  if (focus) $(`.queue-tab[data-col="${state.tab}"]`).focus();
}

function onTabKeydown(e) {
  const tabs = $$(".queue-tab").map(b => b.dataset.col);
  const i = tabs.indexOf(state.tab);
  const next = { ArrowLeft: tabs[(i - 1 + tabs.length) % tabs.length],
    ArrowRight: tabs[(i + 1) % tabs.length], Home: tabs[0], End: tabs[tabs.length - 1] }[e.key];
  if (!next) return;
  e.preventDefault();
  selectTab(next, true);
}

// Statuses in which the plan can still be refined, so its questions still matter.
const PLANNABLE = new Set(["unplanned", "drafted", "planned", "failed"]);
const hasOpenQuestions = t => PLANNABLE.has(t.status) && (t.questions || []).length > 0;

function renderCard(t) {
  const p = currentProject();
  const selected = state.selected.has(t.id);
  return el("div", {
    class: `card status-${t.status}${t.id === state.openTid ? " open" : ""}${selected ? " selected" : ""}`,
    tabindex: "0",
    "data-tid": String(t.id),
    onclick: () => openDrawer(t.id),
    onkeydown: e => {
      if (e.target !== e.currentTarget) return;
      if (e.key === "Enter") openDrawer(t.id);
      if (e.key === " ") { e.preventDefault(); toggleSelect(t.id, !selected, e.shiftKey, true); }
    },
  },
  el("input", {
    type: "checkbox", class: "select", checked: selected, "aria-label": `Select task #${t.id}`,
    title: "Select for batch actions (Shift+click selects a range)",
    onclick: e => { e.stopPropagation(); toggleSelect(t.id, e.target.checked, e.shiftKey, false); },
    onkeydown: e => e.stopPropagation(),
  }),
  el("div", { class: "card-top" },
    el("span", { class: "tid" }, `#${t.id}`),
    el("span", { class: `badge ${t.status}` }, STATUS_LABEL[t.status])),
  el("div", { class: "card-title" }, t.title),
  el("div", { class: "card-meta" },
    t.provider && t.provider !== p.provider ? el("span", { class: "chip" }, providerName(t.provider)) : null,
    t.plan_model ? el("span", { class: "chip", title: "Planning model for this task" }, `plan: ${t.plan_model}`) : null,
    t.code_model ? el("span", { class: "chip", title: "Coding model for this task" }, `code: ${t.code_model}`) : null,
    hasOpenQuestions(t) ? el("span", { class: "chip question", title: t.questions.join("\n") },
      `? ${t.questions.length} question${t.questions.length === 1 ? "" : "s"}`) : null,
    t.error && t.status !== "failed" ? el("span", { class: "chip warn", title: t.error }, "last attempt failed") : null,
    t.commit ? el("span", { class: "chip mono" }, t.commit.slice(0, 7)) : null,
    el("span", { class: "muted" }, relTime(t.updated_at))));
}

async function createTask(ev) {
  ev.preventDefault();
  const title = $("#nt-title").value.trim();
  if (!title) return;
  try {
    const task = await api("POST", `/api/projects/${state.pid}/tasks`,
      { title, description: $("#nt-desc").value.trim() });
    $("#nt-title").value = "";
    $("#nt-desc").value = "";
    state.tasks.push(task);
    if (state.tab !== "unplanned") selectTab("unplanned");
    else renderBoard();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- batch actions ---------------- */

// Mirrors the drawer's per-status buttons and TRANSITIONS in app.py. An optional `when`
// narrows the eligible tasks further.
const BATCH_ACTIONS = [
  { action: "plan", label: "Plan with AI", from: ["unplanned", "drafted", "planned", "failed"] },
  { action: "mark_planned", label: "Mark planned", from: ["unplanned", "drafted", "failed"] },
  { action: "mark_drafted", label: "Move to drafted", from: ["planned"], when: hasOpenQuestions },
  { action: "queue", label: "Queue for AI", from: ["drafted", "planned", "failed"] },
  { action: "dequeue", label: "Remove from queue", from: ["queued"] },
  { action: "unplan", label: "Back to unplanned", from: ["drafted", "planned"] },
  { action: "reopen", label: "Reopen", from: ["done"] },
  { action: "cancel", label: "Cancel", from: ["planning", "running"], cls: "danger" },
];
const NOT_BUSY = ["unplanned", "drafted", "planned", "queued", "done", "failed"];
const BATCH_VERB = { delete: "Deleted", set_provider: "Updated", set_models: "Updated", plan: "Started planning",
  mark_planned: "Marked planned", mark_drafted: "Moved to drafted", queue: "Queued", dequeue: "Removed from queue",
  unplan: "Moved back", reopen: "Reopened", cancel: "Cancelled" };

const tabTasks = () => sortTasks(state.tab, state.tasks.filter(t => COLUMN_OF[t.status] === state.tab));
const selectedTasks = () => state.tasks.filter(t => state.selected.has(t.id));
const eligible = (from, when) => selectedTasks().filter(t => from.includes(t.status) && (!when || when(t)));

function clearSelection(render = true) {
  state.selected.clear();
  state.lastSelected = null;
  if (render) renderBoard();
}

function toggleSelect(tid, on, range, focus) {
  const ids = tabTasks().map(t => t.id);
  const from = ids.indexOf(state.lastSelected);
  const to = ids.indexOf(tid);
  const span = range && from >= 0 && to >= 0
    ? ids.slice(Math.min(from, to), Math.max(from, to) + 1) : [tid];
  for (const id of span) on ? state.selected.add(id) : state.selected.delete(id);
  state.lastSelected = tid;
  renderBoard();
  if (focus) { const card = $(`.card[data-tid="${tid}"]`); if (card) card.focus(); }
}

function renderBatchBar() {
  const bar = $("#batch-bar");
  // Drop ids that left the tab (status changed) or were deleted.
  const inTab = new Set(tabTasks().map(t => t.id));
  for (const id of state.selected) if (!inTab.has(id)) state.selected.delete(id);
  const n = state.selected.size;
  bar.hidden = n === 0;
  if (!n) return;

  $("#batch-count").textContent = `${n} selected`;
  const all = $("#batch-all");
  all.checked = n === inTab.size;
  all.indeterminate = n > 0 && n < inTab.size;
  all.title = all.checked ? "Select none" : "Select all in this tab";

  $("#batch-actions").replaceChildren(...BATCH_ACTIONS.map(a => {
    const count = eligible(a.from, a.when).length;
    return count ? actionButton(`${a.label} (${count})`, () => doBatch(a.action), `small ${a.cls || ""}`) : null;
  }).filter(Boolean));

  const editable = eligible(NOT_BUSY).length;
  $(".batch-provider", bar).hidden = !editable;
  $("#batch-provider-btn").textContent = `Set AI (${editable})`;
  $(".batch-models", bar).hidden = !editable;
  $("#batch-models-btn").textContent = `Set models (${editable})`;
  const del = $("#batch-delete");
  del.hidden = !editable;
  del.textContent = `Delete (${editable})`;
}

async function doBatch(action, extra = {}) {
  const label = action === "delete" ? "Delete" : action === "set_provider" ? "Set AI"
    : action === "set_models" ? "Set models" : BATCH_ACTIONS.find(a => a.action === action).label;
  const spec = BATCH_ACTIONS.find(a => a.action === action);
  const targets = eligible(spec?.from || NOT_BUSY, spec?.when);
  if (!targets.length) return;
  const n = targets.length;
  const many = `${n} task${n === 1 ? "" : "s"}`;
  if (action === "delete" && !confirm(`Delete ${many} permanently?\n\n${targets.map(t => `#${t.id} ${t.title}`).join("\n")}`)) return;
  if (action === "cancel" && !confirm(`Cancel the AI job for ${many}?`)) return;
  if (action === "queue") {
    const asking = targets.filter(hasOpenQuestions).length;
    if (asking && !confirm(`${asking} of these plans still have unanswered questions. Queue anyway?`)) return;
  }
  const body = { action, ids: targets.map(t => t.id), ...extra };
  if (action === "plan") {
    const feedback = prompt(`Plan ${many} with AI.\nOptional feedback for the AI (applies to every task):`, "");
    if (feedback === null) return;
    body.feedback = feedback;
  }
  let data;
  try {
    data = await api("POST", `/api/projects/${state.pid}/tasks/batch`, body);
  } catch (e) { toast(e.message, true); return; }

  state.tasks = data.tasks;
  const ok = data.results.filter(r => r.ok);
  const failed = data.results.filter(r => !r.ok);
  for (const r of ok) state.selected.delete(r.id);
  const affectsDrawer = state.openTid !== null && ok.some(r => r.id === state.openTid);
  if (affectsDrawer) state.formStamp = null;
  renderBoard();
  if (state.openTid !== null) renderDrawer(false);

  let message = `${BATCH_VERB[action] || label} ${ok.length}`;
  if (failed.length) {
    message += `, skipped ${failed.length}: ` + failed.slice(0, 3).map(r => `#${r.id} ${r.error}`).join("; ")
      + (failed.length > 3 ? "; …" : "");
  }
  if (affectsDrawer && state.dirty) message += " (the open task has unsaved edits; they were kept)";
  toast(message, failed.length > 0);
}

function setupBatchBar() {
  $("#batch-all").onchange = e => {
    if (e.target.checked) for (const t of tabTasks()) state.selected.add(t.id);
    else state.selected.clear();
    state.lastSelected = null;
    renderBoard();
  };
  $("#batch-clear").onclick = () => clearSelection();
  $("#batch-delete").onclick = () => doBatch("delete");
  $("#batch-provider-btn").onclick = () => doBatch("set_provider", { provider: $("#batch-provider").value });
  const batchModels = { plan_model: $("#batch-plan-model"), code_model: $("#batch-code-model") };
  for (const select of Object.values(batchModels)) {
    select.onchange = () => {
      const provider = currentProject().provider || "claude";
      if (select.value !== CUSTOM_MODEL) { select.dataset.value = select.value; return; }
      const name = (prompt("Model name:", "") || "").trim();
      fillBatchModel(select, provider, name || select.dataset.value);
    };
  }
  $("#batch-models-btn").onclick = () => {
    const extra = {};
    for (const [key, select] of Object.entries(batchModels)) {
      if (select.value !== KEEP_MODEL) extra[key] = select.value;
    }
    if (!Object.keys(extra).length) { toast("Choose a planning or coding model to set", true); return; }
    doBatch("set_models", extra);
  };
}

// Batch model selects: "unchanged" (not sent), "project default" (clears the override) or a model.
const KEEP_MODEL = "\u0000keep";
const KEEP_LABEL = { "batch-plan-model": "Planning: unchanged", "batch-code-model": "Coding: unchanged" };

function fillBatchModel(select, provider, current = KEEP_MODEL) {
  fillModelSelect(select, provider, current === KEEP_MODEL ? "" : current, "Project default");
  select.prepend(el("option", { value: KEEP_MODEL }, KEEP_LABEL[select.id]));
  select.value = current;
  select.dataset.value = current;
}

/* ---------------- drawer ---------------- */

function openDrawer(tid) {
  if (state.openTid !== tid && state.dirty && !confirm("Discard unsaved changes to the open task?")) return;
  closeChat();
  state.openTid = tid;
  state.dirty = false;
  state.questionStamp = null;
  $("#d-feedback").value = "";
  $("#drawer").hidden = false;
  renderDrawer(true);
  renderBoard();
}

function closeDrawer() {
  if (state.dirty && !confirm("Discard unsaved changes?")) return false;
  state.openTid = null;
  state.dirty = false;
  $("#drawer").hidden = true;
  renderBoard();
  return true;
}

// A press counts as "outside" a panel unless it lands in it, in an open dialog, or on the toast.
function isOutside(panel, target) {
  return target.isConnected && !panel.contains(target) && !target.closest("dialog[open], #toast");
}

function actionButton(label, fn, cls = "", title = undefined) {
  return el("button", { type: "button", class: cls, onclick: fn, title }, label);
}

// Save/Discard only exist while there are unsaved edits, next to the fields they save.
function renderEditActions() {
  const t = openTask();
  const locked = !t || BUSY.has(t.status);
  $("#d-edit-actions").hidden = !state.dirty || locked;
}

function renderQuestions(t, canPlan) {
  const questions = t.questions || [];
  $("#d-questions").hidden = !canPlan || !questions.length;
  const stamp = JSON.stringify([t.id, questions]);
  if (stamp === state.questionStamp) return;
  state.questionStamp = stamp;
  $("#d-question-list").replaceChildren(...questions.map((q, i) => el("li", {},
    el("label", { for: `d-q-${i}`, class: "question-text" }, q),
    el("textarea", { id: `d-q-${i}`, rows: "1", "data-q": String(i), placeholder: "Your answer…" }))));
}

async function answerQuestions() {
  const t = openTask();
  if (!t) return;
  const answers = $$("#d-question-list textarea").map(box => ({
    question: t.questions[Number(box.dataset.q)] || "", answer: box.value.trim(),
  })).filter(a => a.answer);
  if (!answers.length) { toast("Type an answer to at least one question first.", true); return; }
  await doAction("plan", { answers });
}

function renderDrawer(fillForm) {
  const t = openTask();
  if (!t) { state.openTid = null; state.dirty = false; $("#drawer").hidden = true; return; }
  const locked = t.status === "planning" || t.status === "running";

  $("#d-id").textContent = `#${t.id}`;
  const badge = $("#d-status");
  badge.textContent = STATUS_LABEL[t.status];
  badge.className = `badge ${t.status}`;

  // Only refill the form when the task changed on the server and the user has no edits.
  if (fillForm || (!state.dirty && state.formStamp !== formStamp(t))) {
    $("#d-title").value = t.title;
    $("#d-desc").value = t.description || "";
    setProviderValue($("#d-provider"), t.provider || "");
    fillTaskModels(t.plan_model || "", t.code_model || "");
    $("#d-plan").value = t.plan || "";
    state.formStamp = formStamp(t);
    state.dirty = false;
  }
  for (const id of ["#d-title", "#d-desc", "#d-provider", "#d-plan-model", "#d-code-model", "#d-plan"]) {
    $(id).disabled = locked;
  }

  showError($("#d-error"), t.error);

  const canPlan = PLANNABLE.has(t.status);
  $("#d-feedback-wrap").hidden = !canPlan;
  renderQuestions(t, canPlan);
  renderEditActions();
  const hasPlan = (t.plan || "").trim().length > 0;
  // With open questions, answering them is the main way forward; plain refining is secondary.
  const asking = hasOpenQuestions(t);
  const planLabel = hasPlan ? "Refine plan with AI" : "Plan with AI";
  const planTip = "The AI drafts a new plan (read-only), using any feedback below. Unsaved edits are saved first.";
  const queueTip = "The AI will implement this plan and commit the result. Unsaved edits are saved first.";
  const skipTip = "Accept the plan as written without asking the AI";

  const A = [];
  const act = action => () => doAction(action);
  switch (t.status) {
    case "unplanned":
      A.push(actionButton(planLabel, act("plan"), asking ? "ghost" : "primary", planTip));
      A.push(actionButton("Mark planned (skip AI)", act("mark_planned"), "ghost", skipTip));
      break;
    case "planning":
      A.push(actionButton("Cancel planning", act("cancel"), "danger"));
      break;
    case "drafted":
      // Answering the questions (above) is the main way forward.
      A.push(actionButton(planLabel, act("plan"), "ghost", planTip));
      A.push(actionButton("Mark planned", act("mark_planned"), "ghost",
        "Accept the plan once its questions are removed. Unsaved edits are saved first."));
      A.push(actionButton("Queue anyway", act("queue"), "ghost", queueTip));
      A.push(actionButton("Back to unplanned", act("unplan"), "ghost"));
      A.push(el("span", { class: "muted small" },
        "Answer the questions, or delete them from the plan, save, and click Mark planned."));
      break;
    case "planned":
      A.push(actionButton("Queue to run", act("queue"), asking ? "" : "primary", queueTip));
      A.push(actionButton(planLabel, act("plan"), "ghost", planTip));
      if (asking) {
        A.push(actionButton("Move to drafted", act("mark_drafted"), "ghost",
          "The plan has open questions; park it in Drafted until they're answered"));
      }
      A.push(actionButton("Back to unplanned", act("unplan"), "ghost"));
      break;
    case "queued":
      A.push(actionButton("Remove from queue", act("dequeue")));
      break;
    case "running":
      A.push(actionButton("Cancel run", act("cancel"), "danger"));
      break;
    case "done":
      A.push(actionButton("Reopen", act("reopen")));
      break;
    case "failed":
      A.push(actionButton("Queue to run again", act("queue"), asking ? "" : "primary", queueTip));
      A.push(actionButton(planLabel, act("plan"), "ghost", planTip));
      A.push(actionButton("Mark planned (skip AI)", act("mark_planned"), "ghost", skipTip));
      break;
  }
  $("#d-flow-actions").replaceChildren(...A);
  $("#d-danger-actions").replaceChildren(...(locked ? []
    : [actionButton("Delete task", deleteTask, "ghost danger small", "Delete this task permanently")]));

  const commit = $("#d-commit");
  commit.hidden = !t.commit;
  commit.textContent = t.commit ? `Committed as ${t.commit.slice(0, 12)}` : "";

  $("#d-output-wrap").hidden = !t.output || t.active;
  $("#d-output").textContent = t.output || "";
  $("#d-live-wrap").hidden = !t.active;

  $("#d-history").replaceChildren(...[...t.history].reverse().map(h =>
    el("li", {}, el("span", { class: "muted" }, new Date(h.at).toLocaleString()), " ", h.event)));

  if (t.active && !pollLive.timer) pollLive();
}

// The drawer's model selects list the models of the task's effective provider. Blank falls back
// to the project's model only when the task uses the project's AI.
function fillTaskModels(planModel, codeModel) {
  const p = currentProject();
  const projectProvider = (p && p.provider) || "claude";
  const provider = $("#d-provider").value || projectProvider;
  const blank = provider === projectProvider ? "Project default" : "Global default";
  const stillValid = () => ($("#d-provider").value || projectProvider) === provider && !$("#drawer").hidden;
  fillModelSelectLive($("#d-plan-model"), provider, planModel, stillValid, blank);
  fillModelSelectLive($("#d-code-model"), provider, codeModel, stillValid, blank);
}

function formFields() {
  return {
    title: $("#d-title").value,
    description: $("#d-desc").value,
    provider: $("#d-provider").value,
    plan_model: $("#d-plan-model").dataset.value || "",
    code_model: $("#d-code-model").dataset.value || "",
    plan: $("#d-plan").value,
  };
}

async function saveTask(quiet) {
  const t = openTask();
  if (!t) return false;
  try {
    const updated = await api("PATCH", `/api/projects/${state.pid}/tasks/${t.id}`, formFields());
    Object.assign(t, updated);
    state.dirty = false;
    state.formStamp = formStamp(t);
    if (quiet !== true) toast("Saved");
    renderBoard();
    renderDrawer(false);
    return true;
  } catch (e) {
    toast(e.message, true);
    return false;
  }
}

async function doAction(action, extra = {}) {
  const t = openTask();
  if (!t) return;
  if (action === "queue" && hasOpenQuestions(t)) {
    const n = t.questions.length;
    if (!confirm(`The plan still has ${n} unanswered question${n === 1 ? "" : "s"}. Queue anyway?`)) return;
  }
  if (state.dirty && action !== "cancel" && !(await saveTask(true))) return;
  const body = { action, ...extra };
  if (action === "plan") body.feedback = $("#d-feedback").value;
  try {
    const updated = await api("POST", `/api/projects/${state.pid}/tasks/${t.id}/action`, body);
    Object.assign(t, updated);
    if (action === "plan") {
      $("#d-feedback").value = "";
      for (const box of $$("#d-question-list textarea")) box.value = "";
    }
    state.formStamp = null;
    renderBoard();
    renderDrawer(false);
  } catch (e) { toast(e.message, true); }
}

async function deleteTask() {
  const t = openTask();
  if (!t || !confirm(`Delete task #${t.id} "${t.title}"?`)) return;
  try {
    await api("DELETE", `/api/projects/${state.pid}/tasks/${t.id}`);
    state.tasks = state.tasks.filter(x => x.id !== t.id);
    state.dirty = false;
    closeDrawer();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- live output ---------------- */

async function pollLive() {
  clearTimeout(pollLive.timer);
  pollLive.timer = null;
  const t = openTask();
  if (!t || !t.active) return;
  pollLive.timer = -1; // mark as running while the request is in flight
  try {
    const live = await api("GET", `/api/projects/${state.pid}/tasks/${t.id}/live`);
    if (state.openTid !== t.id) { pollLive.timer = null; return; }
    const pre = $("#d-live");
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent = live.output || "Waiting for output…";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
    $("#d-elapsed").textContent = live.active ? `${live.elapsed}s` : "finished";
    if (!live.active) { pollLive.timer = null; loadTasks(); return; }
  } catch { /* transient; next tick retries */ }
  pollLive.timer = setTimeout(pollLive, 1500);
}

/* ---------------- chat ---------------- */

const chat = { messages: [], active: false, timer: null };

function openChat() {
  if (!$("#drawer").hidden && !closeDrawer()) return;
  $("#chat").hidden = false;
  loadChat();
  $("#c-input").focus();
}

function closeChat() {
  $("#chat").hidden = true;
  clearTimeout(chat.timer);
  chat.timer = null;
}

async function loadChat() {
  clearTimeout(chat.timer);
  chat.timer = null;
  const pid = state.pid;
  if (!pid || $("#chat").hidden) return;
  try {
    const data = await api("GET", `/api/projects/${pid}/chat`);
    if (pid === state.pid) renderChat(data);
  } catch (e) { toast(e.message, true); }
  if (chat.active && !$("#chat").hidden) chat.timer = setTimeout(loadChat, 1500);
}

// Chat uses the project's chat model, else its planning model (else the global default).
function renderChatWhere() {
  const p = currentProject();
  const model = p ? p.chat_model || p.plan_model || "" : "";
  $("#c-where").textContent = p ? `${p.name} · ${providerName(p.provider || "claude")}${model ? " · " + model : ""}` : "";
}

function renderChat(data) {
  const log = $("#c-log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  renderChatWhere();
  chat.active = data.active;
  chat.messages = data.messages;
  $("#c-empty").hidden = data.messages.length > 0;
  $("#c-messages").replaceChildren(...data.messages.map(m => el("li", {
    class: `${m.role}${m.error ? " error" : ""}`,
  }, el("span", { class: "when" }, `${m.role === "user" ? "You" : "AI"} · ${new Date(m.at).toLocaleTimeString()}`),
     m.text)));
  $("#c-live-wrap").hidden = !data.active;
  $("#c-live").textContent = data.output || "Waiting for output…";
  $("#c-elapsed").textContent = data.active ? `${data.elapsed}s` : "";
  $("#c-send").disabled = data.active;
  $("#c-cancel").hidden = !data.active;
  $("#c-clear").disabled = data.active || !data.messages.length;
  if (atBottom) log.scrollTop = log.scrollHeight;
}

async function chatRequest(method, suffix, body) {
  try {
    renderChat(await api(method, `/api/projects/${state.pid}/chat${suffix}`, body));
    if (chat.active) loadChat();
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

async function sendChat(ev) {
  ev.preventDefault();
  const input = $("#c-input");
  const message = input.value.trim();
  if (!message || chat.active) return;
  $("#c-send").disabled = true;
  if (await chatRequest("POST", "", { message })) {
    input.value = "";
    $("#c-log").scrollTop = $("#c-log").scrollHeight;
  } else $("#c-send").disabled = false;
}

function setupChat() {
  $("#chat-btn").onclick = openChat;
  $("#chat-form").onsubmit = sendChat;
  $("#c-input").onkeydown = e => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) sendChat(e);
  };
  $("#c-cancel").onclick = () => chatRequest("POST", "/cancel");
  $("#c-clear").onclick = () => {
    if (confirm("Clear this conversation?")) chatRequest("DELETE", "");
  };
}

/* ---------------- dialogs ---------------- */

// Folder picker for the Add project dialog. Browsing happens on the server so it
// can list real absolute paths, on this machine or on the chosen SSH host.
function setupFolderBrowser(form) {
  const panel = $("#browser");
  const list = $("#br-list");
  const err = $("#br-error");
  const br = { path: null, parent: null, sep: "/", dirs: [] };

  const hostFields = () => ({
    location: form.location.value,
    ssh_target: form.ssh_target.value.trim(),
    ssh_port: form.ssh_port.value,
  });

  function render() {
    const pathLabel = $("#br-path");
    pathLabel.textContent = br.path === "" ? "Drives" : (br.path || "");
    pathLabel.title = br.path || "";
    $("#br-up").disabled = br.parent === null || br.parent === undefined;
    $("#br-use").disabled = !br.path;
    const showHidden = $("#br-hidden").checked;
    // Dot-folders and Windows system folders ($RECYCLE.BIN etc.) count as hidden.
    const dirs = br.dirs.filter(d => showHidden || !/^[.$]/.test(d.name));
    const open = d => go({ path: d.path });
    list.replaceChildren(...(dirs.length
      ? dirs.map(d => el("li", {
        role: "option", tabindex: "0", title: d.path,
        onclick: () => open(d),
        onkeydown: e => { if (e.key === "Enter") { e.preventDefault(); open(d); } },
      }, d.name))
      : [el("li", { class: "note" }, br.path === null ? "" : "No subfolders")]));
  }

  async function go(request) {
    const host = hostFields();
    if (host.location === "ssh" && !host.ssh_target) {
      Object.assign(br, { path: null, parent: null, dirs: [] });
      render();
      showError(err, "Enter the SSH target first.");
      return false;
    }
    list.replaceChildren(el("li", { class: "note" }, host.location === "ssh" ? "Connecting…" : "Loading…"));
    showError(err, "");
    try {
      Object.assign(br, await api("POST", "/api/browse", { ...host, ...request }));
      render();
      list.scrollTop = 0;
      return true;
    } catch (e) {
      showError(err, e.message);
      render();
      return false;
    }
  }

  function use() {
    if (!br.path) return;
    const sub = $("#br-new").value.trim().replace(/^[\\/]+/, "");
    form.path.value = !sub ? br.path : br.path.endsWith(br.sep) ? br.path + sub : br.path + br.sep + sub;
    panel.hidden = true;
    form.path.focus();
  }

  $("#browse-btn").onclick = async () => {
    if (!panel.hidden) { panel.hidden = true; return; }
    panel.hidden = false;
    const typed = form.path.value.trim();
    if (typed && await go({ path: typed })) return;
    if (await go({ home: true }) && typed) showError(err, `Couldn't open ${typed}; showing your home folder.`);
  };
  $("#br-up").onclick = () => go({ path: br.parent });
  $("#br-home").onclick = () => go({ home: true });
  $("#br-hidden").onchange = render;
  $("#br-use").onclick = use;
  $("#br-new").onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); use(); } };

  // Returns a reset function: the listing belongs to one host, so hide it when that changes.
  return () => {
    panel.hidden = true;
    Object.assign(br, { path: null, parent: null, dirs: [] });
    $("#br-new").value = "";
    showError(err, "");
    render();
  };
}

function setupProjectDialog() {
  const dialog = $("#project-dialog");
  const form = $("#project-form");
  const resetBrowser = setupFolderBrowser(form);
  const open = () => {
    form.reset();
    for (const select of [form.plan_model, form.code_model]) {
      fillModelSelectLive(select, form.provider.value, "", () => dialog.open, "Global default");
    }
    syncLocation();
    showError($("#project-form-error"), "");
    dialog.showModal();
  };
  const syncLocation = () => {
    const ssh = form.location.value === "ssh";
    $$(".ssh-only", form).forEach(n => { n.hidden = !ssh; });
    form.ssh_target.required = ssh;
    resetBrowser();
  };
  $("#add-project-btn").onclick = open;
  $("#empty-add-btn").onclick = open;
  $$("input[name=location]", form).forEach(r => { r.onchange = syncLocation; });
  form.provider.onchange = async () => {
    const provider = form.provider.value;
    const selects = [form.plan_model, form.code_model];
    const current = selects.map(s => s.dataset.value || "");
    for (const select of selects) fillModelSelect(select, provider, "");
    await ensureModels(provider);
    if (form.provider.value !== provider) return;
    selects.forEach((select, i) => fillModelSelect(select, provider, modelFor(provider, current[i])));
  };
  for (const select of [form.plan_model, form.code_model]) {
    select.onchange = () => pickModel(select, form.provider.value);
  }
  form.ssh_target.addEventListener("change", resetBrowser);
  form.ssh_port.addEventListener("change", resetBrowser);
  form.onsubmit = async ev => {
    ev.preventDefault();
    const btn = $("#project-submit");
    btn.disabled = true;
    btn.textContent = "Connecting…";
    const f = Object.fromEntries(new FormData(form));
    f.create = form.create.checked;
    try {
      const project = await api("POST", "/api/projects", f);
      dialog.close();
      state.projects.push(project);
      await selectProject(project.id);
      toast(`Added ${project.name}`);
    } catch (e) {
      showError($("#project-form-error"), e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Add project";
    }
  };
}

const SETTING_FIELDS = [
  "claude.plan", "claude.run", "codex.plan", "codex.run",
  "claude.plan_model", "claude.code_model", "codex.plan_model", "codex.code_model",
  "timeouts.plan", "timeouts.run",
];

/* ---------------- OpenAI-compatible endpoints ---------------- */

function keyStatus(ep) {
  const env = (ep.api_key_env || "").trim();
  if (ep.api_key_saved) return "Key saved (used)" + (env ? `; $${env} is ignored` : "");
  if (env) return ep.api_key_env_present ? `$${env} detected` : `$${env} not set in PatchGoblin's environment`;
  return "No key (keyless, e.g. a local server)";
}

function endpointBlock(ep = {}) {
  const node = $("#endpoint-tpl").content.firstElementChild.cloneNode(true);
  const f = name => $(`[data-f="${name}"]`, node);
  node.dataset.id = ep.id || "";
  f("name").value = ep.name || "";
  f("base_url").value = ep.base_url || "";
  f("api_key").placeholder = ep.api_key_saved ? "saved (leave blank to keep)" : "not set";
  f("api_key_clear").closest("label").hidden = !ep.api_key_saved;
  f("api_key_env").value = ep.api_key_env || "";
  f("key_status").textContent = ep.id ? keyStatus(ep) : "";
  f("model").value = ep.model || "";
  f("code_model").value = ep.code_model || "";
  f("models").value = (ep.models || []).join(" ");
  f("max_steps").value = ep.max_steps || 40;
  f("headers").value = Object.entries(ep.headers || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
  f("allow_commands").checked = !!ep.allow_commands;
  $('[data-act="remove"]', node).onclick = () => removeEndpoint(node);
  $('[data-act="test"]', node).onclick = () => testEndpoint(node);
  return node;
}

function parseHeaders(text) {
  const headers = {};
  for (const line of text.split("\n")) {
    const at = line.indexOf(":");
    if (at <= 0) continue;
    const name = line.slice(0, at).trim();
    if (name) headers[name] = line.slice(at + 1).trim();
  }
  return headers;
}

function readEndpoint(node) {
  const f = name => $(`[data-f="${name}"]`, node);
  return {
    id: node.dataset.id || "",
    name: f("name").value.trim(),
    base_url: f("base_url").value.trim(),
    api_key: f("api_key").value.trim(),
    api_key_clear: f("api_key_clear").checked,
    api_key_env: f("api_key_env").value.trim(),
    model: f("model").value.trim(),
    code_model: f("code_model").value.trim(),
    models: f("models").value.split(/[\s,]+/).filter(Boolean),
    max_steps: Number(f("max_steps").value) || 40,
    headers: parseHeaders(f("headers").value),
    allow_commands: f("allow_commands").checked,
  };
}

function removeEndpoint(node) {
  const id = node.dataset.id;
  const name = $('[data-f="name"]', node).value.trim() || id || "this endpoint";
  if (id) {
    const projects = state.projects.filter(p => p.provider === id).length;
    const tasks = state.tasks.filter(t => t.provider === id).length;
    const uses = [projects && `${projects} project(s)`, tasks && `${tasks} task(s)`].filter(Boolean).join(" and ");
    if (uses && !confirm(`${uses} use ${name}; they will fail with "provider missing" until changed. `
        + "Remove anyway? (Takes effect when you save.)")) return;
  }
  node.remove();
}

async function testEndpoint(node) {
  const status = $('[data-f="status"]', node);
  status.textContent = "Testing…";
  status.classList.remove("error");
  try {
    const r = await api("POST", "/api/endpoints/test", readEndpoint(node));
    status.textContent = r.ok ? `OK: ${r.count} tool-capable model${r.count === 1 ? "" : "s"}` : r.error;
    status.classList.toggle("error", !r.ok);
  } catch (e) {
    status.textContent = e.message;
    status.classList.add("error");
  }
}

function applyProviderSettings(s) {
  PROVIDERS = s.providers;
  for (const key of Object.keys(MODELS)) delete MODELS[key];
  Object.assign(MODELS, s.models);
  modelFetch.clear();
  for (const key of Object.keys(modelErrors)) delete modelErrors[key];
  renderProviderSelects();
  renderProjectModel();
  renderBoard();
}

function settingPath(name, settings) {
  const [group, key] = name.split(".");
  return group === "claude" || group === "codex" ? [settings.commands[group], key] : [settings[group], key];
}

function setupSettingsDialog() {
  const dialog = $("#settings-dialog");
  const form = $("#settings-form");
  $("#add-endpoint-btn").onclick = () => {
    const node = endpointBlock();
    $("#endpoint-list").append(node);
    $('[data-f="name"]', node).focus();
  };
  $("#settings-btn").onclick = async () => {
    try {
      const s = await api("GET", "/api/settings");
      for (const name of SETTING_FIELDS) {
        const [obj, key] = settingPath(name, s);
        const input = form.elements[name];
        if (input.type === "checkbox") input.checked = !!obj[key];
        else input.value = Array.isArray(obj[key]) ? obj[key].join(" ") : obj[key] ?? "";
      }
      $("#endpoint-list").replaceChildren(...s.endpoints.map(endpointBlock));
      showError($("#settings-error"), "");
      dialog.showModal();
    } catch (e) { toast(e.message, true); }
  };
  form.onsubmit = async ev => {
    ev.preventDefault();
    const out = { commands: { claude: {}, codex: {} }, timeouts: {},
      endpoints: $$("#endpoint-list .endpoint").map(readEndpoint) };
    for (const name of SETTING_FIELDS) {
      const [obj, key] = settingPath(name, out);
      const input = form.elements[name];
      obj[key] = input.type === "checkbox" ? input.checked
        : input.type === "number" ? Number(input.value) : input.value.trim();
    }
    try {
      applyProviderSettings(await api("PUT", "/api/settings", out));
      dialog.close();
      toast("Settings saved");
    } catch (e) { showError($("#settings-error"), e.message); }
  };
}

async function showCommits() {
  const list = $("#commit-list");
  list.replaceChildren(el("li", { class: "muted" }, "Loading…"));
  $("#commits-dialog").showModal();
  try {
    const data = await api("GET", `/api/projects/${state.pid}/commits`);
    list.replaceChildren(...(data.commits.length ? data.commits.map(c =>
      el("li", {}, el("code", {}, c.hash), " ", c.subject, " ",
        el("span", { class: "muted" }, `— ${c.author}, ${c.when}`)))
      : [el("li", { class: "muted" }, "No commits yet.")]));
  } catch (e) { list.replaceChildren(el("li", { class: "error" }, e.message)); }
}

function renderRemote(r) {
  $("#r-url").value = r.url || "";
  $("#r-mode").value = r.sync_mode || "ff-only";
  const parts = [r.branch ? `Branch ${r.branch}` : "Detached HEAD"];
  if (r.upstream) parts.push(`tracking ${r.upstream}`, `${r.ahead} ahead, ${r.behind} behind`);
  else if (r.url) parts.push(`not pushed yet (${r.ahead} local commit${r.ahead === 1 ? "" : "s"})`);
  else parts.push("no remote set");
  if (r.dirty) parts.push("uncommitted changes");
  $("#r-status").textContent = parts.join(" · ");
  $("#r-sync-btn").disabled = !r.url;
}

async function showRemote() {
  showError($("#r-error"), "");
  $("#r-log").hidden = true;
  $("#r-status").textContent = "Loading…";
  $("#remote-dialog").showModal();
  try {
    renderRemote(await api("GET", `/api/projects/${state.pid}/remote`));
  } catch (e) { $("#r-status").textContent = ""; showError($("#r-error"), e.message); }
}

function setupRemoteDialog() {
  $("#sync-btn").onclick = showRemote;
  $("#r-save-btn").onclick = async () => {
    showError($("#r-error"), "");
    try {
      renderRemote(await api("PUT", `/api/projects/${state.pid}/remote`, { url: $("#r-url").value }));
      toast("Remote saved");
    } catch (e) { showError($("#r-error"), e.message); }
  };
  $("#r-url").onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); $("#r-save-btn").click(); } };
  $("#r-mode").onchange = e => updateProject({ sync_mode: e.target.value });
  $("#r-sync-btn").onclick = async () => {
    const btn = $("#r-sync-btn");
    showError($("#r-error"), "");
    $("#r-log").hidden = true;
    btn.disabled = true;
    btn.textContent = "Syncing…";
    try {
      const r = await api("POST", `/api/projects/${state.pid}/remote/sync`,
        { mode: $("#r-mode").value, push: $("#r-push").checked });
      renderRemote(r);
      $("#r-log").textContent = r.log.join("\n");
      $("#r-log").hidden = false;
      toast("Synced");
    } catch (e) {
      showError($("#r-error"), e.message);
      toast(e.message, true);
    } finally {
      btn.textContent = "Sync now";
      btn.disabled = !$("#r-url").value.trim();
      loadTasks();
    }
  };
}

/* ---------------- wiring ---------------- */

function init() {
  setupProjectDialog();
  setupSettingsDialog();
  setupChat();
  setupBatchBar();
  setupRemoteDialog();
  $("#new-task").onsubmit = createTask;
  $("#commits-btn").onclick = showCommits;
  $("#terminal-btn").onclick = async () => {
    try {
      await api("POST", `/api/projects/${state.pid}/terminal`);
      toast("Terminal opened");
    } catch (e) { toast(e.message, true); }
  };
  for (const btn of $$(".queue-tab")) btn.onclick = () => selectTab(btn.dataset.col);
  $(".queue-tabs").onkeydown = onTabKeydown;
  $("#p-provider").onchange = async e => {
    const p = currentProject();
    const provider = e.target.value;
    for (const id of ["#p-plan-model", "#p-code-model", "#c-model"]) fillModelSelect($(id), provider, "");
    await ensureModels(provider);
    await updateProject({
      provider,
      plan_model: modelFor(provider, p.plan_model || ""),
      code_model: modelFor(provider, p.code_model || ""),
      chat_model: modelFor(provider, p.chat_model || ""),
    });
    if (currentProject() === p) renderProjectModel();
  };
  $("#p-model-refresh").onclick = async () => {
    const p = currentProject();
    const provider = p.provider || "claude";
    const job = ensureModels(provider, true);
    refillProjectModels(p, provider);
    await job;
    if (currentProject() === p) {
      refillProjectModels(p, provider);
      toast(modelErrors[provider] ? `Couldn't list models: ${modelErrors[provider]}` : "Model list refreshed",
        !!modelErrors[provider]);
    }
  };
  for (const [id, key] of [["#p-plan-model", "plan_model"], ["#p-code-model", "code_model"], ["#c-model", "chat_model"]]) {
    $(id).onchange = async e => {
      const model = pickModel(e.target, currentProject().provider || "claude");
      if (model === null) return;
      await updateProject({ [key]: model });
      renderChatWhere();
    };
  }
  $("#p-plan-limit").onchange = async e => {
    await updateProject({ plan_limit: e.target.value === "" ? 0 : Number(e.target.value) });
    e.target.value = currentProject().plan_limit || "";
  };
  $("#p-rewrite-titles").onchange = async e => {
    await updateProject({ rewrite_titles: e.target.checked });
    e.target.checked = currentProject().rewrite_titles !== false;
  };
  $("#p-auto-sync").onchange = async e => {
    await updateProject({ auto_sync: e.target.checked });
    e.target.checked = currentProject().auto_sync === true;
  };
  $("#remove-project-btn").onclick = async () => {
    const p = currentProject();
    if (!p || !confirm(`Remove "${p.name}" from PatchGoblin? Files, tasks.json and git history are kept.`)) return;
    try {
      await api("DELETE", `/api/projects/${p.id}`);
      state.pid = null;
      closeDrawer();
      await loadProjects();
    } catch (e) { toast(e.message, true); }
  };
  for (const id of ["#d-title", "#d-desc", "#d-provider", "#d-plan"]) {
    $(id).addEventListener("input", () => { state.dirty = true; renderEditActions(); });
  }
  $("#d-provider").addEventListener("change", () => {
    // Keep only models the new provider lists; a model name for another AI would fail there.
    const provider = $("#d-provider").value || currentProject().provider || "claude";
    fillTaskModels(modelFor(provider, $("#d-plan-model").dataset.value || ""),
      modelFor(provider, $("#d-code-model").dataset.value || ""));
  });
  for (const id of ["#d-plan-model", "#d-code-model"]) {
    $(id).onchange = e => {
      const provider = $("#d-provider").value || currentProject().provider || "claude";
      if (pickModel(e.target, provider) === null) return;
      state.dirty = true;
      renderEditActions();
    };
  }
  $("#d-save-btn").onclick = saveTask;
  $("#d-discard-btn").onclick = () => {
    if (!confirm("Discard your unsaved changes to this task?")) return;
    state.dirty = false;
    renderDrawer(true);
  };
  $("#d-answer-btn").onclick = answerQuestions;
  document.addEventListener("click", e => {
    const target = e.target.closest("[data-close]");
    if (!target) return;
    const id = target.dataset.close;
    if (id === "drawer") closeDrawer();
    else if (id === "chat") closeChat();
    else $("#" + id).close();
  });
  // Outside presses close the drawer/chat. pointerdown, not click: a card's click re-renders the board.
  document.addEventListener("pointerdown", e => {
    if (e.button !== 0 || !(e.target instanceof Element) || document.querySelector("dialog[open]")) return;
    const drawer = $("#drawer"), chatPanel = $("#chat");
    if (!drawer.hidden && isOutside(drawer, e.target)
        && !e.target.closest(".card[data-tid], #chat-btn") && !closeDrawer()) {
      // Cancelled the discard confirm: swallow the click that follows so the press does nothing else.
      e.preventDefault();
      e.stopPropagation();
      const swallow = ev => { ev.preventDefault(); ev.stopPropagation(); };
      document.addEventListener("click", swallow, { capture: true, once: true });
      document.addEventListener("pointerdown", () => document.removeEventListener("click", swallow, true),
        { capture: true, once: true });
      return;
    }
    if (!chatPanel.hidden && isOutside(chatPanel, e.target) && !e.target.closest("#chat-btn")) closeChat();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && $("#drawer").hidden && $("#chat").hidden && state.selected.size
        && !document.querySelector("dialog[open]")) clearSelection();
    if (e.key === "Escape" && !$("#drawer").hidden && !document.querySelector("dialog[open]")) closeDrawer();
    if (e.key === "Escape" && !$("#chat").hidden && !document.querySelector("dialog[open]")) closeChat();
    if ((e.ctrlKey || e.metaKey) && e.key === "s" && !$("#drawer").hidden) { e.preventDefault(); saveTask(); }
  });
  window.addEventListener("beforeunload", e => { if (state.dirty) e.preventDefault(); });

  loadProjects().catch(e => toast(e.message, true));
  setInterval(() => { if (!document.hidden) loadTasks(); }, 3000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadTasks(); });
}

init();
