"""A non-admin never learns about the Claude subscription behind the gateway: not through the dashboard API,
the proxy's response headers, or error messages. Their own limits are all they have."""
import time

import pytest
from fastapi.responses import JSONResponse

from claude_proxy.app import create_app
from claude_proxy.db import create_user
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client, make_gateway, message_json, seed_oauth

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
ACCOUNT_WORDS = ("utilization", "subscription", "quota", "account", "credential", "plan 5h", "share", "ratelimit",
                 "organization", "claude-proxy login", "resets_at", "stale")
ACCOUNT_HEADERS = {"anthropic-ratelimit-unified-5h-utilization": "0.25", "anthropic-ratelimit-unified-5h-reset": "1790000000",
                   "anthropic-ratelimit-unified-status": "allowed", "anthropic-organization-id": "org-123"}
GET_ROUTES = ["/api/session", "/api/overview", "/api/series?split=model", "/api/series?split=provider", "/api/models",
              "/api/heatmap", "/api/sessions", "/api/errors", "/api/requests", "/api/limits", "/api/me/status",
              "/api/me/status?format=text"]


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def env(cfg, db, anthropic):
    conn = db[1]
    admin_id, admin_key = create_user(conn, "admin", role="admin")
    uid, key = create_user(conn, "alice")
    seed_oauth(conn, access="oauth-secret-token")
    now = time.time()
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                 (now - 50, "header", "5h", 40, now + 3600))
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (uid, "share_5h", "*", "20", "pct"))
    for status, err, rej in [(200, None, None), (429, "upstream_quota", None), (503, "gateway_needs_login", None),
                             (429, "rate_limit_error", "share_5h"), (429, "upstream_throttle", None)]:
        conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status, input_tokens, "
                     "output_tokens, session_id, error_type, rejected_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (uid, now - 100, now - 99, "POST", "/v1/messages", "anthropic", "claude-sonnet-5", status, 1000, 100,
                      "s-1", err, rej))
    return make_gateway(cfg, conn, anthropic), conn, uid, key, admin_key


async def test_dashboard_api_tells_a_user_nothing_about_the_account(env):
    gw, conn, uid, key, admin_key = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        for route in GET_ROUTES:
            r = await c.get(route, headers=bearer(key))
            assert r.status_code == 200, route
            text = r.text.lower()
            assert not [w for w in ACCOUNT_WORDS if w in text], (route, r.text)
        for route in ("/api/quota/timeline", "/api/users", "/api/audit"):
            assert (await c.get(route, headers=bearer(key))).status_code == 403, route
        # The admin still sees all of it.
        ov = (await c.get("/api/overview", headers=bearer(admin_key))).json()
        assert ov["quota"][0]["utilization_pct"] == 40 and "credential" in ov
        assert (await c.get("/api/quota/timeline", headers=bearer(admin_key))).status_code == 200


async def test_share_limit_reads_as_the_users_own_allowance(env):
    gw, conn, uid, key, admin_key = env
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status, input_tokens, output_tokens) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)", (uid, time.time() - 60, time.time() - 59, "POST", "/v1/messages", "anthropic",
                                                 "claude-sonnet-5", 200, 10, 1))
    async with asgi_client(create_dashboard_app(gw)) as c:
        mine = (await c.get("/api/limits", headers=bearer(key))).json()
        admins = (await c.get("/api/limits", headers=bearer(admin_key))).json()
    [lim] = mine["limits"]
    assert lim["kind"] == "5h_limit" and lim["limit"] == 100 and lim["unit"] == "pct" and not lim["estimated"]
    assert "kinds" not in mine
    [adm] = [x for x in admins["limits"] if x["user"] == "alice"]
    assert adm["kind"] == "share_5h" and adm["limit"] == 20
    if adm["current"] is not None:
        assert lim["current"] == pytest.approx(100 * adm["current"] / 20)


async def test_proxy_strips_account_headers_for_users_but_still_records_them(env, anthropic):
    gw, conn, uid, key, admin_key = env
    anthropic.default = lambda req: JSONResponse({"data": []} if req["path"] == "/v1/models" else message_json(),
                                                 headers=ACCOUNT_HEADERS)
    async with asgi_client(create_app(gw)) as c:
        mine = await c.post("/v1/messages", json=MSG, headers=bearer(key))
        admins = await c.post("/v1/messages", json=MSG, headers=bearer(admin_key))
        models = await c.get("/v1/models", headers=bearer(key))
    for r in (mine, models):
        assert r.status_code == 200
        assert not [h for h in r.headers if h.startswith(("anthropic-ratelimit", "anthropic-organization"))]
    assert admins.headers["anthropic-ratelimit-unified-5h-utilization"] == "0.25"
    assert admins.headers["anthropic-organization-id"] == "org-123"
    assert conn.execute("SELECT COUNT(*) FROM quota_snapshots WHERE utilization_pct=25").fetchone()[0] >= 1


async def test_upstream_quota_429_is_reworded_for_users(env, anthropic):
    gw, conn, uid, key, admin_key = env
    anthropic.default = lambda req: JSONResponse(
        {"type": "error", "error": {"type": "rate_limit_error", "message": "Your Claude Max 5-hour limit is used up"}}, status_code=429,
        headers={"retry-after": "300", "anthropic-ratelimit-unified-status": "rejected", "anthropic-ratelimit-unified-5h-utilization": "1.0"})
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=bearer(key))
        a = await c.post("/v1/messages", json=MSG, headers=bearer(admin_key))
    assert r.status_code == 429 and r.headers["retry-after"] == "300"
    assert r.json()["error"] == {"type": "rate_limit_error", "message": "Usage limit reached. Try again in 5 min."}
    assert "anthropic-ratelimit-unified-status" not in r.headers
    row = conn.execute("SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    assert (row["status"], row["error_type"]) == (429, "upstream_quota")
    assert "Claude Max" in a.json()["error"]["message"]


@pytest.mark.parametrize("status", [401, 403])
async def test_upstream_auth_failure_is_a_plain_503_for_users(env, anthropic, status):
    gw, conn, uid, key, admin_key = env
    anthropic.default = lambda req: JSONResponse({"type": "error", "error": {"type": "authentication_error", "message": "OAuth token revoked"}},
                                                 status_code=status)
    gw.backend.on_unauthorized = lambda token: _false()
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=bearer(key))
    assert r.status_code == 503
    assert not [w for w in ACCOUNT_WORDS + ("oauth",) if w in r.text.lower()]


async def _false():
    return False


async def test_needs_login_is_a_plain_503_for_users(env):
    gw, conn, uid, key, admin_key = env
    conn.execute("UPDATE credentials SET state='needs_login'")
    gw.backend._cache = None
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=bearer(key))
        a = await c.post("/v1/messages", json=MSG, headers=bearer(admin_key))
    assert r.status_code == 503 and not [w for w in ACCOUNT_WORDS if w in r.text.lower()]
    assert "claude-proxy login" in a.json()["error"]["message"]


async def test_share_limit_rejection_speaks_of_the_users_own_limit(env):
    gw, conn, uid, key, admin_key = env
    conn.execute("UPDATE limits SET value='0' WHERE user_id=?", (uid,))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=bearer(key))
    assert r.status_code == 429
    msg = r.json()["error"]["message"]
    assert msg.startswith("Gateway 5-hour limit reached") and not [w for w in ACCOUNT_WORDS if w in msg.lower()]
