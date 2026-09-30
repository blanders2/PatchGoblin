"""AI back ends. Each runs *in the project directory on the project's host*:

* ``claude`` / ``codex`` / ``opencode`` / ``cline`` — the CLI agents, launched in the
  project directory (over SSH for remote projects), with the prompt sent on stdin. opencode
  runs a named opencode agent (``plan_agent`` for planning and chat, ``run_agent`` for
  runs) and gets extra checks, since its agents are defined by the user's opencode config.
  Cline's styled output is reduced to its final reply, and its plan mode (which can still
  run commands) gets the same no-edits check.
* configured endpoints (``openai``, ``openrouter``, …) — a tool-calling loop
  against an OpenAI-compatible Chat Completions API. The model's file and command
  tools are executed through the project's host, so remote projects work without
  any key on the remote side.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import gitops, opencode
from .gitops import dirty_fingerprint, git
from .hosts import HostError
from .store import find_endpoint

PLAN_INSTRUCTIONS = """\
You are planning a software task for the project in the current working directory.
Investigate the relevant code as needed, but DO NOT create, modify or delete any files.
Reply with ONLY the implementation plan, in Markdown:
- a one-paragraph summary of the approach
- numbered, concrete steps naming the files/functions to change
- how to verify the change (tests or checks to run)
- a "## Risks" section: things the implementer should watch for
- a "## Questions for you" section: a numbered list of decisions only the user can make,
  one line each. If there are none, write "None."
  If a question is yes/no, end it with `[Yes / No]`; if it has a few likely answers, end it
  with them in brackets separated by ` / `, e.g. `[Tabs / Spaces]` (2–5 short options).
  Open-ended questions have no brackets.
"""

PLAN_TRUST_LEVELS = ("low", "normal", "high")

TRUST_INSTRUCTIONS = {
    "normal": "",
    "low": """\
Planning trust is LOW: do not assume. Whenever requirements, scope, UX, naming or approach
are ambiguous, ask under "Questions for you" instead of choosing. Prefer asking over guessing.
List any unavoidable assumptions as one-line bullets in a "## Assumptions" section placed
before "## Risks" ("None." if there are none).
""",
    "high": """\
Planning trust is HIGH: act on your best judgement. Resolve ambiguity yourself using the
existing code, conventions and common practice. Record each such choice as a one-line bullet
in a "## Assumptions" section placed before "## Risks" ("None." if there are none).
Ask under "Questions for you" only about decisions that are costly or hard to undo if wrong
(e.g. data loss, public API or file-format changes, security), or that can't be inferred at
all. Never assume against an answer the user already gave. Most plans should have no questions.
""",
}


def resolve_trust(task: dict, project: dict) -> str:
    """The planning trust level for a task: its own override, else its project's, else 'normal'."""
    for level in ((task or {}).get("plan_trust"), (project or {}).get("plan_trust")):
        if level in PLAN_TRUST_LEVELS:
            return level
    return "normal"

TITLE_INSTRUCTIONS = """\
Start your reply with a single line `Title: <a concise, specific task title in the imperative,
under 80 characters>`, then a blank line, then the plan.
"""

MAX_TITLE = 120
_TITLE_LINE = re.compile(r"^\s*[#>*_\s]*title\s*[*_]*\s*[:\-]\s*[*_]*\s*(.+?)\s*[*_]*\s*$", re.IGNORECASE)


def split_title(text: str) -> tuple[str, str]:
    """Split a leading ``Title: …`` line off an AI plan: returns (title, rest), or ("", text)."""
    lines = (text or "").splitlines()
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return "", text
    match = _TITLE_LINE.match(lines[first])
    if not match:
        return "", text
    title = " ".join(match.group(1).strip().strip("\"'`").split())
    if len(title) > MAX_TITLE:
        title = title[:MAX_TITLE - 1].rstrip() + "…"
    if not title:
        return "", text
    return title, "\n".join(lines[first + 1:]).strip("\n")

RUN_INSTRUCTIONS = """\
You are implementing a planned task in the project in the current working directory.
Follow the plan, adapting it if the code requires. Keep changes focused on this task.
Do not run git commit or push; the changes are committed automatically when you finish.
Do not edit anything under .patchgoblin/.
When you are done, reply with a short summary of what you changed and how you verified it.
"""

