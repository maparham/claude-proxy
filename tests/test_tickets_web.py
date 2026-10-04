import sqlite3
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


async def grant(c, body):
    """Grant as the dashboard does: preview first, then send back the price and rate the preview showed."""
    p = await c.post("/api/admin/tickets/preview", json=body)
    quote = {k: p.json()[k] for k in ("usd", "rate")} if p.status_code == 200 else {}
    return await c.post("/api/admin/tickets", json=quote | body)


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
        r = await grant(c, {"user": "alice", "tier": "lite", "length": "week", "currency": "EUR", "note": "paid 7.50",
                                                      "remove_limits": [{"kind": "tokens_daily", "scope": "*"}]})
        assert r.status_code == 200, r.text
        t = r.json()["ticket"]
        assert (t["user_name"], t["amount"], t["note"]) == ("alice", 7.5, "paid 7.50")
        assert conn.execute("SELECT COUNT(*) FROM limits WHERE user_id=?", (ids["alice"],)).fetchone()[0] == 0
        lst = (await c.get(f"/api/admin/tickets?user_id={ids['alice']}")).json()["tickets"]
        assert lst[0]["id"] == t["id"] and lst[0]["state"] == "active" and lst[0]["granted_by_name"] == "admin" and lst[0]["bonuses"] == []
        users = {u["name"]: u for u in (await c.get("/api/users")).json()["users"]}
        assert users["alice"]["ticket"]["gated"] and users["alice"]["ticket"]["live"] and users["alice"]["ticket"]["current"]["id"] == t["id"]
        assert users["admin"]["ticket"] == {"gated": False, "live": False, "current": None, "queued": None, "has_tickets": False, "paused": False}
        assert users["alice"]["ticket"]["has_tickets"] is True
        # A second grant queues after the first; the preview says so.
        p = (await c.post("/api/admin/tickets/preview", json={"user": "alice", "tier": "lite", "length": "day", "currency": "USD"})).json()
        assert p["queued"] is True and p["starts_at"] == t["ends_at"]
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions[-3:] == ["credit_removed", "limit_clear", "ticket_grant"]


async def test_grant_refusals_map_to_400_and_409(env):
    gw, conn, cfg, ids, keys = env
    cfg.tickets.max_sold_pct = 5
    async with admin_client(gw) as c:
        assert (await grant(c, {"user": "alice", "tier": "lite", "length": "week", "currency": "EUR"})).status_code == 200
        bob = (await c.post("/api/admin/users", json={"name": "bob"})).json()["id"]
        r = await grant(c, {"user": bob, "tier": "lite", "length": "day", "currency": "USD"})
        assert r.status_code == 409 and "Not enough capacity" in r.json()["error"]
        assert (await grant(c, {"user": "nobody", "tier": "lite", "length": "day", "currency": "USD"})).status_code == 404
        assert (await grant(c, {"user": bob, "tier": "gold", "length": "day", "currency": "USD"})).status_code == 400
        conn.execute("UPDATE fx_rates SET set_at=?", (int(time.time()) - 40 * 3600,))
        cfg.tickets.max_sold_pct = 80
        r = await grant(c, {"user": bob, "tier": "lite", "length": "day", "currency": "EUR"})
        assert r.status_code == 400 and "hours old" in r.json()["error"]
        r = await grant(c, {"user": bob, "tier": "lite", "length": "day", "currency": "EUR", "confirm_stale_rate": True})
        assert r.status_code == 200


async def test_cancel_bonus_capacity_and_ungate(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        t = (await grant(c, {"user": "alice", "tier": "standard", "length": "week", "currency": "USD"})).json()["ticket"]
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


async def test_pricing_api_is_public_and_says_only_prices_and_sold_out(env):
    gw, conn, cfg, ids, keys = env
    now = int(time.time())
    tickets.create_discount(conn, cfg, "lite", "month", 15, now - 10, now + DAY, ids["admin"], now=now)
    cfg.tickets.max_sold_pct = 25
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "standard", "day", "USD", now=now)
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/api/pricing")
        assert r.status_code == 200
        p = r.json()
    assert p["currency"] == "EUR" and p["rate_set_at"] is not None and p["how_to_buy"].startswith("Send")
    lite = next(t for t in p["tiers"] if t["tier"] == "lite")
    assert lite["compare"] == "Claude Pro" and lite["lengths"]["month"] == {
        "days": 30, "list_usd": 20, "usd": 15, "discount_ends_at": now + DAY, "amount": 14.0, "list_amount": 18.5, "sold_out": True}   # 25 sold: nothing fits today
    assert lite["lengths"]["week"]["sold_out"] is True and lite["lengths"]["week"]["discount_ends_at"] is None
    assert lite["hours"] == {"sonnet": {"per_5h": None, "per_day": None}, "opus": {"per_5h": None, "per_day": None}}
    text = r.text
    for leaked in ("utilization", "alice", "sold_now", "shares"):
        assert leaked not in text


