#!/usr/bin/env python3
"""Synthetic OpenAI-compatible provider for the isolated AutoRouter dev environment (not production)."""
import http.server, json, os, sys, time, hmac

LOGDIR = os.environ.get("PROVIDER_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "runtime", "provider-logs"))
os.makedirs(LOGDIR, exist_ok=True)
NAME, PORT, MODEL = sys.argv[1], int(sys.argv[2]), sys.argv[3]
LOG = os.path.join(LOGDIR, NAME + ".jsonl")
MODE_FILE = os.path.join(LOGDIR, NAME + ".mode")
os.makedirs(LOGDIR, exist_ok=True)


def mode():
    try:
        return open(MODE_FILE, encoding="utf-8").read().strip() or "ok"
    except Exception:
        return "ok"


def fixture_account_label(authorization: str) -> str:
    """Recognize ONLY fixed local fixture tokens, never persist or hash credentials.

    This is a loopback test helper, not a real credential identity producer.
    Any non-fixture Authorization header is reduced to 'unrecognized'.
    """
    synthetic_tokens = (
        ("alpha", "sk-isolation-alpha-not-a-secret"),
        ("beta", "sk-isolation-beta-not-a-secret"),
    )
    for label, token in synthetic_tokens:
        if (hmac.compare_digest(authorization, "Bearer " + token)
                or hmac.compare_digest(authorization, token)):
            return label
    return "unrecognized"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _log(self, entry):
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **entry}) + "\n")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("models"):
            self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": NAME}]})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        path = self.path.rstrip("/")
        if path.endswith("chat/completions"):
            m = mode()
            raw = self._body()
            try:
                req = json.loads(raw or b"{}")
            except Exception:
                self._json(400, {"error": {"message": "bad json"}})
                return
            self._log({"event": "request", "model": req.get("model"), "stream": bool(req.get("stream")), "mode": m,
                      "body": raw[:300].decode("utf-8", "replace") if not req.get("stream") else None,
                      # Isolated test fixture identity only: never record or hash bearer tokens.
                      "auth_account": fixture_account_label(self.headers.get("Authorization") or "")})
            if m == "ratelimit":
                body = b'{"error": {"message": "rate limited"}}'
                self.send_response(429)
                self.send_header("Retry-After", "2")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self._log({"event": "response", "status": 429})
                return
            if m == "auth":
                self._json(401, {"error": {"message": "invalid api key"}})
                self._log({"event": "response", "status": 401})
                return
            if m == "unavailable":
                self._json(503, {"error": {"message": "provider unavailable"}})
                self._log({"event": "response", "status": 503})
                return
            if m == "timeout":
                time.sleep(30)
                return
            if m == "stream_fail_mid":
                # Streaming response: two chunks COMMIT visible output, then the
                # stream breaks irrecoverably. AutoRouter must not replay the
                # request after any output may have committed.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                chunk1 = {"id": "chatcmpl-syn", "object": "chat.completion.chunk", "created": int(time.time()),
                          "model": MODEL, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "committed partial output"}, "finish_reason": None}]}
                chunk2 = {"id": "chatcmpl-syn", "object": "chat.completion.chunk", "created": int(time.time()),
                          "model": MODEL, "choices": [{"index": 0, "delta": {"content": " still more"}, "finish_reason": None}]}
                for chunk in (chunk1, chunk2):
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.flush()
                self._log({"event": "response", "status": 200, "stream": True, "interrupted_mid_stream": True})
                return
            content = f"synthetic-{NAME}/{MODEL}: the quick brown fox jumps over the lazy dog"
            if req.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                chunks = [
                    {"id": "chatcmpl-syn", "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": MODEL, "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]},
                    {"id": "chatcmpl-syn", "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": MODEL, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                ]
                for chunk in chunks:
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self._log({"event": "response", "status": 200, "stream": True})
            else:
                self._json(200, {"id": "chatcmpl-syn", "object": "chat.completion", "created": int(time.time()),
                                 "model": MODEL,
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                              "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 9, "completion_tokens": 9, "total_tokens": 18}})
                self._log({"event": "response", "status": 200, "stream": False})
            return
        self._json(404, {"error": {"message": "not found"}})

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
