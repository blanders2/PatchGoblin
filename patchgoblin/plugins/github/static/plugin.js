// GitHub plugin: browse an account's repositories and clone one onto this computer or an SSH server.
// The token never leaves the server; the browser only sees repo lists and clone results.
(() => {
const { $, $$, el, api, toast, showError, withBusy } = PG;

const gh = {
  accounts: [], // public servers of type github
  account: null, // selected account id
  repos: [], // [{full_name, name, private, …, projects: [{id, name}]}]
  protocol: "ssh",
  truncated: false,
  targets: [], // [{id, name}]
  filters: { owned: false, private: false, forks: false, archived: false },
  query: "",
  repo: null, // the repo in the clone dialog
  dir: null, // the folder picker's last listing
};

const providers = () => JSON.parse(document.body.dataset.providers || "[]");

async function loadAccounts() {
  try {
    gh.accounts = (await api("GET", "/api/github/accounts")).accounts.filter(a => a.usable !== false);
  } catch (e) { showError($("#gh-error"), e.message); return; }
  if (!gh.accounts.some(a => a.id === gh.account)) gh.account = gh.accounts[0] ? gh.accounts[0].id : null;
  renderAccounts();
  if (gh.account) await loadRepos(false);
  else { gh.repos = []; render(); }
}

async function loadRepos(refresh) {
  showError($("#gh-error"), "");
  const btn = $("#gh-refresh");
  btn.disabled = true;
  try {
    const d = await api("GET", `/api/github/accounts/${encodeURIComponent(gh.account)}/repos${refresh ? "?refresh=1" : ""}`);
    Object.assign(gh, { repos: d.repos, truncated: d.truncated, protocol: d.protocol });
  } catch (e) {
    gh.repos = [];
    showError($("#gh-error"), e.message);
  } finally {
    btn.disabled = false;
  }
  render();
}

function renderAccounts() {
  const list = $("#gh-accounts");
  list.replaceChildren(...gh.accounts.map(a => el("li", {
    tabindex: "0", class: a.id === gh.account ? "active" : "",
    onclick: () => { gh.account = a.id; renderAccounts(); loadRepos(false); },
    onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.currentTarget.click(); } },
  }, el("span", {}, a.name))));
}

function visible() {
  const q = gh.query.trim().toLowerCase(), f = gh.filters;
  const owner = (gh.accounts.find(a => a.id === gh.account) || {}).login;
  return gh.repos.filter(r => {
    if (r.archived && !f.archived) return false;
    if (f.private && !r.private) return false;
    if (f.forks && !r.fork) return false;
    if (f.owned && owner && r.owner !== owner) return false;
    return !q || `${r.full_name} ${r.description}`.toLowerCase().includes(q);
  });
}

const projectChip = p => el("span", { class: "gh-managed", title: "Go to projects",
  onclick: () => PG.show("projects") }, "Managed in: " + p.name);

function rowFor(r) {
  return el("li", { class: "gh-repo" },
    el("div", { class: "gh-main" },
      el("div", { class: "gh-name" }, r.full_name, " ",
        r.private ? el("span", { class: "gh-badge" }, "private") : null,
        r.fork ? el("span", { class: "gh-badge" }, "fork") : null,
        r.archived ? el("span", { class: "gh-badge" }, "archived") : null),
      r.description ? el("div", { class: "gh-desc" }, r.description) : null,
      el("div", { class: "gh-meta" }, r.language ? el("span", {}, r.language) : null,
        el("span", {}, "updated " + PG.relTime(r.updated_at)), ...r.projects.map(projectChip))),
    el("button", { type: "button", onclick: () => openClone(r) }, "Clone…"));
}

function render() {
  const shown = visible();
  $("#gh-list").replaceChildren(...shown.map(rowFor));
  const empty = $("#gh-empty");
  empty.hidden = shown.length > 0 || !!$("#gh-error").textContent;
  empty.textContent = !gh.account ? "Add a GitHub account in the sidebar to list its repositories."
    : "No repositories match.";
  const note = $("#gh-note");
  note.hidden = !gh.truncated;
  note.textContent = "Only the most recently updated repositories are shown (page limit reached).";
}

// ---- clone dialog --------------------------------------------------------------
const form = () => $("#gh-clone-form");

