"""LLM API console plugin: talk to any LLM server and see exactly what goes over the wire."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import uuid
from collections import deque

from flask import Response, abort, jsonify, request, stream_with_context

from patchgoblin.pluginkit import Plugin

from . import llmapi

HISTORY_SIZE = 100


def body() -> dict:
    return request.get_json(silent=True) or {}


def ndjson(event: str, **data) -> str:
    return json.dumps({"event": event, **data}) + "\n"


class LlmPlugin(Plugin):
    id = "llm"
    name = "LLM API console"
    description = ("Test any LLM API (OpenAI-compatible, llama.cpp, llama-swap, vLLM, SGLang, Ollama, Anthropic…): "
                   "chat with every sampling and thinking setting, discover models and context sizes, and inspect "
                   "raw requests, streams and timings.")
    icon = ('<path d="M12 3a3 3 0 0 0-3 3v1H7a3 3 0 0 0-3 3v1a3 3 0 0 0 0 6v1a3 3 0 0 0 3 3h10a3 3 0 0 0 3-3v-1'
            'a3 3 0 0 0 0-6v-1a3 3 0 0 0-3-3h-2V6a3 3 0 0 0-3-3z"/><circle cx="9" cy="13" r="1"/><circle cx="15" cy="13" r="1"/>'
            '<path d="M9.5 17h5"/>')
    order = 20

    def __init__(self, ctx):
        super().__init__(ctx)
        self.history: deque = deque(maxlen=HISTORY_SIZE)  # recorded exchanges, newest last; memory only
        self.cancels: dict[str, threading.Event] = {}
        self.lock = threading.Lock()

    def stop(self) -> None:
        with self.lock:
            for event in self.cancels.values():
                event.set()

    # ---- saved servers -----------------------------------------------------------
    def servers(self) -> list[dict]:
        return llmapi.clean_servers(self.ctx.load().get("servers"))

    def server(self, sid: str) -> dict:
        found = next((s for s in self.servers() if s["id"] == sid), None)
        if found is None:
            abort(404)
        return found

    def save_servers(self, servers: list[dict]) -> None:
        self.ctx.save({**self.ctx.load(), "servers": servers})

    def suggestions(self, servers: list[dict]) -> list[dict]:
        """PatchGoblin's own OpenAI-compatible endpoints that aren't saved here yet."""
        have = {s["base_url"].lower() for s in servers}
        out = []
        for ep in self.ctx.settings.get().get("endpoints") or []:
            base = llmapi.normalize_base(ep.get("base_url") or "")
            if base and base.lower() not in have:
                out.append({"name": ep.get("name") or ep.get("id"), "base_url": base, "endpoint": ep.get("id"),
                            "source": f"PatchGoblin endpoint {ep.get('id')}"})
        return out

    def servers_view(self) -> dict:
        servers = self.servers()
        return {"servers": [llmapi.public_server(s) for s in servers], "suggestions": self.suggestions(servers)}

    # ---- the recording relay ---------------------------------------------------------
    def relay(self, server: dict, method: str, path: str, payload: str | None, label: str):
        """Stream one exchange to the browser as NDJSON events while recording it:
        request → response (status, headers) → line… or body → done (timings)."""
        rid = uuid.uuid4().hex[:12]
        cancel = threading.Event()
        with self.lock:
            self.cancels[rid] = cancel
        url = llmapi.build_url(server, path)
        sent_headers = llmapi.request_headers(server)
        if payload is not None and method not in ("GET", "HEAD"):
            sent_headers.setdefault("Content-Type", "application/json")
        rec = {"id": rid, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "server": server["id"], "server_name": server["name"],
               "label": label, "method": method, "url": url, "path": path,
               "request_headers": llmapi.masked_headers(sent_headers), "request_body": payload,
               "status": 0, "response_headers": {}, "response": "", "lines": 0, "bytes": 0,
               "ms_ttfb": None, "ms_total": None, "error": "", "cancelled": False}

        def gen():
            t0 = time.monotonic()
            ms = lambda: round((time.monotonic() - t0) * 1000, 1)  # noqa: E731
            yield ndjson("request", id=rid, method=method, url=url, headers=rec["request_headers"], body=payload)
            resp = None
            parts: list[str] = []
            try:
                ok, err = llmapi.open_request(server, method, path, payload)
                resp = ok or err
                rec["status"] = ok.status if ok else err.code
                rec["response_headers"] = dict(resp.headers.items())
                rec["ms_ttfb"] = ms()
                yield ndjson("response", status=rec["status"], reason=getattr(resp, "reason", "") or "",
                             headers=rec["response_headers"], ms=rec["ms_ttfb"])
                ctype = (resp.headers.get("Content-Type") or "").lower()
                streaming = "event-stream" in ctype or "ndjson" in ctype or "jsonl" in ctype
                if streaming:
                    while not cancel.is_set():
                        raw = resp.readline()
                        if not raw:
                            break
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        rec["lines"] += 1
                        rec["bytes"] += len(raw)
                        if rec["bytes"] < llmapi.MAX_RECORD:
                            parts.append(line + "\n")
                        if line:
                            yield ndjson("line", t=ms(), data=line)
                else:
                    data = resp.read(llmapi.MAX_RECORD + 1)
                    rec["bytes"] = len(data)
                    text = data[:llmapi.MAX_RECORD].decode("utf-8", "replace")
                    parts.append(text)
                    yield ndjson("body", t=ms(), data=text, truncated=len(data) > llmapi.MAX_RECORD)
                if cancel.is_set():
                    rec["cancelled"] = True
                    yield ndjson("cancelled", t=ms())
            except (urllib.error.URLError, OSError, ValueError) as exc:
                rec["error"] = str(getattr(exc, "reason", None) or exc) or type(exc).__name__
                yield ndjson("error", t=ms(), message=rec["error"])
            finally:
                if resp is not None:
                    resp.close()
                rec["response"] = "".join(parts)
                rec["ms_total"] = ms()
                with self.lock:
                    self.cancels.pop(rid, None)
                    self.history.append(rec)
            yield ndjson("done", t=rec["ms_total"], status=rec["status"], lines=rec["lines"], bytes=rec["bytes"])

        return Response(stream_with_context(gen()), mimetype="application/x-ndjson",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    def resolve_endpoint_key(self, spec: dict) -> dict:
        """Adding a suggested PatchGoblin endpoint copies its key here, never via the browser."""
        if spec.get("from_endpoint") and not spec.get("api_key"):
            ep = next((e for e in self.ctx.settings.get().get("endpoints") or []
                       if e.get("id") == spec["from_endpoint"]), None)
            if ep:
                spec["api_key"] = ep.get("api_key") or ""
                spec.setdefault("api_key_env", ep.get("api_key_env") or "")
        return spec

    # ---- routes ----------------------------------------------------------------------
    def routes(self, bp) -> None:
        @bp.get("/meta")
        def meta():
            return jsonify(kinds=llmapi.KINDS, probes=llmapi.PROBES)

        @bp.get("/servers")
        def get_servers():
            return jsonify(self.servers_view())

        @bp.post("/servers")
        def save_server():
            data = body()
            servers = self.servers()
            spec = self.resolve_endpoint_key(dict(data.get("server") or {}))
            server = llmapi.clean_server(spec, servers)
            at = next((i for i, s in enumerate(servers) if s["id"] == server["id"]), None)
            if at is None:
                servers.append(server)
            else:
                servers[at] = server
            self.save_servers(servers)
            return jsonify(saved=server["id"], **self.servers_view())

        @bp.post("/servers/test")
        def test_server():
            # Checks a server as typed in the form (nothing is saved, and the key never comes back).
            spec = self.resolve_endpoint_key(dict(body().get("server") or {}))
            server = llmapi.clean_server(spec, self.servers())
            found = llmapi.probe(server)
            return jsonify(models=llmapi.list_models(server), detected=found["detected"],
                           reachable=[r["path"] for r in found["results"] if 200 <= (r["status"] or 0) < 300])

        @bp.delete("/servers/<sid>")
        def delete_server(sid):
            self.save_servers([s for s in self.servers() if s["id"] != sid])
            return jsonify(self.servers_view())

        @bp.post("/scan")
        def scan():
            have = {s["base_url"] for s in self.servers()}
            return jsonify(found=[f for f in llmapi.scan_local() if f["base_url"] not in have])

        @bp.post("/servers/<sid>/probe")
        def probe(sid):
            return jsonify(llmapi.probe(self.server(sid)))

        @bp.post("/servers/<sid>/models")
        def models(sid):
            return jsonify(llmapi.list_models(self.server(sid)))

        @bp.post("/servers/<sid>/model-info")
        def model_info(sid):
            model = str(body().get("model") or "").strip()
            if not model:
                raise ValueError("Choose a model.")
            return jsonify(llmapi.model_info(self.server(sid), model))

        @bp.post("/servers/<sid>/tokenize")
        def tokenize(sid):
            data = body()
            messages = data.get("messages") if isinstance(data.get("messages"), list) else None
            return jsonify(llmapi.count_tokens(self.server(sid), str(data.get("model") or ""),
                                               str(data.get("text") or ""), messages))

        @bp.post("/servers/<sid>/send")
        def send(sid):
            data = body()
            server = self.server(sid)
            payload = data.get("body")
            if payload is not None and not isinstance(payload, str):
                payload = json.dumps(payload)
            method = str(data.get("method") or "POST").upper()
            path = str(data.get("path") or "")
            llmapi.build_url(server, path)  # validate before streaming starts
            if method not in llmapi.METHODS:
                raise ValueError(f"Unsupported method {method}.")
            return self.relay(server, method, path, payload, str(data.get("label") or ""))

        @bp.post("/cancel/<rid>")
        def cancel(rid):
            with self.lock:
                event = self.cancels.get(rid)
            if event:
                event.set()
            return jsonify(cancelled=bool(event))

        @bp.get("/history")
        def history():
            with self.lock:
                rows = list(self.history)
            keys = ("id", "at", "server", "server_name", "label", "method", "path", "status", "ms_ttfb", "ms_total",
                    "bytes", "lines", "error", "cancelled")
            return jsonify(history=[{k: r[k] for k in keys} for r in reversed(rows)])

        @bp.get("/history/<rid>")
        def history_item(rid):
            with self.lock:
                found = next((r for r in self.history if r["id"] == rid), None)
            if found is None:
                abort(404)
            return jsonify(found)

        @bp.delete("/history")
        def clear_history():
            with self.lock:
                self.history.clear()
            return jsonify(history=[])


PLUGIN = LlmPlugin
