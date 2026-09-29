"""Persistence: the app's own registry/settings, and each project's tasks.json.

Tasks live *inside the project* at ``.patchgoblin/tasks.json`` (on the SSH
host for remote projects), so they travel with the repository and are
committed to its git history alongside the work they describe.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone

from .hosts import host_for

STATUSES = ("unplanned", "planning", "drafted", "planned", "queued", "running", "review", "done", "failed")
CLI_PROVIDERS = ("claude", "codex", "opencode", "cline")
CLI_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "opencode", "cline": "Cline"}
# Suggestions for the model dropdowns; any other model name can still be entered as "Custom…".
# "openai" is only used for the built-in endpoint with that id. opencode's models come from
# each project's opencode config (see opencode.py), and Cline's from whichever provider the
# user set up with `cline auth`, so neither has a fixed list.
MODELS = {
    "claude": ("opus", "sonnet", "haiku", "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5",
               "claude-haiku-4-5"),
    "codex": ("gpt-5-codex", "gpt-5", "gpt-5-mini"),
    "opencode": (),
    "cline": (),
    "openai": ("gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4.1"),
}
TASKS_DIR = ".patchgoblin"
TASKS_FILE = "tasks.json"

DEFAULT_SETTINGS = {
    "commands": {
        "claude": {
            "plan": "claude -p --output-format text --allowedTools Read,Glob,Grep "
                    "--disallowedTools Edit,Write,NotebookEdit,Bash",
            "run": "claude -p --output-format text --permission-mode acceptEdits "
                   "--allowedTools Read,Glob,Grep,Edit,Write,Bash",
            "model_flag": "--model",
            "plan_model": "",
            "code_model": "",
        },
        "codex": {
            "plan": "codex exec --sandbox read-only --color never -",
            "run": "codex exec --sandbox workspace-write --color never -",
            "model_flag": "-m",
            "plan_model": "",
            "code_model": "",
        },
        "opencode": {
            # {agent} is replaced by plan_agent (planning and chat) or run_agent (runs).
            "plan": "opencode run --agent {agent}",
            "run": "opencode run --agent {agent}",
            "plan_agent": "plan",
            "run_agent": "build",
            "model_flag": "-m",
            "plan_model": "",
            "code_model": "",
            "require_agents": True,  # refuse to start if a custom agent isn't defined
            "plan_must_not_edit": True,  # fail a plan (or chat) that changed files
        },
        "cline": {
            # Plan mode (-p) blocks file edits but can still run commands; act mode is the default.
            "plan": "cline -p --auto-approve true",
            "run": "cline --auto-approve true",
            "model_flag": "-m",
            "plan_model": "",
            "code_model": "",
            "plan_must_not_edit": True,  # fail a plan (or chat) that changed files
        },
    },
    "endpoints": [{
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-5",
    }],
    "timeouts": {"plan": 900, "run": 3600},
    "automation": {"auto_plan": False, "auto_queue": False},
}

# Automation modes: a global default in Settings, overridden per project by True/False
# (None or missing inherits). Each maps to the status of the tasks it acts on when turned on.
AUTO_MODES = ("auto_plan", "auto_queue")
AUTO_TARGETS = {"auto_plan": "unplanned", "auto_queue": "planned"}


def resolve_auto(project: dict, settings: dict, key: str) -> bool:
    """A project's effective automation mode: its own True/False, else the global default."""
    value = project.get(key)
    if isinstance(value, bool):
        return value
    return (settings.get("automation") or {}).get(key) is True

# An OpenAI-compatible Chat Completions endpoint; each one is its own provider, by id.
DEFAULT_ENDPOINT = {
    "name": "",
    "base_url": "",
    "api_key": "",
    "api_key_env": "",
    "headers": {},
    "model": "",  # the endpoint's default planning model
    "code_model": "",  # its default coding model; blank means the same as "model"
    "models": [],
    "allow_commands": False,
    "max_steps": 40,
}


