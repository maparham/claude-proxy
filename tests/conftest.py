import base64
import json
import os
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from claude_proxy.config import Config
from claude_proxy.db import init_db


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    # Never let a test find the real ~/.claude-gateway (its key or its database).
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_PROXY_DB", raising=False)
    monkeypatch.delenv("CLAUDE_PROXY_CREDENTIAL_KEY_FILE", raising=False)


@pytest.fixture(autouse=True)
def credential_key(monkeypatch, isolated_home):
    monkeypatch.setenv("CLAUDE_PROXY_CREDENTIAL_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    monkeypatch.delenv("CLAUDE_PROXY_DEV_INSECURE_KEY", raising=False)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    conn = init_db(path)
    yield path, conn
    conn.close()


@pytest.fixture
def cfg(db):
    c = Config()
    c.db.path = db[0]
    c.upstream.base_url = "http://anthropic.fake"
    return c


class FakeUpstream:
    """Records every request and replays queued responses.

    Queue entries are callables taking the parsed request and returning a Response.
    When the queue is empty, `default` is used.
    """

    def __init__(self, name: str):
        self.name = name
        self.calls: list[dict] = []
        self.queue: list = []
        self.default = lambda req: JSONResponse(message_json(req.get("json", {}).get("model", "m")))
        self.app = FastAPI()

        @self.app.api_route("/{path:path}", methods=["GET", "POST"])
        async def handle(request: Request, path: str):
            body = await request.body()
            try:
                parsed = json.loads(body) if body else {}
            except ValueError:
                parsed = {}
            req = {"path": "/" + path, "method": request.method, "headers": dict(request.headers), "body": body, "json": parsed}
            self.calls.append(req)
            fn = self.queue.pop(0) if self.queue else self.default
            return fn(req)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=f"http://{self.name}")


def message_json(model="claude-sonnet-4-6", input_tokens=10, output_tokens=5, cache_creation=0, cache_read=0, cc5=None, cc1=None, text="hi"):
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens,
             "cache_creation_input_tokens": cache_creation, "cache_read_input_tokens": cache_read}
    if cc5 is not None or cc1 is not None:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": cc5 or 0, "ephemeral_1h_input_tokens": cc1 or 0}
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": usage}


def sse_events(model="claude-sonnet-4-6", input_tokens=100, output_tokens=40, cache_creation=0, cache_read=0, stop=True, text="Hello"):
    events = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_s", "type": "message", "role": "assistant", "model": model, "content": [],
                                                                "usage": {"input_tokens": input_tokens, "output_tokens": 1, "cache_creation_input_tokens": cache_creation, "cache_read_input_tokens": cache_read}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        *(("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[i:i + 8]}})
          for i in range(0, len(text), 8)),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": None}, "usage": {"output_tokens": output_tokens // 2}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": output_tokens}}),
    ]
    if stop:
        events.append(("message_stop", {"type": "message_stop"}))
    return b"".join(f"event: {e}\ndata: {json.dumps(d)}\n\n".encode() for e, d in events)


def sse_response(headers=None, **kw):
    body = sse_events(**kw)

    async def gen():
        for i in range(0, len(body), 37):
            yield body[i:i + 37]

    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers or {})


@pytest.fixture
def anthropic():
    return FakeUpstream("anthropic.fake")


@pytest.fixture
def meta():
    return FakeUpstream("meta.fake")


def seed_oauth(conn, access="oauth-access", refresh="oauth-refresh", expires_in=3600):
    from claude_proxy.credentials import encrypt_blob
    exp = int(time.time()) + expires_in
    conn.execute(
        "INSERT OR REPLACE INTO credentials(backend, encrypted_blob, expires_at, updated_at, state) VALUES('oauth',?,?,?,'active')",
        (encrypt_blob({"access_token": access, "refresh_token": refresh, "expires_at": exp}), exp, int(time.time())),
    )


def mounted_client(*upstreams: FakeUpstream) -> httpx.AsyncClient:
    return httpx.AsyncClient(mounts={f"http://{u.name}": httpx.ASGITransport(app=u.app) for u in upstreams})


def make_gateway(cfg, conn, *upstreams: FakeUpstream):
    from claude_proxy.gateway import Gateway
    return Gateway(cfg, conn, http=mounted_client(*upstreams))


def asgi_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
