import time
from contextlib import asynccontextmanager

import pytest
from argon2 import PasswordHasher

from claude_proxy.db import create_user
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client, make_gateway, seed_oauth

PW = "correct horse battery"


@pytest.fixture
def env(cfg, db):
    conn = db[1]
    admin_id, admin_key = create_user(conn, "admin", role="admin", password_hash=PasswordHasher().hash(PW))
    alice, alice_key = create_user(conn, "alice")
    bob, bob_key = create_user(conn, "bob")
    seed_oauth(conn)
    now = time.time()
    for uid, model, i, o in [(alice, "claude-opus-5", 1000, 200), (alice, "muse-spark-1.3", 500, 100), (bob, "claude-sonnet-5", 300, 50)]:
        conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status, input_tokens, output_tokens, session_id) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uid, now - 100, now - 99, "POST", "/v1/messages",
                                                      "meta" if model.startswith("muse") else "anthropic", model, 200, i, o, f"s-{uid}"))
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)", (now - 200, "header", "5h", 10, now + 3600))
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)", (now - 50, "header", "5h", 14, now + 3600))
    gw = make_gateway(cfg, conn)
    return gw, conn, {"admin": admin_id, "alice": alice, "bob": bob}, {"admin": admin_key, "alice": alice_key, "bob": bob_key}


@asynccontextmanager
async def admin_client(gw):
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.post("/api/login", json={"username": "admin", "password": PW})
        assert r.status_code == 200, r.text
        c.headers["x-csrf-token"] = r.json()["csrf"]
        yield c


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


async def test_admin_login_sets_strict_httponly_cookie(env):
    gw, *_ = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.post("/api/login", json={"username": "admin", "password": PW})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


async def test_wrong_password_is_401(env):
    gw, *_ = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.post("/api/login", json={"username": "admin", "password": "nope"})).status_code == 401
        assert (await c.post("/api/login", json={"username": "alice", "password": PW})).status_code == 401


async def test_user_logs_in_with_virtual_key_and_sees_only_own_data(env):
    gw, conn, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.post("/api/login/key", json={"key": keys["alice"]})
        assert r.status_code == 200 and r.json()["user"]["role"] == "user"
        ov = (await c.get("/api/overview")).json()
        assert ov["scope"] == "self"
        assert ov["totals"]["30d"]["requests"] == 2
        series = (await c.get("/api/series?range=1d&granularity=hour&split=user")).json()
        assert {p["key"] for p in series["points"]} == {"alice"}
        assert (await c.get("/api/users")).status_code == 403
        assert (await c.get("/api/audit")).status_code == 403


async def test_admin_overview_is_account_wide(env):
    gw, conn, ids, keys = env
    async with admin_client(gw) as c:
        ov = (await c.get("/api/overview")).json()
    assert ov["scope"] == "account"
    assert ov["totals"]["24h"]["requests"] == 3
    q5 = next(b for b in ov["quota"] if b["bucket"] == "5h")
    assert q5["utilization_pct"] == 14
    # +4 points split by API-equivalent cost: alice's Opus request ($0.0100) vs bob's Sonnet one ($0.0011);
    # alice's Muse request is not on the subscription and does not count.
    assert q5["shares"]["alice"] == pytest.approx(4 * 0.01 / 0.0111)
    assert q5["shares"]["bob"] == pytest.approx(4 * 0.0011 / 0.0111)
    assert q5["unattributed"] == pytest.approx(10)
    assert ov["credential"]["healthy"] is True
    assert ov["totals"]["24h"]["cost_usd"] > 0


async def test_state_change_requires_csrf_for_cookie_sessions(env):
    gw, conn, ids, keys = env
    async with admin_client(gw) as c:
        token = c.headers.pop("x-csrf-token")
        r = await c.post("/api/admin/limits", json={"user": "alice", "kind": "requests_daily", "value": 5})
        assert r.status_code == 403
        c.headers["x-csrf-token"] = token
        r = await c.post("/api/admin/limits", json={"user": "alice", "kind": "requests_daily", "value": 5})
        assert r.status_code == 200, r.text
    row = conn.execute("SELECT * FROM limits WHERE user_id=?", (ids["alice"],)).fetchone()
    assert (row["kind"], row["value"], row["unit"], row["scope"]) == ("requests_daily", "5", "count", "*")
    assert conn.execute("SELECT action FROM audit_log ORDER BY id DESC").fetchone()[0] == "limit_set"


