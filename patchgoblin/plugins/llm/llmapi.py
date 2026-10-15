"""HTTP plumbing for the LLM API console: saved servers, discovery and a recording proxy.

Every call goes through PatchGoblin (not the browser) so API keys stay here, there are no
CORS problems, and the exact bytes on the wire can be recorded for the console's inspector.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
AUTH_STYLES = ("bearer", "x-api-key", "none")
SECRET_HEADERS = ("authorization", "x-api-key", "api-key", "proxy-authorization")
MAX_RECORD = 2_000_000  # characters of a response kept in the history

# Server kinds: how to talk to them by default. "flavor" is the request format the chat starts
# with, "think" how a thinking on/off switch is sent (see plugin.js), "url" a typical base URL.
KINDS = {
    "auto": {"label": "Auto / OpenAI-compatible", "flavor": "chat", "think": "enable_thinking", "url": "http://127.0.0.1:8080"},
    "llamacpp": {"label": "llama.cpp server", "flavor": "chat", "think": "enable_thinking", "url": "http://127.0.0.1:8080"},
    "llama-swap": {"label": "llama-swap", "flavor": "chat", "think": "enable_thinking", "url": "http://127.0.0.1:8080"},
    "vllm": {"label": "vLLM", "flavor": "chat", "think": "enable_thinking", "url": "http://127.0.0.1:8000"},
    "sglang": {"label": "SGLang", "flavor": "chat", "think": "enable_thinking", "url": "http://127.0.0.1:30000"},
    "ollama": {"label": "Ollama", "flavor": "ollama", "think": "ollama", "url": "http://127.0.0.1:11434"},
    "lmstudio": {"label": "LM Studio", "flavor": "chat", "think": "none", "url": "http://127.0.0.1:1234"},
    "tgi": {"label": "Hugging Face TGI", "flavor": "chat", "think": "none", "url": "http://127.0.0.1:8080"},
    "koboldcpp": {"label": "KoboldCpp", "flavor": "chat", "think": "none", "url": "http://127.0.0.1:5001"},
    "tabbyapi": {"label": "TabbyAPI / ExLlama", "flavor": "chat", "think": "none", "url": "http://127.0.0.1:5000"},
    "hermes": {"label": "Hermes (Nous, OpenAI-compatible)", "flavor": "chat", "think": "thinking",
               "url": "https://inference-api.nousresearch.com/v1"},
    "openai": {"label": "OpenAI", "flavor": "chat", "think": "none", "url": "https://api.openai.com/v1"},
    "openrouter": {"label": "OpenRouter", "flavor": "chat", "think": "reasoning", "url": "https://openrouter.ai/api/v1"},
    "anthropic": {"label": "Anthropic", "flavor": "anthropic", "think": "anthropic", "url": "https://api.anthropic.com",
                  "auth": "x-api-key"},
}

# GET endpoints worth trying on an unknown server, and what a good answer says about it.
PROBES = [
    "/health", "/v1/models", "/models", "/props", "/slots", "/metrics", "/version", "/v1/version",
    "/api/version", "/api/tags", "/api/ps", "/running", "/get_model_info", "/get_server_info",
    "/server_info", "/info", "/api/v0/models", "/v1/model", "/api/v1/model", "/api/extra/version",
]
# Ports local LLM servers usually listen on, for "Scan this computer".
LOCAL_PORTS = (8080, 8081, 8000, 8001, 11434, 30000, 1234, 5000, 5001, 9292, 7860, 3000)


def normalize_base(url: str) -> str:
    """The server root: no trailing slash, and no trailing /v1 or pasted endpoint path."""
    base = (url or "").strip().rstrip("/")
    for tail in ("/chat/completions", "/completions", "/messages", "/models"):
        if base.endswith(tail):
            base = base[: -len(tail)].rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


def _header_lines(value) -> dict:
    """Custom headers from {"Name": "value"} or "Name: value" lines."""
    if isinstance(value, str):
        out = {}
        for line in value.splitlines():
            if line.strip():
                name, sep, val = line.partition(":")
                if not sep:
                    raise ValueError(f"Header line {line.strip()!r} needs a colon.")
                out[name.strip()] = val.strip()
        value = out
    if not isinstance(value, dict):
        raise ValueError("headers must be an object.")
    for name, val in value.items():
        if not isinstance(name, str) or not isinstance(val, str) or not name.strip() or any(c in name + val for c in "\r\n"):
            raise ValueError(f"Invalid header {name!r}.")
    return {k.strip(): v.strip() for k, v in value.items()}


def clean_server(spec, saved: list[dict]) -> dict:
    """Validate a server from the form. A blank key keeps the saved one unless clear_key is set."""
    if not isinstance(spec, dict):
        raise ValueError("server must be an object.")
    name = str(spec.get("name") or "").strip()
    base = normalize_base(str(spec.get("base_url") or ""))
    if not re.match(r"^https?://[^/\s]+", base, re.IGNORECASE):
        raise ValueError("The base URL must start with http:// or https://.")
    kind = spec.get("kind") or "auto"
    if kind not in KINDS:
        raise ValueError(f"Unknown server kind {kind!r}.")
    auth = spec.get("auth") or KINDS[kind].get("auth", "bearer")
    if auth not in AUTH_STYLES:
        raise ValueError("auth must be bearer, x-api-key or none.")
    try:
        timeout = int(spec.get("timeout") or 600)
    except (TypeError, ValueError):
        raise ValueError("timeout must be a number of seconds.") from None
    sid = str(spec.get("id") or "").strip()
    old = next((s for s in saved if s["id"] == sid), None) if sid else None
    if not sid:
        slug = re.sub(r"[^a-z0-9_-]+", "-", (name or urllib.parse.urlparse(base).netloc).lower()).strip("-")[:28] or "server"
        sid, n = slug, 2
        while any(s["id"] == sid for s in saved):
            sid, n = f"{slug[:28]}-{n}", n + 1
    if not SERVER_ID.match(sid):
        raise ValueError("Invalid server id.")
    key = str(spec.get("api_key") or "").strip()
    if not key and old and not spec.get("clear_key"):
        key = old.get("api_key", "")
    return {"id": sid, "name": name or sid, "kind": kind, "base_url": base, "auth": auth, "api_key": key,
            "api_key_env": str(spec.get("api_key_env") or "").strip(),
            "headers": _header_lines(spec.get("headers") or {}),
            "verify_tls": spec.get("verify_tls") is not False, "timeout": max(5, min(timeout, 3600)),
            "default_model": str(spec.get("default_model") or "").strip()}


def clean_servers(values) -> list[dict]:
    """Saved servers, dropping anything malformed."""
    out: list[dict] = []
    for value in values if isinstance(values, list) else []:
        try:
            server = clean_server({**value, "id": value.get("id")}, out) if isinstance(value, dict) else None
        except ValueError:
            continue
        if server and not any(s["id"] == server["id"] for s in out):
            out.append(server)
    return out


def server_key(server: dict) -> str:
    if server.get("api_key"):
        return server["api_key"]
    env = server.get("api_key_env") or ""
    return os.environ.get(env, "").strip() if env else ""


def public_server(server: dict) -> dict:
    """A server for the browser: the key is never sent, only whether there is one."""
    out = {k: v for k, v in server.items() if k != "api_key"}
    out["api_key_saved"] = bool(server.get("api_key"))
    out["api_key_present"] = bool(server_key(server))
    return out


def mask(value: str) -> str:
    """A secret header value with all but its last 4 characters hidden."""
    scheme, _, token = value.partition(" ") if value.lower().startswith("bearer ") else ("", "", value)
    shown = "…" + token[-4:] if len(token) > 8 else "…"
    return f"{scheme} {shown}".strip()


def request_headers(server: dict, extra: dict | None = None) -> dict:
    headers = {"User-Agent": "PatchGoblin-LLM-console", "Accept": "*/*"}
    headers.update(server.get("headers") or {})
    key = server_key(server)
    if key and server.get("auth") == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif key and server.get("auth") == "x-api-key":
        headers["x-api-key"] = key
    if server.get("kind") == "anthropic" or server.get("auth") == "x-api-key":
        headers.setdefault("anthropic-version", "2023-06-01")
    headers.update(extra or {})
    return headers


def masked_headers(headers: dict) -> dict:
    return {k: mask(v) if k.lower() in SECRET_HEADERS else v for k, v in headers.items()}


def build_url(server: dict, path: str) -> str:
    path = (path or "").strip()
    if not path.startswith("/") or any(c in path for c in " \r\n"):
        raise ValueError("The path must start with / and contain no spaces.")
    return server["base_url"] + path


def _context(server: dict):
    if server.get("verify_tls", True):
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def open_request(server: dict, method: str, path: str, body: str | None = None, timeout: float | None = None,
                 headers: dict | None = None):
    """Send one request. Returns ``(response, error_response)``: an open http response, or the
    HTTPError (which is also a readable response) for a non-2xx status. Network failures raise."""
    method = (method or "GET").upper()
    if method not in METHODS:
        raise ValueError(f"Unsupported method {method}.")
    hdrs = request_headers(server, headers)
    data = None
    if body is not None and method not in ("GET", "HEAD"):
        data = body.encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(build_url(server, path), data=data, method=method, headers=hdrs)
    try:
        return urllib.request.urlopen(req, timeout=timeout or server.get("timeout", 600), context=_context(server)), None
    except urllib.error.HTTPError as exc:
        return None, exc


def call(server: dict, method: str, path: str, body=None, timeout: float = 15) -> dict:
    """A small request read in full: {status, ms, headers, text, json, error}."""
    if body is not None and not isinstance(body, str):
        body = json.dumps(body)
    t0 = time.monotonic()
    out = {"path": path, "status": 0, "ms": 0, "headers": {}, "text": "", "json": None, "error": ""}
    try:
        resp, err = open_request(server, method, path, body, timeout)
        r = resp or err
        with r:
            out["status"] = r.status if resp else err.code
            out["headers"] = dict(r.headers.items())
            out["text"] = r.read(MAX_RECORD).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["error"] = str(getattr(exc, "reason", None) or exc) or type(exc).__name__
    out["ms"] = round((time.monotonic() - t0) * 1000)
    try:
        out["json"] = json.loads(out["text"]) if out["text"] else None
    except ValueError:
        pass
    return out


def ok(result: dict) -> bool:
    return 200 <= result["status"] < 300


# ---- discovery ------------------------------------------------------------------------
def detect_kind(results: dict) -> str:
    """Guess the server software from probe results ({path: call result})."""
    def j(path):
        r = results.get(path)
        return r["json"] if r and ok(r) and isinstance(r["json"], (dict, list)) else None
    if isinstance(j("/running"), dict) and "running" in j("/running"):
        return "llama-swap"
    if isinstance(j("/api/tags"), dict) and "models" in j("/api/tags"):
        return "ollama"
    if isinstance(j("/props"), dict) and ("default_generation_settings" in j("/props") or "n_ctx" in json.dumps(j("/props"))):
        return "llamacpp"
    if j("/get_server_info") is not None or j("/server_info") is not None or j("/get_model_info") is not None:
        return "sglang"
    models = j("/v1/models")
    if isinstance(models, dict) and any(isinstance(m, dict) and "max_model_len" in m for m in models.get("data") or []):
        return "vllm"
    if isinstance(j("/info"), dict) and "model_id" in j("/info"):
        return "tgi"
    if j("/api/v0/models") is not None:
        return "lmstudio"
    if "kobold" in json.dumps(j("/api/extra/version") or "").lower():
        return "koboldcpp"
    if j("/v1/model") is not None:
        return "tabbyapi"
    return "auto"


def probe(server: dict, paths=PROBES, timeout: float = 6) -> dict:
    """GET every known endpoint at once; report status, latency and a snippet of each."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip(paths, pool.map(lambda p: call(server, "GET", p, timeout=timeout), paths)))
    rows = [{"path": p, "status": r["status"], "ms": r["ms"], "error": r["error"],
             "type": r["headers"].get("Content-Type", r["headers"].get("content-type", "")),
             "text": r["text"][:20000]} for p, r in results.items()]
    return {"results": rows, "detected": detect_kind(results)}


