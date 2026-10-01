"""Flask app: JSON API plus a single-page UI."""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, render_template, request

from . import cline, gitops, opencode, svnops
from .engine import Engine
from .hosts import HostError, host_for, open_terminal, open_vscode, probe
from .providers import IMAGE_MIME, PLAN_TRUST_LEVELS, endpoint_key, list_models, plan_questions, ready_status
from .store import (ATTACHMENT_NAME, AUTO_MODES, CLI_PROVIDERS, MODELS, PAUSABLE, STATUSES, Registry, Settings,
                    TaskStore, attachment_path, cli_model_suggestions, empty_doc, find_endpoint, find_task, log_event, new_task, now,
                    provider_choices, resolve_auto, save_attachment, set_status, tasks_path, valid_provider)

EDITABLE = ("title", "description", "plan", "provider", "plan_model", "code_model", "plan_trust")
MODEL_KEYS = ("plan_model", "code_model")
PROJECT_MODEL_KEYS = ("plan_model", "code_model", "chat_model")
LOCKED = ("planning", "running")
ENDPOINT_ID = re.compile(r"^[a-z0-9_-]{1,32}$")
MODELS_CACHE_SECONDS = 600


AGENT_NAME = re.compile(r"^[A-Za-z0-9._/-]+$")
COMMAND_FLAGS = ("require_agents", "plan_must_not_edit", "prompt_arg")


def model_suggestions(settings: dict) -> dict:
    """Model dropdown suggestions per provider id (endpoint lists, and opencode's per project,
    are extended live in the UI)."""
    out = {p: cli_model_suggestions(settings, p) for p in CLI_PROVIDERS}
    for ep in settings["endpoints"]:
        builtin = list(MODELS["openai"]) if ep["id"] == "openai" else []
        own = [m for m in (ep["model"], ep.get("code_model", "")) if m]
        out[ep["id"]] = list(dict.fromkeys(ep["models"] + own + builtin))
    return out


def model_name(value, what: str = "model") -> str:
    """A model name from a request: a single-line string, stripped ("" for none)."""
    if value is None:
        return ""
    if not isinstance(value, str) or any(c in value for c in "\r\n"):
        raise ValueError(f"The {what} must be a single-line name.")
    return value.strip()


def model_list(value, what: str) -> list[str]:
    """Model names from a space/comma separated string or a list of strings, deduplicated."""
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    if not isinstance(value, list) or not all(isinstance(m, str) for m in value):
        raise ValueError(f"The {what} must be a list of names.")
    return list(dict.fromkeys(m for m in (model_name(m, what) for m in value) if m))


def clean_commands(commands) -> dict:
    """Check the CLI settings sent by the Settings form (their model defaults must be names)."""
    if not isinstance(commands, dict):
        raise ValueError("commands must be an object.")
    for name, cfg in commands.items():
        if isinstance(cfg, dict):
            for key in MODEL_KEYS:
                if key in cfg:
                    cfg[key] = model_name(cfg[key], "default model")
            if "models" in cfg:
                cfg["models"] = model_list(cfg["models"] or [], f"{name} model suggestions")
            for key in ("plan_agent", "run_agent"):
                if key in cfg:
                    value = cfg[key].strip() if isinstance(cfg[key], str) else ""
                    if not AGENT_NAME.match(value):
                        raise ValueError(f"{name} {key.replace('_', ' ')} must be a name made of letters, "
                                         "digits, '.', '_', '-' or '/'.")
                    cfg[key] = value
            for key in COMMAND_FLAGS:
                if key in cfg and not isinstance(cfg[key], bool):
                    raise ValueError(f"{name}.{key} must be true or false.")
    return commands


def clean_automation(values) -> dict:
    """The global automation defaults sent by the Settings form: one bool per mode sent."""
    if not isinstance(values, dict):
        raise ValueError("automation must be an object.")
    out = {}
    for key in AUTO_MODES:
        if key in values:
            if not isinstance(values[key], bool):
                raise ValueError(f"automation.{key} must be true or false.")
            out[key] = values[key]
    return out


def clean_git(values) -> dict:
    """The git settings sent by the Settings form: the .gitignore template for new repositories."""
    if not isinstance(values, dict):
        raise ValueError("git must be an object.")
    out = {}
    if "gitignore" in values:
        text = str(values["gitignore"]).replace("\r\n", "\n")
        if len(text.encode("utf-8")) > 64 * 1024:
            raise ValueError("The .gitignore template must be under 64 KB.")
        out["gitignore"] = text if not text or text.endswith("\n") else text + "\n"
    return out


