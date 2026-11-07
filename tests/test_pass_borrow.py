"""A pass across the weekly reset borrows from its next part (spec 2026-10-30-pass-borrow-across-reset)."""
import pytest

from claude_proxy import capacity, limits, tickets
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


def grant_no_report(conn, cfg, ids, monkeypatch, uid, length="day", start=0):
    """grant(), with the capacity gate stubbed fresh for just this call: bob's grant here is only there to make his
    pass reserve against alice's week, and inserting a real snapshot fresh enough for it shifts the attribution window
    the `week()`/`piece()` arithmetic below depends on (a sale needs a fresh report, 2026-11-04 spec)."""
    monkeypatch.setattr(tickets, "week_bounds", lambda conn, cfg, now: (now - DAY, now + 6 * DAY))
    monkeypatch.setattr(capacity, "fresh", lambda att, now: True)
    monkeypatch.setattr(capacity, "_att", lambda conn, cfg, now: {
        "utilization_pct": 0.0, "stale": False, "resets_at": now + 6 * DAY, "ticket_shares": {}, "unattributed": 0.0})
    t = grant(conn, cfg, ids, uid, length=length, start=start)
    monkeypatch.undo()
    return t


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


def test_a_hand_set_weekly_share_is_headroom_and_holds_nothing_back(env):
    conn, cfg, ids, t = env
    set_limit(conn, ids["admin"], "share_7d", 93.6, "pct")
    use(conn, ids["alice"], t, 300, 1.3)
    assert check(conn, cfg, ids["alice"]) is None                  # borrows as on an empty account
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(OWN + NEXT)


def test_the_part_before_the_reset_borrows_the_next_part_on_an_empty_account(env):
    conn, cfg, ids, t = env
    use(conn, ids["alice"], t, 100, 1.1)                  # 0.1 points, past its own 2-hour part
    assert check(conn, cfg, ids["alice"]) is None
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(OWN + NEXT)


def test_borrowing_stops_where_the_free_room_ends_not_halfway(env):
    """Free room = 100 - U - reserved - margin. A borrow margin of 98.6 (93.6 + the default 5) holds back the same, so 0.3 are free."""
    conn, cfg, ids, t = env
    cfg.tickets.borrow_margin_pct = 93.6 + 5
    use(conn, ids["alice"], t, 100, 1.1)
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.1 + 0.3)
    use(conn, ids["alice"], t, 200, 1.3, when=AT - 40)    # the account rises with her use
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.4)
    assert check(conn, cfg, ids["alice"]) is None
    use(conn, ids["alice"], t, 100, 1.4, when=AT - 10)
    assert check(conn, cfg, ids["alice"]).kind == "share_7d"


def test_a_full_account_gives_no_extra(env):
    conn, cfg, ids, t = env
    cfg.tickets.borrow_margin_pct = 94 + 5                # 100 - 1.1 - 99 < 0
    use(conn, ids["alice"], t, 100, 1.1)
    st = week(conn, cfg, ids["alice"])
    assert st.limit == pytest.approx(0.1) and st.exceeded   # what it has used, and not a point more


def test_other_passes_unused_parts_are_held_back(env):
    conn, cfg, ids, t = env
    bob, _ = create_user(conn, "bob")
    cfg.tickets.borrow_margin_pct = 93 + 5                # an ungated hand-set share no longer reserved: 93, plus the default margin
    b = grant(conn, cfg, ids, bob, length="week", start=START - DAY)   # running, unused: its part this week is reserved
    use(conn, ids["alice"], t, 100, 1.1)
    free = 100 - 1.1 - 93 - piece(5, DAY + 2 * 3600) - 5
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.1 + free)
    assert b["id"]


def test_a_pass_queued_to_start_later_this_week_is_reserved(env, monkeypatch):
    conn, cfg, ids, t = env
    bob, _ = create_user(conn, "bob")
    cfg.tickets.borrow_margin_pct = 93.8 + 5
    grant_no_report(conn, cfg, ids, monkeypatch, bob, start=AT + 1400)           # starts 2200 s before the reset
    use(conn, ids["alice"], t, 100, 1.1)
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.1 + (100 - 1.1 - 93.8 - 5) - piece(5, 2200))


def test_a_bonus_starting_later_this_week_is_reserved(env, monkeypatch):
    conn, cfg, ids, t = env
    bob, _ = create_user(conn, "bob")
    cfg.tickets.borrow_margin_pct = 93.8 + 5
    b = grant_no_report(conn, cfg, ids, monkeypatch, bob, length="week", start=AT + 1400)
    conn.execute("INSERT INTO ticket_bonuses(ticket_id, share_pct, starts_at, ends_at, granted_at) VALUES(?,?,?,?,?)",
                 (b["id"], 2, AT + 1600, AT + 3600 * 24, AT))
    use(conn, ids["alice"], t, 100, 1.1)
    reserved = piece(5, 2200) + piece(2, 2000)             # both up to the reset at AT + 3600
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.1 + (100 - 1.1 - 93.8 - 5) - reserved)