async def test_pricing_falls_back_to_usd_without_a_rate(env):
    gw, conn, cfg, ids, keys = env
    conn.execute("DELETE FROM fx_rates")
    async with asgi_client(create_dashboard_app(gw)) as c:
        p = (await c.get("/api/pricing")).json()
        page = await c.get("/")
    assert p["currency"] == "USD" and p["rate_set_at"] is None
    assert next(t for t in p["tiers"] if t["tier"] == "lite")["lengths"]["day"]["amount"] == 3.0
    assert page.status_code == 200 and "home.js?v=" in page.text and "home.css?v=" in page.text and "app.css?v=" in page.text
    assert page.headers["content-security-policy"].startswith("default-src 'self'")


async def test_pricing_reads_only_usage_estimates(env):
    gw, conn, cfg, ids, keys = env
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (int(time.time()), "sonnet", "5h", 80, 1.25))
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (int(time.time()), "sonnet", "7d", 80, 0.25))
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (int(time.time()), "opus", "5h", 10, 5.0))
    conn.execute("DELETE FROM requests")
    conn.execute("DELETE FROM quota_snapshots")
    async with asgi_client(create_dashboard_app(gw)) as c:
        p = (await c.get("/api/pricing")).json()
    lite = next(t for t in p["tiers"] if t["tier"] == "lite")
    assert lite["hours"] == {"sonnet": {"per_5h": 4.0, "per_day": 2.8}, "opus": {"per_5h": None, "per_day": None}}   # 5/1.25; (5/7)/0.25 = 2.857


async def test_pricing_is_404_when_tickets_are_off(env):
    gw, conn, cfg, ids, keys = env
    cfg.tickets.enabled = False
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/pricing")).status_code == 404
        assert (await c.get("/pricing")).status_code == 404
        for path in ("/", "/?home"):
            r = await c.get(path)
            assert (r.status_code, r.headers["location"]) == (307, "/dashboard")


async def test_pricing_moved_to_the_home_page(env):
    gw, conn, cfg, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/pricing")
    assert (r.status_code, r.headers["location"]) == (308, "/#pricing")


async def test_home_is_for_visitors_and_signed_in_browsers_go_to_the_dashboard(env):
    gw, conn, cfg, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        page = await c.get("/")
        assert page.status_code == 200 and 'id="pricing"' in page.text and 'name="robots" content="noindex"' in page.text
        assert 'href="/dashboard"' in page.text
    async with admin_client(gw) as c:
        r = await c.get("/")
        assert (r.status_code, r.headers["location"]) == (307, "/dashboard")
        assert (await c.get("/?home")).status_code == 200            # the admin previews the public page


async def test_home_ignores_a_stale_session_cookie(env):
    gw, conn, cfg, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/", cookies={"cp_session": "expired-or-junk"})
    assert r.status_code == 200 and 'id="pricing"' in r.text


