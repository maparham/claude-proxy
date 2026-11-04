"""API clients' requests kept in full (contents.py): what is kept, who may read it, and when it goes."""
import time

import pytest
from fastapi.responses import JSONResponse

from claude_proxy import api_credit, contents, db
from claude_proxy.app import create_app
from claude_proxy.db import create_user, revoke
from claude_proxy.forwarder import SESSION_HEADERS
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client, make_gateway, message_json, seed_oauth, sse_events, sse_response

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "system": "Be brief.", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def env(cfg, db, anthropic):
    conn = db[1]
    cfg.api.enabled = True
    uid, full = create_user(conn, "alice")
    _, bob = create_user(conn, "bob")
    _, admin = create_user(conn, "admin", role="admin")
    seed_oauth(conn)
    gw = make_gateway(cfg, conn, anthropic)
    api_credit.set_rate(conn, "claude-sonnet-5*", 3, 15)
    api_credit.grant(conn, uid, 10, "x", None)
    _, raw = api_credit.create_key(conn, uid, "laptop")
    return gw, conn, uid, {"x-api-key": raw}, {"alice": full, "bob": bob, "admin": admin}


def bearer(k):
    return {"Authorization": f"Bearer {k}"}


async def test_a_streamed_api_request_is_kept_and_put_back_together(env, anthropic):
    gw, conn, uid, h, keys = env
    anthropic.queue.append(lambda req: sse_response(model="claude-sonnet-5", text="Hello there, friend"))
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages", json={**MSG, "stream": True}, headers=h)).status_code == 200
    async with asgi_client(create_dashboard_app(gw)) as c:
        [row] = (await c.get("/api/requests", headers=bearer(keys["alice"]))).json()["requests"]
        assert row["has_content"] is True
        d = (await c.get(f"/api/requests/{row['id']}/content", headers=bearer(keys["alice"]))).json()
        admin = await c.get(f"/api/requests/{row['id']}/content", headers=bearer(keys["admin"]))
        other = await c.get(f"/api/requests/{row['id']}/content", headers=bearer(keys["bob"]))
    assert d["request"] == {**MSG, "stream": True}   # as the client sent it, without the gateway's identity block
    assert d["stream"] is True and d["truncated"] is False
    assert d["reply"]["content"] == [{"type": "text", "text": "Hello there, friend"}]
    assert d["reply"]["stop_reason"] == "end_turn" and d["reply"]["usage"]["output_tokens"] == 40
    assert admin.status_code == 200 and other.status_code == 404


async def test_a_plain_reply_is_kept_as_json(env, anthropic):
    gw, conn, uid, h, keys = env
    anthropic.queue.append(lambda req: JSONResponse(message_json(model="claude-sonnet-5", text="ok")))
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages", json=MSG, headers=h)
    rid = conn.execute("SELECT id FROM requests ORDER BY id DESC LIMIT 1").fetchone()[0]
    d = contents.load(conn, rid)
    assert d["stream"] is False and d["reply"]["content"] == [{"type": "text", "text": "ok"}]


async def test_claude_code_traffic_is_never_kept(env, anthropic):
    gw, conn, uid, h, keys = env
    anthropic.queue.extend([lambda req: JSONResponse(message_json()), lambda req: JSONResponse(message_json())])
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages", json=MSG, headers=bearer(keys["alice"]))                 # Claude Code's own key
        await c.post("/v1/messages", json=MSG, headers={**h, SESSION_HEADERS[0]: "s-1"})   # Claude Code on an API key
    assert conn.execute("SELECT COUNT(*) FROM requests WHERE rejected_by IS NULL").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM request_contents").fetchone()[0] == 0


def test_tool_use_and_thinking_are_put_back_together():
    raw = b"".join(f"event: x\ndata: {d}\n\n".encode() for d in [
        '{"type":"message_start","message":{"id":"m","role":"assistant","content":[],"usage":{"input_tokens":5}}}',
        '{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}',
        '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"hmm"}}',
        '{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"t","name":"Read","input":{}}}',
        '{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}',
        '{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"a.py\\"}"}}',
        '{"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":9}}'])
    m = contents.assemble(raw)
    assert m["content"] == [{"type": "thinking", "thinking": "hmm"},
                            {"type": "tool_use", "id": "t", "name": "Read", "input": {"path": "a.py"}}]
    assert m["stop_reason"] == "tool_use" and m["usage"] == {"input_tokens": 5, "output_tokens": 9}
    assert contents.assemble(b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error"}}\n\n') is None


def test_a_reply_over_the_cap_is_cut(monkeypatch):
    monkeypatch.setattr(contents, "MAX_REPLY_BYTES", 10)
    cap = contents.Capture()
    cap.feed(b"123456")
    cap.feed(b"789abc")
    cap.feed(b"def")
    assert bytes(cap.buf) == b"123456789a" and cap.truncated


def _kept(conn, uid, started_at):
    rid = db.insert_request(conn, user_id=uid, started_at=started_at, method="POST", path="/v1/messages")
    contents.save(conn, rid, b"{}", sse_events(), True, False)
    return rid


def test_contents_go_after_retention_even_when_usage_is_kept(env):
    gw, conn, uid, h, keys = env
    old, new = _kept(conn, uid, time.time() - 200 * 86400), _kept(conn, uid, time.time())
    db.cleanup(conn, 180, keep_usage=True)
    assert [r[0] for r in conn.execute("SELECT request_id FROM request_contents")] == [new]
    assert conn.execute("SELECT 1 FROM requests WHERE id=?", (old,)).fetchone()   # the usage row stays


def test_deleting_a_user_deletes_their_contents(env):
    gw, conn, uid, h, keys = env
    _kept(conn, uid, time.time())
    revoke(conn, uid)
    db.delete_user(conn, uid)
    assert conn.execute("SELECT COUNT(*) FROM request_contents").fetchone()[0] == 0
