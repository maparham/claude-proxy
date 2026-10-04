import logging
import sqlite3
import threading

import pytest

from claude_proxy import db as dbm
from claude_proxy import tickets
from tests.conftest import make_gateway
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


def bonus(conn, cfg, ids, tid, **kw):
    return tickets.add_bonus(conn, cfg, ids["admin"], tid, now=kw.pop("now", NOW), **kw)


def test_cancel_frees_the_slice_and_ends_bonuses(db):
    conn, cfg, ids = seeded(db)
    t = grant(conn, cfg, ids, tier="standard", length="week")
    bonus(conn, cfg, ids, t["id"], share_pct=5, starts_at=NOW, ends_at=NOW + DAY, note="welcome")
    assert tickets.sold_at(conn, 1, NOW + 10) == 30
    r = tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW + 100)
    assert r["ticket"]["cancelled_at"] == NOW + 100 and r["ticket"]["cancelled_by"] == ids["admin"]
    assert tickets.sold_at(conn, 1, NOW + 200) == 0
    assert tickets.bonuses(conn, t["id"])[0]["cancelled_at"] == NOW + 100
    assert tickets.covering(conn, ids["alice"], NOW + 200) is None
    assert tickets.is_gated(conn, ids["alice"])                      # a cancelled ticket still gates
    with pytest.raises(tickets.TicketError, match="already cancelled"):
        tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW + 300)
    assert conn.execute("SELECT action FROM audit_log WHERE action='ticket_cancel'").fetchone() is not None


def test_cancel_moves_queued_tickets_forward_to_close_the_gap(db):
    conn, cfg, ids = seeded(db)
    a = grant(conn, cfg, ids, length="week")                                  # [0, 7d)
    b = grant(conn, cfg, ids, length="week", now=NOW + 10)                    # [7d, 14d)
    c = grant(conn, cfg, ids, length="day", now=NOW + 20)                     # [14d, 15d)
    bonus(conn, cfg, ids, b["id"], share_pct=1, starts_at=NOW + 8 * DAY, ends_at=NOW + 9 * DAY)
    r = tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW + DAY)       # active: the next one starts now
    assert (r["moved"], r["dates_kept"]) == (2, False)
    b2, c2 = tickets.get(conn, b["id"]), tickets.get(conn, c["id"])
    assert (b2["starts_at"], b2["ends_at"]) == (NOW + DAY, NOW + 8 * DAY)
    assert (c2["starts_at"], c2["ends_at"]) == (NOW + 8 * DAY, NOW + 9 * DAY)
    bb = tickets.bonuses(conn, b["id"])[0]
    assert (bb["starts_at"], bb["ends_at"]) == (NOW + 2 * DAY, NOW + 3 * DAY)   # bonus period moved with its ticket
    assert tickets.covering(conn, ids["alice"], NOW + DAY)["id"] == b["id"]


def test_cancelling_a_queued_ticket_moves_the_rest_to_its_start(db):
    conn, cfg, ids = seeded(db)
    grant(conn, cfg, ids, length="day")                                        # [0, 1d)
    b = grant(conn, cfg, ids, length="day", now=NOW + 10)                      # [1d, 2d)
    c = grant(conn, cfg, ids, length="day", now=NOW + 20)                      # [2d, 3d)
    tickets.cancel(conn, cfg, ids["admin"], b["id"], now=NOW + 100)
    assert tickets.get(conn, c["id"])["starts_at"] == NOW + DAY


def test_cancel_never_refused_dates_kept_when_a_move_does_not_fit(db):
    conn, cfg, ids = seeded(db)
    bob, carol = add_user(conn, ids, "bob"), add_user(conn, ids, "carol")
    a = grant(conn, cfg, ids, tier="standard", length="day")                   # alice 25% day 0
    b = grant(conn, cfg, ids, tier="standard", length="day", now=NOW + 10)     # alice 25% day 1
    grant(conn, cfg, ids, who="bob", tier="standard", length="day")            # bob 25% day 0
    grant(conn, cfg, ids, who="carol", tier="standard", length="day")          # carol 25% day 0: day 0 = 75 of 80
    cfg.tickets.max_sold_pct = 50                                              # the admin lowers the ceiling: day 0 is now over it
    r = tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW + 100)        # b cannot move onto day 0 (50 + 25 > 50)
    assert (r["moved"], r["dates_kept"]) == (0, True) and "Not enough capacity" in r["reason"]
    assert tickets.get(conn, b["id"])["starts_at"] == NOW + DAY                # b kept its date
    assert tickets.get(conn, a["id"])["cancelled_at"] == NOW + 100             # but a is cancelled all the same


