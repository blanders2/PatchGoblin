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

**Tray icon.** To keep PatchGoblin running in the background instead, run `uv run tray.pyw`
(or point a shortcut at `.venv\Scripts\pythonw.exe tray.pyw`, for example in your Startup
folder). It runs without a console window and adds a goblin icon to the system tray. Click it
to open PatchGoblin in your browser, or right-click and choose **Quit** to stop the server.
**Restart** stops the server and starts it again, for example to pick up code or settings
changes; it interrupts running AI jobs, after asking first. The
log goes to `patchgoblin.log` in the data directory. If PatchGoblin is already running on the
port, launching it again just opens the browser.

**Linux install.** `python3 install_linux.py` runs `uv sync` and installs, for your user only (no
root), a goblin icon, an app-menu launcher for `tray.pyw` and, optionally, a login autostart
entry and a systemd user service. Pass `--service/--no-service` and `--autostart/--no-autostart`
(or `--yes` for the defaults: autostart on, service off) to skip the questions. With the service,
the server runs as `patchgoblin.service` and the tray only attaches to it: Restart runs
`systemctl --user restart` and Quit stops the service. Service logs: `journalctl --user -u
patchgoblin`; the unit gets your `PATH` at install time so the AI CLIs are found, and
`loginctl enable-linger $USER` starts it at boot without a login. GNOME and COSMIC only show
AppIndicator tray icons, so install PyGObject first (`sudo apt install python3-gi
gir1.2-ayatanaappindicator3-0.1`); the installer then builds `.venv` with system site packages.
Restart prompts use zenity or kdialog. `python3 install_linux.py --uninstall` removes
everything it wrote and leaves `data/` and `.venv` alone.

## How it works

**Theme.** Pick System, Light, Dark, Midnight or Sepia from the Theme menu in the top bar; the choice is remembered per browser.

**Projects.** Add a project by giving it a directory, either on this computer or
on an SSH host. Type the path or use **Browse…** to pick a folder. The browser lists
folders on whichever machine the project is on (drives on Windows, the remote file
system over SSH), and you can add a new subfolder name to start a fresh project. If the directory doesn't exist it's created.

**Git tracking** is off by default for a newly added project; tick **Track with git** in
the Add project dialog, or turn it on later from Project settings → Git. Turning it on commits the whole directory, so you must confirm it contains no secrets (API keys, `.env` files, credentials) first. It
runs `git init` there (with a basic `.gitignore` taken from Settings → New repositories, which ignores
`.patchgoblin/` by default; remove that line to commit and sync tasks, and note that an
existing `.gitignore` is never overwritten) and an initial commit, unless the
directory is already the root of a git repository, in which case it's left as it is
(and, if it has no commits yet, gets an initial commit). Turning tracking off again only
stops PatchGoblin from running further git commands there; `.git` and its history are
kept, and turning tracking back on never recreates the initial commit. Projects
registered with an older version of PatchGoblin (which always tracked) keep git tracking
on. A dot next to
each project in the sidebar shows whether its directory can be reached right now
(green), can't be (red; hover for the reason), or is still being checked (grey). It
is re-checked every minute and when you return to the tab.

**Terminal.** The project's **Terminal** button switches to the project's terminal, or opens a
shell in the project directory (over `ssh` for SSH projects) if it doesn't have one yet. The **+**
next to it always opens another. **Settings → Terminal** chooses where they open:
- **Windows Terminal** (the default). PatchGoblin finds the project's tab by a tag in the shell's
  command line (`PATCHGOBLIN_TAB`; SSH tabs pass it as `ssh -o SetEnv=…`, which servers that don't
  accept it ignore). Windows Terminal can't list its tabs, so the tab is picked by the order the
  tagged shells were opened. If you've dragged tabs around, or opened other tabs by hand in that
  window, it can focus the wrong one.
- **The browser:** the Terminals plugin (see below), which must be turned on.

With Windows Terminal installed, every terminal PatchGoblin opens there is a
new tab in one window named **PatchGoblin**, created the first time. Each tab is named after the
project (with the SSH host for remote ones), and the shell can't rename it. Each tab also gets the
project's own colour, picked from its place in the project list so that neighbouring projects
look clearly different. A project keeps its colour unless a project above it is removed. Without Windows
Terminal, a PowerShell window opens with the project name as its title. On macOS the
system terminal opens a new window.

