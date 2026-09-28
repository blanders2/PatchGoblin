# PatchGoblin

A small Flask web app for queueing up AI work on your projects. Write tasks, get an
AI (Claude Code, Codex, or the OpenAI API) to help plan them, then queue them for the
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
| Planned | Has a plan (AI-drafted or yours). You can edit it, refine it with AI feedback, or queue it. |
| Queued | Waiting for the AI to implement it. |
| Running… | The AI is working in the project directory. Live output is shown in the task panel. |
| Done | Finished and committed. You can reopen it. |
| Failed | The run failed or was cancelled. You can re-queue it, replan it, or mark it planned. |

Planning and implementation both run **in the project's directory, on the project's
host**. Each project runs one task at a time, in queue order. Different projects run in
parallel. Planning jobs start straight away because they don't change files.

**Git.** Before a run, any uncommitted changes you made are committed as a
`checkpoint before task #N`, so the AI's commit contains only its own work. After a
successful run, everything is committed as `PatchGoblin: task #N <title>` with the
AI's summary. Nothing is ever pushed. The commit uses your git identity if it's set,
otherwise `PatchGoblin <patchgoblin@localhost>`. Failed runs leave their changes
uncommitted so you can inspect them. The **Commits** button shows recent history.

**Chat.** The **Chat** button opens a conversation with the project's AI, running in
the project directory with the same read-only access as planning (the planning command
and timeout are used). Conversations are kept in memory until PatchGoblin restarts.

## AI providers

Choose a default AI for each project and optionally a model. Individual tasks can
override the default. Commands can be edited under **Settings**.

- **Claude Code** (`claude`) runs in print mode with the prompt on stdin. Planning
  only allows read/search tools. Runs use `--permission-mode acceptEdits` and allow
  `Read, Glob, Grep, Edit, Write, Bash`, so the agent can run tests and builds without
  prompting. Tighten the run command in Settings if you want less.
- **Codex** (`codex exec`) uses the `read-only` sandbox for planning and
  `workspace-write` for runs.
- **OpenAI API** runs a tool-calling agent against any OpenAI-compatible Chat
  Completions endpoint (set the base URL and model in Settings). The key is read from
  `OPENAI_API_KEY` in PatchGoblin's environment and is never stored. The agent can list,
  read and search files, and during runs it can also write files. Its tools run
  through the project's host, so remote projects don't need a key on the remote
  machine. It can't write to `.git/` or `.patchgoblin/`, or to paths outside the project.
  Shell commands are off by default. Turning them on in Settings runs them unsandboxed.

The CLIs must be installed and logged in on whichever machine hosts the project.

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
