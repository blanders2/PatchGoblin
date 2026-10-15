"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const STATUS_LABEL = {
  unplanned: "Unplanned", planning: "Planning…", drafted: "Drafted", planned: "Planned", queued: "Queued",
  running: "Running…", review: "Needs review", done: "Done", failed: "Failed",
};
const COLUMN_OF = {
  unplanned: "unplanned", planning: "unplanned", drafted: "drafted", planned: "planned",
  queued: "queue", running: "queue", review: "review", done: "finished", failed: "failed",
};
// Waiting on the user: not locked by the AI (LOCKED in app.py) and not done.
const ACTIONABLE = new Set(["unplanned", "drafted", "planned", "queued", "review", "failed"]);
const BUSY = new Set(["planning", "running"]);
const NOT_BUSY = Object.keys(STATUS_LABEL).filter(s => !BUSY.has(s));
const AUTO_MODES = ["auto_plan", "auto_queue", "auto_promote"];

const state = {
  projects: [],
  pid: localStorageGet("pg.pid"),
  tab: validTab(localStorageGet("pg.tab")),
  tasks: [],
  tasksLoaded: false, // true once the current project's tasks have been fetched
  openTid: null,
  formStamp: null, // server values of the editable fields when the drawer form was filled
  questionStamp: null, // questions shown in the drawer, so polling doesn't wipe typed answers
  dirty: false,
  selected: new Set(), // task ids ticked for batch actions; always within the current tab
  lastSelected: null, // anchor for shift-click range selection
  mode: "projects", // sidebar mode: "projects" or "ssh" (SSH tools); the project UI is only hidden in "ssh"
  sshTool: "keys", // "keys", "install" or "tunnels"
  view: "board", // "board" or "settings" (the full-page Project settings view)
  settingsDirty: false,
  automation: {}, // the global automation defaults, for the project settings' "Default (…)" labels
  reach: {}, // pid -> {ok, error, vcs?} from /api/projects/status; missing means still checking
  reachInFlight: false,
  reachAgain: false,
  remoteUrl: "", // origin's URL as last shown in the Sync dialog
  changesKey: null, // project/task/commit whose changed files the drawer shows
  toastTimer: null,
  chat: { active: false, timer: null, errorShown: false },
  live: { timer: null, tabFor: null, userTab: false }, // the drawer's live-output poll
};

function validTab(col) { return Object.values(COLUMN_OF).includes(col) ? col : "unplanned"; }
function localStorageGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function localStorageSet(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } }

const THEMES = ["system", "light", "dark", "midnight", "sepia"];
function validTheme(v) { return THEMES.includes(v) ? v : "system"; }
function applyTheme(choice) {
  let theme = validTheme(choice);
  if (theme === "system") {
    theme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.dataset.theme = theme;
}
function setupThemePicker() {
  const select = document.getElementById("theme-select");
  const stored = () => validTheme(localStorageGet("pg.theme"));
  select.value = stored();
  select.onchange = () => {
    localStorageSet("pg.theme", select.value);
    applyTheme(select.value);
  };
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (stored() === "system") applyTheme("system");
  });
  window.addEventListener("storage", (e) => {
    if (e.key !== "pg.theme") return;
    select.value = stored();
    applyTheme(select.value);
  });
}

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
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 6000 : 2500);
}

function showError(node, message) {
  node.textContent = message || "";
  node.hidden = !message;
}

// Disables `btn` and clears `errNode` while fn runs; a failure is shown in errNode. Returns whether fn succeeded.
async function withBusy(btn, errNode, fn) {
  btn.disabled = true;
  showError(errNode, "");
  try {
    await fn();
    return true;
  } catch (e) {
    showError(errNode, e.message);
    return false;
  } finally {
    btn.disabled = false;
  }
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function isScrolledToBottom(node, slack = 30) {
  return node.scrollTop + node.clientHeight >= node.scrollHeight - slack;
}

// POSTs to a project endpoint that just does something on the server, and toasts the outcome.
async function postAndToast(path, message) {
  try {
    await api("POST", `/api/projects/${state.pid}/${path}`);
    toast(message);
  } catch (e) { toast(e.message, true); }
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
  t.code_model || "", t.plan_trust || "", t.plan]);

const sshHost = p => `${p.ssh_target}${p.ssh_port ? ":" + p.ssh_port : ""}`;
const currentProject = () => state.projects.find(p => p.id === state.pid);
const projectProvider = p => (p && p.provider) || "claude";
const openTask = () => state.tasks.find(t => t.id === state.openTid);

/* ---------------- model dropdowns ---------------- */

const MODELS = JSON.parse(document.body.dataset.models || "{}");
let PROVIDERS = JSON.parse(document.body.dataset.providers || "[]");
const CUSTOM_MODEL = "\u0000custom";
// Batch model selects: "unchanged" (not sent), "project default" (clears the override) or a model.
const KEEP_MODEL = "\u0000keep";
const CLI = new Set(["claude", "codex", "opencode", "cline"]);
// opencode's models come from each project's own opencode config, and Cline's from its installed
// catalog on the project's host, so they are listed per project.
const PER_PROJECT_MODELS = new Set(["opencode", "cline"]);

// Live model lists are fetched once per session and merged into MODELS, under modelKey().
const modelFetch = new Map(); // model key -> Promise
const modelLoading = new Set();
const modelErrors = {};

const isEndpoint = id => !!id && !CLI.has(id) && PROVIDERS.some(p => p.id === id);
const fetchesModels = id => isEndpoint(id) || PER_PROJECT_MODELS.has(id);

// Where a provider's model list is kept: per project for opencode (pid "" = no project yet).
const modelKey = (provider, pid = state.pid) =>
  PER_PROJECT_MODELS.has(provider) ? `${provider}@${pid || ""}` : provider;
// Selects in the Add project dialog (data-no-project) belong to no project yet.
const selectPid = select => ("noProject" in select.dataset ? "" : state.pid);
const modelList = (provider, pid = state.pid) => MODELS[modelKey(provider, pid)] || MODELS[provider] || [];

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

function ensureModels(provider, refresh = false, pid = state.pid) {
  if (!fetchesModels(provider)) return Promise.resolve();
  const perProject = PER_PROJECT_MODELS.has(provider);
  if (perProject && !pid) return Promise.resolve(); // no project yet: only the saved defaults
  const key = modelKey(provider, pid);
  if (!refresh && modelFetch.has(key)) return modelFetch.get(key);
  modelLoading.add(key);
  const url = perProject ? `/api/projects/${encodeURIComponent(pid)}/${provider}/models`
    : `/api/endpoints/${encodeURIComponent(provider)}/models`;
  const job = api("GET", url + (refresh ? "?refresh=1" : ""))
    .then(data => {
      MODELS[key] = [...new Set([...modelList(provider, pid), ...data.models])];
      modelErrors[key] = data.error || "";
    })
    .catch(e => { modelErrors[key] = e.message; modelFetch.delete(key); })
    .finally(() => modelLoading.delete(key));
  modelFetch.set(key, job);
  return job;
}

// Fills the select now, then again once the provider's live model list arrives.
async function fillModelSelectLive(select, provider, current, stillValid = () => true, blankLabel = undefined) {
  fillModelSelect(select, provider, current, blankLabel);
  const pid = selectPid(select), key = modelKey(provider, pid);
  if (!fetchesModels(provider) || (modelFetch.has(key) && !modelLoading.has(key))) return;
  const job = ensureModels(provider, false, pid);
  fillModelSelect(select, provider, current);
  await job;
  if (stillValid()) fillModelSelect(select, provider, select.dataset.value);
}

// The blank option's text says what blank falls back to ("Global default", "Project default"…);
// it is remembered on the select so later refills keep it.
function fillModelSelect(select, provider, current = "", blankLabel = select.dataset.blank || "default") {
  select.dataset.blank = blankLabel;
  const pid = selectPid(select), key = modelKey(provider, pid);
  const models = [...modelList(provider, pid)];
  if (current && !models.includes(current)) models.push(current);
  select.replaceChildren(el("option", { value: "" }, blankLabel),
    ...models.map(m => el("option", { value: m }, m)),
    modelLoading.has(key) ? el("option", { value: "", disabled: true }, "Loading models…") : null,
    el("option", { value: CUSTOM_MODEL }, "Custom…"));
  select.value = current;
  select.dataset.value = current;
  select.title = modelErrors[key] ? `Couldn't list models: ${modelErrors[key]}` : "";
}

// Resolves the select's new value, asking for a name when "Custom…" is picked.
// Returns null (and restores the previous choice) if the prompt is cancelled.
function pickModel(select, provider, initial = select.dataset.value) {
  if (select.value !== CUSTOM_MODEL) {
    select.dataset.value = select.value;
    return select.value;
  }
  const name = (prompt("Model name:", initial) || "").trim();
  if (!name) { select.value = select.dataset.value; return null; }
  // Remember it (the server saves it too) so other dropdowns list it without a reload.
  for (const key of new Set([provider, modelKey(provider, selectPid(select))])) {
    if (key === provider || key in MODELS) MODELS[key] = [...new Set([...(MODELS[key] || []), name])];
  }
  fillModelSelect(select, provider, name);
  return name;
}

// A model belongs to its provider, so drop it when switching to a provider that doesn't list it.
const modelFor = (provider, model, pid = state.pid) => (modelList(provider, pid).includes(model) ? model : "");

/* ---------------- projects ---------------- */

async function loadProjects() {
  const data = await api("GET", "/api/projects");
  state.projects = data.projects;
  if (!currentProject()) state.pid = state.projects[0] ? state.projects[0].id : null;
  renderProjects();
  loadReachability();
  await selectProject(state.pid);
}

// Whether each project's directory can be reached; SSH checks are slow, so this runs in the background.
async function loadReachability() {
  // Don't pile up slow checks; a request made meanwhile runs once the current one finishes.
  if (state.reachInFlight) { state.reachAgain = true; return; }
  state.reachInFlight = true;
  try {
    state.reach = (await api("GET", "/api/projects/status")).status || {};
    renderProjects();
  } catch { /* a failed check shouldn't toast on every poll */ }
  finally { state.reachInFlight = false; }
  if (state.reachAgain) { state.reachAgain = false; loadReachability(); }
}

function reachDot(p) {
  const r = state.reach[p.id];
  const [cls, label] = !r ? ["checking", "Checking…"] : r.ok ? ["ok", "Reachable"] : ["bad", r.error || "Not reachable"];
  return el("span", { class: `p-dot ${cls}`, title: label, "aria-label": label, role: "img" });
}

// Static glyphs (never user data) in the same stroke style as the other inline SVGs; el() can't build SVG.
const svgIcon = body => `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
  `stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
const VCS_LOCAL_ICON = svgIcon('<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="12" r="2.5"/><path d="M6 8.5v7M8.5 6C14 6 18 7 18 9.5"/>');
const CLOUD = '<path d="M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 8.5a4.75 4.75 0 0 1-.5 9.5z"/>';
const VCS_REMOTE_ICONS = {
  synced: svgIcon(CLOUD + '<path d="m9.5 13 2 2 3.5-4"/>'),
  unpushed: svgIcon(CLOUD + '<path d="M12 16v-5M9.8 12.8 12 10.6l2.2 2.2"/>'),
  behind: svgIcon(CLOUD + '<path d="M12 10v5M9.8 13.2 12 15.4l2.2-2.2"/>'),
};

// 0-2 small icons after a project's name: has a local repository, and (if it has a remote) whether all is pushed.
function vcsIcons(p) {
  const v = (state.reach[p.id] || {}).vcs;
  if (!v) return [];
  const icon = (cls, label, html, count) => {
    const span = el("span", { class: `p-vcs ${cls}`, title: label, "aria-label": label, role: "img" });
    span.innerHTML = html;
    if (count) span.append(el("span", { class: "count" }, String(count)));
    return span;
  };
  const icons = [icon("local", v.kind === "svn" ? "SVN working copy" : "Local git repository", VCS_LOCAL_ICON)];
  if (!v.remote) return icons;
  let state_, label, count = 0;
  if (v.kind === "svn") {
    state_ = v.pending ? "unpushed" : "synced";
    label = v.pending ? `${plural(v.pending, "change")} pending check-in` : "All changes checked in";
  } else if (!v.ahead && !v.behind && !v.dirty) {
    state_ = "synced";
    label = "All changes pushed to origin";
  } else if (!v.ahead && !v.dirty) {
    state_ = "behind";
    label = `${plural(v.behind, "commit")} on origin not pulled (as of last fetch)`;
  } else {
    state_ = "unpushed";
    count = v.ahead;
    label = [v.ahead ? `${plural(v.ahead, "commit")} not pushed` : "", v.dirty ? "uncommitted changes" : ""]
      .filter(Boolean).join(" · ");
  }
  icons.push(icon(`remote ${state_}`, label, VCS_REMOTE_ICONS[state_], count));
  return icons;
}