On Linux, **Settings → Terminal → Terminal tabs on Linux** picks a backend. Tabs are matched on an
exact `PATCHGOBLIN_TAB=<project id>` value in the shell's environment, so dragging tabs around
can't focus the wrong one.
- **kitty** (the main option): each project is a tab named after it and coloured like on Windows
  (`kitty @ launch`, `set-tab-color`, `focus-tab --match env:…`). This needs kitty's remote
  control: add `allow_remote_control socket-only` and `listen_on unix:/tmp/kitty` to
  `~/.config/kitty/kitty.conf` and restart kitty (or enter the socket under *kitty socket*).
  If no kitty is listening, PatchGoblin starts its own with remote control turned on by flags, so
  `kitty.conf` needs no edits.
- **tmux** (works in any emulator, and over SSH): one session named `PatchGoblin` with a window per
  project, named and coloured in the status bar. If nobody is attached, a terminal window running
  `tmux attach` opens. Needs tmux 3.0 or newer.
- **WezTerm**: named tabs (no colour), found through `wezterm cli`.
- **A new terminal window each time**: the old behaviour.

*Automatic* uses a reachable kitty, else tmux, else a kitty PatchGoblin starts, else a plain window.
Commands are passed to the terminal as argument lists, so fish needs no special handling; kitty
keeps the tab's title and tmux has window renaming turned off, so `fish_title` can't overwrite
it. SSH projects work with a fish login shell on the remote side too: every remote script runs
under `sh -c`.

*Bringing the window forward:* switching tabs works everywhere, but on **Wayland** the
compositor may not let an app raise a window, so the window may only flash "needs attention". On
**X11**, PatchGoblin raises it with `xdotool` or `wmctrl` when one is installed.

**Live activity.** While the AI plans, runs or answers in chat, PatchGoblin shows a live feed of
what it is doing: files it reads and edits, commands it runs, and its own messages, plus a one-line
"now: …" status on the task card. Claude Code runs with `--output-format stream-json --verbose` to
provide this (the final answer is taken from its closing result), as do endpoint agents. If you
customize the Claude commands, keep those two flags to get the feed; a text-mode command still
works but only shows the raw output. Codex, opencode and Cline show their raw output on the
**Raw output** tab. Activity is kept in memory with the job; nothing is written to the project.

**Tasks** are stored in `.patchgoblin/tasks.json` inside the project directory (on the
remote host for SSH projects), so the task list and plans live with the code and are
committed along with it.

**Images** can be pasted (or dropped) into a task's description, the new-task Details field, the
AI feedback box and chat. Each image (PNG, JPEG, GIF or WebP, up to 8 MB) is saved as
`.patchgoblin/attachments/<id>.<ext>` in the project (on the remote host for SSH projects), so it
is committed along with `.patchgoblin/`, and a Markdown reference is inserted into the text. AI
prompts list the referenced files so the agent can open them. OpenAI-compatible endpoints
also receive the images inline, which needs a vision-capable model. Attachments are not deleted
automatically.

**States:**

| State | Meaning |
| --- | --- |
| Unplanned | New task. Edit it, ask the AI to plan it, or mark it planned yourself. |
| Planning… | The AI is investigating the project (read-only) and writing a plan. Shown in the Plan column. |
| Plan | AI plan has open questions; answer them (or remove them and click Mark planned) to reach Planned. A planned task with hand-added questions can be moved back with Move to Plan. |
| Planned | Has a plan (AI-drafted or yours). You can edit it, refine it with AI feedback, or queue it. |
| Queued | Waiting for the AI to implement it. |
| Running… | The AI is working in the project directory. Live output is shown in the task panel. |
| Needs review | The AI run finished and its work is committed. Approve it, send it back to the AI with feedback, or reopen it. |
| Done | Approved by you. You can send it back to the AI with feedback, or reopen it. |
| Failed | The run failed or was cancelled. You can re-queue it, replan it, or mark it planned. Failed tasks sit in their own **Failed** tab, which only shows while there is a failed task. |

**Pausing.** Pause is a flag, not a state: a paused task stays where it is, highlighted with a
Paused chip, and Auto-plan, Auto-queue and the run queue skip it until you resume it. A paused
queued task keeps its place in the queue. While paused, only Resume, editing and Delete work.

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
- **Approve → Finished** moves the task to Finished (Done). It also works as a batch action,
  and each Review card has an **Approve** button that does the same in one click.
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

