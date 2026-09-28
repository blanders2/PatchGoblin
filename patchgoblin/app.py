"""Flask app: JSON API plus a single-page UI."""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request

from . import gitops
from .engine import Engine
from .hosts import HostError, host_for, open_terminal
from .providers import plan_questions
from .store import (MODELS, PROVIDERS, STATUSES, Registry, Settings, TaskStore, empty_doc, find_task,
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
        return {**task, "active": job is not None, "questions": plan_questions(task.get("plan", ""))}

    def plan_answers(value) -> list[dict] | None:
        if value is None:
            return None
        if not isinstance(value, list) or not all(
                isinstance(a, dict) and isinstance(a.get("question", ""), str)
                and isinstance(a.get("answer", ""), str) for a in value):
            raise ValueError("answers must be a list of {question, answer} strings.")
        answers = [{"question": a.get("question", ""), "answer": a.get("answer", "")}
                   for a in value if a.get("answer", "").strip()]
        return answers or None

    # ---- pages --------------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html", statuses=STATUSES, providers=PROVIDERS, models=MODELS)

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
        if "plan_limit" in data:
            fields["plan_limit"] = plan_limit(data["plan_limit"])
        if "rewrite_titles" in data:
            fields["rewrite_titles"] = bool(data["rewrite_titles"])
        if "auto_sync" in data:
            fields["auto_sync"] = bool(data["auto_sync"])
        if "sync_mode" in data:
            fields["sync_mode"] = sync_mode(data["sync_mode"])
        return jsonify(registry.update(project["id"], fields))

    def plan_limit(value) -> int:
        """Max simultaneous planning jobs for a project; 0 means unlimited."""
        if value is None or value == "":
            return 0
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("Plan limit must be a whole number ≥ 0.")
        try:
            limit = int(value)
        except ValueError:
            raise ValueError("Plan limit must be a whole number ≥ 0.") from None
        if limit < 0:
            raise ValueError("Plan limit must be a whole number ≥ 0.")
        return limit

    def sync_mode(value) -> str:
        if value not in gitops.SYNC_MODES:
            raise ValueError("Sync mode must be one of: " + ", ".join(gitops.SYNC_MODES) + ".")
        return value

    @app.delete("/api/projects/<pid>")
    def remove_project(pid):
        project_or_404(pid)
        if engine.busy(pid):
            raise ValueError("Wait for this project's AI jobs to finish first.")
        registry.remove(pid)
        store.forget(pid)
        engine.chats.pop(pid, None)
        return jsonify(ok=True)

    @app.get("/api/projects/<pid>/commits")
    def commits(pid):
        project = project_or_404(pid)
        return jsonify(commits=gitops.recent_commits(host_for(project), project["path"]))

    def remote_view(project: dict, status: dict) -> dict:
        return {**status, "auto_sync": project.get("auto_sync") is True,
                "sync_mode": project.get("sync_mode") or "ff-only"}

    @app.get("/api/projects/<pid>/remote")
    def get_remote(pid):
        project = project_or_404(pid)
        return jsonify(remote_view(project, gitops.remote_status(host_for(project), project["path"])))

    @app.put("/api/projects/<pid>/remote")
    def put_remote(pid):
        project = project_or_404(pid)
        url = body().get("url") or ""
        if not isinstance(url, str):
            raise ValueError("url must be a string.")
        host = host_for(project)
        gitops.set_remote(host, project["path"], url)
        return jsonify(remote_view(project, gitops.remote_status(host, project["path"])))

    @app.post("/api/projects/<pid>/remote/sync")
    def sync_remote(pid):
        project = project_or_404(pid)
        data = body()
        mode = sync_mode(data.get("mode") or project.get("sync_mode") or "ff-only")
        if engine.busy(pid) or any(t["status"] in LOCKED for t in store.read(project, fresh=True)["tasks"]):
            raise ValueError("Wait for this project's AI jobs to finish before syncing.")
        result = engine.sync(project, mode, push=data.get("push", True) is not False)
        return jsonify(remote_view(project, result))

    @app.post("/api/projects/<pid>/terminal")
    def terminal(pid):
        open_terminal(project_or_404(pid))
        return jsonify(ok=True)

    # ---- chat ---------------------------------------------------------------
    def chat_view(pid: str) -> dict:
        chat = engine.chat(pid)
        job = chat.job
        return {"messages": list(chat.messages), "active": job is not None,
                "output": job.text() if job else "", "elapsed": round(job.elapsed()) if job else 0}

    @app.get("/api/projects/<pid>/chat")
    def get_chat(pid):
        project_or_404(pid)
        return jsonify(chat_view(pid))

    @app.post("/api/projects/<pid>/chat")
    def send_chat(pid):
        project = project_or_404(pid)
        text = (body().get("message") or "").strip()
        if not text:
            raise ValueError("Type a message first.")
        engine.send_chat(project, text)
        return jsonify(chat_view(pid))

    @app.post("/api/projects/<pid>/chat/cancel")
    def cancel_chat(pid):
        project_or_404(pid)
        job = engine.chat(pid).job
        if job is None:
            raise ValueError("The AI is not replying.")
        job.cancel()
        return jsonify(chat_view(pid))

    @app.delete("/api/projects/<pid>/chat")
    def clear_chat(pid):
        project_or_404(pid)
        engine.clear_chat(pid)
        return jsonify(chat_view(pid))

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

    def transition(task: dict, action: str, queued_at: str | None = None) -> None:
        allowed, status, message = TRANSITIONS[action]
        if task["status"] not in allowed:
            raise ValueError(f"Cannot {action.replace('_', ' ')} a task that is {task['status']}.")
        if action == "queue":
            task["queued_at"] = queued_at or now()
        set_status(task, status, message)

    def apply_action(project: dict, tid: int, action: str, data: dict) -> None:
        """One task's action; raises KeyError for a missing task, ValueError if not allowed."""
        pid = project["id"]
        if action == "plan":
            engine.start_planning(project, tid, data.get("feedback") or "", plan_answers(data.get("answers")))
        elif action == "cancel":
            if not engine.cancel(pid, tid):
                raise ValueError("No AI job is running for this task.")
        elif action in TRANSITIONS:
            with store.edit(project) as doc:
                task = find_task(doc, tid)
                if task is None:
                    raise KeyError(tid)
                transition(task, action)
        else:
            raise ValueError(f"Unknown action {action!r}.")

    @app.post("/api/projects/<pid>/tasks/<int:tid>/action")
    def task_action(pid, tid):
        project = project_or_404(pid)
        data = body()
        action = data.get("action")
        try:
            apply_action(project, tid, action, data)
        except KeyError:
            abort(404)
        if action == "queue":
            engine.kick(pid)
        doc = store.read(project, fresh=True)
        return jsonify(task_view(pid, find_task(doc, tid) or abort(404)))

    BATCH_ACTIONS = set(TRANSITIONS) | {"plan", "cancel", "delete", "set_provider"}
    MAX_BATCH = 200

    @app.post("/api/projects/<pid>/tasks/batch")
    def batch_action(pid):
        """Apply one action to many tasks. Each task succeeds or fails on its own."""
        project = project_or_404(pid)
        data = body()
        action = data.get("action")
        if action not in BATCH_ACTIONS:
            raise ValueError(f"Unknown action {action!r}.")
        ids = data.get("ids")
        if not isinstance(ids, list) or not ids or not all(
                isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise ValueError("ids must be a non-empty list of task ids.")
        ids = list(dict.fromkeys(ids))
        if len(ids) > MAX_BATCH:
            raise ValueError(f"At most {MAX_BATCH} tasks per batch.")
        provider = data.get("provider") or ""
        if action == "set_provider" and provider and provider not in PROVIDERS:
            raise ValueError("Unknown provider.")

        errors: dict[int, str] = {}

        def each(fn) -> None:
            for tid in ids:
                try:
                    fn(tid)
                except KeyError:
                    errors[tid] = "not found"
                except ValueError as exc:
                    errors[tid] = str(exc)

        if action == "plan":
            each(lambda tid: engine.start_planning(project, tid, data.get("feedback") or ""))
        elif action == "cancel":
            each(lambda tid: apply_action(project, tid, "cancel", data))
        else:
            # Everything else is a plain edit of tasks.json: one read and one write for the batch.
            stamp = now()
            with store.edit(project) as doc:
                def edit_one(tid):
                    task = find_task(doc, tid)
                    if task is None:
                        raise KeyError(tid)
                    if action in TRANSITIONS:
                        transition(task, action, stamp)
                    elif task["status"] in LOCKED:
                        raise ValueError(f"Task is {task['status']}; cancel it first.")
                    elif action == "delete":
                        doc["tasks"].remove(task)
                    elif task.get("provider", "") != provider:
                        task["provider"] = provider
                        log_event(task, "Edited provider")
                each(edit_one)
            if action == "queue" and len(errors) < len(ids):
                engine.kick(pid)

        doc = store.read(project, fresh=True)
        results = [{"id": tid, "ok": False, "error": errors[tid]} if tid in errors else {"id": tid, "ok": True}
                   for tid in ids]
        return jsonify(results=results, tasks=[task_view(pid, t) for t in doc["tasks"]])

    @app.get("/api/projects/<pid>/tasks/<int:tid>/live")
    def live(pid, tid):
        job = engine.job(pid, tid)
        return jsonify(active=job is not None, kind=job.kind if job else None,
                       output=job.text() if job else "",
                       elapsed=round(job.elapsed()) if job else 0)

    if start_engine:
        engine.startup()
    return app
