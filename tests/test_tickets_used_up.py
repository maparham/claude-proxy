"""Tickets bought to start now run beside the running one, limits added up; used-up tickets give way to a new one at
once; only a ticket not used yet, running on its own, can be upgraded."""
import pytest

from claude_proxy import limits, orders, tickets
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


def test_a_ticket_started_now_runs_beside_the_first_and_the_limits_add_up(env):
    conn, cfg, ids, t = env
    use_up(conn, ids)
    req(conn, ids["alice"], NOW - 190, i=0)
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "standard", "week", "USD", NOW - 100, start_now=True)
    assert (p["starts_at"], p["replaces"]) == (NOW - 100, [])
    t2 = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "standard", "week", "USD", now=NOW - 100,
                       terms_version="v1", start_now=True)
    assert t2["starts_at"] == NOW - 100 and tickets.get(conn, t["id"])["used_up_at"] is None   # both run
    assert [x["id"] for x in tickets.running(conn, ids["alice"], NOW - 50)] == [t["id"], t2["id"]]
    st = {s.kind: s for s in limits.ticket_states(conn, cfg, ids["alice"], NOW - 50)}
    assert st["share_5h"].value == "30" and st["share_5h"].tier == "Lite + Standard"   # 5% + 25%
    w = t2["ends_at"]
    week_end = tickets.week_bounds(conn, cfg, NOW - 50)[1]
    pool = 5 * (t["ends_at"] - (NOW - 300)) / DAY / 7 + 25 * (min(w, week_end) - (NOW - 100)) / DAY / 7
    assert st["share_7d"].limit == pytest.approx(pool)
    assert check(conn, cfg, ids["alice"], now=NOW - 50) is None
    with pytest.raises(tickets.TicketError, match="on its own"):
        tickets.upgrade_quote(conn, cfg, tickets.get(conn, t["id"]), "standard", "USD", NOW - 50)


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