def test_what_one_pass_borrows_is_gone_for_the_next(env):
    conn, cfg, ids, t = env
    bob, _ = create_user(conn, "bob")
    cfg.tickets.borrow_margin_pct = 93.6 + 5
    b = grant(conn, cfg, ids, bob)
    use(conn, ids["alice"], t, 100, 1.1)
    room = 100 - 1.1 - 93.6 - 5 - OWN                     # bob's own part is reserved for him
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.1 + room)
    took = int(room * 1000)
    use(conn, ids["alice"], t, took, 1.1 + took / 1000, when=AT - 40)   # alice takes all of it
    use(conn, bob, b, 100, 1.2 + took / 1000, when=AT - 20)
    st = week(conn, cfg, bob)
    assert st.limit == pytest.approx(0.1) and st.exceeded  # nothing left for bob to borrow


@pytest.mark.parametrize("case", ["stale", "margin", "no_rate"])
def test_nothing_is_borrowed_without_live_data_or_room(db, case):
    conn, cfg, ids = seeded(db)
    at(conn, START - 1000, 0)
    if case != "no_rate":                                 # no rise ever: a young account, measured by attribution
        req(conn, ids["alice"], START - 900, i=1000)
        at(conn, START - 800, 1)
    t = grant(conn, cfg, ids, ids["alice"])
    req(conn, ids["alice"], AT - 150, i=100, ticket=t["id"])
    now, own = AT, OWN
    if case == "stale":
        now = AT + cfg.quota.stale_after_s + 1000         # still before the reset
        assert now < RESET7
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


def test_split_bar_parts_still_add_up_after_the_rate_changed(env, monkeypatch):
    # The past part is drawn with what it took as recorded then, the figure the next part's debt uses, not re-measured
    # at today's rate: the two must agree for the parts to add up to the whole pass.
    from claude_proxy import quota
    conn, cfg, ids, t = env
    borrowed(conn, cfg, ids, t)
    after = RESET7 + 600
    at(conn, after - 60, 0, resets=RESET7 + 7 * DAY)
    monkeypatch.setattr(quota, "observed_rate", lambda *a, **k: 500.0)   # the rate halved since the borrowing
    weeks = limits.ticket_weeks(conn, cfg, ids["alice"], after, week(conn, cfg, ids["alice"], now=after))
    whole = OWN + NEXT
    assert [w["share"] for w in weeks["weeks"]] == pytest.approx([0.4 / whole, (NEXT - (0.4 - OWN)) / whole])
    assert sum(w["share"] for w in weeks["weeks"]) == pytest.approx(1)


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


def test_only_a_day_pass_borrows(db):
    """A week or month pass keeps each week's share: used up before the reset, it waits, room or no room."""
    conn, cfg, ids = seeded(db)
    at(conn, START - 1000, 0)
    req(conn, ids["alice"], START - 900, i=1000)
    at(conn, START - 800, 1)
    t = grant(conn, cfg, ids, ids["alice"], length="week")
    use(conn, ids["alice"], t, 200, 1.2)                  # 0.2 points, past its 2-hour part of the week
    st = week(conn, cfg, ids["alice"])
    assert st.limit == pytest.approx(OWN) and st.exceeded
    assert check(conn, cfg, ids["alice"]).kind == "share_7d"
    assert conn.execute("SELECT COUNT(*) FROM pass_borrows").fetchone()[0] == 0


def test_debt_larger_than_the_second_part_leaves_nothing_and_carries_nowhere(env):
    conn, cfg, ids, t = env
    conn.execute("INSERT INTO pass_borrows(ticket_id, week_end, own_pts, rate, updated_at) VALUES(?,?,?,?,?)",
                 (t["id"], RESET7, OWN, 1000.0, AT))
    req(conn, ids["alice"], AT, i=int((OWN + NEXT + 1) * 1000), ticket=t["id"])   # a point more than the whole pass, in flight at the reset
    after = RESET7 + 600
    at(conn, after - 60, 0, resets=RESET7 + 7 * DAY)
    assert week(conn, cfg, ids["alice"], now=after).limit == 0


def test_a_pass_borrows_while_the_users_other_pass_still_has_room(env):
    conn, cfg, ids, t = env
    cfg.tickets.borrow_margin_pct = 93 + 5
    w = grant(conn, cfg, ids, ids["alice"], length="week", start=START - DAY)
    conn.execute("UPDATE users SET pass_pin=? WHERE id=?", (t["id"], ids["alice"]))
    use(conn, ids["alice"], t, 100, 1.1)                  # the pinned day pass is past its own part
    served = {}
    assert check(conn, cfg, ids["alice"], served=served) is None and served["ticket_id"] == t["id"]   # it borrows, 2026-11-05
    free = 100 - 1.1 - 93 - piece(5, DAY + 2 * 3600) - 5  # the week pass's unused part this week is held back as reserved
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.1 + free)
    assert not any(s.exceeded for s in limits.pass_states(conn, cfg, w, AT))