def _endpoint(ep: dict) -> dict:
    out = _merge(DEFAULT_ENDPOINT, ep)
    if not isinstance(out["models"], list):
        out["models"] = []
    out["models"] = [m for m in out["models"] if isinstance(m, str) and m.strip()]
    if not isinstance(out["headers"], dict):
        out["headers"] = {}
    out["headers"] = {k: v for k, v in out["headers"].items() if isinstance(k, str) and isinstance(v, str)}
    return out


def endpoint_ids(settings: dict) -> list[str]:
    return [ep["id"] for ep in settings.get("endpoints", [])]


def find_endpoint(settings: dict, pid: str) -> dict | None:
    return next((ep for ep in settings.get("endpoints", []) if ep.get("id") == pid), None)


def valid_provider(settings: dict, pid) -> bool:
    return isinstance(pid, str) and (pid in CLI_PROVIDERS or find_endpoint(settings, pid) is not None)


def global_model(settings: dict, provider: str, key: str) -> str:
    """The Settings default for a provider's ``plan_model`` or ``code_model`` ("" if none)."""
    if provider in CLI_PROVIDERS:
        value = settings.get("commands", {}).get(provider, {}).get(key, "")
        return value if isinstance(value, str) else ""
    ep = find_endpoint(settings, provider)
    if ep is None:
        return ""
    if key == "code_model":
        return ep.get("code_model") or ep.get("model") or ""
    return ep.get("model") or ""


def provider_choices(settings: dict) -> list[dict]:
    """Every selectable AI: the CLIs, then each endpoint by its display name."""
    return ([{"id": p, "name": CLI_NAMES[p]} for p in CLI_PROVIDERS]
            + [{"id": ep["id"], "name": ep.get("name") or ep["id"]} for ep in settings.get("endpoints", [])])


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class JsonFile:
    """A small local JSON file owned by this app process."""

    def __init__(self, path: str, default):
        self.path = path
        self.default = default
        self.lock = threading.RLock()

    def load(self):
        with self.lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except FileNotFoundError:
                return copy.deepcopy(self.default)

    def save(self, data) -> None:
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.path)


class Registry:
    """Which projects this PatchGoblin instance knows about and how to reach them."""

    def __init__(self, data_dir: str):
        self.file = JsonFile(os.path.join(data_dir, "projects.json"), {"projects": []})
        with self.file.lock:
            self._migrate()

    def _migrate(self) -> None:
        """Split the old single project ``model`` into planning and coding models (once)."""
        data = self.file.load()
        changed = False
        for p in data.get("projects", []):
            if isinstance(p, dict) and "model" in p and "plan_model" not in p:
                model = p.pop("model") or ""
                p["plan_model"] = p["code_model"] = model
                p.setdefault("chat_model", "")
                changed = True
        if not changed:
            return
        backup = self.file.path + ".bak"
        if not os.path.exists(backup):
            with open(self.file.path, "rb") as src, open(backup, "wb") as dst:
                dst.write(src.read())
        self.file.save(data)

    def list(self) -> list[dict]:
        return self.file.load()["projects"]

    def get(self, pid: str) -> dict | None:
        return next((p for p in self.list() if p["id"] == pid), None)

    def add(self, project: dict) -> dict:
        with self.file.lock:
            data = self.file.load()
            project = {**project, "id": uuid.uuid4().hex[:10], "created_at": now()}
            data["projects"].append(project)
            self.file.save(data)
            return project

    def update(self, pid: str, fields: dict) -> dict | None:
        with self.file.lock:
            data = self.file.load()
            for p in data["projects"]:
                if p["id"] == pid:
                    p.update(fields)
                    self.file.save(data)
                    return p
            return None

    def rename_provider(self, old: str, new: str) -> None:
        """Point projects using provider ``old`` at ``new`` (after an endpoint id rename)."""
        with self.file.lock:
            data = self.file.load()
            projects = [p for p in data["projects"] if p.get("provider") == old]
            for p in projects:
                p["provider"] = new
            if projects:
                self.file.save(data)

    def remove(self, pid: str) -> bool:
        with self.file.lock:
            data = self.file.load()
            kept = [p for p in data["projects"] if p["id"] != pid]
            self.file.save({**data, "projects": kept})
            return len(kept) != len(data["projects"])


