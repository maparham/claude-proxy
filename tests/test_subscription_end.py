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


def test_the_admin_may_grant_past_the_end(db):
    import json
    from tests.tickets_helpers import user
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    _clear(conn)   # the end falls back to the weekly reset, NOW + 3d: a week pass outlasts it
    alice = user(conn, ids["alice"])
    p = tickets.preview(conn, cfg, alice, "lite", "week", "EUR", NOW)
    assert (p["available"], p["short_end"], p["over_end"]) == (False, True, False) and "weekly reset" in p["end_warning"]
    p = tickets.preview(conn, cfg, alice, "lite", "week", "EUR", NOW, over_end=True)
    assert (p["available"], p["short_end"], p["over_end"]) == (True, True, True)
    assert p["sold_out_now"]   # buyers still see it sold out: the override is the admin's alone
    t = tickets.grant(conn, cfg, ids["admin"], alice, "lite", "week", "EUR", now=NOW, terms_version=tickets.TERMS_VERSION,
                      over_end=True)
    a = json.loads(conn.execute("SELECT detail_json FROM audit_log WHERE action='ticket_grant'").fetchone()[0])
    assert a["ticket_id"] == t["id"] and a["over_end"] is True


def test_over_end_does_not_lift_the_weekly_checks(db):
    from tests.tickets_helpers import report, user
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    _clear(conn)
    report(conn, ids, 99.0, NOW - 30)   # the week is nearly full too
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "lite", "week", "EUR", NOW, over_end=True)
    assert (p["available"], p["short_end"], p["short_week"]) == (False, True, True) and "This week has" in p["reason"]


@pytest.mark.parametrize("over_end, over_week, ok, refusal", [
    (False, False, False, "This week has"),   # this week's room is named first, as the checks run
    (True, False, False, "This week has"),
    (False, True, False, "weekly reset"),
    (True, True, True, None),
])
def test_over_week_oversells_this_week(db, over_end, over_week, ok, refusal):
    from tests.tickets_helpers import report, user
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    _clear(conn)
    report(conn, ids, 99.0, NOW - 30)   # this week's room is short, and a week pass outlasts the weekly reset
    alice = user(conn, ids["alice"])
    p = tickets.preview(conn, cfg, alice, "lite", "week", "EUR", NOW, over_end=over_end, over_week=over_week)
    assert (p["available"], p["short_week"], p["over_week"]) == (ok, True, over_week)
    run = lambda: tickets.grant(conn, cfg, ids["admin"], alice, "lite", "week", "EUR", now=NOW,  # noqa: E731
                                terms_version=tickets.TERMS_VERSION, over_end=over_end, over_week=over_week)
    if ok:
        run()
        detail = conn.execute("SELECT detail_json FROM audit_log WHERE action='ticket_grant'").fetchone()["detail_json"]
        assert '"over_week": true' in detail
    else:
        with pytest.raises(tickets.CapacityError, match=refusal):
            run()


def test_the_end_refusal_is_named(db):
    from tests.tickets_helpers import user
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    _clear(conn)
    with pytest.raises(tickets.CapacityError, match="weekly reset; no end date is set"):
        tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "EUR", now=NOW,
                      terms_version=tickets.TERMS_VERSION)


@pytest.mark.parametrize("over_5h, over_end, ok, refusal", [
    (False, False, False, "5-hour window"),
    (True, False, False, "subscription ends"),
    (False, True, False, "5-hour window"),
    (True, True, True, None),
])
def test_both_overrides_when_both_are_short(db, over_5h, over_end, ok, refusal):
    from tests.test_capacity_points import _five_hour_cfg, five_hour
    from tests.tickets_helpers import user
    conn, cfg, ids = seeded(db)
    _five_hour_cfg(conn, cfg, ids)
    five_hour(conn, 95.0)
    tickets.set_subscription_end(conn, int(NOW + DAY / 2), ids["admin"], now=NOW)   # ends before a day pass would
    alice = user(conn, ids["alice"])
    p = tickets.preview(conn, cfg, alice, "lite", "day", "EUR", NOW, over_5h=over_5h, over_end=over_end)
    assert (p["available"], p["short_5h"], p["short_end"]) == (ok, True, True)
    assert "5-hour window" in p["reason"]   # the dialog keeps the 5-hour line; the end has its own (end_warning)
    run = lambda: tickets.grant(conn, cfg, ids["admin"], alice, "lite", "day", "EUR", now=NOW,  # noqa: E731
                                terms_version=tickets.TERMS_VERSION, over_5h=over_5h, over_end=over_end)
    if ok:
        run()
    else:
        with pytest.raises(tickets.CapacityError, match=refusal):
            run()


async def test_admin_checks_the_plan_now(env):  # noqa: F811
    gw, conn, cfg, ids, keys = env
    sent = [{"organization": {"organization_type": "claude_pro", "rate_limit_tier": "default_claude_ai"}}, None]

    async def fetch_profile():
        return sent.pop(0)
    gw.poller.backend.fetch_profile = fetch_profile
    async with admin_client(gw) as c:
        r = await c.post("/api/admin/subscription/refresh")
        assert r.status_code == 200 and r.json()["profile"]["profile"]["organization"]["organization_type"] == "claude_pro"
        assert gw.poller.next_plan_check > time.time()   # the hourly check starts over from this one
        assert (await c.post("/api/admin/subscription/refresh")).status_code == 502   # Anthropic sent nothing
