"""Terminals plugin: real shells (local or over SSH) shown in the browser with xterm.js.

Output reaches the page through a streamed POST (waitress has no WebSockets): the page
attaches to the session it shows, from the last offset it has, and gets the missed scrollback
plus live output. Keystrokes and resizes are small POSTs.

The page shows several terminals at once, but a browser allows only about six HTTP/1.1
connections per origin, so ``POST /sessions/stream`` multiplexes every visible session over one
connection (events carry ``s``, the session id). The per-session ``/sessions/<id>/stream`` stays
for compatibility.
"""
from __future__ import annotations

import json
import os
import sys
import time

from flask import Response, abort, jsonify, request, stream_with_context

from patchgoblin.hosts import (HostError, host_for, open_terminal, terminal_color, terminal_title,
                               windows_shell)
from patchgoblin.pluginkit import Plugin

from .sessions import MAX_SESSIONS, SessionManager

PING_SECONDS = 15  # an idle stream sends a ping this often, so a closed page frees its thread
MAX_INPUT = 65536


def body() -> dict:
    return request.get_json(silent=True) or {}


def ndjson(event: str, **data) -> str:
    return json.dumps({"event": event, **data}) + "\n"


class TerminalsPlugin(Plugin):
    id = "terminals"
    name = "Terminals"
    description = "Shells for your projects (local or over SSH) right in the browser, using xterm.js."
    icon = ('<rect x="3" y="4" width="18" height="16" rx="2"/><polyline points="7 9 10 12 7 15"/>'
            '<line x1="12" y1="15" x2="17" y2="15"/>')
    order = 5
    scripts = ["vendor/xterm.js", "vendor/addon-fit.js", "vendor/addon-web-links.js"]
    styles = ["vendor/xterm.css"]

    def __init__(self, ctx):
        super().__init__(ctx)
        self.sessions = SessionManager()
        self.streams: dict[str, object] = {}  # page id -> its newest multiplexed stream

    def stop(self) -> None:
        self.sessions.kill_all()

    # ---- what to run --------------------------------------------------------------
    def project(self, pid: str) -> dict:
        project = self.ctx.registry.get(pid)
        if project is None:
            raise ValueError("That project no longer exists.")
        return project

    def color(self, pid: str) -> str:
        ids = [p["id"] for p in self.ctx.registry.list()]
        return terminal_color(ids.index(pid)) if pid in ids else ""

    def shell_for(self, project: dict | None) -> tuple[list[str], str | None]:
        """``(argv, cwd)`` of the shell for a project, or for a plain shell in the home folder."""
        local = windows_shell() if sys.platform == "win32" else [os.environ.get("SHELL") or "/bin/sh", "-l"]
        if project is None:
            return local, os.path.expanduser("~")
        host = host_for(project)
        if host.kind == "ssh":
            return host.shell_argv(project["path"]), None
        if not host.is_dir(project["path"]):
            raise HostError(f"Directory does not exist: {project['path']}")
        return local, project["path"]

    def session(self, sid: str):
        found = self.sessions.get(sid)
        if found is None:
            abort(404)
        return found

    # ---- routes ----------------------------------------------------------------------
    def routes(self, bp) -> None:
        def views():
            return {"sessions": [s.view() for s in self.sessions.list()]}

        @bp.get("/sessions")
        def list_sessions():
            return jsonify(views())

        def create(pid: str, cols, rows, title: str = ""):
            project = self.project(pid) if pid else None
            argv, cwd = self.shell_for(project)
            return self.sessions.create(argv, cwd, title or (terminal_title(project) if project else "Shell"),
                                        self.color(pid) if project else "", pid, cols or 100, rows or 30)

        @bp.post("/sessions")
        def new_session():
            data = body()
            s = create(str(data.get("project") or ""), data.get("cols"), data.get("rows"))
            return jsonify(session=s.view(), **views()), 201

        @bp.post("/sessions/<sid>/restart")
        def restart(sid):
            old = self.session(sid)
            s = create(old.project, old.cols, old.rows, old.title)
            self.sessions.close(sid)
            return jsonify(session=s.view(), **views())

        @bp.post("/sessions/stream")
        def stream_many():
            since = body().get("since")
            if not isinstance(since, dict) or len(since) > MAX_SESSIONS:
                raise ValueError(f"since must map at most {MAX_SESSIONS} session ids to offsets.")
            pos = {str(sid): (o if isinstance(o, int) and not isinstance(o, bool) and o >= 0 else 0)
                   for sid, o in since.items()}

            # A page that reopens its stream (the visible panes changed) names itself, so the
            # superseded stream ends at once instead of holding a server thread until its ping.
            client = str(body().get("client") or "")[:40]
            token = object()
            if client:
                self.streams[client] = token
                with self.sessions.changed:
                    self.sessions.changed.notify_all()

            def superseded() -> bool:
                return bool(client) and self.streams.get(client) is not token

            def gen():
                first, idle = True, time.monotonic()
                while pos:
                    if not first:
                        self.sessions.wait_any(pos, PING_SECONDS, superseded)
                        if superseded():
                            return
                        if any((s := self.sessions.get(sid)) and s.running for sid in pos):
                            time.sleep(0.008)  # let a burst of output arrive as one event
                    first = False
                    for sid in list(pos):
                        s = self.sessions.get(sid)
                        if s is None:
                            del pos[sid]
                            yield ndjson("gone", s=sid)
                            idle = time.monotonic()
                            continue
                        text, end, truncated = s.read_since(pos[sid])
                        if text:
                            yield ndjson("out", s=sid, o=pos[sid], d=text, end=end, truncated=truncated)
                            pos[sid], idle = end, time.monotonic()
                        elif not s.running:
                            del pos[sid]
                            yield ndjson("exit", s=sid, code=s.exit_code)
                            idle = time.monotonic()
                    if pos and time.monotonic() - idle >= PING_SECONDS - 0.5:
                        yield ndjson("ping")
                        idle = time.monotonic()

            return Response(stream_with_context(gen()), mimetype="application/x-ndjson",
                            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

        @bp.post("/sessions/<sid>/stream")
        def stream(sid):
            s = self.session(sid)
            since = body().get("since")
            pos = since if isinstance(since, int) and since >= 0 else 0

            def gen():
                nonlocal pos
                text, end, truncated = s.read_since(pos)
                yield ndjson("out", o=pos, d=text, end=end, truncated=truncated, alive=s.running)
                pos = end
                idle = time.monotonic()
                while True:
                    text, end, truncated = s.read_since(pos, wait=PING_SECONDS)
                    if text:
                        if s.running:
                            time.sleep(0.008)  # let a burst of output arrive as one event
                            more, end, _ = s.read_since(end)
                            text += more
                        yield ndjson("out", o=pos, d=text, end=end, truncated=truncated)
                        pos, idle = end, time.monotonic()
                    elif not s.running:
                        yield ndjson("exit", code=s.exit_code)
                        return
                    elif time.monotonic() - idle >= PING_SECONDS - 0.5:
                        yield ndjson("ping")
                        idle = time.monotonic()

            return Response(stream_with_context(gen()), mimetype="application/x-ndjson",
                            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

        @bp.post("/sessions/<sid>/input")
        def send_input(sid):
            data = body().get("data")
            if not isinstance(data, str) or len(data) > MAX_INPUT:
                raise ValueError("data must be text (at most 64 KB at a time).")
            self.session(sid).write(data)
            return jsonify(ok=True)

        @bp.post("/sessions/<sid>/resize")
        def resize(sid):
            data = body()
            self.session(sid).resize(data.get("cols"), data.get("rows"))
            return jsonify(ok=True)

        @bp.post("/sessions/<sid>/rename")
        def rename(sid):
            title = " ".join(str(body().get("title") or "").split())[:80]
            if not title:
                raise ValueError("Enter a name.")
            self.session(sid).title = title
            return jsonify(views())

        @bp.post("/sessions/<sid>/popout")
        def popout(sid):
            s = self.session(sid)
            if not s.project:
                raise ValueError("Only project terminals can be opened in Windows Terminal.")
            open_terminal(self.project(s.project), self.color(s.project), new=True)
            return jsonify(ok=True)

        @bp.delete("/sessions/<sid>")
        def close(sid):
            self.sessions.close(sid)
            return jsonify(views())


PLUGIN = TerminalsPlugin