**Automation.** Three modes move tasks along without clicks. Auto-plan and Auto-queue are
off by default; Auto-promote is on by default. Queued tasks always run.
- **Auto-plan** starts AI planning as soon as a task is created.
- **Auto-queue** queues a task the first time it goes from Unplanned to Planned, through an
  AI plan or **Mark planned**. A plan with open questions waits for your answers, and a re-plan after
  answering questions, Mark planned from Plan, or a task you took out of the queue is
  not auto-queued.
- **Auto-promote** moves a task in Plan to Planned as soon as its plan has no open
  questions, after an AI re-plan with your answers or a hand edit that removes the last
  question. Turn it off and such tasks wait in Plan until you click **Mark planned**.
  Turning it on promotes every question-free Plan task now.

The board shows a small arrow between the tabs for each mode: after Unplanned (Auto-plan),
after Plan (Auto-promote) and after Planned (Auto-queue). A coloured arrow means the mode is on
for the current project; a red line through it means it is off. Click it to turn the mode on or off for that project; this sets
a project override, and turning on Auto-plan, Auto-promote or Auto-queue applies it to existing tasks
right away, like a Settings save.

Set the defaults in **Settings → Automation**. Each project can override them in Project
settings with **On**, **Off** or **Default** (follow Settings). Turning a mode on,
either way, also applies it right away to the existing tasks it covers: Auto-plan starts
planning every Unplanned task (the plan limit still applies; with no limit, that is one AI
job per task at once), and Auto-queue queues every Planned task. Turning it on globally does
this in every project that uses the default. Each automatic step is written to the task's
history ("Auto-planned", "Auto-queued"). With both modes on, a task whose plan has no open
questions goes from creation to an AI run and a commit without you reviewing the plan.

**Unplanned shortcuts.** Each Unplanned card has **Plan with AI** and **Mark planned**
buttons that act in one click without opening the task. Below the list, **Plan all (N)** and
**Mark all planned (N)** act on every Unplanned task in the tab, whatever is ticked. Plan all
asks for optional feedback shared by every task (Cancel aborts), and Mark all planned asks
for confirmation when Auto-queue is on. Paused tasks get no buttons and aren't counted.

**Batch actions.** Tick the checkbox on each card you want (Shift+click selects a
range, Space toggles the focused card, Esc clears). A toolbar then shows the actions
that apply to the ticked tasks: Plan with AI, Mark planned, Move to Plan, Queue for AI, Remove from
queue, Back to unplanned, Approve, Reopen, Pause, Resume, Cancel, Set AI and Delete. The same rules apply as for
a single task. Tasks that don't qualify are skipped and listed in the result message,
and the rest still go through. Batch-queued tasks run in id order. Selections stay
within the current tab. Paused tasks only qualify for Resume, Set AI and Delete.

**Git.** While a project has git tracking on: before a run, any uncommitted changes you
made are committed as a `checkpoint before task #N`, so the AI's commit contains only
its own work. After a successful run, everything is committed as `PatchGoblin: task #N
<title>` with the AI's summary. Nothing is pushed unless you sync (see below). The
commit uses your git identity if it's set, otherwise `PatchGoblin <patchgoblin@localhost>`.
Failed runs leave their changes uncommitted so you can inspect them. The **Commits**
button shows recent history. While tracking is off, runs go straight to Needs review
with nothing committed, and the **Commits** and **Sync** buttons are hidden.

**SVN.** A project that is already an SVN working copy can use **Use existing SVN
working copy** in Project settings → Version control (git and SVN tracking are mutually
exclusive; PatchGoblin never runs `svn checkout` for you). Nothing is committed
automatically, because an SVN commit is published to the server at once. Instead each
successful run records the files it changed and the AI's summary on the task, which then
shows as pending check-in. The **Check in** button opens a dialog with those tasks, the
working copy's current `svn status`, and an editable commit message built from the tasks.
Check in adds new files, removes missing ones and runs `svn commit`, then stamps the
revision on each task. The commit covers the whole working copy (including `tasks.json` and
manual edits), and edits you make while a task runs are indistinguishable from the AI's.
Commands run with `--non-interactive`, so your own SVN credentials must already be cached,
and an out-of-date working copy fails with SVN's error (update it yourself). `svn` and
`git` must both be installed on the project's host. **Commits** shows `svn log`; Sync is
git-only.

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

