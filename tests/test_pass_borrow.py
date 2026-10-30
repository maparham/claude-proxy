"""A pass across the weekly reset borrows from its next part (spec 2026-10-30-pass-borrow-across-reset)."""
import pytest

from claude_proxy import limits, tickets
from claude_proxy.config import LENGTHS
from claude_proxy.db import create_user
from tests.test_tickets_limits import RESET7, piece, req, set_limit, snap
from tests.tickets_helpers import DAY, seeded, user

START = RESET7 - 2 * 3600          # a day pass bought 2 hours before Anthropic's weekly reset
AT = START + 3600                  # checked an hour in
OWN, NEXT = piece(5, 2 * 3600), piece(5, 22 * 3600)   # its parts: 2 hours before the reset, 22 after


def at(conn, t, util7, resets=RESET7):
    """Fresh snapshots at t: the 7-day bucket at util7, the 5-hour one empty in a window of its own."""
    snap(conn, t, 0, "5h", t + 3000)
    snap(conn, t, util7, "7d", resets)


def grant(conn, cfg, ids, uid, length="day", start=START):
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, uid), "lite", length, "USD", now=start, terms_version="v1")
    conn.execute("UPDATE tickets SET starts_at=?, ends_at=? WHERE id=?", (start, start + LENGTHS[length] * DAY, t["id"]))
    return tickets.get(conn, t["id"])


@pytest.fixture
def env(db):
    """Alice's day pass across the reset, on an account with a rate of 1000 weighted tokens per point and 1 point used."""
    conn, cfg, ids = seeded(db)
    at(conn, START - 1000, 0)
    req(conn, ids["alice"], START - 900, i=1000)
    at(conn, START - 800, 1)
    t = grant(conn, cfg, ids, ids["alice"])
    return conn, cfg, ids, t


def use(conn, uid, t, tokens, util7, when=AT - 100):
    """`tokens` served on pass t, and the account's snapshot after them."""
    req(conn, uid, when - 5, i=tokens, ticket=t["id"])
    at(conn, when, util7)


def week(conn, cfg, uid, now=AT):
    return {s.kind: s for s in limits.states(conn, cfg, uid, now=now)}["share_7d"]


def check(conn, cfg, uid, now=AT, served=None):
    return limits.evaluate(conn, cfg, uid, "claude-sonnet-5", "/v1/messages", now=now, served=served)


def test_the_part_before_the_reset_borrows_the_next_part_on_an_empty_account(env):
    conn, cfg, ids, t = env
    use(conn, ids["alice"], t, 100, 1.1)                  # 0.1 points, past its own 2-hour part
    assert check(conn, cfg, ids["alice"]) is None
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(OWN + NEXT)


def test_borrowing_stops_where_the_free_room_ends_not_halfway(env):
    """Free room = 100 - U - owed - margin. Bob's hand-set weekly share holds back 93.6 points, so 0.3 are free."""
    conn, cfg, ids, t = env
    bob, _ = create_user(conn, "bob")
    set_limit(conn, bob, "share_7d", 93.6, "pct")
    use(conn, ids["alice"], t, 100, 1.1)
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.1 + 0.3)
    use(conn, ids["alice"], t, 200, 1.3, when=AT - 40)    # the account rises with her use
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.4)
    assert check(conn, cfg, ids["alice"]) is None
    use(conn, ids["alice"], t, 100, 1.4, when=AT - 10)
    assert check(conn, cfg, ids["alice"]).kind == "share_7d"


def test_a_full_account_gives_no_extra(env):
    conn, cfg, ids, t = env
    bob, _ = create_user(conn, "bob")
    set_limit(conn, bob, "share_7d", 94, "pct")           # 100 - 1.1 - 94 - 5 < 0
    use(conn, ids["alice"], t, 100, 1.1)
    st = week(conn, cfg, ids["alice"])
    assert st.limit == pytest.approx(0.1) and st.exceeded   # what it has used, and not a point more


def test_other_passes_unused_parts_are_held_back(env):
    conn, cfg, ids, t = env
    bob, _ = create_user(conn, "bob")
    set_limit(conn, ids["admin"], "share_7d", 93, "pct")  # an ungated hand-set share: 93 owed
    b = grant(conn, cfg, ids, bob, length="week", start=START - DAY)   # running, unused: its part this week is owed
    use(conn, ids["alice"], t, 100, 1.1)
    free = 100 - 1.1 - 93 - piece(5, DAY + 2 * 3600) - 5
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.1 + free)
    assert b["id"]


