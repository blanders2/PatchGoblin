import base64
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import threading
import uuid
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from .host import atomic_json, lock


class Transport:
    def call(self, project, action, payload=None):
        envelope = {"project": project, "action": action, "payload": payload or {}}
        source = Path(__file__).with_name("host.py")
        if project["kind"] == "local":
            argv = [sys.executable, str(source)]
            stdin = json.dumps(envelope)
        else:
            # SSH invokes a remote shell: quote every command argument and keep all
            # project/task text in stdin, never in the remote command string.
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                    "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
                    "-o", "ServerAliveCountMax=3", "-p", str(project["port"]),
                    project["host"], shlex.join([project["python"], "-c", "import sys; exec(sys.stdin.read())"])]
            encoded = base64.b64encode(json.dumps(envelope).encode()).decode()
            stdin = "__name__ = 'patchgoblin_remote'\n" + source.read_text(encoding="utf-8")
            stdin += f"\nimport base64\nmain(json.loads(base64.b64decode('{encoded}')))\n"
        timeout = 7800 if action == "run" else 45
        try:
            result = subprocess.run(argv, input=stdin, text=True, encoding="utf-8",
                                    errors="replace", capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ValueError("Host request timed out. An AI run may still be active; refresh before retrying.") from None
        if result.returncode:
            raise ValueError((result.stderr or result.stdout)[-4000:])
        try:
            answer = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ValueError("Host returned invalid output. Check Python, SSH shell startup output, and permissions.") from None
        if not answer.get("ok"):
            raise ValueError(answer.get("error", "Host operation failed."))
        return answer["data"]


class Registry:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "projects.json"
        self.mutex = threading.RLock()
        if not self.path.exists():
            atomic_json(self.path, {"projects": [], "paused": False})

    def read(self):
        with self.mutex:
            return json.loads(self.path.read_text(encoding="utf-8"))

    def change(self, callback):
        with self.mutex:
            data = self.read()
            result = callback(data)
            atomic_json(self.path, data)
            return result

    def project(self, project_id):
        for project in self.read()["projects"]:
            if project["id"] == project_id:
                return project
        raise ValueError("Project not found.")


class Worker:
    def __init__(self, registry, transport):
        self.registry, self.transport = registry, transport
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.active = None
        self.errors = {}
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.loop, name="patchgoblin-queue", daemon=True)
        self.thread.start()

    def step(self):
        registry = self.registry.read()
        if registry["paused"]:
            return
        candidates = []
        for project in registry["projects"]:
            try:
                state = self.transport.call(project, "inspect")
                self.errors.pop(project["id"], None)
                # An interrupted run must be explicitly recovered before more work.
                if any(t["status"] in {"running", "planning", "revising"} for t in state["tasks"]):
                    continue
                for task in state["tasks"]:
                    if task["status"] in {"queued", "planning_queued", "revising_queued"}:
                        candidates.append((task["queued_at"], project, task))
            except Exception as exc:
                self.errors[project["id"]] = str(exc)
        if candidates:
            _, project, task = min(candidates, key=lambda candidate: candidate[0])
            self.active = {"project_id": project["id"], "task_id": task["id"], "title": task["title"]}
            try:
                self.transport.call(project, "run", {"task_id": task["id"]})
            except Exception as exc:
                self.errors[project["id"]] = str(exc)
            finally:
                self.active = None

    def loop(self):
        while not self.stop.is_set():
            try:
                self.step()
            except Exception:
                logging.exception("Queue iteration failed")
            self.wake.wait(3)
            self.wake.clear()


def validate_config(body, existing=None):
    project = dict(existing or {})
    project.update({key: body[key] for key in ("name", "path", "kind", "host", "port", "python", "provider", "model", "allow_commands") if key in body})
    project.setdefault("id", uuid.uuid4().hex)
    project.setdefault("kind", "local")
    project.setdefault("provider", "codex")
    project.setdefault("model", "")
    project.setdefault("host", "")
    project.setdefault("port", 22)
    project.setdefault("python", "python3")
    project.setdefault("allow_commands", False)
    for field in ("name", "path", "kind", "host", "python", "provider", "model"):
        if not isinstance(project.get(field), str):
            raise ValueError(f"{field} must be text.")
        project[field] = project[field].strip()
    if not project["name"] or len(project["name"]) > 100 or not project["path"]:
        raise ValueError("Project name and absolute directory are required.")
    if project["kind"] not in {"local", "ssh"} or project["provider"] not in {"codex", "claude", "openai"}:
        raise ValueError("Invalid host type or provider.")
    if project["provider"] == "openai" and not project["model"]:
        raise ValueError("Enter an OpenAI model ID available to your account.")
    if not isinstance(project["allow_commands"], bool):
        raise ValueError("allow_commands must be a boolean.")
    try:
        project["port"] = int(project["port"])
    except (ValueError, TypeError):
        raise ValueError("SSH port must be a number.") from None
    if not 1 <= project["port"] <= 65535:
        raise ValueError("SSH port must be between 1 and 65535.")
    if project["kind"] == "ssh":
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", project["host"]):
            raise ValueError("Use an SSH config alias or user@hostname (no spaces or options).")
        if not project["path"].startswith("/"):
            raise ValueError("SSH projects require an absolute POSIX directory such as /home/me/project.")
        if not re.fullmatch(r"[A-Za-z0-9_/.+-]+", project["python"]) or project["python"].startswith("-"):
            raise ValueError("Enter a Python executable name or absolute path.")
    return project


