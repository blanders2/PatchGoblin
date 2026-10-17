"""GitHub plugin: list your repositories and clone them (here or over SSH) into PatchGoblin projects."""
from __future__ import annotations

import threading
import time

from flask import abort, jsonify, request

from patchgoblin import gitops, servers as servermod
from patchgoblin.hosts import HostError, host_for
from patchgoblin.pluginkit import Plugin
from patchgoblin.servers import Field, ServerType

from . import ghapi

CACHE_TTL = 300  # seconds a repo list is reused


def body() -> dict:
    return request.get_json(silent=True) or {}


class GithubPlugin(Plugin):
    id = "github"
    name = "GitHub"
    description = "List your GitHub repositories and clone them onto this computer or an SSH server as projects."
    icon = ('<path d="M9 19c-4.3 1.4-4.3-2.5-6-3m12 5v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.6 4.6 0 0 0-1.3-3.2'
            ' 4.2 4.2 0 0 0-.1-3.2s-1.1-.3-3.5 1.3a12 12 0 0 0-6.2 0C6.5 2.8 5.4 3.1 5.4 3.1a4.2 4.2 0 0 0-.1 3.2'
            'A4.6 4.6 0 0 0 4 9.5c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V21"/>')
    order = 30

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cache: dict[str, tuple[float, list, bool]] = {}  # account id -> (time, repos, truncated)
        self.lock = threading.Lock()

    def server_types(self) -> list:
        return [ServerType(
            id="github", label="GitHub account",
            fields=[
                Field("token", "Personal access token", kind="password", secret=True),
                Field("token_env", "…or environment variable holding the token", placeholder="GITHUB_TOKEN"),
                Field("api_url", "API URL", placeholder=ghapi.DEFAULT_API, help="Change only for GitHub Enterprise."),
                Field("protocol", "Clone with", kind="select", default="ssh",
                      options=[("ssh", "SSH (git@github.com:…)"), ("https", "HTTPS")]),
            ],
            clean=ghapi.clean_server, test=ghapi.test_server)]

    def accounts(self) -> list[dict]:
        return self.ctx.servers.list("github")

    def account(self, sid: str) -> dict:
        found = next((s for s in self.accounts() if s["id"] == sid), None)
        if found is None:
            abort(404)
        return found

    def repos(self, account: dict, refresh: bool = False) -> tuple[list, bool]:
        key = account["id"]
        with self.lock:
            hit = self.cache.get(key)
        if hit and not refresh and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1], hit[2]
        try:
            repos, truncated = ghapi.list_repos(account)
        except (ValueError, HostError) as exc:
            raise type(exc)(ghapi.scrub(str(exc), account)) from None
        with self.lock:
            self.cache[key] = (time.monotonic(), repos, truncated)
        return repos, truncated

    def cloned(self) -> dict:
        data = self.ctx.load().get("clones")
        return data if isinstance(data, dict) else {}

    def record(self, full_name: str, project_id: str) -> None:
        with self.lock:
            data = self.ctx.load()
            clones = data.get("clones") if isinstance(data.get("clones"), dict) else {}
            ids = [i for i in clones.get(full_name, []) if i != project_id] + [project_id]
            self.ctx.save({**data, "clones": {**clones, full_name: ids}})

    def routes(self, bp) -> None:
        pub = lambda s: servermod.public_server(s, self.ctx.servers.types.get("github"))

        @bp.get("/accounts")
        def list_accounts():
            return jsonify(accounts=[pub(s) for s in self.accounts()])

        @bp.get("/targets")
        def targets():
            ssh = [{"id": s["id"], "name": s["name"]} for s in self.ctx.servers.list("ssh")]
            return jsonify(targets=[{"id": "local", "name": "This computer"}, *ssh])

        @bp.get("/accounts/<sid>/repos")
        def list_repos(sid):
            account = self.account(sid)
            repos, truncated = self.repos(account, request.args.get("refresh") == "1")
            live = {p["id"]: p for p in self.ctx.registry.list()}
            clones = self.cloned()
            out = [{**r, "projects": [{"id": i, "name": live[i]["name"]} for i in clones.get(r["full_name"], [])
                                      if i in live]} for r in repos]
            return jsonify(repos=out, truncated=truncated, protocol=account.get("protocol") or "ssh")

        @bp.post("/clone")
        def clone():
            data = body()
            account = self.account(str(data.get("account") or ""))
            repos, _ = self.repos(account)
            repo = next((r for r in repos if r["full_name"] == data.get("repo")), None)
            if repo is None:
                raise ValueError("That repository is not in this account's list.")
            protocol = data.get("protocol") or account.get("protocol") or "ssh"
            if protocol not in ghapi.PROTOCOLS:
                raise ValueError("Clone with ssh or https.")
            url = repo["ssh_url"] if protocol == "ssh" else repo["clone_url"]
            add = data.get("add_project") is not False
            track = add and data.get("git_tracking") is not False
            if track:
                from patchgoblin.validators import require_secrets_ack
                require_secrets_ack(data)
            target = str(data.get("target") or "local")
            spec = {"location": "local"}
            if target != "local":
                saved = self.ctx.servers.get(target)
                if saved is None or saved.get("type") != "ssh":
                    raise ValueError("Unknown SSH server.")
                spec = {"location": "ssh", "server": saved["id"], "ssh_target": saved["target"],
                        "ssh_port": saved.get("port"),
                        "ssh_os": saved["os"] if saved.get("os") in ("posix", "windows") else None}
            host = host_for(spec)
            if spec["location"] == "ssh":
                host.check()
            parent = host.normalize(str(data.get("parent") or ""))
            if not parent:
                raise ValueError("Choose a parent folder.")
            folder = str(data.get("folder") or repo["name"])
            path = gitops.clone(host, parent, url, folder)
            result = {"path": path, "project": None}
            try:
                gitops.exclude_metadata(host, path)  # before any commit: tasks must never reach GitHub
            except HostError as exc:
                result["error"] = f"Cloned to {path}, but could not hide .patchgoblin/ from git: {exc}"
                return jsonify(result), 502
            if add:
                try:
                    project = self.ctx.add_project({
                        "location": spec["location"], "server": spec.get("server"), "path": path,
                        "name": str(data.get("name") or "").strip() or repo["name"],
                        "provider": data.get("provider"), "git_tracking": track,
                        "secrets_ack": data.get("secrets_ack"), "create": False})
                except (ValueError, HostError) as exc:
                    result["error"] = f"Cloned to {path}, but adding it as a project failed: {exc}"
                    return jsonify(result), 502
                self.record(repo["full_name"], project["id"])
                result["project"] = project
            return jsonify(result), 201


PLUGIN = GithubPlugin
