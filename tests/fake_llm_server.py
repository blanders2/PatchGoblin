"""A tiny fake LLM server (llama.cpp-style OpenAI API plus a bit of Ollama) for the LLM console tests.

Run it directly to try the console by hand: ``python tests/fake_llm_server.py 18080``.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "sk-test-123456"


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-llama"
    requests: list = []  # (method, path, headers, body) of every request, for assertions

    def log_message(self, *args):
        pass

    def _send(self, status, data, ctype="application/json"):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        Handler.requests.append((self.command, self.path, dict(self.headers.items()), body))
        return json.loads(body) if body else {}

    def _authed(self):
        if self.server.require_key and self.headers.get("Authorization") != f"Bearer {KEY}":
            self._send(401, {"error": {"message": "Invalid API key", "type": "authentication_error"}})
            return False
        return True

    def do_GET(self):
        self._record()
        if not self._authed():
            return
        routes = {
            "/health": {"status": "ok"},
            "/v1/models": {"object": "list", "data": [
                {"id": "qwen3-8b", "object": "model", "owned_by": "llamacpp", "meta": {"n_ctx_train": 40960}},
                {"id": "tiny", "object": "model", "owned_by": "llamacpp"}]},
            "/props": {"default_generation_settings": {"n_ctx": 8192}, "model_path": "/models/qwen3-8b.gguf"},
        }
        if self.path in routes:
            self._send(200, routes[self.path])
        else:
            self._send(404, {"error": {"message": "File Not Found", "code": 404}})

    def do_POST(self):
        req = self._record()
        if not self._authed():
            return
        if self.path == "/tokenize":
            self._send(200, {"tokens": list(range(len((req.get("content") or "").split())))})
        elif self.path == "/v1/chat/completions":
            self.chat(req)
        elif self.path == "/api/chat":
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for part in ("Hel", "lo"):
                self.wfile.write((json.dumps({"model": req["model"], "message": {"role": "assistant", "content": part}, "done": False}) + "\n").encode())
            self.wfile.write((json.dumps({"model": req["model"], "message": {"role": "assistant", "content": ""}, "done": True,
                                          "done_reason": "stop", "prompt_eval_count": 7, "eval_count": 2,
                                          "eval_duration": 20_000_000}) + "\n").encode())
        else:
            self._send(404, {"error": {"message": "File Not Found", "code": 404}})

    def chat(self, req):
        if not req.get("messages"):
            self._send(400, {"error": {"message": "'messages' is required", "type": "invalid_request_error"}})
            return
        words = ["Hello", " there", "!"]
        usage = {"prompt_tokens": 12, "completion_tokens": len(words), "total_tokens": 12 + len(words)}
        timings = {"prompt_n": 12, "prompt_per_second": 900.0, "predicted_n": 3, "predicted_per_second": 42.0}
        if not req.get("stream"):
            self._send(200, {"id": "c1", "object": "chat.completion", "model": req.get("model"),
                             "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                 "role": "assistant", "content": "".join(words), "reasoning_content": "Thinking hard."}}],
                             "usage": usage, "timings": timings})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def chunk(delta, finish=None, **extra):
            data = {"id": "c1", "object": "chat.completion.chunk", "model": req.get("model"),
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
            self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
            time.sleep(self.server.delay)
        chunk({"role": "assistant", "reasoning_content": "Thinking"})
        for w in words:
            chunk({"content": w})
        chunk({}, "stop", timings=timings)
        if (req.get("stream_options") or {}).get("include_usage"):
            self.wfile.write(f"data: {json.dumps({'id': 'c1', 'choices': [], 'usage': usage})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def start(port=0, require_key=False, delay=0.0):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.handle_error = lambda request, address: None  # clients hanging up mid-stream are expected
    server.require_key = require_key
    server.delay = delay
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    srv = start(int(sys.argv[1]) if len(sys.argv) > 1 else 18080, delay=0.15)
    print(f"fake LLM server on http://127.0.0.1:{srv.server_address[1]}")
    threading.Event().wait()
