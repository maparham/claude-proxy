"""A stand-in for api.anthropic.com, for the end-to-end test when it has no real gateway to send Claude requests to.

Answers /v1/models, /v1/messages (plain and streamed, as Claude Code asks) and /v1/messages/count_tokens, every
reply saying "pong". GET /_requests lists what it was sent: path and Authorization header.

  python e2e/fake_anthropic.py 18490
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEEN: list[dict] = []
MODEL = {"type": "model", "id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5", "created_at": "2025-10-01T00:00:00Z"}


def quota_headers() -> dict:
    reset = str(int(time.time()) + 3600)
    return {"anthropic-ratelimit-unified-5h-utilization": "0.12", "anthropic-ratelimit-unified-5h-reset": reset,
            "anthropic-ratelimit-unified-5h-status": "allowed"}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code: int, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        for k, v in {"content-type": ctype, "content-length": str(len(data)), **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/_requests":
            return self.send(200, SEEN)
        SEEN.append({"path": path, "authorization": self.headers.get("authorization", "")})
        if path == "/v1/models":
            return self.send(200, {"data": [MODEL], "has_more": False, "first_id": MODEL["id"], "last_id": MODEL["id"]})
        self.send(404, {"type": "error", "error": {"type": "not_found_error", "message": path}})

    def do_POST(self):
        path = self.path.split("?")[0]
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        SEEN.append({"path": path, "authorization": self.headers.get("authorization", "")})
        if path == "/v1/messages/count_tokens":
            return self.send(200, {"input_tokens": 12})
        if path != "/v1/messages":
            return self.send(404, {"type": "error", "error": {"type": "not_found_error", "message": path}})
        model = body.get("model", MODEL["id"])
        usage = {"input_tokens": 12, "output_tokens": 2, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        msg = {"id": "msg_e2e", "type": "message", "role": "assistant", "model": model, "stop_reason": "end_turn",
               "stop_sequence": None, "content": [{"type": "text", "text": "pong"}], "usage": usage}
        if not body.get("stream"):
            return self.send(200, msg, extra=quota_headers())
        events = [
            ("message_start", {"type": "message_start", "message": {**msg, "content": [], "stop_reason": None,
                                                                   "usage": {**usage, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "pong"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 2}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        data = "".join(f"event: {name}\ndata: {json.dumps(ev)}\n\n" for name, ev in events).encode()
        self.send(200, data, "text/event-stream", quota_headers())


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    print(f"fake Anthropic on port {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