Each CLI's dropdown combines its built-in suggestions, the **Model suggestions** list in
Settings (space or comma separated) and its default models. A model name you save for
Claude Code, Codex or Cline that isn't a suggestion yet is added to that list
automatically (remove it in Settings). Cline's dropdown also lists the models in the
installed Cline's bundled catalog for the provider chosen with `cline auth`, read on the
project's host with `node`; if that fails, the saved suggestions are used and the
dropdown's tooltip shows the error.

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
`cline auth`; pick **Custom…** to enter one not in the list.

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
- Windows hosts work too (OpenSSH Server with key auth). The remote OS is detected
  automatically; the server's default shell can be cmd.exe or PowerShell because every
  command is sent as `powershell.exe -EncodedCommand`, so Windows PowerShell 5.1+ must be
  present. Use drive paths (`C:\Users\me\project`); git and the AI CLIs must be on the
  user's PATH. Long arguments are limited by cmd.exe's ~8,000 character command line, so
  prompts must go over stdin.
- Cancelling a remote run closes the SSH session. Most CLIs exit when that happens, but
  check the host if one doesn't.

## Server Manager

The **Servers** button (the stacked-servers icon at the bottom of the activity bar) opens one
list of saved servers shared by PatchGoblin and its plugins, grouped by type. **SSH host** is
built in; the LLM API console adds **LLM API**. Each server has a name and the fields of its
type; some types have a **Test connection** button. Secrets (API keys) are kept in
`settings.json` under `servers`, like the OpenAI-compatible endpoints' keys, and are never sent
back to the browser: leave a secret blank when editing to keep the saved one, or tick the
remove box to clear it. Servers of a plugin that is switched off are kept but shown as
unavailable (they can be deleted, not edited). The "OpenAI-compatible APIs" endpoints in
Settings are separate and unchanged.

In the **Add project** dialog an SSH project can pick a **Saved server**; the project then stays
linked to it, so editing the server's host or port updates the project, and a server that
projects use can't be deleted. Typing a host by hand still works. Servers that the SSH tools and
the LLM console used to keep themselves are moved into this list the first time the new version
starts (an id that clashed gets a `-2` suffix); the old copies are left in `plugin_data`.

A plugin adds a server type by overriding `Plugin.server_types()` to return
`patchgoblin.servers.ServerType` objects (an id, a label, `Field` specs and optional `clean` and
`test` hooks); the dialog builds its form from the field specs, so no UI code is needed. A plugin
reads and writes servers through `self.ctx.servers` (`list(type)`, `get(id)` with secrets,
`save(spec)`, `delete(id)`), and its script can call `PG.servers.list(type)` and
`PG.servers.open(type, prefill)` and listen for `pg:servers-changed`.

## The activity bar and plugins

The narrow icon bar on the far left switches the sidebar and the main area between
**Projects** and each developer tool (plugin), such as **SSH tools** or **Todo**. Switching only hides the
project screens. Running tasks, the open drawer or chat and unsaved Project settings all keep
going, and switching back returns you to exactly where you were. Click the selected icon again
to collapse the sidebar and click it once more to bring it back. Arrow keys move between the icons. A plugin can show a badge on its
icon (SSH tools counts running tunnels). The last view is remembered.

**Plugins** (the puzzle icon at the bottom of the bar) lists every plugin found, with a
switch for each. Turning one off stops it (SSH tools stops its tunnels), removes its icon and
makes its API answer 404. The page reloads to apply the change. Plugins that fail to load are listed with
the error and don't affect the rest of PatchGoblin.

Built-in plugins live in `patchgoblin/plugins/`. To add your own, drop its folder into
`<data dir>/plugins/` (or a folder listed in `PATCHGOBLIN_PLUGINS`, separated by `;` on Windows
and `:` elsewhere) and restart. To remove it, delete the folder. A plugin folder looks like this:

```
mytool/
  __init__.py           PLUGIN = a patchgoblin.pluginkit.Plugin subclass
  templates/sidebar.html   optional: its sidebar pane
  templates/view.html      optional: its main view
  static/plugin.js         optional: loaded on the page
  static/plugin.css        optional: loaded on the page
```