def test_bonus_validation(db):
    conn, cfg, ids = seeded(db)
    t = grant(conn, cfg, ids)
    for kw in ({}, {"share_pct": 0, "extra_days": 0}, {"share_pct": -1}, {"extra_days": -1}):
        with pytest.raises(tickets.TicketError):
            bonus(conn, cfg, ids, t["id"], **kw)
    with pytest.raises(tickets.TicketError, match="within the ticket"):
        bonus(conn, cfg, ids, t["id"], share_pct=1, starts_at=NOW + 8 * DAY, ends_at=NOW + 9 * DAY)   # after the ticket
    tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW)
    with pytest.raises(tickets.TicketError, match="cancelled"):
        bonus(conn, cfg, ids, t["id"], extra_days=1)


def test_bonus_share_is_clamped_checked_and_counted(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 30
    t = grant(conn, cfg, ids, tier="standard", length="week")                  # 25%
    r = bonus(conn, cfg, ids, t["id"], share_pct=5, starts_at=NOW - DAY, ends_at=NOW + 30 * DAY, note="thanks")
    assert (r["bonus"]["starts_at"], r["bonus"]["ends_at"], r["bonus"]["note"]) == (NOW, NOW + 7 * DAY, "thanks")
    assert tickets.bonus_share(conn, t["id"], NOW + DAY) == 5 and tickets.bonus_share(conn, t["id"], NOW + 7 * DAY) == 0
    with pytest.raises(tickets.CapacityError):
        bonus(conn, cfg, ids, t["id"], share_pct=1)                           # 25 + 5 + 1 > 30
    assert conn.execute("SELECT action, target FROM audit_log WHERE action='ticket_bonus'").fetchone()[:] == ("ticket_bonus", "alice")


def test_bonus_days_extend_at_the_ticket_share_and_push_queued_tickets(db):
    conn, cfg, ids = seeded(db)
    a = grant(conn, cfg, ids, length="week")                                   # [0, 7d)
    b = grant(conn, cfg, ids, length="day", now=NOW + 10)                      # [7d, 8d)
    c = grant(conn, cfg, ids, length="day", now=NOW + 20)                      # [8d, 9d)
    r = bonus(conn, cfg, ids, a["id"], extra_days=2, note="sorry for the outage")
    assert r["moved"] == 2 and r["ticket"]["effective_end"] == NOW + 9 * DAY and r["ticket"]["ends_at"] == NOW + 7 * DAY
    assert (r["bonus"]["share_pct"], r["bonus"]["extra_days"], r["bonus"]["starts_at"], r["bonus"]["ends_at"]) == (0, 2, NOW + 7 * DAY, NOW + 9 * DAY)
    assert tickets.get(conn, b["id"])["starts_at"] == NOW + 9 * DAY
    assert tickets.get(conn, c["id"])["starts_at"] == NOW + 10 * DAY
    assert tickets.sold_at(conn, 1, NOW + 8 * DAY) == 5                        # the extension counts at the ticket share
    assert tickets.covering(conn, ids["alice"], NOW + 8 * DAY)["id"] == a["id"]
    assert tickets.current_day(tickets.get(conn, a["id"]), NOW + 8 * DAY + 5) == (NOW + 8 * DAY, NOW + 9 * DAY)   # same boundaries


def test_bonus_days_refused_when_a_moved_ticket_does_not_fit(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 50
    bob, carol = add_user(conn, ids, "bob"), add_user(conn, ids, "carol")
    a = grant(conn, cfg, ids, tier="standard", length="day")                   # alice day 0
    b = grant(conn, cfg, ids, tier="standard", length="day", now=NOW + 10)     # alice day 1
    # the EUR rate from seeded() is days old by now
    grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW + 2 * DAY, confirm_stale_rate=True)     # bob day 2
    grant(conn, cfg, ids, who="carol", tier="standard", length="day", now=NOW + 2 * DAY, confirm_stale_rate=True)   # carol day 2: day 2 = 50
    # An extra day on a extends it over day 1, so b must move onto day 2, which is full.
    with pytest.raises(tickets.CapacityError, match="queued ticket"):
        bonus(conn, cfg, ids, a["id"], extra_days=1)
    assert tickets.get(conn, a["id"])["effective_end"] == NOW + DAY           # nothing applied
    assert tickets.get(conn, b["id"])["starts_at"] == NOW + DAY
    assert conn.execute("SELECT COUNT(*) FROM ticket_bonuses").fetchone()[0] == 0


def test_bonus_days_on_an_ended_ticket_make_it_cover_now_with_the_old_day_boundaries(db):
    conn, cfg, ids = seeded(db)
    a = grant(conn, cfg, ids, length="day")                                    # [0, 1d)
    later = NOW + DAY + 3600                                                   # an hour after it ended
    assert tickets.covering(conn, ids["alice"], later) is None
    bonus(conn, cfg, ids, a["id"], extra_days=1, now=later)
    t = tickets.covering(conn, ids["alice"], later)
    assert t["id"] == a["id"] and t["effective_end"] == NOW + 2 * DAY
    assert tickets.current_day(t, later) == (NOW + DAY, NOW + 2 * DAY)


def test_bonus_rejects_non_finite_share_pct(db):
    conn, cfg, ids = seeded(db)
    t = grant(conn, cfg, ids)
    with pytest.raises(tickets.TicketError, match="extra share above 0"):
        bonus(conn, cfg, ids, t["id"], share_pct=float("nan"))
    with pytest.raises(tickets.TicketError, match="extra share above 0"):
        bonus(conn, cfg, ids, t["id"], share_pct=float("inf"))
    assert conn.execute("SELECT COUNT(*) FROM ticket_bonuses").fetchone()[0] == 0


def test_bonus_bounds_extra_days_to_365(db):
    conn, cfg, ids = seeded(db)
    t = grant(conn, cfg, ids)
    with pytest.raises(tickets.TicketError, match="365 or fewer"):
        bonus(conn, cfg, ids, t["id"], extra_days=366)
    assert bonus(conn, cfg, ids, t["id"], extra_days=365)["bonus"]["extra_days"] == 365


def test_bonus_days_refused_when_the_next_ticket_has_already_started(db):
    conn, cfg, ids = seeded(db)
    a = grant(conn, cfg, ids, length="day")                                    # [0, 1d)
    b = grant(conn, cfg, ids, length="week", now=NOW + 10)                     # queued: [1d, 8d)
    now = NOW + DAY + 3600                                                      # b has been active for an hour
    assert tickets.covering(conn, ids["alice"], now)["id"] == b["id"]
    with pytest.raises(tickets.TicketError, match="already started"):
        bonus(conn, cfg, ids, a["id"], extra_days=2, now=now)
    assert tickets.get(conn, a["id"])["effective_end"] == NOW + DAY             # nothing applied
    assert tickets.get(conn, b["id"])["starts_at"] == NOW + DAY                 # b's start is untouched
    assert conn.execute("SELECT COUNT(*) FROM ticket_bonuses").fetchone()[0] == 0


def test_cancel_savepoint_undoes_the_first_move_when_the_second_in_the_chain_fails(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 60
    bob, carol = add_user(conn, ids, "bob"), add_user(conn, ids, "carol")
    a = grant(conn, cfg, ids, tier="lite", length="week")                              # alice 5%, [0, 7d)
    b = grant(conn, cfg, ids, tier="standard", length="day", now=NOW + 10)             # alice 25%, [7d, 8d)
    c = grant(conn, cfg, ids, tier="standard", length="day", now=NOW + 20)             # alice 25%, [8d, 9d)
    grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW + DAY)     # bob 25%, [1d, 2d)
    grant(conn, cfg, ids, who="carol", tier="standard", length="day", now=NOW + DAY)   # carol 25%, [1d, 2d)
    # Cancelling a (active) tries to shift the chain [b, c] back by 7 days: b -> [0,1d) fits; c -> [1d,2d)
    # lands on bob+carol's 50%, which is where the already-applied move to b must be undone.
    r = tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW)
    assert (r["moved"], r["dates_kept"]) == (0, True)
    assert "Not enough capacity" in r["reason"]
    assert tickets.get(conn, b["id"])["starts_at"] == NOW + 7 * DAY             # kept, not moved to day 0
    assert tickets.get(conn, c["id"])["starts_at"] == NOW + 8 * DAY             # kept too
    assert tickets.get(conn, a["id"])["cancelled_at"] == NOW                    # but a is cancelled all the same


