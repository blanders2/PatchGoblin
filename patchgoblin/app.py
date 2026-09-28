"""Flask app: JSON API plus a single-page UI."""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request

from . import gitops
from .engine import Engine
from .hosts import HostError, host_for, open_terminal
from .store import (PROVIDERS, STATUSES, Registry, Settings, TaskStore, empty_doc, find_task,
                    log_event, new_task, now, set_status, tasks_path)

EDITABLE = ("title", "description", "plan", "provider")
LOCKED = ("planning", "running")


def create_app(data_dir: str | None = None, start_engine: bool = True) -> Flask:
    app = Flask(__name__)
    data_dir = data_dir or os.environ.get("PATCHGOBLIN_DATA") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    registry, settings, store = Registry(data_dir), Settings(data_dir), TaskStore()
    engine = Engine(registry, store, settings)
    app.config.update(REGISTRY=registry, SETTINGS=settings, STORE=store, ENGINE=engine)

    # ---- request guards ---------------------------------------------------
    @app.before_request
    def guard():
        # Local-only tool: refuse foreign Host headers (DNS rebinding) and require a
        # custom header on writes, which cross-site pages cannot send without CORS.
        host = (request.host or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            abort(403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("X-PatchGoblin") != "1":
                abort(403)
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                abort(403)

    @app.errorhandler(HostError)
    def host_error(exc):
        return jsonify(error=str(exc)), 502

    @app.errorhandler(ValueError)
    def value_error(exc):
        return jsonify(error=str(exc)), 400

    def body() -> dict:
        return request.get_json(silent=True) or {}

    def project_or_404(pid: str) -> dict:
        project = registry.get(pid)
        if project is None:
            abort(404)
        return project

    def task_view(pid: str, task: dict) -> dict:
        job = engine.job(pid, task["id"])
        return {**task, "active": job is not None}

    # ---- pages --------------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html", statuses=STATUSES, providers=PROVIDERS)

    # ---- settings -----------------------------------------------------------
    @app.get("/api/settings")
    def get_settings():
        return jsonify(settings.get() | {"openai_key_present": bool(os.environ.get("OPENAI_API_KEY"))})

    @app.put("/api/settings")
    def put_settings():
        data = body()
        allowed = {k: data[k] for k in ("commands", "openai", "timeouts") if k in data}
        return jsonify(settings.update(allowed))

    # ---- projects -----------------------------------------------------------
    @app.get("/api/projects")
    def list_projects():
        return jsonify(projects=registry.list())

    def project_fields(data: dict) -> dict:
        provider = data.get("provider") or "claude"
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider {provider}.")
        location = data.get("location") or "local"
        fields = {
            "name": (data.get("name") or "").strip(),
            "location": location,
            "ssh_target": (data.get("ssh_target") or "").strip() if location == "ssh" else "",
            "ssh_port": int(data["ssh_port"]) if location == "ssh" and data.get("ssh_port") else None,
            "provider": provider,
            "model": (data.get("model") or "").strip(),
        }
        return fields

    @app.post("/api/browse")
    def browse():
        """List folders on the machine a project would live on (for the folder picker)."""
        data = body()
        host = host_for(project_fields(data))
        path = (data.get("path") or "").strip()
        if data.get("home") or (not path and not (os.name == "nt" and host.kind == "local")):
            path = host.home()
        return jsonify(host.list_dirs(path))

    @app.post("/api/projects")
    def add_project():
        data = body()
        fields = project_fields(data)
        host = host_for(fields)
        if fields["location"] == "ssh":
            host.check()
        path = host.normalize(data.get("path") or "")
        fields["path"] = path
        fields["name"] = fields["name"] or path.replace("\\", "/").rstrip("/").split("/")[-1] or path
        if any(p["location"] == fields["location"] and p.get("ssh_target") == fields["ssh_target"]
               and p["path"] == path for p in registry.list()):
            raise ValueError("That project is already registered.")
        if not host.is_dir(path):
            if not data.get("create", True):
                raise ValueError(f"Directory does not exist: {path}")
            host.ensure_dir(path)
        created = gitops.ensure_repo(host, path)
        tpath = tasks_path(fields, host)
        if host.read_text(tpath) is None:
            host.write_text(tpath, json.dumps(empty_doc(), indent=2) + "\n")
        if created:
            gitops.commit_all(host, path, "PatchGoblin: initial commit")
        return jsonify(registry.add(fields)), 201

    @app.patch("/api/projects/<pid>")
    def update_project(pid):
        project = project_or_404(pid)
        data = body()
        fields = {}
        if "name" in data and data["name"].strip():
            fields["name"] = data["name"].strip()
        if "provider" in data:
            if data["provider"] not in PROVIDERS:
                raise ValueError("Unknown provider.")
            fields["provider"] = data["provider"]
        if "model" in data:
            fields["model"] = (data["model"] or "").strip()
        return jsonify(registry.update(project["id"], fields))

    @app.delete("/api/projects/<pid>")
    def remove_project(pid):
        project_or_404(pid)
        if any(key[0] == pid for key in engine.jobs):
            raise ValueError("Wait for this project's AI jobs to finish first.")
        registry.remove(pid)
        store.forget(pid)
        return jsonify(ok=True)

    @app.get("/api/projects/<pid>/commits")
    def commits(pid):
        project = project_or_404(pid)
        return jsonify(commits=gitops.recent_commits(host_for(project), project["path"]))

    @app.post("/api/projects/<pid>/terminal")
    def terminal(pid):
        open_terminal(project_or_404(pid))
        return jsonify(ok=True)

    # ---- tasks --------------------------------------------------------------
    @app.get("/api/projects/<pid>/tasks")
    def list_tasks(pid):
        project = project_or_404(pid)
        engine.reconcile(project)
        doc = store.read(project)
        return jsonify(tasks=[task_view(pid, t) for t in doc["tasks"]])

    @app.post("/api/projects/<pid>/tasks")
    def create_task(pid):
        project = project_or_404(pid)
        data = body()
        title = (data.get("title") or "").strip()
        if not title:
            raise ValueError("A task needs a title.")
        provider = data.get("provider") or ""
        if provider and provider not in PROVIDERS:
            raise ValueError("Unknown provider.")
        with store.edit(project) as doc:
            task = new_task(doc, title, (data.get("description") or "").strip(), provider)
        return jsonify(task_view(pid, task)), 201

    @app.patch("/api/projects/<pid>/tasks/<int:tid>")
    def update_task(pid, tid):
        project = project_or_404(pid)
        data = body()
        with store.edit(project) as doc:
            task = find_task(doc, tid) or abort(404)
            if task["status"] in LOCKED:
                raise ValueError(f"Task is {task['status']}; wait or cancel first.")
            changed = [k for k in EDITABLE if k in data and data[k] != task[k]]
            if "provider" in changed and data["provider"] and data["provider"] not in PROVIDERS:
                raise ValueError("Unknown provider.")
            if "title" in changed and not str(data["title"]).strip():
                raise ValueError("A task needs a title.")
            for key in changed:
                task[key] = data[key].strip() if key == "title" else data[key]
            if changed:
                log_event(task, "Edited " + ", ".join(changed))
        return jsonify(task_view(pid, task))

    @app.delete("/api/projects/<pid>/tasks/<int:tid>")
    def delete_task(pid, tid):
        project = project_or_404(pid)
        with store.edit(project) as doc:
            task = find_task(doc, tid) or abort(404)
            if task["status"] in LOCKED:
                raise ValueError(f"Task is {task['status']}; cancel it first.")
            doc["tasks"].remove(task)
        return jsonify(ok=True)

    # Simple state changes: action -> (allowed from, new status, history message)
    TRANSITIONS = {
        "mark_planned": (("unplanned", "failed"), "planned", "Marked planned"),
        "unplan": (("planned",), "unplanned", "Moved back to unplanned"),
        "queue": (("planned", "failed"), "queued", "Queued for AI"),
        "dequeue": (("queued",), "planned", "Removed from queue"),
        "reopen": (("done",), "planned", "Reopened"),
    }

    @app.post("/api/projects/<pid>/tasks/<int:tid>/action")
    def task_action(pid, tid):
        project = project_or_404(pid)
        data = body()
        action = data.get("action")
        if action == "plan":
            engine.start_planning(project, tid, data.get("feedback") or "")
        elif action == "cancel":
            if not engine.cancel(pid, tid):
                raise ValueError("No AI job is running for this task.")
        elif action in TRANSITIONS:
            allowed, status, message = TRANSITIONS[action]
            with store.edit(project) as doc:
                task = find_task(doc, tid) or abort(404)
                if task["status"] not in allowed:
                    raise ValueError(f"Cannot {action.replace('_', ' ')} a task that is {task['status']}.")
                if action == "queue":
                    task["queued_at"] = now()
                set_status(task, status, message)
            if action == "queue":
                engine.kick(pid)
        else:
            raise ValueError(f"Unknown action {action!r}.")
        doc = store.read(project, fresh=True)
        return jsonify(task_view(pid, find_task(doc, tid) or abort(404)))

    @app.get("/api/projects/<pid>/tasks/<int:tid>/live")
    def live(pid, tid):
        job = engine.job(pid, tid)
        return jsonify(active=job is not None, kind=job.kind if job else None,
                       output=job.text() if job else "",
                       elapsed=round(job.elapsed()) if job else 0)

    if start_engine:
        engine.startup()
    return app
