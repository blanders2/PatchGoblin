// Todo plugin: a global checklist with optional project tags. The server owns the list; every
// change is one small request that returns the whole list, which is then re-rendered.
(() => {
const { $, el, api, showError } = PG;

const FILTERS = { all: "All", open: "Open", done: "Done" };
const todo = {
  items: [], // [{id, text, done, project, created, done_at}]
  projects: [], // [{id, name}]
  filter: "all", // a FILTERS key or "p:<project id>"
  editing: null, // id of the item whose text is being edited
  dragId: null,
};

const projectName = id => (todo.projects.find(p => p.id === id) || {}).name || id;
const filterProject = () => todo.filter.startsWith("p:") ? todo.filter.slice(2) : "";

function visible() {
  const project = filterProject();
  return todo.items.filter(i => {
    if (project) return i.project === project;
    if (todo.filter === "open") return !i.done;
    if (todo.filter === "done") return i.done;
    return true;
  });
}

function apply(data) {
  todo.items = data.items;
  PG.setBadge("todo", data.open || "", `${data.open} open`);
  render();
}

async function send(method, url, body) {
  showError($("#todo-error"), "");
  try {
    apply(await api(method, url, body));
    return true;
  } catch (e) {
    showError($("#todo-error"), e.message);
    return false;
  }
}

async function load() {
  try { apply(await api("GET", "/api/todo/items")); } catch (e) { showError($("#todo-error"), e.message); }
}

async function loadProjects() {
  try { todo.projects = (await api("GET", "/api/projects")).projects; } catch { /* keep the old list */ }
  render();
}

function setFilter(filter) {
  todo.filter = filter;
  PG.storageSet("todo-filter", filter);
  render();
}

function renderFilters() {
  const list = $("#todo-filters");
  const count = f => todo.items.filter(i => f(i)).length;
  const entry = (key, label, n) => el("li", {
    tabindex: "0", class: todo.filter === key ? "active" : "",
    onclick: () => setFilter(key),
    onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setFilter(key); } },
  }, el("span", {}, label), el("span", { class: "todo-count" }, String(n)));
  const rows = [
    entry("all", "All", todo.items.length),
    entry("open", "Open", count(i => !i.done)),
    entry("done", "Done", count(i => i.done)),
  ];
  if (todo.projects.length) rows.push(el("li", { class: "todo-sep", "aria-hidden": "true" }));
  for (const p of todo.projects) rows.push(entry(`p:${p.id}`, p.name, count(i => i.project === p.id)));
  list.replaceChildren(...rows);
}

function renderProjectSelect() {
  const sel = $("#todo-add-project"), want = filterProject() || sel.value;
  sel.replaceChildren(el("option", { value: "" }, "No project"),
    ...todo.projects.map(p => el("option", { value: p.id }, p.name)));
  sel.value = [...sel.options].some(o => o.value === want) ? want : "";
}

function finishEdit(item, input, save) {
  todo.editing = null;
  const text = input.value.trim();
  if (save && text && text !== item.text) send("PATCH", `/api/todo/items/${item.id}`, { text });
  else render();
}

function rowFor(item) {
  const text = todo.editing === item.id
    ? el("input", { class: "todo-edit", value: item.text, maxlength: "500", "aria-label": "Edit item" })
    : el("span", { class: "todo-text", ondblclick: () => { todo.editing = item.id; render(); } }, item.text);
  if (todo.editing === item.id) {
    let ended = false;
    const end = save => { if (!ended) { ended = true; finishEdit(item, text, save); } };
    text.addEventListener("keydown", e => {
      if (e.key === "Enter") end(true);
      else if (e.key === "Escape") end(false);
    });
    text.addEventListener("blur", () => end(true));
    setTimeout(() => text.focus(), 0);
  }
  const row = el("li", { class: `todo-item${item.done ? " todo-done" : ""}`, draggable: todo.editing ? null : "true", "data-id": item.id },
    el("input", { type: "checkbox", checked: item.done, "aria-label": "Done",
      onchange: e => send("PATCH", `/api/todo/items/${item.id}`, { done: e.target.checked }) }),
    text,
    item.project ? el("span", { class: "todo-chip", title: "Project" }, projectName(item.project)) : null,
    el("button", { type: "button", class: "ghost todo-del", title: "Delete", "aria-label": "Delete",
      onclick: () => send("DELETE", `/api/todo/items/${item.id}`) }, "✕"));
  row.addEventListener("dragstart", e => {
    todo.dragId = item.id;
    row.classList.add("todo-dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", item.id);
  });
  row.addEventListener("dragend", () => { todo.dragId = null; render(); });
  row.addEventListener("dragover", e => {
    if (!todo.dragId || todo.dragId === item.id) return;
    e.preventDefault();
    row.classList.add("todo-over");
  });
  row.addEventListener("dragleave", () => row.classList.remove("todo-over"));
  row.addEventListener("drop", e => {
    e.preventDefault();
    const from = todo.dragId;
    todo.dragId = null;
    if (from && from !== item.id) moveBefore(from, item.id);
  });
  return row;
}

// Put `fromId` just before `beforeId`. With a filter active only the visible items are reordered:
// their slots in the full list are refilled in the new order, so hidden items stay where they are.
function moveBefore(fromId, beforeId) {
  const shown = visible().map(i => i.id);
  const order = shown.filter(id => id !== fromId);
  order.splice(order.indexOf(beforeId), 0, fromId);
  const slots = new Set(shown);
  let next = 0;
  const ids = todo.items.map(i => slots.has(i.id) ? order[next++] : i.id);
  send("PUT", "/api/todo/order", { ids });
}

function render() {
  const project = filterProject();
  $("#todo-title").textContent = project ? projectName(project) : FILTERS[todo.filter] || "All";
  renderFilters();
  renderProjectSelect();
  const shown = visible();
  $("#todo-list").replaceChildren(...shown.map(rowFor));
  $("#todo-empty").hidden = shown.length > 0;
  $("#todo-clear-done").hidden = !todo.items.some(i => i.done);
}

PG.register({
  id: "todo",
  setup() {
    const saved = PG.storageGet("todo-filter");
    if (saved && (FILTERS[saved] || saved.startsWith("p:"))) todo.filter = saved;
    $("#todo-add-form").onsubmit = async e => {
      e.preventDefault();
      const input = $("#todo-add-text");
      if (!input.value.trim()) return;
      if (await send("POST", "/api/todo/items", { text: input.value, project: $("#todo-add-project").value })) {
        input.value = "";
        input.focus();
      }
    };
    $("#todo-clear-done").onclick = () => send("POST", "/api/todo/clear-done");
    load();
    loadProjects();
  },
  activate() {
    load();
    loadProjects();
  },
  deactivate() {},
});
})();
