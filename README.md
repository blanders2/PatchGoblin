# PatchGoblin

A small Flask web app for queueing up AI work on your projects. Write tasks, get an
AI (Claude Code, Codex, opencode, Cline, or any OpenAI-compatible API such as OpenAI, OpenRouter or
Ollama) to help plan them, then queue them for the
AI to implement. Projects can be local directories or directories on SSH hosts.
Git tracking is opt-in per project (off by default for new projects); turn it on
to have every completed task committed to the project's own git repository.

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
system over SSH), and you can add a new subfolder name to start a fresh project. If the directory doesn't exist it's created.

**Git tracking** is off by default for a newly added project; tick **Track with git** in
the Add project dialog, or turn it on later from Project settings → Git. Turning it on
runs `git init` there (with a basic `.gitignore`) and an initial commit, unless the
directory is already the root of a git repository, in which case it's left as it is
(and, if it has no commits yet, gets an initial commit). Turning tracking off again only
stops PatchGoblin from running further git commands there; `.git` and its history are
kept, and turning tracking back on never recreates the initial commit. Projects
registered with an older version of PatchGoblin (which always tracked) keep git tracking
on. A dot next to
each project in the sidebar shows whether its directory can be reached right now
(green), can't be (red; hover for the reason), or is still being checked (grey). It
is re-checked every minute and when you return to the tab.

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
| Needs review | The AI run finished and its work is committed. Approve it, send it back to the AI with feedback, or reopen it. |
| Done | Approved by you. You can send it back to the AI with feedback, or reopen it. |
| Failed | The run failed or was cancelled. You can re-queue it, replan it, or mark it planned. |

Planning and implementation both run **in the project's directory, on the project's
host**. Each project runs one task at a time, in queue order. Different projects run in
parallel. Planning jobs start straight away because they don't change files.

**Project settings.** The **Project settings** button in the project header opens a
full-page view in place of the board, with every per-project option: **General** (name,
location and path), **AI** (provider and the planning, coding and chat models),
**Planning** (plan limit, AI titles, plan trust), **Automation** (Auto-plan, Auto-queue), **Git**
(a **Turn on git tracking** button while tracking is off; once on, the `origin` URL,
auto-sync, sync mode, and a **Stop tracking** checkbox that keeps `.git`) and a **Danger
zone** to remove the project (its files, `tasks.json` and git history, if any, are kept).
Changes apply together when you click **Save**; **← Back to board**, Cancel or
Esc leave without saving (asking first if you edited anything). Settings are stored in
PatchGoblin's local `projects.json`.

**Plan limit.** By default there's no limit on how many planning jobs a project runs
at once, and each job is a separate AI process. If batch planning hits your provider's
rate limits, set **Plan limit** in Project settings. Extra tasks then show as Planning…
and wait for a free slot. Leave it blank for unlimited. Lowering the limit doesn't stop
jobs that are already running.