async def test_me_tickets_shows_the_users_own_ticket_and_nothing_about_the_account(env):
    gw, conn, cfg, ids, keys = env
    now = int(time.time())
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "EUR", now=now - 3600)
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=2, extra_days=1, note="welcome", now=now)
    q = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "EUR", now=now)
    async with asgi_client(create_dashboard_app(gw)) as c:
        me = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()
    assert me["enabled"] and me["gated"]
    cur = me["current"]
    assert (cur["label"], cur["share_pct"], cur["bonus_days"], cur["bonus_share"]) == ("Lite", 5, 1, 2)
    assert "id" not in cur and "id" not in me["queued"]          # a ticket id counts the account's sales
    assert cur["effective_end"] == t["ends_at"] + DAY and cur["day_end"] == t["starts_at"] + DAY
    assert cur["bonuses"] == [{"share_pct": 2, "note": "welcome", "ends_at": t["ends_at"] + DAY}]
    assert cur["day_bonuses"] == [] and abs(me["now"] - time.time()) < 5   # that bonus's days show with its share
    assert (me["queued"]["label"], me["queued"]["starts_at"]) == ("Lite", t["ends_at"] + DAY)
    assert me["prices"]["currency"] == "EUR" and me["how_to_buy"].startswith("Send")
    for leaked in ("utilization", "sold_now", "shares", "granted_by"):
        assert leaked not in str(me)
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/me/tickets")).status_code == 401


async def test_pricing_api_says_the_servers_time(env):
    gw, conn, cfg, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        p = (await c.get("/api/pricing")).json()
    assert abs(p["now"] - time.time()) < 5          # the page counts down on the server's clock, not the visitor's


async def test_money_inputs_are_rounded_to_cents_and_bounded(env):
    gw, conn, cfg, ids, keys = env
    now = int(time.time())
    async with admin_client(gw) as c:
        assert (await c.post("/api/admin/prices", json={"tier": "lite", "length": "week", "usd": 9.999})).status_code == 200
        assert {(x["tier"], x["length"]): x["usd"] for x in (await c.get("/api/admin/prices")).json()["prices"]}[("lite", "week")] == 10
        r = await c.post("/api/admin/discounts", json={"tier": "lite", "length": "week", "usd": 6.004, "starts_at": now, "ends_at": now + DAY})
        assert r.status_code == 200 and r.json()["discount"]["usd"] == 6
        assert (await c.post("/api/admin/prices", json={"tier": "lite", "length": "day", "usd": 0.001})).status_code == 400
        for raw in ('{"tier": "lite", "length": "day", "usd": NaN}', '{"tier": "lite", "length": "day", "usd": Infinity}',
                    '{"tier": "lite", "length": "day", "usd": 1e300}', '{"tier": "lite", "length": "day", "usd": 1e6}'):
            r = await c.post("/api/admin/prices", content=raw, headers={"content-type": "application/json"})
            assert r.status_code == 400, raw
        for rate in ("NaN", "1e300", "0", "1e10"):
            r = await c.post("/api/admin/rates", content=f'{{"currency": "EUR", "rate": {rate}}}', headers={"content-type": "application/json"})
            assert r.status_code == 400, rate
        r = await c.post("/api/admin/discounts", content=f'{{"tier": "lite", "length": "day", "usd": 1, "starts_at": {now}, "ends_at": 1e300}}',
                         headers={"content-type": "application/json"})
        assert r.status_code == 400
        t = (await grant(c, {"user": "alice", "tier": "lite", "length": "week", "currency": "USD"})).json()["ticket"]
        for body in ({"extra_days": 1.5}, {"share_pct": 1e300}, {"share_pct": 101}, {"extra_days": 1e300}):
            r = await c.post(f"/api/admin/tickets/{t['id']}/bonus", json=body)
            assert r.status_code == 400, body
        r = await c.post(f"/api/admin/tickets/{t['id']}/bonus", content='{"share_pct": NaN, "extra_days": 1}', headers={"content-type": "application/json"})
        assert r.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM ticket_bonuses").fetchone()[0] == 0


