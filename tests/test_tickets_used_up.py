"""Tickets bought to start now run beside the running one, limits added up; used-up tickets give way to a new one at
once; only a ticket not used yet, running on its own, can be upgraded."""
import pytest

from claude_proxy import limits, orders, tickets
from claude_proxy.db import create_user
from tests.test_tickets_limits import check, fresh, req
from tests.tickets_helpers import DAY, NOW, seeded, user


def day(conn, cfg, ids, now, tier="lite"):
    return tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), tier, "day", "USD", now=now, terms_version="v1")


@pytest.fixture
def env(db):
    """Alice on a Lite day ticket from NOW - 300, the 7d rate known: 1000 tokens a point."""
    conn, cfg, ids = seeded(db)
    fresh(conn, NOW - 600)
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)
    return conn, cfg, ids, day(conn, cfg, ids, NOW - 300)


def use_up(conn, ids):
    req(conn, ids["alice"], NOW - 200, i=2000)   # 2 points: past a day ticket's 5/7


def test_a_used_up_ticket_hands_over_to_the_queued_one(env):
    conn, cfg, ids, t = env
    nxt = day(conn, cfg, ids, NOW - 250, tier="standard")   # bought before the first was used up: queued after it
    assert nxt["starts_at"] == t["ends_at"]
    use_up(conn, ids)
    assert limits.used_up(conn, cfg, ids["alice"], NOW - 100)
    assert check(conn, cfg, ids["alice"], now=NOW - 100) is None
    old, new = tickets.get(conn, t["id"]), tickets.get(conn, nxt["id"])
    assert (old["used_up_at"], old["effective_end"]) == (NOW - 100, NOW - 100)
    assert (new["starts_at"], new["effective_end"]) == (NOW - 100, NOW - 100 + DAY)   # the whole day, from now
    assert tickets.covering(conn, ids["alice"], NOW - 50)["id"] == nxt["id"]
    assert conn.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0] == "ticket_used_up"


def test_a_ticket_bought_while_the_last_is_used_up_starts_now(env):
    conn, cfg, ids, t = env
    use_up(conn, ids)
    assert check(conn, cfg, ids["alice"], now=NOW - 150).kind == "share_7d"   # nothing queued: nothing to hand over
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "lite", "day", "USD", NOW - 100)
    assert (p["starts_at"], p["queued"], p["replaces"], p["available"]) == (NOW - 100, False, [t["id"]], True)
    nxt = day(conn, cfg, ids, NOW - 100)
    assert nxt["starts_at"] == NOW - 100 and tickets.get(conn, t["id"])["used_up_at"] == NOW - 100
    assert check(conn, cfg, ids["alice"], now=NOW - 50) is None
    with pytest.raises(tickets.TicketError, match="used up"):
        tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=2, now=NOW - 50)


def test_a_ticket_with_time_and_use_left_queues_as_before(env):
    conn, cfg, ids, t = env
    assert not limits.used_up(conn, cfg, ids["alice"], NOW - 100)
    assert day(conn, cfg, ids, NOW - 100)["starts_at"] == t["ends_at"]


def test_a_week_piece_used_up_before_the_reset_is_not_the_ticket_used_up(db):
    """A month ticket's weekly piece refills at the reset: it waits, it is not replaced."""
    conn, cfg, ids = seeded(db)
    cfg.tickets.borrow_margin_pct = 100   # no borrowing from the next week: its own piece is used up
    fresh(conn, NOW - 600)
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "month", "USD", now=NOW - 300, terms_version="v1")
    req(conn, ids["alice"], NOW - 200, i=5000)
    assert check(conn, cfg, ids["alice"], now=NOW - 100).kind == "share_7d"
    assert not limits.used_up(conn, cfg, ids["alice"], NOW - 100)


def test_only_an_unused_ticket_can_be_upgraded(env):
    conn, cfg, ids, t = env
    t = tickets.get(conn, t["id"])
    req(conn, ids["alice"], NOW - 250, path="/v1/messages/count_tokens")   # counting tokens is not using the ticket
    assert tickets.upgrade_quote(conn, cfg, t, "standard", "USD", NOW - 200)["tier"] == "standard"
    req(conn, ids["alice"], NOW - 150, i=10)
    with pytest.raises(tickets.TicketError, match="has been used"):
        tickets.upgrade_quote(conn, cfg, t, "standard", "USD", NOW - 100)