**Review.** A successful run is committed (and auto-synced, if that's on) as soon as it
finishes, then waits in the **Review** tab for you to check it. The drawer lists the files
its commit changed. Unreviewed work is therefore already on your branch, and pushed if
auto-sync is on.
- **Approve → Finished** moves the task to Finished (Done). It also works as a batch action.
- **Send back to AI** (from Review or Finished) needs feedback. The AI re-plans with it as
  a follow-up on top of the existing commit, rather than redoing the work. Queue the new
  plan as usual; the follow-up run is committed on top and lands in Review again.
- **Reopen** returns the task to Planned without asking the AI.

**AI titles.** When the AI plans a task (including replanning), it also suggests a
concise title, which replaces the task's title. The old title is kept in the task's
history. This is on by default. Untick the AI titles option in Project settings to keep your
own titles. Marking a task planned by hand never changes its title.

**Plan trust.** Sets how much the AI may assume when planning. **Low** asks about
anything ambiguous instead of guessing; **High** makes its own calls and asks only about
decisions that are costly or hard to undo; **Normal** (the default) is in between. At Low
and High the plan lists the AI's guesses under **## Assumptions** so you can check them.
Set it in Project settings; a task can override it in its drawer (**Project default**
uses the project's level).

**Automation.** Two modes move tasks along without clicks. Both are off by default.
- **Auto-plan** starts AI planning as soon as a task is created.
- **Auto-queue** queues a task the first time it goes from Unplanned to Planned, through an
  AI plan or **Mark planned**. A drafted plan waits for your answers, and a re-plan after
  answering questions, Mark planned from Drafted, or a task you took out of the queue is
  not auto-queued.

Set the defaults in **Settings → Automation**. Each project can override them in Project
settings with **On**, **Off** or **Default** (follow Settings). Turning a mode on,
either way, also applies it right away to the existing tasks it covers: Auto-plan starts
planning every Unplanned task (the plan limit still applies; with no limit, that is one AI
job per task at once), and Auto-queue queues every Planned task. Turning it on globally does
this in every project that uses the default. Each automatic step is written to the task's
history ("Auto-planned", "Auto-queued"). With both modes on, a task whose plan has no open
questions goes from creation to an AI run and a commit without you reviewing the plan.

**Batch actions.** Tick the checkbox on each card you want (Shift+click selects a
range, Space toggles the focused card, Esc clears). A toolbar then shows the actions
that apply to the ticked tasks: Plan with AI, Mark planned, Move to drafted, Queue for AI, Remove from
queue, Back to unplanned, Approve, Reopen, Cancel, Set AI and Delete. The same rules apply as for
a single task. Tasks that don't qualify are skipped and listed in the result message,
and the rest still go through. Batch-queued tasks run in id order. Selections stay
within the current tab.

**Git.** While a project has git tracking on: before a run, any uncommitted changes you
made are committed as a `checkpoint before task #N`, so the AI's commit contains only
its own work. After a successful run, everything is committed as `PatchGoblin: task #N
<title>` with the AI's summary. Nothing is pushed unless you sync (see below). The
commit uses your git identity if it's set, otherwise `PatchGoblin <patchgoblin@localhost>`.
Failed runs leave their changes uncommitted so you can inspect them. The **Commits**
button shows recent history. While tracking is off, runs go straight to Needs review
with nothing committed, and the **Commits** and **Sync** buttons are hidden.

**Remote sync.** Requires git tracking. Set the project's `origin` URL and sync mode in Project settings; the
**Sync** button then syncs with it, always using the saved mode.
A sync commits any uncommitted changes (including `tasks.json`) as `checkpoint before
sync`, fetches `origin`, brings in its commits for the current branch, then pushes
(`push -u`, so the branch tracks `origin`). The mode is **Fast-forward only** (the default;
diverged history is refused) or **Rebase** (your commits are replayed on top of
origin's). No merge commits are ever made, and a conflicting rebase is aborted, so the
repository is never left mid-merge. Sync is refused while any AI job or chat is running
in the project. Tick **Auto-sync** in Project settings (off by default) to sync after every
successful task commit; if that sync fails, the task still goes to Review and the failure is
written to its history. Credentials come from your own git setup (SSH keys and agent,
or a credential manager); prompts are turned off, so missing credentials fail at once
instead of hanging. For SSH projects the remote is reached from the SSH host. A URL
with an embedded token is stored in `.git/config` and shown in Project settings and the Sync dialog.

**Chat.** The **Chat** button opens a conversation with the project's AI, running in
the project directory with the same read-only access as planning (the planning command
and timeout are used). Conversations are kept in memory until PatchGoblin restarts.

## AI providers

Choose a default AI for each project. Individual tasks can override the default.
Commands can be edited under **Settings**.

### Models

Each project has a **Planning Model** (used to plan tasks), a **Coding Model** (used to
run them) and an optional **Chat model** (blank means the planning model), all set in
Project settings; the chat panel's model picker is a quick override that saves at once. Each task can override its planning and coding models in the task
drawer, or for many tasks at once with **Set models** in the batch bar. **Settings** has
a default planning and coding model for Claude Code, Codex, opencode, Cline and each endpoint.
opencode's model dropdown lists the models in the project's opencode config (or, if it
names none, `opencode models`).

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
- **opencode** (`opencode run`) runs a named opencode agent (see below).
- **Cline** (`cline`) plans in plan mode (`-p`) and runs in act mode, both with
  `--auto-approve true` because no one is there to approve tool calls (see below).
- **OpenAI-compatible endpoints** run a tool-calling agent against a Chat Completions
  API (see below). The agent can list, read and search files, and during runs it can
  also write files. Its tools run through the project's host, so remote projects don't
  need a key on the remote machine. It can't write to `.git/` or `.patchgoblin/`, or to
  paths outside the project. Shell commands are off by default. Turning them on for an
  endpoint runs them unsandboxed.

The CLIs must be installed and logged in on whichever machine hosts the project.

### opencode

Log in with `opencode auth login` on the machine that hosts the project. PatchGoblin
runs `opencode run --agent {agent}` with the prompt on stdin. `{agent}` is the
**planning agent** for planning and chat, or the **run agent** for runs. By default these
are opencode's built-in `plan` and `build` agents; you can change them under
**Settings → opencode**. Model names are `provider/model`, e.g. `anthropic/claude-sonnet-5`.

Safety checks (both are on by default):

- **Refuse to run if a custom agent isn't defined.** A name other than a built-in agent must
  be defined in the project's or your `opencode.json`, or as an `.opencode/agent/<name>.md`
  (or `~/.config/opencode/agent/<name>.md`) file. Otherwise the job fails before opencode
  starts, rather than letting opencode fall back to a different agent.
- **Fail planning if the plan agent edits files.** PatchGoblin compares `git status` before
  and after planning (and chat). If files changed, the plan fails and names them. The
  changes are left in place for you to review. While a task run is active in the same
  project, changes can't be traced to the plan, so they are only logged.

For planning, PatchGoblin also passes inline config (`OPENCODE_CONFIG_CONTENT`) that
denies the planning agent `edit` and `bash`. It is still worth setting this in your own
config, so the plan agent is read-only however opencode is started:

```jsonc
// opencode.json
{
  "agent": {
    "plan": { "permission": { "edit": "deny", "bash": "deny", "webfetch": "allow" } }
  }
}
```

or in `.opencode/agent/plan.md` front matter:

```markdown
---
permission:
  edit: deny
  bash: deny
  webfetch: allow
---
```

Permissions set to `ask` have no one to answer them under `opencode run`, so a job may
wait until the plan or run timeout. The `build` agent has unsandboxed shell access, just
like the Claude Code run command.

An OpenAI-compatible endpoint saved with the id `opencode` (from before opencode was a
CLI provider) is renamed to `opencode-api` on first start. Projects and tasks that used it
are updated to match.

### Cline

Install and log in to the Cline CLI on the machine that hosts the project
(`npm i -g cline`, then `cline auth`). PatchGoblin runs `cline -p --auto-approve true`
for planning and chat and `cline --auto-approve true` for runs, with the prompt on stdin.
Model overrides are passed with `-m` and are ids for the provider you set up with
`cline auth`; there is no built-in model list, so pick **Custom…** to enter one.

Cline prints its thinking and tool calls along with its answer. PatchGoblin keeps all of
it in the job log, but only Cline's final reply becomes the plan or chat answer.

Cline's plan mode blocks its file-editing tools, but it can still run shell commands,
which could change files. **Fail planning if Cline changes files** (on by default) runs
the same `git status` check as opencode's. Act mode has unsandboxed shell access, just
like the Claude Code run command.

An endpoint saved with the id `cline` is renamed to `cline-api` on first start, in the same
way as `opencode` above.

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