```python
from flask import jsonify
from patchgoblin.pluginkit import Plugin

class MyTool(Plugin):
    id = "mytool"            # API at /api/mytool/…, files at /plugins/mytool/…
    name = "My tool"
    description = "What it does."
    icon = '<circle cx="12" cy="12" r="9"/>'  # inner markup of a 24×24 stroke SVG
    order = 50               # position on the bar

    def routes(self, bp):    # a Flask blueprint mounted at /api/mytool
        @bp.get("/things")
        def things():
            return jsonify(things=self.ctx.load().get("things", []))

    def start(self): ...     # when enabled (at startup or switched on)
    def stop(self): ...      # when switched off or PatchGoblin quits

PLUGIN = MyTool
```

`self.ctx` gives the plugin `load()`/`save()` for its own saved data (kept in `settings.json`
under `plugin_data.<id>`), plus `registry` (the projects), `settings` and `data_dir`. Templates are
included in the page as `<id>/sidebar.html` and `<id>/view.html`. The page script registers itself
with the `PG` object that the core page provides:

```js
PG.register({
  id: "mytool",
  setup() { /* once, after the page loads: bind events */ },
  activate() { /* the plugin was shown */ },
  deactivate() { /* the plugin was hidden */ },
});
```

`PG` also offers the page helpers (`$`, `$$`, `el`, `api`, `toast`, `showError`, `withBusy`,
`storageGet`/`storageSet`), `PG.setBadge(id, value, title)` and `PG.show(id)`. The core page
fires `pg:project-dialog-open` and `pg:project-added` events on `document` for plugins that
want to extend the project screens (the `server_types()` hook and `pg:servers-changed` event cover servers, see Server Manager). Keep plugin
element ids prefixed with the plugin id, because every enabled plugin shares the page.

## LLM API console

A built-in plugin for testing LLM servers and seeing exactly what goes over the wire. Every
request goes through PatchGoblin, which adds the API key and records the exchange. Keys never
reach the browser and are masked in everything it shows.

- **Servers** are kept in the Server Manager (**Add server**, the sidebar's **+ Add server** or
  **Manage servers…** open it): pick a kind (llama.cpp, llama-swap, vLLM, SGLang, Ollama,
  LM Studio, TGI, KoboldCpp, TabbyAPI, Hermes/Nous, OpenAI, OpenRouter, Anthropic or generic
  OpenAI-compatible) and enter the base URL (a trailing `/v1` is fine). The API key is either
  typed or read from an environment variable; auth style, timeout, TLS checking, extra headers
  and the default model are further fields. **Test connection** checks the URL and key, lists the models and
  guesses the server software without saving anything. **Scan this computer** looks for
  servers on the usual local ports. PatchGoblin's own AI endpoints are offered as suggestions,
  and adding one copies its key on the server side.
- **JSON responses** (Raw HTTP, Probe, model Details, History and the chat Response tab) show as
  tables, with the usual `data`/`models` envelopes unwrapped and streamed lines as a time/event/
  text table. A **Table / Raw** toggle switches to the pretty-printed JSON, and the choice is
  remembered. Anything that isn't JSON is shown raw.
- **Chat** sends a conversation through any of six APIs: OpenAI chat, completions or responses,
  Anthropic messages, Ollama `/api/chat` or llama.cpp `/completion`. The request body is
  rebuilt live on the **Request** tab as you change anything. Tick **Edit raw body** to send
  your own.
  - **Sampling:** temperature, top-p/k, min-p, max tokens, seed, penalties, stop sequences, Ollama
    `num_ctx`, logprobs, JSON output and llama.cpp `cache_prompt`. Each one is mapped to the
    right field name for the chosen API, and a blank field isn't sent.
  - **Thinking:** a thinking on/off switch, effort and budget, sent the way the server expects:
    - `chat_template_kwargs.enable_thinking` (Qwen3, GLM) or `chat_template_kwargs.thinking` (DeepSeek, Hermes 4)
    - OpenRouter's `reasoning` object
    - `reasoning_effort`
    - Anthropic's `thinking`
    - Ollama's `think`

    Reasoning is shown separately from the answer, whether it comes in `reasoning_content`
    or `reasoning` fields or in `<think>` tags.
  - **Extra body JSON** is merged in last, for tools or any server-specific option.
- **Inspector tabs:**
  - **Response:** status and headers.
  - **Stream:** every raw stream line with its arrival time.
  - **Stats:** time to first byte and first token, total time, prompt/completion/reasoning/cached
    tokens, your measured tokens per second and the server's own timings (llama.cpp `timings`,
    Ollama durations).
  - **Logprobs:** tokens shaded by probability.
  - **Copy curl:** the request as a curl command, with `$LLM_API_KEY` in place of the key.