def test_a_pass_queued_to_start_later_this_week_is_owed(env):
    conn, cfg, ids, t = env
    bob, _ = create_user(conn, "bob")
    set_limit(conn, ids["admin"], "share_7d", 93.8, "pct")
    grant(conn, cfg, ids, bob, start=AT + 1400)           # starts 2200 s before the reset
    use(conn, ids["alice"], t, 100, 1.1)
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.1 + (100 - 1.1 - 93.8 - 5) - piece(5, 2200))


def test_a_bonus_starting_later_this_week_is_owed(env):
    conn, cfg, ids, t = env
    bob, _ = create_user(conn, "bob")
    set_limit(conn, ids["admin"], "share_7d", 93.8, "pct")
    b = grant(conn, cfg, ids, bob, length="week", start=AT + 1400)
    conn.execute("INSERT INTO ticket_bonuses(ticket_id, share_pct, starts_at, ends_at, granted_at) VALUES(?,?,?,?,?)",
                 (b["id"], 2, AT + 1600, AT + 3600 * 24, AT))
    use(conn, ids["alice"], t, 100, 1.1)
    owed = piece(5, 2200) + piece(2, 2000)             # both up to the reset at AT + 3600
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.1 + (100 - 1.1 - 93.8 - 5) - owed)


def test_what_one_pass_borrows_is_gone_for_the_next(env):
    conn, cfg, ids, t = env
    bob, _ = create_user(conn, "bob")
    set_limit(conn, ids["admin"], "share_7d", 93.6, "pct")
    b = grant(conn, cfg, ids, bob)
    use(conn, ids["alice"], t, 100, 1.1)
    room = 100 - 1.1 - 93.6 - 5 - OWN                     # bob's own part is owed to him
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.1 + room)
    took = int(room * 1000)
    use(conn, ids["alice"], t, took, 1.1 + took / 1000, when=AT - 40)   # alice takes all of it
    use(conn, bob, b, 100, 1.2 + took / 1000, when=AT - 20)
    st = week(conn, cfg, bob)
    assert st.limit == pytest.approx(0.1) and st.exceeded  # nothing left for bob to borrow


@pytest.mark.parametrize("case", ["stale", "margin", "no_rate", "reset_passed"])
def test_nothing_is_borrowed_without_live_data_or_room(db, case):
    conn, cfg, ids = seeded(db)
    at(conn, START - 1000, 0)
    if case != "no_rate":                                 # no rise ever: a young account, measured by attribution
        req(conn, ids["alice"], START - 900, i=1000)
        at(conn, START - 800, 1)
    t = grant(conn, cfg, ids, ids["alice"], length="month" if case == "reset_passed" else "day")
    req(conn, ids["alice"], AT - 150, i=100, ticket=t["id"])
    now, own = AT, OWN
    if case == "stale":
        now = AT + cfg.quota.stale_after_s + 1000         # still before the reset
        assert now < RESET7
    elif case == "reset_passed":
        now, own = RESET7 + 600, 5                        # the month pass's whole second week
        req(conn, ids["alice"], now - 100, i=6000, ticket=t["id"])
        at(conn, now - 60, 1)                             # fresh, but still naming the old week's reset
    else:
        cfg.tickets.borrow_margin_pct = 100 if case == "margin" else cfg.tickets.borrow_margin_pct
        at(conn, AT - 100, 1.1 if case == "margin" else 0)
    st = week(conn, cfg, ids["alice"], now=now)
    if case == "no_rate":                                 # measured by attribution, which pins no rise on her yet
        assert st.limit == pytest.approx(own) and st.current == 0
    else:
        assert st.limit == pytest.approx(own) and st.exceeded


def test_a_pass_ending_inside_the_week_never_borrows(env):
    conn, cfg, ids, t = env
    conn.execute("UPDATE tickets SET ends_at=? WHERE id=?", (START + 3600 * 1.5, t["id"]))
    use(conn, ids["alice"], t, 100, 1.1)
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(piece(5, 3600 * 1.5))


def borrowed(conn, cfg, ids, t):
    """Alice borrows 0.4 points before the reset: 0.4 - OWN of them from her next part."""
    use(conn, ids["alice"], t, 400, 1.4)
    served = {}
    assert check(conn, cfg, ids["alice"], served=served) is None and served["ticket_id"] == t["id"]