def test_ungate_only_without_live_tickets(db):
    conn, cfg, ids = seeded(db)
    a = grant(conn, cfg, ids, length="day")
    with pytest.raises(tickets.TicketError, match="active or queued"):
        tickets.ungate(conn, ids["admin"], ids["alice"], now=NOW + 100)
    tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW + 100)
    b = grant(conn, cfg, ids, length="day", now=NOW + 200)
    conn.execute("UPDATE tickets SET starts_at=?, ends_at=? WHERE id=?", (NOW + 5 * DAY, NOW + 6 * DAY, b["id"]))   # queued
    with pytest.raises(tickets.TicketError, match="active or queued"):
        tickets.ungate(conn, ids["admin"], ids["alice"], now=NOW + 300)
    assert tickets.ungate(conn, ids["admin"], ids["alice"], now=NOW + 7 * DAY) == 2
    assert tickets.is_gated(conn, ids["alice"]) is False
    assert conn.execute("SELECT COUNT(*) FROM tickets WHERE user_id=?", (ids["alice"],)).fetchone()[0] == 2   # records stay
    assert conn.execute("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[:] == ("ungate", "alice")
    with pytest.raises(tickets.TicketError, match="no tickets"):
        tickets.ungate(conn, ids["admin"], ids["admin"], now=NOW)


def test_user_state_summarises_gating_and_live_tickets(db):
    conn, cfg, ids = seeded(db)
    assert tickets.user_state(conn, ids["alice"], NOW) == {"gated": False, "current": None, "queued": None, "live": False, "has_tickets": False}
    a = grant(conn, cfg, ids, length="day")
    s = tickets.user_state(conn, ids["alice"], NOW + 10)
    assert s["gated"] and s["live"] and s["current"]["id"] == a["id"] and s["queued"] is None
    s = tickets.user_state(conn, ids["alice"], NOW + 2 * DAY)
    assert s == {"gated": True, "current": None, "queued": None, "live": False, "has_tickets": True}


def test_gateway_warns_when_tickets_are_off_but_users_are_still_gated(db, caplog):
    conn, cfg, ids = seeded(db)
    grant(conn, cfg, ids)                 # alice is now ticket-gated
    cfg.tickets.enabled = False
    with caplog.at_level(logging.WARNING, logger="claude_proxy"):
        make_gateway(cfg, conn)
    assert "tickets are off" in caplog.text and "1 users are ticket-gated" in caplog.text


def test_cancel_does_not_move_two_overlapping_bonuses_onto_a_crowded_day(db):
    """Each bonus fits on its own at the new place; together, where they overlap, they pass the ceiling."""
    conn, cfg, ids = seeded(db)
    a = grant(conn, cfg, ids, tier="lite", length="week")                       # alice 5%, [0, 7d)
    b = grant(conn, cfg, ids, tier="standard", length="week", now=NOW + 10)     # alice 25%, queued [7d, 14d)
    bonus(conn, cfg, ids, b["id"], share_pct=10, starts_at=NOW + 8 * DAY, ends_at=NOW + 10 * DAY)
    bonus(conn, cfg, ids, b["id"], share_pct=10, starts_at=NOW + 9 * DAY, ends_at=NOW + 11 * DAY)   # 45% on [9d, 10d)
    add_user(conn, ids, "bob")
    grant(conn, cfg, ids, who="bob", tier="standard", length="week")
    for name in ("c1", "c2", "c3"):
        add_user(conn, ids, name)
        grant(conn, cfg, ids, who=name, tier="lite", length="week")             # others: 25 + 3 x 5 = 40% on [0, 7d)
    r = tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW)               # b would land on [0, 7d): 40 + 25 + 20 = 85 > 80
    assert (r["moved"], r["dates_kept"]) == (0, True) and "Not enough capacity" in r["reason"]
    assert tickets.get(conn, b["id"])["starts_at"] == NOW + 7 * DAY
    assert [x["starts_at"] for x in tickets.bonuses(conn, b["id"])] == [NOW + 8 * DAY, NOW + 9 * DAY]
    assert max(tickets.sold_at(conn, 1, NOW + k * DAY + 1) for k in range(14)) <= 80


