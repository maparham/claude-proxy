import json

import pytest
from fastapi.responses import JSONResponse

from claude_proxy.app import create_app
from claude_proxy.db import create_user, revoke, set_enabled
from tests.conftest import asgi_client, make_gateway, message_json, seed_oauth, sse_events, sse_response

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def setup(cfg, db, anthropic, meta, monkeypatch):
    conn = db[1]
    uid, key = create_user(conn, "alice")
    seed_oauth(conn, access="oauth-secret-token")
    cfg.routes[0].base_url = "http://meta.fake"
    monkeypatch.setenv("META_API_KEY", "meta-key-123")
    gw = make_gateway(cfg, conn, anthropic, meta)
    return gw, conn, uid, {"Authorization": f"Bearer {key}", "anthropic-beta": "claude-code-20250219", "user-agent": "claude-cli/2.1.280 (external, cli)"}


def last_request(conn):
    return conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()


async def test_streaming_bytes_forwarded_unchanged_and_metered(setup, anthropic):
    gw, conn, uid, h = setup
    anthropic.default = lambda req: sse_response(model="claude-sonnet-5", input_tokens=120, output_tokens=64, cache_read=1000)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, "stream": True}, headers=h)
    assert r.status_code == 200
    assert r.content == sse_events(model="claude-sonnet-5", input_tokens=120, output_tokens=64, cache_read=1000)
    row = last_request(conn)
    assert (row["user_id"], row["provider"], row["stream"], row["complete"]) == (uid, "anthropic", 1, 1)
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (120, 64, 1000)
    assert row["model"] == "claude-sonnet-5"


async def test_anthropic_request_body_is_byte_identical_and_credentials_swapped(setup, anthropic):
    gw, conn, uid, h = setup
    body = b'{"model":"claude-sonnet-5",  "max_tokens":1,"messages":[]}'
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages?beta=true", content=body, headers={**h, "content-type": "application/json"})
    call = anthropic.calls[0]
    assert call["body"] == body
    assert call["path"] == "/v1/messages"
    assert call["headers"]["authorization"] == "Bearer oauth-secret-token"
    assert call["headers"]["anthropic-beta"] == "claude-code-20250219, oauth-2025-04-20"
    assert call["headers"]["user-agent"].startswith("claude-cli/")
    assert "x-api-key" not in call["headers"]


async def test_quota_headers_are_recorded(setup, anthropic):
    gw, conn, uid, h = setup
    anthropic.default = lambda req: JSONResponse(message_json(), headers={
        "anthropic-ratelimit-unified-5h-utilization": "0.25", "anthropic-ratelimit-unified-5h-reset": "1790000000",
        "anthropic-ratelimit-unified-7d-utilization": "0.4", "anthropic-ratelimit-unified-7d-reset": "1790500000"})
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages", json=MSG, headers=h)
    rows = {r["bucket"]: r["utilization_pct"] for r in conn.execute("SELECT * FROM quota_snapshots")}
    assert rows == {"5h": pytest.approx(25), "7d": pytest.approx(40)}


async def test_muse_route_uses_meta_key_and_never_sees_oauth(setup, anthropic, meta):
    gw, conn, uid, h = setup
    meta.default = lambda req: sse_response(model="muse-spark-1.3", input_tokens=50, output_tokens=20)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, "model": "muse-spark", "stream": True}, headers=h)
    assert r.status_code == 200
    assert anthropic.calls == []
    call = meta.calls[0]
    assert call["headers"]["authorization"] == "Bearer meta-key-123"
    assert "anthropic-beta" not in call["headers"]
    everything = json.dumps(call["headers"]) + call["body"].decode()
    assert "oauth-secret-token" not in everything
    assert "sk-proxy-" not in everything
    assert call["json"]["model"] == "muse-spark-1.3"
    assert call["json"]["messages"] == MSG["messages"]
    row = last_request(conn)
    assert (row["provider"], row["model"], row["input_tokens"], row["output_tokens"]) == ("meta", "muse-spark-1.3", 50, 20)
    assert conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0] == 0