def create_app(data_dir=None, transport=None, start_worker=False):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 200000
    app.config["TRUSTED_HOSTS"] = ["localhost", "127.0.0.1", "[::1]"]
    registry = Registry(data_dir or os.environ.get("PATCHGOBLIN_DATA", ".patchgoblin-app"))
    transport = transport or Transport()
    worker = Worker(registry, transport)
    csrf = secrets.token_urlsafe(32)
    app.extensions.update(registry=registry, transport=transport, worker=worker, csrf=csrf)

    @app.before_request
    def protect_local_app():
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not secrets.compare_digest(request.headers.get("X-PatchGoblin-Token", ""), csrf):
                return jsonify(error="Session expired. Reload the page."), 403
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                return jsonify(error="Cross-origin requests are not allowed."), 403

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def bad_request(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(Exception)
    def other_error(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        app.logger.exception("Request failed")
        return jsonify(error="Operation failed. Check the server output for details."), 500

    def body():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    @app.get("/")
    def index():
        return render_template("index.html", csrf=csrf)

    @app.get("/api/projects")
    def projects():
        return jsonify(**registry.read(), active=worker.active, errors=worker.errors.copy())

    @app.post("/api/projects")
    def add_project():
        config = validate_config(body())
        result = transport.call(config, "init")
        config["path"] = result["path"]
        def insert(data):
            if any((p["kind"], p["host"], p["port"], os.path.normcase(p["path"])) ==
                   (config["kind"], config["host"], config["port"], os.path.normcase(config["path"])) for p in data["projects"]):
                raise ValueError("This directory is already registered.")
            data["projects"].append(config)
        registry.change(insert)
        worker.wake.set()
        return jsonify(config), 201

    @app.patch("/api/projects/<project_id>")
    def settings(project_id):
        current = registry.project(project_id)
        payload = body()
        config = validate_config({key: payload[key] for key in ("name", "provider", "model", "allow_commands") if key in payload}, current)
        def update(data):
            data["projects"] = [config if p["id"] == project_id else p for p in data["projects"]]
        registry.change(update)
        return jsonify(config)

    @app.get("/api/projects/<project_id>")
    def project_detail(project_id):
        project = registry.project(project_id)
        return jsonify(project=project, **transport.call(project, "inspect"))

    @app.post("/api/projects/<project_id>/checkpoint")
    def checkpoint(project_id):
        return jsonify(transport.call(registry.project(project_id), "checkpoint"))

    @app.post("/api/projects/<project_id>/tasks")
    def create_task(project_id):
        return jsonify(transport.call(registry.project(project_id), "create", body())), 201

    @app.post("/api/projects/<project_id>/tasks/<task_id>/<action>")
    def task_action(project_id, task_id, action):
        if action not in {"edit", "plan", "discuss", "mark_planned", "queue", "unqueue", "reopen", "recover", "complete"}:
            raise ValueError("Unknown task action.")
        payload = body()
        payload["task_id"] = task_id
        result = transport.call(registry.project(project_id), action, payload)
        worker.wake.set()
        return jsonify(result)

    @app.post("/api/queue")
    def pause_queue():
        paused = body().get("paused")
        if not isinstance(paused, bool):
            raise ValueError("paused must be a boolean.")
        registry.change(lambda data: data.update(paused=paused))
        worker.wake.set()
        return jsonify(paused=paused)

    if start_worker:
        worker.start()
    return app


def main():
    from waitress import serve
    data_dir = Path(os.environ.get("PATCHGOBLIN_DATA", ".patchgoblin-app")).resolve()
    with lock(data_dir / "server.lock"):
        app = create_app(data_dir, start_worker=True)
        port = int(os.environ.get("PATCHGOBLIN_PORT", "5050"))
        print(f"PatchGoblin is ready at http://127.0.0.1:{port}", flush=True)
        serve(app, host="127.0.0.1", port=port, threads=8)


if __name__ == "__main__":
    main()