def scan_local(timeout: float = 1.2) -> list[dict]:
    """Look for LLM servers on this computer's usual ports."""
    def one(port):
        server = {"base_url": f"http://127.0.0.1:{port}", "headers": {}, "auth": "none", "kind": "auto"}
        results = {p: call(server, "GET", p, timeout=timeout) for p in ("/v1/models", "/api/tags")}
        if not any(ok(r) for r in results.values()):
            return None
        for p in ("/running", "/props", "/get_server_info", "/api/v0/models", "/info", "/api/extra/version"):
            results[p] = call(server, "GET", p, timeout=timeout)
        return {"base_url": server["base_url"], "kind": detect_kind(results), "port": port}
    with ThreadPoolExecutor(max_workers=len(LOCAL_PORTS)) as pool:
        return [r for r in pool.map(one, LOCAL_PORTS) if r]


CTX_KEYS = ("loaded_context_length", "n_ctx", "num_ctx", "max_model_len", "context_length", "max_context_length",
            "context_window", "max_total_num_tokens", "max_seq_len", "max_input_length", "n_ctx_train")


def find_ctx(data, keys=CTX_KEYS):
    """The first context-size value found in a JSON document (searching keys in priority order)."""
    found: dict[str, int] = {}

    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                base = k.rsplit(".", 1)[-1]  # ollama: "llama.context_length"
                if base in keys and isinstance(v, (int, float)) and v > 0 and base not in found:
                    found[base] = int(v)
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node[:50]:
                walk(v, depth + 1)
    walk(data)
    for k in keys:
        if k in found:
            return found[k], k
    return None, ""