function syncOptions() {
  const f = form();
  $("#gh-project-opts").hidden = !f.add_project.checked;
  $("#gh-secrets").hidden = !f.git_tracking.checked;
  f.secrets_ack.required = f.add_project.checked && f.git_tracking.checked;
}

async function openClone(repo) {
  gh.repo = repo;
  const f = form();
  f.reset();
  $("#gh-picker").hidden = true;
  showError($("#gh-clone-error"), "");
  $("#gh-clone-repo").textContent = repo.full_name;
  f.folder.value = repo.name;
  f.protocol.value = gh.protocol;
  f.provider.replaceChildren(...providers().map(p => el("option", { value: p.id }, p.name)));
  try { gh.targets = (await api("GET", "/api/github/targets")).targets; } catch { gh.targets = [{ id: "local", name: "This computer" }]; }
  f.target.replaceChildren(...gh.targets.map(t => el("option", { value: t.id }, t.name)));
  f.parent.value = PG.storageGet("gh-parent") || "";
  syncOptions();
  $("#gh-clone-dialog").showModal();
}

const targetBody = () => form().target.value === "local"
  ? { location: "local" } : { location: "ssh", server: form().target.value };

async function browse(extra) {
  const err = $("#gh-clone-error");
  showError(err, "");
  try {
    gh.dir = await api("POST", "/api/browse", { ...targetBody(), ...extra });
  } catch (e) { showError(err, e.message); return; }
  renderDirs();
}

function renderDirs() {
  const d = gh.dir, hidden = $("#gh-hidden").checked;
  $("#gh-picker").hidden = false;
  $("#gh-picker-path").textContent = d.path;
  $("#gh-up").disabled = d.parent == null;
  const dirs = d.dirs.filter(x => hidden || !x.name.startsWith("."));
  $("#gh-dirs").replaceChildren(...dirs.map(x => el("li", { onclick: () => browse({ path: x.path }) }, x.name)));
}

async function submitClone(e) {
  e.preventDefault();
  const f = form(), btn = $("#gh-clone-go");
  const payload = {
    account: gh.account, repo: gh.repo.full_name, target: f.target.value, parent: f.parent.value.trim(),
    folder: f.folder.value.trim(), protocol: f.protocol.value, add_project: f.add_project.checked,
    git_tracking: f.git_tracking.checked, secrets_ack: f.secrets_ack.checked, provider: f.provider.value,
  };
  btn.textContent = "Cloning…";
  const ok = await withBusy(btn, $("#gh-clone-error"), async () => {
    const d = await api("POST", "/api/github/clone", payload);
    PG.storageSet("gh-parent", payload.parent);
    $("#gh-clone-dialog").close();
    toast(d.project ? `Cloned and added ${d.project.name}` : `Cloned to ${d.path}`);
    if (d.project) {
      await PG.reloadProjects();
      PG.show("projects");
    }
  });
  btn.textContent = "Clone";
  if (ok) loadRepos(false);
}

PG.register({
  id: "github",
  setup() {
    $("#gh-add-account").onclick = () => PG.servers.open("github");
    document.addEventListener("pg:servers-changed", e => {
      if (!e.detail || e.detail.type === "github") loadAccounts();
    });
    $("#gh-search").oninput = e => { gh.query = e.target.value; render(); };
    $("#gh-refresh").onclick = () => gh.account && loadRepos(true);
    for (const box of $$("#gh-chips input")) {
      box.onchange = () => { gh.filters[box.dataset.filter] = box.checked; render(); };
    }
    const f = form();
    f.add_project.onchange = f.git_tracking.onchange = syncOptions;
    f.onsubmit = submitClone;
    f.target.onchange = () => { $("#gh-picker").hidden = true; };
    $("#gh-clone-cancel").onclick = () => $("#gh-clone-dialog").close();
    $("#gh-browse-btn").onclick = () => browse(f.parent.value.trim() ? { path: f.parent.value.trim() } : { home: true });
    $("#gh-up").onclick = () => gh.dir && gh.dir.parent != null && browse({ path: gh.dir.parent });
    $("#gh-home").onclick = () => browse({ home: true });
    $("#gh-hidden").onchange = () => gh.dir && renderDirs();
    $("#gh-use").onclick = () => { if (gh.dir) { f.parent.value = gh.dir.path; $("#gh-picker").hidden = true; } };
    loadAccounts();
  },
  activate() { loadAccounts(); },
  deactivate() {},
});
})();
