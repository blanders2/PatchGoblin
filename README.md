# PatchGoblin

A small Flask web app for queueing up AI work on your projects. Write tasks, get an
AI (Claude Code, Codex, or any OpenAI-compatible API such as OpenAI, OpenRouter or
Ollama) to help plan them, then queue them for the
AI to implement. Projects can be local directories or directories on SSH hosts, and
every completed task is committed to the project's own git repository.

## Run

Requires [uv](https://docs.astral.sh/uv/) and git (uv provides Python 3.10+ if needed).

```sh
uv sync
uv run run.py
```

`uv sync` creates `.venv` from `pyproject.toml` / `uv.lock`.

Then open <http://127.0.0.1:5050>. Set `PATCHGOBLIN_PORT` to change the port and
`PATCHGOBLIN_DATA` to change where the app keeps its project list and settings
(default `./data`).

## How it works

**Projects.** Add a project by giving it a directory, either on this computer or
on an SSH host. Type the path or use **Browse…** to pick a folder. The browser lists
folders on whichever machine the project is on (drives on Windows, the remote file
system over SSH), and you can add a new subfolder name to start a fresh project. If the directory doesn't exist it's created. If it isn't already the
root of a git repository, `git init` is run there (with a basic `.gitignore`) and an
initial commit is made. Existing repositories are left as they are.

**Tasks** are stored in `.patchgoblin/tasks.json` inside the project directory (on the
remote host for SSH projects), so the task list and plans live with the code and are
committed along with it.

**States:**

| State | Meaning |
| --- | --- |
| Unplanned | New task. Edit it, ask the AI to plan it, or mark it planned yourself. |
| Planning… | The AI is investigating the project (read-only) and writing a plan. |
| Drafted | AI plan has open questions; answer them (or remove them and click Mark planned) to reach Planned. A planned task with hand-added questions can be moved back with Move to drafted. |
| Planned | Has a plan (AI-drafted or yours). You can edit it, refine it with AI feedback, or queue it. |
| Queued | Waiting for the AI to implement it. |
| Running… | The AI is working in the project directory. Live output is shown in the task panel. |
| Done | Finished and committed. You can reopen it. |
| Failed | The run failed or was cancelled. You can re-queue it, replan it, or mark it planned. |

Planning and implementation both run **in the project's directory, on the project's
host**. Each project runs one task at a time, in queue order. Different projects run in
parallel. Planning jobs start straight away because they don't change files.

**Plan limit.** By default there's no limit on how many planning jobs a project runs
at once, and each job is a separate AI process. If batch planning hits your provider's
rate limits, set **Plan limit** in the project bar. Extra tasks then show as Planning…
and wait for a free slot. Leave it blank for unlimited. Lowering the limit doesn't stop
jobs that are already running.

**AI titles.** When the AI plans a task (including replanning), it also suggests a
concise title, which replaces the task's title. The old title is kept in the task's
history. This is on by default. Untick **AI titles** in the project bar to keep your
own titles. Marking a task planned by hand never changes its title.

**Batch actions.** Tick the checkbox on each card you want (Shift+click selects a
range, Space toggles the focused card, Esc clears). A toolbar then shows the actions
that apply to the ticked tasks: Plan with AI, Mark planned, Move to drafted, Queue for AI, Remove from
queue, Back to unplanned, Reopen, Cancel, Set AI and Delete. The same rules apply as for
a single task. Tasks that don't qualify are skipped and listed in the result message,
and the rest still go through. Batch-queued tasks run in id order. Selections stay
within the current tab.

**Git.** Before a run, any uncommitted changes you made are committed as a
`checkpoint before task #N`, so the AI's commit contains only its own work. After a
successful run, everything is committed as `PatchGoblin: task #N <title>` with the
AI's summary. Nothing is pushed unless you sync (see below). The commit uses your git
identity if it's set, otherwise `PatchGoblin <patchgoblin@localhost>`. Failed runs
leave their changes uncommitted so you can inspect them. The **Commits** button shows
recent history.

**Remote sync.** The **Sync** button sets the project's `origin` URL and syncs with it.
A sync commits any uncommitted changes (including `tasks.json`) as `checkpoint before
sync`, fetches `origin`, brings in its commits for the current branch, then pushes
(`push -u`, so the branch tracks `origin`). Choose **Fast-forward only** (the default;
diverged history is refused) or **Rebase** (your commits are replayed on top of
origin's). No merge commits are ever made, and a conflicting rebase is aborted, so the
repository is never left mid-merge. Sync is refused while any AI job or chat is running
in the project. Tick **Auto-sync** on a project (off by default) to sync after every
successful task commit; if that sync fails, the task stays done and the failure is
written to its history. Credentials come from your own git setup (SSH keys and agent,
or a credential manager); prompts are turned off, so missing credentials fail at once
instead of hanging. For SSH projects the remote is reached from the SSH host. A URL
with an embedded token is stored in `.git/config` and shown in the dialog.

**Chat.** The **Chat** button opens a conversation with the project's AI, running in
the project directory with the same read-only access as planning (the planning command
and timeout are used). Conversations are kept in memory until PatchGoblin restarts.

## AI providers

Choose a default AI for each project. Individual tasks can override the default.
Commands can be edited under **Settings**.

### Models

Each project has a **Planning Model** (used to plan tasks), a **Coding Model** (used to
run them) and an optional **Chat model** (picked in the chat panel; blank means the
planning model). Each task can override its planning and coding models in the task
drawer, or for many tasks at once with **Set models** in the batch bar. **Settings** has
a default planning and coding model for Claude Code, Codex and each endpoint.

The model for a job is the first one set of:

1. the task's own planning/coding model;
2. the project's model (for chat: chat model, then planning model), but only when the
   task uses the project's AI;
3. the Settings default for that AI (an endpoint's coding default falls back to its
   planning default);
4. nothing, so the CLI or endpoint picks its own default.

A blank project Coding Model therefore means "the global default", not "same as
planning". When a task's AI changes, its model overrides are cleared (the task drawer
keeps any that the new AI also lists).

Projects saved before planning and coding models existed had a single `model`. On first
start, PatchGoblin copies it into both the planning and coding model and removes the old
field, after saving a one-time backup to `projects.json.bak` in its data directory.

- **Claude Code** (`claude`) runs in print mode with the prompt on stdin. Planning
  only allows read/search tools. Runs use `--permission-mode acceptEdits` and allow
  `Read, Glob, Grep, Edit, Write, Bash`, so the agent can run tests and builds without
  prompting. Tighten the run command in Settings if you want less.
- **Codex** (`codex exec`) uses the `read-only` sandbox for planning and
  `workspace-write` for runs.
- **OpenAI-compatible endpoints** run a tool-calling agent against a Chat Completions
  API (see below). The agent can list, read and search files, and during runs it can
  also write files. Its tools run through the project's host, so remote projects don't
  need a key on the remote machine. It can't write to `.git/` or `.patchgoblin/`, or to
  paths outside the project. Shell commands are off by default. Turning them on for an
  endpoint runs them unsandboxed.

The CLIs must be installed and logged in on whichever machine hosts the project.

### OpenAI-compatible endpoints

Under **Settings → OpenAI-compatible APIs** you can add any number of named endpoints.
Each one shows up by name in every AI dropdown. For each endpoint you set:

- a **base URL** (the part before `/chat/completions`);
- an **API key**, either saved in Settings or read from an **env var** in PatchGoblin's
  environment. A saved key takes priority. Saved keys are stored in plain text in
  PatchGoblin's own `settings.json` (in its data directory, never in a project) and are
  never sent back to the browser. Leave both empty for keyless local servers;
- a default planning model and coding model, optional model suggestions, extra HTTP headers, max agent steps and
  whether shell commands are allowed.

The model dropdown combines your suggestions with the server's `/models` list (cached
for 10 minutes; ↻ refreshes it). The agent needs tool calling, so when the server says
which models support tools (OpenRouter does), only those are listed. **Test** checks the
URL and key by listing models.

