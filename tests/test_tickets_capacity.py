import sqlite3
import threading

import pytest

from claude_proxy import db as dbm
from claude_proxy import tickets
from tests.tickets_helpers import DAY, NOW, seeded, user


def grant(conn, cfg, ids, who="alice", tier="lite", length="week", now=NOW, **kw):
    return tickets.grant(conn, cfg, ids["admin"], user(conn, ids[who]), tier, length, kw.pop("currency", "EUR"), now=now, **kw)


def add_user(conn, ids, name):
    ids[name], _ = dbm.create_user(conn, name)
    return ids[name]


def test_grant_records_prices_rate_and_period(db):
    conn, cfg, ids = seeded(db)
    t = grant(conn, cfg, ids)
    assert (t["tier"], t["share_pct"], t["length"], t["days"]) == ("lite", 5, "week", 7)
    assert (t["starts_at"], t["ends_at"], t["effective_end"]) == (NOW, NOW + 7 * DAY, NOW + 7 * DAY)
    assert (t["list_usd"], t["usd"], t["discount_id"], t["currency"], t["rate"], t["amount"]) == (8, 8, None, "EUR", 0.92, 7.5)
    assert (t["user_id"], t["user_name"], t["granted_by"], t["account_id"]) == (ids["alice"], "alice", ids["admin"], 1)
    row = conn.execute("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == ("ticket_grant", "alice")
    assert tickets.is_gated(conn, ids["alice"]) and not tickets.is_gated(conn, ids["admin"])


def test_grant_charges_the_discount_and_keeps_it_fixed(db):
    conn, cfg, ids = seeded(db)
    d = tickets.create_discount(conn, cfg, "lite", "week", 6, NOW, NOW + DAY, ids["admin"], now=NOW)
    t = grant(conn, cfg, ids, currency="USD")
    assert (t["list_usd"], t["usd"], t["discount_id"], t["amount"]) == (8, 6, d["id"], 6)
    tickets.set_price(conn, cfg, "lite", "week", 50, ids["admin"], now=NOW)
    tickets.set_rate(conn, cfg, "EUR", 2.0, ids["admin"], now=NOW)
    assert tickets.get(conn, t["id"])["usd"] == 6


def test_grant_needs_a_rate_and_confirms_a_stale_one(db):
    conn, cfg, ids = seeded(db)
    conn.execute("DELETE FROM fx_rates")
    with pytest.raises(tickets.TicketError, match="No exchange rate for EUR"):
        grant(conn, cfg, ids)
    tickets.set_rate(conn, cfg, "EUR", 0.9, ids["admin"], now=NOW - 40 * 3600)
    with pytest.raises(tickets.TicketError, match="40 hours old"):
        grant(conn, cfg, ids)
    assert grant(conn, cfg, ids, confirm_stale_rate=True)["rate"] == 0.9
    with pytest.raises(tickets.TicketError, match="Unknown currency"):
        grant(conn, cfg, ids, currency="GBP")


def test_grant_refused_for_revoked_disabled_or_when_disabled_feature(db):
    conn, cfg, ids = seeded(db)
    dbm.set_enabled(conn, ids["alice"], False)
    with pytest.raises(tickets.TicketError, match="disabled"):
        grant(conn, cfg, ids)
    dbm.set_enabled(conn, ids["alice"], True)
    dbm.revoke(conn, ids["alice"])
    with pytest.raises(tickets.TicketError, match="revoked"):
        grant(conn, cfg, ids)
    cfg.tickets.enabled = False
    with pytest.raises(tickets.TicketError, match="not enabled"):
        tickets.grant(conn, cfg, ids["admin"], user(conn, ids["admin"]), "lite", "day", "USD", now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_first_grant_removes_the_credit_and_ticked_rows_with_audit(db):
    conn, cfg, ids = seeded(db)
    for kind, scope, value, unit in [("cost_total", "*", "5", "usd"), ("tokens_daily", "*", "1000", "weighted"),
                                     ("requests_minute", "*", "20", "count"), ("share_7d", "*", "10", "pct"), ("allowed_models", "*", "claude-*", "list")]:
        conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (ids["alice"], kind, scope, value, unit))
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "lite", "week", "EUR", NOW)
    assert [(r["kind"], r["scope"]) for r in p["limit_rows"]] == [("allowed_models", "*"), ("requests_minute", "*"), ("tokens_daily", "*")]
    assert p["credit"] is True and p["first_ticket"] is True
    grant(conn, cfg, ids, remove_limits=[("tokens_daily", "*"), ("share_7d", "*")])   # share rows are not removable: ignored
    kinds = {r[0] for r in conn.execute("SELECT kind FROM limits WHERE user_id=?", (ids["alice"],))}
    assert kinds == {"requests_minute", "share_7d", "allowed_models"}
    actions = [tuple(r) for r in conn.execute("SELECT action, target FROM audit_log WHERE action IN ('credit_removed','limit_clear','ticket_grant') ORDER BY id")]
    assert actions == [("credit_removed", "alice"), ("limit_clear", "alice:tokens_daily:*"), ("ticket_grant", "alice")]


