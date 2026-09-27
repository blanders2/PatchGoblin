# PatchGoblin

A local Flask web app for planning and queuing AI work across local directories and SSH hosts.

## Run

Requires Python 3.10+ and Git. On Windows, from this directory:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

On Linux/macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

Open **http://127.0.0.1:5050**. Keep that process running for the queue to work.
The included launcher uses Waitress, one background queue worker, and a single-instance lock.
Use `run.py`, not Flask's reloader or a multi-process WSGI deployment.
`PATCHGOBLIN_PORT` changes the port; `PATCHGOBLIN_DATA` changes the local registry directory
(default `.patchgoblin-app` relative to the launch directory).

## Workflow

1. Connect a project using an absolute directory. A missing directory is created and Git is initialized if needed.
2. Create tasks. Each project's source of truth is **`.patchgoblin/tasks.json` inside that project**, including on SSH hosts.
3. Choose **Plan with AI**, or write a plan in **Edit task & plan** and choose **Mark planned**. Manual planning does not require AI credentials.
4. Review the plan, then queue the task. Planning and implementation both use that project's directory and selected provider.
5. Inspect the result, errors, history, and commit in the task detail panel. Successful runs enter **Validation**, never Completed automatically. Review the changes and checks, then choose **Mark complete** or **Send back to run queue**.

States: `unplanned → planning_queued → planning → planned → queued → running → validation → completed`.
Manual planning skips the two planning states. Run errors become `failed`; failed tasks can be edited,
replanned, or marked planned again. Planning and implementation share a first-in-first-out queue.
**Pause queue** stops new jobs, allowing current work to finish. Waiting tasks can be removed from the queue.

**Validation and discussion:** Open a task and use **Work on the plan** to report problems, ask questions, and collaborate with AI. Each reply reads the current project, sees the previous result and recent conversation, and updates the editable plan. Validation discussion uses `revising_queued → revising → validation`; it never launches implementation. Repeat the discussion as needed, edit the plan manually, then send the task back to the run queue. Only explicit user validation marks a task complete. Conversation messages are saved in the project task JSON. Initial planning tasks support the same discussion interface.

Task files survive server restarts. Queued work resumes; active work is never blindly rerun.
Use **Recover interrupted run** if a crashed run remains active. It refuses recovery while the
host's run lock is held. An interrupted project's further work waits for recovery.
There is no force-cancel button: use the host's process tools if an agent must be terminated.

## Providers

### Codex CLI

Install and authenticate `codex` on each project host; it must be on the launching process's PATH.
PatchGoblin uses `codex exec`, sends the prompt through stdin, sets the project working directory,
and uses the `read-only` sandbox for planning and `workspace-write` for implementation.
Leave Model ID empty to use the CLI configuration.
It does not drive or embed the Codex desktop GUI; you can open the same repository in your IDE.

