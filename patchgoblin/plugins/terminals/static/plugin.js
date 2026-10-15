// Terminals plugin: xterm.js front end for the PTY sessions in sessions.py.
// Terminals are tiled: groups (tabs) each hold a split tree of panes. The layout is per browser
// (localStorage); the sessions themselves live on the server. The visible panes share ONE
// multiplexed output stream, because a browser allows only ~6 HTTP/1.1 connections per origin.
// Every session's xterm instance stays in memory with its offset, so showing a pane again only
// fetches what was missed, and a page reload replays the scrollback.
(() => {
const { $, el, api, toast } = PG;

const MAX_PANES = 4; // new terminals go to a new group past this many panes in one
const MIN_FRACTION = 0.1;
const LAYOUT_KEY = "pg.term.layout", FONT_KEY = "pg.term.font";
const clientId = Math.random().toString(36).slice(2, 12); // names this page's stream to the server

const T = {
  active: false,
  list: [], // session views from the server
  projects: [],
  layout: { groups: [], current: null },
  font: 13,
  terms: new Map(), // id -> {term, fit, host, offset, sent, pending, timer, exited, rz}
  panes: new Map(), // sid -> header elements of the rendered pane
  stream: null, // {sids: Set, controller}
  syncTimer: null,
  drawn: false, // the layout has been rendered at least once
  drag: null, // sid of the pane header being dragged
  poll: null,
};

const cssVar = name => getComputedStyle(document.body).getPropertyValue(name).trim();
const theme = () => ({ background: cssVar("--panel"), foreground: cssVar("--text"), cursor: cssVar("--accent"),
  cursorAccent: cssVar("--panel"), selectionBackground: cssVar("--b-planned") || "#5557" });
const session = id => T.list.find(s => s.id === id);
const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
let gid = 0;
const newGroupId = () => `g${Date.now().toString(36)}${(gid++).toString(36)}`;

// ---- layout model -----------------------------------------------------------------------
// node: {type:"pane", sid} | {type:"split", dir:"row"|"col", sizes:[fractions], children:[node]}
const panesOf = node => !node ? [] : node.type === "pane" ? [node.sid] : node.children.flatMap(panesOf);
const curGroup = () => T.layout.groups.find(g => g.id === T.layout.current) || T.layout.groups[0] || null;
const groupOf = sid => T.layout.groups.find(g => panesOf(g.root).includes(sid)) || null;
const newGroup = name => ({ id: newGroupId(), name: name || `Group ${T.layout.groups.length + 1}`, root: null, focus: null, zoom: null });

function normalize(sizes, n) {
  let s = Array.isArray(sizes) && sizes.length === n ? sizes.map(x => (typeof x === "number" && x > 0 ? x : 0)) : [];
  if (s.length !== n || s.some(x => !x)) s = new Array(n).fill(1);
  const total = s.reduce((a, b) => a + b, 0);
  return s.map(x => x / total);
}

// Drop panes that fail `keep`, collapsing splits left with one child. Returns the new node or null.
function prune(node, keep) {
  if (!node) return null;
  if (node.type === "pane") return keep(node.sid) ? node : null;
  const kept = [], sizes = [];
  node.children.forEach((c, i) => { const n = prune(c, keep); if (n) { kept.push(n); sizes.push(node.sizes[i]); } });
  if (!kept.length) return null;
  if (kept.length === 1) return kept[0];
  return { ...node, children: kept, sizes: normalize(sizes, kept.length) };
}

// Put `sid` beside `target` on `side` ("left"|"right"|"top"|"bottom").
function insertAt(node, target, sid, side) {
  const dir = side === "left" || side === "right" ? "row" : "col";
  const before = side === "left" || side === "top";
  if (node.type === "pane") {
    if (node.sid !== target) return node;
    const added = { type: "pane", sid };
    return { type: "split", dir, sizes: [0.5, 0.5], children: before ? [added, node] : [node, added] };
  }
  const i = node.children.findIndex(c => c.type === "pane" && c.sid === target);
  if (i >= 0 && node.dir === dir) {
    const children = node.children.slice(), sizes = node.sizes.slice(), half = sizes[i] / 2;
    children.splice(before ? i : i + 1, 0, { type: "pane", sid });
    sizes.splice(i, 1, half, half);
    return { ...node, children, sizes };
  }
  return { ...node, children: node.children.map(c => insertAt(c, target, sid, side)) };
}

function findPane(node, sid) {
  if (!node) return null;
  return node.type === "pane" ? (node.sid === sid ? node : null) : node.children.map(c => findPane(c, sid)).find(Boolean) || null;
}

function sanitize(node, depth = 0) {
  if (!node || typeof node !== "object" || depth > 8) return null;
  if (node.type === "pane") return typeof node.sid === "string" ? { type: "pane", sid: node.sid } : null;
  if (node.type !== "split" || !Array.isArray(node.children)) return null;
  const children = node.children.map(c => sanitize(c, depth + 1)).filter(Boolean);
  if (!children.length) return null;
  if (children.length === 1) return children[0];
  const sizes = children.length === node.children.length ? node.sizes : null;
  return { type: "split", dir: node.dir === "col" ? "col" : "row", sizes: normalize(sizes, children.length), children };
}

function loadLayout() {
  try {
    const data = JSON.parse(PG.storageGet(LAYOUT_KEY) || "null");
    const groups = (data && Array.isArray(data.groups) ? data.groups : []).map(g => ({
      id: typeof g.id === "string" ? g.id : newGroupId(), name: typeof g.name === "string" ? g.name.slice(0, 40) : "Group",
      root: sanitize(g.root), focus: typeof g.focus === "string" ? g.focus : null, zoom: typeof g.zoom === "string" ? g.zoom : null }));
    T.layout = { groups, current: data && typeof data.current === "string" ? data.current : null };
  } catch { T.layout = { groups: [], current: null }; }
  const f = parseInt(PG.storageGet(FONT_KEY), 10);
  T.font = f >= 8 && f <= 32 ? f : 13;
}

const saveLayout = () => PG.storageSet(LAYOUT_KEY, JSON.stringify(T.layout));

// Side to split `target` on: along its longer side, going by what's on screen.
function longerSide(target) {
  const pane = T.panes.get(target);
  const r = pane ? pane.root.getBoundingClientRect() : $("#term-stage").getBoundingClientRect();
  return r.width >= r.height ? "right" : "bottom";
}

// Place a session in the layout. where: {group, target, side, force, switch}.
function addPane(sid, where = {}) {
  if (groupOf(sid)) return;
  let g = where.group ? T.layout.groups.find(x => x.id === where.group) : curGroup();
  if (!g) { g = newGroup(); T.layout.groups.push(g); T.layout.current = g.id; }
  if (!where.force && !where.side && panesOf(g.root).length >= MAX_PANES) {
    const last = T.layout.groups[T.layout.groups.length - 1];
    if (!where.switch && last !== g && panesOf(last.root).length < MAX_PANES) g = last; // fill the spare group first
    else {
      g = newGroup();
      T.layout.groups.push(g);
      if (where.switch) T.layout.current = g.id;
    }
  }
  const panes = panesOf(g.root);
  if (!g.root) g.root = { type: "pane", sid };
  else {
    const target = panes.includes(where.target) ? where.target : panes.includes(g.focus) ? g.focus : panes[panes.length - 1];
    g.root = insertAt(g.root, target, sid, where.side || (T.panes.has(target) ? longerSide(target) : panes.length % 2 ? "right" : "bottom"));
  }
  g.focus = sid;
  g.zoom = null;
}

// Make the layout match the sessions: prune the gone ones, place new ones, tidy groups.
function reconcile() {
  const seen = new Set();
  for (const g of T.layout.groups) {
    g.root = prune(g.root, sid => session(sid) && !seen.has(sid) && (seen.add(sid), true));
  }
  for (const s of T.list) if (!seen.has(s.id)) { addPane(s.id); seen.add(s.id); }
  fixup();
}

function fixup() {
  const L = T.layout;
  if (!L.groups.some(g => g.id === L.current)) L.current = L.groups.length ? L.groups[0].id : null;
  L.groups = L.groups.filter(g => g.root || g.id === L.current);
  if (!L.groups.length) { const g = newGroup(); L.groups.push(g); L.current = g.id; }
  if (!L.groups.some(g => g.id === L.current)) L.current = L.groups[0].id;
  for (const g of L.groups) {
    const panes = panesOf(g.root);
    if (!panes.includes(g.focus)) g.focus = panes[0] || null;
    if (!panes.includes(g.zoom) || panes.length < 2) g.zoom = null;
  }
}

function removeFromLayout(sid) {
  const g = groupOf(sid);
  if (g) g.root = prune(g.root, s => s !== sid);
}

// ---- sessions list --------------------------------------------------------------------
async function loadSessions() {
  try { T.list = (await api("GET", "/api/terminals/sessions")).sessions; }
  catch { return; }
  for (const id of [...T.terms.keys()]) if (!session(id)) dropTerm(id);
  const before = JSON.stringify(T.layout);
  reconcile();
  updateBadge();
  if (JSON.stringify(T.layout) !== before || !T.drawn) refresh();
  else { updatePanes(); renderList(); if (T.active) syncStream(); }
}

function updateBadge() {
  const running = T.list.filter(s => s.alive).length;
  PG.setBadge("terminals", running, `${running} running`);
}

const visibleSids = () => {
  const g = curGroup();
  return !g ? [] : g.zoom ? [g.zoom] : panesOf(g.root);
};

function renderList() {
  const items = [], shown = new Set(visibleSids()), cur = curGroup();
  const multi = T.layout.groups.length > 1;
  for (const g of T.layout.groups) {
    const panes = panesOf(g.root);
    if (multi && panes.length) items.push(el("li", { class: "term-group-head" }, g.name));
    for (const sid of panes) {
      const s = session(sid);
      if (!s) continue;
      items.push(el("li", {
        class: [g === cur && g.focus === sid ? "active" : "", g === cur && shown.has(sid) ? "shown" : ""].join(" ").trim(),
        tabindex: 0, title: s.title, onclick: () => focusSession(sid), onkeydown: e => { if (e.key === "Enter") focusSession(sid); } },
      el("div", { class: "p-title" },
        el("span", { class: "term-dot", style: s.color ? `background:${s.color}` : "" }),
        el("span", { class: "p-name" }, s.title),
        el("button", { class: "ghost small term-x", type: "button", title: "Close", "aria-label": `Close ${s.title}`,
          onclick: e => { e.stopPropagation(); closeSession(sid); } }, "×")),
      el("div", { class: "p-sub" }, s.alive ? `${s.cols}×${s.rows}` : `exited${s.exit_code !== null ? ` (${s.exit_code})` : ""}`)));
    }
  }
  $("#term-list").replaceChildren(...(items.length ? items : [el("li", { class: "muted small term-none" }, "No terminals yet.")]));
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
  $("#term-parking").append(host);
  // Start at the shell's size: output written before the first fit must lay out the way the
  // shell drew it.
  const term = new Terminal({ cols: s.cols, rows: s.rows, fontFamily: '"Cascadia Mono", "Cascadia Code", Consolas, ui-monospace, monospace',
    fontSize: T.font, cursorBlink: true, scrollback: 10000, theme: theme() });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new WebLinksAddon.WebLinksAddon());
  term.open(host);
  t = { id: s.id, term, fit, host, offset: 0, sent: "", pending: "", timer: null, exited: false, rz: null };
  term.onData(data => queueInput(t, data));
  term.attachCustomKeyEventHandler(e => keyHandler(t, e));
  host.addEventListener("focusin", () => setFocus(s.id));
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
  clearTimeout(t.rz);
  t.term.dispose();
  t.host.remove();
  T.terms.delete(id);
}

// Ctrl+Shift+key runs the layout shortcuts; Ctrl+C copies when text is selected (else it's ^C for
// the shell); Ctrl+V pastes. Other keys go to the shell untouched.
function keyHandler(t, e) {
  if (e.type !== "keydown" || !(e.ctrlKey || e.metaKey) || e.altKey) return true;
  if (e.ctrlKey && e.shiftKey && shortcut(t, e)) { e.preventDefault(); return false; }
  if (e.ctrlKey && (!e.shiftKey || e.key === "+")) {
    const step = e.key === "=" || e.key === "+" ? 1 : e.key === "-" ? -1 : e.key === "0" ? 0 : null;
    if (step !== null) { e.preventDefault(); setFont(step === 0 ? 13 : T.font + step); return false; }
  }
  const key = e.key.toLowerCase();
  if (key === "c" && t.term.hasSelection()) {
    navigator.clipboard.writeText(t.term.getSelection()).catch(() => {});
    t.term.clearSelection();
    return false;
  }
  return key !== "v"; // let the browser's paste event reach xterm instead of sending ^V
}

function shortcut(t, e) {
  const g = curGroup();
  switch (e.code) {
    case "KeyD": splitPane(t.id, "right"); return true;
    case "KeyE": splitPane(t.id, "bottom"); return true;
    case "KeyW": closeSession(t.id); return true;
    case "KeyZ": toggleZoom(t.id); return true;
    case "KeyT": addGroup(); return true;
    case "PageUp": case "PageDown": {
      const i = T.layout.groups.indexOf(g), n = T.layout.groups.length;
      if (n > 1) switchGroup(T.layout.groups[(i + (e.code === "PageUp" ? n - 1 : 1)) % n].id);
      return true;
    }
    case "ArrowLeft": case "ArrowRight": case "ArrowUp": case "ArrowDown": moveFocus(t.id, e.code.slice(5).toLowerCase()); return true;
    default: return false;
  }
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

// Fit a terminal to its pane. Hidden or zero-size hosts are skipped (a fit there would shrink the
// shell to nothing); the resulting size is sent to the shell after a short quiet period, so
// dragging a divider doesn't flood the PTY with resizes.
function fitNow(t) {
  if (!t || !t.host.isConnected || !t.host.offsetParent || !t.host.clientWidth || !t.host.clientHeight) return;
  try { t.fit.fit(); } catch { return; }
  if (`${t.term.cols}x${t.term.rows}` === t.sent || t.exited) return;
  clearTimeout(t.rz);
  t.rz = setTimeout(() => {
    const size = `${t.term.cols}x${t.term.rows}`;
    if (size === t.sent || t.exited) return;
    t.sent = size;
    api("POST", `/api/terminals/sessions/${t.id}/resize`, { cols: t.term.cols, rows: t.term.rows }).catch(() => {});
    const s = session(t.id);
    if (s) { s.cols = t.term.cols; s.rows = t.term.rows; renderList(); }
  }, 150);
}

function fitAll(refresh = false) {
  for (const sid of visibleSids()) {
    const t = T.terms.get(sid);
    if (!t) continue;
    fitNow(t);
    if (refresh && t.host.offsetParent) t.term.refresh(0, t.term.rows - 1);
  }
}

function setFont(size) {
  T.font = clamp(size, 8, 32);
  PG.storageSet(FONT_KEY, String(T.font));
  for (const t of T.terms.values()) t.term.options.fontSize = T.font;
  requestAnimationFrame(() => fitAll(true));
}

// ---- output stream ------------------------------------------------------------------------
// One fetch carries the output of every visible, unfinished session.
function closeStream() {
  clearTimeout(T.syncTimer);
  if (T.stream) T.stream.controller.abort();
  T.stream = null;
}

function syncStream() {
  clearTimeout(T.syncTimer);
  T.syncTimer = setTimeout(openStream, 80);
}

async function openStream() {
  const wanted = T.active ? visibleSids().filter(sid => session(sid)).map(sid => termFor(session(sid))).filter(t => !t.exited) : [];
  const ids = new Set(wanted.map(t => t.id));
  if (T.stream && T.stream.sids.size === ids.size && [...ids].every(id => T.stream.sids.has(id))) return;
  closeStream();
  if (!wanted.length) return;
  const controller = new AbortController(), mine = { sids: ids, controller };
  T.stream = mine;
  const since = {};
  for (const t of wanted) since[t.id] = t.offset;
  const remaining = new Set(ids);
  try {
    const res = await fetch("/api/terminals/sessions/stream", { method: "POST", signal: controller.signal,
      headers: { "X-PatchGoblin": "1", "Content-Type": "application/json" }, body: JSON.stringify({ since, client: clientId }) });
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
        const t = T.terms.get(ev.s);
        if (ev.event === "out" && t) {
          if (ev.truncated && t.offset > 0) t.term.write("\r\n\x1b[2m[older output was dropped]\x1b[0m\r\n");
          if (ev.d) t.term.write(ev.d);
          t.offset = ev.end;
        } else if (ev.event === "exit") {
          remaining.delete(ev.s);
          if (t) markExited(t, ev.code);
        } else if (ev.event === "gone") {
          remaining.delete(ev.s);
          loadSessions();
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") return;
  } finally {
    if (T.stream === mine) T.stream = null;
  }
  // The stream dropped with shells still running (e.g. PatchGoblin restarted): try again.
  if (remaining.size && T.active && T.stream === null) setTimeout(() => { if (T.active && !T.stream) loadSessions(); }, 1500);
}

function markExited(t, code) {
  if (t.exited) return;
  t.exited = true;
  t.term.write(`\r\n\x1b[2m[process exited${code !== null && code !== undefined ? ` with code ${code}` : ""}]\x1b[0m\r\n`);
  const s = session(t.id);
  if (s) { s.alive = false; s.exit_code = code; }
  renderList();
  updatePanes();
  updateBadge();
}

// ---- rendering the layout --------------------------------------------------------------------
function refresh(focus = false) {
  fixup();
  saveLayout();
  renderTabs();
  renderLayout(focus);
  renderList();
  if (T.active) syncStream();
}

function renderTabs() {
  const strip = $("#term-groups");
  strip.replaceChildren(...T.layout.groups.map(g => {
    const tab = el("div", { class: `term-tab${g.id === T.layout.current ? " active" : ""}`, role: "tab" },
      el("button", { type: "button", title: "Switch to this group (double-click to rename)", onclick: () => switchGroup(g.id),
        ondblclick: () => renameGroup(g) }, g.name),
      el("button", { type: "button", class: "term-tab-x", title: "Close this group and its terminals", "aria-label": `Close ${g.name}`,
        onclick: () => closeGroup(g) }, "×"));
    tab.addEventListener("dragover", e => { if (T.drag) { e.preventDefault(); tab.classList.add("drop"); } });
    tab.addEventListener("dragleave", () => tab.classList.remove("drop"));
    tab.addEventListener("drop", e => { e.preventDefault(); tab.classList.remove("drop"); if (T.drag) moveToGroup(T.drag, g.id); });
    return tab;
  }), el("button", { type: "button", class: "term-tab-add", title: "New group (Ctrl+Shift+T)", "aria-label": "New group", onclick: addGroup }, "+"));
}

function renderLayout(focus = false) {
  const stage = $("#term-stage"), g = curGroup();
  const empty = !T.list.length;
  $("#term-bar").hidden = empty;
  $("#term-empty").hidden = !empty;
  stage.hidden = empty;
  T.panes = new Map();
  const place = new Set();
  let rootEl = null;
  if (g && g.root) {
    g.root = prune(g.root, sid => !!session(sid));
    const node = g.zoom && session(g.zoom) ? { type: "pane", sid: g.zoom } : g.root;
    if (node) { rootEl = build(node, place, g); rootEl.style.flex = "1 1 0"; }
  }
  T.drawn = true;
  // Hosts of panes that aren't on screen wait in the hidden parking area; they keep their scrollback.
  for (const [sid, t] of T.terms) if (!place.has(sid) && t.host.parentElement !== $("#term-parking")) $("#term-parking").append(t.host);
  stage.replaceChildren(...(rootEl ? [rootEl] : []));
  updatePanes();
  requestAnimationFrame(() => {
    fitAll(true);
    const f = T.terms.get(g && g.focus);
    if (focus && f && T.active) f.term.focus();
  });
}

function build(node, place, g) {
  if (node.type === "pane") return buildPane(node.sid, place, g);
  const box = el("div", { class: `term-split ${node.dir}` });
  node.children.forEach((c, i) => {
    const childEl = build(c, place, g);
    childEl.style.flex = `${node.sizes[i]} 1 0`;
    if (i) box.append(gutter(node, i - 1, box));
    box.append(childEl);
  });
  return box;
}

function buildPane(sid, place, g) {
  const s = session(sid);
  const t = termFor(s);
  place.add(sid);
  const swatch = el("span", { class: "term-swatch" }), title = el("span", { class: "term-pane-title" }),
    state = el("span", { class: "muted small" });
  const btn = (label, tip, fn, cls = "ghost small") => el("button", { type: "button", class: cls, title: tip, "aria-label": tip,
    onclick: e => { e.stopPropagation(); fn(e); } }, label);
  const zoom = btn(g.zoom === sid ? "⤡" : "⤢", g.zoom === sid ? "Show all panes (Ctrl+Shift+Z)" : "Zoom this pane (Ctrl+Shift+Z)", () => toggleZoom(sid));
  const head = el("div", { class: "term-pane-head", draggable: "true" }, swatch, title, state,
    el("span", { class: "term-pane-btns" },
      btn("◫", "Split right (Ctrl+Shift+D)", () => splitPane(sid, "right")),
      btn("⬓", "Split down (Ctrl+Shift+E)", () => splitPane(sid, "bottom")),
      zoom,
      btn("⋯", "More", e => paneMenu(e.currentTarget, sid)),
      btn("×", "Close this terminal (Ctrl+Shift+W)", () => closeSession(sid))));
  const slot = el("div", { class: "term-slot" });
  slot.append(t.host);
  const root = el("div", { class: `term-pane${g.focus === sid ? " focused" : ""}` }, head, slot);
  head.addEventListener("dragstart", e => {
    T.drag = sid;
    e.dataTransfer.setData("text/plain", sid);
    e.dataTransfer.effectAllowed = "move";
  });
  head.addEventListener("dragend", () => { T.drag = null; clearDrops(); });
  root.addEventListener("mousedown", () => setFocus(sid));
  root.addEventListener("dragover", e => {
    if (!T.drag || T.drag === sid) return;
    e.preventDefault();
    showDrop(root, dropZone(root, e));
  });
  root.addEventListener("dragleave", e => { if (!root.contains(e.relatedTarget)) clearDrops(); });
  root.addEventListener("drop", e => {
    if (!T.drag || T.drag === sid) return;
    e.preventDefault();
    const zone = dropZone(root, e);
    clearDrops();
    movePane(T.drag, sid, zone);
  });
  T.panes.set(sid, { root, swatch, title, state, zoom });
  return root;
}

// Refresh headers (title, colour, state) without rebuilding the layout.
function updatePanes() {
  const g = curGroup();
  for (const [sid, p] of T.panes) {
    const s = session(sid);
    if (!s) continue;
    p.title.textContent = s.title;
    p.title.title = s.title;
    p.swatch.style.background = s.color || "var(--muted)";
    p.state.textContent = s.alive ? "" : `exited${s.exit_code !== null ? ` (${s.exit_code})` : ""}`;
    p.root.classList.toggle("focused", !!g && g.focus === sid);
  }
}

function gutter(node, i, box) {
  const g = el("div", { class: "term-gutter", role: "separator", "aria-orientation": node.dir === "row" ? "vertical" : "horizontal" });
  g.addEventListener("pointerdown", e => {
    e.preventDefault();
    g.setPointerCapture(e.pointerId);
    g.classList.add("drag");
    $("#term-stage").classList.add("dragging");
    const row = node.dir === "row", kids = [...box.children].filter(c => !c.classList.contains("term-gutter"));
    const room = (row ? box.clientWidth : box.clientHeight) - 6 * (kids.length - 1);
    const start = row ? e.clientX : e.clientY, a = node.sizes[i], b = node.sizes[i + 1];
    let raf = 0;
    const move = ev => {
      const d = ((row ? ev.clientX : ev.clientY) - start) / room;
      node.sizes[i] = clamp(a + d, MIN_FRACTION, a + b - MIN_FRACTION);
      node.sizes[i + 1] = a + b - node.sizes[i];
      kids[i].style.flex = `${node.sizes[i]} 1 0`;
      kids[i + 1].style.flex = `${node.sizes[i + 1]} 1 0`;
      if (!raf) raf = requestAnimationFrame(() => { raf = 0; fitAll(); });
    };
    const up = () => {
      g.removeEventListener("pointermove", move);
      g.removeEventListener("pointerup", up);
      g.removeEventListener("pointercancel", up);
      g.classList.remove("drag");
      $("#term-stage").classList.remove("dragging");
      saveLayout();
      fitAll(true);
    };
    g.addEventListener("pointermove", move);
    g.addEventListener("pointerup", up);
    g.addEventListener("pointercancel", up);
  });
  return g;
}

// ---- drag a pane header to rearrange ------------------------------------------------------------
function dropZone(root, e) {
  const r = root.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
  if (x > 0.3 && x < 0.7 && y > 0.3 && y < 0.7) return "center";
  const d = { left: x, right: 1 - x, top: y, bottom: 1 - y };
  return Object.keys(d).reduce((best, k) => (d[k] < d[best] ? k : best), "left");
}

function showDrop(root, zone) {
  clearDrops();
  const box = el("div", { class: "term-drop" });
  const pos = { left: "top:0;bottom:0;left:0;width:50%", right: "top:0;bottom:0;right:0;width:50%",
    top: "left:0;right:0;top:0;height:50%", bottom: "left:0;right:0;bottom:0;height:50%", center: "inset:25%" }[zone];
  box.style.cssText = pos;
  root.append(box);
}

function clearDrops() {
  for (const d of document.querySelectorAll("#term-view .term-drop")) d.remove();
  for (const d of document.querySelectorAll("#term-view .term-tab.drop")) d.classList.remove("drop");
}

function movePane(src, target, zone) {
  if (src === target) return;
  if (zone === "center") {
    const ga = groupOf(src), gb = groupOf(target);
    const a = ga && findPane(ga.root, src), b = gb && findPane(gb.root, target);
    if (!a || !b) return;
    a.sid = target;
    b.sid = src;
    if (ga.zoom === src) ga.zoom = target;
    if (ga !== gb && ga.focus === src) ga.focus = target;
    gb.focus = src;
    T.layout.current = gb.id;
  } else {
    removeFromLayout(src);
    const g = groupOf(target);
    if (!g) return;
    g.root = insertAt(g.root, target, src, zone);
    g.focus = src;
    g.zoom = null;
    T.layout.current = g.id;
  }
  refresh(true);
}

function moveToGroup(sid, groupId) {
  const to = T.layout.groups.find(g => g.id === groupId);
  if (!to || groupOf(sid) === to) return;
  removeFromLayout(sid);
  if (to.root) {
    const panes = panesOf(to.root), target = panes.includes(to.focus) ? to.focus : panes[0];
    to.root = insertAt(to.root, target, sid, longerSide(target));
  } else to.root = { type: "pane", sid };
  to.focus = sid;
  to.zoom = null;
  refresh();
}

// ---- focus and navigation ----------------------------------------------------------------------
function setFocus(sid) {
  const g = groupOf(sid);
  if (!g || g.focus === sid) return;
  g.focus = sid;
  saveLayout();
  updatePanes();
  renderList();
}

function focusSession(sid) {
  const g = groupOf(sid);
  if (!g) return;
  T.layout.current = g.id;
  g.focus = sid;
  if (g.zoom && g.zoom !== sid) g.zoom = sid;
  if (!T.active) PG.show("terminals");
  refresh(true);
}

function switchGroup(id) {
  if (id === T.layout.current || !T.layout.groups.some(g => g.id === id)) return;
  T.layout.current = id;
  refresh(true);
}

function addGroup() {
  const g = newGroup();
  T.layout.groups.push(g);
  T.layout.current = g.id;
  const pid = $("#term-new-project").value;
  newSession(pid, { group: g.id });
  refresh();
}

function renameGroup(g) {
  const name = prompt("Name this group", g.name);
  if (!name || !name.trim()) return;
  g.name = name.trim().slice(0, 40);
  saveLayout();
  renderTabs();
  renderList();
}

async function closeGroup(g) {
  const sids = panesOf(g.root).filter(session);
  if (sids.some(sid => session(sid).alive) && !confirm(`End ${sids.length} terminal(s) in "${g.name}"?`)) return;
  try {
    for (const sid of sids) { T.list = (await api("DELETE", `/api/terminals/sessions/${sid}`)).sessions; dropTerm(sid); }
  } catch (e) { toast(e.message, true); }
  T.layout.groups = T.layout.groups.filter(x => x !== g);
  if (T.layout.current === g.id) T.layout.current = null;
  reconcile();
  updateBadge();
  refresh();
}

function toggleZoom(sid) {
  const g = groupOf(sid);
  if (!g || panesOf(g.root).length < 2) return;
  g.zoom = g.zoom ? null : sid;
  g.focus = sid;
  refresh(true);
}

function moveFocus(sid, dir) {
  const here = T.panes.get(sid);
  if (!here) return;
  const c = r => ({ x: r.left + r.width / 2, y: r.top + r.height / 2 }), me = c(here.root.getBoundingClientRect());
  let best = null, bestScore = Infinity;
  for (const [id, p] of T.panes) {
    if (id === sid) continue;
    const o = c(p.root.getBoundingClientRect()), dx = o.x - me.x, dy = o.y - me.y;
    const along = dir === "left" ? -dx : dir === "right" ? dx : dir === "up" ? -dy : dy;
    const across = Math.abs(dir === "left" || dir === "right" ? dy : dx);
    if (along <= 0) continue;
    const score = along + 2 * across;
    if (score < bestScore) { best = id; bestScore = score; }
  }
  if (!best) return;
  const g = curGroup();
  g.focus = best;
  saveLayout();
  updatePanes();
  renderList();
  const t = T.terms.get(best);
  if (t) t.term.focus();
}

// ---- pane actions --------------------------------------------------------------------------------
let menuEl = null;
function closeMenu() { if (menuEl) menuEl.remove(); menuEl = null; }

function paneMenu(button, sid) {
  closeMenu();
  const s = session(sid);
  if (!s) return;
  const item = (label, fn, disabled = false) => el("button", { type: "button", class: "ghost small", disabled,
    onclick: () => { closeMenu(); fn(); } }, label);
  menuEl = el("div", { class: "term-menu" },
    item("Rename", () => renameSession(sid)),
    item("Pop out", () => popOut(sid), !s.project),
    item("Restart", () => restartSession(sid)));
  const r = button.getBoundingClientRect();
  menuEl.style.top = `${r.bottom + 2}px`;
  menuEl.style.right = `${Math.max(4, window.innerWidth - r.right)}px`;
  $("#term-view").append(menuEl);
  const away = e => { if (menuEl && menuEl.contains(e.target)) return; document.removeEventListener("pointerdown", away); closeMenu(); };
  setTimeout(() => document.addEventListener("pointerdown", away), 0);
}

async function renameSession(sid) {
  const s = session(sid);
  const title = s && prompt("Name this terminal", s.title);
  if (!title) return;
  try { T.list = (await api("POST", `/api/terminals/sessions/${sid}/rename`, { title })).sessions; updatePanes(); renderList(); }
  catch (e) { toast(e.message, true); }
}

async function popOut(sid) {
  try { await api("POST", `/api/terminals/sessions/${sid}/popout`); toast("Opened in Windows Terminal"); }
  catch (e) { toast(e.message, true); }
}

async function restartSession(old) {
  try {
    const data = await api("POST", `/api/terminals/sessions/${old}/restart`);
    const g = groupOf(old), fresh = data.session.id;
    if (g) (function walk(n) { if (n.type === "pane") { if (n.sid === old) n.sid = fresh; } else n.children.forEach(walk); })(g.root);
    if (g && g.focus === old) g.focus = fresh;
    if (g && g.zoom === old) g.zoom = fresh;
    dropTerm(old);
    T.list = data.sessions;
    refresh(true);
  } catch (e) { toast(e.message, true); }
}

function splitPane(sid, side) {
  const s = session(sid);
  newSession(s ? s.project : $("#term-new-project").value, { target: sid, side });
}

async function newSession(projectId, where = {}) {
  const ref = T.terms.get(where.target) || T.terms.get(curGroup()?.focus);
  try {
    const data = await api("POST", "/api/terminals/sessions", { project: projectId || "",
      cols: ref ? ref.term.cols : 120, rows: ref ? ref.term.rows : 32 });
    T.list = data.sessions;
    updateBadge();
    if (!T.active) PG.show("terminals");
    const id = data.session.id;
    if (!groupOf(id)) addPane(id, { ...where, switch: true });
    const g = groupOf(id);
    if (g) { T.layout.current = g.id; g.focus = id; }
    refresh(true);
  } catch (e) {
    toast(e.message, true);
    refresh();
  }
}

async function closeSession(id) {
  const s = session(id);
  if (s && s.alive && !confirm(`End the shell in "${s.title}"?`)) return;
  try {
    T.list = (await api("DELETE", `/api/terminals/sessions/${id}`)).sessions;
    removeFromLayout(id);
    dropTerm(id);
    updateBadge();
    refresh(true);
  } catch (e) { toast(e.message, true); }
}

PG.register({
  id: "terminals",
  setup() {
    if (typeof Terminal === "undefined") { toast("Terminals: xterm.js didn't load", true); return; }
    loadLayout();
    $("#term-new-btn").onclick = () => newSession($("#term-new-project").value);
    $("#term-split-right").onclick = () => { const g = curGroup(); if (g && g.focus) splitPane(g.focus, "right"); else newSession($("#term-new-project").value); };
    $("#term-split-down").onclick = () => { const g = curGroup(); if (g && g.focus) splitPane(g.focus, "bottom"); else newSession($("#term-new-project").value); };
    $("#term-font-minus").onclick = () => setFont(T.font - 1);
    $("#term-font-plus").onclick = () => setFont(T.font + 1);
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
        if (running) { PG.show("terminals"); focusSession(running.id); } else await newSession(pid);
      } finally { starting.delete(pid); }
    });
    let fitTimer = null;
    new ResizeObserver(() => { clearTimeout(fitTimer); fitTimer = setTimeout(() => fitAll(true), 60); })
      .observe($("#term-stage"));
    new MutationObserver(() => { for (const t of T.terms.values()) t.term.options.theme = theme(); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeMenu(); });
    renderTabs();
    loadSessions();
  },
  activate() {
    T.active = true;
    loadProjects();
    loadSessions().then(() => { // lays out the panes and opens the stream
      const g = curGroup(), t = g && T.terms.get(g.focus);
      if (t && T.active) t.term.focus();
    });
    clearInterval(T.poll);
    T.poll = setInterval(() => { if (!document.hidden) loadSessions(); }, 10000);
  },
  deactivate() {
    T.active = false;
    clearInterval(T.poll);
    closeStream(); // frees the server thread; output keeps collecting in the scrollback
  },
});
})();