def auto_override(value, key: str) -> bool | None:
    """A project's automation override: True/False, or None to use the global default."""
    if value is None or value == "":
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true, false or null (use the default).")
    return value


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:32].strip("-") or "api"


def clean_endpoints(values, saved: list[dict]) -> list[dict]:
    """Validate endpoints sent by the Settings form. Blank keys keep the saved key for that id."""
    if not isinstance(values, list):
        raise ValueError("endpoints must be a list.")
    saved_by_id = {ep["id"]: ep for ep in saved}
    out, seen = [], set(CLI_PROVIDERS)
    for raw in values:
        if not isinstance(raw, dict):
            raise ValueError("Each endpoint must be an object.")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("Every endpoint needs a name.")
        eid = str(raw.get("id") or "").strip()
        if not eid:
            base, n, eid = _slug(name), 2, _slug(name)
            while eid in seen or eid in (e.get("id") for e in values if isinstance(e, dict)):
                eid = f"{base[:29]}-{n}"
                n += 1
        if not ENDPOINT_ID.match(eid):
            raise ValueError(f"Endpoint id {eid!r} must be 1–32 lowercase letters, digits, - or _.")
        if eid in seen:
            raise ValueError(f"Endpoint id {eid!r} is reserved or used twice.")
        seen.add(eid)
        base_url = str(raw.get("base_url") or "").strip()
        if not re.match(r"^https?://[^/\s]+", base_url, re.IGNORECASE):
            raise ValueError(f"{name}: the base URL must start with http:// or https://.")
        try:
            max_steps = int(40 if raw.get("max_steps") in (None, "") else raw["max_steps"])
        except (TypeError, ValueError):
            raise ValueError(f"{name}: max steps must be a whole number.") from None
        if not 1 <= max_steps <= 200:
            raise ValueError(f"{name}: max steps must be between 1 and 200.")
        headers = raw.get("headers") or {}
        if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) and k.strip()
                and not any(c in k + v for c in "\r\n") for k, v in headers.items()):
            raise ValueError(f"{name}: headers must be single-line 'Name: value' strings.")
        models = model_list(raw.get("models") or [], f"{name}: model suggestions")
        key = raw.get("api_key") or ""
        if not isinstance(key, str) or any(c in key for c in "\r\n"):
            raise ValueError(f"{name}: the API key must be a single line.")
        key = key.strip()
        if not key and not raw.get("api_key_clear"):
            key = saved_by_id.get(eid, {}).get("api_key", "")
        out.append({
            "id": eid, "name": name, "base_url": base_url, "api_key": key,
            "api_key_env": str(raw.get("api_key_env") or "").strip(),
            "headers": {k.strip(): v.strip() for k, v in headers.items()},
            "model": str(raw.get("model") or "").strip(),
            "code_model": str(raw.get("code_model") or "").strip(),
            "models": models,
            "allow_commands": bool(raw.get("allow_commands")),
            "max_steps": max_steps,
        })
    return out


