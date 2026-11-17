"""No pass may run past the Anthropic subscription's end: the admin's date, else the current weekly reset."""
import time

import pytest

from claude_proxy import tickets
from tests.test_tickets_web import env  # noqa: F401  (a fixture here too)
from tests.test_web import admin_client
from tests.tickets_helpers import DAY, NOW, RESET7, live, seeded


def _clear(conn):
    conn.execute("DELETE FROM settings WHERE key=?", (tickets.SUB_END_KEY,))


def on_sale(conn, cfg):
    return {length: not tickets.sold_out(conn, cfg, "lite", length, NOW) for length in ("day", "week", "month")}


def test_unset_end_falls_back_to_the_weekly_reset(db):
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    _clear(conn)
    # The week resets at NOW + 3d: a day pass fits, a week or month pass would outlive it.
    assert on_sale(conn, cfg) == {"day": True, "week": False, "month": False}
    with pytest.raises(tickets.CapacityError, match="weekly reset"):
        tickets.grant(conn, cfg, ids["admin"], conn.execute("SELECT * FROM users WHERE id=?", (ids["alice"],)).fetchone(),
                      "lite", "week", "EUR", now=NOW, terms_version="v1")


@pytest.mark.parametrize("days, expect", [
    (0.5, {"day": False, "week": False, "month": False}),   # ending today: not even a day pass
    (5, {"day": True, "week": False, "month": False}),      # ending this week: no week pass
    (20, {"day": True, "week": True, "month": False}),      # ending this month: no month pass
    (40, {"day": True, "week": True, "month": True}),
])
def test_set_end_limits_each_length(db, days, expect):
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    tickets.set_subscription_end(conn, int(NOW + days * DAY), ids["admin"], now=NOW)
    assert on_sale(conn, cfg) == expect


def test_a_pass_ending_exactly_at_the_end_fits(db):
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    tickets.set_subscription_end(conn, NOW + 7 * DAY, ids["admin"], now=NOW)
    assert on_sale(conn, cfg)["week"]


def test_a_past_end_falls_back_to_the_weekly_reset(db):
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    conn.execute("UPDATE settings SET value=? WHERE key=?", (str(NOW - DAY), tickets.SUB_END_KEY))
    assert tickets.subscription_end(conn, (RESET7 - 7 * DAY, RESET7), NOW) == RESET7
    assert on_sale(conn, cfg) == {"day": True, "week": False, "month": False}
    from claude_proxy import capacity
    with pytest.raises(tickets.CapacityError, match="has passed"):
        capacity.check_sub_end(conn, (RESET7 - 7 * DAY, RESET7), NOW + 7 * DAY, NOW)


def test_setting_refuses_a_past_end_and_clears(db):
    conn, cfg, ids = seeded(db)
    with pytest.raises(tickets.TicketError):
        tickets.set_subscription_end(conn, NOW - 1, ids["admin"], now=NOW)
    tickets.set_subscription_end(conn, None, ids["admin"], now=NOW)
    assert tickets.subscription_end_set(conn) is None
    assert conn.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0] == "subscription_end_set"


async def test_admin_sets_and_reads_the_end(env):  # noqa: F811
    gw, conn, cfg, ids, keys = env
    end = int(time.time()) + 30 * DAY
    async with admin_client(gw) as c:
        assert (await c.post("/api/admin/subscription/end", json={"ends_at": end})).status_code == 200
        r = (await c.get("/api/admin/subscription")).json()
        assert (r["ends_at_set"], r["ends_at"]) == (end, end)
        assert (await c.post("/api/admin/subscription/end", json={"ends_at": int(time.time()) - 10})).status_code == 400
        assert (await c.post("/api/admin/subscription/end", json={"ends_at": ""})).status_code == 200
        assert (await c.get("/api/admin/subscription")).json()["ends_at_set"] is None


def test_buyers_see_when_it_is_back_not_the_subscription(db):
    from claude_proxy import orders
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    end = int(NOW + 5 * DAY + 3 * 3600)
    tickets.set_subscription_end(conn, end, ids["admin"], now=NOW)
    lite = next(x for x in tickets.price_table(conn, cfg, NOW, "EUR")["tiers"] if x["tier"] == "lite")["lengths"]
    assert (lite["week"]["sold_out"], lite["week"]["available_at"]) == (True, end)
    assert (lite["day"]["sold_out"], lite["day"]["available_at"]) == (False, None)
    with pytest.raises(orders.OrderError) as e:
        orders._check_on_sale(conn, cfg, "lite", "week", NOW, None, True)
    assert str(e.value) == "That pass is sold out; it is available again in 5 days and 3 hours."
    assert "subscription" not in str(e.value).lower()