function renderProjects() {
  const list = $("#project-list");
  list.replaceChildren(...state.projects.map(p => el("li", {
    class: p.id === state.pid ? "active" : "",
    onclick: () => selectProject(p.id),
  }, el("div", { class: "p-title" }, reachDot(p), el("span", { class: "p-name" }, p.name), ...vcsIcons(p)),
     el("div", { class: "p-sub" }, p.location === "ssh" ? `ssh · ${p.ssh_target}` : "local"))));
  const has = state.projects.length > 0;
  if (!has) state.view = "board";
  $("#empty-state").hidden = has;
  $("#project-view").hidden = !has || state.view !== "board";
  $("#project-settings-view").hidden = !has || state.view !== "settings";
}

async function selectProject(pid) {
  // Switching projects leaves settings for the new project's board; refreshes of the same pid don't.
  if (state.view === "settings" && pid !== state.pid && !closeProjectSettings()) return;
  if (pid !== state.pid && !closeDrawer()) return;
  if (pid !== state.pid) clearSelection(false);
  state.pid = pid;
  if (pid) localStorageSet("pg.pid", pid);
  renderProjects();
  const p = currentProject();
  if (!p) { closeChat(); return; }
  $("#p-name").textContent = p.name;
  $("#p-where").textContent = p.location === "ssh" ? `${sshHost(p)}:${p.path}` : p.path;
  updateVcsButtons(p);
  renderProjectModel();
  renderProjectTrust();
  state.tasks = [];
  state.tasksLoaded = false;
  renderBoard();
  loadChat();
  await loadTasks();
}

function renderProjectModel() {
  const p = currentProject();
  if (!p) return;
  const provider = projectProvider(p);
  const stillValid = () => currentProject() === p && projectProvider(p) === provider;
  fillModelSelectLive($("#c-model"), provider, p.chat_model || "", stillValid, "Planning model");
  fillBatchModel($("#batch-plan-model"), provider);
  fillBatchModel($("#batch-code-model"), provider);
  renderChatWhere();
}

const TRUST_NAMES = { low: "Low", normal: "Normal", high: "High" };

const planTrust = p => (TRUST_NAMES[p.plan_trust] ? p.plan_trust : "normal");

// The level a task's "Project default" plan trust resolves to.
function renderProjectTrust() {
  const p = currentProject();
  if (!p) return;
  $("#d-plan-trust").options[0].textContent = `Project default (${TRUST_NAMES[planTrust(p)]})`;
}

async function updateProject(fields) {
  const p = currentProject();
  try {
    const updated = await api("PATCH", `/api/projects/${p.id}`, fields);
    Object.assign(p, updated);
    toast("Project updated");
  } catch (e) { toast(e.message, true); }
}

/* ---------------- project settings view ---------------- */

const SYNC_MODE_NAMES = { "ff-only": "Fast-forward only", rebase: "Rebase" };
const syncMode = p => (SYNC_MODE_NAMES[p.sync_mode] ? p.sync_mode : "ff-only");
const syncModeName = mode => SYNC_MODE_NAMES[mode] || SYNC_MODE_NAMES["ff-only"];
// An automation setting as the project settings' select value: "on", "off" or "" (use the default).
const autoSelectValue = v => (v === true ? "on" : v === false ? "off" : "");
const settingsProject = () => state.projects.find(x => x.id === settingsForm().dataset.pid);
const settingsForm = () => $("#project-settings-form");
const settingsModelSelects = form => [form.plan_model, form.code_model, form.chat_model];

function openProjectSettings() {
  const p = currentProject();
  if (!p || !closeDrawer()) return;
  closeChat();
  clearSelection();
  state.view = "settings";
  renderProjects();

  const form = settingsForm();
  const provider = projectProvider(p);
  form.dataset.pid = p.id;
  delete form.dataset.remoteLoaded;
  delete form.dataset.remoteUrl;
  $("#ps-name").textContent = p.name;
  form.elements.name.value = p.name;
  $("#ps-where").textContent = p.location === "ssh"
    ? `SSH · ${sshHost(p)} · ${p.path}` : `Local · ${p.path}`;
  setProviderValue(form.provider, provider);
  $("#ps-model-refresh").hidden = !fetchesModels(provider);
  // A late model list must not refill the selects once the form shows another provider or project.
  const stillValid = () => state.view === "settings" && form.dataset.pid === p.id
    && form.provider.value === provider;
  fillModelSelectLive(form.plan_model, provider, p.plan_model || "", stillValid, "Global default");
  fillModelSelectLive(form.code_model, provider, p.code_model || "", stillValid, "Global default");
  fillModelSelectLive(form.chat_model, provider, p.chat_model || "", stillValid, "Planning model");
  form.plan_limit.value = p.plan_limit || "";
  form.rewrite_titles.checked = p.rewrite_titles !== false;
  form.plan_trust.value = planTrust(p);
  for (const key of AUTO_MODES) form[key].value = autoSelectValue(p[key]);
  renderAutoDefaults();
  form.auto_sync.checked = p.auto_sync === true;
  form.sync_mode.value = syncMode(p);
  form.remote_url.value = "";
  form.remote_url.disabled = true;
  form.git_tracking_off.checked = false;
  form.git_secrets_ack.checked = false;
  form.svn_tracking_off.checked = false;
  const tracked = p.git_tracking === true;
  const svn = p.svn_tracking === true;
  $("#ps-git-off").hidden = tracked || svn;
  $("#ps-git-on").hidden = !tracked;
  $("#ps-svn").hidden = !svn;
  showError($("#ps-error"), "");
  state.settingsDirty = false;
  if (tracked) startSettingsRemote(p.id);
}

// Commits shows for either kind of version control; Sync is git-only; Check in is SVN-only.
function updateVcsButtons(p) {
  const git = p.git_tracking === true, svn = p.svn_tracking === true;
  $("#commits-btn").hidden = !git && !svn;
  $("#sync-btn").hidden = !git;
  $("#checkin-btn").hidden = !svn;
  updateCheckinCount();
}

function updateCheckinCount() {
  const n = state.tasks.filter(t => t.checkin_pending).length;
  $("#checkin-btn").textContent = n ? `Check in (${n})` : "Check in";
}

// Shows what "Default" means for each automation select, from the global settings.
function renderAutoDefaults() {
  const form = settingsForm();
  for (const key of AUTO_MODES) {
    form[key].options[0].textContent = `Default (${state.automation[key] ? "On" : "Off"})`;
  }
}

async function loadAutomation() {
  try {
    state.automation = (await api("GET", "/api/settings")).automation || {};
    renderAutoDefaults();
    renderAutoFlow();
  } catch { /* the labels just say "Default (Off)" */ }
}

function startSettingsRemote(pid) {
  $("#ps-remote-status").textContent = "Loading…";
  loadSettingsRemote(pid);
}

// Only a successfully loaded URL may be sent back, or a failed load could wipe origin on Save.
async function loadSettingsRemote(pid) {
  const form = settingsForm();
  const current = () => state.view === "settings" && form.dataset.pid === pid;
  try {
    const r = await api("GET", `/api/projects/${pid}/remote`);
    if (!current()) return;
    form.remote_url.value = r.url || "";
    form.dataset.remoteUrl = r.url || "";
    form.dataset.remoteLoaded = "1";
    form.remote_url.disabled = false;
    $("#ps-remote-status").textContent = r.url
      ? `Branch ${r.branch || "(detached)"}${r.upstream ? ` · tracking ${r.upstream}` : ""}` : "no remote set";
  } catch (e) {
    if (current()) $("#ps-remote-status").textContent = `Couldn't load the remote: ${e.message}`;
  }
}

function closeProjectSettings(force = false) {
  if (state.view !== "settings") return true;
  if (!force && state.settingsDirty && !confirm("Discard unsaved project settings?")) return false;
  state.view = "board";
  state.settingsDirty = false;
  renderProjects();
  return true;
}

// Refills the form's model selects for `provider` without fetching; `values` are the models to show.
function refillSettingsModels(form, provider, values) {
  settingsModelSelects(form).forEach((select, i) => fillModelSelect(select, provider, values[i]));
}

async function saveProjectSettings(ev) {
  ev.preventDefault();
  const form = settingsForm();
  const p = settingsProject();
  if (!p) return;
  const fields = {
    name: form.elements.name.value,
    provider: form.provider.value,
    plan_model: form.plan_model.dataset.value || "",
    code_model: form.code_model.dataset.value || "",
    chat_model: form.chat_model.dataset.value || "",
    plan_limit: form.plan_limit.value === "" ? 0 : Number(form.plan_limit.value),
    rewrite_titles: form.rewrite_titles.checked,
    plan_trust: form.plan_trust.value,
  };
  for (const key of AUTO_MODES) fields[key] = { on: true, off: false }[form[key].value] ?? null;
  if (p.git_tracking === true) {
    if (form.git_tracking_off.checked) {
      fields.git_tracking = false;
    } else {
      fields.auto_sync = form.auto_sync.checked;
      fields.sync_mode = form.sync_mode.value;
      if (form.dataset.remoteLoaded && form.remote_url.value.trim() !== form.dataset.remoteUrl) {
        fields.remote_url = form.remote_url.value.trim();
      }
    }
  }
  if (p.svn_tracking === true && form.svn_tracking_off.checked) fields.svn_tracking = false;
  const saved = await withBusy($("#ps-save-btn"), $("#ps-error"), async () => {
    Object.assign(p, await api("PATCH", `/api/projects/${p.id}`, fields));
  });
  if (!saved) return;
  renderProjects();
  renderAutoFlow();
  if (currentProject() === p) {
    $("#p-name").textContent = p.name;
    updateVcsButtons(p);
    renderProjectModel();
    renderProjectTrust();
  }
  toast("Project settings saved");
  loadReachability(); // the path or host may have changed
  closeProjectSettings(true);
  loadTasks(); // turning an automation mode on may have moved tasks
}

async function removeProject() {
  const p = currentProject();
  if (!p || !confirm(`Remove "${p.name}" from PatchGoblin? Files, tasks.json and git history are kept.`)) return;
  if (!closeDrawer()) return;
  try {
    await api("DELETE", `/api/projects/${p.id}`);
    state.view = "board";
    state.settingsDirty = false;
    state.pid = null;
    await loadProjects();
  } catch (e) { toast(e.message, true); }
}

function setupProjectSettings() {
  const form = settingsForm();
  const markDirty = () => { state.settingsDirty = true; };
  form.addEventListener("input", markDirty);
  form.addEventListener("change", markDirty);
  const chosen = () => settingsModelSelects(form).map(s => s.dataset.value || "");
  form.provider.addEventListener("change", async () => {
    // A model belongs to its provider, so keep only those the new provider lists.
    const provider = form.provider.value;
    const before = chosen();
    const keep = () => before.map(m => modelFor(provider, m));
    $("#ps-model-refresh").hidden = !fetchesModels(provider);
    refillSettingsModels(form, provider, keep());
    await ensureModels(provider);
    if (form.provider.value === provider && state.view === "settings") refillSettingsModels(form, provider, keep());
  });
  $("#ps-model-refresh").onclick = async () => {
    const provider = form.provider.value;
    const job = ensureModels(provider, true);
    refillSettingsModels(form, provider, chosen());
    await job;
    if (form.provider.value !== provider || state.view !== "settings") return;
    refillSettingsModels(form, provider, chosen());
    const error = modelErrors[modelKey(provider)];
    toast(error ? `Couldn't list models: ${error}` : "Model list refreshed", !!error);
  };
  for (const select of settingsModelSelects(form)) {
    select.addEventListener("change", () => pickModel(select, form.provider.value));
  }
  form.onsubmit = saveProjectSettings;
  $("#project-settings-btn").onclick = openProjectSettings;
  $("#ps-back-btn").onclick = () => closeProjectSettings();
  $("#ps-cancel-btn").onclick = () => closeProjectSettings();
  $("#ps-remove-btn").onclick = removeProject;
  $("#ps-svn-enable-btn").onclick = () => enableVcs("svn");
  $("#ps-git-enable-btn").onclick = () => enableVcs("git");
}

