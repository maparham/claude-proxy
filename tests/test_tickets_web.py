import time

import pytest
from argon2 import PasswordHasher

from claude_proxy import db as dbm, tickets
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client, make_gateway
from tests.test_web import PW, admin_client, bearer
from tests.tickets_helpers import DAY, seeded, user


@pytest.fixture
def env(db, cfg):
    conn, tc, ids = seeded(db)
    conn.execute("UPDATE fx_rates SET set_at=?", (int(time.time()) - 3600,))   # web endpoints use the wall clock; seeded() stamps the rate at the fixture's future NOW
    cfg.tickets = tc.tickets
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (PasswordHasher().hash(PW), ids["admin"]))
    alice_key = dbm.rotate_key(conn, ids["alice"])                 # a key we know, for the user's own endpoints
    gw = make_gateway(cfg, conn)
    return gw, conn, cfg, ids, {"alice": alice_key}


def last_audit(conn):
    return tuple(conn.execute("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1").fetchone())


async def test_rates_panel_and_setting_a_rate(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        r = (await c.get("/api/admin/rates")).json()["rates"]
        assert r == [{"currency": "EUR", "round_to": 0.5, "rate": 0.92, "set_at": r[0]["set_at"], "set_by": "admin", "stale": False}]
        assert (await c.post("/api/admin/rates", json={"currency": "eur", "rate": 0.95})).status_code == 200
        assert (await c.get("/api/admin/rates")).json()["rates"][0]["rate"] == 0.95
        assert (await c.post("/api/admin/rates", json={"currency": "GBP", "rate": 0.8})).status_code == 400
        assert (await c.post("/api/admin/rates", json={"currency": "EUR", "rate": -1})).status_code == 400
    assert last_audit(conn) == ("rate_set", "EUR")
    assert conn.execute("SELECT COUNT(*) FROM fx_rates WHERE currency='GBP'").fetchone()[0] == 0


async def test_stale_rate_is_flagged(env):
    gw, conn, cfg, ids, keys = env
    conn.execute("UPDATE fx_rates SET set_at=?", (int(time.time()) - 40 * 3600,))
    async with admin_client(gw) as c:
        assert (await c.get("/api/admin/rates")).json()["rates"][0]["stale"] is True


async def test_prices_grid_and_discounts(env):
    gw, conn, cfg, ids, keys = env
    now = int(time.time())
    async with admin_client(gw) as c:
        p = (await c.get("/api/admin/prices")).json()
        assert p["tiers"]["lite"] == {"label": "Lite", "share_pct": 5, "compare": "Claude Pro"} and p["lengths"] == {"day": 1, "week": 7, "month": 30}
        assert {(x["tier"], x["length"]): x["usd"] for x in p["prices"]}[("standard", "month")] == 100
        assert (await c.post("/api/admin/prices", json={"tier": "lite", "length": "week", "usd": 9})).status_code == 200
        assert last_audit(conn) == ("price_set", "lite:week")
        assert (await c.post("/api/admin/prices", json={"tier": "lite", "length": "year", "usd": 9})).status_code == 400
        r = await c.post("/api/admin/discounts", json={"tier": "lite", "length": "week", "usd": 6, "starts_at": now, "ends_at": now + DAY})
        assert r.status_code == 200 and r.json()["discount"]["usd"] == 6
        did = r.json()["discount"]["id"]
        assert last_audit(conn) == ("discount_create", "lite:week")
        r = await c.post("/api/admin/discounts", json={"tier": "lite", "length": "week", "usd": 5, "starts_at": now + 10, "ends_at": now + 20})
        assert r.status_code == 400 and "already covers" in r.json()["error"]
        assert (await c.post("/api/admin/discounts", json={"tier": "lite", "length": "week", "usd": 9, "starts_at": now, "ends_at": "soon"})).status_code == 400
        assert (await c.post(f"/api/admin/discounts/{did}/cancel")).status_code == 200
        assert last_audit(conn) == ("discount_cancel", "lite:week")
        assert (await c.post(f"/api/admin/discounts/{did}/cancel")).status_code == 400
        assert [d["id"] for d in (await c.get("/api/admin/prices")).json()["discounts"]] == [did]


async def test_ticket_endpoints_are_admin_only_and_off_without_config(env):
    gw, conn, cfg, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/admin/rates", headers=bearer(keys["alice"]))).status_code == 403
        assert (await c.post("/api/admin/prices", headers=bearer(keys["alice"]), json={})).status_code == 403
    cfg.tickets.enabled = False
    async with admin_client(gw) as c:
        assert (await c.get("/api/admin/rates")).status_code == 404
        assert (await c.get("/api/session")).json()["tickets"] == {"enabled": False}


async def test_session_carries_the_ticket_settings_for_admins_only(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        t = (await c.get("/api/session")).json()["tickets"]
    assert t["enabled"] and t["max_sold_pct"] == 80 and t["currencies"] == ["USD", "EUR"] and t["lengths"] == {"day": 1, "week": 7, "month": 30}
    assert t["tiers"]["standard"] == {"label": "Standard", "share_pct": 25, "compare": "Claude Max 5x"} and t["how_to_buy"].startswith("Send")
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/session", headers=bearer(keys["alice"]))).json()["tickets"] == {"enabled": True}