def test_a_pass_started_now_runs_beside_the_first_with_limits_of_its_own(env):
    """As if another user held it: the second pass counts only what it served, so it starts with all of its limits."""
    conn, cfg, ids, t = env
    use_up(conn, ids)
    req(conn, ids["alice"], NOW - 190, i=0)
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "standard", "week", "USD", NOW - 100, start_now=True)
    assert (p["starts_at"], p["replaces"]) == (NOW - 100, [])
    t2 = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "standard", "week", "USD", now=NOW - 100,
                       terms_version="v1", start_now=True)
    assert t2["starts_at"] == NOW - 100 and tickets.get(conn, t["id"])["used_up_at"] is None   # both run
    assert [x["id"] for x in tickets.running(conn, ids["alice"], NOW - 50)] == [t["id"], t2["id"]]
    served, sts = limits.serving(conn, cfg, ids["alice"], NOW - 50)
    assert served["id"] == t2["id"]   # the first is at its limit: requests spill over to the second
    st = {s.kind: s for s in sts}
    assert st["share_5h"].value == "25" and st["share_5h"].tier == "Standard" and st["share_5h"].current == 0
    week_end = tickets.week_bounds(conn, cfg, NOW - 50)[1]
    assert st["share_7d"].limit == pytest.approx(25 * (min(t2["ends_at"], week_end) - (NOW - 100)) / DAY / 7)
    assert st["share_7d"].current == 0
    out = {}
    assert limits.evaluate(conn, cfg, ids["alice"], "claude-sonnet-5", "/v1/messages", now=NOW - 50, served=out) is None
    assert out == {"ticket_id": t2["id"]}
    with pytest.raises(tickets.TicketError, match="on its own"):
        tickets.upgrade_quote(conn, cfg, tickets.get(conn, t["id"]), "standard", "USD", NOW - 50)


def test_the_oldest_pass_serves_first_unless_the_user_pins_another(env):
    conn, cfg, ids, t = env
    t2 = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "standard", "week", "USD", now=NOW - 100,
                       terms_version="v1", start_now=True)
    assert limits.serving(conn, cfg, ids["alice"], NOW - 50)[0]["id"] == t["id"]
    conn.execute("UPDATE users SET pass_pin=? WHERE id=?", (t2["id"], ids["alice"]))
    assert limits.serving(conn, cfg, ids["alice"], NOW - 50)[0]["id"] == t2["id"]
    req(conn, ids["alice"], NOW - 40, i=100000, ticket=t2["id"])   # the pinned one at its limit: back to the oldest
    assert limits.serving(conn, cfg, ids["alice"], NOW - 30)[0]["id"] == t["id"]


def test_an_order_to_start_now_is_granted_beside_the_running_ticket(env):
    conn, cfg, ids, t = env
    o = orders.create(conn, cfg, tier="lite", length="day", currency="USD", name="alice", email="a@example.com",
                      user_id=ids["alice"], notify=False, now=NOW - 100, terms_version="v1", start_now=True)
    assert o["start_now"] == 1
    g = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", order_id=o["id"], now=NOW - 90)
    assert g["starts_at"] == NOW - 90


def test_timeline_stacks_overlapping_tickets_relative_to_the_tallest(env):
    conn, cfg, ids, t = env
    assert tickets.timeline(conn, cfg, ids["alice"], NOW) is None   # one ticket: no chart
    t2 = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "standard", "week", "USD", now=NOW - 100,
                       terms_version="v1", start_now=True)
    t3 = day(conn, cfg, ids, NOW - 50)   # queued after the last
    tl = tickets.timeline(conn, cfg, ids["alice"], NOW)
    assert [p["tier"] for p in tl["passes"]] == ["lite", "standard", "lite"]
    assert [(s["from"], s["to"], [b["i"] for b in s["stack"]]) for s in tl["segments"]] == [
        (NOW, t["ends_at"], [0, 1]), (t["ends_at"], t2["ends_at"], [1]), (t2["ends_at"], t3["ends_at"], [2])]
    assert [b["w"] for b in tl["segments"][0]["stack"]] == pytest.approx([5 / 30, 25 / 30])
    assert tl["plan"] is None and tl["segments"][0]["plan_x"] is None   # no compare plan set