def test_bonus_with_share_and_days_is_checked_as_written(db):
    """Extra share reaching into the extra days counts the ticket's own share there too."""
    conn, cfg, ids = seeded(db)
    a = grant(conn, cfg, ids, tier="standard", length="week")                   # alice 25%, [0, 7d)
    for name in ("bob", "carol"):
        add_user(conn, ids, name)
        grant(conn, cfg, ids, who=name, tier="standard", length="day", now=NOW + 7 * DAY, confirm_stale_rate=True)   # 50% on [7d, 8d)
    with pytest.raises(tickets.CapacityError):                                  # [7d, 8d): 50 + 25 + 10 = 85 > 80
        bonus(conn, cfg, ids, a["id"], share_pct=10, extra_days=1, starts_at=NOW + 6 * DAY)
    assert tickets.get(conn, a["id"])["effective_end"] == NOW + 7 * DAY        # nothing applied
    assert conn.execute("SELECT COUNT(*) FROM ticket_bonuses").fetchone()[0] == 0
    bonus(conn, cfg, ids, a["id"], share_pct=5, extra_days=1, starts_at=NOW + 6 * DAY)   # 80 exactly: fits


def test_only_the_first_ticket_removes_a_credit(db):
    conn, cfg, ids = seeded(db)
    t = grant(conn, cfg, ids, length="day")
    tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW + 10)
    tickets.ungate(conn, ids["admin"], ids["alice"], now=NOW + 20)
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,'cost_total','*','3','usd',0)", (ids["alice"],))
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "lite", "day", "EUR", NOW + 30)
    assert p["first_ticket"] is False and p["credit"] is False        # a credit the admin set later is not a sign-up credit
    grant(conn, cfg, ids, length="day", now=NOW + 30)
    assert conn.execute("SELECT 1 FROM limits WHERE user_id=? AND kind='cost_total'", (ids["alice"],)).fetchone() is not None