def test_a_pass_at_its_5_hour_limit_does_not_borrow(env):
    conn, cfg, ids, t = env
    conn.execute("UPDATE tickets SET share_5h_pct=0 WHERE id=?", (t["id"],))
    use(conn, ids["alice"], t, 100, 1.1)
    assert check(conn, cfg, ids["alice"]).kind == "share_5h"


def test_split_bar_follows_what_was_borrowed(env):
    from claude_proxy.web import _ticket_weeks_text
    conn, cfg, ids, t = env
    use(conn, ids["alice"], t, 400, 1.4)
    st = week(conn, cfg, ids["alice"])
    weeks = limits.ticket_weeks(conn, cfg, ids["alice"], AT, st)
    whole = OWN + NEXT
    assert [w["share"] for w in weeks["weeks"]] == pytest.approx([0.4 / whole, (NEXT - (0.4 - OWN)) / whole])
    assert weeks["weeks"][0]["pct"] == pytest.approx(100 * 0.4 / whole)    # the meter: use of what it may use now
    assert weeks["ticket_pct"] == pytest.approx(100 * 0.4 / whole)
    assert _ticket_weeks_text(weeks, "").startswith(f"{100 * 0.4 / whole:.0f}% used")   # one allowance: no part named


def test_serving_pass_card_is_not_marked_at_its_limit_while_borrowing(env):
    conn, cfg, ids, t = env
    use(conn, ids["alice"], t, 100, 1.1)
    srv, sts = limits.serving(conn, cfg, ids["alice"], AT)
    assert srv["id"] == t["id"] and not any(s.exceeded for s in sts)



async def test_the_dashboard_card_of_a_borrowing_pass_shows_it_serving_not_at_its_limit(db, cfg, monkeypatch):
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
    grant_no_report(conn, cfg, ids, monkeypatch, ids["alice"], start=int(now + 2 * DAY))   # queued, just to list on the card
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


def test_a_reset_time_that_shifts_a_little_keeps_one_borrow_record_and_the_last_write_counts(env):
    conn, cfg, ids, t = env
    use(conn, ids["alice"], t, 300, 1.3)
    assert check(conn, cfg, ids["alice"]) is None
    req(conn, ids["alice"], AT + 50, i=100, ticket=t["id"])
    at(conn, AT + 60, 1.4, resets=RESET7 - 30)            # Anthropic reports the reset 30 s earlier now
    assert check(conn, cfg, ids["alice"], now=AT + 100) is None
    rows = conn.execute("SELECT week_end FROM pass_borrows").fetchall()
    assert len(rows) == 1


def test_a_pass_that_ended_earlier_this_week_holds_nothing_back(env):
    conn, cfg, ids, t = env
    bob, _ = create_user(conn, "bob")
    cfg.tickets.borrow_margin_pct = 93.6 + 5
    grant(conn, cfg, ids, bob, start=START - 2 * DAY)     # a day pass over a day ago, its part all unused
    use(conn, ids["alice"], t, 100, 1.1)
    assert week(conn, cfg, ids["alice"]).limit == pytest.approx(0.1 + (100 - 1.1 - 93.6 - 5))


def test_a_pass_stopped_for_now_tells_the_user_when_it_is_back(env):
    conn, cfg, ids, t = env
    cfg.tickets.borrow_margin_pct = 100                  # no room to borrow: its first part is used up
    use(conn, ids["alice"], t, 100, 1.1)
    view = next(limits.user_view(s) for s in limits.states(conn, cfg, ids["alice"], now=AT) if s.kind == "share_7d")
    assert view["exceeded"] and view["reset_in"] == RESET7 - AT   # relative: never the account's reset time
    cfg.tickets.borrow_margin_pct = 5
    view = next(limits.user_view(s) for s in limits.states(conn, cfg, ids["alice"], now=AT) if s.kind == "share_7d")
    assert not view["exceeded"] and view["reset_in"] is None


def test_every_pass_card_shows_its_whole_use(env):
    conn, cfg, ids, t = env
    use(conn, ids["alice"], t, 100, 1.1)
    w = limits.pass_weeks(conn, cfg, t, AT)
    assert w["ticket_pct"] == pytest.approx(100 * 0.1 / (OWN + NEXT))