async function enableVcs(kind) {
  const form = settingsForm();
  const p = settingsProject();
  if (!p) return;
  const git = kind === "git";
  if (git && !form.git_secrets_ack.checked) {
    showError($("#ps-error"), "Tick the confirmation that this directory contains no secrets first.");
    form.git_secrets_ack.focus();
    return;
  }
  await withBusy($(`#ps-${kind}-enable-btn`), $("#ps-error"), async () => {
    Object.assign(p, await api("POST", `/api/projects/${p.id}/${kind}/enable`, git ? { secrets_ack: true } : undefined));
    renderProjects();
    loadReachability();
    if (currentProject() === p) updateVcsButtons(p);
    $("#ps-git-off").hidden = true;
    $("#ps-svn").hidden = git;
    $("#ps-git-on").hidden = !git;
    form[`${kind}_tracking_off`].checked = false;
    if (git) {
      form.auto_sync.checked = p.auto_sync === true;
      form.sync_mode.value = syncMode(p);
      startSettingsRemote(p.id);
    }
    toast(`${git ? "Git" : "SVN"} tracking turned on`);
  });
}

/* ---------------- tasks & board ---------------- */

let tasksJob = null, tasksAgain = false, tasksVersion = 0;
const markTasksChanged = () => { tasksVersion++; };

// Polls never overlap: a call made while one is in flight re-runs once after it.
function loadTasks() {
  if (tasksJob) { tasksAgain = true; return tasksJob; }
  tasksJob = (async () => {
    try {
      do { tasksAgain = false; await fetchTasksOnce(); } while (tasksAgain);
    } finally { tasksJob = null; }
  })();
  return tasksJob;
}