def test_bonus_days_on_an_ended_ticket_are_checked_from_now_not_from_the_past(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 30
    add_user(conn, ids, "bob")
    a = grant(conn, cfg, ids, length="day")                                    # alice lite [0, 1d)
    # bob's standard day covered alice's old end, and has ended by now: only the past is full.
    grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW + DAY - 7200, confirm_stale_rate=True)
    cfg.tickets.max_sold_pct = 29                                              # 25 + 5 at alice's old end would not fit
    now = NOW + 2 * DAY - 3600
    r = bonus(conn, cfg, ids, a["id"], extra_days=1, now=now)
    assert r["ticket"]["effective_end"] == NOW + 2 * DAY
    assert tickets.covering(conn, ids["alice"], now)["id"] == a["id"]


def test_bonus_days_allowed_when_they_stop_short_of_a_started_later_ticket(db):
    conn, cfg, ids = seeded(db)
    a = grant(conn, cfg, ids, length="day")                                    # [0, 1d)
    b = grant(conn, cfg, ids, length="week", now=NOW + 3 * DAY, confirm_stale_rate=True)   # [3d, 10d), a gap after a
    now = NOW + 3 * DAY + 3600                                                  # b has started
    r = bonus(conn, cfg, ids, a["id"], extra_days=1, now=now)                  # [1d, 2d): clear of b
    assert r["moved"] == 0 and r["ticket"]["effective_end"] == NOW + 2 * DAY
    assert tickets.get(conn, b["id"])["starts_at"] == NOW + 3 * DAY
    with pytest.raises(tickets.TicketError, match="already started"):
        bonus(conn, cfg, ids, a["id"], extra_days=2, now=now)                  # [2d, 4d) would run into b
