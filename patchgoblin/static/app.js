"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const STATUS_LABEL = {
  unplanned: "Unplanned", planning: "Planning…", planned: "Planned", queued: "Queued",
  running: "Running…", done: "Done", failed: "Failed",
};
const COLUMN_OF = {
  unplanned: "unplanned", planning: "unplanned", planned: "planned",
  queued: "queue", running: "queue", done: "finished", failed: "finished",
};
// Not locked by the AI and not finished; mirrors LOCKED in app.py.
const ACTIONABLE = new Set(["unplanned", "planned", "queued", "failed"]);
const BUSY = new Set(["planning", "running"]);

const state = {
  projects: [],
  pid: localStorageGet("pg.pid"),
  tab: validTab(localStorageGet("pg.tab")),
  tasks: [],
  openTid: null,
  formStamp: null, // server values of the editable fields when the drawer form was filled
  dirty: false,
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
const formStamp = t => JSON.stringify([t.title, t.description, t.provider, t.plan]);

const currentProject = () => state.projects.find(p => p.id === state.pid);
const openTask = () => state.tasks.find(t => t.id === state.openTid);

/* ---------------- model dropdowns ---------------- */

const MODELS = JSON.parse(document.body.dataset.models || "{}");
const CUSTOM_MODEL = "\u0000custom";

function fillModelSelect(select, provider, current = "") {
  const models = [...(MODELS[provider] || [])];
  if (current && !models.includes(current)) models.push(current);
  select.replaceChildren(el("option", { value: "" }, "default"),
    ...models.map(m => el("option", { value: m }, m)),
    el("option", { value: CUSTOM_MODEL }, "Custom…"));
  select.value = current;
  select.dataset.value = current;
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
  state.pid = pid;
  if (pid) localStorageSet("pg.pid", pid);
  renderProjects();
  const p = currentProject();
  if (!p) { closeChat(); return; }
  $("#p-name").textContent = p.name;
  $("#p-where").textContent = p.location === "ssh"
    ? `${p.ssh_target}${p.ssh_port ? ":" + p.ssh_port : ""}:${p.path}` : p.path;
  $("#p-provider").value = p.provider || "claude";
  fillModelSelect($("#p-model"), p.provider || "claude", p.model || "");
  state.tasks = [];
  renderBoard();
  loadChat();
  await loadTasks();
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
}

function selectTab(col, focus = false) {
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

function renderCard(t) {
  const p = currentProject();
  return el("div", {
    class: `card status-${t.status}${t.id === state.openTid ? " open" : ""}`,
    tabindex: "0",
    onclick: () => openDrawer(t.id),
    onkeydown: e => { if (e.key === "Enter") openDrawer(t.id); },
  },
  el("div", { class: "card-top" },
    el("span", { class: "tid" }, `#${t.id}`),
    el("span", { class: `badge ${t.status}` }, STATUS_LABEL[t.status])),
  el("div", { class: "card-title" }, t.title),
  el("div", { class: "card-meta" },
    t.provider && t.provider !== p.provider ? el("span", { class: "chip" }, t.provider) : null,
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

/* ---------------- drawer ---------------- */

function openDrawer(tid) {
  if (state.openTid !== tid && state.dirty && !confirm("Discard unsaved changes to the open task?")) return;
  closeChat();
  state.openTid = tid;
  state.dirty = false;
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

function actionButton(label, fn, cls = "") {
  return el("button", { class: cls, onclick: fn }, label);
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
    $("#d-provider").value = t.provider || "";
    $("#d-plan").value = t.plan || "";
    state.formStamp = formStamp(t);
    state.dirty = false;
  }
  for (const id of ["#d-title", "#d-desc", "#d-provider", "#d-plan"]) $(id).disabled = locked;

  showError($("#d-error"), t.error);

  const canPlan = ["unplanned", "planned", "failed"].includes(t.status);
  $("#d-feedback-wrap").hidden = !canPlan;
  const hasPlan = (t.plan || "").trim().length > 0;
  const planLabel = hasPlan ? "Refine plan with AI" : "Plan with AI";

  const A = [];
  const act = (action, extra) => () => doAction(action, extra);
  switch (t.status) {
    case "unplanned":
      A.push(actionButton(planLabel, act("plan"), "primary"));
      A.push(actionButton("Mark planned", act("mark_planned")));
      break;
    case "planning":
      A.push(actionButton("Cancel planning", act("cancel"), "danger"));
      break;
    case "planned":
      A.push(actionButton("Queue for AI", act("queue"), "primary"));
      A.push(actionButton(planLabel, act("plan")));
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
      A.push(actionButton("Queue again", act("queue"), "primary"));
      A.push(actionButton(planLabel, act("plan")));
      A.push(actionButton("Mark planned", act("mark_planned"), "ghost"));
      break;
  }
  if (!locked) {
    A.push(actionButton("Save", saveTask, "ghost"));
    A.push(actionButton("Delete", deleteTask, "ghost danger"));
  }
  $("#d-actions").replaceChildren(...A);

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

function formFields() {
  return {
    title: $("#d-title").value,
    description: $("#d-desc").value,
    provider: $("#d-provider").value,
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

async function doAction(action) {
  const t = openTask();
  if (!t) return;
  if (state.dirty && action !== "cancel" && !(await saveTask(true))) return;
  const body = { action };
  if (action === "plan") body.feedback = $("#d-feedback").value;
  try {
    const updated = await api("POST", `/api/projects/${state.pid}/tasks/${t.id}/action`, body);
    Object.assign(t, updated);
    if (action === "plan") $("#d-feedback").value = "";
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

function renderChat(data) {
  const log = $("#c-log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  const p = currentProject();
  $("#c-where").textContent = p ? `${p.name} · ${p.provider || "claude"}${p.model ? " · " + p.model : ""}` : "";
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
    fillModelSelect(form.model, form.provider.value);
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
  form.provider.onchange = () =>
    fillModelSelect(form.model, form.provider.value, modelFor(form.provider.value, form.model.value));
  form.model.onchange = () => pickModel(form.model, form.provider.value);
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
  "openai.base_url", "openai.model", "openai.max_steps", "openai.allow_commands",
  "timeouts.plan", "timeouts.run",
];

function settingPath(name, settings) {
  const [group, key] = name.split(".");
  return group === "claude" || group === "codex" ? [settings.commands[group], key] : [settings[group], key];
}

function setupSettingsDialog() {
  const dialog = $("#settings-dialog");
  const form = $("#settings-form");
  $("#settings-btn").onclick = async () => {
    try {
      const s = await api("GET", "/api/settings");
      for (const name of SETTING_FIELDS) {
        const [obj, key] = settingPath(name, s);
        const input = form.elements[name];
        if (input.type === "checkbox") input.checked = !!obj[key];
        else input.value = Array.isArray(obj[key]) ? obj[key].join(" ") : obj[key];
      }
      $("#key-status").textContent = s.openai_key_present
        ? "· OPENAI_API_KEY detected" : "· OPENAI_API_KEY not set in the server environment";
      showError($("#settings-error"), "");
      dialog.showModal();
    } catch (e) { toast(e.message, true); }
  };
  form.onsubmit = async ev => {
    ev.preventDefault();
    const out = { commands: { claude: {}, codex: {} }, openai: {}, timeouts: {} };
    for (const name of SETTING_FIELDS) {
      const [obj, key] = settingPath(name, out);
      const input = form.elements[name];
      obj[key] = input.type === "checkbox" ? input.checked
        : input.type === "number" ? Number(input.value) : input.value.trim();
    }
    try {
      await api("PUT", "/api/settings", out);
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

/* ---------------- wiring ---------------- */

function init() {
  setupProjectDialog();
  setupSettingsDialog();
  setupChat();
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
    await updateProject({ provider, model: modelFor(provider, p.model || "") });
    fillModelSelect($("#p-model"), p.provider || "claude", p.model || "");
  };
  $("#p-model").onchange = e => {
    const model = pickModel(e.target, currentProject().provider || "claude");
    if (model !== null) updateProject({ model });
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
    $(id).addEventListener("input", () => { state.dirty = true; });
  }
  document.addEventListener("click", e => {
    const target = e.target.closest("[data-close]");
    if (!target) return;
    const id = target.dataset.close;
    if (id === "drawer") closeDrawer();
    else if (id === "chat") closeChat();
    else $("#" + id).close();
  });
  document.addEventListener("keydown", e => {
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