def rename_reserved_endpoints(endpoints: list) -> tuple[list, dict]:
    """Give endpoints whose id is now a CLI provider's a free new id (``opencode`` →
    ``opencode-api``, or ``opencode-api-2`` if that is taken). Returns (endpoints, {old: new})."""
    taken = {ep.get("id") for ep in endpoints if isinstance(ep, dict)}
    out, renames = [], {}
    for ep in endpoints:
        if isinstance(ep, dict) and ep.get("id") in CLI_PROVIDERS:
            base = new = f"{ep['id']}-api"
            n = 2
            while new in taken or new in CLI_PROVIDERS:
                new = f"{base}-{n}"
                n += 1
            taken.add(new)
            renames[ep["id"]] = new
            ep = {**ep, "id": new}
        out.append(ep)
    return out, renames


def rename_task_providers(doc: dict, renames: dict) -> bool:
    """Point tasks saved before an id became a CLI's (tasks.json version < RESERVED_SINCE[id])
    at their endpoint's new id. Changes ``doc`` in place; True if any task changed."""
    version = doc.get("version") or 1
    changed = False
    for task in doc.get("tasks", []):
        old = task.get("provider")
        if old in renames and version < RESERVED_SINCE.get(old, DOC_VERSION):
            task["provider"] = renames[task["provider"]]
            changed = True
    return changed


class Settings:
    def __init__(self, data_dir: str):
        self.file = JsonFile(os.path.join(data_dir, "settings.json"), {})
        with self.file.lock:
            self.renamed = self._migrate()

    def _migrate(self) -> dict:
        """Rename saved endpoints whose id became a CLI provider (once). Returns {old: new}.

        Every rename is also kept in ``provider_renames`` so tasks.json files saved before it
        (possibly on an offline SSH host, or pulled in later) can be pointed at the new id.
        """
        data = self.file.load()
        endpoints = data.get("endpoints")
        if not isinstance(endpoints, list):
            return {}
        endpoints, renames = rename_reserved_endpoints(endpoints)
        if renames:
            data["endpoints"] = endpoints
            data["provider_renames"] = {**(data.get("provider_renames") or {}), **renames}
            self.file.save(data)
        return renames

    def get(self) -> dict:
        saved = self.file.load()
        legacy = saved.get("openai")
        if "endpoints" not in saved and isinstance(legacy, dict):
            # Before named endpoints there was a single "openai" block; keep its id.
            saved = {**saved, "endpoints": [{**DEFAULT_SETTINGS["endpoints"][0], **legacy, "id": "openai"}]}
        saved.pop("openai", None)
        out = _merge(DEFAULT_SETTINGS, saved)
        endpoints = out["endpoints"] if isinstance(out["endpoints"], list) else []
        out["endpoints"] = [_endpoint(ep) for ep in endpoints if isinstance(ep, dict) and ep.get("id")]
        if not isinstance(out.get("provider_renames"), dict):
            out["provider_renames"] = {}
        return out

    def update(self, values: dict) -> dict:
        with self.file.lock:
            self.file.save(_merge(self.file.load(), values))
        return self.get()

    def save_endpoints(self, endpoints: list[dict]) -> dict:
        """Replace the whole endpoint list (``_merge`` would replace lists anyway)."""
        with self.file.lock:
            data = self.file.load()
            data.pop("openai", None)
            data["endpoints"] = endpoints
            self.file.save(data)
        return self.get()