def create_app(data_dir: str | None = None, start_engine: bool = True) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # an 8 MB image is ~11 MB as base64
    data_dir = data_dir or os.environ.get("PATCHGOBLIN_DATA") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    registry, settings = Registry(data_dir), Settings(data_dir)
    # An endpoint whose id became a CLI provider was just renamed (once): follow it in projects.
    for old, new in settings.renamed.items():
        registry.rename_provider(old, new)
    store = TaskStore(settings.get()["provider_renames"])
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

    def remember(provider: str, names) -> None:
        """Save new CLI model names as suggestions; never fails the request."""
        try:
            settings.remember_models(provider, list(names))
        except Exception as exc:
            app.logger.warning("Could not remember models: %s", exc)

    def body() -> dict:
        return request.get_json(silent=True) or {}

    def project_or_404(pid: str) -> dict:
        project = registry.get(pid)
        if project is None:
            abort(404)
        return project

    def task_view(pid: str, task: dict) -> dict:
        job = engine.job(pid, task["id"])
        return {**task, "active": job is not None, "activity": job.current if job else "",
                "questions": plan_questions(task.get("plan", ""))}

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
        current = settings.get()
        return render_template("index.html", statuses=STATUSES, providers=provider_choices(current),
                               models=model_suggestions(current))

    # ---- settings -----------------------------------------------------------
    def settings_view(values: dict) -> dict:
        """Settings for the browser: saved keys are never sent, only whether one exists."""
        endpoints = []
        for ep in values["endpoints"]:
            env = ep["api_key_env"].strip()
            endpoints.append({**ep, "api_key": "", "api_key_saved": bool(ep["api_key"].strip()),
                              "api_key_env_present": bool(env and os.environ.get(env))})
        return {**values, "endpoints": endpoints, "providers": provider_choices(values),
                "models": model_suggestions(values)}

    @app.get("/api/settings")
    def get_settings():
        return jsonify(settings_view(settings.get()))

    def auto_values() -> dict[str, dict[str, bool]]:
        """Every project's effective automation modes, by project id."""
        current = settings.get()
        return {p["id"]: {k: resolve_auto(p, current, k) for k in AUTO_MODES} for p in registry.list()}

    def apply_turned_on(before: dict[str, dict[str, bool]]) -> None:
        """Apply each mode that just went from off to on to the project's existing tasks."""
        after = auto_values()
        for project in registry.list():
            old, new = before.get(project["id"], {}), after.get(project["id"], {})
            flipped = [k for k in AUTO_MODES if new.get(k) and not old.get(k)]
            if flipped:
                try:
                    engine.apply_auto_now(project, flipped)
                except Exception as exc:
                    app.logger.warning("Automation for %s failed: %s", project.get("name"), exc)

    @app.put("/api/settings")
    def put_settings():
        data = body()
        allowed = {k: data[k] for k in ("commands", "timeouts") if k in data}
        if "commands" in allowed:
            allowed["commands"] = clean_commands(allowed["commands"])
        if "automation" in data:
            allowed["automation"] = clean_automation(data["automation"])
        if "git" in data:
            allowed["git"] = clean_git(data["git"])
        endpoints = clean_endpoints(data["endpoints"], settings.get()["endpoints"]) if "endpoints" in data else None
        before = auto_values()
        updated = settings.update(allowed)
        if endpoints is not None:
            updated = settings.save_endpoints(endpoints)
            models_cache.clear()
        apply_turned_on(before)
        return jsonify(settings_view(updated))

    # ---- OpenAI-compatible endpoints ---------------------------------------
    models_cache: dict[tuple, tuple[float, list[str]]] = {}
    models_lock = threading.Lock()

    @app.get("/api/endpoints/<eid>/models")
    def endpoint_models(eid):
        ep = find_endpoint(settings.get(), eid) or abort(404)
        suggestions = model_suggestions(settings.get())[eid]
        cache_key = (eid, ep["base_url"], bool(endpoint_key(ep)))
        with models_lock:
            cached = models_cache.get(cache_key)
        if cached and not request.args.get("refresh") and time.monotonic() - cached[0] < MODELS_CACHE_SECONDS:
            live, error = cached[1], ""
        else:
            try:
                live, error = list_models(ep), ""
                with models_lock:
                    models_cache[cache_key] = (time.monotonic(), live)
            except RuntimeError as exc:
                live, error = [], str(exc)
        return jsonify(models=list(dict.fromkeys(suggestions + live)), error=error)

    # ---- opencode -------------------------------------------------------------
    opencode_cache: dict[str, tuple[float, dict]] = {}

    @app.get("/api/projects/<pid>/opencode/models")
    def opencode_models(pid):
        """Models from the project's opencode config, else from ``opencode models``, plus the
        Settings defaults. ``source`` says which ("config" or "cli")."""
        project = project_or_404(pid)
        with models_lock:
            cached = opencode_cache.get(pid)
        if cached and not request.args.get("refresh") and time.monotonic() - cached[0] < MODELS_CACHE_SECONDS:
            found = cached[1]
        else:
            host, errors = host_for(project), []
            models, source = [], "config"
            try:
                configs, errors = opencode.read_configs(host, project["path"])
                models = opencode.config_models(configs)
                if not models:
                    models, source = opencode.cli_models(host, project["path"]), "cli"
            except (HostError, RuntimeError) as exc:
                errors.append(str(exc))
            found = {"models": models, "source": source, "error": " ".join(errors)}
            if not errors:
                with models_lock:
                    opencode_cache[pid] = (time.monotonic(), found)
        defaults = model_suggestions(settings.get())["opencode"]
        return jsonify({**found, "models": list(dict.fromkeys(defaults + found["models"]))})

    # ---- Cline ----------------------------------------------------------------
    cline_cache: dict[str, tuple[float, tuple[str, list[str]]]] = {}

    @app.get("/api/projects/<pid>/cline/models")
    def cline_models(pid):
        """Cline's bundled model catalog for its active provider, plus the saved suggestions.
        Any failure falls back to the suggestions and is reported in ``error``."""
        project = project_or_404(pid)
        with models_lock:
            cached = cline_cache.get(pid)
        provider, error = "", ""
        if cached and not request.args.get("refresh") and time.monotonic() - cached[0] < MODELS_CACHE_SECONDS:
            provider, live = cached[1]
        else:
            try:
                provider, live = cline.catalog_models(host_for(project), project["path"])
                with models_lock:
                    cline_cache[pid] = (time.monotonic(), (provider, live))
            except (HostError, RuntimeError) as exc:
                live, error = [], str(exc)
        defaults = model_suggestions(settings.get())["cline"]
        return jsonify(models=list(dict.fromkeys(defaults + live)), provider=provider, error=error)

    @app.post("/api/endpoints/test")
    def test_endpoint():
        ep = clean_endpoints([body()], settings.get()["endpoints"])[0]
        try:
            models = list_models(ep)
        except RuntimeError as exc:
            return jsonify(ok=False, error=str(exc), count=0)
        return jsonify(ok=True, error="", count=len(models))

    # ---- projects -----------------------------------------------------------
    @app.get("/api/projects")
    def list_projects():
        return jsonify(projects=registry.list())

    @app.get("/api/projects/status")
    def projects_status():
        # Separate from list_projects: an SSH probe can take ~20 s, so check in parallel.
        projects = registry.list()
        def status_of(project: dict) -> dict:
            result = probe(project)
            if not result.get("ok"):
                return result
            ops = gitops if gitops.tracked(project) else svnops if svnops.tracked(project) else None
            if ops:
                try:
                    result = {**result, "vcs": ops.vcs_summary(host_for(project), project["path"])}
                except (HostError, OSError, ValueError):
                    pass  # a broken repo must not break the reachability dot
            return result

        with ThreadPoolExecutor(max_workers=min(8, len(projects) or 1)) as pool:
            results = list(pool.map(status_of, projects))
        return jsonify(status={p["id"]: r for p, r in zip(projects, results)})

    def project_fields(data: dict) -> dict:
        provider = data.get("provider") or "claude"
        if not valid_provider(settings.get(), provider):
            raise ValueError(f"Unknown provider {provider}.")
        location = data.get("location") or "local"
        fields = {
            "name": (data.get("name") or "").strip(),
            "location": location,
            "ssh_target": (data.get("ssh_target") or "").strip() if location == "ssh" else "",
            "ssh_port": int(data["ssh_port"]) if location == "ssh" and data.get("ssh_port") else None,
            "provider": provider,
            **{k: model_name(data.get(k)) for k in PROJECT_MODEL_KEYS},
        }
        return fields

    def require_secrets_ack(data: dict) -> None:
        if data.get("secrets_ack") is not True:
            raise ValueError("Confirm that this directory contains no secrets (API keys, .env files, "
                             "credentials) before PatchGoblin creates a git repository and commits it.")

    @app.post("/api/browse")
    def browse():
        """List folders on the machine a project would live on (for the folder picker)."""
        data = body()
        host = host_for(project_fields(data))
        path = (data.get("path") or "").strip()
        if data.get("home") or (not path and not getattr(host, "lists_drives", False)):
            path = host.home()
        return jsonify(host.list_dirs(path))

    @app.post("/api/projects")
    def add_project():
        data = body()
        fields = project_fields(data)
        fields["git_tracking"] = data.get("git_tracking") is True
        if fields["git_tracking"]:
            require_secrets_ack(data)
        host = host_for(fields)
        if fields["location"] == "ssh":
            host.check()
            fields["ssh_os"] = host.os
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
        if fields["git_tracking"]:
            created = gitops.ensure_repo(host, path, settings.get()["git"]["gitignore"])
        tpath = tasks_path(fields, host)
        if host.read_text(tpath) is None:
            host.write_text(tpath, json.dumps(empty_doc(), indent=2) + "\n")
        if fields["git_tracking"] and created:
            gitops.commit_all(host, path, "PatchGoblin: initial commit")
        added = registry.add(fields)
        remember(fields["provider"], [fields[k] for k in PROJECT_MODEL_KEYS])
        return jsonify(added), 201

    @app.post("/api/projects/<pid>/git/enable")
    def enable_git(pid):
        """Turn on git tracking for a project that started untracked: create the repository
        (or adopt an existing repo root) and its initial commit, then set the flag."""
        data = body()
        project = project_or_404(pid)
        if engine.busy(pid) or any(t["status"] in LOCKED for t in store.read(project, fresh=True)["tasks"]):
            raise ValueError("Wait for this project's AI jobs to finish first.")
        if gitops.tracked(project):
            return jsonify(project)
        if svnops.tracked(project):
            raise ValueError("This project is tracked with SVN; stop SVN tracking before using git.")
        require_secrets_ack(data)
        host, path = host_for(project), project["path"]
        created = gitops.ensure_repo(host, path, settings.get()["git"]["gitignore"])
        if created or not gitops.recent_commits(host, path, limit=1):
            gitops.commit_all(host, path, "PatchGoblin: initial commit")
        updated = registry.update(project["id"], {"git_tracking": True})
        return jsonify(updated)

    @app.post("/api/projects/<pid>/svn/enable")
    def enable_svn(pid):
        """Turn on SVN check-in tracking for a project that is an existing working copy root."""
        project = project_or_404(pid)
        if engine.busy(pid) or any(t["status"] in LOCKED for t in store.read(project, fresh=True)["tasks"]):
            raise ValueError("Wait for this project's AI jobs to finish first.")
        if svnops.tracked(project):
            return jsonify(project)
        if gitops.tracked(project):
            raise ValueError("This project is tracked with git; stop git tracking before using SVN.")
        if not svnops.is_wc_root(host_for(project), project["path"]):
            raise ValueError("This folder is not the root of an SVN working copy (or svn is not installed "
                             "on its host). PatchGoblin never runs svn checkout itself.")
        return jsonify(registry.update(project["id"], {"svn_tracking": True}))

    @app.patch("/api/projects/<pid>")
    def update_project(pid):
        project = project_or_404(pid)
        data = body()
        fields = {}
        if "git_tracking" in data:
            if data["git_tracking"] is True:
                raise ValueError("Use POST /api/projects/<id>/git/enable to turn on git tracking.")
            if data["git_tracking"] is not False:
                raise ValueError("git_tracking must be true or false.")
            fields["git_tracking"] = False
        if "svn_tracking" in data:
            if data["svn_tracking"] is True:
                raise ValueError("Use POST /api/projects/<id>/svn/enable to turn on SVN tracking.")
            if data["svn_tracking"] is not False:
                raise ValueError("svn_tracking must be true or false.")
            fields["svn_tracking"] = False
        if not gitops.tracked(project) and any(k in data for k in ("remote_url", "auto_sync", "sync_mode")):
            raise ValueError("Git tracking is off for this project; turn it on first.")
        if "name" in data:
            if not isinstance(data["name"], str) or not data["name"].strip():
                raise ValueError("Project name can't be blank.")
            fields["name"] = data["name"].strip()
        if "provider" in data:
            if not valid_provider(settings.get(), data["provider"]):
                raise ValueError("Unknown provider.")
            fields["provider"] = data["provider"]
        if "model" in data and "plan_model" not in data:  # before planning/coding models
            fields["plan_model"] = model_name(data["model"])
        for key in PROJECT_MODEL_KEYS:
            if key in data:
                fields[key] = model_name(data[key])
        if "plan_limit" in data:
            fields["plan_limit"] = plan_limit(data["plan_limit"])
        if "rewrite_titles" in data:
            fields["rewrite_titles"] = bool(data["rewrite_titles"])
        if "auto_sync" in data:
            fields["auto_sync"] = bool(data["auto_sync"])
        if "sync_mode" in data:
            fields["sync_mode"] = sync_mode(data["sync_mode"])
        if "plan_trust" in data:
            fields["plan_trust"] = plan_trust(data["plan_trust"])
        for key in AUTO_MODES:
            if key in data:
                fields[key] = auto_override(data[key], key)
        remote_url = None
        if "remote_url" in data:
            if not isinstance(data["remote_url"], str):
                raise ValueError("Remote URL must be a string.")
            remote_url = data["remote_url"].strip()
        # Everything is validated above, so a bad field changes nothing; the remote is set
        # before saving so a git failure leaves projects.json untouched.
        if remote_url is not None:
            host = host_for(project)
            if remote_url != gitops.get_remote(host, project["path"]):
                gitops.set_remote(host, project["path"], remote_url)
        before = auto_values()
        updated = registry.update(project["id"], fields)
        remember(fields.get("provider", project.get("provider") or "claude"),
                 [fields[k] for k in PROJECT_MODEL_KEYS if k in fields])
        apply_turned_on(before)
        return jsonify(updated)

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

    def plan_trust(value, allow_blank: bool = False) -> str:
        """A planning trust level; "" (a task's "use the project's level") only if allowed."""
        if (value == "" and allow_blank) or value in PLAN_TRUST_LEVELS:
            return value
        raise ValueError("Plan trust must be one of: " + ", ".join(PLAN_TRUST_LEVELS) + ".")

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
        if svnops.tracked(project):
            return jsonify(commits=svnops.recent_log(host_for(project), project["path"]))
        if not gitops.tracked(project):
            raise ValueError("Version control tracking is off for this project.")
        return jsonify(commits=gitops.recent_commits(host_for(project), project["path"]))

    def svn_project(pid: str) -> dict:
        project = project_or_404(pid)
        if not svnops.tracked(project):
            raise ValueError("SVN tracking is off for this project.")
        return project

    @app.get("/api/projects/<pid>/checkin")
    def get_checkin(pid):
        project = svn_project(pid)
        tasks = engine.pending_checkin(project)
        entries = svnops.status_entries(host_for(project), project["path"])
        return jsonify(tasks=[{"id": t["id"], "title": t["title"], "summary": t.get("summary", ""),
                               "changes": t.get("changes", [])} for t in tasks],
                       status=[{"status": svnops.LETTER[item], "item": item, "path": name}
                               for name, item in sorted(entries.items())],
                       message=svnops.default_checkin_message(tasks))

    @app.post("/api/projects/<pid>/checkin")
    def post_checkin(pid):
        project = svn_project(pid)
        message = body().get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Enter a check-in message.")
        if engine.busy(pid) or any(t["status"] in LOCKED for t in store.read(project, fresh=True)["tasks"]):
            raise ValueError("Wait for this project's AI jobs to finish before checking in.")
        rev = engine.checkin(project, message.strip() + "\n")
        return jsonify(revision=rev)

    def remote_view(project: dict, status: dict) -> dict:
        return {**status, "auto_sync": project.get("auto_sync") is True,
                "sync_mode": project.get("sync_mode") or "ff-only"}

    @app.get("/api/projects/<pid>/remote")
    def get_remote(pid):
        project = project_or_404(pid)
        if not gitops.tracked(project):
            raise ValueError("Git tracking is off for this project.")
        return jsonify(remote_view(project, gitops.remote_status(host_for(project), project["path"])))

    @app.put("/api/projects/<pid>/remote")
    def put_remote(pid):
        project = project_or_404(pid)
        if not gitops.tracked(project):
            raise ValueError("Git tracking is off for this project.")
        url = body().get("url") or ""
        if not isinstance(url, str):
            raise ValueError("url must be a string.")
        host = host_for(project)
        gitops.set_remote(host, project["path"], url)
        return jsonify(remote_view(project, gitops.remote_status(host, project["path"])))

    @app.post("/api/projects/<pid>/remote/sync")
    def sync_remote(pid):
        project = project_or_404(pid)
        if not gitops.tracked(project):
            raise ValueError("Git tracking is off for this project.")
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

    @app.post("/api/projects/<pid>/vscode")
    def vscode(pid):
        open_vscode(project_or_404(pid))
        return jsonify(ok=True)

    # ---- chat ---------------------------------------------------------------
    def chat_view(pid: str) -> dict:
        chat = engine.chat(pid)
        job = chat.job
        return {"messages": list(chat.messages), "active": job is not None,
                "output": job.text() if job else "", "elapsed": round(job.elapsed()) if job else 0,
                "events": job.events() if job else [], "current": job.current if job else ""}

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
        if provider and not valid_provider(settings.get(), provider):
            raise ValueError("Unknown provider.")
        models = {k: model_name(data.get(k)) for k in MODEL_KEYS}
        trust = plan_trust(data.get("plan_trust") or "", allow_blank=True)
        paused = data.get("paused") is True
        with store.edit(project) as doc:
            task = new_task(doc, title, (data.get("description") or "").strip(), provider, **models,
                            plan_trust=trust, paused=paused)
        remember(provider or project.get("provider") or "claude", models.values())
        if not paused and engine.auto(pid, "auto_plan"):
            try:
                engine.start_planning(project, task["id"], auto=True)
            except (ValueError, KeyError) as exc:  # creating the task must still succeed
                with store.edit(project) as doc:
                    current = find_task(doc, task["id"])
                    if current is not None:
                        log_event(current, f"Auto-plan could not start: {exc}")
            task = find_task(store.read(project, fresh=True), task["id"]) or task
        return jsonify(task_view(pid, task)), 201

    @app.patch("/api/projects/<pid>/tasks/<int:tid>")
    def update_task(pid, tid):
        project = project_or_404(pid)
        data = body()
        with store.edit(project) as doc:
            task = find_task(doc, tid) or abort(404)
            if task["status"] in LOCKED:
                raise ValueError(f"Task is {task['status']}; wait or cancel first.")
            data = {**data, **{k: model_name(data[k]) for k in MODEL_KEYS if k in data}}
            if "plan_trust" in data:
                data["plan_trust"] = plan_trust(data["plan_trust"], allow_blank=True)
            changed = [k for k in EDITABLE if k in data and data[k] != task.get(k, "")]
            if "provider" in changed and data["provider"] and not valid_provider(settings.get(), data["provider"]):
                raise ValueError("Unknown provider.")
            if "title" in changed and not str(data["title"]).strip():
                raise ValueError("A task needs a title.")
            for key in changed:
                task[key] = data[key].strip() if key == "title" else data[key]
            if changed:
                log_event(task, "Edited " + ", ".join(changed))
            provider = task.get("provider") or project.get("provider") or "claude"
            edited = [task[k] for k in MODEL_KEYS if k in changed]
        remember(provider, edited)
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

    # Simple state changes: action -> (allowed from, new status, history message).
    # A "planned" target lands in "drafted" instead while the plan has open questions.
    TRANSITIONS = {
        "mark_planned": (("unplanned", "failed", "drafted"), "planned", "Marked planned"),
        "mark_drafted": (("planned",), "drafted", "Moved to drafted"),
        "unplan": (("planned", "drafted"), "unplanned", "Moved back to unplanned"),
        "queue": (("planned", "drafted", "failed"), "queued", "Queued for AI"),
        "dequeue": (("queued",), "planned", "Removed from queue"),
        "approve": (("review",), "done", "Approved by engineer"),
        "reopen": (("done", "review"), "planned", "Reopened"),
        # Pause and resume only set or clear the task's "paused" flag (no status change); they
        # are handled in transition() and listed here for their allowed statuses.
        "pause": (PAUSABLE, None, "Paused"),
        "resume": (STATUSES, None, "Resumed"),
    }

    def transition(pid: str, task: dict, action: str, queued_at: str | None = None) -> bool:
        """Apply a simple state change; True if the task was queued (by hand or Auto-queue)."""
        allowed, status, message = TRANSITIONS[action]
        source = task["status"]
        if action == "resume":
            if not task.get("paused"):
                raise ValueError(f"Cannot resume a task that is {source}.")
            task["paused"] = False
            log_event(task, message)
            return source == "queued"
        if task.get("paused"):
            raise ValueError("Task is paused; resume it first.")
        if source not in allowed:
            raise ValueError(f"Cannot {action.replace('_', ' ')} a task that is {task['status']}.")
        if action == "pause":
            task["paused"] = True
            log_event(task, message)
            return False
        if status == "planned":
            status = ready_status(task.get("plan", ""))
            if status == "drafted":
                if task["status"] == "drafted":
                    raise ValueError("Plan still has open questions; "
                                     "answer them or remove them from the plan first.")
                message += " (plan has open questions)"
        elif action == "mark_drafted" and not plan_questions(task.get("plan", "")):
            raise ValueError("Plan has no open questions; nothing to draft.")
        if action == "queue":
            task["queued_at"] = queued_at or now()
        set_status(task, status, message)
        if action == "queue":
            return True
        # Like an AI plan, only a first plan is auto-queued (not one from drafted or failed).
        return action == "mark_planned" and source == "unplanned" and engine.maybe_auto_queue(pid, task)

    def apply_action(project: dict, tid: int, action: str, data: dict) -> bool:
        """One task's action; raises KeyError for a missing task, ValueError if not allowed.
        Returns True if the task was queued, so the caller kicks the runner."""
        pid = project["id"]
        if action == "plan":
            engine.start_planning(project, tid, data.get("feedback") or "", plan_answers(data.get("answers")))
        elif action == "send_back":  # per-task feedback, so never a batch action
            feedback = data.get("feedback") or ""
            if not isinstance(feedback, str):
                raise ValueError("feedback must be a string.")
            engine.start_planning(project, tid, feedback, review=True)
        elif action == "cancel":
            if not engine.cancel(pid, tid):
                raise ValueError("No AI job is running for this task.")
        elif action == "approve":  # optional per-task note; batch approve sends none
            note = data.get("note") or ""
            if not isinstance(note, str):
                raise ValueError("note must be a string.")
            note = note.replace("\0", "").strip()[:4000]
            with store.edit(project) as doc:
                task = find_task(doc, tid)
                if task is None:
                    raise KeyError(tid)
                queued = transition(pid, task, "approve")
                task["approval_note"] = note
                title = task["title"]
            if note and gitops.tracked(project):
                engine.record_approval(project, tid, title, note)
            return queued
        elif action in TRANSITIONS:
            with store.edit(project) as doc:
                task = find_task(doc, tid)
                if task is None:
                    raise KeyError(tid)
                return transition(pid, task, action)
        else:
            raise ValueError(f"Unknown action {action!r}.")
        return False

    @app.post("/api/projects/<pid>/tasks/<int:tid>/action")
    def task_action(pid, tid):
        project = project_or_404(pid)
        data = body()
        action = data.get("action")
        try:
            queued = apply_action(project, tid, action, data)
        except KeyError:
            abort(404)
        if queued:
            engine.kick(pid)
        doc = store.read(project, fresh=True)
        return jsonify(task_view(pid, find_task(doc, tid) or abort(404)))

    BATCH_ACTIONS = set(TRANSITIONS) | {"plan", "cancel", "delete", "set_provider", "set_models"}
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
        if action == "set_provider" and provider and not valid_provider(settings.get(), provider):
            raise ValueError("Unknown provider.")
        # set_models only touches the keys sent; "" resets a task to the project's model.
        models = {k: model_name(data[k]) for k in MODEL_KEYS if k in data}
        if action == "set_models" and not models:
            raise ValueError("Choose a planning or coding model to set.")

        errors: dict[int, str] = {}
        queued: list[int] = []
        model_providers: set[str] = set()

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
                        if transition(pid, task, action, stamp):
                            queued.append(tid)
                    elif task["status"] in LOCKED:
                        raise ValueError(f"Task is {task['status']}; cancel it first.")
                    elif action == "delete":
                        doc["tasks"].remove(task)
                    elif action == "set_models":
                        changed = [k for k, v in models.items() if task.get(k, "") != v]
                        task.update(models)
                        model_providers.add(task.get("provider") or project.get("provider") or "claude")
                        if changed:
                            log_event(task, "Edited models")
                    elif task.get("provider", "") != provider:
                        # A task's model overrides were chosen for its old provider.
                        task["provider"] = provider
                        task["plan_model"] = task["code_model"] = ""
                        log_event(task, "Edited provider")
                each(edit_one)
            for name in model_providers:
                remember(name, models.values())
            if queued:
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
                       events=job.events() if job else [], current=job.current if job else "",
                       elapsed=round(job.elapsed()) if job else 0)

    @app.get("/api/projects/<pid>/tasks/<int:tid>/changes")
    def task_changes(pid, tid):
        """The files changed by the task's (latest) commit; [] without one."""
        project = project_or_404(pid)
        task = find_task(store.read(project), tid) or abort(404)
        if svnops.tracked(project):
            return jsonify(files=task.get("changes") or [])
        sha = task.get("commit") or ""
        if not sha or not gitops.tracked(project):
            return jsonify(files=[])
        try:
            files = gitops.commit_files(host_for(project), project["path"], sha)
        except HostError:  # e.g. the commit is not in this clone
            files = []
        return jsonify(files=files)

    @app.post("/api/projects/<pid>/attachments")
    def upload_attachment(pid):
        project = project_or_404(pid)
        raw = body().get("data")
        if not isinstance(raw, str) or not raw:
            raise ValueError("Missing image data.")
        if raw.startswith("data:"):
            _, _, raw = raw.partition(",")
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("The image data is not valid base64.") from None
        path = save_attachment(project, data)
        return jsonify(path=path, url=f"/api/projects/{pid}/attachments/{path.rsplit('/', 1)[1]}"), 201

    @app.get("/api/projects/<pid>/attachments/<name>")
    def get_attachment(pid, name):
        project = project_or_404(pid)
        if not ATTACHMENT_NAME.match(name):
            abort(404)
        host = host_for(project)
        data = host.read_bytes(attachment_path(project, name, host))
        if data is None:
            abort(404)
        return Response(data, mimetype=IMAGE_MIME[name.rsplit(".", 1)[1]],
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"})

    if start_engine:
        engine.startup()
    return app