async def test_muse_count_tokens_is_routed_to_meta(setup, anthropic, meta):
    gw, conn, uid, h = setup
    meta.default = lambda req: JSONResponse({"input_tokens": 12})
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages/count_tokens", json={**MSG, "model": "muse-spark"}, headers=h)
    assert r.json() == {"input_tokens": 12}
    assert meta.calls[0]["path"] == "/v1/messages/count_tokens"
    assert anthropic.calls == []


async def test_muse_route_without_key_fails_closed(setup, meta, monkeypatch):
    gw, conn, uid, h = setup
    monkeypatch.delenv("META_API_KEY")
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, "model": "muse-spark"}, headers=h)
    assert r.status_code == 503
    assert "META_API_KEY" in r.json()["error"]["message"]
    assert meta.calls == []


async def test_models_list_includes_route_models(setup, anthropic):
    gw, conn, uid, h = setup
    anthropic.default = lambda req: JSONResponse({"data": [{"type": "model", "id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"}],
                                                  "has_more": False, "first_id": "claude-sonnet-5", "last_id": "claude-sonnet-5"})
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/v1/models", headers=h)
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["claude-sonnet-5", "muse-spark"]


async def test_upstream_quota_429_passed_through_and_classified(setup, anthropic):
    gw, conn, uid, h = setup
    anthropic.default = lambda req: JSONResponse({"type": "error", "error": {"type": "rate_limit_error", "message": "x"}}, status_code=429,
                                                 headers={"retry-after": "300", "anthropic-ratelimit-unified-status": "rejected",
                                                          "anthropic-ratelimit-unified-5h-utilization": "1.0"})
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 429 and r.headers["retry-after"] == "300"
    assert last_request(conn)["error_type"] == "upstream_quota"


async def test_limit_rejection_recorded_and_not_forwarded(setup, anthropic):
    gw, conn, uid, h = setup
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (uid, "requests_daily", "*", "0", "count"))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 429
    assert anthropic.calls == []
    assert last_request(conn)["rejected_by"] == "requests_daily"


async def test_disabled_user_gets_403_and_revoked_gets_401(setup, anthropic):
    gw, conn, uid, h = setup
    set_enabled(conn, uid, False)
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages", json=MSG, headers=h)).status_code == 403
        revoke(conn, uid)
        assert (await c.post("/v1/messages", json=MSG, headers=h)).status_code == 401
    assert anthropic.calls == []


async def test_x_api_key_is_accepted_as_virtual_key(setup, anthropic):
    gw, conn, uid, h = setup
    key = h["Authorization"].split()[1]
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers={"x-api-key": key})
    assert r.status_code == 200
    assert anthropic.calls[0]["headers"]["authorization"] == "Bearer oauth-secret-token"


async def test_truncated_stream_recorded_incomplete(setup, anthropic):
    gw, conn, uid, h = setup
    anthropic.default = lambda req: sse_response(stop=False)
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages", json={**MSG, "stream": True}, headers=h)
    assert last_request(conn)["complete"] == 0


async def test_needs_login_returns_503(setup):
    gw, conn, uid, h = setup
    conn.execute("UPDATE credentials SET state='needs_login'")
    gw.backend._cache = None
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 503
    assert "claude-proxy login" in r.json()["error"]["message"]


async def test_session_and_version_recorded(setup):
    gw, conn, uid, h = setup
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages", json=MSG, headers={**h, "x-claude-code-session-id": "sess-1"})
    row = last_request(conn)
    assert row["session_id"] == "sess-1"
    assert row["client_version"] == "claude-cli/2.1.280 (external, cli)"


async def test_requested_model_is_recorded(setup, meta):
    gw, conn, uid, h = setup
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages", json={**MSG, "model": "muse-spark"}, headers=h)
    row = last_request(conn)
    assert (row["requested_model"], row["model"]) == ("muse-spark", "muse-spark-1.3")


async def test_route_can_drop_body_fields(setup, meta):
    gw, conn, uid, h = setup
    gw.cfg.routes[0].drop_body_fields = ["context_management", "output_config"]
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages", json={**MSG, "model": "muse-spark", "context_management": {"edits": []}, "output_config": {"effort": "high"}}, headers=h)
    sent = meta.calls[0]["json"]
    assert "context_management" not in sent and "output_config" not in sent
    assert sent["model"] == "muse-spark-1.3" and sent["messages"] == MSG["messages"]
