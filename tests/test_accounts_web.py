"""Dashboard Accounts tab (spec 2026-10-13): list, link, re-link and remove subscription accounts."""
import time

import pytest
from argon2 import PasswordHasher
from fastapi.responses import JSONResponse

from claude_proxy import db as dbm, login, plans
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
    cfg.credential.profile_url = "http://auth.fake/profile"
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
    token = token_answer("sub-2", "two@x.com")
    auth.default = lambda req: (JSONResponse({"organization": {"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_20x"}})
                                if req["path"] == "/profile" else token(req))
    async with admin_client(gw) as c:
        rows = (await c.get("/api/admin/accounts")).json()["accounts"]
        assert [(r["account"], r["email"], r["healthy"]) for r in rows] == [("default", "default@x.com", True)]
        r = await link(c, conn, "second")
        assert r.status_code == 200 and r.json()["email"] == "two@x.com"
        assert OAuthBackend(gw.cfg, conn, None, "second").current_token() == "acc-sub-2"
        # Served at once; its plan is read by the poll loop's next tick, which linking wakes.
        assert set(gw.backends) == set(gw.pollers) == {"default", "second"}
        assert gw.pollers["second"].next_plan_check == 0
        await gw.pollers["second"].check_plan(time.time())
        assert conn.execute("SELECT rate_limit_tier FROM account_plans WHERE account='second'").fetchone()[0] == "default_claude_max_20x"
        assert {r["account"] for r in (await c.get("/api/admin/accounts")).json()["accounts"]} == {"default", "second"}
        assert (await c.post("/api/admin/accounts/second/remove", json={})).status_code == 200
        assert set(gw.backends) == {"default"}
    assert [r["account"] for r in list_accounts(conn)] == ["default"]
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE target='oauth:second' ORDER BY id")]
    assert actions == ["login_start", "login", "account_remove"]
    assert conn.execute("SELECT actor_user_id FROM audit_log WHERE action='login'").fetchone()[0] is not None


async def test_relink_default_reaches_the_live_backend(env):
    gw, conn, auth, _ = env
    auth.default = token_answer("sub-default", "default@x.com")
    async with admin_client(gw) as c:
        assert gw.backends["default"].current_token() == "oauth-access"
        assert (await link(c, conn, "default")).status_code == 200
    assert gw.backends["default"].current_token() == "acc-sub-default"


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
        assert r.status_code == 400 and "last linked" in r.json()["error"]
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


async def test_accounts_list_has_plans(env):
    gw, conn, auth, _ = env
    seed_oauth(conn, account="b", sub="sub-b", email="b@x.com")
    seed_oauth(conn, account="c", sub="sub-c", email="c@x.com")
    for a, tier in (("b", "default_claude_max_20x"), ("c", "default_claude_mystery")):
        conn.execute("INSERT INTO account_plans(observed_at, organization_type, rate_limit_tier, account) VALUES(?,?,?,?)",
                     (time.time() - 60, "claude_max", tier, a))
    plans.set_override(conn, "b", "pro", None)
    _, bob_key = dbm.create_user(conn, "bob", account="b")
    async with admin_client(gw) as c:
        out = (await c.get("/api/admin/accounts")).json()
        rows = {r["account"]: r for r in out["accounts"]}
        b = rows["b"]
        assert (b["plan"], b["plan_detected"], b["plan_override"], b["sells"]) == ("pro", "max_20x", "pro", None)
        assert b["rate_limit_tier"] == "default_claude_max_20x" and b["factor"] == {"7d": 1.0, "5h": 1.0}
        assert b["assigned_users"] == 1 and b["running_passes"] == 0 and b["queued_passes"] == 0
        assert b["pending_change"] is None
        assert (rows["c"]["plan"], rows["c"]["plan_detected"], rows["c"]["sells"], rows["c"]["factor"]) == \
            (None, None, "plan_unknown", None)
        assert rows["default"]["sells"] == "plan_unknown" and rows["default"]["rate_limit_tier"] is None
        assert out["factors"] == plans.factors(conn) and out["plans"] == plans.PLAN_LABELS
        assert "default" not in out