def test_cancelling_a_pass_beside_another_keeps_the_queue_behind_the_other(db):
    conn, cfg, ids = seeded(db)
    g = lambda length, now, **kw: tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", length, "EUR",  # noqa: E731
                                                now=now, terms_version="v1", confirm_stale_rate=True, **kw)
    a, q = g("week", NOW), g("day", NOW + 100)   # q queued after a
    b, c = g("day", NOW + 200, start_now=True), g("day", NOW + 300, start_now=True)
    tickets.cancel(conn, cfg, ids["admin"], b["id"], now=NOW + 400)   # a still runs: q stays behind it
    assert tickets.get(conn, q["id"])["starts_at"] == a["ends_at"]
    tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW + 500)   # c still runs: q closes up to c's end, not to now
    assert tickets.get(conn, q["id"])["starts_at"] == c["ends_at"]


def test_a_used_up_pool_hands_over_to_a_pass_queued_before_a_side_pass_ends(env):
    conn, cfg, ids, t = env
    q = day(conn, cfg, ids, NOW - 250)
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW - 240,
                  terms_version="v1", start_now=True)   # ends after q starts
    side = tickets.running(conn, ids["alice"], NOW - 200)[-1]
    req(conn, ids["alice"], NOW - 200, i=4000)
    assert not limits.used_up(conn, cfg, ids["alice"], NOW - 100)   # the side pass has its own limits left
    req(conn, ids["alice"], NOW - 150, i=4000, ticket=side["id"])
    assert limits.used_up(conn, cfg, ids["alice"], NOW - 100)
    assert limits.hand_over(conn, cfg, ids["alice"], NOW - 100)
    assert tickets.get(conn, q["id"])["starts_at"] == NOW - 100


def test_replacing_a_used_up_pass_at_the_ceiling_frees_its_share_and_cuts_its_bonus(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 30
    fresh(conn, NOW - 600)
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)
    t = day(conn, cfg, ids, NOW - 300, tier="standard")                                     # 25%
    b = tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=5, now=NOW - 250)["bonus"]   # 30%: the ceiling
    req(conn, ids["alice"], NOW - 200, i=5000)                                               # 5 points: past ~4.3
    assert limits.used_up(conn, cfg, ids["alice"], NOW - 100)
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "standard", "day", "USD", NOW - 100)
    assert (p["replaces"], p["available"]) == ([t["id"]], True)
    nxt = day(conn, cfg, ids, NOW - 100, tier="standard")
    assert nxt["starts_at"] == NOW - 100
    assert tickets.sold_at(conn, 1, NOW - 100) == 25
    assert conn.execute("SELECT ends_at FROM ticket_bonuses WHERE id=?", (b["id"],)).fetchone()[0] == NOW - 100
    assert tickets.active_bonuses(conn, t["id"], NOW - 50) == []


def test_hand_over_changes_nothing_when_the_queued_pass_no_longer_fits(env):
    """Moved up to now, the queued pass's bonus would land beside bob's pass: over the ceiling."""
    conn, cfg, ids, t = env
    cfg.tickets.max_sold_pct = 30
    bob, _ = create_user(conn, "bob")
    tickets.grant(conn, cfg, ids["admin"], user(conn, bob), "standard", "day", "USD", now=NOW - 300, terms_version="v1")   # ends with t
    q = day(conn, cfg, ids, NOW - 250)
    tickets.add_bonus(conn, cfg, ids["admin"], q["id"], share_pct=20, starts_at=q["starts_at"], ends_at=q["starts_at"] + 3600,
                      now=NOW - 240)
    use_up(conn, ids)
    assert limits.used_up(conn, cfg, ids["alice"], NOW - 100)
    assert tickets.hand_over(conn, cfg, ids["alice"], NOW - 100) is False
    assert tickets.get(conn, t["id"])["used_up_at"] is None
    assert tickets.get(conn, q["id"])["starts_at"] == t["ends_at"]
    assert check(conn, cfg, ids["alice"], now=NOW - 100).kind == "share_7d"


def test_requests_spill_to_the_next_pass_and_come_back_when_the_first_refills_at_the_reset(db):
    from tests.test_tickets_limits import RESET7, week_snap
    conn, cfg, ids = seeded(db)
    fresh(conn, NOW - 600)
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "USD", now=NOW - 300, terms_version="v1")
    req(conn, ids["alice"], NOW - 200, i=3000)   # 3 points: past its ~2.1 piece of this week
    t2 = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "standard", "week", "USD", now=NOW - 100,
                       terms_version="v1", start_now=True)
    assert limits.serving(conn, cfg, ids["alice"], NOW - 50)[0]["id"] == t2["id"]
    week_snap(conn, RESET7 + 3600, 1)
    assert limits.serving(conn, cfg, ids["alice"], RESET7 + 3600)[0]["id"] == t["id"]
