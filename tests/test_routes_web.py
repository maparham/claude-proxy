"""Routes-only keys on the dashboard (spec 2.4)."""
import time

from claude_proxy.db import set_routes_key
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_web import admin_client, bearer, env  # noqa: F401  (fixture)


async def test_routes_key_reads_its_own_status_and_nothing_else(env):  # noqa: F811
    gw, conn, ids, keys = env
    rk = set_routes_key(conn, ids["alice"])
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/api/me/status", headers=bearer(rk))
        assert r.status_code == 200 and r.json()["user"]["name"] == "alice"
        r = await c.get("/api/me/status?format=text", headers={"x-api-key": rk})
        assert r.status_code == 200 and r.text.startswith("alice")
        r = await c.get("/api/overview", headers=bearer(rk))
        assert r.status_code == 403
        assert r.json()["error"] == "This key only works for third-party models; use your Claude Code key for the dashboard."


async def test_an_admins_routes_key_cannot_do_admin_actions(env):  # noqa: F811
    gw, conn, ids, keys = env
    rk = set_routes_key(conn, ids["admin"])
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.post("/api/admin/users", json={"name": "eve"}, headers=bearer(rk))).status_code == 403
        assert (await c.get("/api/users", headers=bearer(rk))).status_code == 403
    assert conn.execute("SELECT 1 FROM users WHERE name='eve'").fetchone() is None


async def test_routes_key_cannot_sign_in_and_is_not_a_failed_attempt(env):  # noqa: F811
    gw, conn, ids, keys = env
    rk = set_routes_key(conn, ids["alice"])
    async with asgi_client(create_dashboard_app(gw)) as c:
        for _ in range(7):           # more than the limiter's 5 failures
            r = await c.post("/api/login/key", json={"key": rk})
            assert r.status_code == 403
            assert r.json()["error"] == "This key only works for third-party models; sign in with your Claude Code key."
        assert (await c.post("/api/login/key", json={"key": keys["alice"]})).status_code == 200


async def test_admin_issues_and_removes_an_opencode_key(env):  # noqa: F811
    gw, conn, ids, keys = env

    async def bob(c):
        return next(u for u in (await c.get("/api/users")).json()["users"] if u["name"] == "bob")
    async with admin_client(gw) as c:
        assert (await bob(c))["routes_prefix"] is None
        key = (await c.post(f"/api/admin/users/{ids['bob']}/routes_key", json={})).json()["key"]
        assert key.startswith("sk-proxy-r-")
        assert (await bob(c))["routes_prefix"] == key[:14]
        r = await c.post(f"/api/admin/users/{ids['bob']}/routes_key_remove", json={})
        assert r.json() == {"ok": True, "removed": True}
        assert (await bob(c))["routes_prefix"] is None
    assert [r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE target='bob' ORDER BY id")] == \
        ["routes_key_issue", "routes_key_remove"]


async def test_key_scope_refusals_are_not_counted_as_limit_hits(env):  # noqa: F811
    gw, conn, ids, keys = env
    conn.execute("INSERT INTO requests(user_id, started_at, method, path, model, status, rejected_by) VALUES(?,?,?,?,?,?,?)",
                 (ids["bob"], time.time() - 60, "POST", "/v1/messages", "claude-sonnet-5", 403, "key_scope"))
    async with admin_client(gw) as c:
        d = (await c.get(f"/api/errors?range=7d&user_id={ids['bob']}")).json()
    assert [(r["k"], r["rejected_by"]) for r in d["recent"]] == [("gateway_key_scope", "key_scope")]
    assert {p["k"] for p in d["points"]} == {"gateway_key_scope"}