def test_after_the_reset_the_next_part_is_less_what_was_borrowed(env):
    conn, cfg, ids, t = env
    borrowed(conn, cfg, ids, t)
    after = RESET7 + 600
    at(conn, after - 60, 0, resets=RESET7 + 7 * DAY + 60)   # the new week's reset reported a minute later
    # The new week starts a minute late, so the part is a minute short, as it is today.
    assert week(conn, cfg, ids["alice"], now=after).limit == pytest.approx(piece(5, 22 * 3600 - 60) - (0.4 - OWN))


def test_debt_does_not_follow_capacity_or_rate_changes_after_the_reset(env):
    conn, cfg, ids, t = env
    borrowed(conn, cfg, ids, t)
    after = RESET7 + 600
    at(conn, after - 300, 0, resets=RESET7 + 7 * DAY)
    req(conn, ids["admin"], after - 200, i=500)          # the rate halves: 500 tokens moved a whole point
    at(conn, after - 100, 1, resets=RESET7 + 7 * DAY)
    assert week(conn, cfg, ids["alice"], now=after).limit == pytest.approx(NEXT - (0.4 - OWN))


def test_a_pass_that_never_borrowed_gets_its_whole_next_part(env):
    conn, cfg, ids, t = env
    use(conn, ids["alice"], t, 50, 1.05)
    assert check(conn, cfg, ids["alice"]) is None
    after = RESET7 + 600
    at(conn, after - 60, 0, resets=RESET7 + 7 * DAY)
    assert week(conn, cfg, ids["alice"], now=after).limit == pytest.approx(NEXT)


def test_only_an_allowed_request_records_the_borrowing(env):
    from claude_proxy.web import _status_line
    conn, cfg, ids, t = env
    use(conn, ids["alice"], t, 400, 1.4)
    sts = limits.states(conn, cfg, ids["alice"], now=AT)
    limits.ticket_weeks(conn, cfg, ids["alice"], AT, next(s for s in sts if s.kind == "share_7d"))
    _status_line(user(conn, ids["alice"]), sts, None)
    set_limit(conn, ids["alice"], "requests_daily", 0, "count")
    assert check(conn, cfg, ids["alice"]).kind == "requests_daily"
    assert conn.execute("SELECT COUNT(*) FROM pass_borrows").fetchone()[0] == 0


def test_a_week_pass_borrowing_twice_is_charged_both_times(db):
    conn, cfg, ids = seeded(db)
    at(conn, START - 1000, 0)
    req(conn, ids["alice"], START - 900, i=1000)
    at(conn, START - 800, 1)
    t = grant(conn, cfg, ids, ids["alice"], length="month")
    parts = tickets.week_split(conn, cfg, 5, START, START + 30 * DAY, now=AT)
    use(conn, ids["alice"], t, 400, 1.4)                  # week 1: borrows 0.4 - parts[0]
    assert check(conn, cfg, ids["alice"], served={}) is None
    w2 = RESET7 + 600
    own2 = parts[1] - (0.4 - parts[0])
    req(conn, ids["alice"], w2 - 100, i=int((own2 + 1) * 1000), ticket=t["id"])   # week 2: a point past its own limit
    at(conn, w2, 0, resets=RESET7 + 7 * DAY)
    assert check(conn, cfg, ids["alice"], now=w2 + 60, served={}) is None
    w3 = RESET7 + 7 * DAY + 600
    at(conn, w3 - 60, 0, resets=RESET7 + 14 * DAY)
    assert week(conn, cfg, ids["alice"], now=w3).limit == pytest.approx(parts[2] - 1, abs=1e-3)


def test_debt_larger_than_the_next_part_carries_into_the_week_after(db):
    conn, cfg, ids = seeded(db)
    at(conn, START - 1000, 0)
    req(conn, ids["alice"], START - 900, i=1000)
    at(conn, START - 800, 1)
    t = grant(conn, cfg, ids, ids["alice"], length="month")
    req(conn, ids["alice"], AT, i=6000, ticket=t["id"])  # 6 points before the reset, recorded against an own part of 0
    conn.execute("INSERT INTO pass_borrows(ticket_id, week_end, own_pts, rate, updated_at) VALUES(?,?,?,?,?)",
                 (t["id"], RESET7, 0.0, 1000.0, AT))
    w2, w3 = RESET7 + 600, RESET7 + 7 * DAY + 600
    at(conn, w2 - 60, 0, resets=RESET7 + 7 * DAY)
    assert week(conn, cfg, ids["alice"], now=w2).limit == 0   # 6 points of debt against a 5-point week
    at(conn, w3 - 60, 0, resets=RESET7 + 14 * DAY)
    assert week(conn, cfg, ids["alice"], now=w3).limit == pytest.approx(5 - 1)


