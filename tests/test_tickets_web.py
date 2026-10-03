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


async def test_preview_grant_list_and_users_state(env):
    gw, conn, cfg, ids, keys = env
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (ids["alice"], "cost_total", "*", "5", "usd"))
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (ids["alice"], "tokens_daily", "*", "9", "weighted"))
    async with admin_client(gw) as c:
        p = (await c.post("/api/admin/tickets/preview", json={"user": "alice", "tier": "lite", "length": "week", "currency": "EUR"})).json()
        assert (p["usd"], p["amount"], p["available"], p["queued"], p["credit"], p["first_ticket"]) == (8, 7.5, True, False, True, True)
        assert [r["kind"] for r in p["limit_rows"]] == ["tokens_daily"]
        r = await c.post("/api/admin/tickets", json={"user": "alice", "tier": "lite", "length": "week", "currency": "EUR", "note": "paid 7.50",
                                                      "remove_limits": [{"kind": "tokens_daily", "scope": "*"}]})
        assert r.status_code == 200, r.text
        t = r.json()["ticket"]
        assert (t["user_name"], t["amount"], t["note"]) == ("alice", 7.5, "paid 7.50")
        assert conn.execute("SELECT COUNT(*) FROM limits WHERE user_id=?", (ids["alice"],)).fetchone()[0] == 0
        lst = (await c.get(f"/api/admin/tickets?user_id={ids['alice']}")).json()["tickets"]
        assert lst[0]["id"] == t["id"] and lst[0]["state"] == "active" and lst[0]["granted_by_name"] == "admin" and lst[0]["bonuses"] == []
        users = {u["name"]: u for u in (await c.get("/api/users")).json()["users"]}
        assert users["alice"]["ticket"]["gated"] and users["alice"]["ticket"]["live"] and users["alice"]["ticket"]["current"]["id"] == t["id"]
        assert users["admin"]["ticket"] == {"gated": False, "live": False, "current": None, "queued": None}
        # A second grant queues after the first; the preview says so.
        p = (await c.post("/api/admin/tickets/preview", json={"user": "alice", "tier": "lite", "length": "day", "currency": "USD"})).json()
        assert p["queued"] is True and p["starts_at"] == t["ends_at"]
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions[-3:] == ["credit_removed", "limit_clear", "ticket_grant"]


async def test_grant_refusals_map_to_400_and_409(env):
    gw, conn, cfg, ids, keys = env
    cfg.tickets.max_sold_pct = 5
    async with admin_client(gw) as c:
        assert (await c.post("/api/admin/tickets", json={"user": "alice", "tier": "lite", "length": "week", "currency": "EUR"})).status_code == 200
        bob = (await c.post("/api/admin/users", json={"name": "bob"})).json()["id"]
        r = await c.post("/api/admin/tickets", json={"user": bob, "tier": "lite", "length": "day", "currency": "USD"})
        assert r.status_code == 409 and "Not enough capacity" in r.json()["error"]
        assert (await c.post("/api/admin/tickets", json={"user": "nobody", "tier": "lite", "length": "day", "currency": "USD"})).status_code == 404
        assert (await c.post("/api/admin/tickets", json={"user": bob, "tier": "gold", "length": "day", "currency": "USD"})).status_code == 400
        conn.execute("UPDATE fx_rates SET set_at=?", (int(time.time()) - 40 * 3600,))
        cfg.tickets.max_sold_pct = 80
        r = await c.post("/api/admin/tickets", json={"user": bob, "tier": "lite", "length": "day", "currency": "EUR"})
        assert r.status_code == 400 and "hours old" in r.json()["error"]
        r = await c.post("/api/admin/tickets", json={"user": bob, "tier": "lite", "length": "day", "currency": "EUR", "confirm_stale_rate": True})
        assert r.status_code == 200


async def test_cancel_bonus_capacity_and_ungate(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        t = (await c.post("/api/admin/tickets", json={"user": "alice", "tier": "standard", "length": "week", "currency": "USD"})).json()["ticket"]
        r = await c.post(f"/api/admin/tickets/{t['id']}/bonus", json={"share_pct": 5, "extra_days": 1, "note": "welcome"})
        assert r.status_code == 200 and r.json()["bonus"]["note"] == "welcome" and r.json()["ticket"]["effective_end"] == t["ends_at"] + DAY
        assert (await c.post(f"/api/admin/tickets/{t['id']}/bonus", json={"share_pct": 0, "extra_days": 0})).status_code == 400
        assert (await c.post(f"/api/admin/tickets/{t['id']}/bonus", json={"share_pct": "five"})).status_code == 400
        cap = (await c.get("/api/admin/capacity")).json()
        assert (cap["sold_now_pct"], cap["peak_30d_pct"], cap["max_sold_pct"]) == (30, 30, 80) and set(cap["utilization"]) == {"5h", "7d"}
        assert (await c.post(f"/api/admin/users/{ids['alice']}/ungate")).status_code == 400          # live ticket
        r = await c.post(f"/api/admin/tickets/{t['id']}/cancel")
        assert r.status_code == 200 and r.json()["moved"] == 0 and r.json()["dates_kept"] is False
        assert (await c.get("/api/admin/capacity")).json()["sold_now_pct"] == 0
        assert (await c.post(f"/api/admin/tickets/{t['id']}/cancel")).status_code == 400
        assert (await c.post(f"/api/admin/users/{ids['alice']}/ungate")).status_code == 200
        users = {u["name"]: u for u in (await c.get("/api/users")).json()["users"]}
        assert users["alice"]["ticket"]["gated"] is False
        assert (await c.get("/api/admin/tickets")).json()["tickets"][0]["state"] == "cancelled"
    assert [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action IN ('ticket_bonus','ticket_cancel','ungate') ORDER BY id")] == ["ticket_bonus", "ticket_cancel", "ungate"]