def test_second_ticket_queues_after_the_first_and_a_returning_user_starts_now(db):
    conn, cfg, ids = seeded(db)
    first = grant(conn, cfg, ids, length="day")
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "lite", "day", "EUR", NOW + 100)
    assert (p["starts_at"], p["queued"]) == (first["ends_at"], True)
    second = grant(conn, cfg, ids, length="day", now=NOW + 100)
    assert (second["starts_at"], second["ends_at"]) == (NOW + DAY, NOW + 2 * DAY)
    assert tickets.covering(conn, ids["alice"], NOW + 100)["id"] == first["id"]
    assert tickets.next_queued(conn, ids["alice"], NOW + 100)["id"] == second["id"]
    assert tickets.covering(conn, ids["alice"], NOW + DAY)["id"] == second["id"]   # half-open handover at the shared instant
    # Long after both ended, a new ticket starts now, not at the old end.
    # the EUR rate from seeded() is days old by now
    third = grant(conn, cfg, ids, length="day", now=NOW + 30 * DAY, confirm_stale_rate=True)
    assert third["starts_at"] == NOW + 30 * DAY
    assert tickets.last_ended(conn, ids["alice"], NOW + 29 * DAY)["ended_at"] == NOW + 2 * DAY


def test_sold_at_counts_tickets_and_bonus_shares_over_half_open_periods(db):
    conn, cfg, ids = seeded(db)
    bob = add_user(conn, ids, "bob")
    a = grant(conn, cfg, ids, tier="standard", length="day")                       # 25%, [NOW, NOW+1d)
    grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW + DAY)  # 25%, [NOW+1d, NOW+2d)
    conn.execute("INSERT INTO ticket_bonuses(ticket_id, share_pct, extra_days, starts_at, ends_at, granted_at) VALUES(?,?,?,?,?,?)",
                 (a["id"], 10, 0, NOW + 3600, NOW + 7200, NOW))
    assert tickets.sold_at(conn, 1, NOW) == 25
    assert tickets.sold_at(conn, 1, NOW + 3600) == 35
    assert tickets.sold_at(conn, 1, NOW + 7200) == 25             # bonus over at its end
    assert tickets.sold_at(conn, 1, NOW + DAY) == 25              # a's end and bob's start touch: never 50
    assert tickets.sold_at(conn, 1, NOW + DAY, exclude=a["id"]) == 25
    assert tickets.sold_at(conn, 1, NOW - 1) == 0


