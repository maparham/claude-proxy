"""Routes-only keys on the proxy (spec 2.3): third-party routes and the model list, nothing else, ever."""
import time

import pytest

from claude_proxy.app import create_app, scope_allows
from claude_proxy.config import Route
from claude_proxy.db import create_user, revoke, set_enabled, set_routes_key
from tests.conftest import asgi_client, make_gateway, seed_oauth, sse_response

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
MUSE = {**MSG, "model": "muse-spark"}
ROUTE = Route(name="meta", base_url="http://meta.fake", api_key_env="META_API_KEY", models=["muse-spark*"])
REFUSAL = "This key is for third-party models only (muse-spark*). Claude models need your Claude Code key."


@pytest.fixture
def env(cfg, db, anthropic, meta, monkeypatch):
    conn = db[1]
    uid, full = create_user(conn, "alice")
    routes = set_routes_key(conn, uid)
    seed_oauth(conn, access="oauth-secret-token")
    cfg.routes[0].base_url = "http://meta.fake"
    monkeypatch.setenv("META_API_KEY", "meta-key-123")
    gw = make_gateway(cfg, conn, anthropic, meta)
    return gw, conn, uid, {"authorization": f"Bearer {full}"}, {"x-api-key": routes}


def last_request(conn):
    return conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()


def forbid_credential(gw, monkeypatch):
    async def boom():
        raise AssertionError("a routes-only key made the gateway load the subscription credential")
    monkeypatch.setattr(gw.backend, "upstream_headers", boom)


@pytest.mark.parametrize("scope,method,path,routed,allowed", [
    ("full", "POST", "/v1/messages", False, True),
    ("full", "GET", "/v1/anything", False, True),
    ("routes", "POST", "/v1/messages", True, True),
    ("routes", "POST", "/v1/messages/count_tokens", True, True),
    ("routes", "GET", "/v1/models", False, True),
    ("routes", "POST", "/v1/messages", False, False),
    ("routes", "POST", "/v1/messages/count_tokens", False, False),
    ("routes", "POST", "/v1/models", False, False),
    ("routes", "HEAD", "/v1/models", False, False),
    ("routes", "GET", "/v1/messages", False, False),
    ("routes", "GET", "/v1/foo", False, False),
    ("routes", "POST", "/v1/foo", True, False),
    ("bogus", "POST", "/v1/messages", True, True),     # unknown scope: fail closed, treated like "routes"
    ("bogus", "POST", "/v1/messages", False, False),
    ("bogus", "GET", "/v1/models", False, True),
])
def test_scope_allows(scope, method, path, routed, allowed):
    assert scope_allows(scope, method, path, ROUTE if routed else None) is allowed


async def test_claude_on_a_routes_key_is_refused_before_anything_upstream(env, anthropic, monkeypatch):
    gw, conn, uid, full, routes = env
    forbid_credential(gw, monkeypatch)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=routes)
    assert r.status_code == 403
    assert r.json() == {"type": "error", "error": {"type": "permission_error", "message": REFUSAL}}
    assert anthropic.calls == []
    row = last_request(conn)
    assert (row["user_id"], row["status"], row["rejected_by"]) == (uid, 403, "key_scope")


async def test_scope_is_checked_before_limits(env, anthropic):
    gw, conn, uid, full, routes = env
    now = time.time()
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)",
                 (uid, "requests_daily", "claude-*", "1", "count"))
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status) VALUES(?,?,?,?,?,?,?,?)",
                 (uid, now - 10, now - 9, "POST", "/v1/messages", "anthropic", "claude-sonnet-5", 200))
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages", json=MSG, headers=full)).status_code == 429
        r = await c.post("/v1/messages", json=MSG, headers=routes)
    assert r.status_code == 403 and last_request(conn)["rejected_by"] == "key_scope"
    assert anthropic.calls == []


