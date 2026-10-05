"""Payments design, section 4 (Grant) and section 3 (Data): the paid grant and the payments table."""
import pytest

from claude_proxy import orders, tickets
from claude_proxy.config import Currency
from tests.tickets_helpers import NOW, seeded, user


@pytest.fixture
def env(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.currencies["IRT"] = Currency(round_to=1000)
    tickets.set_rate(conn, cfg, "IRT", 100000, ids["admin"], now=NOW - 3600)
    return conn, cfg, ids


def irt_order(conn, cfg, ids, notify=False):
    return orders.create(conn, cfg, tier="lite", length="week", currency="IRT", name="alice", email="a@example.com",
                         user_id=ids["alice"], notify=notify, now=NOW)


def test_order_without_notify_sends_no_mail(env):
    conn, cfg, ids = env
    from claude_proxy.config import EmailConfig
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    o = irt_order(conn, cfg, ids)
    assert (o["admin_mail"], o["buyer_mail"], o["quoted_amount"]) == ("off", "off", 800000)


def test_paid_grant_uses_the_order_quote(env):
    conn, cfg, ids = env
    o = irt_order(conn, cfg, ids)
    tickets.set_rate(conn, cfg, "IRT", 120000, ids["admin"], now=NOW - 60)          # the rate moved during the payment
    tickets.set_price(conn, cfg, "lite", "week", 9, ids["admin"], now=NOW - 60)      # and the price
    seen = []
    t = tickets.grant(conn, cfg, None, user(conn, ids["alice"]), "lite", "week", "IRT", order_id=o["id"], paid=True,
                      after=seen.append, now=NOW)
    assert (t["usd"], t["rate"], t["amount"]) == (8, 100000, 800000)
    assert seen == [t["id"]]
    assert orders.get(conn, o["id"])["status"] == "done"


def test_paid_grant_ignores_a_stale_rate(env):
    conn, cfg, ids = env
    o = irt_order(conn, cfg, ids)
    t = tickets.grant(conn, cfg, None, user(conn, ids["alice"]), "lite", "week", "IRT", order_id=o["id"], paid=True,
                      now=NOW + 40 * 3600)
    assert t["amount"] == 800000


def test_after_failing_rolls_the_grant_back(env):
    conn, cfg, ids = env
    o = irt_order(conn, cfg, ids)
    def boom(tid):
        raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        tickets.grant(conn, cfg, None, user(conn, ids["alice"]), "lite", "week", "IRT", order_id=o["id"], paid=True, after=boom, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
    assert orders.get(conn, o["id"])["status"] == "new"


def test_paid_needs_an_order(env):
    conn, cfg, ids = env
    with pytest.raises(tickets.TicketError):
        tickets.grant(conn, cfg, None, user(conn, ids["alice"]), "lite", "week", "IRT", paid=True, now=NOW)


def test_payments_table(env):
    conn, cfg, ids = env
    o = irt_order(conn, cfg, ids)
    conn.execute("INSERT INTO payments(order_id, user_id, created_at, updated_at, amount, authority, status) VALUES(?,?,?,?,?,?,'started')",
                 (o["id"], ids["alice"], NOW, NOW, 800000, "A1"))
    with pytest.raises(Exception):   # authority is unique
        conn.execute("INSERT INTO payments(order_id, user_id, created_at, updated_at, amount, authority, status) VALUES(?,?,?,?,?,?,'started')",
                     (o["id"], ids["alice"], NOW, NOW, 800000, "A1"))
    with pytest.raises(Exception):   # unknown status
        conn.execute("INSERT INTO payments(order_id, user_id, created_at, updated_at, amount, authority, status) VALUES(?,?,?,?,?,?,'weird')",
                     (o["id"], ids["alice"], NOW, NOW, 800000, "A2"))
