const $ = (selector, root = document) => root.querySelector(selector);
const token = $('meta[name="patchgoblin-token"]').content;
let projects = [], selected = localStorage.getItem('patchgoblin-project'), detail = null;
let paused = false, editingProject = false, editingTask = null, openTask = null, refreshing = false;
const providerNames = {codex: 'Codex CLI', claude: 'Claude Code', openai: 'OpenAI API'};
const labels = {unplanned: 'Unplanned', planning_queued: 'Waiting to plan', planning: 'Planning', planned: 'Planned', queued: 'Queued', running: 'Running', validation: 'Validation', revising_queued: 'Waiting to discuss', revising: 'Revising plan', completed: 'Completed', failed: 'Failed'};
const lanes = [
  ['unplanned', 'Unplanned', 'Capture the next change.', ['unplanned', 'planning_queued', 'planning']],
  ['planned', 'Planned', 'A little thinking before the doing.', ['planned']],
  ['queued', 'Queued', 'Ready when you are.', ['queued']],
  ['running', 'In progress', 'The goblin is standing by.', ['running', 'failed']],
  ['validation', 'Validation', 'Your review. Your call.', ['validation', 'revising_queued', 'revising']],
  ['completed', 'Completed', 'Finished work lands here.', ['completed']],
];
function element(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
async function api(path, method = 'GET', data) {
  const res = await fetch('/api' + path, {method, headers: {'Content-Type': 'application/json', 'X-PatchGoblin-Token': token}, body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await res.json();
  if (!res.ok) throw new Error(result.error || 'Request failed');
  return result;
}
function notify(message, error = false) {
  const el = $('#notice'); el.textContent = message; el.className = error ? 'error' : ''; el.hidden = false;
}
function button(text, action, className = 'subtle') {
  const el = element('button', className, text); el.type = 'button';
  el.addEventListener('click', async () => {
    el.disabled = true;
    try { await action(); } catch (err) {
      if ($('#detail-dialog').open) $('#detail-error').textContent = err.message;
      else notify(err.message, true);
    }
    finally { el.disabled = false; }
  });
  return el;
}
function projectPath() { return `/projects/${selected}`; }
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const state = await api('/projects'); projects = state.projects; paused = state.paused;
    if (!projects.some(p => p.id === selected)) selected = projects[0]?.id;
    $('#pause').textContent = paused ? 'Resume queue' : 'Pause queue';
    $('#pause').hidden = !projects.length;
    const nav = $('#projects'); nav.replaceChildren();
    for (const project of projects) {
      const el = button(project.name, async () => {
        selected = project.id; detail = null; $('#board').replaceChildren(); localStorage.setItem('patchgoblin-project', selected);
        $('#notice').hidden = true; await refresh();
      }, 'project-link' + (selected === project.id ? ' selected' : ''));
      el.append(element('span', '', project.kind === 'ssh' ? project.host : 'This computer'));
      nav.append(el);
    }
    $('#welcome').hidden = !!selected; $('#workspace').hidden = !selected;
    if (!selected) { $('#breadcrumb').textContent = 'Projects'; return; }
    const current = selected;
    try {
      const response = await api(`/projects/${current}`);
      if (current !== selected) return;
      detail = response; render(state);
    } catch (err) {
      $('#project-name').textContent = projects.find(p => p.id === selected)?.name || 'Project';
      $('#sync-status').textContent = 'Connection unavailable';
      notify(err.message, true);
    }
  } catch (err) { notify(err.message, true); }
  finally { refreshing = false; }
}
function render(state) {
  const project = detail.project;
  $('#breadcrumb').textContent = project.name;
  $('#project-name').textContent = project.name;
  $('#project-path').textContent = project.path;
  $('#host-label').textContent = project.kind === 'ssh' ? `SSH PROJECT / ${project.host}` : 'LOCAL PROJECT';
  $('#branch').textContent = '⑂ ' + (detail.branch || 'No branch yet');
  $('#provider').textContent = providerNames[project.provider];
  $('#provider-health').textContent = detail.providers[project.provider] ? 'Available on host' : 'Setup needed on host';
  const pending = detail.tasks.filter(t => ['queued', 'planning_queued', 'revising_queued'].includes(t.status)).length;
  $('#queue-status').textContent = paused ? 'Queue paused' : state.active ? 'Goblin at work' : pending ? `${pending} waiting` : 'Queue ready';
  $('#queue-description').textContent = state.active ? state.active.title : paused ? 'Current work will finish. Waiting tasks stay put.' : 'Tasks run one at a time.';
  $('#task-count').textContent = `${detail.tasks.length} task${detail.tasks.length === 1 ? '' : 's'}`;
  $('#sync-status').textContent = 'Synced ' + new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  const board = $('#board'); board.replaceChildren();
  for (const [key, label, empty, statuses] of lanes) {
    const lane = element('section', 'lane'); lane.dataset.lane = key;
    const tasks = detail.tasks.filter(t => statuses.includes(t.status));
    const heading = element('div', 'lane-heading');
    heading.append(element('span', 'lane-mark'), element('span', '', label), element('span', 'count', tasks.length));
    lane.append(heading);
    if (!tasks.length) lane.append(element('div', 'empty-lane', empty));
    for (const task of tasks) {
      const card = element('article', 'task-card' + (task.status === 'failed' ? ' failed' : ''));
      card.append(element('span', 'card-id', '#' + task.id.slice(0, 6).toUpperCase()));
      card.append(button(task.title, () => showDetail(task.id), 'card-title'));
      if (['planning', 'planning_queued', 'running', 'failed', 'revising_queued', 'revising'].includes(task.status)) card.append(element('div', 'state-label', labels[task.status]));
      card.append(element('p', 'card-description', task.error || task.description || 'No description yet.'));
      const meta = element('div', 'card-meta'); meta.append(element('span', '', providerNames[task.provider]));
      if (task.status === 'unplanned') meta.append(button('Plan with AI', () => act(task.id, 'plan'), 'card-action'));
      else if (task.status === 'planned') meta.append(button('Queue →', () => act(task.id, 'queue'), 'card-action'));
      else meta.append(button(task.status === 'validation' ? 'Review' : 'Open', () => showDetail(task.id), 'card-action'));
      card.append(meta); lane.append(card);
    }
    board.append(lane);
  }
  if (openTask && $('#detail-dialog').open) showDetail(openTask);
  if (state.errors[selected]) notify(state.errors[selected], true);
}
async function act(taskId, action) {
  await api(`${projectPath()}/tasks/${taskId}/${action}`, 'POST', {});
  await refresh();
}
function showProject(edit = false) {
  editingProject = edit; const form = $('#project-form'); form.reset();
  $('.form-error', form).textContent = '';
  $('#project-dialog-title').textContent = edit ? 'Project settings' : 'Connect a project';
  $('#connection-fields').hidden = edit;
  $('#project-help').hidden = edit;
  form.elements.path.required = !edit;
  if (edit) for (const [key, value] of Object.entries(detail.project)) {
    const field = form.elements.namedItem(key);
    if (field) field.type === 'checkbox' ? field.checked = value : field.value = value;
  }
  updateProjectFields(); $('#project-dialog').showModal();
}
function updateProjectFields() {
  const form = $('#project-form'); const ssh = form.elements.kind.value === 'ssh';
  $('#ssh-fields').hidden = !ssh;
  form.elements.host.required = ssh && !editingProject;
  form.elements.path.placeholder = ssh ? '/home/me/projects/my-project' : 'G:\\Projects\\my-project';
  const isApi = form.elements.provider.value === 'openai';
  form.elements.model.required = isApi;
  form.elements.model.placeholder = isApi ? 'Enter a model ID' : 'Provider default';
  $('#provider-help').textContent = isApi ? 'Set OPENAI_API_KEY in the environment on the project host. File tools work within the project directory.' : 'The CLI must be installed and signed in on the project host.';
}
function showTask(task = null) {
  editingTask = task?.id || null; const form = $('#task-form'); form.reset();
  $('.form-error', form).textContent = ''; $('#plan-field').hidden = !task;
  $('#task-dialog-title').textContent = task ? 'Edit task & plan' : 'New task';
  if (task) for (const key of ['title', 'description', 'plan']) form.elements[key].value = task[key];
  $('#detail-dialog').close(); $('#task-dialog').showModal();
}
function showDetail(id) {
  const task = detail.tasks.find(t => t.id === id); if (!task) return;
  openTask = id;
  $('#detail-error').textContent = '';
  $('#detail-title').textContent = task.title;
  $('#detail-status').textContent = labels[task.status] + ' / #' + task.id.slice(0, 6);
  const actions = $('#detail-actions'); actions.replaceChildren();
  const add = (text, action, style) => actions.append(button(text, () => act(id, action), style));
  if (['unplanned', 'planned', 'failed', 'validation'].includes(task.status)) {
    actions.append(button('Edit task & plan', () => showTask(task)));
    add(task.plan ? 'Replan with AI' : 'Plan with AI', 'plan');
  }
  if (['unplanned', 'failed'].includes(task.status)) add('Mark planned', 'mark_planned', 'primary');
  if (task.status === 'planned') add('Queue for AI', 'queue', 'primary');
  if (task.status === 'validation') {
    add('Mark complete', 'complete', 'primary');
    add('Send back to run queue', 'queue');
  }
  if (['queued', 'planning_queued', 'revising_queued'].includes(task.status)) add('Remove from queue', 'unqueue');
  if (['planned', 'completed', 'failed'].includes(task.status)) add('Move to unplanned', 'reopen');
  if (['running', 'planning', 'revising'].includes(task.status)) add('Recover interrupted run', 'recover');
  const content = $('#detail-content'); content.replaceChildren();
  for (const [title, text] of [['Description', task.description], ['Plan', task.plan], ['Result', task.result], ['Error', task.error], ['Git commit', task.commit], ['Activity', task.history.map(h => `${new Date(h.at).toLocaleString()}  ${labels[h.status]}`).join('\n')]]) {
    if (!text) continue;
    const section = element('section', 'detail-section' + (title === 'Error' ? ' error' : ''));
    section.append(element('h3', '', title), element('pre', '', text)); content.append(section);
  }
  const conversation = $('#conversation'); conversation.replaceChildren();
  for (const message of task.messages || []) {
    const entry = element('div', 'message ' + message.role);
    entry.append(element('strong', '', message.role === 'user' ? 'You' : 'AI planner'), element('pre', '', message.content));
    conversation.append(entry);
  }
  const editable = ['unplanned', 'planned', 'failed', 'validation'].includes(task.status);
  $('#discuss-form').hidden = !editable;
  $('#discussion-status').textContent = ['revising', 'planning'].includes(task.status) ? 'AI is reading the project and working on your plan…' : ['revising_queued', 'planning_queued'].includes(task.status) ? 'Your discussion is queued. The reply will appear here.' : 'Discuss issues and refine the plan here. Implementation starts only when you queue it.';
  if (openTask !== $('#discussion-message').dataset.task) {
    $('#discussion-message').value = ''; $('#discussion-message').dataset.task = openTask;
  }
  if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
}
for (const id of ['#add-project', '#add-project-icon', '#welcome-add']) $(id).onclick = () => showProject();
$('#settings').onclick = () => detail && showProject(true);
$('#new-task').onclick = () => showTask();
$('#project-form').elements.kind.onchange = updateProjectFields;
$('#project-form').elements.provider.onchange = updateProjectFields;
document.querySelectorAll('.close').forEach(el => el.onclick = () => el.closest('dialog').close());
$('#detail-dialog').addEventListener('close', () => openTask = null);
$('#project-form').onsubmit = async event => {
  event.preventDefault(); const form = event.target; const submit = $('button[type=submit]', form); submit.disabled = true;
  const data = Object.fromEntries(new FormData(form)); data.allow_commands = form.elements.allow_commands.checked;
  try {
    const result = await api(editingProject ? projectPath() : '/projects', editingProject ? 'PATCH' : 'POST', data);
    selected = result.id; localStorage.setItem('patchgoblin-project', selected);
    $('#project-dialog').close(); $('#notice').hidden = true; await refresh();
  } catch (err) { $('.form-error', form).textContent = err.message; }
  finally { submit.disabled = false; }
};
$('#task-form').onsubmit = async event => {
  event.preventDefault(); const form = event.target; const submit = $('button[type=submit]', form); submit.disabled = true;
  try {
    await api(projectPath() + (editingTask ? `/tasks/${editingTask}/edit` : '/tasks'), 'POST', Object.fromEntries(new FormData(form)));
    $('#task-dialog').close(); await refresh();
  } catch (err) { $('.form-error', form).textContent = err.message; }
  finally { submit.disabled = false; }
};
$('#pause').onclick = async () => {
  try { await api('/queue', 'POST', {paused: !paused}); await refresh(); } catch (err) { notify(err.message, true); }
};
$('#checkpoint').onclick = () => {
  if (!detail) return;
  $('#git-changes').textContent = detail.git_status || 'Working tree is clean.';
  $('.form-error', $('#checkpoint-dialog')).textContent = ''; $('#checkpoint-dialog').showModal();
};
$('#confirm-checkpoint').onclick = async event => {
  event.target.disabled = true;
  try {
    const result = await api(projectPath() + '/checkpoint', 'POST', {});
    $('#checkpoint-dialog').close(); notify('Git checkpoint created: ' + result.commit.slice(0, 10)); await refresh();
  } catch (err) { $('.form-error', $('#checkpoint-dialog')).textContent = err.message; }
  finally { event.target.disabled = false; }
};
$('#discuss-form').onsubmit = async event => {
  event.preventDefault(); const form = event.target; const submit = $('button[type=submit]', form);
  submit.disabled = true; $('.form-error', form).textContent = '';
  try {
    await api(`${projectPath()}/tasks/${openTask}/discuss`, 'POST', {message: $('#discussion-message').value});
    $('#discussion-message').value = ''; await refresh();
  } catch (err) { $('.form-error', form).textContent = err.message; }
  finally { submit.disabled = false; }
};
async function poll() { await refresh(); setTimeout(poll, 5000); }
poll();