RUN_INSTRUCTIONS_UNTRACKED = """\
You are implementing a planned task in the project in the current working directory.
Follow the plan, adapting it if the code requires. Keep changes focused on this task.
Do not edit anything under .patchgoblin/.
When you are done, reply with a short summary of what you changed and how you verified it.
"""


CHAT_INSTRUCTIONS = """\
You are chatting with the user about the project in the current working directory.
Investigate the code as needed to answer, but DO NOT create, modify or delete any files.
Reply to the user's latest message in Markdown, concisely.
"""

MAX_CHAT_CONTEXT = 40000


def chat_prompt(messages: list[dict]) -> str:
    """The conversation so far (oldest turns dropped if long), ending with the user's message."""
    turns, size = [], 0
    for msg in reversed(messages):
        turn = f"### {'User' if msg['role'] == 'user' else 'Assistant'}\n{msg['text'].strip()}"
        if turns and size + len(turn) > MAX_CHAT_CONTEXT:
            break
        turns.insert(0, turn)
        size += len(turn)
    return CHAT_INSTRUCTIONS + "\n# Conversation\n\n" + "\n\n".join(turns) + "\n"


_QUESTIONS_HEADING = re.compile(r"^#{1,6}\s*(open\s+)?questions\b", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s")
_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*+])\s+(.*)$")
_NONE = re.compile(r"^\W*(none|n/?a|no( open)? questions)\W*$", re.IGNORECASE)
_OPTIONS = re.compile(r"\s*\[([^\[\]]*/[^\[\]]*)\]\s*[.?]?\s*$")
MAX_OPTIONS = 6


def _parse_question(q: str) -> dict:
    """Split a trailing ``[A / B / C]`` off a question into its answer options."""
    match = _OPTIONS.search(q)
    if not match:
        return {"text": q, "options": []}
    options, seen = [], set()
    for opt in match.group(1).split("/"):
        opt = opt.strip()
        if opt and opt.lower() not in seen:
            seen.add(opt.lower())
            options.append(opt)
    options = options[:MAX_OPTIONS]
    if len(options) < 2:
        return {"text": q, "options": []}
    text = q[:match.start()].rstrip()
    if "?" in q[match.end(1):] and not text.endswith("?"):  # "Which [A / B]?" keeps its "?"
        text += "?"
    return {"text": text, "options": options}


def plan_questions(plan: str) -> list[dict]:
    """The items of the plan's "Questions for you" (or "Open questions") section, as
    ``{"text", "options"}``; options come only from a trailing ``[A / B]`` on the item."""
    items: list[str] = []
    inside = False
    for line in (plan or "").splitlines():
        stripped = line.strip()
        if _HEADING.match(stripped):
            if inside:
                break
            inside = bool(_QUESTIONS_HEADING.match(stripped))
            continue
        if not inside or not stripped:
            continue
        item = _ITEM.match(line)
        if item:
            items.append(item.group(1).strip())
        elif items and line[:1].isspace():
            items[-1] += " " + stripped  # wrapped continuation of the previous item
    return [_parse_question(q) for q in items if q and not _NONE.match(q)]


def ready_status(plan: str) -> str:
    """Where a task with this plan belongs once it is ready: 'drafted' while it has open questions."""
    return "drafted" if plan_questions(plan or "") else "planned"


def plan_prompt(task: dict, feedback: str = "", answers: list[dict] | None = None,
                rewrite_title: bool = False, trust: str = "normal", review_feedback: str = "",
                commit: str = "") -> str:
    # The title rule stays last so "Start your reply with…" wins.
    instructions = (PLAN_INSTRUCTIONS + TRUST_INSTRUCTIONS.get(trust, "")
                    + (TITLE_INSTRUCTIONS if rewrite_title else ""))
    parts = [instructions, f"# Task #{task['id']}: {task['title']}"]
    if task.get("description", "").strip():
        parts.append(f"## Description\n{task['description'].strip()}")
    if task.get("plan", "").strip():
        parts.append(f"## Current draft plan\n{task['plan'].strip()}")
    answered = [a for a in answers or [] if a.get("answer", "").strip()]
    if answered:
        qa = "\n\n".join(f"Q: {a.get('question', '').strip()}\nA: {a['answer'].strip()}" for a in answered)
        parts.append(f"## Answers to your questions\n{qa}\n\n"
                     "Fold these answers into a complete revised plan. Under \"Questions for you\", "
                     "repeat only questions that are still open.")
    if feedback.strip():
        parts.append(f"## Feedback on the plan from the user\n{feedback.strip()}\n\n"
                     "Produce a revised, complete plan that addresses this feedback.")
    if review_feedback.strip():
        parts.append(_review_section(review_feedback, commit))
    return "\n\n".join(parts) + "\n"