async def test_admin_bearer_key_needs_no_csrf(env):
    gw, conn, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.post("/api/admin/limits", headers=bearer(keys["admin"]),
                         json={"user": "bob", "kind": "tokens_daily", "value": 1000, "unit": "raw", "scope": "claude-opus-*"})
    assert r.status_code == 200, r.text


async def test_non_admin_cannot_change_limits(env):
    gw, conn, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.post("/api/admin/limits", headers=bearer(keys["alice"]), json={"user": "alice", "kind": "requests_daily", "value": 999})
    assert r.status_code == 403


async def test_invalid_limit_is_400(env):
    gw, *_ = env
    async with admin_client(gw) as c:
        r = await c.post("/api/admin/limits", json={"user": "alice", "kind": "share_5h", "value": 200})
    assert r.status_code == 400 and "100" in r.json()["error"]


async def test_admin_user_lifecycle(env):
    gw, conn, ids, keys = env
    async with admin_client(gw) as c:
        r = await c.post("/api/admin/users", json={"name": "carol"})
        assert r.status_code == 200
        new_key = r.json()["key"]
        uid = r.json()["id"]
        assert new_key.startswith("sk-proxy-")
        rot = await c.post(f"/api/admin/users/{uid}/rotate")
        assert rot.json()["key"] != new_key
        assert (await c.post(f"/api/admin/users/{uid}/disable")).status_code == 200
        assert conn.execute("SELECT enabled FROM users WHERE id=?", (uid,)).fetchone()[0] == 0
        assert (await c.post(f"/api/admin/users/{uid}/revoke")).status_code == 200
        users = (await c.get("/api/users")).json()["users"]
    assert next(u for u in users if u["name"] == "carol")["revoked"] is True


async def test_admin_cannot_revoke_self(env):
    gw, conn, ids, keys = env
    async with admin_client(gw) as c:
        assert (await c.post(f"/api/admin/users/{ids['admin']}/revoke")).status_code == 400


async def test_me_status_for_statusline(env):
    gw, conn, ids, keys = env
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (ids["alice"], "requests_daily", "*", "10", "count"))
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/api/me/status", headers=bearer(keys["alice"]))
    d = r.json()
    assert d["user"]["name"] == "alice"
    assert d["limits"][0]["kind"] == "requests_daily" and d["limits"][0]["current"] == 2
    assert d["account"]["5h"]["utilization_pct"] == 14
    assert "line" in d and "acct 5h 14%" in d["line"]


@pytest.mark.parametrize("path", ["/api/series?range=7d&granularity=day&split=model", "/api/models", "/api/heatmap",
                                  "/api/sessions", "/api/errors?range=7d", "/api/quota/timeline?bucket=5h&range=1d", "/api/audit", "/api/users", "/api/limits"])
async def test_admin_read_endpoints_respond(env, path):
    gw, *_ = env
    async with admin_client(gw) as c:
        r = await c.get(path)
    assert r.status_code == 200, r.text


async def test_dashboard_page_served_with_security_headers(env):
    gw, *_ = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/dashboard")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert r.headers["x-frame-options"] == "DENY"


async def test_me_status_plain_text_for_statusline_script(env):
    gw, conn, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/api/me/status?format=text", headers=bearer(keys["bob"]))
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("bob · acct 5h 14%")


async def test_delete_only_revoked_users_and_their_history(env):
    gw, conn, ids, keys = env
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (ids["bob"], "requests_daily", "*", "5", "count"))
    async with admin_client(gw) as c:
        assert (await c.post(f"/api/admin/users/{ids['bob']}/delete")).status_code == 400   # not revoked yet
        assert (await c.post(f"/api/admin/users/{ids['bob']}/revoke")).status_code == 200
        r = await c.post(f"/api/admin/users/{ids['bob']}/delete")
        assert r.status_code == 200 and r.json()["deleted_requests"] == 1
        names = [u["name"] for u in (await c.get("/api/users")).json()["users"]]
    assert "bob" not in names
    for table in ("users", "requests", "limits", "sessions"):
        col = "id" if table == "users" else "user_id"
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (ids["bob"],)).fetchone()[0] == 0
    assert conn.execute("SELECT action, target FROM audit_log ORDER BY id DESC").fetchone()[:] == ("delete_user", "bob")


async def test_session_reports_settings_the_dashboard_quotes(env, cfg):
    gw, *_ = env
    cfg.pricing.reference_model = "claude-opus-5-5"
    cfg.quota.stale_after_s = 900
    async with admin_client(gw) as c:
        s = (await c.get("/api/session")).json()
    assert s["settings"] == {"reference_model": "claude-opus-5-5", "stale_after_s": 900}
