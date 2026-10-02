"""AI back ends. Each runs *in the project directory on the project's host*:

* ``claude`` / ``codex`` / ``opencode`` / ``cline`` — the CLI agents, launched in the
  project directory (over SSH for remote projects), with the prompt sent on stdin. opencode
  runs a named opencode agent (``plan_agent`` for planning and chat, ``run_agent`` for
  runs) and gets extra checks, since its agents are defined by the user's opencode config.
  Cline's styled output is reduced to its final reply, and its plan mode (which can still
  run commands) gets the same no-edits check.
* configured endpoints (``openai``, ``openrouter``, …) — the tool-calling loop in
  ``openai_agent``.
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass

from . import gitops, opencode, svnops
from .gitops import dirty_fingerprint
from .hosts import HostError
from .store import find_endpoint, vcs_for


class Cancelled(Exception):
    pass


@dataclass
class Outcome:
    ok: bool
    text: str = ""
    error: str = ""


def run_ai(provider: str, mode: str, prompt: str, *, host, project: dict, settings: dict,
           model: str, job, run_marker=None) -> Outcome:
    """Run ``prompt`` with ``provider`` in ``mode`` ("plan" or "run").

    ``run_marker`` (optional) returns a value that changes whenever a task run starts or is
    active in the project; opencode's plan check uses it to avoid blaming a run's edits on a plan.
    """
    timeout = float(settings["timeouts"][mode])
    ops = vcs_for(project)
    is_tracked = ops is gitops  # git only: SVN projects use the untracked code paths
    svn_wc = ops is svnops
    endpoint = find_endpoint(settings, provider)
    if endpoint is not None:
        from .openai_agent import OpenAIAgent  # imported here: openai_agent imports this module
        return OpenAIAgent(host, project["path"], endpoint, model, mode, job, tracked=is_tracked).run(prompt, timeout)
    if provider not in settings["commands"]:
        return Outcome(False, error=f"AI provider '{provider}' is not configured "
                                    "(it may have been removed in Settings).")
    cfg, cwd = settings["commands"][provider], project["path"]
    if provider in ("opencode", "cline"):
        runner = run_opencode if provider == "opencode" else run_cline
        return runner(cfg, mode, prompt, host=host, cwd=cwd, model=model, timeout=timeout, job=job,
                      run_marker=run_marker, tracked=is_tracked, svn=svn_wc)
    runner = run_claude if provider == "claude" else run_cli
    return runner(cfg, mode, prompt, host=host, cwd=cwd, model=model, timeout=timeout, job=job)


def agent_for(cfg: dict, mode: str) -> str:
    """The agent name a CLI config uses in ``mode`` (planning and chat use the plan agent)."""
    value = cfg.get("plan_agent" if mode == "plan" else "run_agent")
    return value.strip() if isinstance(value, str) else ""


def cli_argv(cfg: dict, mode: str, model: str) -> list[str]:
    template = cfg[mode]
    argv = list(template) if isinstance(template, list) else shlex.split(template)
    agent = agent_for(cfg, mode)
    argv = [arg.replace("{agent}", agent) for arg in argv]
    if model and cfg.get("model_flag"):
        # Keep a trailing "-" (read prompt from stdin) as the final argument.
        at = len(argv) - 1 if argv and argv[-1] == "-" else len(argv)
        argv[at:at] = [cfg["model_flag"], model]
    return argv


def run_opencode(cfg: dict, mode: str, prompt: str, *, host, cwd: str, model: str,
                 timeout: float, job, run_marker=None, tracked: bool = True,
                 svn: bool = False) -> Outcome:
    """opencode, with checks for what its config can get wrong: the agent must be defined
    (opencode may otherwise fall back to its full-access default agent), and planning must
    leave the working tree as it was (changed files are reported, never reverted)."""
    agent = agent_for(cfg, mode)
    role = "plan" if mode == "plan" else "run"
    if not agent:
        return Outcome(False, error=f"No opencode {role} agent is set. Set it in Settings → opencode.")
    if cfg.get("require_agents", True) and agent not in opencode.BUILTIN_AGENTS:
        dirs = opencode.config_dirs(host)
        configs, _ = opencode.read_configs(host, cwd, dirs)
        if agent not in opencode.agent_names(host, cwd, configs, dirs):
            return Outcome(False, error=f'opencode agent "{agent}" is not defined for this project. Add it to '
                                        f'opencode.json or .opencode/agent/{agent}.md, or change it in '
                                        "Settings → opencode.")
    env = {"NO_COLOR": "1"}
    if mode == "plan":
        # Highest-precedence inline config: the planning agent may read but not edit or run commands.
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            {"agent": {agent: {"permission": {"edit": "deny", "bash": "deny"}}}})

    def run() -> Outcome:
        outcome = run_cli(cfg, mode, prompt, host=host, cwd=cwd, model=model, timeout=timeout, job=job, env=env)
        outcome.text = opencode.strip_ansi(outcome.text).strip()
        outcome.error = opencode.strip_ansi(outcome.error)
        return outcome

    return _no_plan_edits(cfg, mode, run, host=host, cwd=cwd, job=job, run_marker=run_marker, tracked=tracked, svn=svn,
                          blame=lambda paths: f"opencode's plan agent modified files: {paths}. Review them, "
                                              f'and set agent.{agent} permissions edit/bash to "deny" in '
                                              "opencode.json.")


def _no_plan_edits(cfg: dict, mode: str, run, *, host, cwd: str, job, run_marker, blame,
                   tracked: bool = True, svn: bool = False) -> Outcome:
    """``run()``, failing a plan (or chat) that left the working tree changed (the changed files
    are reported, never reverted) unless ``plan_must_not_edit`` is off. ``blame(paths)`` is the
    error message. Untracked projects skip the check entirely (there is no git to diff)."""
    check = mode == "plan" and cfg.get("plan_must_not_edit", True) is not False
    before = _fingerprint(host, cwd, job, tracked, svn) if check else None
    marker = run_marker() if before is not None and run_marker else None
    outcome = run()
    if before is None:
        return outcome
    after = _fingerprint(host, cwd, job, tracked, svn)
    if after is None:
        return outcome
    paths = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    if not paths:
        return outcome
    if run_marker and (marker != run_marker() or marker[1]):
        job.write(f"Files changed while a task run was active, so they aren't blamed on planning: "
                  f"{', '.join(paths)}\n")
        return outcome
    return Outcome(False, outcome.text, blame(", ".join(paths)))


def _fingerprint(host, cwd: str, job, tracked: bool = True, svn: bool = False) -> dict | None:
    """The working tree's dirty fingerprint, or None (logged) if git/svn can't give one (or tracking
    is off), so a plan never fails because of version control."""
    if svn:
        try:
            return svnops.dirty_fingerprint(host, cwd)
        except HostError as exc:
            job.write(f"Can't check planning edits (svn status failed): {exc}\n")
            return None
    if not tracked:
        job.write("Git tracking is off; planning edits aren't checked.\n")
        return None
    try:
        return dirty_fingerprint(host, cwd)
    except HostError as exc:
        job.write(f"Can't check planning edits (git status failed): {exc}\n")
        return None


class _PlainLog:
    """A job whose log writes have ANSI styling removed (an escape split across two writes is
    held back until it is complete)."""

    def __init__(self, job):
        self._job, self._pending = job, ""

    def write(self, text: str) -> None:
        text = self._pending + text
        at = text.rfind("\x1b")
        if at != -1 and len(text) - at < 32 and not opencode.ANSI.match(text, at):
            text, self._pending = text[:at], text[at:]
        else:
            self._pending = ""
        if text:
            self._job.write(opencode.strip_ansi(text))

    def __getattr__(self, name):
        return getattr(self._job, name)


def cline_reply(output: str) -> str:
    """Cline's final reply from its plain output. Its thinking and tool calls are styled with
    ANSI escapes and the reply is not, so the reply is whatever follows the last escape."""
    last = None
    for last in opencode.ANSI.finditer(output or ""):
        pass
    reply = output[last.end():].strip() if last else ""
    return reply or opencode.strip_ansi(output).strip()


def run_cline(cfg: dict, mode: str, prompt: str, *, host, cwd: str, model: str,
              timeout: float, job, run_marker=None, tracked: bool = True,
              svn: bool = False) -> Outcome:
    """The Cline CLI: its output is cut down to the final reply, and since plan mode only
    blocks file-editing tools (commands can still change files), planning is checked too."""

    def run() -> Outcome:
        outcome = run_cli(cfg, mode, prompt, host=host, cwd=cwd, model=model, timeout=timeout,
                          job=_PlainLog(job))
        outcome.text = cline_reply(outcome.text)
        outcome.error = opencode.strip_ansi(outcome.error)
        return outcome

    return _no_plan_edits(cfg, mode, run, host=host, cwd=cwd, job=job, run_marker=run_marker, tracked=tracked, svn=svn,
                          blame=lambda paths: f"Cline modified files while planning: {paths}. Review them; "
                                              "the planning command should use plan mode (-p).")


def run_cli(cfg: dict, mode: str, prompt: str, *, host, cwd: str, model: str,
            timeout: float, job, env: dict | None = None) -> Outcome:
    argv = cli_argv(cfg, mode, model)
    job.write(f"$ {' '.join(argv)}\n")
    stdin = prompt
    if cfg.get("prompt_arg"):  # for CLIs that take the prompt as an argument, not on stdin
        argv, stdin = [*argv, prompt], None
    res = host.run(argv, cwd=cwd, input=stdin, timeout=timeout,
                   on_output=job.write, on_start=job.attach, login=True, env=env)
    if job.cancelled:
        raise Cancelled()
    text = res.stdout.strip()
    if res.timed_out:
        return Outcome(False, text, f"Timed out after {int(timeout)}s.")
    if res.returncode != 0:
        tail = (res.stderr.strip() or text)[-2000:]
        return Outcome(False, text, f"{argv[0]} exited with code {res.returncode}.\n{tail}")
    if not text:
        return Outcome(False, text, f"{argv[0]} produced no output.")
    return Outcome(True, text)


def _rel_path(path: str, cwd: str) -> str:
    """``path`` relative to the project directory when it lies inside it."""
    norm, base = path.replace("\\", "/"), (cwd or "").replace("\\", "/").rstrip("/")
    if base and norm.lower().startswith(base.lower() + "/"):
        return norm[len(base) + 1:]
    return path


def describe_tool(name: str, args, cwd: str = "") -> tuple[str, str]:
    """A readable (label, detail) for a tool call, for the live activity feed."""
    args = args if isinstance(args, dict) else {}

    def arg(*keys) -> str:
        for key in keys:
            if isinstance(args.get(key), str) and args[key]:
                return args[key]
        return ""

    verbs = {"Read": "Reading", "Edit": "Editing", "Write": "Writing", "MultiEdit": "Editing",
             "NotebookEdit": "Editing", "read_file": "Reading", "write_file": "Writing"}
    if name in verbs:
        return f"{verbs[name]} {_rel_path(arg('file_path', 'notebook_path', 'path'), cwd) or '(file)'}", ""
    if name in ("Bash", "run_command"):
        return f"Running `{clip_head(arg('command'), 200)}`", arg("description")
    if name in ("Grep", "Glob", "search"):
        return f"Searching `{arg('pattern')}`", ""
    if name == "list_files":
        return f"Listing files {arg('prefix')}".rstrip(), ""
    if name == "TodoWrite":
        todos = args.get("todos")
        lines = [f"[{t.get('status', '')}] {t.get('content', '')}" for t in todos if isinstance(t, dict)] \
            if isinstance(todos, list) else []
        return "Updating plan", "\n".join(lines)
    if name == "Task":
        return "Starting sub-agent", arg("description")
    return f"{name} {clip_head(json.dumps(args), 200)}".strip(), ""


class _StreamJson:
    """A job adapter for Claude Code's ``--output-format stream-json``: complete JSON lines
    become activity events; anything else (stderr, non-JSON) goes to the raw log unchanged."""

    def __init__(self, job, cwd: str = ""):
        self._job, self._cwd, self._buf = job, cwd, ""
        self.result_text: str | None = None
        self.is_error = False
        self.stray = ""  # the tail of the non-JSON output, for error messages

    def write(self, text: str) -> None:
        self._buf += text
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            self._line(line)

    def flush(self) -> None:
        line, self._buf = self._buf, ""
        self._line(line)

    def _line(self, line: str) -> None:
        if not line.strip():
            return
        try:
            data = json.loads(line)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self._raw(line)
            return
        try:
            self._handle(data)
        except Exception:  # a surprising shape must never fail the job
            self._raw(line[:500])

    def _raw(self, line: str) -> None:
        self.stray = (self.stray + line + "\n")[-2000:]
        self._job.write(line + "\n")

    def _handle(self, data: dict) -> None:
        job, kind = self._job, data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            job.event("note", f"Started: model {data.get('model', '?')}, in {data.get('cwd', '?')}")
        elif kind == "assistant":
            content = (data.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if block.get("type") == "text" and isinstance(text, str) and text.strip():
                    job.event("say", text.strip(), text.strip() if len(text) > 200 else "")
                elif block.get("type") == "tool_use":
                    job.event("tool", *describe_tool(block.get("name") or "tool", block.get("input"), self._cwd))
        elif kind == "user":
            content = (data.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    body = block.get("content")
                    if isinstance(body, list):
                        body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
                    job.event("error", clip_head(str(body or "Tool error"), 500).strip())
        elif kind == "result":
            self.result_text = data["result"] if isinstance(data.get("result"), str) else ""
            self.is_error = bool(data.get("is_error"))
            bits = []
            if data.get("duration_ms") is not None:
                bits.append(f"{float(data['duration_ms']) / 1000:.0f}s")
            if data.get("num_turns") is not None:
                bits.append(f"{data['num_turns']} turns")
            if data.get("total_cost_usd") is not None:
                bits.append(f"${float(data['total_cost_usd']):.2f}")
            job.event("note", "Finished" + (f": {', '.join(bits)}" if bits else ""))

    def __getattr__(self, name):
        return getattr(self._job, name)


def run_claude(cfg: dict, mode: str, prompt: str, *, host, cwd: str, model: str,
               timeout: float, job) -> Outcome:
    """Claude Code. With ``--output-format stream-json`` its steps feed the live activity view
    and the final answer is the ``result`` event; other commands run as plain CLIs."""
    if "stream-json" not in cli_argv(cfg, mode, model):
        return run_cli(cfg, mode, prompt, host=host, cwd=cwd, model=model, timeout=timeout, job=job)
    stream = _StreamJson(job, cwd)
    outcome = run_cli(cfg, mode, prompt, host=host, cwd=cwd, model=model, timeout=timeout, job=stream)
    stream.flush()
    if stream.result_text is None:
        if not outcome.ok:  # timed out or exited with an error: that message says why
            return outcome
        tail = stream.stray.strip()
        return Outcome(False, "", "Claude Code ended without a result." + (f"\n{tail}" if tail else ""))
    text = stream.result_text.strip()
    if stream.is_error:
        return Outcome(False, text, text or "Claude Code reported an error.")
    if not outcome.ok:
        return Outcome(False, text, outcome.error)
    return Outcome(bool(text), text, "" if text else "claude produced no output.")


def clip_head(text: str, limit: int = 20000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… [truncated {len(text) - limit} chars]"

