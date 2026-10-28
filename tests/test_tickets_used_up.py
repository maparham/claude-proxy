"""A ticket used up before it ends gives way to a new one at once; only a ticket not used yet can be upgraded."""
import pytest

from claude_proxy import limits, tickets
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
    assert limits.used_up(conn, cfg, tickets.get(conn, t["id"]), NOW - 100)
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
    assert (p["starts_at"], p["queued"], p["replaces"], p["available"]) == (NOW - 100, False, t["id"], True)
    nxt = day(conn, cfg, ids, NOW - 100)
    assert nxt["starts_at"] == NOW - 100 and tickets.get(conn, t["id"])["used_up_at"] == NOW - 100
    assert check(conn, cfg, ids["alice"], now=NOW - 50) is None
    with pytest.raises(tickets.TicketError, match="used up"):
        tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=2, now=NOW - 50)


def test_a_ticket_with_time_and_use_left_queues_as_before(env):
    conn, cfg, ids, t = env
    assert not limits.used_up(conn, cfg, tickets.get(conn, t["id"]), NOW - 100)
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
    assert not limits.used_up(conn, cfg, tickets.get(conn, t["id"]), NOW - 100)


def test_only_an_unused_ticket_can_be_upgraded(env):
    conn, cfg, ids, t = env
    t = tickets.get(conn, t["id"])
    req(conn, ids["alice"], NOW - 250, path="/v1/messages/count_tokens")   # counting tokens is not using the ticket
    assert tickets.upgrade_quote(conn, cfg, t, "standard", "USD", NOW - 200)["tier"] == "standard"
    req(conn, ids["alice"], NOW - 150, i=10)
    with pytest.raises(tickets.TicketError, match="has been used"):
        tickets.upgrade_quote(conn, cfg, t, "standard", "USD", NOW - 100)
    assert tickets.upgrade_options(conn, cfg, ids["alice"], "USD", NOW - 100) == []
