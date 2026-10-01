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
  view: "board", // "board" or "settings" (the full-page Project settings view)
  settingsDirty: false,
  automation: {}, // the global automation defaults, for the project settings' "Default (…)" labels
  reach: {}, // pid -> {ok, error, vcs?} from /api/projects/status; missing means still checking
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
  t.code_model || "", t.plan_trust || "", t.plan]);

const currentProject = () => state.projects.find(p => p.id === state.pid);
const openTask = () => state.tasks.find(t => t.id === state.openTid);

/* ---------------- model dropdowns ---------------- */

const MODELS = JSON.parse(document.body.dataset.models || "{}");
let PROVIDERS = JSON.parse(document.body.dataset.providers || "[]");
const CUSTOM_MODEL = "\u0000custom";
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
function pickModel(select, provider) {
  if (select.value !== CUSTOM_MODEL) {
    select.dataset.value = select.value;
    return select.value;
  }
  const name = (prompt("Model name:", select.dataset.value) || "").trim();
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
let reachInFlight = false, reachAgain = false;
async function loadReachability() {
  // Don't pile up slow checks; a request made meanwhile runs once the current one finishes.
  if (reachInFlight) { reachAgain = true; return; }
  reachInFlight = true;
  try {
    state.reach = (await api("GET", "/api/projects/status")).status || {};
    renderProjects();
  } catch { /* a failed check shouldn't toast on every poll */ }
  finally { reachInFlight = false; }
  if (reachAgain) { reachAgain = false; loadReachability(); }
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

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

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
  const provider = p.provider || "claude";
  const stillValid = () => currentProject() === p && (p.provider || "claude") === provider;
  fillModelSelectLive($("#c-model"), provider, p.chat_model || "", stillValid, "Planning model");
  fillBatchModel($("#batch-plan-model"), provider);
  fillBatchModel($("#batch-code-model"), provider);
  renderChatWhere();
}

const TRUST_NAMES = { low: "Low", normal: "Normal", high: "High" };

// The level a task's "Project default" plan trust resolves to.
function renderProjectTrust() {
  const p = currentProject();
  if (!p) return;
  const level = TRUST_NAMES[p.plan_trust] ? p.plan_trust : "normal";
  $("#d-plan-trust").options[0].textContent = `Project default (${TRUST_NAMES[level]})`;
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
  const provider = p.provider || "claude";
  form.dataset.pid = p.id;
  delete form.dataset.remoteLoaded;
  delete form.dataset.remoteUrl;
  $("#ps-name").textContent = p.name;
  form.elements.name.value = p.name;
  $("#ps-where").textContent = p.location === "ssh"
    ? `SSH · ${p.ssh_target}${p.ssh_port ? ":" + p.ssh_port : ""} · ${p.path}` : `Local · ${p.path}`;
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
  form.plan_trust.value = TRUST_NAMES[p.plan_trust] ? p.plan_trust : "normal";
  for (const key of AUTO_MODES) form[key].value = p[key] === true ? "on" : p[key] === false ? "off" : "";
  renderAutoDefaults();
  form.auto_sync.checked = p.auto_sync === true;
  form.sync_mode.value = SYNC_MODE_NAMES[p.sync_mode] ? p.sync_mode : "ff-only";
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
  if (tracked) {
    $("#ps-remote-status").textContent = "Loading…";
    loadSettingsRemote(p.id);
  }
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

const AUTO_MODES = ["auto_plan", "auto_queue", "auto_run"];

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
  const p = state.projects.find(x => x.id === form.dataset.pid);
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
  const btn = $("#ps-save-btn");
  btn.disabled = true;
  showError($("#ps-error"), "");
  try {
    Object.assign(p, await api("PATCH", `/api/projects/${p.id}`, fields));
  } catch (e) {
    showError($("#ps-error"), e.message);
    return;
  } finally {
    btn.disabled = false;
  }
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
  try {
    await api("DELETE", `/api/projects/${p.id}`);
    state.view = "board";
    state.settingsDirty = false;
    state.pid = null;
    closeDrawer();
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
  $("#ps-svn-enable-btn").onclick = async () => {
    const p = state.projects.find(x => x.id === form.dataset.pid);
    if (!p) return;
    const btn = $("#ps-svn-enable-btn");
    btn.disabled = true;
    showError($("#ps-error"), "");
    try {
      Object.assign(p, await api("POST", `/api/projects/${p.id}/svn/enable`));
      renderProjects();
      loadReachability();
      if (currentProject() === p) updateVcsButtons(p);
      $("#ps-git-off").hidden = true;
      $("#ps-svn").hidden = false;
      form.svn_tracking_off.checked = false;
      toast("SVN tracking turned on");
    } catch (e) {
      showError($("#ps-error"), e.message);
    } finally {
      btn.disabled = false;
    }
  };
  $("#ps-git-enable-btn").onclick = async () => {
    const p = state.projects.find(x => x.id === form.dataset.pid);
    if (!p) return;
    const btn = $("#ps-git-enable-btn");
    if (!form.git_secrets_ack.checked) {
      showError($("#ps-error"), "Tick the confirmation that this directory contains no secrets first.");
      form.git_secrets_ack.focus();
      return;
    }
    btn.disabled = true;
    showError($("#ps-error"), "");
    try {
      Object.assign(p, await api("POST", `/api/projects/${p.id}/git/enable`, {secrets_ack: true}));
      renderProjects();
      loadReachability();
      if (currentProject() === p) {
        updateVcsButtons(p);
      }
      $("#ps-git-off").hidden = true;
      $("#ps-svn").hidden = true;
      $("#ps-git-on").hidden = false;
      form.auto_sync.checked = p.auto_sync === true;
      form.sync_mode.value = SYNC_MODE_NAMES[p.sync_mode] ? p.sync_mode : "ff-only";
      form.git_tracking_off.checked = false;
      $("#ps-remote-status").textContent = "Loading…";
      loadSettingsRemote(p.id);
      toast("Git tracking turned on");
    } catch (e) {
      showError($("#ps-error"), e.message);
    } finally {
      btn.disabled = false;
    }
  };
}

/* ---------------- tasks & board ---------------- */

async function loadTasks() {
  const pid = state.pid;
  if (!pid) return;
  try {
    const data = await api("GET", `/api/projects/${pid}/tasks`);
    if (pid !== state.pid) return;
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
    $(".activity", tab).hidden = !tasks.some(t => BUSY.has(t.status));
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
  auto_run: "starts the waiting AI queue now",
};

// Colours (or strikes through) each Auto-plan / Auto-queue / Auto-run indicator between the tabs for the current project.
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
      for (const key of AUTO_MODES) settingsForm()[key].value = p[key] === true ? "on" : p[key] === false ? "off" : "";
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
const hasOpenQuestions = t =>PLANNABLE.has(t.status) && (t.questions || []).length > 0;

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
    t.provider && t.provider !== p.provider ? el("span", { class: "chip" }, providerName(t.provider)) : null,
    t.plan_model ? el("span", { class: "chip", title: "Planning model for this task" }, `plan: ${t.plan_model}`) : null,
    t.code_model ? el("span", { class: "chip", title: "Coding model for this task" }, `code: ${t.code_model}`) : null,
    t.plan_trust ? el("span", { class: "chip", title: "Planning trust for this task" }, `trust: ${t.plan_trust}`) : null,
    hasOpenQuestions(t) ? el("span", { class: "chip question", title: t.questions.map(q => q.text).join("\n") },
      `? ${t.questions.length} question${t.questions.length === 1 ? "" : "s"}`) : null,
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
    Object.assign(t, updated);
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
    state.tasks.push(task);
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
const NOT_BUSY = ["unplanned", "drafted", "planned", "queued", "review", "done", "failed"];
const BATCH_VERB = { delete: "Deleted", set_provider: "Updated", set_models: "Updated", plan: "Started planning",
  mark_planned: "Marked planned", mark_drafted: "Moved to drafted", queue: "Queued", dequeue: "Removed from queue",
  unplan: "Moved back", reopen: "Reopened", approve: "Approved", send_back: "Sent back",
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
  state.tasksLoaded = true;
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
  const hasPlan = (t.plan || "").trim().length > 0;
  // With open questions, answering them is the main way forward; plain refining is secondary.
  const asking = hasOpenQuestions(t);
  const planLabel = hasPlan ? "Refine plan with AI" : "Plan with AI";
  const planTip = "The AI drafts a new plan (read-only), using any feedback below. Unsaved edits are saved first.";
  const queueTip = "The AI will implement this plan and commit the result; finished runs wait in Review. "
    + "Unsaved edits are saved first.";
  const sendBackTip = "AI re-plans with your feedback; the committed work stays";
  const sendBack = () => {
    const b = actionButton("Send back to AI", act("send_back"), "", sendBackTip);
    b.dataset.needsFeedback = "";
    b.disabled = !$("#d-feedback").value.trim();
    return b;
  };
  const skipTip = "Accept the plan as written without asking the AI";
  const pauseButton = () => actionButton("Pause", act("pause"), "ghost",
    "Keep automation (Auto-plan / Auto-queue) from changing this task");

  const A = [];
  const act = action => () => doAction(action);
  if (t.paused) {
    A.push(actionButton("Resume", act("resume"), "primary", "Let automation and the run queue pick the task up again"));
    A.push(el("span", { class: "muted small" }, "Paused: automation won't touch it; resume to continue."));
  } else {
  switch (t.status) {
    case "unplanned":
      A.push(actionButton(planLabel, act("plan"), asking ? "ghost" : "primary", planTip));
      A.push(actionButton("Mark planned (skip AI)", act("mark_planned"), "ghost", skipTip));
      A.push(pauseButton());
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
      A.push(pauseButton());
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
      A.push(pauseButton());
      break;
    case "queued":
      A.push(actionButton("Remove from queue", act("dequeue")));
      A.push(pauseButton());
      break;
    case "running":
      A.push(actionButton("Cancel run", act("cancel"), "danger"));
      break;
    case "review":
      A.push(actionButton("Approve → Finished", act("approve"), "primary", "The work is good; move it to Finished. A note is committed to the repository."));
      A.push(sendBack());
      A.push(actionButton("Reopen", act("reopen"), "ghost", "Back to Planned without asking the AI"));
      break;
    case "done":
      A.push(actionButton("Reopen", act("reopen"), "", "Back to Planned without asking the AI"));
      A.push(sendBack());
      break;
    case "failed":
      A.push(actionButton("Queue to run again", act("queue"), asking ? "" : "primary", queueTip));
      A.push(actionButton(planLabel, act("plan"), "ghost", planTip));
      A.push(actionButton("Mark planned (skip AI)", act("mark_planned"), "ghost", skipTip));
      A.push(pauseButton());
      break;
  }
  }
  $("#d-flow-actions").replaceChildren(...A);
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

  if (t.active && !pollLive.timer) pollLive();
}

// Files changed by the task's commit, fetched once per commit while the drawer shows it.
async function renderChanges(t, show) {
  const box = $("#d-changes");
  const p = currentProject();
  const ref = t.commit || (p && p.svn_tracking === true && (t.changes || []).length
    ? `svn:${t.changes.length}:${t.checkin || ""}` : "");
  if (!show || !ref) { box.hidden = true; renderChanges.key = null; return; }
  const key = `${state.pid}/${t.id}/${ref}`;
  if (renderChanges.key === key) return;
  renderChanges.key = key;
  box.hidden = true;
  let files;
  try {
    files = (await api("GET", `/api/projects/${state.pid}/tasks/${t.id}/changes`)).files;
  } catch { files = []; }
  if (renderChanges.key !== key) return;
  $("#d-changes-count").textContent = `(${files.length})`;
  $("#d-changes-list").replaceChildren(...files.map(f =>
    el("li", {}, el("span", { class: `change-status s-${f.status}` }, f.status), " ", f.path)));
  box.hidden = !files.length;
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
    plan_trust: $("#d-plan-trust").value,
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
  if (action === "plan" || action === "send_back") body.feedback = $("#d-feedback").value;
  if (action === "approve") body.note = $("#d-approval-note").value;
  try {
    const updated = await api("POST", `/api/projects/${state.pid}/tasks/${t.id}/action`, body);
    Object.assign(t, updated);
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
    state.tasks = state.tasks.filter(x => x.id !== t.id);
    state.dirty = false;
    closeDrawer();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- live output ---------------- */

const ACTIVITY_ICON = { say: "💬", tool: "🔧", error: "⚠", note: "·" };

function activityAtBottom(list) {
  return list.scrollTop + list.clientHeight >= list.scrollHeight - 30;
}

// Appends only new events; re-renders fully if the list shrank (cap rollover or a new job).
function renderActivity(list, events) {
  const stick = activityAtBottom(list);
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
  clearTimeout(pollLive.timer);
  pollLive.timer = null;
  const t = openTask();
  if (!t || !t.active) return;
  pollLive.timer = -1; // mark as running while the request is in flight
  try {
    const live = await api("GET", `/api/projects/${state.pid}/tasks/${t.id}/live`);
    if (state.openTid !== t.id) { pollLive.timer = null; return; }
    renderActivity($("#d-activity"), live.events || []);
    $("#d-current").textContent = live.current ? `▶ ${live.current}` : "";
    $("#d-kind").textContent = live.kind === "plan" ? "· Planning" : live.kind ? "· Running" : "";
    if (pollLive.tabFor !== t.id) {  // first poll for this task: pick a default tab
      pollLive.tabFor = t.id;
      showLiveTab(!(live.events || []).length);
    } else if ((live.events || []).length && !pollLive.userTab && $("#d-activity").hidden) {
      showLiveTab(false);
    }
    const pre = $("#d-live");
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent = live.output || "Waiting for output…";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
    $("#d-elapsed").textContent = live.active ? `${live.elapsed}s` : "finished";
    if (!live.active) { pollLive.timer = null; loadTasks(); return; }
  } catch { /* transient; next tick retries */ }
  pollLive.timer = setTimeout(pollLive, 1000);
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
  $("#d-tab-activity").onclick = () => { pollLive.userTab = true; showLiveTab(false); };
  $("#d-tab-raw").onclick = () => { pollLive.userTab = true; showLiveTab(true); };
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
  form.ssh_target.addEventListener("change", resetBrowser);
  form.ssh_port.addEventListener("change", resetBrowser);
  form.onsubmit = async ev => {
    ev.preventDefault();
    const btn = $("#project-submit");
    btn.disabled = true;
    btn.textContent = "Connecting…";
    const f = Object.fromEntries(new FormData(form));
    f.create = form.create.checked;
    f.git_tracking = form.git_tracking.checked;
    f.secrets_ack = form.git_tracking.checked && form.secrets_ack.checked;
    try {
      const project = await api("POST", "/api/projects", f);
      dialog.close();
      state.projects.push(project);
      renderProjects();
      loadReachability();
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
  "claude.models", "codex.models", "opencode.models", "cline.models",
  "opencode.plan", "opencode.run", "opencode.plan_agent", "opencode.run_agent",
  "opencode.plan_model", "opencode.code_model", "opencode.require_agents", "opencode.plan_must_not_edit",
  "cline.plan", "cline.run", "cline.plan_model", "cline.code_model", "cline.plan_must_not_edit",
  "timeouts.plan", "timeouts.run", "automation.auto_plan", "automation.auto_queue",
  "automation.auto_run", "git.gitignore",
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
    const out = { commands: Object.fromEntries([...CLI].map(p => [p, {}])), timeouts: {}, automation: {}, git: {},
      endpoints: $$("#endpoint-list .endpoint").map(readEndpoint) };
    for (const name of SETTING_FIELDS) {
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
        `${t.changes.length} file${t.changes.length === 1 ? "" : "s"}`),
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
  const btn = $("#ci-submit-btn");
  btn.disabled = true;
  showError($("#ci-error"), "");
  try {
    const data = await api("POST", `/api/projects/${state.pid}/checkin`, { message: $("#ci-message").value });
    $("#checkin-dialog").close();
    toast(data.revision ? `Checked in as ${data.revision}` : "Nothing to check in");
    loadTasks();
    loadReachability();
  } catch (e) {
    showError($("#ci-error"), e.message);
    btn.disabled = false;
  }
}

let remoteUrl = ""; // origin's URL as last shown in the Sync dialog

function renderRemote(r) {
  remoteUrl = r.url || "";
  $("#r-mode-text").textContent = SYNC_MODE_NAMES[r.sync_mode] || SYNC_MODE_NAMES["ff-only"];
  const parts = [r.url ? `origin ${r.url}` : "", r.branch ? `Branch ${r.branch}` : "Detached HEAD"].filter(Boolean);
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
  const p = currentProject();
  $("#r-mode-text").textContent = SYNC_MODE_NAMES[p && p.sync_mode] || SYNC_MODE_NAMES["ff-only"];
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
    showError($("#r-error"), "");
    $("#r-log").hidden = true;
    btn.disabled = true;
    btn.textContent = "Syncing…";
    try {
      // The server syncs with the project's saved mode.
      const r = await api("POST", `/api/projects/${state.pid}/remote/sync`, { push: $("#r-push").checked });
      renderRemote(r);
      $("#r-log").textContent = r.log.join("\n");
      $("#r-log").hidden = false;
      toast("Synced");
      loadReachability();
    } catch (e) {
      showError($("#r-error"), e.message);
      toast(e.message, true);
    } finally {
      btn.textContent = "Sync now";
      btn.disabled = !remoteUrl;
      loadTasks();
    }
  };
}

/* ---------------- wiring ---------------- */

function init() {
  setupThemePicker();
  setupProjectDialog();
  setupSettingsDialog();
  setupChat();
  for (const [f, p] of Object.entries(IMAGE_FIELDS)) enableImagePaste($(f), $(p));
  setupBatchBar();
  setupRemoteDialog();
  setupProjectSettings();
  loadAutomation();
  $("#new-task").onsubmit = createTask;
  $("#commits-btn").onclick = showCommits;
  $("#checkin-btn").onclick = showCheckin;
  $("#ci-submit-btn").onclick = submitCheckin;
  $("#ci-copy-btn").onclick = async () => {
    try { await navigator.clipboard.writeText($("#ci-message").value); toast("Message copied"); }
    catch { toast("Could not copy", true); }
  };
  $("#terminal-btn").onclick = async () => {
    try {
      await api("POST", `/api/projects/${state.pid}/terminal`);
      toast("Terminal opened");
    } catch (e) { toast(e.message, true); }
  };
  $("#vscode-btn").onclick = async () => {
    try {
      await api("POST", `/api/projects/${state.pid}/vscode`);
      toast("VS Code opened");
    } catch (e) { toast(e.message, true); }
  };
  for (const btn of $$(".queue-tab")) btn.onclick = () => selectTab(btn.dataset.col);
  $(".queue-tabs").onkeydown = onTabKeydown;
  for (const btn of $$(".auto-flow")) btn.onclick = () => toggleAutoFlow(btn);
  // The chat drawer's model is a quick override that saves immediately.
  $("#c-model").onchange = async e => {
    const model = pickModel(e.target, currentProject().provider || "claude");
    if (model === null) return;
    await updateProject({ chat_model: model });
    renderChatWhere();
  };
  for (const id of ["#d-title", "#d-desc", "#d-provider", "#d-plan-trust", "#d-plan"]) {
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
  $("#d-feedback").addEventListener("input", () => {
    const empty = !$("#d-feedback").value.trim();
    for (const b of $$("#d-flow-actions [data-needs-feedback]")) b.disabled = empty;
  });
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

  loadProjects().catch(e => toast(e.message, true));
  setInterval(() => { if (!document.hidden) loadTasks(); }, 3000);
  setInterval(() => { if (!document.hidden) loadReachability(); }, 60000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { loadTasks(); loadReachability(); }
  });
}

init();
