// SSH tools plugin: saved servers, keys, key copying, the login network diagram and port tunnels.
// Showing or hiding the plugin never resets anything; tunnels are polled either way (slower when
// hidden) so the activity-bar badge stays current.
(() => {
const { $, $$, el, api, toast, showError } = PG;

const SSH_TOOLS = { servers: "Servers", keys: "Keys", install: "Install keys", network: "Network", tunnels: "Tunnels" };
const ssh = {
  active: false, // whether the plugin is the one shown
  tool: "keys", // the open tool, a SSH_TOOLS key
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

function resetTunnelTimer() {
  clearInterval(ssh.timer);
  ssh.timer = setInterval(() => { if (!document.hidden) loadTunnels(); }, ssh.active ? 5000 : 30000);
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
  if (!SSH_TOOLS[tool]) tool = "keys";  ssh.tool = tool;
  PG.storageSet("pg.sshTool", tool);
  for (const li of $$("#ssh-nav li")) li.classList.toggle("active", li.dataset.tool === tool);
  for (const t of Object.keys(SSH_TOOLS)) $(`#ssh-${t}-panel`).hidden = t !== tool;
  $("#ssh-title").textContent = SSH_TOOLS[tool];
  $("#ssh-log").hidden = true;
  await loadSshServers(tool === "servers"); // always fresh suggestions on the Servers tool
  if (ssh.tool !== tool) return; // the user moved on while the servers loaded
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
  PG.setBadge("ssh", ssh.running, `${ssh.running} running SSH tunnel${ssh.running === 1 ? "" : "s"}`);
  if (!ssh.active) return;
  renderTunnelList();
  if (ssh.tool === "tunnels") renderTunnelPanel();
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
  } catch (e) { toast(e.message, true); if (ssh.tool === "tunnels") sshLog(e.message); }
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

// The Add project dialog offers saved servers as SSH targets and fills in their port.
function hookProjectDialog(form) {
  loadSshServers(true);
  if (form.dataset.sshHooked) return;
  form.dataset.sshHooked = "1";
  form.ssh_target.addEventListener("change", () => {
    const server = matchServer(form.ssh_target.value);
    if (server && server.port && !form.ssh_port.value) form.ssh_port.value = server.port;
  });
}

PG.register({
  id: "ssh",
  setup() {
    const saved = PG.storageGet("pg.sshTool");
    ssh.tool = SSH_TOOLS[saved] ? saved : "keys";
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
    document.addEventListener("pg:project-dialog-open", e => hookProjectDialog(e.detail.form));
    document.addEventListener("pg:project-added", () => { ssh.loaded = false; }); // its host becomes a suggestion
    document.addEventListener("visibilitychange", () => { if (!document.hidden) loadTunnels(); });
    loadTunnels();
    resetTunnelTimer();
  },
  activate() {
    ssh.active = true;
    openSshTool(ssh.tool);
    loadTunnels();
    resetTunnelTimer();
  },
  deactivate() {
    ssh.active = false;
    resetTunnelTimer();
  },
});
})();
