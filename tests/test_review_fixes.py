"""Regression tests for the findings in docs/superpowers/reviews/2026-09-22-implementation-review.md."""
import pytest
from fastapi.responses import JSONResponse

from claude_proxy.db import create_user
from tests.conftest import FakeUpstream, asgi_client, make_gateway, message_json, seed_oauth


# P0-1: dashboard read API requires auth and scopes non-admins
@pytest.mark.parametrize("path", ["/api/overview", "/api/users", "/api/limits"])
async def test_read_api_requires_auth(cfg, db, path):
    from claude_proxy.web import create_dashboard_app
    gw = make_gateway(cfg, db[1])
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get(path)
    assert r.status_code == 401


async def test_read_api_admin_only_for_other_users(cfg, db):
    from claude_proxy.web import create_dashboard_app
    _, key = create_user(db[1], "alice")
    gw = make_gateway(cfg, db[1])
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/api/users", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403


async def test_proxy_app_does_not_serve_dashboard(cfg, db):
    from claude_proxy.app import create_app
    _, key = create_user(db[1], "alice")
    gw = make_gateway(cfg, db[1])
    async with asgi_client(create_app(gw)) as c:
        for p in ("/dashboard", "/api/users", "/api/overview"):
            r = await c.get(p, headers={"Authorization": f"Bearer {key}"})
            assert r.status_code == 404, p


# P0-2: credential encryption fails closed without a configured key
def test_encrypt_without_key_fails_closed(monkeypatch):
    from claude_proxy import credentials
    monkeypatch.delenv("CLAUDE_PROXY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_PROXY_CREDENTIAL_KEY_FILE", raising=False)
    with pytest.raises(credentials.CredentialKeyMissing):
        credentials.encrypt_blob({"a": 1})


# P1-1: a 401 triggers a real refresh even when the token is far from expiry
async def test_upstream_401_refreshes_and_retries(cfg, db):
    conn = db[1]
    _, key = create_user(conn, "alice")
    seed_oauth(conn, access="dead", refresh="r1", expires_in=3600)
    anthropic = FakeUpstream("anthropic.fake")
    auth = FakeUpstream("auth.fake")
    cfg.credential.token_url = "http://auth.fake/v1/oauth/token"
    auth.default = lambda req: JSONResponse({"access_token": "fresh", "refresh_token": "r2", "expires_in": 28800})
    anthropic.queue.append(lambda req: JSONResponse({"type": "error", "error": {"type": "authentication_error", "message": "x"}}, status_code=401))
    gw = make_gateway(cfg, conn, anthropic, auth)
    from claude_proxy.app import create_app
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={"model": "claude-sonnet-4-6", "max_tokens": 5, "messages": []},
                         headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert len(auth.calls) == 1
    assert auth.calls[0]["json"]["refresh_token"] == "r1"
    assert anthropic.calls[1]["headers"]["authorization"] == "Bearer fresh"


async def test_concurrent_401s_trigger_one_refresh(cfg, db):
    import asyncio
    conn = db[1]
    seed_oauth(conn, access="dead", refresh="r1")
    auth = FakeUpstream("auth.fake")
    cfg.credential.token_url = "http://auth.fake/v1/oauth/token"
    auth.default = lambda req: JSONResponse({"access_token": "fresh", "refresh_token": "r2", "expires_in": 28800})
    gw = make_gateway(cfg, conn, auth)
    results = await asyncio.gather(*[gw.backend.on_unauthorized("dead") for _ in range(10)])
    assert all(results)
    assert len(auth.calls) == 1


# P1-2: cache creation is counted once in raw totals
def test_raw_tokens_count_cache_creation_once(db):
    from claude_proxy.usage import raw_tokens_sql
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    conn.execute("INSERT INTO requests(user_id, started_at, method, path, input_tokens, output_tokens, cache_creation_tokens, cache_creation_5m, cache_creation_1h, cache_read_tokens) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (uid, 1, "POST", "/v1/messages", 10, 5, 1000, 1000, 0, 0))
    total = conn.execute(f"SELECT SUM({raw_tokens_sql()}) FROM requests").fetchone()[0]
    assert total == 1015


# P1-3: a failing retry returns an Anthropic-shaped 502, not a 500
async def test_retry_connect_error_returns_502(cfg, db):
    import httpx
    conn = db[1]
    _, key = create_user(conn, "alice")
    seed_oauth(conn, access="dead")
    auth = FakeUpstream("auth.fake")
    cfg.credential.token_url = "http://auth.fake/v1/oauth/token"
    auth.default = lambda req: JSONResponse({"access_token": "fresh", "refresh_token": "r2", "expires_in": 28800})
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "x"}})
        raise httpx.ConnectError("down")

    from claude_proxy.gateway import Gateway
    http = httpx.AsyncClient(mounts={"http://anthropic.fake": httpx.MockTransport(handler),
                                     "http://auth.fake": httpx.ASGITransport(app=auth.app)})
    gw = Gateway(cfg, conn, http=http)
    from claude_proxy.app import create_app
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={"model": "claude-sonnet-4-6", "messages": []}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "api_error"


# P1-4: login is rate limited per IP
async def test_login_rate_limited(cfg, db):
    from claude_proxy.web import create_dashboard_app
    gw = make_gateway(cfg, db[1])
    async with asgi_client(create_dashboard_app(gw)) as c:
        codes = [(await c.post("/api/login", json={"username": "admin", "password": "wrong"})).status_code for _ in range(12)]
    assert codes[0] == 401
    assert 429 in codes


# P2: one retry-after header on limit rejections
async def test_single_retry_after_header(cfg, db):
    conn = db[1]
    uid, key = create_user(conn, "alice")
    seed_oauth(conn)
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,?)", (uid, "requests_daily", "*", "0", "count", 0))
    gw = make_gateway(cfg, conn, FakeUpstream("anthropic.fake"))
    from claude_proxy.app import create_app
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={"model": "claude-sonnet-4-6", "messages": []}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 429
    assert len(r.headers.get_list("retry-after")) == 1