# tasks.json format. 2: the 'drafted' status exists (version-1 files are migrated by
# Engine.reconcile; every write stamps the current version). 3: the 'review' status exists.
# 4: a task provider "opencode" means the opencode CLI; older files meant an endpoint with that
# id, which was renamed (TaskStore applies Settings' provider_renames when loading them).
# 5: likewise for "cline" (the Cline CLI).
DOC_VERSION = 5
# The tasks.json version from which each CLI id means the CLI rather than an endpoint.
RESERVED_SINCE = {"opencode": 4, "cline": 5}


def empty_doc() -> dict:
    return {"version": DOC_VERSION, "next_id": 1, "tasks": []}


def tasks_path(project: dict, host=None) -> str:
    host = host or host_for(project)
    return host.join(project["path"], TASKS_DIR, TASKS_FILE)


class TaskStore:
    """Read-modify-write access to each project's tasks.json, serialized per project."""

    CACHE_SECONDS = 2.0

    def __init__(self, provider_renames: dict | None = None):
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self._cache: dict[str, tuple[float, dict]] = {}
        # Endpoint ids renamed by Settings; applied to documents saved before the rename.
        self.provider_renames = dict(provider_renames or {})

    def _load(self, project: dict) -> dict:
        text = host_for(project).read_text(tasks_path(project))
        if not text or not text.strip():
            return empty_doc()
        doc = json.loads(text)
        doc.setdefault("tasks", [])
        doc.setdefault("next_id", max((t["id"] for t in doc["tasks"]), default=0) + 1)
        rename_task_providers(doc, self.provider_renames)
        return doc

    def read(self, project: dict, fresh: bool = False) -> dict:
        cached = self._cache.get(project["id"])
        if cached and not fresh and time.monotonic() - cached[0] < self.CACHE_SECONDS:
            return copy.deepcopy(cached[1])
        with self._locks[project["id"]]:
            doc = self._load(project)
            self._cache[project["id"]] = (time.monotonic(), doc)
            return copy.deepcopy(doc)

    def write(self, project: dict, doc: dict) -> None:
        doc["version"] = max(doc.get("version") or 1, DOC_VERSION)
        host_for(project).write_text(tasks_path(project), json.dumps(doc, indent=2) + "\n")
        self._cache[project["id"]] = (time.monotonic(), copy.deepcopy(doc))

    @contextmanager
    def edit(self, project: dict):
        """Yield the current document; it is saved if the block exits normally."""
        with self._locks[project["id"]]:
            doc = self._load(project)
            yield doc
            self.write(project, doc)

    def lock(self, pid: str) -> threading.RLock:
        """The lock serializing tasks.json edits (held by git sync while it changes the tree)."""
        return self._locks[pid]

    def forget(self, pid: str) -> None:
        self._cache.pop(pid, None)


def find_task(doc: dict, tid: int) -> dict | None:
    return next((t for t in doc["tasks"] if t["id"] == tid), None)


def new_task(doc: dict, title: str, description: str = "", provider: str = "",
             plan_model: str = "", code_model: str = "", plan_trust: str = "") -> dict:
    ts = now()
    task = {
        "id": doc["next_id"],
        "title": title,
        "description": description,
        "plan": "",
        "status": "unplanned",
        "provider": provider,
        "plan_model": plan_model,
        "code_model": code_model,
        "plan_trust": plan_trust,  # "" = the project's level
        "created_at": ts,
        "updated_at": ts,
        "queued_at": None,
        "started_at": None,
        "finished_at": None,
        "output": "",
        "error": "",
        "commit": "",
        "review_feedback": "",  # older tasks lack it; read with .get()
        "history": [],
    }
    doc["next_id"] += 1
    doc["tasks"].append(task)
    log_event(task, "Created")
    return task


def log_event(task: dict, message: str) -> None:
    task["updated_at"] = now()
    task["history"].append({"at": task["updated_at"], "event": message})
    del task["history"][:-100]


def set_status(task: dict, status: str, message: str) -> None:
    assert status in STATUSES
    task["status"] = status
    log_event(task, message)