def list_models(server: dict) -> dict:
    """Models from every listing endpoint the server kind offers, merged by id."""
    kind = server.get("kind", "auto")
    paths = {"ollama": ["/api/tags", "/api/ps", "/v1/models"], "llama-swap": ["/v1/models", "/running"],
             "lmstudio": ["/api/v0/models", "/v1/models"], "anthropic": ["/v1/models?limit=100"]}.get(
        kind, ["/v1/models", "/api/tags"] if kind == "auto" else ["/v1/models"])
    with ThreadPoolExecutor(max_workers=len(paths)) as pool:
        results = dict(zip(paths, pool.map(lambda p: call(server, "GET", p), paths)))
    models: dict[str, dict] = {}

    def add(mid, **info):
        if not mid:
            return
        row = models.setdefault(mid, {"id": mid, "context_length": None, "context_source": "", "loaded": None, "info": ""})
        for k, v in info.items():
            if v not in (None, "") and row.get(k) in (None, ""):
                row[k] = v

    errors, sources = [], []
    for path, r in results.items():
        if not ok(r):
            if not (kind == "auto" and path == "/api/tags"):
                errors.append(f"{path}: {r['error'] or 'HTTP ' + str(r['status'])}")
            continue
        sources.append(path)
        data = r["json"] or {}
        if path.startswith("/v1/models") or path == "/models":
            for m in (data.get("data") or []) if isinstance(data, dict) else []:
                if isinstance(m, dict):
                    ctx, src = find_ctx(m)
                    status = (m.get("status") or {}).get("value") if isinstance(m.get("status"), dict) else None
                    add(m.get("id"), context_length=ctx, context_source=f"{path} {src}" if ctx else "",
                        info=m.get("owned_by") or m.get("display_name") or "",
                        loaded=True if status == "loaded" else None)
        elif path == "/api/tags":
            for m in data.get("models") or []:
                d = m.get("details") or {}
                add(m.get("name"), info=" ".join(x for x in (d.get("parameter_size"), d.get("quantization_level"), d.get("family")) if x))
        elif path == "/api/ps":
            for m in data.get("models") or []:
                add(m.get("name"), loaded=True, context_length=m.get("context_length"),
                    context_source="/api/ps context_length" if m.get("context_length") else "")
        elif path == "/running":
            for m in data.get("running") or []:
                add(m.get("model"), loaded=True, info=m.get("state") or "")
        elif path == "/api/v0/models":
            for m in data.get("data") or []:
                ctx, src = find_ctx(m)
                add(m.get("id"), loaded=m.get("state") == "loaded", context_length=ctx,
                    context_source=f"{path} {src}" if ctx else "", info=" ".join(str(x) for x in (m.get("type"), m.get("quantization")) if x))
    if kind in ("ollama",) or "/api/ps" in results:
        for row in models.values():
            if row["loaded"] is None:
                row["loaded"] = False
    return {"models": sorted(models.values(), key=lambda m: m["id"].lower()), "sources": sources, "errors": errors}