def test_a_users_other_pass_serves_before_one_borrows(env):
    conn, cfg, ids, t = env
    w = grant(conn, cfg, ids, ids["alice"], length="week", start=START - DAY)
    conn.execute("UPDATE users SET pass_pin=? WHERE id=?", (t["id"], ids["alice"]))
    use(conn, ids["alice"], t, 100, 1.1)                  # the pinned day pass is past its own part
    served = {}
    assert check(conn, cfg, ids["alice"], served=served) is None and served["ticket_id"] == w["id"]


def test_a_pass_at_its_5_hour_limit_does_not_borrow(env):
    conn, cfg, ids, t = env
    conn.execute("UPDATE tickets SET share_5h_pct=0 WHERE id=?", (t["id"],))
    use(conn, ids["alice"], t, 100, 1.1)
    assert check(conn, cfg, ids["alice"]).kind == "share_5h"


def test_split_bar_and_warning_follow_what_was_borrowed(env):
    from claude_proxy.web import _ticket_weeks_text
    conn, cfg, ids, t = env
    use(conn, ids["alice"], t, 400, 1.4)
    st = week(conn, cfg, ids["alice"])
    weeks = limits.ticket_weeks(conn, cfg, ids["alice"], AT, st)
    whole = OWN + NEXT
    assert [w["share"] for w in weeks["weeks"]] == pytest.approx([0.4 / whole, (NEXT - (0.4 - OWN)) / whole])
    assert weeks["weeks"][0]["pct"] == pytest.approx(100 * 0.4 / whole)    # the meter: use of what it may use now
    assert weeks["ticket_pct"] == pytest.approx(100 * 0.4 / whole)
    assert _ticket_weeks_text(weeks, "").startswith(f"{100 * 0.4 / whole:.0f}% of first part · ")   # past 80% of its own part


def test_serving_pass_card_is_not_marked_at_its_limit_while_borrowing(env):
    conn, cfg, ids, t = env
    use(conn, ids["alice"], t, 100, 1.1)
    srv, sts = limits.serving(conn, cfg, ids["alice"], AT)
    assert srv["id"] == t["id"] and not any(s.exceeded for s in sts)



async def test_the_dashboard_card_of_a_borrowing_pass_shows_it_serving_not_at_its_limit(db, cfg):
    """The cards run on the wall clock: a day pass an hour into its last two hours before the reset, and a queued pass
    so the card lists them."""
    import time
    from tests.test_orders_web import client
    from tests.test_tickets_web import env as web_env
    from tests.test_web import bearer
    gw, conn, cfg, ids, keys = web_env.__wrapped__(db, cfg)
    now = time.time()
    reset = now + 3600
    snap(conn, now - 1000, 0, "7d", reset)
    req(conn, ids["alice"], now - 900, i=1000)
    snap(conn, now - 800, 1, "7d", reset)
    snap(conn, now - 800, 0, "5h", now + 3000)
    t = grant(conn, cfg, ids, ids["alice"], start=int(now - 3600))
    grant(conn, cfg, ids, ids["alice"], start=int(now + 2 * DAY))
    req(conn, ids["alice"], now - 100, i=100, ticket=t["id"])   # 0.1 points, past its own 2-hour part
    snap(conn, now - 50, 1.1, "7d", reset)
    async with client(gw) as c:
        me = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["current"]
    card = me["passes"][0]
    assert card["serving"] and "back_at" not in card


@pytest.mark.parametrize("value, ok", [("0", True), ("12.5", True), ("100", True), ("-1", False), ("101", False), ("true", False)])
def test_borrow_margin_pct_is_a_number_from_0_to_100(tmp_path, value, ok):
    from claude_proxy.config import Config, ConfigError, TicketsConfig
    assert TicketsConfig().borrow_margin_pct == 5
    p = tmp_path / "c.toml"
    p.write_text(f"[tickets]\nborrow_margin_pct = {value}\n")
    if ok:
        assert Config.load(str(p)).tickets.borrow_margin_pct == float(value)
    else:
        with pytest.raises(ConfigError, match="borrow_margin_pct"):
            Config.load(str(p))
