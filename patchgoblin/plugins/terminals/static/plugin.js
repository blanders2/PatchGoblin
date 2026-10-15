// Terminals plugin: xterm.js front end for the PTY sessions in sessions.py.
// Only the shown session has an output stream open (each one holds a server thread); every
// session's xterm instance stays in memory with its offset, so switching back only fetches
// what was missed, and a page reload replays the scrollback.
(() => {
const { $, el, api, toast } = PG;

const T = {
  active: false,
  list: [], // session views from the server
  projects: [],
  shown: null, // id of the session on screen
  terms: new Map(), // id -> {term, fit, host, offset, sent, pending, timer, exited}
  stream: null, // {sid, controller}
  poll: null,
};

const cssVar = name => getComputedStyle(document.body).getPropertyValue(name).trim();
const theme = () => ({ background: cssVar("--panel"), foreground: cssVar("--text"), cursor: cssVar("--accent"),
  cursorAccent: cssVar("--panel"), selectionBackground: cssVar("--b-planned") || "#5557" });
const session = id => T.list.find(s => s.id === id);

// ---- sessions list --------------------------------------------------------------------
async function loadSessions() {
  try { T.list = (await api("GET", "/api/terminals/sessions")).sessions; }
  catch { return; }
  for (const id of [...T.terms.keys()]) if (!session(id)) dropTerm(id);
  if (T.shown && !session(T.shown)) T.shown = null;
  if (!T.shown && T.list.length) T.shown = (session(PG.storageGet("pg.term.shown")) || T.list[T.list.length - 1]).id;
  renderList();
  updateBadge();
  // Only (re)attach when needed: a periodic refresh mustn't steal focus or refit.
  if (T.active && (!T.stream || T.stream.sid !== T.shown)) show(T.shown);
  else renderHead();
}

function updateBadge() {
  const running = T.list.filter(s => s.alive).length;
  PG.setBadge("terminals", running, `${running} running`);
}

function renderList() {
  $("#term-list").replaceChildren(...(T.list.length ? T.list.map(s => el("li", {
    class: s.id === T.shown ? "active" : "", tabindex: 0, title: s.title,
    onclick: () => show(s.id), onkeydown: e => { if (e.key === "Enter") show(s.id); } },
  el("div", { class: "p-title" },
    el("span", { class: "term-dot", style: s.color ? `background:${s.color}` : "" }),
    el("span", { class: "p-name" }, s.title),
    el("button", { class: "ghost small term-x", type: "button", title: "Close", "aria-label": `Close ${s.title}`,
      onclick: e => { e.stopPropagation(); closeSession(s.id); } }, "×")),
  el("div", { class: "p-sub" }, s.alive ? `${s.cols}×${s.rows}` : `exited${s.exit_code !== null ? ` (${s.exit_code})` : ""}`)))
    : [el("li", { class: "muted small term-none" }, "No terminals yet.")]));
}

async function loadProjects() {
  try { T.projects = (await api("GET", "/api/projects")).projects; } catch { /* keep the old list */ }
  const sel = $("#term-new-project"), keep = sel.value || PG.storageGet("pg.pid") || "";
  sel.replaceChildren(...T.projects.map(p => el("option", { value: p.id }, p.name)),
    el("option", { value: "" }, "Plain shell (home folder)"));
  sel.value = [...sel.options].some(o => o.value === keep) ? keep : sel.options[0].value;
}

// ---- terminals ---------------------------------------------------------------------------
function termFor(s) {
  let t = T.terms.get(s.id);
  if (t) return t;
  const host = el("div", { class: "term-host" });
  host.hidden = true;
  $("#term-stage").append(host);
  // Start at the shell's size: output written before the first fit must lay out the way the
  // shell drew it.
  const term = new Terminal({ cols: s.cols, rows: s.rows, fontFamily: '"Cascadia Mono", "Cascadia Code", Consolas, ui-monospace, monospace',
    fontSize: 13, cursorBlink: true, scrollback: 10000, theme: theme() });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new WebLinksAddon.WebLinksAddon());
  term.open(host);
  t = { id: s.id, term, fit, host, offset: 0, sent: "", pending: "", timer: null, exited: false };
  term.onData(data => queueInput(t, data));
  term.attachCustomKeyEventHandler(e => keyHandler(t, e));
  host.addEventListener("contextmenu", async e => {
    e.preventDefault();
    try { term.paste(await navigator.clipboard.readText()); } catch { toast("Could not read the clipboard", true); }
  });
  T.terms.set(s.id, t);
  return t;
}

function dropTerm(id) {
  const t = T.terms.get(id);
  if (!t) return;
  if (T.stream && T.stream.sid === id) detach();
  t.term.dispose();
  t.host.remove();
  T.terms.delete(id);
}

// Ctrl+C copies when text is selected (else it's ^C for the shell); Ctrl+V pastes.
function keyHandler(t, e) {
  if (e.type !== "keydown" || !(e.ctrlKey || e.metaKey) || e.altKey) return true;
  const key = e.key.toLowerCase();
  if (key === "c" && t.term.hasSelection()) {
    navigator.clipboard.writeText(t.term.getSelection()).catch(() => {});
    t.term.clearSelection();
    return false;
  }
  return key !== "v"; // let the browser's paste event reach xterm instead of sending ^V
}

function queueInput(t, data) {
  t.pending += data;
  if (!t.timer) t.timer = setTimeout(() => flushInput(t), 5);
}

async function flushInput(t) {
  const data = t.pending;
  t.pending = "";
  t.timer = null;
  if (!data || t.exited) return;
  try { await api("POST", `/api/terminals/sessions/${t.id}/input`, { data }); }
  catch (e) { toast(e.message, true); }
}

function fitNow(t) {
  if (!t || t.host.hidden) return;
  try { t.fit.fit(); } catch { return; }
  const size = `${t.term.cols}x${t.term.rows}`;
  if (size === t.sent || t.exited) return;
  t.sent = size;
  api("POST", `/api/terminals/sessions/${t.id}/resize`, { cols: t.term.cols, rows: t.term.rows }).catch(() => {});
  const s = session(t.id);
  if (s) { s.cols = t.term.cols; s.rows = t.term.rows; renderList(); }
}

// ---- output stream ------------------------------------------------------------------------
function detach() {
  if (T.stream) T.stream.controller.abort();
  T.stream = null;
}

async function attach(t) {
  if (T.stream && T.stream.sid === t.id) return;
  detach();
  const controller = new AbortController(), mine = { sid: t.id, controller };
  T.stream = mine;
  let ended = false;
  try {
    const res = await fetch(`/api/terminals/sessions/${t.id}/stream`, { method: "POST", signal: controller.signal,
      headers: { "X-PatchGoblin": "1", "Content-Type": "application/json" }, body: JSON.stringify({ since: t.offset }) });
    if (res.status === 404) { loadSessions(); return; }
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const ev = JSON.parse(buf.slice(0, nl));
        buf = buf.slice(nl + 1);
        if (ev.event === "out") {
          if (ev.truncated && t.offset > 0) t.term.write("\r\n\x1b[2m[older output was dropped]\x1b[0m\r\n");
          if (ev.d) t.term.write(ev.d);
          t.offset = ev.end;
        } else if (ev.event === "exit") {
          ended = true;
          markExited(t, ev.code);
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") return;
  } finally {
    if (T.stream === mine) T.stream = null;
  }
  // The stream dropped without the shell ending (e.g. PatchGoblin restarted): try again.
  if (!ended && T.active && T.shown === t.id) setTimeout(() => { if (T.shown === t.id && !T.stream) loadSessions(); }, 1500);
}

function markExited(t, code) {
  if (t.exited) return;
  t.exited = true;
  t.term.write(`\r\n\x1b[2m[process exited${code !== null && code !== undefined ? ` with code ${code}` : ""}]\x1b[0m\r\n`);
  const s = session(t.id);
  if (s) { s.alive = false; s.exit_code = code; }
  renderList();
  renderHead();
  updateBadge();
}

// ---- showing ------------------------------------------------------------------------------
function renderHead() {
  const s = session(T.shown);
  $("#term-title").textContent = s ? s.title : "No terminal open";
  $("#term-swatch").style.background = s && s.color ? s.color : "transparent";
  $("#term-state").textContent = s ? (s.alive ? "" : `exited${s.exit_code !== null ? ` (${s.exit_code})` : ""}`) : "";
  for (const id of ["#term-rename", "#term-restart", "#term-close"]) $(id).disabled = !s;
  $("#term-popout").disabled = !s || !s.project;
  $("#term-empty").hidden = !!s;
  $("#term-stage").hidden = !s;
}

function show(id) {
  const s = session(id);
  T.shown = s ? s.id : null;
  if (s) PG.storageSet("pg.term.shown", s.id);
  for (const t of T.terms.values()) t.host.hidden = t.id !== T.shown;
  renderList();
  renderHead();
  if (!s || !T.active) { detach(); return; }
  const t = termFor(s);
  t.host.hidden = false;
  if (!s.alive && !t.exited && t.offset > 0) markExited(t, s.exit_code);
  requestAnimationFrame(() => { fitNow(t); t.term.focus(); });
  attach(t);
}

async function newSession(projectId) {
  const shown = T.terms.get(T.shown);
  try {
    const data = await api("POST", "/api/terminals/sessions", { project: projectId || "",
      cols: shown ? shown.term.cols : 120, rows: shown ? shown.term.rows : 32 });
    T.list = data.sessions;
    updateBadge();
    if (!T.active) PG.show("terminals");
    show(data.session.id);
  } catch (e) { toast(e.message, true); }
}

async function closeSession(id) {
  const s = session(id);
  if (s && s.alive && !confirm(`End the shell in "${s.title}"?`)) return;
  try {
    T.list = (await api("DELETE", `/api/terminals/sessions/${id}`)).sessions;
    dropTerm(id);
    if (T.shown === id) T.shown = T.list.length ? T.list[T.list.length - 1].id : null;
    updateBadge();
    show(T.shown);
  } catch (e) { toast(e.message, true); }
}

PG.register({
  id: "terminals",
  setup() {
    if (typeof Terminal === "undefined") { toast("Terminals: xterm.js didn't load", true); return; }
    $("#term-new-btn").onclick = () => newSession($("#term-new-project").value);
    $("#term-rename").onclick = async () => {
      const s = session(T.shown);
      const title = s && prompt("Name this terminal", s.title);
      if (!title) return;
      try { T.list = (await api("POST", `/api/terminals/sessions/${s.id}/rename`, { title })).sessions; renderList(); renderHead(); }
      catch (e) { toast(e.message, true); }
    };
    $("#term-popout").onclick = async () => {
      try { await api("POST", `/api/terminals/sessions/${T.shown}/popout`); toast("Opened in Windows Terminal"); }
      catch (e) { toast(e.message, true); }
    };
    $("#term-restart").onclick = async () => {
      const old = T.shown;
      try {
        const data = await api("POST", `/api/terminals/sessions/${old}/restart`);
        dropTerm(old);
        T.list = data.sessions;
        show(data.session.id);
      } catch (e) { toast(e.message, true); }
    };
    $("#term-close").onclick = () => closeSession(T.shown);
    // A project's Terminal button (when Settings opens terminals in the browser): show its running
    // terminal if there is one, else start one. Its + button (detail.new) always starts one.
    const starting = new Set(); // projects whose terminal is still starting (a double-click opens one)
    document.addEventListener("pg:open-terminal", async e => {
      const pid = e.detail.project.id;
      if (starting.has(pid)) return;
      starting.add(pid);
      try {
        if (!e.detail.new) await loadSessions(); // it may have exited, or been opened in another browser tab
        const running = !e.detail.new && T.list.find(s => s.project === pid && s.alive);
        if (running) { PG.show("terminals"); show(running.id); } else await newSession(pid);
      } finally { starting.delete(pid); }
    });
    let fitTimer = null;
    new ResizeObserver(() => { clearTimeout(fitTimer); fitTimer = setTimeout(() => fitNow(T.terms.get(T.shown)), 60); })
      .observe($("#term-stage"));
    new MutationObserver(() => { for (const t of T.terms.values()) t.term.options.theme = theme(); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    renderHead();
    loadSessions();
  },
  activate() {
    T.active = true;
    loadProjects();
    loadSessions(); // shows the current session and attaches its stream
    clearInterval(T.poll);
    T.poll = setInterval(() => { if (!document.hidden) loadSessions(); }, 10000);
  },
  deactivate() {
    T.active = false;
    clearInterval(T.poll);
    detach(); // frees the server thread; output keeps collecting in the scrollback
  },
});
})();
