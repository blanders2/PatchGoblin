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

STATUSES = ("unplanned", "planning", "planned", "queued", "running", "done", "failed")
PROVIDERS = ("claude", "codex", "openai")
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
        },
        "codex": {
            "plan": "codex exec --sandbox read-only --color never -",
            "run": "codex exec --sandbox workspace-write --color never -",
            "model_flag": "-m",
        },
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5",
        "allow_commands": False,
        "max_steps": 40,
    },
    "timeouts": {"plan": 900, "run": 3600},
}


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

    def remove(self, pid: str) -> bool:
        with self.file.lock:
            data = self.file.load()
            kept = [p for p in data["projects"] if p["id"] != pid]
            self.file.save({**data, "projects": kept})
            return len(kept) != len(data["projects"])


class Settings:
    def __init__(self, data_dir: str):
        self.file = JsonFile(os.path.join(data_dir, "settings.json"), {})

    def get(self) -> dict:
        return _merge(DEFAULT_SETTINGS, self.file.load())

    def update(self, values: dict) -> dict:
        with self.file.lock:
            self.file.save(_merge(self.file.load(), values))
        return self.get()


def empty_doc() -> dict:
    return {"version": 1, "next_id": 1, "tasks": []}


def tasks_path(project: dict, host=None) -> str:
    host = host or host_for(project)
    return host.join(project["path"], TASKS_DIR, TASKS_FILE)


class TaskStore:
    """Read-modify-write access to each project's tasks.json, serialized per project."""

    CACHE_SECONDS = 2.0

    def __init__(self):
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self._cache: dict[str, tuple[float, dict]] = {}

    def _load(self, project: dict) -> dict:
        text = host_for(project).read_text(tasks_path(project))
        if not text or not text.strip():
            return empty_doc()
        doc = json.loads(text)
        doc.setdefault("tasks", [])
        doc.setdefault("next_id", max((t["id"] for t in doc["tasks"]), default=0) + 1)
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
        host_for(project).write_text(tasks_path(project), json.dumps(doc, indent=2) + "\n")
        self._cache[project["id"]] = (time.monotonic(), copy.deepcopy(doc))

    @contextmanager
    def edit(self, project: dict):
        """Yield the current document; it is saved if the block exits normally."""
        with self._locks[project["id"]]:
            doc = self._load(project)
            yield doc
            self.write(project, doc)

    def forget(self, pid: str) -> None:
        self._cache.pop(pid, None)


def find_task(doc: dict, tid: int) -> dict | None:
    return next((t for t in doc["tasks"] if t["id"] == tid), None)


def new_task(doc: dict, title: str, description: str = "", provider: str = "") -> dict:
    ts = now()
    task = {
        "id": doc["next_id"],
        "title": title,
        "description": description,
        "plan": "",
        "status": "unplanned",
        "provider": provider,
        "created_at": ts,
        "updated_at": ts,
        "queued_at": None,
        "started_at": None,
        "finished_at": None,
        "output": "",
        "error": "",
        "commit": "",
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
