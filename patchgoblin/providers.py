"""AI back ends. Each runs *in the project directory on the project's host*:

* ``claude`` / ``codex`` — the CLI agents, launched in the project directory
  (over SSH for remote projects), with the prompt sent on stdin.
* ``openai`` — a tool-calling loop against an OpenAI-compatible Chat
  Completions API. The model's file and command tools are executed through the
  project's host, so remote projects work without any key on the remote side.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import urllib.error
import urllib.request
from dataclasses import dataclass

from .gitops import git

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
"""

RUN_INSTRUCTIONS = """\
You are implementing a planned task in the project in the current working directory.
Follow the plan, adapting it if the code requires. Keep changes focused on this task.
Do not run git commit or push; the changes are committed automatically when you finish.
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


def plan_questions(plan: str) -> list[str]:
    """The items of the plan's "Questions for you" (or "Open questions") section."""
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
    return [q for q in items if q and not _NONE.match(q)]


def plan_prompt(task: dict, feedback: str = "", answers: list[dict] | None = None) -> str:
    parts = [PLAN_INSTRUCTIONS, f"# Task #{task['id']}: {task['title']}"]
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
    return "\n\n".join(parts) + "\n"


def run_prompt(task: dict) -> str:
    parts = [RUN_INSTRUCTIONS, f"# Task #{task['id']}: {task['title']}"]
    if task.get("description", "").strip():
        parts.append(f"## Description\n{task['description'].strip()}")
    plan = task.get("plan", "").strip() or "(No written plan: use the description.)"
    parts.append(f"## Plan\n{plan}")
    return "\n\n".join(parts) + "\n"


class Cancelled(Exception):
    pass


@dataclass
class Outcome:
    ok: bool
    text: str = ""
    error: str = ""


def run_ai(provider: str, mode: str, prompt: str, *, host, project: dict, settings: dict,
           model: str, job) -> Outcome:
    """Run ``prompt`` with ``provider`` in ``mode`` ("plan" or "run")."""
    timeout = float(settings["timeouts"][mode])
    if provider == "openai":
        return OpenAIAgent(host, project["path"], settings["openai"], model, mode, job).run(prompt, timeout)
    if provider not in settings["commands"]:
        return Outcome(False, error=f"Unknown AI provider: {provider}")
    return run_cli(settings["commands"][provider], mode, prompt, host=host, cwd=project["path"],
                   model=model, timeout=timeout, job=job)


def cli_argv(cfg: dict, mode: str, model: str) -> list[str]:
    template = cfg[mode]
    argv = list(template) if isinstance(template, list) else shlex.split(template)
    if model and cfg.get("model_flag"):
        # Keep a trailing "-" (read prompt from stdin) as the final argument.
        at = len(argv) - 1 if argv and argv[-1] == "-" else len(argv)
        argv[at:at] = [cfg["model_flag"], model]
    return argv


def run_cli(cfg: dict, mode: str, prompt: str, *, host, cwd: str, model: str,
            timeout: float, job) -> Outcome:
    argv = cli_argv(cfg, mode, model)
    job.write(f"$ {' '.join(argv)}\n")
    res = host.run(argv, cwd=cwd, input=prompt, timeout=timeout,
                   on_output=job.write, on_start=job.attach, login=True)
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


class OpenAIAgent:
    def __init__(self, host, root: str, cfg: dict, model: str, mode: str, job):
        self.host, self.root, self.cfg, self.mode, self.job = host, root, cfg, mode, job
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
            res = git(self.host, self.root, "ls-files", "--cached", "--others", "--exclude-standard")
            prefix = (args.get("prefix") or "").strip("/")
            names = [n for n in res.stdout.splitlines() if not prefix or n.startswith(prefix)]
            return _clip("\n".join(names[:3000]) or "(no files)")
        if name == "read_file":
            text = self.host.read_text(self.files.resolve(args["path"]))
            return "(file not found)" if text is None else _clip(text, 100000)
        if name == "search":
            res = git(self.host, self.root, "grep", "--untracked", "-n", "-I", "-E", "-e", args["pattern"])
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
        key = os.environ.get("OPENAI_API_KEY", "")
        url = self.cfg["base_url"].rstrip("/") + "/chat/completions"
        body = json.dumps({"model": self.model, "messages": messages, "tools": self.tools()}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"OpenAI API error {exc.code}: {detail[:1500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach {url}: {exc.reason}") from exc

    def run(self, prompt: str, timeout: float) -> Outcome:
        if not self.model:
            return Outcome(False, error="No OpenAI model configured (Settings or project).")
        if not os.environ.get("OPENAI_API_KEY") and "api.openai.com" in self.cfg["base_url"]:
            return Outcome(False, error="OPENAI_API_KEY is not set in PatchGoblin's environment.")
        system = ("You are a careful software engineering agent working through tools on a repository. "
                  "All paths are relative to the project root.")
        if self.mode == "plan":
            system += " You are in read-only planning mode."
        elif not self.allow_commands:
            system += " You cannot run commands; verify by reading code."
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        self.job.write(f"OpenAI agent: model {self.model}, mode {self.mode}\n")
        for _ in range(int(self.cfg.get("max_steps", 40))):
            if self.job.cancelled:
                raise Cancelled()
            if self.job.elapsed() > timeout:
                return Outcome(False, error=f"Timed out after {int(timeout)}s.")
            try:
                reply = self._request(messages)
            except RuntimeError as exc:
                return Outcome(False, error=str(exc))
            msg = reply["choices"][0]["message"]
            messages.append({k: v for k, v in msg.items() if v is not None})
            if msg.get("content"):
                self.job.write(msg["content"].rstrip() + "\n")
            calls = msg.get("tool_calls") or []
            if not calls:
                text = (msg.get("content") or "").strip()
                return Outcome(bool(text), text, "" if text else "The model returned an empty reply.")
            for call in calls:
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    self.job.write(f"→ {fn['name']} {_clip(json.dumps(args), 200)}\n")
                    result = self.call_tool(fn["name"], args)
                except Cancelled:
                    raise
                except Exception as exc:  # report tool errors back to the model
                    result = f"Error: {exc}"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        return Outcome(False, error="Stopped: reached the maximum number of agent steps.")