def _review_section(feedback: str, commit: str = "") -> str:
    """The engineer's feedback on a finished AI run, framed as follow-up work."""
    where = (f"The previous attempt was already committed (`{commit[:10]}`)" if commit
             else "The previous attempt's changes are already in the working tree")
    return (f"## Feedback from reviewing the last AI run\n{feedback.strip()}\n\n"
            f"{where}, so the current code already includes it. Plan only the follow-up changes "
            "needed on top of the current code; do not redo work that is already correct.")


def run_prompt(task: dict, tracked: bool = True) -> str:
    parts = [RUN_INSTRUCTIONS if tracked else RUN_INSTRUCTIONS_UNTRACKED, f"# Task #{task['id']}: {task['title']}"]
    if task.get("description", "").strip():
        parts.append(f"## Description\n{task['description'].strip()}")
    plan = task.get("plan", "").strip() or "(No written plan: use the description.)"
    parts.append(f"## Plan\n{plan}")
    if (task.get("review_feedback") or "").strip():
        parts.append(_review_section(task["review_feedback"], task.get("commit", "")))
    return "\n\n".join(parts) + "\n"


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
    is_tracked = gitops.tracked(project)
    endpoint = find_endpoint(settings, provider)
    if endpoint is not None:
        return OpenAIAgent(host, project["path"], endpoint, model, mode, job, tracked=is_tracked).run(prompt, timeout)
    if provider not in settings["commands"]:
        return Outcome(False, error=f"AI provider '{provider}' is not configured "
                                    "(it may have been removed in Settings).")
    if provider == "opencode":
        return run_opencode(settings["commands"][provider], mode, prompt, host=host, cwd=project["path"],
                            model=model, timeout=timeout, job=job, run_marker=run_marker, tracked=is_tracked)
    if provider == "cline":
        return run_cline(settings["commands"][provider], mode, prompt, host=host, cwd=project["path"],
                         model=model, timeout=timeout, job=job, run_marker=run_marker, tracked=is_tracked)
    return run_cli(settings["commands"][provider], mode, prompt, host=host, cwd=project["path"],
                   model=model, timeout=timeout, job=job)


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
                 timeout: float, job, run_marker=None, tracked: bool = True) -> Outcome:
    """opencode, with checks for what its config can get wrong: the agent must be defined
    (opencode may otherwise fall back to its full-access default agent), and planning must
    leave the working tree as it was (changed files are reported, never reverted)."""
    agent = agent_for(cfg, mode)
    role = "plan" if mode == "plan" else "run"
    if not agent:
        return Outcome(False, error=f"No opencode {role} agent is set. Set it in Settings → opencode.")
    if cfg.get("require_agents", True) and agent not in opencode.BUILTIN_AGENTS:
        configs, _ = opencode.read_configs(host, cwd)
        if agent not in opencode.agent_names(host, cwd, configs):
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

    return _no_plan_edits(cfg, mode, run, host=host, cwd=cwd, job=job, run_marker=run_marker, tracked=tracked,
                          blame=lambda paths: f"opencode's plan agent modified files: {paths}. Review them, "
                                              f'and set agent.{agent} permissions edit/bash to "deny" in '
                                              "opencode.json.")