Reference: [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

### Claude Code CLI

Install and authenticate `claude` on each project host. PatchGoblin invokes print mode and parses its JSON result.
Planning exposes Read/Glob/Grep. Implementation exposes Read/Glob/Grep/Edit/Write, with Bash
only when **Allow Claude / API agents to run commands** is enabled. Command execution is authorized
for the entire task when that option is enabled; it is not an interactive terminal approval flow.
Existing Claude hooks and configuration still apply. No permission-bypass flag is used.

Reference: [Run Claude Code programmatically](https://code.claude.com/docs/en/headless).

### OpenAI API

Choose OpenAI API and enter a model ID available to your account that supports Responses function calling.
Set `OPENAI_API_KEY` in the environment **on the project host** before starting PatchGoblin or the remote helper.
No key is stored in task files, the project registry, or browser storage.

The built-in Responses API loop provides project file listing, reading, and writing. Planning is read-only.
Paths are resolved within the project (including symlink checks), and `.git`, PatchGoblin metadata,
`.env*`, `.pem`, and `.key` paths are reserved. Listing respects Git ignore rules.
The optional command tool runs argument arrays with the project as cwd, with **no OS sandbox**.
Only enable it for trusted tasks and repositories; CLI tools also inherit the host account's environment.
Without command access, the API can edit files but cannot run tests/builds or install dependencies.
Files sent to this provider go to OpenAI; API charges apply to live runs.
The loop is bounded to 40 model steps and approximately 30 minutes (an in-flight request can finish after that limit).

Reference: [Responses function calling](https://developers.openai.com/api/docs/guides/function-calling).

## SSH hosts

The controller can run on Windows, macOS, or Linux. Remote hosts currently require a POSIX shell
(Linux/macOS), Python 3.10+, Git, and the selected AI CLI or `OPENAI_API_KEY`.

- Use `user@hostname` or an alias from your SSH config. Configure keys/agent access first.
- Verify and add the host's key using normal SSH before registering it; host verification is never disabled.
- Test `ssh your-alias 'python3 --version'` and the provider's availability in that same noninteractive environment.
- Enter an absolute POSIX directory (for example `/home/me/projects/service`) and, if needed, a custom Python executable.
- SSH uses the controller's `ssh` executable and configuration. Port defaults to 22.
- The standard-library helper is sent through stdin; no app installation or remote HTTP port is needed.
- Prompts and paths are sent as data, not interpolated into remote shell commands.
- API keys must already be available to noninteractive SSH sessions. They are never forwarded from the controller.
- A disconnected run can continue on the host. Refresh and inspect its state before recovery/retry.

Remote Windows hosts and password-prompt SSH sessions are not currently supported.

## Git behavior

Project registration initializes an independent repository at that directory if `.git` is absent,
including when its parent is another repository. Existing repositories and remotes are preserved.
New repositories receive basic ignore patterns for dependencies and common credential files; inspect
your own `.gitignore` before committing a real project. Existing ignore rules are not replaced.
New repositories get an initial commit containing only `.gitignore` and the task JSON. Existing
project source is left for an explicit user checkpoint; an empty new project is ready immediately.

Before implementation, tracked and untracked changes outside `.patchgoblin` must be committed.
**Create Git checkpoint** shows the current file list and commits all nonignored changes when selected.
Successful implementation stages and commits changes with a task-specific message. Commits use
per-command `PatchGoblin <patchgoblin@localhost>` identity without changing Git configuration.
No pushes happen. Failures preserve edits for review. Task JSON is tracked; operational locks are excluded.
The final commit ID is saved after committing, so task metadata can remain modified between checkpoints.
Avoid editing the same working tree while an AI job runs; Git cannot distinguish simultaneous human edits.

## Storage and operating limits

- Atomic JSON replacement and host-side OS file locks protect task writes and serialize project runs.
- The local registry holds project connection settings and the queue pause flag. It is ignored by Git.
- One app instance owns each registry. Multiple controllers may share host locks, but a single controller
  is the supported operating mode; this is not a distributed or multi-user service.
- Serve only on localhost. Mutations require a per-process browser token; Host and Origin are checked.
  There is no user authentication or public deployment support.
- Logs/results render as plain text, including Markdown, to avoid interpreting model-generated HTML.
- Large/binary file editing, force cancellation, streaming agent logs and embedded
  IDE sessions are not included. Plans can be edited and regenerated with updated task instructions.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use real temporary directories, JSON, Git repositories, HTTP routes, and local helper subprocesses.
AI responses and the SSH transport are mocked, so the suite does not make paid API calls or require a remote host.

For the optional browser smoke test, install Playwright in your own development environment, start
`.venv/Scripts/python.exe -m tests.serve_browser_fixture` (or `.venv/bin/python` on POSIX), then run
`node tests/browser-smoke.cjs`. This uses an isolated temporary project on port 5051 and fake AI replies.
It requires installed Chrome by default; set `PATCHGOBLIN_BROWSER=msedge` for Edge.
Screenshots go to ignored `test-results/`. Stop the fixture server after testing.
