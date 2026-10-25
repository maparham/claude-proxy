"""API keys on the dashboard (2026-10-21 spec, section 5)."""
from claude_proxy import api_credit
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_web import admin_client, bearer, env  # noqa: F401  (fixture)

REFUSAL = "API keys work only with Claude Code (/v1/messages); sign in to the dashboard instead."


def on(gw):
    gw.cfg.api.enabled = True
    api_credit.seed_rates(gw.conn, gw.cfg)


async def test_everything_is_404_while_the_api_is_off(env):  # noqa: F811
    gw, conn, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/me/api", headers=bearer(keys["alice"]))).status_code == 404
        assert (await c.get("/api/session", headers=bearer(keys["alice"]))).json()["api"] == {"enabled": False}


async def test_user_creates_lists_and_revokes_keys_and_sees_balance(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    gw.cfg.listener.public_url = "https://gw.example"
    api_credit.grant(conn, ids["alice"], 7.5, "paid", None)
    async with asgi_client(create_dashboard_app(gw)) as c:
        h = bearer(keys["alice"])
        r = await c.post("/api/me/api/keys", json={"label": "laptop"}, headers=h)
        assert r.status_code == 200 and r.json()["key"].startswith("sk-proxy-a-")
        kid = r.json()["id"]
        me = (await c.get("/api/me/api", headers=h)).json()
        assert me["balance"] == 7.5 and [k["label"] for k in me["keys"]] == ["laptop"] and me["base_url"] == "https://gw.example"
        assert me["grants"][0]["note"] == "paid" and me["rates"] and me["limits"]["rpm"] == gw.cfg.api.rpm
        assert "key" not in me["keys"][0] and "key_hash" not in me["keys"][0]
        assert (await c.post(f"/api/me/api/keys/{kid}/revoke", headers=bearer(keys["bob"]))).status_code == 404
        assert (await c.post(f"/api/me/api/keys/{kid}/revoke", headers=h)).status_code == 200
        assert (await c.get("/api/me/api", headers=h)).json()["keys"] == []


async def test_an_api_key_cannot_use_the_dashboard(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    _, raw = api_credit.create_key(conn, ids["alice"], "k")
    async with asgi_client(create_dashboard_app(gw)) as c:
        for path in ("/api/me/api", "/api/overview", "/api/me/status"):
            r = await c.get(path, headers={"x-api-key": raw})
            assert r.status_code == 403 and r.json()["error"] == REFUSAL
        r = await c.post("/api/login/key", json={"key": raw})
        assert r.status_code == 403


async def test_admin_edits_rates_grants_credit_and_sets_limits(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    async with admin_client(gw) as c:
        r = await c.post("/api/admin/api/rates", json={"pattern": "claude-test-*", "input": 1, "output": 2})
        assert r.status_code == 200 and r.json()["rate"]["cache_read"] == 0.1
        assert (await c.post("/api/admin/api/rates", json={"pattern": "x", "input": -1, "output": 2})).status_code == 400
        assert (await c.post("/api/admin/api/rates/delete", json={"pattern": "claude-test-*"})).status_code == 200
        assert (await c.post("/api/admin/api/rates/delete", json={"pattern": "claude-test-*"})).status_code == 404
        r = await c.post("/api/admin/api/credit", json={"user": "alice", "usd": 20, "note": "card"})
        assert r.json() == {"ok": True, "balance": 20}
        assert (await c.post("/api/admin/api/credit", json={"user": "alice", "usd": 0, "note": "x"})).status_code == 400
        assert (await c.post("/api/admin/api/credit", json={"user": "nobody", "usd": 1, "note": "x"})).status_code == 404
        r = await c.post("/api/admin/api/limits", json={"user": "alice", "max_inflight": 9, "rpm": None})
        assert r.json()["limits"] == {"max_inflight": 9, "rpm": gw.cfg.api.rpm, "custom": True, "own_max_inflight": 9, "own_rpm": None}
        [a] = (await c.get("/api/admin/api")).json()["accounts"]
        assert (a["name"], a["balance"], a["max_inflight"]) == ("alice", 20, 9)
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.post("/api/admin/api/credit", json={"user": "alice", "usd": 5, "note": "x"},
                             headers=bearer(keys["alice"]))).status_code == 403


async def test_writes_need_the_csrf_token(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    async with admin_client(gw) as c:
        del c.headers["x-csrf-token"]
        assert (await c.post("/api/me/api/keys", json={"label": "x"})).status_code == 403


async def test_a_computers_key_cannot_create_or_revoke_api_keys(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    from claude_proxy.db import add_machine_key
    laptop = add_machine_key(conn, ids["alice"], "laptop")
    kid, _ = api_credit.create_key(conn, ids["alice"], "ci")
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.post("/api/me/api/keys", json={"label": "x"}, headers=bearer(laptop))
        assert r.status_code == 403 and r.json()["error"] == MACHINE
        r = await c.post(f"/api/me/api/keys/{kid}/revoke", headers=bearer(laptop))
        assert r.status_code == 403
        assert (await c.get("/api/me/api", headers=bearer(laptop))).status_code == 200   # reading is fine
    assert [k["label"] for k in api_credit.keys(conn, ids["alice"])] == ["ci"]


MACHINE = "A computer's key cannot manage API keys. Sign in with your first key to manage them."


async def test_usage_takes_the_viewers_time_zone(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    kid, _ = api_credit.create_key(conn, ids["alice"], "k")
    api_credit.charge(conn, ids["alice"], kid, 1, "claude-sonnet-5", {"input_tokens": 1000, "output_tokens": 0}, now=1792794600.0)
    async with asgi_client(create_dashboard_app(gw)) as c:
        for q in ("tz=Europe/Berlin&tz_offset_s=3600", "tz=Not/AZone&tz_offset_s=3600", "tz=../../etc/passwd", "tz=" + "x" * 500):
            r = await c.get(f"/api/me/api?{q}", headers=bearer(keys["alice"]))
            assert r.status_code == 200 and len(r.json()["usage"]) == 1   # a bad zone falls back to the offset


async def test_admin_creates_lists_and_revokes_a_users_api_keys(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    from claude_proxy.db import find_user_by_key
    async with admin_client(gw) as c:
        r = await c.post("/api/admin/api/keys", json={"user": "alice", "label": "for her laptop"})
        assert r.status_code == 200 and r.json()["key"].startswith("sk-proxy-a-")
        kid, raw = r.json()["id"], r.json()["key"]
        assert find_user_by_key(conn, raw)["id"] == ids["alice"]
        [k] = (await c.get("/api/admin/api")).json()["keys"]
        assert (k["id"], k["name"], k["label"]) == (kid, "alice", "for her laptop") and "key_hash" not in k
        assert (await c.post("/api/admin/api/keys", json={"user": "nobody", "label": "x"})).status_code == 404
        assert (await c.post("/api/admin/api/keys", json={"user": "alice", "label": " "})).status_code == 400
        assert (await c.post(f"/api/admin/api/keys/{kid}/revoke")).status_code == 200
        assert (await c.post(f"/api/admin/api/keys/{kid}/revoke")).status_code == 404
        assert find_user_by_key(conn, raw) is None and (await c.get("/api/admin/api")).json()["keys"] == []
    audit = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert "api_key_add" in audit and "api_key_revoke" in audit


async def test_only_an_admin_manages_other_peoples_api_keys(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    kid, _ = api_credit.create_key(conn, ids["bob"], "bob's")
    async with asgi_client(create_dashboard_app(gw)) as c:
        h = bearer(keys["alice"])
        assert (await c.post("/api/admin/api/keys", json={"user": "bob", "label": "x"}, headers=h)).status_code == 403
        assert (await c.post(f"/api/admin/api/keys/{kid}/revoke", headers=h)).status_code == 403
    async with admin_client(gw) as c:
        del c.headers["x-csrf-token"]
        assert (await c.post("/api/admin/api/keys", json={"user": "bob", "label": "x"})).status_code == 403
    assert len(api_credit.keys(conn, ids["bob"])) == 1