async function fetchTasksOnce() {
  const pid = state.pid;
  if (!pid) return;
  const version = tasksVersion;
  try {
    const data = await api("GET", `/api/projects/${pid}/tasks`);
    if (pid !== state.pid) return;
    if (version !== tasksVersion) { tasksAgain = true; return; } // a local change landed meanwhile
    state.tasks = data.tasks;
    state.tasksLoaded = true;
    updateCheckinCount();
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
  if (col === "review") return tasks.sort(by("finished_at"));
  if (col === "finished" || col === "failed") return tasks.sort(by("finished_at", -1));
  return tasks.sort((a, b) => a.id - b.id);
}

function renderBoard() {
  // The Failed tab only shows while a task is failed; fall back to Finished once it empties
  // (but not before this project's tasks have loaded, or a remembered tab would be lost).
  const hasFailed = state.tasks.some(t => t.status === "failed");
  if (state.tab === "failed" && !hasFailed && state.tasksLoaded) {
    state.tab = "finished";
    localStorageSet("pg.tab", state.tab);
    clearSelection(false);
  }
  for (const column of $$(".column")) {
    const col = column.dataset.col;
    const tasks = sortTasks(col, state.tasks.filter(t => COLUMN_OF[t.status] === col));
    const active = col === state.tab;
    $(".cards", column).replaceChildren(...(tasks.length ? tasks.map(renderCard)
      : [el("div", { class: "muted empty-col" }, "No tasks here")]));
    column.hidden = !active;

    // The badge counts only tasks the user can act on; the tooltip gives the full breakdown.
    const tab = $(`.queue-tab[data-col="${col}"]`);
    tab.hidden = col === "failed" && !hasFailed && !active;
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
    $(".tab-activity", tab).hidden = !tasks.some(t => BUSY.has(t.status));
    tab.title = summary;
    tab.setAttribute("aria-label", summary);
    tab.setAttribute("aria-selected", String(active));
    tab.classList.toggle("active", active);
    tab.tabIndex = active ? 0 : -1;
  }
  renderAutoFlow();
  renderBatchBar();
}

// The effective automation mode for a project: its own override, else the global default.
function effectiveAuto(p, key) {
  return typeof p[key] === "boolean" ? p[key] : state.automation[key] === true;
}

const AUTO_FLOW_HINT = {
  auto_plan: "plans every Unplanned task now",
  auto_queue: "queues every Planned task now",
  auto_promote: "moves every question-free Drafted task to Planned now",
};

// Colours (or strikes through) each Auto-plan / Auto-promote / Auto-queue indicator between the tabs for the current project.
function renderAutoFlow() {
  const p = currentProject();
  for (const btn of $$(".auto-flow")) {
    const mode = btn.dataset.mode, label = btn.dataset.label;
    const on = !!p && effectiveAuto(p, mode);
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-pressed", String(on));
    btn.disabled = !p || btn.dataset.busy === "1";
    if (!p) { btn.title = label; continue; }
    const source = typeof p[mode] === "boolean" ? "project override" : "default";
    btn.title = `${label}: ${on ? "On" : "Off"} (${source}) — click to turn ${on ? "off" : `on (${AUTO_FLOW_HINT[mode]})`}`;
    btn.setAttribute("aria-label", btn.title);
  }
}

async function toggleAutoFlow(btn) {
  const p = currentProject();
  if (!p) return;
  const mode = btn.dataset.mode;
  btn.dataset.busy = "1";
  btn.disabled = true;
  try {
    Object.assign(p, await api("PATCH", `/api/projects/${p.id}`, { [mode]: !effectiveAuto(p, mode) }));
    if (state.view === "settings" && settingsForm().dataset.pid === p.id) {
      for (const key of AUTO_MODES) settingsForm()[key].value = autoSelectValue(p[key]);
    }
    loadTasks(); // turning a mode on may have moved tasks
  } catch (e) {
    toast(e.message, true);
  } finally {
    delete btn.dataset.busy;
    renderAutoFlow();
  }
}

function selectTab(col, focus = false) {
  if (validTab(col) !== state.tab) clearSelection(false);
  state.tab = validTab(col);
  localStorageSet("pg.tab", state.tab);
  renderBoard();
  if (focus) $(`.queue-tab[data-col="${state.tab}"]`).focus();
}

function onTabKeydown(e) {
  const tabs = $$(".queue-tab").filter(b => !b.hidden).map(b => b.dataset.col);
  const i = tabs.indexOf(state.tab);
  const next = { ArrowLeft: tabs[(i - 1 + tabs.length) % tabs.length],
    ArrowRight: tabs[(i + 1) % tabs.length], Home: tabs[0], End: tabs[tabs.length - 1] }[e.key];
  if (!next) return;
  e.preventDefault();
  selectTab(next, true);
}

// Statuses in which the plan can still be refined, so its questions still matter.
const PLANNABLE = new Set(["unplanned", "drafted", "planned", "failed"]);
// Statuses whose finished work can be sent back to the AI with feedback.
const REVIEWABLE = new Set(["review", "done"]);
const hasOpenQuestions = t => PLANNABLE.has(t.status) && (t.questions || []).length > 0;

function renderCard(t) {
  const p = currentProject();
  const selected = state.selected.has(t.id);
  return el("div", {
    class: `card status-${t.status}${t.paused ? " paused" : ""}${t.id === state.openTid ? " open" : ""}${selected ? " selected" : ""}`,
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
  t.active && t.activity ? el("div", { class: "card-activity", title: t.activity }, `▶ ${t.activity}`) : null,
  el("div", { class: "card-meta" },
    t.paused ? el("span", { class: "chip paused", title: "Automation won't touch this task" }, "Paused") : null,
    t.provider && t.provider !== projectProvider(p) ? el("span", { class: "chip" }, providerName(t.provider)) : null,
    t.plan_model ? el("span", { class: "chip", title: "Planning model for this task" }, `plan: ${t.plan_model}`) : null,
    t.code_model ? el("span", { class: "chip", title: "Coding model for this task" }, `code: ${t.code_model}`) : null,
    t.plan_trust ? el("span", { class: "chip", title: "Planning trust for this task" }, `trust: ${t.plan_trust}`) : null,
    hasOpenQuestions(t) ? el("span", { class: "chip question", title: t.questions.map(q => q.text).join("\n") },
      `? ${plural(t.questions.length, "question")}`) : null,
    t.error && t.status !== "failed" ? el("span", { class: "chip warn", title: t.error }, "last attempt failed") : null,
    t.commit ? el("span", { class: "chip mono" }, t.commit.slice(0, 7)) : null,
    el("span", { class: "muted" }, relTime(t.updated_at))),
  t.status === "review" ? el("button", {
    type: "button", class: "card-action primary", title: "The work is good; move it to Finished",
    onclick: e => { e.stopPropagation(); e.currentTarget.disabled = true; approveFromCard(t); },
    onkeydown: e => e.stopPropagation(),
  }, "Approve") : null);
}

async function approveFromCard(t) {
  if (t.id === state.openTid) return doAction("approve");
  try {
    const updated = await api("POST", `/api/projects/${state.pid}/tasks/${t.id}/action`, { action: "approve" });
    Object.assign(t, updated); markTasksChanged();
    state.selected.delete(t.id);
    renderBoard();
    toast("Approved");
  } catch (e) {
    toast(e.message, true);
    renderBoard();
  }
}

async function createTask(ev) {
  ev.preventDefault();
  const title = $("#nt-title").value.trim();
  if (!title) return;
  const paused = $("#nt-paused").checked;
  try {
    const task = await api("POST", `/api/projects/${state.pid}/tasks`,
      { title, description: $("#nt-desc").value.trim(), ...(paused ? { paused: true } : {}) });
    $("#nt-title").value = "";
    $("#nt-desc").value = "";
    renderImagePreviews($("#nt-desc"), $("#nt-desc-img"));
    state.tasks.push(task); markTasksChanged();
    if (state.tab !== "unplanned") selectTab("unplanned");
    else renderBoard();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- batch actions ---------------- */

// Mirrors the drawer's per-status buttons and TRANSITIONS in app.py. An optional `when`
// narrows the eligible tasks further. Paused tasks only accept Resume (and edits/delete).
const BATCH_ACTIONS = [
  { action: "plan", label: "Plan with AI", from: ["unplanned", "drafted", "planned", "failed"] },
  { action: "mark_planned", label: "Mark planned", from: ["unplanned", "drafted", "failed"] },
  { action: "mark_drafted", label: "Move to drafted", from: ["planned"], when: hasOpenQuestions },
  { action: "queue", label: "Queue for AI", from: ["drafted", "planned", "failed"] },
  { action: "dequeue", label: "Remove from queue", from: ["queued"] },
  { action: "unplan", label: "Back to unplanned", from: ["drafted", "planned"] },
  { action: "approve", label: "Approve", from: ["review"] },
  { action: "reopen", label: "Reopen", from: ["done", "review"] },
  { action: "pause", label: "Pause", from: ["unplanned", "drafted", "planned", "queued", "failed"] },
  { action: "resume", label: "Resume", from: ["unplanned", "drafted", "planned", "queued", "failed"],
    when: t => t.paused },
  { action: "cancel", label: "Cancel", from: ["planning", "running"], cls: "danger" },
].map(a => a.action === "resume" ? a : { ...a, when: t => !t.paused && (!a.when || a.when(t)) });
const BATCH_VERB = { delete: "Deleted", set_provider: "Updated", set_models: "Updated", plan: "Started planning",
  mark_planned: "Marked planned", mark_drafted: "Moved to drafted", queue: "Queued", dequeue: "Removed from queue",
  unplan: "Moved back", reopen: "Reopened", approve: "Approved",
  cancel: "Cancelled", pause: "Paused", resume: "Resumed" };

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
  const spec = BATCH_ACTIONS.find(a => a.action === action);
  const targets = eligible(spec?.from || NOT_BUSY, spec?.when);
  if (!targets.length) return;
  const many = plural(targets.length, "task");
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

  state.tasks = data.tasks; markTasksChanged();
  state.tasksLoaded = true;
  const ok = data.results.filter(r => r.ok);
  const failed = data.results.filter(r => !r.ok);
  for (const r of ok) state.selected.delete(r.id);
  const affectsDrawer = state.openTid !== null && ok.some(r => r.id === state.openTid);
  if (affectsDrawer) state.formStamp = null;
  renderBoard();
  if (state.openTid !== null) renderDrawer(false);

  let message = `${BATCH_VERB[action]} ${ok.length}`;
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
      const provider = projectProvider(currentProject());
      const custom = select.value === CUSTOM_MODEL;
      const name = pickModel(select, provider, "");
      if (custom && name !== null) fillBatchModel(select, provider, name); // refill to restore the "unchanged" option
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
  renderImagePreviews($("#d-feedback"), $("#d-feedback-img"));
  $("#d-approval-note").value = "";
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
  $("#d-question-list").replaceChildren(...questions.map((q, i) => {
    const options = q.options || [];
    if (!options.length) {
      return el("li", { "data-q": String(i) },
        el("label", { for: `d-q-${i}`, class: "question-text" }, q.text),
        el("textarea", { id: `d-q-${i}`, rows: "1", placeholder: "Your answer…" }));
    }
    return el("li", { "data-q": String(i) },
      el("label", { for: `d-q-${i}`, class: "question-text" }, q.text),
      el("div", { class: "choice-group", role: "group", "aria-label": q.text, onclick: pickChoice },
        ...options.map(opt => el("button", { type: "button", class: "choice", "data-value": opt,
          "aria-pressed": "false" }, opt)),
        el("button", { type: "button", class: "choice other", "aria-expanded": "false",
          "aria-controls": `d-q-${i}` }, "Other…")),
      el("textarea", { id: `d-q-${i}`, rows: "1", hidden: true,
        placeholder: "Add a note or type your own answer…" }));
  }));
}

// One pressed option per question (click again to un-press); "Other…" shows/hides the note box.
function pickChoice(e) {
  const button = e.target.closest("button.choice");
  if (!button) return;
  const box = button.closest("li").querySelector("textarea");
  if (button.classList.contains("other")) {
    if (box.hidden) {
      box.hidden = false;
      button.setAttribute("aria-expanded", "true");
      box.focus();
    } else if (!box.value.trim()) {
      box.value = "";
      box.hidden = true;
      button.setAttribute("aria-expanded", "false");
    } else {
      box.focus();  // never hide a typed note, or it would be sent unseen
    }
    return;
  }
  const on = button.getAttribute("aria-pressed") !== "true";
  for (const b of $$(".choice:not(.other)", e.currentTarget)) b.setAttribute("aria-pressed", "false");
  button.setAttribute("aria-pressed", String(on));
}

function resetQuestionAnswers() {
  for (const box of $$("#d-question-list textarea")) box.value = "";
  for (const b of $$("#d-question-list .choice[aria-pressed]")) b.setAttribute("aria-pressed", "false");
  for (const b of $$("#d-question-list .choice.other")) {
    b.setAttribute("aria-expanded", "false");
    b.closest("li").querySelector("textarea").hidden = true;
  }
}

async function answerQuestions() {
  const t = openTask();
  if (!t) return;
  const answers = $$("#d-question-list > li").map(li => {
    const picked = $(".choice[aria-pressed='true']:not(.other)", li)?.dataset.value || "";
    const note = $("textarea", li).value.trim();
    return {
      question: t.questions[Number(li.dataset.q)]?.text || "",
      answer: picked && note ? `${picked} — ${note}` : picked || note,
    };
  }).filter(a => a.answer);
  if (!answers.length) { toast("Answer at least one question first.", true); return; }
  await doAction("plan", { answers });
}

/* ---------------- pasted images ---------------- */

const IMAGE_REF = /!\[[^\]]*\]\(\.patchgoblin\/attachments\/([0-9a-f]{32}\.(?:png|jpg|gif|webp))\)/g;

function imageUrls(text) {
  const names = [...new Set([...(text || "").matchAll(IMAGE_REF)].map(m => m[1]))];
  return names.map(n => `/api/projects/${state.pid}/attachments/${n}`);
}

function thumbnails(text) {
  return imageUrls(text).map(url => el("img", { src: url, alt: "attached image", title: "Open full size", loading: "lazy",
    onclick: () => window.open(url, "_blank", "noopener") }));
}

function renderImagePreviews(field, previewEl) {
  const thumbs = thumbnails(field.value);
  previewEl.replaceChildren(...thumbs);
  previewEl.hidden = !thumbs.length;
}

const IMAGE_FIELDS = { "#d-desc": "#d-desc-img", "#nt-desc": "#nt-desc-img", "#c-input": "#c-input-img",
                       "#d-feedback": "#d-feedback-img" };

function refreshImagePreviews() {
  for (const [f, p] of Object.entries(IMAGE_FIELDS)) renderImagePreviews($(f), $(p));
}

function readDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// Pasting or dropping an image uploads it to the project and inserts a Markdown reference.
function enableImagePaste(field, previewEl) {
  const imagesIn = dt => {
    if (!dt) return [];
    const files = [...(dt.files || [])].filter(f => f.type.startsWith("image/"));
    if (files.length) return files;
    return [...(dt.items || [])].filter(i => i.kind === "file" && i.type.startsWith("image/"))
      .map(i => i.getAsFile()).filter(Boolean);
  };
  const attach = async files => {
    const pid = state.pid;
    for (const file of files) {
      try {
        const res = await api("POST", `/api/projects/${pid}/attachments`, { data: await readDataUrl(file) });
        if (pid !== state.pid) return;
        const ref = `![image](${res.path})`;
        const start = field.selectionStart ?? field.value.length;
        const pad = field.value.slice(0, start) && !/\s$/.test(field.value.slice(0, start)) ? " " : "";
        field.setRangeText(pad + ref + " ", start, field.selectionEnd ?? start, "end");
        field.dispatchEvent(new Event("input", { bubbles: true }));
      } catch (e) { toast(e.message, true); }
    }
  };
  const handle = e => {
    const files = imagesIn(e.clipboardData || e.dataTransfer);
    if (!files.length) return;  // plain text paste/drop is left alone
    e.preventDefault();
    attach(files);
  };
  field.addEventListener("paste", handle);
  field.addEventListener("drop", handle);
  field.addEventListener("input", () => renderImagePreviews(field, previewEl));
}

function renderDrawer(fillForm) {
  const t = openTask();
  if (!t) { state.openTid = null; state.dirty = false; $("#drawer").hidden = true; return; }
  const locked = BUSY.has(t.status);

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
    $("#d-plan-trust").value = t.plan_trust || "";
    $("#d-plan").value = t.plan || "";
    state.formStamp = formStamp(t);
    state.dirty = false;
    renderImagePreviews($("#d-desc"), $("#d-desc-img"));
  }
  for (const id of ["#d-title", "#d-desc", "#d-provider", "#d-plan-model", "#d-code-model", "#d-plan-trust", "#d-plan"]) {
    $(id).disabled = locked;
  }

  showError($("#d-error"), t.error);

  const canPlan = PLANNABLE.has(t.status);
  const reviewing = REVIEWABLE.has(t.status);
  $("#d-feedback-wrap").hidden = !canPlan && !reviewing;
  $("#d-feedback-label").replaceChildren(...(reviewing ? ["Feedback for the AI"]
    : ["Other feedback for the AI ", el("span", { class: "muted" }, "(optional)")]));
  $("#d-feedback").placeholder = reviewing ? "e.g. The new button is missing on the mobile layout."
    : "e.g. Use the existing API client instead of adding a new one.";
  const reviewFeedback = (t.review_feedback || "").trim();
  $("#d-review-feedback").hidden = !reviewFeedback || reviewing;
  $("#d-review-feedback-text").textContent = reviewFeedback;
  $("#d-approval-wrap").hidden = t.status !== "review";
  const approvalNote = (t.approval_note || "").trim();
  $("#d-approval").hidden = !approvalNote;
  $("#d-approval-text").textContent = approvalNote;
  $("#d-approval-commit").hidden = !t.approval_commit;
  $("#d-approval-commit").textContent = t.approval_commit ? `Note committed as ${t.approval_commit.slice(0, 10)}` : "";
  renderQuestions(t, canPlan);
  renderEditActions();
  $("#d-flow-actions").replaceChildren(...drawerActions(t));
  $("#d-danger-actions").replaceChildren(...(locked ? []
    : [actionButton("Delete task", deleteTask, "ghost danger small", "Delete this task permanently")]));

  const commit = $("#d-commit");
  const svnState = t.checkin === "pending" || t.checkin_pending ? "Pending check-in"
    : t.checkin ? `Checked in ${t.checkin}` : "";
  commit.hidden = !t.commit && !svnState;
  commit.textContent = t.commit ? `Committed as ${t.commit.slice(0, 12)}` : svnState;
  renderChanges(t, reviewing);

  $("#d-output-wrap").hidden = !t.output || t.active;
  $("#d-output").textContent = t.output || "";
  $("#d-live-wrap").hidden = !t.active;

  $("#d-history").replaceChildren(...[...t.history].reverse().map(h =>
    el("li", {}, el("span", { class: "muted" }, new Date(h.at).toLocaleString()), " ", h.event)));

  if (t.active && !state.live.timer) pollLive();
}

const PLAN_TIP = "The AI drafts a new plan (read-only), using any feedback below. Unsaved edits are saved first.";
const QUEUE_TIP = "The AI will implement this plan and commit the result; finished runs wait in Review. "
  + "Unsaved edits are saved first.";
const SEND_BACK_TIP = "AI re-plans with your feedback; the committed work stays";
const SKIP_TIP = "Accept the plan as written without asking the AI";

// The flow buttons for a task's status (a paused task only offers Resume).
function drawerActions(t) {
  const act = action => () => doAction(action);
  // With open questions, answering them is the main way forward; plain refining is secondary.
  const asking = hasOpenQuestions(t);
  const planLabel = (t.plan || "").trim() ? "Refine plan with AI" : "Plan with AI";
  const sendBack = () => {
    const b = actionButton("Send back to AI", act("send_back"), "", SEND_BACK_TIP);
    b.dataset.needsFeedback = "";
    b.disabled = !$("#d-feedback").value.trim();
    return b;
  };
  const pauseButton = () => actionButton("Pause", act("pause"), "ghost",
    "Keep automation (Auto-plan / Auto-queue) from changing this task");
  const plan = (cls = "ghost") => actionButton(planLabel, act("plan"), cls, PLAN_TIP);
  const backToUnplanned = () => actionButton("Back to unplanned", act("unplan"), "ghost");

  if (t.paused) {
    return [actionButton("Resume", act("resume"), "primary", "Let automation and the run queue pick the task up again"),
      el("span", { class: "muted small" }, "Paused: automation won't touch it; resume to continue.")];
  }
  switch (t.status) {
    case "unplanned":
      return [plan(asking ? "ghost" : "primary"),
        actionButton("Mark planned (skip AI)", act("mark_planned"), "ghost", SKIP_TIP), pauseButton()];
    case "planning":
      return [actionButton("Cancel planning", act("cancel"), "danger")];
    case "drafted":
      // Answering the questions (above) is the main way forward.
      return [plan(),
        actionButton("Mark planned", act("mark_planned"), "ghost",
          "Accept the plan once its questions are removed. Unsaved edits are saved first."),
        actionButton("Queue anyway", act("queue"), "ghost", QUEUE_TIP),
        backToUnplanned(), pauseButton(),
        el("span", { class: "muted small" }, effectiveAuto(currentProject(), "auto_promote")
          ? "Answer the questions, or delete them from the plan and save; it moves to Planned automatically once no questions remain."
          : "Answer the questions, or delete them from the plan, save, and click Mark planned.")];
    case "planned":
      return [actionButton("Queue to run", act("queue"), asking ? "" : "primary", QUEUE_TIP), plan(),
        asking ? actionButton("Move to drafted", act("mark_drafted"), "ghost",
          "The plan has open questions; park it in Drafted until they're answered") : null,
        backToUnplanned(), pauseButton()].filter(Boolean);
    case "queued":
      return [actionButton("Remove from queue", act("dequeue")), pauseButton()];
    case "running":
      return [actionButton("Cancel run", act("cancel"), "danger")];
    case "review":
      return [actionButton("Approve → Finished", act("approve"), "primary",
          "The work is good; move it to Finished. A note is committed to the repository."),
        sendBack(), actionButton("Reopen", act("reopen"), "ghost", "Back to Planned without asking the AI")];
    case "done":
      return [actionButton("Reopen", act("reopen"), "", "Back to Planned without asking the AI"), sendBack()];
    case "failed":
      return [actionButton("Queue to run again", act("queue"), asking ? "" : "primary", QUEUE_TIP), plan(),
        actionButton("Mark planned (skip AI)", act("mark_planned"), "ghost", SKIP_TIP), pauseButton()];
  }
  return [];
}

// Files changed by the task's commit, fetched once per commit while the drawer shows it.
async function renderChanges(t, show) {
  const box = $("#d-changes");
  const p = currentProject();
  const ref = t.commit || (p && p.svn_tracking === true && (t.changes || []).length
    ? `svn:${t.changes.length}:${t.checkin || ""}` : "");
  if (!show || !ref) { box.hidden = true; state.changesKey = null; return; }
  const key = `${state.pid}/${t.id}/${ref}`;
  if (state.changesKey === key) return;
  state.changesKey = key;
  box.hidden = true;
  let files;
  try {
    files = (await api("GET", `/api/projects/${state.pid}/tasks/${t.id}/changes`)).files;
  } catch { files = []; }
  if (state.changesKey !== key) return;
  $("#d-changes-count").textContent = `(${files.length})`;
  $("#d-changes-list").replaceChildren(...files.map(f =>
    el("li", {}, el("span", { class: `change-status s-${f.status}` }, f.status), " ", f.path)));
  box.hidden = !files.length;
}

// The drawer's model selects list the models of the task's effective provider. Blank falls back
// to the project's model only when the task uses the project's AI.
function fillTaskModels(planModel, codeModel) {
  const p = currentProject();
  const own = projectProvider(p);
  const provider = $("#d-provider").value || own;
  const blank = provider === own ? "Project default" : "Global default";
  const stillValid = () => ($("#d-provider").value || own) === provider && !$("#drawer").hidden;
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
    plan_trust: $("#d-plan-trust").value,
    plan: $("#d-plan").value,
  };
}

