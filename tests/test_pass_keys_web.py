"""Pass app keys on the dashboard (2026-11-18 spec, section 3a)."""
import time

from claude_proxy import api_credit, passkeys
from claude_proxy.db import add_machine_key
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_web import admin_client, bearer, env  # noqa: F401  (fixture)

OFF = "Passes are not enabled on this gateway."
REFUSAL = "Pass app keys work only in apps (/v1/messages); sign in to the dashboard instead."
MACHINE = "A computer's key cannot manage API or app keys. Sign in with your first key to manage them."


def on(gw):
    gw.cfg.tickets.enabled = True


async def test_user_creates_lists_and_revokes_keys(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    gw.cfg.listener.public_url = "https://gw.example"
    async with asgi_client(create_dashboard_app(gw)) as c:
        h = bearer(keys["alice"])
        r = await c.post("/api/me/passkeys", json={"label": "Desktop"}, headers=h)
        assert r.status_code == 200 and r.json()["key"].startswith("sk-proxy-p-")
        kid = r.json()["id"]
        me = (await c.get("/api/me/passkeys", headers=h)).json()
        assert me["enabled"] is True and [k["label"] for k in me["keys"]] == ["Desktop"] and me["base_url"] == "https://gw.example"
        assert "usage" in me and "key" not in me["keys"][0] and "key_hash" not in me["keys"][0]
        assert (await c.post(f"/api/me/passkeys/{kid}/revoke", headers=bearer(keys["bob"]))).status_code == 404
        assert (await c.post(f"/api/me/passkeys/{kid}/revoke", headers=h)).status_code == 200
        assert (await c.get("/api/me/passkeys", headers=h)).json()["keys"] == []
        assert (await c.post("/api/me/passkeys", json={"label": ""}, headers=h)).status_code == 400


async def test_a_computer_cannot_create_or_revoke_pass_keys(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    laptop = add_machine_key(conn, ids["alice"], "laptop")
    kid, _ = passkeys.create_key(conn, ids["alice"], "app")
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.post("/api/me/passkeys", json={"label": "x"}, headers=bearer(laptop))
        assert r.status_code == 403 and r.json()["error"] == MACHINE
        assert (await c.post(f"/api/me/passkeys/{kid}/revoke", headers=bearer(laptop))).status_code == 403
        assert (await c.get("/api/me/passkeys", headers=bearer(laptop))).status_code == 200
    assert [k["label"] for k in passkeys.keys(conn, ids["alice"])] == ["app"]


async def test_a_pass_key_cannot_use_the_dashboard(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    _, raw = passkeys.create_key(conn, ids["alice"], "app")
    async with asgi_client(create_dashboard_app(gw)) as c:
        for path in ("/api/me/passkeys", "/api/me/status", "/api/overview"):
            r = await c.get(path, headers={"x-api-key": raw})
            assert r.status_code == 403 and "Pass app keys" in r.json()["error"], path
            assert r.json()["error"] == REFUSAL
        r = await c.post("/api/login/key", json={"key": raw})
        assert r.status_code == 403 and r.json()["error"] == REFUSAL


async def test_sources_split_cli_api_and_apps(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    akid, _ = api_credit.create_key(conn, ids["alice"], "ci-bot")
    pkid, _ = passkeys.create_key(conn, ids["alice"], "Desktop")
    now = time.time()
    for model, a, p in (("claude-api-m", akid, None), ("claude-app-m", None, pkid)):
        conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status, api_key_id, pass_key_id) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", (ids["alice"], now - 60, now - 59, "POST", "/v1/messages", "anthropic", model, 200, a, p))
    async with admin_client(gw) as c:
        async def models(source, q=""):
            return [r["model"] for r in (await c.get(f"/api/requests?source={source}&q={q}")).json()["requests"]]
        assert "claude-app-m" not in await models("cli") and "claude-api-m" not in await models("cli")
        assert await models("api") == ["claude-api-m"]
        assert await models("apps") == ["claude-app-m"]
        assert len(await models("all")) == 5
        rows = (await c.get("/api/requests?source=apps")).json()["requests"]
        assert rows[0]["key"] == "Desktop"
        assert await models("apps", "Desk") == ["claude-app-m"] and await models("apps", "nobody") == []
        assert (await c.get("/api/overview?source=apps")).json()["totals"]["24h"]["requests"] == 1


async def test_admin_creates_and_revokes_for_another_user(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    async with admin_client(gw) as c:
        r = await c.post("/api/admin/passkeys", json={"user": "bob", "label": "Desktop"})
        assert r.status_code == 200 and r.json()["key"].startswith("sk-proxy-p-") and r.json()["name"] == "bob"
        kid = r.json()["id"]
        ks = (await c.get(f"/api/admin/users/{ids['bob']}/passkeys")).json()
        assert [k["label"] for k in ks["keys"]] == ["Desktop"] and "key" not in ks["keys"][0]
        assert (await c.post(f"/api/admin/passkeys/{kid}/revoke")).status_code == 200
        assert (await c.post(f"/api/admin/passkeys/{kid}/revoke")).status_code == 404
        assert (await c.post("/api/admin/passkeys", json={"user": "nobody", "label": "x"})).status_code == 404
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.post("/api/admin/passkeys", json={"user": "bob", "label": "x"}, headers=bearer(keys["alice"]))).status_code == 403


async def test_everything_is_404_while_passes_are_off(env):  # noqa: F811
    gw, conn, ids, keys = env
    gw.cfg.tickets.enabled = False
    async with admin_client(gw) as c:
        for r in (await c.get(f"/api/admin/users/{ids['bob']}/passkeys"),
                  await c.post("/api/admin/passkeys", json={"user": "bob", "label": "x"}),
                  await c.post("/api/admin/passkeys/1/revoke")):
            assert r.status_code == 404 and r.json()["error"] == OFF
    async with asgi_client(create_dashboard_app(gw)) as c:
        h = bearer(keys["alice"])
        for r in (await c.get("/api/me/passkeys", headers=h), await c.post("/api/me/passkeys", json={"label": "x"}, headers=h),
                  await c.post("/api/me/passkeys/1/revoke", headers=h)):
            assert r.status_code == 404 and r.json()["error"] == OFF