def test_capacity_check_samples_every_start_inside_the_period(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 50
    bob, carol = add_user(conn, ids, "bob"), add_user(conn, ids, "carol")
    # the EUR rate from seeded() is days old by now
    grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW + 3 * DAY, confirm_stale_rate=True)     # 25% on day 3 only
    tickets.check_capacity(conn, cfg, 1, 25, NOW, NOW + 7 * DAY)                             # day 3 would be 50: fits
    grant(conn, cfg, ids, tier="standard", length="week")                                    # alice: day 3 is now 50
    assert tickets.sold_out(conn, cfg, "standard", "week", NOW) is True                      # fine at the start, but day 3 would be 75
    assert tickets.sold_out(conn, cfg, "lite", "week", NOW) is True                          # day 3 would be 55
    assert tickets.sold_out(conn, cfg, "lite", "day", NOW) is False                          # over before day 3: 25 + 5
    with pytest.raises(tickets.CapacityError, match="50% is sold"):
        grant(conn, cfg, ids, who="carol", tier="standard", length="week")
    assert tickets.sold_out(conn, cfg, "lite", "day", NOW + 8 * DAY) is False


def test_max_sold_pct_refuses_what_100_would_allow_and_lowering_it_keeps_existing_tickets(db):
    conn, cfg, ids = seeded(db)
    for n in range(3):
        add_user(conn, ids, f"u{n}")
        grant(conn, cfg, ids, who=f"u{n}", tier="standard", length="week")                   # 3 × 25 = 75%
    with pytest.raises(tickets.CapacityError):                                               # 75 + 25 = 100 > 80
        grant(conn, cfg, ids, tier="standard", length="week")
    assert grant(conn, cfg, ids, tier="lite", length="week")["share_pct"] == 5             # 80: exactly the ceiling fits
    cfg.tickets.max_sold_pct = 50
    bob = add_user(conn, ids, "bob")
    with pytest.raises(tickets.CapacityError):
        grant(conn, cfg, ids, who="bob", tier="lite", length="day")
    assert conn.execute("SELECT COUNT(*) FROM tickets WHERE cancelled_at IS NULL").fetchone()[0] == 4   # nothing cancelled
    assert tickets.capacity(conn, cfg, NOW) == {"sold_now_pct": 80, "peak_30d_pct": 80, "max_sold_pct": 50}


def test_capacity_panel_reports_sold_now_and_the_peak_over_30_days(db):
    conn, cfg, ids = seeded(db)
    bob = add_user(conn, ids, "bob")
    grant(conn, cfg, ids, tier="lite", length="month")
    # the EUR rate from seeded() is days old by now
    grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW + 10 * DAY, confirm_stale_rate=True)
    assert tickets.capacity(conn, cfg, NOW) == {"sold_now_pct": 5, "peak_30d_pct": 30, "max_sold_pct": 80}
    assert tickets.capacity(conn, cfg, NOW + 31 * DAY)["sold_now_pct"] == 0   # expired tickets stop counting by themselves


def test_two_concurrent_grants_cannot_oversell(db):
    path, conn = db
    _, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 30
    bob = add_user(conn, ids, "bob")
    conns = [dbm.get_conn(path) for _ in range(2)]
    start = threading.Barrier(2)
    results = []

    def go(c, who):
        start.wait()
        try:
            tickets.grant(c, cfg, ids["admin"], user(c, ids[who]), "standard", "week", "USD", now=NOW)
            results.append("ok")
        except tickets.CapacityError:
            results.append("full")

    threads = [threading.Thread(target=go, args=(c, who)) for c, who in zip(conns, ("alice", "bob"))]
    for t in threads: t.start()
    for t in threads: t.join()
    for c in conns: c.close()
    assert sorted(results) == ["full", "ok"]
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


def test_current_day_steps_from_the_ticket_start(db):
    t = {"starts_at": NOW, "ends_at": NOW + 7 * DAY, "effective_end": NOW + 7 * DAY}
    assert tickets.current_day(t, NOW) == (NOW, NOW + DAY)
    assert tickets.current_day(t, NOW + DAY - 1) == (NOW, NOW + DAY)
    assert tickets.current_day(t, NOW + DAY) == (NOW + DAY, NOW + 2 * DAY)
    assert tickets.current_day(t, NOW + 6 * DAY + 5) == (NOW + 6 * DAY, NOW + 7 * DAY)
