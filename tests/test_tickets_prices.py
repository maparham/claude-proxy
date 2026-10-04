import pytest

from claude_proxy import tickets
from tests.tickets_helpers import DAY, NOW, seeded, tcfg


@pytest.mark.parametrize("usd,rate,step,expected", [
    (20, 0.92, 0.5, 18.5),      # 18.40 -> nearest 0.50 is 18.50
    (18.25, 1, 0.5, 18.5),      # a tie rounds up
    (18.24, 1, 0.5, 18.0),
    (3, 1, 0.01, 3.0),
    (3, 0.915, 0.01, 2.75),     # 2.745 -> ties up
    (100, 1.3333, 0.01, 133.33),
])
def test_round_local(usd, rate, step, expected):
    assert tickets.round_local(usd, rate, step) == pytest.approx(expected)


def test_usd_is_always_available_at_rate_one(db):
    conn, cfg, ids = seeded(db)
    assert tickets.currencies(cfg) == {"USD": 0.01, "EUR": 0.5}
    assert tickets.current_rate(conn, "USD") == {"currency": "USD", "rate": 1.0, "set_at": None, "set_by": None}
    assert tickets.rate_is_stale(tickets.current_rate(conn, "USD"), NOW) is False


def test_newest_rate_wins_and_history_is_kept(db):
    conn, cfg, ids = seeded(db)
    tickets.set_rate(conn, cfg, "EUR", 0.95, ids["admin"], now=NOW)
    r = tickets.current_rate(conn, "EUR")
    assert (r["rate"], r["set_at"], r["set_by"]) == (0.95, NOW, ids["admin"])
    assert conn.execute("SELECT COUNT(*) FROM fx_rates WHERE currency='EUR'").fetchone()[0] == 2
    assert conn.execute("SELECT action, target, detail_json FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[:2] == ("rate_set", "EUR")


def test_set_rate_refuses_an_unconfigured_currency(db):
    conn, cfg, ids = seeded(db)
    with pytest.raises(tickets.TicketError, match="GBP"):
        tickets.set_rate(conn, cfg, "GBP", 0.8, ids["admin"], now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM fx_rates WHERE currency='GBP'").fetchone()[0] == 0
    for bad in (0, -1, float("nan"), float("inf"), "0.9"):
        with pytest.raises(tickets.TicketError):
            tickets.set_rate(conn, cfg, "EUR", bad, ids["admin"], now=NOW)


def test_rate_is_stale_after_36_hours(db):
    conn, cfg, ids = seeded(db)
    r = tickets.current_rate(conn, "EUR")   # set an hour ago
    assert tickets.rate_is_stale(r, NOW) is False
    assert tickets.rate_is_stale(r, NOW + 36 * 3600) is True
    assert tickets.current_rate(conn, "CHF") is None


def test_prices_seeded_and_editable_with_audit(db):
    conn, cfg, ids = seeded(db)
    assert tickets.regular_price(conn, "lite", "week") == 8
    tickets.set_price(conn, cfg, "lite", "week", 9.5, ids["admin"], now=NOW)
    assert tickets.regular_price(conn, "lite", "week") == 9.5
    row = conn.execute("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == ("price_set", "lite:week")
    assert {(p["tier"], p["length"]) for p in tickets.prices(conn)} == {(t, l) for t in ("lite", "standard") for l in ("day", "week", "month")}
    with pytest.raises(tickets.TicketError):
        tickets.set_price(conn, cfg, "gold", "week", 9, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError):
        tickets.set_price(conn, cfg, "lite", "year", 9, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError):
        tickets.set_price(conn, cfg, "lite", "week", 0, ids["admin"], now=NOW)


def test_discount_replaces_the_price_while_active(db):
    conn, cfg, ids = seeded(db)
    d = tickets.create_discount(conn, cfg, "lite", "week", 6, NOW + 100, NOW + DAY, ids["admin"], now=NOW)
    assert tickets.price_now(conn, "lite", "week", NOW) == {"list_usd": 8, "usd": 8, "discount_id": None, "discount_ends_at": None}
    assert tickets.price_now(conn, "lite", "week", NOW + 100) == {"list_usd": 8, "usd": 6, "discount_id": d["id"], "discount_ends_at": NOW + DAY}
    assert tickets.price_now(conn, "lite", "week", NOW + DAY)["usd"] == 8          # half-open: over at ends_at
    assert conn.execute("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[:] == ("discount_create", "lite:week")


def test_discount_must_be_below_the_regular_price_and_in_the_future(db):
    conn, cfg, ids = seeded(db)
    with pytest.raises(tickets.TicketError, match="below the regular price"):
        tickets.create_discount(conn, cfg, "lite", "week", 8, NOW, NOW + DAY, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError, match="below the regular price"):
        tickets.create_discount(conn, cfg, "lite", "week", 9, NOW, NOW + DAY, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError, match="end in the future"):
        tickets.create_discount(conn, cfg, "lite", "week", 5, NOW - 2 * DAY, NOW - DAY, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError, match="before it ends"):
        tickets.create_discount(conn, cfg, "lite", "week", 5, NOW + DAY, NOW + DAY, ids["admin"], now=NOW)


def test_overlapping_discount_refused_but_touching_allowed(db):
    conn, cfg, ids = seeded(db)
    tickets.create_discount(conn, cfg, "lite", "week", 6, NOW, NOW + DAY, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError, match="already covers"):
        tickets.create_discount(conn, cfg, "lite", "week", 5, NOW + DAY - 1, NOW + 2 * DAY, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError, match="already covers"):   # a future one overlapping a future one
        tickets.create_discount(conn, cfg, "lite", "week", 5, NOW - 10, NOW + 10, ids["admin"], now=NOW)
    tickets.create_discount(conn, cfg, "lite", "week", 5, NOW + DAY, NOW + 2 * DAY, ids["admin"], now=NOW)   # touching
    tickets.create_discount(conn, cfg, "lite", "day", 2, NOW, NOW + DAY, ids["admin"], now=NOW)             # another length
    assert len(tickets.discounts(conn, NOW)) == 3


def test_cancelled_discount_frees_the_period(db):
    conn, cfg, ids = seeded(db)
    d = tickets.create_discount(conn, cfg, "lite", "week", 6, NOW, NOW + DAY, ids["admin"], now=NOW)
    tickets.cancel_discount(conn, d["id"], ids["admin"], now=NOW + 10)
    assert tickets.price_now(conn, "lite", "week", NOW + 20)["usd"] == 8
    tickets.create_discount(conn, cfg, "lite", "week", 7, NOW, NOW + DAY, ids["admin"], now=NOW + 20)
    with pytest.raises(tickets.TicketError):
        tickets.cancel_discount(conn, d["id"], ids["admin"], now=NOW + 30)   # already cancelled
    assert [x["usd"] for x in tickets.discounts(conn, NOW + 20)] == [7]
    assert len(tickets.discounts(conn, NOW + 20, include_ended=True)) == 2


def test_regular_price_lowered_below_a_discount_charges_the_lower_with_no_strike_through(db):
    conn, cfg, ids = seeded(db)
    tickets.create_discount(conn, cfg, "lite", "week", 6, NOW, NOW + DAY, ids["admin"], now=NOW)
    tickets.set_price(conn, cfg, "lite", "week", 5, ids["admin"], now=NOW + 10)
    assert tickets.price_now(conn, "lite", "week", NOW + 20) == {"list_usd": 5, "usd": 5, "discount_id": None, "discount_ends_at": None}
    tickets.set_price(conn, cfg, "lite", "week", 6, ids["admin"], now=NOW + 30)   # equal: no discount shown either
    assert tickets.price_now(conn, "lite", "week", NOW + 40)["discount_id"] is None