async function saveTask(quiet) {
  const t = openTask();
  if (!t) return false;
  try {
    const updated = await api("PATCH", `/api/projects/${state.pid}/tasks/${t.id}`, formFields());
    Object.assign(t, updated); markTasksChanged();
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
    if (!confirm(`The plan still has ${plural(t.questions.length, "unanswered question")}. Queue anyway?`)) return;
  }
  if (state.dirty && action !== "cancel" && !(await saveTask(true))) return;
  const body = { action, ...extra };
  if (action === "plan" || action === "send_back") body.feedback = $("#d-feedback").value;
  if (action === "approve") body.note = $("#d-approval-note").value;
  try {
    const updated = await api("POST", `/api/projects/${state.pid}/tasks/${t.id}/action`, body);
    Object.assign(t, updated); markTasksChanged();
    if (action === "approve") $("#d-approval-note").value = "";
    if (action === "plan" || action === "send_back") {
      $("#d-feedback").value = "";
      renderImagePreviews($("#d-feedback"), $("#d-feedback-img"));
      resetQuestionAnswers();
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
    state.tasks = state.tasks.filter(x => x.id !== t.id); markTasksChanged();
    state.dirty = false;
    closeDrawer();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- live output ---------------- */

const ACTIVITY_ICON = { say: "💬", tool: "🔧", error: "⚠", note: "·" };

// Appends only new events; re-renders fully if the list shrank (cap rollover or a new job).
function renderActivity(list, events) {
  const stick = isScrolledToBottom(list);
  let done = Number(list.dataset.count || 0);
  if (events.length < done || (done && list.dataset.first !== String(events[0] && events[0].at))) {
    list.replaceChildren();
    done = 0;
  }
  if (events.length === done) return;
  list.dataset.first = String(events[0].at);
  list.append(...events.slice(done).map(ev => {
    const long = ev.detail && ev.detail !== ev.label;
    const label = el("span", { class: "act-label" }, ev.label);
    return el("li", { class: `act ${ev.kind}` },
      el("span", { class: "act-icon" }, ACTIVITY_ICON[ev.kind] || "·"),
      long ? el("details", {}, el("summary", {}, label, " ", el("span", { class: "muted act-at" }, `+${Math.round(ev.at)}s`)),
                el("pre", { class: "act-detail" }, ev.detail))
           : el("span", {}, label, " ", el("span", { class: "muted act-at" }, `+${Math.round(ev.at)}s`)));
  }));
  list.dataset.count = events.length;
  if (stick) list.scrollTop = list.scrollHeight;
}

function showLiveTab(raw) {
  $("#d-activity").hidden = raw;
  $("#d-live").hidden = !raw;
  $("#d-tab-activity").classList.toggle("active", !raw);
  $("#d-tab-raw").classList.toggle("active", raw);
}

async function pollLive() {
  clearTimeout(state.live.timer);
  state.live.timer = null;
  const t = openTask();
  if (!t || !t.active) return;
  state.live.timer = -1; // mark as running while the request is in flight
  try {
    const live = await api("GET", `/api/projects/${state.pid}/tasks/${t.id}/live`);
    if (state.openTid !== t.id) { state.live.timer = null; return; }
    renderActivity($("#d-activity"), live.events || []);
    $("#d-current").textContent = live.current ? `▶ ${live.current}` : "";
    $("#d-kind").textContent = live.kind === "plan" ? "· Planning" : live.kind ? "· Running" : "";
    if (state.live.tabFor !== t.id) {  // first poll for this task: pick a default tab
      state.live.tabFor = t.id;
      state.live.userTab = false;
      showLiveTab(!(live.events || []).length);
    } else if ((live.events || []).length && !state.live.userTab && $("#d-activity").hidden) {
      showLiveTab(false);
    }
    const pre = $("#d-live");
    const atBottom = isScrolledToBottom(pre, 20);
    pre.textContent = live.output || "Waiting for output…";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
    $("#d-elapsed").textContent = live.active ? `${live.elapsed}s` : "finished";
    if (!live.active) { state.live.timer = null; loadTasks(); return; }
  } catch { /* transient; next tick retries */ }
  state.live.timer = setTimeout(pollLive, 1000);
}

/* ---------------- chat ---------------- */

function openChat() {
  if (!$("#drawer").hidden && !closeDrawer()) return;
  $("#chat").hidden = false;
  loadChat();
  $("#c-input").focus();
}

function closeChat() {
  $("#chat").hidden = true;
  clearTimeout(state.chat.timer);
  state.chat.timer = null;
}

async function loadChat() {
  clearTimeout(state.chat.timer);
  state.chat.timer = null;
  const pid = state.pid;
  if (!pid || $("#chat").hidden) return;
  try {
    const data = await api("GET", `/api/projects/${pid}/chat`);
    state.chat.errorShown = false;
    if (pid === state.pid) renderChat(data);
  } catch (e) { if (!state.chat.errorShown) toast(e.message, true); state.chat.errorShown = true; }
  if (state.chat.active && !$("#chat").hidden) state.chat.timer = setTimeout(loadChat, 1500);
}

// Chat uses the project's chat model, else its planning model (else the global default).
function renderChatWhere() {
  const p = currentProject();
  const model = p ? p.chat_model || p.plan_model || "" : "";
  $("#c-where").textContent = p ? `${p.name} · ${providerName(projectProvider(p))}${model ? " · " + model : ""}` : "";
}

function renderChat(data) {
  const log = $("#c-log");
  const atBottom = isScrolledToBottom(log);
  renderChatWhere();
  state.chat.active = data.active;
  $("#c-empty").hidden = data.messages.length > 0;
  $("#c-messages").replaceChildren(...data.messages.map(m => el("li", {
    class: `${m.role}${m.error ? " error" : ""}`,
  }, el("span", { class: "when" }, `${m.role === "user" ? "You" : "AI"} · ${new Date(m.at).toLocaleTimeString()}`),
     m.text, m.role === "user" ? el("div", { class: "image-previews" }, thumbnails(m.text)) : null)));
  $("#c-live-wrap").hidden = !data.active;
  const events = data.events || [];
  renderActivity($("#c-activity"), events);
  $("#c-current").textContent = data.current ? `▶ ${data.current}` : "";
  $("#c-raw-toggle").hidden = !data.active;
  if (!events.length) $("#c-live").hidden = false;
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
    if (state.chat.active) loadChat();
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

async function sendChat(ev) {
  ev.preventDefault();
  const input = $("#c-input");
  const message = input.value.trim();
  if (!message || state.chat.active) return;
  $("#c-send").disabled = true;
  if (await chatRequest("POST", "", { message })) {
    input.value = "";
    refreshImagePreviews();
    $("#c-log").scrollTop = $("#c-log").scrollHeight;
  } else $("#c-send").disabled = false;
}

function setupChat() {
  $("#chat-btn").onclick = openChat;
  $("#chat-form").onsubmit = sendChat;
  $("#c-input").onkeydown = e => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) sendChat(e);
  };
  $("#d-tab-activity").onclick = () => { state.live.userTab = true; showLiveTab(false); };
  $("#d-tab-raw").onclick = () => { state.live.userTab = true; showLiveTab(true); };
  $("#c-raw-toggle").onclick = () => {
    const pre = $("#c-live");
    pre.hidden = !pre.hidden;
    $("#c-raw-toggle").textContent = pre.hidden ? "Show raw output" : "Hide raw output";
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
  const syncGitWarning = () => {
    $("#pf-git-warning").hidden = !form.git_tracking.checked;
    form.secrets_ack.required = form.git_tracking.checked;
  };
  const open = () => {
    form.reset();
    syncGitWarning();
    for (const select of [form.plan_model, form.code_model]) {
      fillModelSelectLive(select, form.provider.value, "", () => dialog.open, "Global default");
    }
    syncLocation();
    showError($("#project-form-error"), "");
    loadSshServers(true);
    dialog.showModal();
  };
  const syncLocation = () => {
    const ssh = form.location.value === "ssh";
    $$(".ssh-only", form).forEach(n => { n.hidden = !ssh; });
    form.ssh_target.required = ssh;
    resetBrowser();
  };
  form.git_tracking.onchange = syncGitWarning;
  $("#add-project-btn").onclick = open;
  $("#empty-add-btn").onclick = open;
  $$("input[name=location]", form).forEach(r => { r.onchange = syncLocation; });
  form.provider.onchange = async () => {
    const provider = form.provider.value;
    const selects = [form.plan_model, form.code_model];
    const current = selects.map(s => s.dataset.value || "");
    for (const select of selects) fillModelSelect(select, provider, "");
    await ensureModels(provider, false, "");
    if (form.provider.value !== provider) return;
    selects.forEach((select, i) => fillModelSelect(select, provider, modelFor(provider, current[i], "")));
  };
  for (const select of [form.plan_model, form.code_model]) {
    select.onchange = () => pickModel(select, form.provider.value);
  }
  form.ssh_target.addEventListener("change", () => {
    const server = matchServer(form.ssh_target.value);
    if (server && server.port && !form.ssh_port.value) form.ssh_port.value = server.port;
    resetBrowser();
  });
  form.ssh_port.addEventListener("change", resetBrowser);
  form.onsubmit = async ev => {
    ev.preventDefault();
    const btn = $("#project-submit");
    btn.textContent = "Connecting…";
    const f = Object.fromEntries(new FormData(form));
    f.create = form.create.checked;
    f.git_tracking = form.git_tracking.checked;
    f.secrets_ack = form.git_tracking.checked && form.secrets_ack.checked;
    await withBusy(btn, $("#project-form-error"), async () => {
      const project = await api("POST", "/api/projects", f);
      dialog.close();
      state.projects.push(project);
      ssh.loaded = false; // a new project host becomes a suggestion
      renderProjects();
      loadReachability();
      await selectProject(project.id);
      toast(`Added ${project.name}`);
    });
    btn.textContent = "Add project";
  };
}

// The form's dotted input names ("claude.plan", "timeouts.run"…) are the settings fields it edits.
const settingFields = form => [...new Set([...form.elements].map(e => e.name).filter(n => n.includes(".")))];

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
    status.textContent = r.ok ? `OK: ${plural(r.count, "tool-capable model")}` : r.error;
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
  return CLI.has(group) ? [settings.commands[group], key] : [settings[group], key];
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
      for (const name of settingFields(form)) {
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
    const out = { commands: Object.fromEntries([...CLI].map(p => [p, {}])), timeouts: {}, automation: {}, git: {},
      endpoints: $$("#endpoint-list .endpoint").map(readEndpoint) };
    for (const name of settingFields(form)) {
      const [obj, key] = settingPath(name, out);
      const input = form.elements[name];
      obj[key] = input.type === "checkbox" ? input.checked
        : input.type === "number" ? Number(input.value) : input.value.trim();
    }
    try {
      const saved = await api("PUT", "/api/settings", out);
      applyProviderSettings(saved);
      state.automation = saved.automation || {};
      renderAutoDefaults();
      renderAutoFlow();
      dialog.close();
      toast("Settings saved");
      loadTasks(); // turning an automation mode on may have moved tasks
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

async function showCheckin() {
  const pid = state.pid;
  showError($("#ci-error"), "");
  $("#ci-tasks").replaceChildren(el("li", { class: "muted" }, "Loading…"));
  $("#ci-status").replaceChildren();
  $("#ci-message").value = "";
  $("#ci-submit-btn").disabled = true;
  $("#checkin-dialog").showModal();
  try {
    const data = await api("GET", `/api/projects/${pid}/checkin`);
    $("#ci-tasks").replaceChildren(...(data.tasks.length ? data.tasks.map(t => {
      const files = t.changes.length ? el("details", {}, el("summary", { class: "muted small" },
        plural(t.changes.length, "file")),
        ...t.changes.map(f => el("div", { class: "small" },
          el("span", { class: `change-status s-${f.status}` }, f.status), " ", f.path))) : null;
      return el("li", {}, el("strong", {}, `#${t.id} ${t.title}`), files);
    }) : [el("li", { class: "muted" }, "No finished tasks are waiting.")]));
    $("#ci-count").textContent = data.status.length;
    $("#ci-status").replaceChildren(...(data.status.length ? data.status.map(f =>
      el("li", {}, el("span", { class: `change-status s-${f.status}` }, f.status), " ", f.path))
      : [el("li", { class: "muted" }, "Nothing to check in.")]));
    $("#ci-message").value = data.message;
    $("#ci-submit-btn").disabled = !data.status.length;
  } catch (e) { showError($("#ci-error"), e.message); }
}

async function submitCheckin() {
  await withBusy($("#ci-submit-btn"), $("#ci-error"), async () => {
    const data = await api("POST", `/api/projects/${state.pid}/checkin`, { message: $("#ci-message").value });
    $("#checkin-dialog").close();
    toast(data.revision ? `Checked in as ${data.revision}` : "Nothing to check in");
    loadTasks();
    loadReachability();
  });
}

function renderRemote(r) {
  state.remoteUrl = r.url || "";
  $("#r-mode-text").textContent = syncModeName(r.sync_mode);
  const parts = [r.url ? `origin ${r.url}` : "", r.branch ? `Branch ${r.branch}` : "Detached HEAD"].filter(Boolean);
  if (r.upstream) parts.push(`tracking ${r.upstream}`, `${r.ahead} ahead, ${r.behind} behind`);
  else if (r.url) parts.push(`not pushed yet (${plural(r.ahead, "local commit")})`);
  else parts.push("no remote set");
  if (r.dirty) parts.push("uncommitted changes");
  $("#r-status").textContent = parts.join(" · ");
  $("#r-sync-btn").disabled = !r.url;
}

async function showRemote() {
  showError($("#r-error"), "");
  $("#r-log").hidden = true;
  $("#r-status").textContent = "Loading…";
  const p = currentProject();
  $("#r-mode-text").textContent = syncModeName(p && p.sync_mode);
  $("#r-sync-btn").disabled = true;
  $("#remote-dialog").showModal();
  try {
    renderRemote(await api("GET", `/api/projects/${state.pid}/remote`));
  } catch (e) { $("#r-status").textContent = ""; showError($("#r-error"), e.message); }
}

function setupRemoteDialog() {
  $("#sync-btn").onclick = showRemote;
  $("#r-open-settings").onclick = () => {
    $("#remote-dialog").close();
    openProjectSettings();
  };
  $("#r-sync-btn").onclick = async () => {
    const btn = $("#r-sync-btn");
    $("#r-log").hidden = true;
    btn.textContent = "Syncing…";
    await withBusy(btn, $("#r-error"), async () => {
      try {
        // The server syncs with the project's saved mode.
        const r = await api("POST", `/api/projects/${state.pid}/remote/sync`, { push: $("#r-push").checked });
        renderRemote(r);
        $("#r-log").textContent = r.log.join("\n");
        $("#r-log").hidden = false;
        toast("Synced");
        loadReachability();
      } catch (e) {
        toast(e.message, true);
        throw e;
      }
    });
    btn.textContent = "Sync now";
    btn.disabled = !state.remoteUrl;
    loadTasks();
  };
}

/* ---------------- wiring ---------------- */

function setupDrawer() {
  for (const id of ["#d-title", "#d-desc", "#d-provider", "#d-plan-trust", "#d-plan"]) {
    $(id).addEventListener("input", () => { state.dirty = true; renderEditActions(); });
  }
  $("#d-provider").addEventListener("change", () => {
    // Keep only models the new provider lists; a model name for another AI would fail there.
    const provider = $("#d-provider").value || projectProvider(currentProject());
    fillTaskModels(modelFor(provider, $("#d-plan-model").dataset.value || ""),
      modelFor(provider, $("#d-code-model").dataset.value || ""));
  });
  for (const id of ["#d-plan-model", "#d-code-model"]) {
    $(id).onchange = e => {
      const provider = $("#d-provider").value || projectProvider(currentProject());
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
  $("#d-feedback").addEventListener("input", () => {
    const empty = !$("#d-feedback").value.trim();
    for (const b of $$("#d-flow-actions [data-needs-feedback]")) b.disabled = empty;
  });
}

function setupGlobalKeys() {
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
    if (state.mode === "ssh") return; // the hidden drawer/chat must stay open
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
    if (state.mode === "ssh") return; // hidden project UI must not react to keys
    // Esc closes an open dialog or drawer first; only then does it leave Project settings.
    if (e.key === "Escape" && state.view === "settings" && $("#drawer").hidden && $("#chat").hidden
        && !document.querySelector("dialog[open]")) {
      closeProjectSettings();
      return;
    }
    if (e.key === "Escape" && $("#drawer").hidden && $("#chat").hidden && state.selected.size
        && !document.querySelector("dialog[open]")) clearSelection();
    if (e.key === "Escape" && !$("#drawer").hidden && !document.querySelector("dialog[open]")) closeDrawer();
    if (e.key === "Escape" && !$("#chat").hidden && !document.querySelector("dialog[open]")) closeChat();
    if ((e.ctrlKey || e.metaKey) && e.key === "s" && !$("#drawer").hidden) { e.preventDefault(); saveTask(); }
  });
  window.addEventListener("beforeunload", e => { if (state.dirty || state.settingsDirty) e.preventDefault(); });
}

// ---- SSH tools -----------------------------------------------------------------------
// The sidebar switches between the project list and these tools. Switching only hides the
// project UI (body.mode-ssh, see app.css): nothing is closed, reset or reselected, and the
// polling of tasks and reachability keeps updating the hidden DOM.

const SSH_TOOLS = { servers: "Servers", keys: "Keys", install: "Install keys", network: "Network", tunnels: "Tunnels" };
const ssh = {
  servers: [], // saved servers [{id, name, target, port, os}] from /api/ssh/servers
  suggestions: [], // [{target, port, source, ssh_os?}]: project hosts and ssh-config aliases not saved yet
  loaded: false,
  keys: [], // keys listed in the Keys panel
  installKeys: [],
  tunnels: [],
  running: 0,
  timer: null,
  confirmed: new Set(), // tunnel ids whose 0.0.0.0 bind was confirmed this session
  highlight: null,
  network: null, // last /api/ssh/network result, kept in memory only
  networkBusy: false,
};

const sshSourceValue = t => `${t.target}\t${t.port || ""}\t${t.os || t.ssh_os || ""}`;
function sshSource(value) {
  const [target, port, os] = (value || "local").split("\t");
  return target === "local" ? null : { target, port: port ? Number(port) : null, os: os && os !== "auto" ? os : null };
}
const sshLabel = t => t.port ? `${t.target}:${t.port}` : t.target;

function sshLog(lines) {
  const log = $("#ssh-log");
  log.textContent = [].concat(lines).join("\n");
  log.hidden = false;
}

function setSidebarMode(mode, save = true) {
  state.mode = mode === "ssh" ? "ssh" : "projects";
  if (save) localStorageSet("pg.sidebarMode", state.mode);
  const on = state.mode === "ssh";
  document.body.classList.toggle("mode-ssh", on);
  $("#sidebar-projects").hidden = on;
  $("#sidebar-ssh").hidden = !on;
  $("#ssh-tools-view").hidden = !on;
  $("#mode-projects-btn").setAttribute("aria-selected", String(!on));
  $("#mode-ssh-btn").setAttribute("aria-selected", String(on));
  if (on) openSshTool(state.sshTool);
  loadTunnels();
  resetTunnelTimer();
}

function resetTunnelTimer() {
  clearInterval(ssh.timer);
  ssh.timer = setInterval(() => { if (!document.hidden) loadTunnels(); }, state.mode === "ssh" ? 5000 : 30000);
}

// Saved servers are managed by the user; suggestions are only ever offered, never saved for them.
async function loadSshServers(force = false) {
  if (ssh.loaded && !force) return;
  try {
    const data = await api("GET", "/api/ssh/servers");
    ssh.servers = data.servers;
    ssh.suggestions = data.suggestions;
    ssh.loaded = true;
  } catch { /* keep what we have; the pickers still work with "This computer" */ }
  fillSshPickers();
}

// A saved server whose target is exactly `target` (case-insensitive), or undefined.
function matchServer(target) {
  const t = String(target || "").trim().toLowerCase();
  return t ? ssh.servers.find(s => s.target.toLowerCase() === t) : undefined;
}

function fillSshPickers() {
  for (const sel of $$("#ssh-key-source, #ssh-gen-source, #ssh-install-source")) {
    const keep = sel.value;
    while (sel.options.length > 1) sel.remove(1); // everything after "This computer"
    sel.append(...ssh.servers.map(s => el("option", { value: sshSourceValue(s) }, s.name === s.target ? sshLabel(s) : `${s.name} (${sshLabel(s)})`)));
    if (ssh.suggestions.length) {
      sel.append(el("optgroup", { label: "Suggestions" },
        ...ssh.suggestions.map(s => el("option", { value: sshSourceValue(s) }, sshLabel(s)))));
    }
    if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
  }
  const all = [...ssh.servers, ...ssh.suggestions];
  for (const list of $$("#ssh-targets, #pf-ssh-servers")) {
    list.replaceChildren(...all.map(t => el("option", { value: t.target }, t.port ? `port ${t.port}` : "")));
  }
}

const OS_LABELS = { auto: "Auto-detect", posix: "Linux/macOS", windows: "Windows" };

function renderServerPanel() {
  $("#ssh-server-rows").replaceChildren(...(ssh.servers.length ? ssh.servers.map(s => el("tr", {},
    el("td", {}, s.name), el("td", { class: "mono", title: s.target }, s.target), el("td", {}, s.port || ""), el("td", {}, OS_LABELS[s.os] || s.os),
    el("td", {}, el("div", { class: "actions" },
      el("button", { class: "small ghost", type: "button", onclick: () => editServer(s) }, "Edit"),
      el("button", { class: "small ghost danger", type: "button", onclick: () => deleteServer(s) }, "Delete")))))
    : [el("tr", {}, el("td", { colspan: 5, class: "muted" }, "No saved servers yet."))]));
  $("#ssh-suggestion-rows").replaceChildren(...(ssh.suggestions.length ? ssh.suggestions.map(s => el("tr", {},
    el("td", { class: "mono", title: s.target }, s.target), el("td", {}, s.port || ""), el("td", { class: "muted" }, s.source),
    el("td", {}, el("button", { class: "small ghost", type: "button", onclick: () => suggestServer(s) }, "Add"))))
    : [el("tr", {}, el("td", { colspan: 4, class: "muted" }, "No suggestions."))]));
}

const serverSpec = s => ({ id: s.id, name: s.name, target: s.target, port: s.port, os: s.os });

async function saveServers(list) {
  const data = await api("PUT", "/api/ssh/servers", { servers: list });
  ssh.servers = data.servers;
  ssh.suggestions = data.suggestions;
  ssh.loaded = true;
  fillSshPickers();
  renderServerPanel();
}

function resetServerForm() {
  const f = $("#ssh-server-form");
  f.reset();
  f.elements.id.value = "";
  $("#ssh-server-form-title").textContent = "Add a server";
  $("#ssh-server-cancel").hidden = true;
  showError($("#ssh-server-error"), "");
}

function editServer(s) {
  const f = $("#ssh-server-form");
  for (const k of ["id", "name", "target", "port", "os"]) f.elements[k].value = s[k] ?? "";
  $("#ssh-server-form-title").textContent = `Edit ${s.name}`;
  $("#ssh-server-cancel").hidden = false;
  showError($("#ssh-server-error"), "");
  f.scrollIntoView({ block: "nearest" });
}

// Adding a suggestion only fills the form: nothing is saved until the user presses Save.
function suggestServer(s) {
  resetServerForm();
  const f = $("#ssh-server-form");
  f.elements.target.value = s.target;
  f.elements.port.value = s.port || "";
  f.elements.os.value = s.ssh_os || "auto";
  f.scrollIntoView({ block: "nearest" });
  f.elements.name.focus();
}

async function deleteServer(s) {
  if (!confirm(`Delete the server "${s.name}"? Projects and tunnels that use it are not changed.`)) return;
  try { await saveServers(ssh.servers.filter(x => x.id !== s.id).map(serverSpec)); }
  catch (e) { toast(e.message, true); }
}

async function submitServer(e) {
  e.preventDefault();
  const f = e.target;
  const spec = { id: f.elements.id.value, name: f.elements.name.value.trim(), target: f.elements.target.value.trim(),
    port: f.elements.port.value ? Number(f.elements.port.value) : null, os: f.elements.os.value };
  const list = ssh.servers.map(serverSpec);
  const at = list.findIndex(s => s.id === spec.id);
  if (at >= 0) list[at] = spec; else list.push(spec);
  try {
    await saveServers(list);
    resetServerForm();
    toast("Server saved");
  } catch (err) { showError($("#ssh-server-error"), err.message); }
}

async function openSshTool(tool) {
  if (!SSH_TOOLS[tool]) tool = "keys";  state.sshTool = tool;
  localStorageSet("pg.sshTool", tool);
  for (const li of $$("#ssh-nav li")) li.classList.toggle("active", li.dataset.tool === tool);
  for (const t of Object.keys(SSH_TOOLS)) $(`#ssh-${t}-panel`).hidden = t !== tool;
  $("#ssh-title").textContent = SSH_TOOLS[tool];
  $("#ssh-log").hidden = true;
  await loadSshServers(tool === "servers"); // always fresh suggestions on the Servers tool
  if (state.sshTool !== tool) return; // the user moved on while the servers loaded
  if (tool === "servers") renderServerPanel();
  if (tool === "keys") loadSshKeys();
  if (tool === "install") loadInstallKeys();
  if (tool === "tunnels") renderTunnelPanel();
  if (tool === "network") loadNetwork();
}

// ---- network diagram: one scan per open/refresh, a one-time radial layout, no animation ----

function svgEl(tag, attrs = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

async function loadNetwork() {
  if (ssh.networkBusy) return;
  ssh.networkBusy = true;
  const busy = $("#ssh-network-busy");
  if (ssh.network) renderNetwork();
  busy.textContent = `Scanning ${ssh.servers.length} server${ssh.servers.length === 1 ? "" : "s"}…`;
  busy.hidden = false;
  $("#ssh-network-refresh").disabled = true;
  try {
    ssh.network = await api("POST", "/api/ssh/network");
    renderNetwork();
    const failed = ssh.network.nodes.filter(n => n.error && n.error !== "not scanned");
    if (failed.length) toast(`Could not scan ${failed.map(n => n.name).join(", ")}`, true);
  } catch (e) { toast(e.message, true); }
  finally {
    ssh.networkBusy = false;
    busy.hidden = true;
    $("#ssh-network-refresh").disabled = false;
  }
}

function renderNetwork() {
  const data = ssh.network, svg = $("#ssh-network-svg"), detail = $("#ssh-network-detail");
  svg.replaceChildren();
  $("#ssh-network-stamp").textContent = data ? `Generated at ${data.generated}` : "";
  if (!data) return;
  const W = 800, H = 560, cx = W / 2, cy = H / 2, R = 24, byId = new Map(data.nodes.map(n => [n.id, n]));
  const servers = data.nodes.filter(n => n.kind === "server");
  const ring = Math.min(170, 90 + servers.length * 14);
  servers.forEach((n, i) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / servers.length;
    n.x = cx + ring * Math.cos(a); n.y = cy + ring * Math.sin(a); n.angle = a;
  });
  const local = byId.get("local"); local.x = cx; local.y = cy;
  const kids = new Map(); // server id -> external nodes pointing at it
  for (const n of data.nodes.filter(n => n.kind === "external")) {
    const edge = data.edges.find(e => e.from === n.id), target = edge && byId.get(edge.to);
    if (!target || target.x === undefined) { n.angle = 0; n.x = W - 40; n.y = 40; continue; }
    kids.set(target.id, [...(kids.get(target.id) || []), n]);
  }
  for (const [sid, list] of kids) {
    const base = byId.get(sid), spread = 0.34;
    list.forEach((n, i) => {
      const a = base.angle + (i - (list.length - 1) / 2) * spread, r = Math.min(255, ring + 85 + (i % 2) * 22);
      n.x = cx + r * Math.cos(a) * 1.35; n.y = cy + r * Math.sin(a);
      n.x = Math.max(60, Math.min(W - 60, n.x)); n.y = Math.max(30, Math.min(H - 30, n.y));
    });
  }
  const short = (s, n = 18) => s.length > n ? s.slice(0, n - 1) + "…" : s;
  const marker = (id, cls) => svgEl("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
    svgEl("path", { d: "M0,0 L10,5 L0,10 z", class: cls }));
  svg.append(svgEl("defs", {}, marker("net-arrow", "net-head"), marker("net-arrow-admin", "net-head admin")));

  const edgeEls = data.edges.map((e, i) => {
    const a = byId.get(e.from), b = byId.get(e.to);
    const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len;
    const bend = Math.min(60, len * 0.18);
    const sx = a.x + ux * R, sy = a.y + uy * R, ex = b.x - ux * (R + 4), ey = b.y - uy * (R + 4);
    const qx = (sx + ex) / 2 - uy * bend, qy = (sy + ey) / 2 + ux * bend;
    const label = `${a.name} → ${b.name}: ${e.keys.length} key${e.keys.length === 1 ? "" : "s"}${e.guessed ? " (guessed)" : ""}`;
    const path = svgEl("path", {
      d: `M${sx.toFixed(1)},${sy.toFixed(1)} Q${qx.toFixed(1)},${qy.toFixed(1)} ${ex.toFixed(1)},${ey.toFixed(1)}`,
      class: `net-edge${e.guessed ? " guess" : ""}${e.admin_file ? " admin" : ""}`,
      "stroke-width": Math.min(5, 1.4 + e.keys.length * 0.7), "marker-end": `url(#net-arrow${e.admin_file ? "-admin" : ""})`,
    });
    const hit = svgEl("path", { d: path.getAttribute("d"), class: "net-hit", onclick: () => showEdge(e), onmouseenter: () => focus(null, e), onmouseleave: () => focus() },
      svgEl("title", {}, label));
    return { e, path, group: svgEl("g", {}, path, hit) };
  });
  svg.append(svgEl("g", {}, ...edgeEls.map(x => x.group)));

  const nodeEls = data.nodes.map(n => {
    const cls = `net-node ${n.kind}${n.error && n.error !== "not scanned" ? " error" : ""}`;
    const glyph = n.kind === "local" ? "▣" : n.kind === "external" ? "?" : n.os === "windows" ? "⊞" : "▤";
    const g = svgEl("g", { class: cls, tabindex: 0, role: "button", "aria-label": n.name, transform: `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})`,
        onclick: () => showNode(n), onkeydown: ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); showNode(n); } },
        onmouseenter: () => focus(n), onmouseleave: () => focus(), onfocus: () => focus(n), onblur: () => focus() },
      svgEl("title", {}, n.kind === "external" ? `${n.name}\n${n.fingerprint}` : n.error ? `${n.name}\n${n.error}` : n.name),
      svgEl("circle", { r: n.kind === "external" ? R - 6 : R }),
      svgEl("text", { class: "glyph", y: 5, "text-anchor": "middle" }, glyph),
      svgEl("text", { class: "label", y: R + 14, "text-anchor": "middle" }, short(n.name)));
    return { n, g };
  });
  svg.append(svgEl("g", {}, ...nodeEls.map(x => x.g)));

  function focus(node, edge) {
    const hot = new Set(), hotEdges = new Set();
    if (node) data.edges.forEach(e => { if (e.from === node.id || e.to === node.id) { hotEdges.add(e); hot.add(e.from); hot.add(e.to); } });
    if (edge) { hotEdges.add(edge); hot.add(edge.from); hot.add(edge.to); }
    const on = node || edge;
    for (const x of edgeEls) { x.path.classList.toggle("dim", !!on && !hotEdges.has(x.e)); x.path.classList.toggle("hot", hotEdges.has(x.e)); }
    for (const x of nodeEls) x.g.classList.toggle("dim", !!on && !hot.has(x.n.id));
  }

  const keyRows = e => e.keys.map(k => el("li", {}, `${k.type} ${k.fingerprint}`, k.comment ? ` · ${k.comment}` : "", ` · ${k.file}`,
    k.options ? el("div", { class: "muted" }, `options: ${k.options}`) : null));
  function showEdge(e) {
    const a = byId.get(e.from), b = byId.get(e.to);
    detail.replaceChildren(el("strong", {}, `${a.name} → ${b.name}`), e.guessed ? " (guessed from the key comment)" : "", el("ul", {}, keyRows(e)));
  }
  function showNode(n) {
    const out = data.edges.filter(e => e.from === n.id), inn = data.edges.filter(e => e.to === n.id);
    detail.replaceChildren(el("strong", {}, n.name), n.target ? ` (${n.target})` : "",
      n.fingerprint ? el("div", {}, n.fingerprint) : null,
      n.error ? el("div", { class: "error" }, n.error) : null,
      ...(n.files || []).map(f => el("div", { class: "muted" }, `${f.path}${f.readable ? "" : " — not readable"}${f.note ? ` (${f.note})` : ""}`)),
      inn.length ? el("div", {}, `Can log in here: ${inn.map(e => byId.get(e.from).name).join(", ")}`) : null,
      out.length ? el("div", {}, `Can log in to: ${out.map(e => byId.get(e.to).name).join(", ")}`) : null);
  }

  $("#ssh-network-rows").replaceChildren(...(data.edges.length ? data.edges.map(e => el("tr", { onclick: () => showEdge(e) },
    el("td", {}, byId.get(e.from).name), el("td", {}, byId.get(e.to).name),
    el("td", {}, e.keys.map(k => k.comment || k.fingerprint.slice(0, 19)).join(", ")),
    el("td", {}, e.guessed ? "guessed" : e.admin_file ? "admin file" : "matched")))
    : [el("tr", {}, el("td", { colspan: 4, class: "muted" }, "No authorized keys found."))]));
}

async function copyText(text, done) {
  try { await navigator.clipboard.writeText(text); toast(done); }
  catch { toast("Could not copy", true); }
}

async function fetchKeys(value) {
  const src = sshSource(value);
  const q = src ? `source=${encodeURIComponent(src.target)}${src.port ? `&port=${src.port}` : ""}${src.os ? `&os=${src.os}` : ""}` : "source=local";
  return (await api("GET", `/api/ssh/keys?${q}`)).keys;
}

async function loadSshKeys() {
  const rows = $("#ssh-key-rows");
  rows.replaceChildren(el("tr", {}, el("td", { colspan: 5, class: "muted" }, "Loading…")));
  try { ssh.keys = await fetchKeys($("#ssh-key-source").value); }
  catch (e) { ssh.keys = []; rows.replaceChildren(el("tr", {}, el("td", { colspan: 5 }, el("div", { class: "error" }, e.message)))); return; }
  rows.replaceChildren(...(ssh.keys.length ? ssh.keys.map(k => el("tr", {},
    el("td", {}, k.type), el("td", {}, k.name + (k.has_private ? "" : " (no private key)")),
    el("td", { class: "mono", title: k.fingerprint }, k.fingerprint), el("td", { title: k.comment }, k.comment),
    el("td", {}, el("button", { class: "small ghost", onclick: () => copyText(k.text, "Public key copied") }, "Copy"))))
    : [el("tr", {}, el("td", { colspan: 5, class: "muted" }, "No public keys found."))]));
}

async function generateKey(e) {
  e.preventDefault();
  const f = e.target, src = sshSource(f.elements.source.value);
  const body = { name: f.elements.name.value.trim(), type: f.elements.type.value, comment: f.elements.comment.value, passphrase: f.elements.passphrase.value,
                 source: src ? src.target : "local", port: src ? src.port : null, os: src ? src.os : null };
  try {
    const { key } = await api("POST", "/api/ssh/keys/generate", body);
    f.elements.passphrase.value = "";
    sshLog([`Created ${key.path}`, key.fingerprint, key.text]);
    toast("Key generated");
    if ($("#ssh-key-source").value === f.elements.source.value) loadSshKeys();
  } catch (err) { toast(err.message, true); sshLog(err.message); }
}

async function loadInstallKeys() {
  const box = $("#ssh-install-keys");
  box.replaceChildren(el("span", { class: "muted" }, "Loading…"));
  try { ssh.installKeys = await fetchKeys($("#ssh-install-source").value); }
  catch (e) { ssh.installKeys = []; box.replaceChildren(el("div", { class: "error" }, e.message)); return; }
  box.replaceChildren(...(ssh.installKeys.length ? ssh.installKeys.map((k, i) => el("label", { class: "check" },
    el("input", { type: "checkbox", value: String(i) }), `${k.name} — ${k.type}`,
    el("span", { class: "mono fp", title: k.fingerprint }, k.fingerprint)))
    : [el("span", { class: "muted" }, "No public keys found. Generate one first.")]));
}

function addInstallDest() {
  const dest = el("input", { type: "text", list: "ssh-targets", placeholder: "user@host or alias", "aria-label": "Destination", autocomplete: "off" });
  const port = el("input", { type: "number", min: 1, max: 65535, placeholder: "port", "aria-label": "Port" });
  const windows = el("input", { type: "checkbox" });
  dest.addEventListener("change", () => {
    const server = matchServer(dest.value);
    if (!server) return;
    if (server.port && !port.value) port.value = server.port;
    if (server.os !== "auto") windows.checked = server.os === "windows";
  });
  const row = el("div", { class: "ssh-dest" }, dest, port,
    el("label", { class: "check" }, windows, "Windows host"),
    el("button", { type: "button", class: "ghost small", onclick: () => row.remove() }, "Remove"));
  $("#ssh-install-dests").append(row);
}

async function installKeys() {
  const keys = $$("#ssh-install-keys input:checked").map(c => ssh.installKeys[Number(c.value)].text);
  const targets = $$("#ssh-install-dests .ssh-dest").map(r => {
    const [t, p, w] = $$("input", r);
    return { target: t.value.trim(), port: p.value ? Number(p.value) : null, windows: w.checked };
  }).filter(t => t.target);
  if (!keys.length) return toast("Choose at least one public key", true);
  if (!targets.length) return toast("Add at least one destination", true);
  const btn = $("#ssh-install-btn");
  btn.disabled = true;
  try {
    const { results } = await api("POST", "/api/ssh/install", { keys, targets, interactive: $("#ssh-interactive").checked });
    sshLog(results.map(r => r.ok
      ? `${r.target}: ok — ${r.added.length} added, ${r.skipped.length} already there${r.note ? `\n  ${r.note}` : ""}`
      : `${r.target}: FAILED — ${r.error}`));
    const bad = results.filter(r => !r.ok).length;
    toast(bad ? `${bad} of ${results.length} destinations failed` : `Keys installed on ${results.length} destination(s)`, bad > 0);
  } catch (e) { toast(e.message, true); sshLog(e.message); }
  finally { btn.disabled = false; }
}

// ---- tunnels
const tunnelText = t => `${t.direction === "local" ? "L" : "R"} ${t.bind}:${t.listen_port}→${t.dest_host}:${t.dest_port}`;

function tunnelDot(t) {
  const cls = t.state === "running" ? "ok" : t.returncode ? "bad" : "";
  const label = t.state === "running" ? "Running" : t.returncode ? `Stopped (exit ${t.returncode})` : "Stopped";
  return el("span", { class: `p-dot ${cls}`, title: label, "aria-label": label, role: "img" });
}

async function loadTunnels() {
  try {
    const data = await api("GET", "/api/ssh/tunnels");
    ssh.tunnels = data.tunnels;
    ssh.running = data.running;
  } catch { return; } // a failed poll shouldn't toast
  const badge = $("#ssh-running-badge");
  badge.textContent = ssh.running;
  badge.hidden = !ssh.running;
  if (state.mode !== "ssh") return;
  renderTunnelList();
  if (state.sshTool === "tunnels") renderTunnelPanel();
}

function tunnelToggle(t) {
  return el("button", { class: "small ghost", type: "button", title: t.state === "running" ? "Stop" : "Start",
    "aria-label": `${t.state === "running" ? "Stop" : "Start"} ${t.name}`,
    onclick: e => { e.stopPropagation(); toggleTunnel(t); } }, t.state === "running" ? "■" : "▶");
}

function renderTunnelList() {
  $("#ssh-tunnel-list").replaceChildren(...ssh.tunnels.map(t => el("li", {
    tabindex: 0,
    onclick: () => { ssh.highlight = t.id; openSshTool("tunnels"); },
    onkeydown: e => { if (e.key === "Enter") { ssh.highlight = t.id; openSshTool("tunnels"); } },
  }, tunnelDot(t), el("div", { class: "t-text" }, el("div", { class: "p-name" }, t.name),
     el("div", { class: "p-sub", title: tunnelText(t) }, tunnelText(t))), tunnelToggle(t))));
}

async function toggleTunnel(t) {
  try {
    if (t.state === "running") {
      await api("POST", `/api/ssh/tunnels/${t.id}/stop`);
    } else {
      if (t.bind === "0.0.0.0" && !ssh.confirmed.has(t.id)) {
        if (!confirm(`"${t.name}" listens on all interfaces (0.0.0.0): anyone who can reach this network can use the forwarded port. Start it?`)) return;
        ssh.confirmed.add(t.id);
      }
      toast("Starting tunnel…");
      await api("POST", `/api/ssh/tunnels/${t.id}/start`);
      toast("Tunnel running");
    }
  } catch (e) { toast(e.message, true); if (state.sshTool === "tunnels") sshLog(e.message); }
  loadTunnels();
}

function renderTunnelPanel() {
  const rows = $("#ssh-tunnel-rows");
  rows.replaceChildren(...(ssh.tunnels.length ? ssh.tunnels.map(t => el("tr", { class: ssh.highlight === t.id ? "flash" : "", "data-tid": t.id },
    el("td", {}, tunnelDot(t)), el("td", { title: t.name }, t.name),
    el("td", { class: "mono", title: tunnelText(t) }, tunnelText(t),
      t.stderr ? el("details", {}, el("summary", {}, "ssh output"), el("pre", { class: "log" }, t.stderr)) : null),
    el("td", { title: sshLabel(t) }, sshLabel(t)),
    el("td", {}, el("div", { class: "actions" }, tunnelToggle(t),
      el("button", { class: "small ghost", type: "button", onclick: () => editTunnel(t) }, "Edit"),
      el("button", { class: "small ghost danger", type: "button", onclick: () => deleteTunnel(t) }, "Delete")))))
    : [el("tr", {}, el("td", { colspan: 5, class: "muted" }, "No saved tunnels yet."))]));
  const hit = ssh.highlight && $(`#ssh-tunnel-rows tr[data-tid="${ssh.highlight}"]`);
  if (hit) { hit.scrollIntoView({ block: "nearest" }); ssh.highlight = null; }
}

const tunnelSpec = t => ({ id: t.id, name: t.name, target: t.target, port: t.port, direction: t.direction, bind: t.bind,
  listen_port: t.listen_port, dest_host: t.dest_host, dest_port: t.dest_port });

async function saveTunnels(list) {
  const data = await api("PUT", "/api/ssh/tunnels", { tunnels: list });
  ssh.tunnels = data.tunnels;
  ssh.running = data.running;
  renderTunnelList();
  renderTunnelPanel();
}

function updateTunnelFormHints() {
  const f = $("#ssh-tunnel-form");
  $("#ssh-bind-warning").hidden = f.elements.bind.value !== "0.0.0.0";
  $("#ssh-remote-note").hidden = f.elements.direction.value !== "remote";
}

function resetTunnelForm() {
  const f = $("#ssh-tunnel-form");
  f.reset();
  f.elements.id.value = "";
  $("#ssh-tunnel-form-title").textContent = "Add a tunnel";
  $("#ssh-tunnel-cancel").hidden = true;
  showError($("#ssh-tunnel-error"), "");
  updateTunnelFormHints();
}

function editTunnel(t) {
  const f = $("#ssh-tunnel-form");
  for (const k of ["id", "name", "target", "port", "direction", "bind", "listen_port", "dest_host", "dest_port"]) f.elements[k].value = t[k] ?? "";
  $("#ssh-tunnel-form-title").textContent = `Edit ${t.name}`;
  $("#ssh-tunnel-cancel").hidden = false;
  updateTunnelFormHints();
  f.scrollIntoView({ block: "nearest" });
}

async function deleteTunnel(t) {
  if (!confirm(`Delete the tunnel "${t.name}"?`)) return;
  try { await saveTunnels(ssh.tunnels.filter(x => x.id !== t.id).map(tunnelSpec)); }
  catch (e) { toast(e.message, true); }
}

async function submitTunnel(e) {
  e.preventDefault();
  const f = e.target;
  const spec = { id: f.elements.id.value, name: f.elements.name.value.trim(), target: f.elements.target.value.trim(), port: f.elements.port.value ? Number(f.elements.port.value) : null,
    direction: f.elements.direction.value, bind: f.elements.bind.value, listen_port: Number(f.elements.listen_port.value),
    dest_host: f.elements.dest_host.value.trim(), dest_port: Number(f.elements.dest_port.value) };
  const list = ssh.tunnels.map(tunnelSpec);
  const at = list.findIndex(t => t.id === spec.id);
  if (at >= 0) list[at] = spec; else list.push(spec);
  try {
    await saveTunnels(list);
    resetTunnelForm();
    toast("Tunnel saved");
  } catch (err) { showError($("#ssh-tunnel-error"), err.message); }
}

function setupSshTools() {
  state.mode = localStorageGet("pg.sidebarMode") === "ssh" ? "ssh" : "projects";
  state.sshTool = SSH_TOOLS[localStorageGet("pg.sshTool")] ? localStorageGet("pg.sshTool") : "keys";
  $("#mode-projects-btn").onclick = () => setSidebarMode("projects");
  $("#mode-ssh-btn").onclick = () => setSidebarMode("ssh");
  for (const li of $$("#ssh-nav li")) {
    li.onclick = () => openSshTool(li.dataset.tool);
    li.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSshTool(li.dataset.tool); } };
  }
  $("#ssh-key-source").onchange = loadSshKeys;
  $("#ssh-keys-refresh").onclick = loadSshKeys;
  $("#ssh-network-refresh").onclick = loadNetwork;
  $("#ssh-generate-form").onsubmit = generateKey;
  $("#ssh-install-source").onchange = loadInstallKeys;
  $("#ssh-add-dest").onclick = addInstallDest;
  $("#ssh-install-btn").onclick = installKeys;
  $("#ssh-tunnel-form").onsubmit = submitTunnel;
  $("#ssh-tunnel-form").onchange = updateTunnelFormHints;
  $("#ssh-tunnel-cancel").onclick = resetTunnelForm;
  $("#ssh-tunnel-form").elements.target.addEventListener("change", e => {
    const server = matchServer(e.target.value), port = $("#ssh-tunnel-form").elements.port;
    if (server && server.port && !port.value) port.value = server.port;
  });
  $("#ssh-server-form").onsubmit = submitServer;
  $("#ssh-server-cancel").onclick = resetServerForm;
  addInstallDest();
  setSidebarMode(state.mode, false);
}