async def test_notes_are_capped_at_200_characters(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        r = await grant(c, {"user": "alice", "tier": "lite", "length": "week", "currency": "USD", "note": "x" * 201})
        assert r.status_code == 400 and "200" in r.json()["error"]
        t = (await grant(c, {"user": "alice", "tier": "lite", "length": "week", "currency": "USD", "note": "x" * 200})).json()["ticket"]
        r = await c.post(f"/api/admin/tickets/{t['id']}/bonus", json={"extra_days": 1, "note": "y" * 201})
        assert r.status_code == 400 and "200" in r.json()["error"]


async def test_grant_refused_when_the_price_or_rate_changed_since_the_preview(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        p = (await c.post("/api/admin/tickets/preview", json={"user": "alice", "tier": "lite", "length": "week", "currency": "EUR"})).json()
        assert (await c.post("/api/admin/prices", json={"tier": "lite", "length": "week", "usd": 9})).status_code == 200
        body = {"user": "alice", "tier": "lite", "length": "week", "currency": "EUR", "usd": p["usd"], "rate": p["rate"]}
        r = await c.post("/api/admin/tickets", json=body)
        assert r.status_code == 409 and "price" in r.json()["error"]
        assert (await c.post("/api/admin/rates", json={"currency": "EUR", "rate": 0.95})).status_code == 200
        r = await c.post("/api/admin/tickets", json=body | {"usd": 9})
        assert r.status_code == 409 and "rate" in r.json()["error"]
        r = await c.post("/api/admin/tickets", json=body | {"usd": 9, "rate": 0.95})
        assert r.status_code == 200 and r.json()["ticket"]["usd"] == 9
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


async def test_a_locked_database_is_a_503_not_a_500(env, monkeypatch):
    gw, conn, cfg, ids, keys = env

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(tickets, "grant", locked)
    async with admin_client(gw) as c:
        r = await grant(c, {"user": "alice", "tier": "lite", "length": "week", "currency": "USD"})
        assert r.status_code == 503 and "try again" in r.json()["error"] and r.headers["retry-after"]


async def test_dashboard_links_the_pricing_page_only_while_tickets_are_on(env):
    gw, conn, cfg, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        page = (await c.get("/dashboard")).text
        assert page.count('href="/pricing"') == 2                 # the header, and the sign-in page's foot
        assert "pricing-link hidden" not in page
        assert 'href="/"' in (await c.get("/pricing")).text       # and back
        cfg.tickets.enabled = False
        page = (await c.get("/dashboard")).text
        assert page.count("pricing-link hidden") == 2


async def test_me_tickets_shows_the_note_of_an_extra_days_bonus(env):
    gw, conn, cfg, ids, keys = env
    now = int(time.time())
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "USD", now=now - 3600)
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], extra_days=2, note="sorry for Tuesday", now=now)
    async with asgi_client(create_dashboard_app(gw)) as c:
        cur = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["current"]
    assert cur["bonus_days"] == 2 and cur["day_bonuses"] == [{"extra_days": 2, "note": "sorry for Tuesday"}]


async def test_users_with_ungated_tickets_still_have_tickets(env):
    gw, conn, cfg, ids, keys = env
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=int(time.time()) - 2 * DAY)
    tickets.ungate(conn, ids["admin"], ids["alice"])
    async with admin_client(gw) as c:
        users = {u["name"]: u for u in (await c.get("/api/users")).json()["users"]}
    assert users["alice"]["ticket"]["gated"] is False and users["alice"]["ticket"]["has_tickets"] is True   # still in the ticket filter


