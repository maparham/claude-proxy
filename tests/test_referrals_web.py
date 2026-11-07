"""Referrals (design 2026-11-07), task 5: the admin API and the Referrals dashboard tab."""
import time

import pytest
from argon2 import PasswordHasher

from claude_proxy import db as dbm, referrals, tickets
from claude_proxy.config import ZarinpalConfig
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client, make_gateway
from tests.test_referrals import add_unit, add_user, friend_of, grant_paid, reward_of, signed_in, unit_state
from tests.test_web import PW, admin_client, bearer
from tests.tickets_helpers import NOW, live, seeded


@pytest.fixture
def env(db, cfg):
    """Referrals on, bob signed up through alice's link: the same shape as test_referrals.env_on, plus a
    dashboard app (admin password set, alice's key rotated so the 403 test can sign in as her)."""
    conn, tc, ids = seeded(db)
    conn.execute("UPDATE fx_rates SET set_at=?", (int(time.time()) - 3600,))
    cfg.tickets = tc.tickets
    cfg.zarinpal = ZarinpalConfig(vat_percent=10)
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (PasswordHasher().hash(PW), ids["admin"]))
    tickets.set_share(conn, cfg, "lite", 20, ids["admin"])
    tickets.set_share(conn, cfg, "standard", 40, ids["admin"])
    live(conn, NOW)
    referrals.set_settings(conn, ids["admin"], {"enabled": True})
    referrals.code_for(conn, ids["alice"])   # alice's link exists before bob ever uses it, as it would for real
    ids["bob"] = add_user(conn, "bob@x.com")
    conn.execute("UPDATE users SET referred_by=? WHERE id=?", (ids["alice"], ids["bob"]))
    alice_key = dbm.rotate_key(conn, ids["alice"])
    gw = make_gateway(cfg, conn)
    return gw, conn, cfg, ids, alice_key


def client(gw):
    return asgi_client(create_dashboard_app(gw))


async def test_admin_only(env):
    gw, conn, cfg, ids, alice_key = env
    async with client(gw) as c:
        r = await c.get("/api/admin/referrals", headers=bearer(alice_key))
        assert r.status_code == 403


async def test_settings_put_roundtrip(env):
    gw, conn, cfg, ids, alice_key = env
    async with admin_client(gw) as c:
        r = await c.put("/api/admin/referrals/settings", json={"unit_pct": 15, "max_held": 3})
        assert r.status_code == 200
        assert r.json()["settings"]["unit_pct"] == 15 and r.json()["settings"]["max_held"] == 3
        got = (await c.get("/api/admin/referrals")).json()["settings"]
        assert got["unit_pct"] == 15 and got["max_held"] == 3
        bad = await c.put("/api/admin/referrals/settings", json={"approval": "maybe"})
        assert bad.status_code == 400


async def test_approve_refuse_endpoints(env):
    gw, conn, cfg, ids, alice_key = env
    referrals.set_settings(conn, ids["admin"], {"approval": "manual"})
    o1 = signed_in(conn, cfg, ids["bob"])
    grant_paid(conn, cfg, ids, ids["bob"], o1)
    r1 = reward_of(conn, o1["id"])
    carol = friend_of(conn, ids["alice"], "carol@x.com")
    o2 = signed_in(conn, cfg, carol)
    grant_paid(conn, cfg, ids, carol, o2)
    r2 = reward_of(conn, o2["id"])
    async with admin_client(gw) as c:
        ra = await c.post(f"/api/admin/referrals/rewards/{r1['id']}/approve")
        assert ra.status_code == 200 and ra.json()["status"] == "earned"
        assert reward_of(conn, o1["id"])["status"] == "earned"
        rr = await c.post(f"/api/admin/referrals/rewards/{r2['id']}/refuse")
        assert rr.status_code == 200
        assert reward_of(conn, o2["id"])["status"] == "refused"
        again = await c.post(f"/api/admin/referrals/rewards/{r1['id']}/approve")
        assert again.status_code == 409
        missing = await c.post("/api/admin/referrals/rewards/99999/approve")
        assert missing.status_code == 404


async def test_cancel_saved_unit(env):
    gw, conn, cfg, ids, alice_key = env
    u = add_unit(conn, ids["alice"])
    async with admin_client(gw) as c:
        r = await c.post(f"/api/admin/referrals/units/{u}/cancel")
        assert r.status_code == 200
        assert unit_state(conn, u) == "cancelled"


async def test_cancel_held_unit_409(env):
    gw, conn, cfg, ids, alice_key = env
    u = add_unit(conn, ids["alice"])
    signed_in(conn, cfg, ids["alice"])   # holds the unit on the open order
    assert unit_state(conn, u) == "held"
    async with admin_client(gw) as c:
        r = await c.post(f"/api/admin/referrals/units/{u}/cancel")
        assert r.status_code == 409
        assert unit_state(conn, u) == "held"