def model_info(server: dict, model: str) -> dict:
    """Everything the server says about one model, and its context size if any of it says."""
    kind = server.get("kind", "auto")
    q = urllib.parse.quote(model, safe="")
    reqs = [("GET", "/v1/models", None)]
    if kind in ("llamacpp", "auto"):
        reqs += [("GET", "/props", None), ("GET", "/slots", None)]
    if kind == "llama-swap":
        running = call(server, "GET", "/running")
        names = [m.get("model") for m in ((running["json"] or {}).get("running") or [])] if ok(running) else []
        if model in names:  # /upstream/<model>/… would load an unloaded model, so only ask loaded ones
            reqs += [("GET", f"/upstream/{q}/props", None)]
    if kind in ("ollama", "auto"):
        reqs += [("POST", "/api/show", {"model": model}), ("GET", "/api/ps", None)]
    if kind in ("sglang", "auto"):
        reqs += [("GET", "/get_model_info", None), ("GET", "/get_server_info", None)]
    if kind == "lmstudio":
        reqs += [("GET", f"/api/v0/models/{q}", None)]
    if kind == "tgi":
        reqs += [("GET", "/info", None)]
    if kind == "anthropic":
        reqs = [("GET", f"/v1/models/{q}", None)]
    with ThreadPoolExecutor(max_workers=len(reqs)) as pool:
        results = list(pool.map(lambda r: (f"{r[0]} {r[1]}", call(server, r[0], r[1], r[2])), reqs))
    details, found = {}, []  # found: (priority, size, source)
    for label, r in results:
        if not ok(r):
            continue
        data = r["json"] if r["json"] is not None else r["text"][:5000]
        if label.endswith("/v1/models") and isinstance(data, dict):
            data = next((m for m in data.get("data") or [] if isinstance(m, dict) and m.get("id") == model), None)
            if data is None:
                continue
        if label.endswith("/api/ps") and isinstance(data, dict):
            data = next((m for m in data.get("models") or [] if m.get("name") == model), None)
            if data is None:
                continue
        details[label] = data
        if label.endswith("/api/show") and isinstance(data, dict):
            # num_ctx in the Modelfile is what Ollama runs with (model_info only has the model's maximum)
            m = re.search(r"num_ctx\s+(\d+)", data.get("parameters") or "")
            if m:
                found.append((CTX_KEYS.index("num_ctx"), int(m.group(1)), f"{label} parameters num_ctx"))
        ctx, key = find_ctx(data)
        if ctx:
            found.append((CTX_KEYS.index(key), ctx, f"{label} {key}"))
    best = min(found) if found else (0, None, "")  # runtime sizes (n_ctx…) beat training maximums
    return {"model": model, "context_length": best[1], "context_source": best[2], "details": details}