def _no_plan_edits(cfg: dict, mode: str, run, *, host, cwd: str, job, run_marker, blame,
                   tracked: bool = True) -> Outcome:
    """``run()``, failing a plan (or chat) that left the working tree changed (the changed files
    are reported, never reverted) unless ``plan_must_not_edit`` is off. ``blame(paths)`` is the
    error message. Untracked projects skip the check entirely (there is no git to diff)."""
    check = mode == "plan" and cfg.get("plan_must_not_edit", True) is not False
    before = _fingerprint(host, cwd, job, tracked) if check else None
    marker = run_marker() if before is not None and run_marker else None
    outcome = run()
    if before is None:
        return outcome
    after = _fingerprint(host, cwd, job, tracked)
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


def _fingerprint(host, cwd: str, job, tracked: bool = True) -> dict | None:
    """The working tree's dirty fingerprint, or None (logged) if git can't give one (or tracking
    is off), so a plan never fails because of git."""
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
              timeout: float, job, run_marker=None, tracked: bool = True) -> Outcome:
    """The Cline CLI: its output is cut down to the final reply, and since plan mode only
    blocks file-editing tools (commands can still change files), planning is checked too."""

    def run() -> Outcome:
        outcome = run_cli(cfg, mode, prompt, host=host, cwd=cwd, model=model, timeout=timeout,
                          job=_PlainLog(job))
        outcome.text = cline_reply(outcome.text)
        outcome.error = opencode.strip_ansi(outcome.error)
        return outcome

    return _no_plan_edits(cfg, mode, run, host=host, cwd=cwd, job=job, run_marker=run_marker, tracked=tracked,
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


class ProjectFiles:
    """File access confined to the project root (used by the OpenAI agent)."""

    RESERVED = (".git", ".patchgoblin")

    def __init__(self, host, root: str):
        self.host, self.root = host, root

    def resolve(self, rel: str, for_write: bool = False) -> str:
        rel = (rel or "").replace("\\", "/").strip()
        norm = posixpath.normpath(rel) if rel else "."
        if norm.startswith("/") or norm == ".." or norm.startswith("../") or ":" in norm.split("/")[0]:
            raise ValueError(f"Path must be relative to the project root: {rel!r}")
        first = norm.split("/")[0]
        if for_write and first in self.RESERVED:
            raise ValueError(f"{first}/ is managed by PatchGoblin and cannot be written.")
        full = self.host.join(self.root, *[p for p in norm.split("/") if p != "."])
        if self.host.kind == "local":
            real_root = os.path.realpath(self.root)
            if os.path.commonpath([real_root, os.path.realpath(full)]) != real_root:
                raise ValueError(f"Path escapes the project: {rel!r}")
        return full


def _clip(text: str, limit: int = 20000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


def _tool(name: str, description: str, **props) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props,
                       "required": list(props), "additionalProperties": False}}}


_S = {"type": "string"}

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def endpoint_key(ep: dict) -> str:
    """The saved key wins; otherwise the named environment variable, if any."""
    if (ep.get("api_key") or "").strip():
        return ep["api_key"].strip()
    env = (ep.get("api_key_env") or "").strip()
    return os.environ.get(env, "").strip() if env else ""


def endpoint_base(ep: dict) -> str:
    base = (ep.get("base_url") or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):  # a pasted full endpoint URL
        base = base[:-len("/chat/completions")].rstrip("/")
    if not re.match(r"^https?://[^/\s]+", base, re.IGNORECASE):
        raise RuntimeError(f"{endpoint_name(ep)}: the base URL must start with http:// or https:// "
                           f"(got {base or 'nothing'}).")
    return base


def endpoint_url(ep: dict, path: str) -> str:
    return endpoint_base(ep) + "/" + path.lstrip("/")


def endpoint_name(ep: dict) -> str:
    return ep.get("name") or ep.get("id") or "API"


def endpoint_headers(ep: dict) -> dict:
    headers = {}
    for name, value in (ep.get("headers") or {}).items():
        if not isinstance(name, str) or not isinstance(value, str) or not name.strip():
            continue
        if any(c in name + value for c in "\r\n"):
            continue
        if name.strip().lower() in ("content-type", "user-agent") or (
                name.strip().lower() == "authorization" and endpoint_key(ep)):
            continue
        headers[name.strip()] = value.strip()
    headers.update({"Content-Type": "application/json", "User-Agent": "PatchGoblin"})
    key = endpoint_key(ep)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _api_error(ep: dict, code: int, detail: str) -> str:
    try:
        data = json.loads(detail)
        err = data.get("error") if isinstance(data, dict) else None
        message = err.get("message") if isinstance(err, dict) else err if isinstance(err, str) else None
        if message:
            detail = message
    except ValueError:
        pass
    return f"{endpoint_name(ep)} API error {code}: {detail.strip()[:1500]}"


