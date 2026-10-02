"""The OpenAI-compatible HTTP back end: a tool-calling loop against a Chat Completions API.
The model's file and command tools are executed through the project's host, so remote projects
work without any key on the remote side."""
from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request

from . import gitops
from .gitops import git
from .hosts import HostError
from .prompts import IMAGE_MIME, image_refs
from .providers import Cancelled, Outcome, clip_head, describe_tool


class ProjectFiles:
    """File access confined to the project root (used by the OpenAI agent)."""

    RESERVED = (".git", ".svn", ".patchgoblin")

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


def valid_base_url(url: str) -> bool:
    return bool(re.match(r"^https?://[^/\s]+", url, re.IGNORECASE))


def valid_header(name, value) -> bool:
    """A usable custom header: string name and value, non-blank name, no line breaks."""
    return (isinstance(name, str) and isinstance(value, str) and bool(name.strip())
            and not any(c in name + value for c in "\r\n"))


def endpoint_base(ep: dict) -> str:
    base = (ep.get("base_url") or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):  # a pasted full endpoint URL
        base = base[:-len("/chat/completions")].rstrip("/")
    if not valid_base_url(base):
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
        if not valid_header(name, value):
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
            shell = "PowerShell command" if getattr(self.host, "os", "") == "windows" else "shell command"
            tools.append(_tool("run_command", f"Run a {shell} in the project root (5 minute limit). "
                               "Returns exit code and output.", command=_S))
        return tools

    def call_tool(self, name: str, args: dict) -> str:
        if name == "list_files":
            prefix = (args.get("prefix") or "").strip("/")
            names = [n for n in gitops.list_files(self.host, self.root, self.tracked)
                     if not prefix or n.startswith(prefix)]
            return clip_head("\n".join(names[:3000]) or "(no files)")
        if name == "read_file":
            text = self.host.read_text(self.files.resolve(args["path"]))
            return "(file not found)" if text is None else clip_head(text, 100000)
        if name == "search":
            if self.tracked:
                res = git(self.host, self.root, "grep", "--untracked", "-n", "-I", "-E", "-e", args["pattern"])
            else:
                # --no-index stops git from resolving an enclosing repository.
                res = git(self.host, self.root, "grep", "--no-index", "--exclude-standard",
                         "-n", "-I", "-E", "-e", args["pattern"])
            return clip_head(res.stdout) if res.stdout else "(no matches)"
        if name == "write_file" and self.mode == "run":
            self.host.write_text(self.files.resolve(args["path"], for_write=True), args["content"])
            return "ok"
        if name == "run_command" and self.allow_commands:
            res = self.host.run_shell(args["command"], cwd=self.root, timeout=300,
                                      on_output=self.job.write, on_start=self.job.attach)
            if self.job.cancelled:
                raise Cancelled()
            status = "timed out" if res.timed_out else f"exit code {res.returncode}"
            return clip_head(f"[{status}]\n{res.stdout}\n{res.stderr}")
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

    def _user_content(self, prompt: str):
        """The first user message: plain text, or text plus inline images the prompt references."""
        parts = []
        for path in image_refs(prompt):
            try:
                data = self.host.read_bytes(self.files.resolve(path))
            except (ValueError, HostError, OSError):
                data = None
            if data:
                mime = IMAGE_MIME[path.rsplit(".", 1)[1]]
                url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
        if not parts:
            return prompt
        return [{"type": "text", "text": prompt}, *parts]

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
        messages = [{"role": "system", "content": system}, {"role": "user", "content": self._user_content(prompt)}]
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
                self.job.event("say", content.strip(), content.strip() if len(content) > 200 else "")
            if not calls:
                text = content.strip()
                return Outcome(bool(text), text, "" if text else "The model returned an empty reply.")
            for call in calls:
                fn = call["function"]
                try:
                    args = json.loads(fn["arguments"] or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must be a JSON object")
                    self.job.event("tool", *describe_tool(fn["name"], args))
                    result = self.call_tool(fn["name"], args)
                except Cancelled:
                    raise
                except Exception as exc:  # report tool errors back to the model
                    result = f"Error: {exc}"
                    self.job.event("error", clip_head(f"{fn['name']}: {exc}", 300))
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