def count_tokens(server: dict, model: str, text: str, messages: list | None) -> dict:
    """Count tokens with whichever tokenizer endpoint the server has."""
    kind = server.get("kind", "auto")
    tries = []
    if kind == "anthropic":
        tries.append(("/v1/messages/count_tokens", {"model": model, "messages": messages or [{"role": "user", "content": text}]}))
    else:
        if messages:
            tries.append(("/tokenize", {"model": model, "messages": messages}))  # vLLM applies the chat template
        tries += [("/tokenize", {"model": model, "prompt": text}), ("/tokenize", {"content": text}),  # vLLM, llama.cpp
                  ("/v1/tokenize", {"model": model, "prompt": text})]
        if kind == "llama-swap":
            q = urllib.parse.quote(model, safe="")
            tries = [(f"/upstream/{q}/tokenize", {"content": text})] + tries
    attempts = []
    for path, body in tries:
        r = call(server, "POST", path, body)
        attempts.append(f"{path}: {r['error'] or r['status']}")
        data = r["json"] if ok(r) else None
        if isinstance(data, dict):
            count = data.get("count") or data.get("input_tokens")
            if count is None and isinstance(data.get("tokens"), list):
                count = len(data["tokens"])
            # A server that ignores the fields it doesn't know (llama.cpp given "messages") counts nothing.
            if isinstance(count, int) and (count > 0 or not text.strip()):
                return {"count": count, "via": path, "templated": "messages" in body, "raw": data}
    raise ValueError("No tokenizer endpoint answered (" + "; ".join(attempts) + ").")