Examples:

| Name | Base URL | Key |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | env var `OPENAI_API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | env var `OPENROUTER_API_KEY` (models like `anthropic/claude-sonnet-5`) |
| Ollama / LM Studio | `http://localhost:11434/v1` / `http://localhost:1234/v1` | none |

Projects and tasks store the endpoint's id, so renaming an endpoint is safe. Removing
one that is still in use leaves those projects and tasks showing "(missing)"; their jobs
fail with a "not configured" error until you pick another AI. Settings from older
versions (a single "OpenAI API" block) become an endpoint with id `openai`.

## SSH projects

- PatchGoblin uses your system `ssh` client, so `~/.ssh/config` aliases, keys and agents
  work as usual. Connections use `BatchMode=yes`, so key-based login is required
  (password prompts aren't supported). Connect once in a terminal first to accept the
  host key.
- The remote host must have a POSIX shell and git. Paths must be absolute
  (e.g. `/home/me/project`).
- Agent commands run in a login shell (`$SHELL -lc`), so PATH changes from your profile
  (npm, nvm, `~/.local/bin`) apply.
- Cancelling a remote run closes the SSH session. Most CLIs exit when that happens, but
  check the host if one doesn't.

## Safety notes

- The server only listens on 127.0.0.1 and rejects requests with a foreign `Host`.
  Write requests also need a custom header that other websites can't send. There is
  no login, so don't expose it on a network.
- AI agents act with your user account's permissions in the project directory. Review
  commits (`git show`) before relying on them.
- If PatchGoblin is restarted during a job, that task is marked interrupted: planning
  goes back to its previous state and a run is marked failed. Queued tasks resume.

## Tests

```sh
uv run python -m unittest discover -s tests -v
```

The tests use real temporary directories, git repositories and subprocesses. A fake
agent script stands in for the CLIs, and the OpenAI HTTP call and SSH hop are mocked,
so no AI or network access is needed.