async def test_a_grant_needs_the_quote_its_preview_showed(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        r = await c.post("/api/admin/tickets", json={"user": "alice", "tier": "lite", "length": "week", "currency": "USD"})
        assert r.status_code == 400 and "Preview the ticket first" in r.json()["error"]
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


async def test_with_tickets_off_a_paused_user_is_shown_and_can_be_ungated(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        assert (await grant(c, {"user": "alice", "tier": "lite", "length": "week", "currency": "USD"})).status_code == 200
    cfg.tickets.enabled = False
    async with admin_client(gw) as c:
        users = {u["name"]: u for u in (await c.get("/api/users")).json()["users"]}
        assert users["alice"]["ticket"]["paused"] and users["alice"]["ticket"]["live"]
        assert users["admin"]["ticket"] is None                       # never gated: nothing to show while tickets are off
        me = (await c.get("/api/me/status", headers=bearer(keys["alice"]))).json()
        assert me["paused"] is True
        assert (await c.post(f"/api/admin/users/{ids['alice']}/ungate", json={})).status_code == 200
        me = (await c.get("/api/me/status", headers=bearer(keys["alice"]))).json()
        assert me["paused"] is False
    assert conn.execute("SELECT cancelled_at IS NOT NULL FROM tickets").fetchone()[0] == 1
    assert last_audit(conn) == ("ungate", "alice")


async def test_the_ticket_filter_reaches_a_deleted_users_tickets(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        t = (await grant(c, {"user": "alice", "tier": "lite", "length": "day", "currency": "USD"})).json()["ticket"]
        assert (await c.post(f"/api/admin/tickets/{t['id']}/cancel", json={})).status_code == 200
        conn.execute("UPDATE tickets SET ungated_at=1")
        dbm.revoke(conn, ids["alice"], ids["admin"])
        dbm.delete_user(conn, ids["alice"], ids["admin"])
        r = (await c.get("/api/admin/tickets")).json()
        assert r["deleted_users"] == ["alice"]
        assert [x["id"] for x in (await c.get("/api/admin/tickets?deleted=alice")).json()["tickets"]] == [t["id"]]
        assert (await c.get("/api/admin/tickets?deleted=bob")).json()["tickets"] == []


HOME_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [src, serverNow, clientNow, endsAt, already, dataJson, fail] = [fs.readFileSync(process.argv[2], "utf8"), +process.argv[3], +process.argv[4],
  +process.argv[5], process.argv[6], process.argv[7], process.argv[8] === "1"];
const data = JSON.parse(dataJson); data.now = serverNow;
let reloads = 0, tickFn = null, onToggle = null;
const store = {}; if (already) store[already] = "1";
const countdownEl = { dataset: { ends: String(endsAt) }, textContent: "" };
const els = {};
const el = (id) => (els[id] ||= { id, innerHTML: "", textContent: "", addEventListener: (ev, fn) => { if (id === "length-toggle") onToggle = fn; } });
const ctx = {
  console, Intl, Math, String, Object, Number, JSON, URLSearchParams, encodeURIComponent,
  Date: { now: () => clientNow * 1000 },
  document: { documentElement: { dataset: {} }, getElementById: el, querySelectorAll: (sel) => sel === ".countdown" ? [countdownEl] : [] },
  location: { reload: () => { reloads++; } },
  localStorage: { getItem: () => null },
  sessionStorage: { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } },
  setInterval: (fn) => { tickFn = fn; },
  fetch: () => fail ? Promise.reject(new Error("down")) : Promise.resolve({ ok: true, json: () => Promise.resolve(data) }),
};
vm.runInNewContext(src, ctx);
setTimeout(() => {
  const out = { howToBuy: el("how-to-buy").textContent, pricing: el("cards").innerHTML };
  if (!fail) {
    out.cards = Object.fromEntries(["day", "week", "month"].map((k) => [k, ctx.cardsHtml(data, k)]));
    out.save = Object.fromEntries(["day", "week", "month"].map((k) => [k, ctx.savePct(data.tiers[0], k)]));
    out.hints = ctx.hintLines(data.tiers[0]);
    onToggle({ target: { closest: () => ({ dataset: { length: "month" } }) } });
    out.afterToggle = el("cards").innerHTML;
    for (let i = 0; i < 3; i++) tickFn();
  }
  out.reloads = reloads; out.text = countdownEl.textContent;
  console.log(JSON.stringify(out));
}, 10);
"""


def _tier(tier="lite", label="Lite", compare="Claude Pro", hours=None, **lengths):
    base = {k: {"days": d, "usd": u, "list_usd": u, "amount": u, "list_amount": u, "discount_ends_at": None, "sold_out": False}
            for k, d, u in (("day", 1, 3), ("week", 7, 8), ("month", 30, 20))}
    for k, v in lengths.items():
        base[k].update(v)
    return {"tier": tier, "label": label, "share_pct": 5, "compare": compare, "hours": hours or {}, "lengths": base}


def _prices(*tiers, how_to_buy=""):
    return {"currency": "USD", "rate_set_at": None, "how_to_buy": how_to_buy, "tiers": list(tiers) or [_tier()]}


def _run_home(tmp_path, data=None, server_now=1_000_000, client_now=1_000_000, ends_at=0, already="", fail=False):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which("node"):
        pytest.skip("no node here")
    harness = tmp_path / "home_harness.js"
    harness.write_text(HOME_HARNESS)
    js = Path(__file__).resolve().parent.parent / "src" / "claude_proxy" / "static" / "home.js"
    out = subprocess.run(["node", str(harness), str(js), str(server_now), str(client_now), str(ends_at), already,
                          json.dumps(data or _prices()), "1" if fail else "0"], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


def test_home_counts_down_on_the_servers_clock(tmp_path):
    ends = 1_001_800
    disc = {"usd": 6, "amount": 6, "discount_ends_at": ends}
    data = _prices(_tier(week=disc, month=disc))
    # The visitor's clock is an hour ahead: the offer still has 30 minutes on the server's clock.
    r = _run_home(tmp_path, data, server_now=1_000_000, client_now=1_003_600, ends_at=ends)
    assert r["reloads"] == 0 and r["text"] == "Offer ends in 00h 30m"
    r = _run_home(tmp_path, data, server_now=1_002_000, client_now=1_002_000, ends_at=ends)   # ended: one reload
    assert r["reloads"] == 1
    r = _run_home(tmp_path, data, server_now=1_002_000, client_now=1_002_000, ends_at=ends, already="pricing-reloaded-1001800")
    assert r["reloads"] == 0 and r["text"] == "Offer ended"


def test_home_shows_week_first_and_the_toggle_switches_length(tmp_path):
    r = _run_home(tmp_path)
    assert "/week" in r["pricing"] and "/month" not in r["pricing"]
    assert "/month" in r["afterToggle"] and "/week" not in r["afterToggle"]


def test_home_sold_out_is_per_length(tmp_path):
    r = _run_home(tmp_path, _prices(_tier(week={"sold_out": True})))
    assert "sold-out" in r["cards"]["week"] and "Sold out" in r["cards"]["week"] and "Get it" not in r["cards"]["week"]
    assert "sold-out" not in r["cards"]["month"] and 'href="/dashboard?tier=lite&length=month"' in r["cards"]["month"]


def test_home_saving_is_against_the_charged_day_price(tmp_path):
    r = _run_home(tmp_path)
    assert r["save"] == {"day": 0, "week": 61, "month": 77}            # 1 - 8/21, 1 - 20/90, rounded down
    assert "Save 61% vs daily" in r["cards"]["week"] and "Save" not in r["cards"]["day"]


def test_home_saving_is_hidden_when_nothing_is_saved(tmp_path):
    r = _run_home(tmp_path, _prices(_tier(week={"usd": 30, "amount": 30})))
    assert "Save" not in r["cards"]["week"]
    r = _run_home(tmp_path, _prices(_tier(day={"usd": 0, "amount": 0, "list_usd": 0, "list_amount": 0})))   # a free day: no division by zero
    assert r["save"]["month"] == 0 and "Save" not in r["cards"]["month"]


def test_home_shows_a_discount_ribbon(tmp_path):
    r = _run_home(tmp_path, _prices(_tier(week={"usd": 6, "amount": 6, "discount_ends_at": 2_000_000})))
    assert "−25%" in r["cards"]["week"] and "<s>" in r["cards"]["week"] and "−" not in r["cards"]["month"]


def test_home_hints_drop_missing_numbers_and_families(tmp_path):
    hours = {"sonnet": {"per_5h": 4.0, "per_day": None}, "opus": {"per_5h": None, "per_day": None}}
    r = _run_home(tmp_path, _prices(_tier(hours=hours)))
    assert r["hints"] == ["Sonnet: at least 4 h per 5-hour window"]


def test_home_highlights_the_second_tier_only(tmp_path):
    r = _run_home(tmp_path, _prices(_tier(), _tier(tier="standard", label="Standard")))
    first, second = r["cards"]["week"].split("</article>")[:2]
    assert "featured" not in first and "featured" in second
    assert "featured" not in _run_home(tmp_path)["cards"]["week"]   # one tier: nothing highlighted


def test_home_fills_how_to_buy_with_a_fallback(tmp_path):
    assert _run_home(tmp_path)["howToBuy"] == "Ask the gateway admin."
    assert _run_home(tmp_path, _prices(how_to_buy="Bank transfer, then email."))["howToBuy"] == "Bank transfer, then email."


def test_home_escapes_tier_text(tmp_path):
    r = _run_home(tmp_path, _prices(_tier(label="<b>Lite</b>", compare="<i>Pro</i>")))
    assert "&lt;b&gt;Lite&lt;/b&gt;" in r["cards"]["week"] and "<i>" not in r["cards"]["week"]


def test_home_says_when_prices_are_unavailable(tmp_path):
    assert _run_home(tmp_path, fail=True)["pricing"] == '<p class="muted">Prices are not available right now.</p>'