async def test_count_tokens_for_claude_on_a_routes_key_is_refused(env, anthropic, monkeypatch):
    gw, conn, uid, full, routes = env
    forbid_credential(gw, monkeypatch)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages/count_tokens", json=MSG, headers=routes)
    assert r.status_code == 403 and anthropic.calls == []


async def test_model_list_on_a_routes_key_is_local_and_route_only(env, anthropic, monkeypatch):
    gw, conn, uid, full, routes = env
    forbid_credential(gw, monkeypatch)
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/v1/models?limit=1000", headers=routes)
    assert r.status_code == 200
    assert r.json() == {"data": [{"type": "model", "id": "muse-spark", "display_name": "Muse Spark 1.3",
                                  "created_at": "2026-01-01T00:00:00Z", "max_input_tokens": 1048576, "max_tokens": 32000}],
                        "has_more": False, "first_id": "muse-spark", "last_id": "muse-spark"}
    assert anthropic.calls == []
    assert (last_request(conn)["provider"], last_request(conn)["status"]) == ("gateway", 200)


@pytest.mark.parametrize("method,path", [("GET", "/v1/foo"), ("POST", "/v1/models"), ("GET", "/v1/messages")])
async def test_other_paths_on_a_routes_key_are_refused(env, anthropic, method, path):
    gw, conn, uid, full, routes = env
    async with asgi_client(create_app(gw)) as c:
        r = await c.request(method, path, headers=routes)
    assert r.status_code == 403 and anthropic.calls == []


async def test_unreadable_body_on_a_routes_key_is_a_scope_refusal(env, anthropic):
    gw, conn, uid, full, routes = env
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", content=b"not json", headers={**routes, "content-type": "application/json"})
    assert r.status_code == 403 and last_request(conn)["rejected_by"] == "key_scope"


@pytest.mark.parametrize("header", ["x-api-key", "authorization"])
async def test_routes_key_reaches_muse_with_the_route_key(env, anthropic, meta, header):
    gw, conn, uid, full, routes = env
    key = routes["x-api-key"]
    h = {"x-api-key": key} if header == "x-api-key" else {"authorization": f"Bearer {key}"}
    meta.default = lambda req: sse_response(model="muse-spark-1.3", input_tokens=50, output_tokens=20)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MUSE, "stream": True}, headers=h)
    assert r.status_code == 200
    call = meta.calls[0]
    assert call["headers"]["authorization"] == "Bearer meta-key-123" and "x-api-key" not in call["headers"]
    row = last_request(conn)
    assert (row["user_id"], row["provider"], row["input_tokens"], row["output_tokens"]) == (uid, "meta", 50, 20)
    assert anthropic.calls == []


@pytest.mark.parametrize("action", ["disable", "revoke"])
async def test_disabling_or_revoking_stops_both_keys(env, meta, action):
    gw, conn, uid, full, routes = env
    (revoke if action == "revoke" else lambda c, u: set_enabled(c, u, False))(conn, uid)
    async with asgi_client(create_app(gw)) as c:
        for h in (full, routes):
            assert (await c.post("/v1/messages", json=MUSE, headers=h)).status_code in (401, 403)
    assert meta.calls == []


async def test_share_limit_blocks_claude_but_muse_still_works_on_the_routes_key(env, anthropic, meta):
    gw, conn, uid, full, routes = env
    now = time.time()
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)",
                 (uid, "share_5h", "*", "20", "pct"))

    def snap(t, util):
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                     (t, "header", "5h", util, now + 3600))
    snap(now - 300, 10)
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status, input_tokens) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", (uid, now - 200, now - 200, "POST", "/v1/messages", "anthropic", "claude-sonnet-5", 200, 1000))
    snap(now - 100, 35)       # alice was the only one active: +25 points, over her 20
    async with asgi_client(create_app(gw)) as c:
        claude = await c.post("/v1/messages", json=MSG, headers=full)
        muse = await c.post("/v1/messages", json=MUSE, headers=routes)
    assert claude.status_code == 429
    assert muse.status_code == 200