function init() {
  setupThemePicker();
  setupProjectDialog();
  setupSettingsDialog();
  setupChat();
  for (const [f, p] of Object.entries(IMAGE_FIELDS)) enableImagePaste($(f), $(p));
  setupBatchBar();
  setupRemoteDialog();
  setupProjectSettings();
  setupDrawer();
  setupGlobalKeys();
  setupSshTools();
  loadAutomation();
  $("#new-task").onsubmit = createTask;
  $("#commits-btn").onclick = showCommits;
  $("#checkin-btn").onclick = showCheckin;
  $("#ci-submit-btn").onclick = submitCheckin;
  $("#ci-copy-btn").onclick = async () => {
    try { await navigator.clipboard.writeText($("#ci-message").value); toast("Message copied"); }
    catch { toast("Could not copy", true); }
  };
  $("#terminal-btn").onclick = () => postAndToast("terminal", "Terminal opened");
  $("#vscode-btn").onclick = () => postAndToast("vscode", "VS Code opened");
  for (const btn of $$(".queue-tab")) btn.onclick = () => selectTab(btn.dataset.col);
  $(".queue-tabs").onkeydown = onTabKeydown;
  for (const btn of $$(".auto-flow")) btn.onclick = () => toggleAutoFlow(btn);
  // The chat drawer's model is a quick override that saves immediately.
  $("#c-model").onchange = async e => {
    const model = pickModel(e.target, projectProvider(currentProject()));
    if (model === null) return;
    await updateProject({ chat_model: model });
    renderChatWhere();
  };

  loadProjects().catch(e => toast(e.message, true));
  setInterval(() => { if (!document.hidden) loadTasks(); }, 3000);
  setInterval(() => { if (!document.hidden) loadReachability(); }, 60000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { loadTasks(); loadReachability(); loadTunnels(); }
  });
}

init();