async def test_plan_override_ack_factors_api(env):
    gw, conn, auth, alice_key = env
    conn.execute("INSERT INTO account_plans(observed_at, organization_type, rate_limit_tier, account) VALUES(?,?,?,?)",
                 (time.time() - 60, "claude_max", "default_claude_max_5x", "default"))
    plans.acknowledge(conn, "default", None)
    conn.execute("INSERT INTO account_plans(observed_at, organization_type, rate_limit_tier, account) VALUES(?,?,?,?)",
                 (time.time() - 30, "claude_max", "default_claude_max_20x", "default"))
    assert plans.sells(conn, "default") == "plan_changed"
    async with admin_client(gw) as c:
        assert (await c.put("/api/admin/accounts/default/plan", json={"plan": "pro"})).status_code == 200
        assert plans.override(conn, "default") == "pro"
        assert (await c.put("/api/admin/accounts/default/plan", json={"plan": None})).status_code == 200
        assert plans.override(conn, "default") is None and plans.plan(conn, "default") == "max_20x"
        assert (await c.put("/api/admin/accounts/default/plan", json={"plan": "team"})).status_code == 400
        assert (await c.put("/api/admin/accounts/default/plan", json={})).status_code == 400
        assert (await c.put("/api/admin/accounts/ghost/plan", json={"plan": "pro"})).status_code == 404
        assert (await c.post("/api/admin/accounts/default/plan/ack", json={})).status_code == 200
        assert plans.sells(conn, "default") is None
        r = await c.put("/api/admin/plan_factors", json={"plan": "max_5x", "f_7d": 6, "f_5h": 4.5})
        assert r.status_code == 200 and r.json()["factors"]["max_5x"] == {"7d": 6.0, "5h": 4.5}
        assert plans.factors(conn)["max_5x"] == {"7d": 6.0, "5h": 4.5}
        assert (await c.put("/api/admin/plan_factors", json={"plan": "team", "f_7d": 1, "f_5h": 1})).status_code == 400
        assert (await c.put("/api/admin/plan_factors", json={"plan": "pro", "f_7d": 0, "f_5h": 1})).status_code == 400
        assert (await c.put("/api/admin/plan_factors", json={"plan": "pro", "f_7d": "x", "f_5h": 1})).status_code == 400
        del c.headers["x-csrf-token"]
        assert (await c.put("/api/admin/accounts/default/plan", json={"plan": "pro"})).status_code == 403
        assert (await c.post("/api/admin/accounts/default/plan/ack", json={})).status_code == 403
        assert (await c.put("/api/admin/plan_factors", json={"plan": "pro", "f_7d": 2, "f_5h": 2})).status_code == 403
    async with asgi_client(create_dashboard_app(gw)) as c:
        alice = bearer(alice_key)
        assert (await c.put("/api/admin/accounts/default/plan", json={"plan": "pro"}, headers=alice)).status_code == 403
        assert (await c.post("/api/admin/accounts/default/plan/ack", json={}, headers=alice)).status_code == 403
        assert (await c.put("/api/admin/plan_factors", json={"plan": "pro", "f_7d": 2, "f_5h": 2}, headers=alice)).status_code == 403
        assert (await c.post("/api/admin/accounts/default/refresh", json={}, headers=alice)).status_code == 403
    assert plans.override(conn, "default") is None and plans.factors(conn)["pro"] == {"7d": 1.0, "5h": 1.0}
    assert [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action IN ('plan_override', 'plan_ack', "
                                       "'plan_factor_set') ORDER BY id")][-4:] == ["plan_override", "plan_override", "plan_ack",
                                                                                  "plan_factor_set"]


async def test_setting_the_default_plan_finishes_pending_sizes(env):
    # The pass sizes wait for the default account's plan (db.SIZES_PENDING_KEY); the admin's pick converts them.
    gw, conn, auth, alice_key = env
    conn.execute("UPDATE ticket_tier_shares SET size=NULL, size_5h=NULL")
    conn.execute("INSERT OR REPLACE INTO ticket_tier_shares(tier, share_pct, updated_at, size) VALUES('lite', 5, 1, NULL)")
    conn.execute("INSERT INTO settings(key, value) VALUES(?, '{}')", (dbm.SIZES_PENDING_KEY,))
    async with admin_client(gw) as c:
        assert (await c.get("/api/admin/accounts")).json()["sizes_pending"] is True
        assert (await c.put("/api/admin/accounts/default/plan", json={"plan": "max_20x"})).status_code == 200
        assert (await c.get("/api/admin/accounts")).json()["sizes_pending"] is False
    assert conn.execute("SELECT size FROM ticket_tier_shares WHERE tier='lite'").fetchone()[0] == 1.0