def _http(ep: dict, req: urllib.request.Request, timeout: float):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_api_error(ep, exc.code, exc.read().decode("utf-8", "replace"))) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {req.full_url}: {exc.reason}") from exc
    except ValueError as exc:
        raise RuntimeError(f"{endpoint_name(ep)} returned a reply that isn't JSON.") from exc


def list_models(ep: dict) -> list[str]:
    """Model ids from ``{base_url}/models``, keeping only tool-capable ones when the server says."""
    req = urllib.request.Request(endpoint_url(ep, "models"), headers=endpoint_headers(ep))
    data = _http(ep, req, 20)
    if isinstance(data, dict):
        if data.get("error"):
            raise RuntimeError(_api_error(ep, 200, json.dumps(data)))
        data = data.get("data", data.get("models", []))
    if not isinstance(data, list):
        raise RuntimeError(f"{endpoint_name(ep)}: unexpected /models reply.")
    ids = set()
    for entry in data:
        if isinstance(entry, str):
            ids.add(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("id") or entry.get("name"), str):
            params = entry.get("supported_parameters")
            if isinstance(params, list) and "tools" not in params:
                continue
            ids.add(entry.get("id") or entry["name"])
    return sorted(ids)


class OpenAIAgent:
    def __init__(self, host, root: str, cfg: dict, model: str, mode: str, job, tracked: bool = True):
        self.host, self.root, self.cfg, self.mode, self.job = host, root, cfg, mode, job
        self.tracked = tracked
        self.name = endpoint_name(cfg)
        self.model = model or cfg.get("model", "")
        self.files = ProjectFiles(host, root)
        self.allow_commands = mode == "run" and bool(cfg.get("allow_commands"))

    def tools(self) -> list[dict]:
        tools = [
            _tool("list_files", "List project files (respects .gitignore). Optional path prefix filter; "
                  "use '' for all.", prefix=_S),
            _tool("read_file", "Read a UTF-8 text file, path relative to the project root.", path=_S),
            _tool("search", "Search file contents with a regular expression (git grep).", pattern=_S),
        ]
        if self.mode == "run":
            tools.append(_tool("write_file", "Create or overwrite a text file with the full new contents.",
                               path=_S, content=_S))
        if self.allow_commands:
            tools.append(_tool("run_command", "Run a shell command in the project root (5 minute limit). "
                               "Returns exit code and output.", command=_S))
        return tools

    def call_tool(self, name: str, args: dict) -> str:
        if name == "list_files":
            prefix = (args.get("prefix") or "").strip("/")
            names = [n for n in gitops.list_files(self.host, self.root, self.tracked)
                     if not prefix or n.startswith(prefix)]
            return _clip("\n".join(names[:3000]) or "(no files)")
        if name == "read_file":
            text = self.host.read_text(self.files.resolve(args["path"]))
            return "(file not found)" if text is None else _clip(text, 100000)
        if name == "search":
            if self.tracked:
                res = git(self.host, self.root, "grep", "--untracked", "-n", "-I", "-E", "-e", args["pattern"])
            else:
                # --no-index stops git from resolving an enclosing repository.
                res = git(self.host, self.root, "grep", "--no-index", "--exclude-standard",
                         "-n", "-I", "-E", "-e", args["pattern"])
            return _clip(res.stdout) if res.stdout else "(no matches)"
        if name == "write_file" and self.mode == "run":
            self.host.write_text(self.files.resolve(args["path"], for_write=True), args["content"])
            return "ok"
        if name == "run_command" and self.allow_commands:
            res = self.host.run_shell(args["command"], cwd=self.root, timeout=300,
                                      on_output=self.job.write, on_start=self.job.attach)
            if self.job.cancelled:
                raise Cancelled()
            status = "timed out" if res.timed_out else f"exit code {res.returncode}"
            return _clip(f"[{status}]\n{res.stdout}\n{res.stderr}")
        return f"Tool {name} is not available."

    def _request(self, messages: list[dict]) -> dict:
        url = endpoint_url(self.cfg, "chat/completions")
        body = json.dumps({"model": self.model, "messages": messages, "tools": self.tools()}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers=endpoint_headers(self.cfg))
        return _http(self.cfg, req, 600)

    def _missing_key(self) -> str:
        """An error if a key is expected but not available (keyless local servers are fine)."""
        if endpoint_key(self.cfg):
            return ""
        try:
            host = (urllib.parse.urlsplit(endpoint_base(self.cfg)).hostname or "").lower()
        except RuntimeError as exc:
            return str(exc)
        env = (self.cfg.get("api_key_env") or "").strip()
        if host in LOCAL_HOSTS or not env:
            return ""
        return f"{env} is not set in PatchGoblin's environment and no key is saved for {self.name}."

    def run(self, prompt: str, timeout: float) -> Outcome:
        if not self.model:
            return Outcome(False, error=f"No model configured for {self.name} (Settings or project).")
        missing = self._missing_key()
        if missing:
            return Outcome(False, error=missing)
        system = ("You are a careful software engineering agent working through tools on a repository. "
                  "All paths are relative to the project root.")
        if self.mode == "plan":
            system += " You are in read-only planning mode."
        elif not self.allow_commands:
            system += " You cannot run commands; verify by reading code."
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        self.job.write(f"{self.name} agent: model {self.model}, mode {self.mode}\n")
        for step in range(int(self.cfg.get("max_steps", 40))):
            if self.job.cancelled:
                raise Cancelled()
            if self.job.elapsed() > timeout:
                return Outcome(False, error=f"Timed out after {int(timeout)}s.")
            try:
                reply = self._request(messages)
            except RuntimeError as exc:
                return Outcome(False, error=str(exc))
            msg, error = self._message(reply)
            if error:
                return Outcome(False, error=error)
            content = msg.get("content")
            if isinstance(content, list):  # some servers return content parts
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            content = content if isinstance(content, str) else ""
            calls = self._calls(msg.get("tool_calls"), step)
            # Echo back only the standard fields; extras (reasoning, …) confuse some servers.
            echo = {"role": "assistant", "content": content or None}
            if calls:
                echo["tool_calls"] = calls
            messages.append(echo)
            if content:
                self.job.write(content.rstrip() + "\n")
            if not calls:
                text = content.strip()
                return Outcome(bool(text), text, "" if text else "The model returned an empty reply.")
            for call in calls:
                fn = call["function"]
                try:
                    args = json.loads(fn["arguments"] or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must be a JSON object")
                    self.job.write(f"→ {fn['name']} {_clip(json.dumps(args), 200)}\n")
                    result = self.call_tool(fn["name"], args)
                except Cancelled:
                    raise
                except Exception as exc:  # report tool errors back to the model
                    result = f"Error: {exc}"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        return Outcome(False, error="Stopped: reached the maximum number of agent steps.")

    def _message(self, reply) -> tuple[dict, str]:
        """The assistant message from a reply, or an error for replies that carry none."""
        if not isinstance(reply, dict):
            return {}, f"{self.name} returned an unexpected reply."
        if reply.get("error"):
            err = reply["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            return {}, f"{self.name} API error: {message or json.dumps(err)[:1500]}"
        choices = reply.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return {}, f"{self.name} returned no choices: {json.dumps(reply)[:1500]}"
        msg = choices[0].get("message")
        if not isinstance(msg, dict):
            return {}, f"{self.name} returned a choice without a message."
        return msg, ""

    @staticmethod
    def _calls(raw, step: int) -> list[dict]:
        """Normalized tool calls: string arguments and an id on every call."""
        calls = []
        for i, call in enumerate(raw if isinstance(raw, list) else []):
            fn = call.get("function") if isinstance(call, dict) else None
            if not isinstance(fn, dict) or not fn.get("name"):
                continue
            args = fn.get("arguments")
            if isinstance(args, (dict, list)):
                args = json.dumps(args)
            calls.append({"id": call.get("id") or f"call_{step}_{i}", "type": "function",
                          "function": {"name": fn["name"], "arguments": args or "{}"}})
        return calls
