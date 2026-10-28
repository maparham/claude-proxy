"""Ticket upgrades (2026-10-26 spec): a running ticket raised to a higher tier for its time left, at the difference.
No longer sold (2026-10-28): the orders placed before are still fulfilled."""
import time

import pytest

from claude_proxy import limits, orders, payments, tickets
from claude_proxy.config import Currency
from tests.test_orders_web import client
from tests.test_tickets_web import env, grant  # noqa: F401  (the fixture)
from tests.test_web import admin_client, bearer
from tests.tickets_helpers import DAY, NOW, seeded, user


@pytest.fixture
def core(db):
    conn, cfg, ids = seeded(db)
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "EUR", now=NOW, terms_version="v1")
    return conn, cfg, ids, t


def test_quote_is_the_difference_for_the_time_left(core):
    conn, cfg, ids, t = core
    q = tickets.upgrade_quote(conn, cfg, tickets.get(conn, t["id"]), "standard", "EUR", NOW + 3.5 * DAY)
    assert (q["usd"], q["amount"], q["from_tier"]) == (13.5, 12.5, "lite")      # ($35 - $8) / 2, at 0.92 to the half euro
    assert (q["share_pct"], q["share_5h_pct"], q["ends_at"], q["sold_out"]) == (20, 20, t["effective_end"], False)


def test_no_upgrade_sideways_late_or_on_a_queued_ticket(core):
    conn, cfg, ids, t = core
    t = tickets.get(conn, t["id"])
    with pytest.raises(tickets.TicketError, match="not above"):
        tickets.upgrade_quote(conn, cfg, t, "lite", "EUR", NOW + DAY)
    with pytest.raises(tickets.TicketError, match="an hour"):
        tickets.upgrade_quote(conn, cfg, t, "standard", "EUR", t["effective_end"] - 600)
    with pytest.raises(tickets.TicketError, match="running"):
        tickets.upgrade_quote(conn, cfg, t, "standard", "EUR", NOW - 1)


def test_upgrade_raises_the_tier_and_the_limits(core):
    conn, cfg, ids, t = core
    at = NOW + 3.5 * DAY
    tickets.upgrade(conn, cfg, ids["admin"], user(conn, ids["alice"]), t["id"], "standard", "EUR", now=at)
    tk = tickets.get(conn, t["id"])
    assert tickets.tier_now(conn, tk) == "standard"
    assert tickets.bonus_share(conn, t["id"], at + 1) == 20
    assert tickets.sold_at(conn, 1, at + 1) == 25
    assert [s.tier for s in limits.ticket_states(conn, cfg, ids["alice"], at + 1)] == ["Standard", "Standard"]
    with pytest.raises(tickets.TicketError, match="not above"):   # already at the top
        tickets.upgrade_quote(conn, cfg, tk, "standard", "EUR", at + 60)
    assert conn.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0] == "ticket_upgrade"


def test_upgrade_order_and_paid_fulfilment(core):
    conn, cfg, ids, t = core
    cfg.tickets.currencies["IRT"] = Currency(round_to=1000)
    tickets.set_rate(conn, cfg, "IRT", 100000, ids["admin"], now=NOW - 3600)
    o = legacy_order(conn, ids, t, "IRT", 13.5, 100000, 1350000)
    assert orders.mine(conn, ids["alice"])["upgrade"] is True
    p = {"id": 0, "order_id": o["id"], "user_id": ids["alice"]}
    conn.execute("INSERT INTO payments(id, order_id, user_id, created_at, updated_at, amount, authority, status) "
                 "VALUES(1, ?, ?, 0, 0, 1350000, 'A1', 'started')", (o["id"], ids["alice"]))
    p = payments.get(conn, 1)
    r = payments._fulfil(conn, cfg, p, "REF", None, NOW + 3.5 * DAY + 60)
    assert r["status"] == "paid"
    assert orders.get(conn, o["id"])["status"] == "done"
    assert tickets.tier_now(conn, tickets.get(conn, t["id"])) == "standard"
    subject, body = orders.granted_mail_text(conn, cfg, orders.get(conn, o["id"]), tickets.get(conn, t["id"]))
    assert subject == "Your pass is upgraded: Standard"


async def test_an_upgrade_order_placed_before_is_sold_by_the_admin(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        assert (await grant(c, {"user": ids["alice"], "tier": "lite", "length": "week", "currency": "EUR"})).status_code == 200
    async with client(gw) as c:
        me = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()
        assert "upgrades" not in me and "upgrades" not in (me["pay"] or {})   # no longer offered
        assert me["current"]["upgraded_from"] is None
    t = tickets.covering(conn, ids["alice"], time.time())
    oid = legacy_order(conn, ids, t, "EUR", 1.0, 0.92, 1.0, now=time.time())["id"]
    assert orders.mine(conn, ids["alice"])["upgrade"] is True
    async with admin_client(gw) as c:
        r = await c.post(f"/api/admin/orders/{oid}", json={"action": "upgrade"})
        assert r.status_code == 200 and r.json()["order"]["status"] == "done"
    async with client(gw) as c:
        me = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()
    assert (me["current"]["tier"], me["current"]["upgraded_from"]["tier"], me["current"]["bonus_pct"]) == ("standard", "lite", 0)
    assert me["current"]["bonuses"] == []
    assert time.time() < me["current"]["effective_end"]


def legacy_order(conn, ids, t, currency, usd, rate, amount, now=NOW + 3.5 * DAY):
    """An upgrade order as orders.create stored them while upgrades were sold."""
    cur = conn.execute("INSERT INTO orders(created_at, updated_at, user_id, name, email, tier, length, currency, quoted_usd, quoted_rate, "
                       "quoted_amount, message, status, admin_mail, buyer_mail, terms_version, lang, upgrade_of) "
                       "VALUES(?,?,?,'alice','a@example.com','standard',?,?,?,?,?,'','new','off','off','v1','en',?)",
                       (now, now, ids["alice"], t["length"], currency, usd, rate, amount, t["id"]))
    return orders.get(conn, cur.lastrowid)


def test_bonus_days_after_an_upgrade_run_at_the_upgraded_tier(core):
    conn, cfg, ids, t = core
    at = NOW + 3.5 * DAY
    tickets.upgrade(conn, cfg, ids["admin"], user(conn, ids["alice"]), t["id"], "standard", "EUR", now=at)
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], extra_days=2, now=at + 60)
    end = tickets.get(conn, t["id"])["effective_end"]
    assert end == t["effective_end"] + 2 * DAY
    assert tickets.bonus_share(conn, t["id"], end - 60) == 20      # the extra days still carry the upgrade
    assert tickets.sold_at(conn, 1, end - 60) == 25