def test_a_used_up_pass_that_ends_before_it_refills_is_not_back_in_a_while(env):
    conn, cfg, ids, t = env
    cfg.tickets.borrow_margin_pct = 100
    conn.execute("UPDATE tickets SET ends_at=? WHERE id=?", (START + 3600 * 1.5, t["id"]))   # over before the reset
    use(conn, ids["alice"], t, 100, 1.1)
    view = next(limits.user_view(s) for s in limits.states(conn, cfg, ids["alice"], now=AT) if s.kind == "share_7d")
    assert view["exceeded"] and view["reset_in"] is None and view["used_up"]


def test_status_line_of_a_stopped_day_pass_says_used_up_for_now(env):
    """No room to borrow: every pass is at its limit, so none serves, but the line still gives the pass's figures."""
    from claude_proxy.web import _status_line
    conn, cfg, ids, t = env
    cfg.tickets.borrow_margin_pct = 100
    use(conn, ids["alice"], t, 100, 1.1)
    sts = limits.states(conn, cfg, ids["alice"], now=AT)
    weeks = limits.ticket_weeks(conn, cfg, ids["alice"], AT, next(s for s in sts if s.kind == "share_7d"))
    assert weeks is not None and weeks["weeks"][0]["pct"] >= 100
    assert " · used up for now (100%) · " in _status_line(user(conn, ids["alice"]), sts, None, weeks)


def test_a_week_pass_over_its_part_shows_no_phantom_debt_on_the_next(db):
    conn, cfg, ids = seeded(db)
    at(conn, START - 1000, 0)
    req(conn, ids["alice"], START - 900, i=1000)
    at(conn, START - 800, 1)
    t = grant(conn, cfg, ids, ids["alice"], length="week")
    use(conn, ids["alice"], t, 200, 1.2)                  # over its 2-hour part (a race, say): it waits, it borrows nothing
    w = limits.pass_weeks(conn, cfg, t, AT, week(conn, cfg, ids["alice"]))
    assert [x["share"] for x in w["weeks"]] == pytest.approx([2 * 3600 / (7 * DAY), 1 - 2 * 3600 / (7 * DAY)])


def test_a_day_pass_that_borrowed_all_its_next_part_is_used_up_not_back_at_the_reset(env):
    conn, cfg, ids, t = env
    use(conn, ids["alice"], t, 100, 1.1)                  # past its own part: borrows from the next
    assert check(conn, cfg, ids["alice"]) is None
    tokens = int((OWN + NEXT) * 1000) + 10                # then past both its parts
    use(conn, ids["alice"], t, tokens, 1.1 + tokens / 1000, when=AT - 50)
    assert check(conn, cfg, ids["alice"]).kind == "share_7d"
    assert limits.pass_weeks(conn, cfg, t, AT)["ticket_pct"] >= 100
    assert limits.used_up(conn, cfg, ids["alice"], AT)    # the reset frees nothing: its next part is spent
    view = next(limits.user_view(s) for s in limits.states(conn, cfg, ids["alice"], now=AT) if s.kind == "share_7d")
    assert view["used_up"] and view["reset_in"] is None   # not "back in" at the reset
    assert "pass is used up. Buy another" in check(conn, cfg, ids["alice"]).body["error"]["message"]


def test_a_day_pass_that_can_still_borrow_at_todays_rate_is_not_used_up(env):
    """Its borrow was recorded at 1000 tokens a point; the rate has since tripled, so the same tokens are fewer points."""
    conn, cfg, ids, t = env
    bob, _ = create_user(conn, "bob")
    use(conn, ids["alice"], t, 100, 1.1, when=AT - 1000)
    assert check(conn, cfg, ids["alice"], now=AT - 990) is None   # records its borrow at 1000 tokens a point
    req(conn, bob, AT - 900, i=3000)
    at(conn, AT - 890, 2.1)
    req(conn, bob, AT - 800, i=3000)
    at(conn, AT - 790, 3.1)
    use(conn, ids["alice"], t, int((OWN + NEXT) * 1000) + 10, 3.2)
    assert limits.serving(conn, cfg, ids["alice"], AT)[0] is not None   # still served, by borrowing
    assert not limits.used_up(conn, cfg, ids["alice"], AT)


def test_the_admin_sees_who_used_the_week_and_each_pass_s_part_of_it(env):
    from claude_proxy import capacity
    conn, cfg, ids, t = env
    use(conn, ids["alice"], t, 100, 1.1)                  # 1 point off a pass before it, then 0.1 on it
    w = capacity.week_use(conn, cfg, 1, AT)
    assert w["by"]["passes"] == pytest.approx(0.1) and w["by"]["outside"] == pytest.approx(0)
    assert [(u["name"], round(u["pts"], 2)) for u in w["by"]["users"]] == [("alice", 1.0)]
    assert w["passes"][t["id"]] == pytest.approx({"sold": OWN, "used": 0.1, "reserved": 0.0})   # over its part: borrowing
    assert capacity.summary(conn, cfg, 1, AT)["used_by"] == w["by"]

