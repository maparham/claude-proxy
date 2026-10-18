"""Dashboard Accounts tab (spec 2026-10-13): list, link, re-link and remove subscription accounts."""
import pytest
from argon2 import PasswordHasher
from fastapi.responses import JSONResponse

from claude_proxy import db as dbm, login
from claude_proxy.credentials import OAuthBackend, list_accounts
from claude_proxy.web import create_dashboard_app
from tests.conftest import FakeUpstream, asgi_client, make_gateway, seed_oauth
from tests.test_web import PW, admin_client, bearer


@pytest.fixture
def env(cfg, db):
    conn = db[1]
    dbm.create_user(conn, "admin", role="admin", password_hash=PasswordHasher().hash(PW))
    _, alice_key = dbm.create_user(conn, "alice")
    seed_oauth(conn, sub="sub-default", email="default@x.com")
    auth = FakeUpstream("auth.fake")
    cfg.credential.token_url = "http://auth.fake/token"
    gw = make_gateway(cfg, conn, auth)
    return gw, conn, auth, alice_key


def token_answer(sub, email):
    return lambda req: JSONResponse({"access_token": f"acc-{sub}", "refresh_token": f"ref-{sub}", "expires_in": 3600,
                                     "account": {"uuid": sub, "email_address": email}})


async def link(c, conn, slug):
    r = await c.post(f"/api/admin/accounts/{slug}/login/start", json={})
    assert r.status_code == 200, r.text
    state = login._pending(conn, slug)["state"]
    assert state in r.json()["url"]
    return await c.post(f"/api/admin/accounts/{slug}/login/finish", json={"code": f"CODE#{state}"})


async def test_list_link_and_remove(env):
    gw, conn, auth, _ = env
    auth.default = token_answer("sub-2", "two@x.com")
    async with admin_client(gw) as c:
        rows = (await c.get("/api/admin/accounts")).json()["accounts"]
        assert [(r["account"], r["email"], r["healthy"]) for r in rows] == [("default", "default@x.com", True)]
        r = await link(c, conn, "second")
        assert r.status_code == 200 and r.json()["email"] == "two@x.com"
        assert OAuthBackend(gw.cfg, conn, None, "second").current_token() == "acc-sub-2"
        assert {r["account"] for r in (await c.get("/api/admin/accounts")).json()["accounts"]} == {"default", "second"}
        assert (await c.post("/api/admin/accounts/second/remove", json={})).status_code == 200
    assert [r["account"] for r in list_accounts(conn)] == ["default"]
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE target='oauth:second' ORDER BY id")]
    assert actions == ["login_start", "login", "account_remove"]
    assert conn.execute("SELECT actor_user_id FROM audit_log WHERE action='login'").fetchone()[0] is not None


async def test_relink_default_reaches_the_live_backend(env):
    gw, conn, auth, _ = env
    auth.default = token_answer("sub-default", "default@x.com")
    async with admin_client(gw) as c:
        assert gw.backend.current_token() == "oauth-access"
        assert (await link(c, conn, "default")).status_code == 200
    assert gw.backend.current_token() == "acc-sub-default"


async def test_duplicate_subscription_refused(env):
    gw, conn, auth, _ = env
    auth.default = token_answer("sub-default", "default@x.com")
    async with admin_client(gw) as c:
        r = await link(c, conn, "second")
        assert r.status_code == 400 and "already linked as account 'default'" in r.json()["error"]
    assert [r["account"] for r in list_accounts(conn)] == ["default"]


async def test_bad_code_slug_and_remove_rules(env):
    gw, conn, auth, _ = env
    async with admin_client(gw) as c:
        assert (await c.post("/api/admin/accounts/NOPE/login/start", json={})).status_code == 400
        assert (await c.post("/api/admin/accounts/second/login/finish", json={"code": "x#y"})).status_code == 400
        await c.post("/api/admin/accounts/second/login/start", json={})
        r = await c.post("/api/admin/accounts/second/login/finish", json={"code": ""})
        assert r.status_code == 400
        r = await c.post("/api/admin/accounts/default/remove", json={})
        assert r.status_code == 400 and "serves all traffic" in r.json()["error"]
        r = await c.post("/api/admin/accounts/ghost/remove", json={})
        assert r.status_code == 400 and "No subscription account" in r.json()["error"]


async def test_admin_only_and_csrf(env):
    gw, conn, auth, alice_key = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/admin/accounts", headers=bearer(alice_key))).status_code == 403
    async with admin_client(gw) as c:
        del c.headers["x-csrf-token"]
        assert (await c.post("/api/admin/accounts/second/login/start", json={})).status_code == 403
    assert login._pending(conn, "second") is None