async def test_view_counts(env):
    gw, conn, cfg, ids, alice_key = env
    referrals.set_settings(conn, ids["admin"], {"approval": "manual"})
    o1 = signed_in(conn, cfg, ids["bob"])
    grant_paid(conn, cfg, ids, ids["bob"], o1)   # waiting for the admin
    add_unit(conn, ids["alice"])                 # a saved unit, for the Referrers section
    async with admin_client(gw) as c:
        v = (await c.get("/api/admin/referrals")).json()
        assert v["waiting"][0]["reward_id"] == reward_of(conn, o1["id"])["id"]
        assert v["waiting"][0]["referrer_id"] == ids["alice"] and v["waiting"][0]["friend_id"] == ids["bob"]
        assert v["waiting"][0]["tier"] == o1["tier"] and v["waiting"][0]["quoted_usd"] == o1["quoted_usd"]
        alice_row = next(r for r in v["referrers"] if r["id"] == ids["alice"])
        assert alice_row["signed_up"] == 1   # bob
        assert alice_row["bought"] == 1       # the one reward above, still waiting
        assert len(alice_row["saved"]) == 1
        assert alice_row["held"] == 0 and alice_row["used"] == 0 and alice_row["expired"] == 0
        assert any(h["action"] == "referral_waiting" for h in v["history"])


# ---------- Task 6: what the user sees (my_status, the quote endpoint) ----------

async def test_me_off_hides_link(db, cfg):
    conn, tc, ids = seeded(db)
    cfg.tickets = tc.tickets
    gw = make_gateway(cfg, conn)
    key = dbm.rotate_key(conn, ids["alice"])
    async with client(gw) as c:
        r = await c.get("/api/referrals/me", headers=bearer(key))
        assert r.status_code == 200 and r.json() == {"enabled": False, "units": [], "waiting": 0}


async def test_me_shows_link_and_units(env):
    gw, conn, cfg, ids, alice_key = env
    cfg.listener.home_url = "https://rahkar.pro"
    referrals.set_settings(conn, ids["admin"], {"approval": "manual"})
    code = referrals.code_for(conn, ids["alice"])
    add_unit(conn, ids["alice"])
    o = signed_in(conn, cfg, ids["bob"])
    grant_paid(conn, cfg, ids, ids["bob"], o)   # a waiting reward for alice (manual approval)
    async with client(gw) as c:
        m = (await c.get("/api/referrals/me", headers=bearer(alice_key))).json()
    assert m["enabled"] is True and m["link"] == f"https://rahkar.pro/r/{code}"
    assert m["friend_pct"] == 10
    assert len(m["units"]) == 1 and m["units"][0]["pct"] == 10 and m["units"][0]["state"] == "saved"
    assert m["waiting"] == 1


async def test_quote_endpoint_friend_and_unit(env):
    gw, conn, cfg, ids, alice_key = env
    bob_key = dbm.rotate_key(conn, ids["bob"])
    async with client(gw) as c:
        rb = await c.get("/api/referrals/quote?tier=lite&length=week&currency=EUR", headers=bearer(bob_key))
        assert rb.status_code == 200
        qb = rb.json()
        assert (qb["kind"], qb["pct"], qb["pct_applied"], qb["list_usd"], qb["usd"], qb["off_usd"]) == \
            ("friend", 10, 10, 8.0, 7.2, 0.8)
        assert "unit_id" not in qb   # internal: the buyer has no use for it (final review #12)
        assert qb["currency"] == "EUR" and qb["list_amount"] > qb["amount"] > 0

        ra = await c.get("/api/referrals/quote?tier=lite&length=week", headers=bearer(alice_key))
        assert ra.status_code == 200 and ra.json() is None   # no referrer, no unit: nothing to quote

        add_unit(conn, ids["alice"])
        ru = await c.get("/api/referrals/quote?tier=lite&length=week", headers=bearer(alice_key))
        qu = ru.json()
        assert qu["kind"] == "unit" and "unit_id" not in qu


# ---------- final review fix wave ----------

async def test_quote_endpoint_with_no_rate_falls_back_to_usd(env):
    """A currency of this gateway with no rate row yet: priced in USD, never a 500 (final review #12)."""
    gw, conn, cfg, ids, alice_key = env
    bob_key = dbm.rotate_key(conn, ids["bob"])
    conn.execute("DELETE FROM fx_rates")
    async with client(gw) as c:
        r = await c.get("/api/referrals/quote?tier=lite&length=week&currency=EUR", headers=bearer(bob_key))
    assert r.status_code == 200 and r.json()["currency"] == "USD" and r.json()["amount"] < r.json()["list_amount"]


async def test_quotes_batch_endpoint(env):
    """Every priced tier and length at once, for the price list: bob's friend discount on each, nothing for a user
    with no discount, and the same numbers as the single quote endpoint."""
    gw, conn, cfg, ids, alice_key = env
    bob_key = dbm.rotate_key(conn, ids["bob"])
    async with client(gw) as c:
        rb = (await c.get("/api/referrals/quotes?currency=EUR", headers=bearer(bob_key))).json()
        one = (await c.get("/api/referrals/quote?tier=lite&length=week&currency=EUR", headers=bearer(bob_key))).json()
        ra = (await c.get("/api/referrals/quotes?currency=EUR", headers=bearer(alice_key))).json()
    priced = {(t, n) for t in cfg.tickets.tiers for n in ("day", "week", "month")
              if conn.execute("SELECT 1 FROM ticket_prices WHERE tier=? AND length=?", (t, n)).fetchone()}
    assert {(q["tier"], q["length"]) for q in rb["quotes"]} == priced
    assert all(q["kind"] == "friend" and q["currency"] == "EUR" and "unit_id" not in q for q in rb["quotes"])
    lite_week = next(q for q in rb["quotes"] if (q["tier"], q["length"]) == ("lite", "week"))
    assert {k: v for k, v in lite_week.items() if k not in ("tier", "length")} == one
    assert ra == {"quotes": []}