- **Context:** the bar on top shows the model's context size and how much of it the last
  exchange used. The size comes from `/props` (llama.cpp), `/api/show` and `/api/ps`
  (Ollama), `max_model_len` (vLLM), `/get_server_info` (SGLang), LM Studio's model info or
  `/v1/models`. Hover over it to see which one answered. **Count tokens** uses the server's
  tokenizer (`/tokenize`, Anthropic `count_tokens`).
- **Models** lists every model from `/v1/models`, Ollama `/api/tags` and `/api/ps`, llama-swap
  `/running` and LM Studio, with context size and whether it's loaded. **Details** shows the raw
  model info. The model box suggests the same list and picks one for you when it's empty.
- **Probe** GETs about twenty well-known endpoints (`/health`, `/props`, `/slots`, `/metrics`,
  `/api/tags`, `/running`, `/get_server_info`…), shows each status, timing and body, and guesses
  the server software.
- **Raw HTTP** sends any method, path and body to the server, streaming or not.
- **History** keeps the last 100 exchanges in memory (never on disk), with masked headers, the
  full response and timings. It can copy a request as curl or reopen it in Raw HTTP.

The chat, sampling settings and the last server and model are remembered in the browser.
`tests/fake_llm_server.py` is a small fake llama.cpp/Ollama server. Run it with
`uv run python tests/fake_llm_server.py 18080` to try the console without a real model.

## Terminals

A built-in plugin with real shells inside the page, drawn with [xterm.js](https://xtermjs.org/)
(the terminal VS Code uses, bundled so it works offline).
- **Shell:** PowerShell 7 if it's installed (else Windows PowerShell) in the project folder,
  `ssh` for SSH projects, or `$SHELL` on macOS and Linux. **+ New** in the sidebar starts one for
  any project, or a plain shell in your home folder.
- **Names and colours** match the project's Windows Terminal tab. The sidebar lists every
  session, and the activity-bar badge counts the running ones.
- **Scrollback:** each session keeps about 1 MB of output. Switching sessions or reloading the
  page brings it back, and a shell keeps running while you look elsewhere. Sessions end when
  PatchGoblin stops.
- **Keys:** Ctrl+C copies when text is selected and interrupts the running command otherwise.
  Ctrl+V or right-click pastes.
- **Splits and groups:** terminals are tiled like tmux or VS Code. **Split right** / **Split down**
  (toolbar or a pane's own buttons) add a terminal beside the focused one. Drag the dividers to
  resize, and the shells redraw at the new size. Drag a pane's header onto another pane's edge to
  move it there (the middle swaps the two), or onto a group tab to move it to that group. Groups
  are the tabs along the top: **+** adds one, double-click a name to rename it, **×** closes it
  and its terminals. Only **Split** divides a pane, and it's refused (with a message) when the
  halves would be under about 40×8 characters or the group already has 4 panes; dividers stop at
  the same minimum. **+ New** and a project's Terminal button open a new full-size group when the
  current one is in use. The layout is remembered per browser, and terminals the server has that
  aren't in it (say, opened in another browser tab) go to the dock instead of splitting.
- **Minimize:** a pane's **▁** button (or Ctrl+Shift+M) moves it to a dock under the panes, and
  the others take its space. Click its chip to restore it (its scrollback and any output it
  missed are still there), drag the chip to a pane edge or group tab to place it, or drag a pane
  header onto the dock to minimize it. Minimized panes are remembered per browser.
- **Pane buttons:** minimize, split, zoom, **⋯** (**Rename**, **Pop out** into Windows Terminal,
  **Restart**) and close.
- **Shortcuts** (Ctrl+Shift so shell keys are untouched): `D` split right, `E` split down, `W`
  close, arrows move focus, `Z` zoom, `M` minimize, `T` new group, `PageUp`/`PageDown` switch group. Ctrl+`=`,
  Ctrl+`-` and Ctrl+`0` change the font size. Some browsers reserve a few of these (e.g. `W`, `T`).
- **Limit:** up to 12 shells can run at once.

On Windows the shell runs in a pseudo-console through `pywinpty`, using its WinPTY backend
because pywinpty's ConPTY backend doesn't pass Ctrl+C on. The page gets output through a
single streamed request shared by all visible panes (browsers limit connections per site) and sends keys as small requests, all under the same localhost-only guards as
the rest of PatchGoblin. Anyone who can use PatchGoblin can already run commands through it,
and a browser shell doesn't change that.

## SSH tools

A built-in plugin with a set of SSH helpers.

- **Servers** lists your SSH hosts (name, host, port, OS, optional wake-on-LAN details), which
  are kept in the Server Manager (**Edit in Server Manager**). The key, install and tunnel
  pickers list these first. Hosts your projects use and `~/.ssh/config` aliases appear as
  **suggestions**: **Add** only opens the Server Manager's form filled in, and nothing is saved
  until you press **Save server**. Nothing is ever added automatically.
- **Keys** lists the public keys in `~/.ssh` on this computer (or on any SSH host), with a
  Copy button, and generates new ed25519, RSA (4096) or ECDSA key pairs with `ssh-keygen`.
- **Install keys** adds chosen public keys to `authorized_keys` on one or more hosts
  (skipping keys that are already there), e.g. to copy a key from host A to host B.
  Host suggestions come from your saved servers, then your projects and the `Host` aliases in `~/.ssh/config`.
  The host must already accept your login by key. For a password-only host, tick
  **Use password (opens terminal)**: a terminal window opens running `ssh`, you type the
  password there, and PatchGoblin never sees it.
- **Network** draws who can log in where across your saved servers. It reads each server's
  `authorized_keys` (and `authorized_keys2`) plus the `*.pub` files on every machine, and
  matches key fingerprints. Dashed arrows are guesses from a key's comment (`user@host`).
  It scans only when you open it or press **Refresh**, never in the background, and needs
  working key login. On a Windows host whose account is an Administrator it reads
  `C:\ProgramData\ssh\administrators_authorized_keys` instead of the user file (which sshd ignores).
- **Tunnels** saves and runs `ssh -N -L` / `-R` port forwards, with a live status dot in
  the sidebar. Tunnels never start by themselves, and they stop when PatchGoblin quits.

Only **public** keys are ever listed, copied or sent. Private key files are never opened.

Things to know:

- On a Windows host whose account is an Administrator, sshd ignores
  `~/.ssh/authorized_keys` and reads `C:\ProgramData\ssh\administrators_authorized_keys`
  instead. PatchGoblin shows a note and does not write there; add the key yourself.
- A tunnel bound to `0.0.0.0` exposes the forwarded port to everyone on the network, with
  no authentication in front of it. PatchGoblin asks before starting one. For a remote
  (`-R`) tunnel the SSH server must set `GatewayPorts` for a non-loopback bind to apply.
- The passphrase of a generated key is passed to `ssh-keygen -N`, so it is briefly visible
  in the process list of the machine running it. Leave it empty to avoid that.
- A hard kill of PatchGoblin (not Quit) can leave `ssh` tunnel processes behind.

## Todo

A built-in plugin with one personal checklist, separate from the project task board. Add an item
with the box at the top, tick it off, double-click its text to edit (Enter saves, Esc cancels),
drag rows to reorder them and press **Clear done** to remove finished items. An item can be tagged with
one of your projects. The icon's badge counts the open items.

The sidebar filters the list: **All**, **Open**, **Done**, or a single project. Dragging while a
filter is active reorders only the visible items. Items (up to 500, each a single line of up to
500 characters) are saved in the plugin's data in `settings.json`, and every change is a small
request, so two browser tabs don't overwrite each other. Deleting a project keeps its items; the
tag then shows the old project id.

## Safety notes

- The server only listens on 127.0.0.1 and rejects requests with a foreign `Host`.
  Write requests also need a custom header that other websites can't send. There is
  no login, so don't expose it on a network.
- AI agents act with your user account's permissions in the project directory. Review
  commits (`git show`) before relying on them.
- SSH tools only handle public keys, never start tunnels on their own, and stop them when PatchGoblin quits. A `0.0.0.0` tunnel is reachable from the network, so use it deliberately.
- If PatchGoblin is restarted during a job, that task is marked interrupted: planning
  goes back to its previous state and a run is marked failed. Queued tasks resume.

## Tests

```sh
uv run python -m unittest discover -s tests -v
```

The tests use real temporary directories, git repositories and subprocesses. A fake
agent script stands in for the CLIs, and the OpenAI HTTP call and SSH hop are mocked,
so no AI or network access is needed.
