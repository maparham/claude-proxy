import pytest

from claude_proxy import limits, tickets
from claude_proxy.db import create_user
from tests.tickets_helpers import DAY, NOW, seeded, user


def set_limit(conn, uid, kind, value, unit, scope="*"):
    conn.execute("INSERT OR REPLACE INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (uid, kind, scope, str(value), unit))


def req(conn, uid, t, i=0, o=0, model="claude-sonnet-5", provider="anthropic", path="/v1/messages"):
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, input_tokens, output_tokens) VALUES(?,?,?,?,?,?,?,?,?)",
                 (uid, t - 1, t, "POST", path, provider, model, i, o))


def snap(conn, t, util, bucket, resets):
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                 (t, "header", bucket, util, resets))


RESET5, RESET7 = NOW + 3500, NOW + 3 * DAY   # fixed reset times: a moving reset time would read as a new window


def fresh(conn, t, util5=0, util7=0):
    """A pair of account snapshots at t, so share limits are measured, not skipped."""
    snap(conn, t, util5, "5h", RESET5)
    snap(conn, t, util7, "7d", RESET7)


def check(conn, cfg, uid, now=NOW, model="claude-sonnet-5", path="/v1/messages"):
    return limits.evaluate(conn, cfg, uid, model, path, now=now)


def by_kind(conn, cfg, uid, now=NOW):
    return {s.kind: s for s in limits.states(conn, cfg, uid, now=now)}


@pytest.fixture
def env(db):
    conn, cfg, ids = seeded(db)
    fresh(conn, NOW - 600)
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "EUR", now=NOW - 300)   # 5%, share_day 0.714
    return conn, cfg, ids, t


def test_non_ticket_users_are_untouched(db):
    conn, cfg, ids = seeded(db)
    set_limit(conn, ids["alice"], "share_5h", 20, "pct")
    assert check(conn, cfg, ids["alice"]) is None
    assert list(by_kind(conn, cfg, ids["alice"])) == ["share_5h"]


def test_ticket_builds_5h_and_day_states_in_place_of_admin_share_rows(env):
    conn, cfg, ids, t = env
    set_limit(conn, ids["alice"], "share_5h", 50, "pct")
    set_limit(conn, ids["alice"], "share_7d", 50, "pct")
    set_limit(conn, ids["alice"], "requests_daily", 100, "count")
    st = by_kind(conn, cfg, ids["alice"])
    assert set(st) == {"requests_daily", "share_5h", "share_day"}
    assert (st["share_5h"].limit, st["share_5h"].tier) == (5, "Lite")
    assert st["share_day"].limit == pytest.approx(5 / 7)
    assert (st["share_day"].current, st["share_day"].reset_in, st["share_day"].resets_at) == (0, DAY - 300, NOW - 300 + DAY)
    assert st["share_day"].to_dict()["no_live_data"] is False


def test_share_5h_measured_as_today_and_exceeded_is_the_usual_429(env):
    conn, cfg, ids, t = env
    req(conn, ids["alice"], NOW - 200, i=1000)
    fresh(conn, NOW - 100, util5=6)             # alice alone: +6 points of the 5-hour bucket
    d = check(conn, cfg, ids["alice"])
    assert (d.status, d.kind, d.retry_after) == (429, "share_5h", 3500)
    assert "5-hour limit" in d.body["error"]["message"]


def test_share_day_counts_the_weekly_bucket_since_the_day_began_and_resets_at_the_boundary(env):
    conn, cfg, ids, t = env
    day2 = t["starts_at"] + DAY
    req(conn, ids["alice"], day2 - 100, i=1000)
    fresh(conn, day2 - 50, util7=0.5)           # +0.5 in day 1
    req(conn, ids["alice"], day2 + 100, i=1000)
    fresh(conn, day2 + 200, util7=1.0)          # +0.5 in day 2
    assert by_kind(conn, cfg, ids["alice"], now=day2 - 10)["share_day"].current == pytest.approx(0.5)
    assert by_kind(conn, cfg, ids["alice"], now=day2 + 300)["share_day"].current == pytest.approx(0.5)   # day 1's use is gone
    assert check(conn, cfg, ids["alice"], now=day2 + 300) is None
    req(conn, ids["alice"], day2 + 400, i=1000)
    fresh(conn, day2 + 500, util7=1.3)          # 0.8 today: over 0.714
    d = check(conn, cfg, ids["alice"], now=day2 + 600)
    assert (d.status, d.kind, d.retry_after) == (429, "share_day", DAY - 600)
    assert d.body["error"]["message"] == "Today's share of your Lite ticket is used up. It resets at 07:55 UTC."   # the ticket started at 07:55


def test_a_slice_handed_over_mid_week_never_draws_more_than_its_share_from_one_week(db):
    """Alice's week ticket ends on Wednesday; Bob's starts then. In the Anthropic week that spans the handover,
    the slice's spend is capped at seven day-shares = the slice share."""
    conn, cfg, ids = seeded(db)
    cfg.tickets.tiers["lite"].share_pct = 7     # share_day = 1 point: whole numbers keep the arithmetic exact
    cfg.tickets.max_sold_pct = 7
    bob, _ = create_user(conn, "bob")
    a = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "USD", now=NOW)
    b = tickets.grant(conn, cfg, ids["admin"], user(conn, bob), "lite", "week", "USD", now=a["ends_at"])   # bob buys the slice the instant alice's ticket ends
    assert b["starts_at"] == a["ends_at"]
    cap = 0
    for day in range(7):                        # each ticket day, whoever holds the slice, allows share/7 at most
        t0 = NOW + 3 * DAY + day * DAY          # a 7-day span starting mid-ticket: 4 of alice's days, 3 of bob's
        holder = ids["alice"] if day < 4 else bob
        fresh(conn, t0 + 1, util7=cap)
        req(conn, holder, t0 + 10, i=1000)
        fresh(conn, t0 + 20, util7=cap + 1)
        cap += 1
        assert by_kind(conn, cfg, holder, now=t0 + 30)["share_day"].exceeded is True
    assert cap == 7                             # the slice drew exactly its share over those seven days, not 14


def test_bonus_share_raises_both_limits_and_bonus_days_continue_the_day_boundaries(env):
    conn, cfg, ids, t = env
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=2, starts_at=NOW, ends_at=NOW + DAY, now=NOW)
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], extra_days=1, now=NOW)
    st = by_kind(conn, cfg, ids["alice"], now=NOW + 10)
    assert st["share_5h"].limit == 7 and st["share_day"].limit == pytest.approx(1)
    st = by_kind(conn, cfg, ids["alice"], now=NOW + 2 * DAY)                  # bonus share over
    assert st["share_5h"].limit == 5
    late = t["ends_at"] + 3600                                                 # inside the bonus day
    fresh(conn, late - 100)
    st = by_kind(conn, cfg, ids["alice"], now=late)
    assert st["share_day"].reset_in == DAY - 3600                              # the day started at the old end


def test_no_active_ticket_is_a_403_with_the_right_message(env):
    conn, cfg, ids, t = env
    after = t["ends_at"] + 100
    fresh(conn, after - 50)
    d = check(conn, cfg, ids["alice"], now=after)
    assert (d.status, d.kind, d.body["error"]["type"]) == (403, "ticket", "permission_error")
    assert d.body["error"]["message"] == f"Your ticket ended on {limits._date(t['ends_at'])}. Send the amount by bank transfer and email the admin."
    assert check(conn, cfg, ids["alice"], now=after, model=None, path="/v1/models").status == 403   # every request
    nxt = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=after)
    conn.execute("UPDATE tickets SET starts_at=?, ends_at=? WHERE id=?", (after + DAY, after + 2 * DAY, nxt["id"]))
    d = check(conn, cfg, ids["alice"], now=after + 10)
    assert d.body["error"]["message"] == f"Your next ticket starts on {limits._date(after + DAY)}."
    assert by_kind(conn, cfg, ids["alice"], now=after + 10) == {}             # nothing to show between tickets


def test_cancelled_ticket_message_uses_the_cancel_time(env):
    conn, cfg, ids, t = env
    tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW)
    d = check(conn, cfg, ids["alice"], now=NOW + 10)
    assert d.body["error"]["message"].startswith(f"Your ticket ended on {limits._date(NOW)}.")


def test_third_party_models_refused_unless_a_cost_limit_exists(env):
    conn, cfg, ids, t = env
    d = check(conn, cfg, ids["alice"], model="muse-spark")
    assert (d.status, d.kind, d.body["error"]["message"]) == (403, "ticket", "Your ticket covers Claude models only.")
    set_limit(conn, ids["alice"], "cost_daily", 1.0, "usd")
    assert check(conn, cfg, ids["alice"], model="muse-spark") is None
    req(conn, ids["alice"], NOW - 60, i=1_000_000, model="muse-spark")        # $1.25 today
    assert check(conn, cfg, ids["alice"], model="muse-spark").kind == "cost_daily"


def test_other_limit_rows_still_apply(env):
    conn, cfg, ids, t = env
    set_limit(conn, ids["alice"], "allowed_models", "claude-sonnet-*", "list")
    assert check(conn, cfg, ids["alice"], model="claude-opus-5").status == 403
    set_limit(conn, ids["alice"], "requests_daily", 1, "count")
    req(conn, ids["alice"], NOW - 60)
    assert check(conn, cfg, ids["alice"]).kind == "requests_daily"


def test_stale_snapshot_estimates_from_weighted_tokens_at_the_observed_rate(env):
    conn, cfg, ids, t = env
    # History: 1000 weighted tokens moved both buckets by 1 point -> rate 1000 per point. Both land inside the
    # ticket's first day (which started at NOW-300), so the day's weighted-since count picks them up too.
    req(conn, ids["alice"], NOW - 200, i=1000)
    fresh(conn, NOW - 100, util5=1, util7=1)
    mid = NOW + 2000                                                           # snapshots are now > 30 min old
    st = by_kind(conn, cfg, ids["alice"], now=mid)
    assert st["share_5h"].no_live_data and st["share_day"].no_live_data
    assert st["share_5h"].current == pytest.approx(1.0)                        # counted from the window's first snapshot
    assert st["share_day"].current == pytest.approx(1.0)                       # and from the day's start (NOW-300)
    later = NOW + 7200                                                         # the stale snapshot's 5-hour reset (NOW+3500) has passed
    req(conn, ids["alice"], later - 60, i=600)
    st = by_kind(conn, cfg, ids["alice"], now=later)
    assert st["share_5h"].current == pytest.approx(0.6)                        # 5-hour count restarts at that reset time
    assert st["share_day"].current == pytest.approx(1.6)                       # the day keeps counting: 1.6 > 0.714
    assert check(conn, cfg, ids["alice"], now=later).kind == "share_day"


def test_ticket_user_gets_503_when_no_rate_was_ever_observed(db):
    conn, cfg, ids = seeded(db)
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW)
    d = check(conn, cfg, ids["alice"], now=NOW + 10)                           # no snapshots at all
    assert (d.status, d.kind, d.retry_after) == (503, "ticket_no_data", 60)
    assert d.body["error"] == {"type": "api_error", "message": "Usage data is unavailable. Please retry in a minute."}
    assert by_kind(conn, cfg, ids["alice"], now=NOW + 10)["share_day"].skipped == limits.NO_RATE
    assert check(conn, cfg, ids["alice"], now=NOW + 10, path="/v1/messages/count_tokens") is None   # consumes no quota


def test_ungated_users_keep_the_old_stale_behaviour(db):
    conn, cfg, ids = seeded(db)
    set_limit(conn, ids["alice"], "share_5h", 1, "pct")
    assert check(conn, cfg, ids["alice"]) is None                              # stale: skipped, as today
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW)
    tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW + 1)
    tickets.ungate(conn, ids["admin"], ids["alice"], now=NOW + 2)
    assert check(conn, cfg, ids["alice"], now=NOW + 3) is None
    assert by_kind(conn, cfg, ids["alice"], now=NOW + 3)["share_5h"].skipped.startswith("no account snapshot")


def test_user_view_and_status_line_name_the_day_limit(env):
    from claude_proxy.web import _status_line
    conn, cfg, ids, t = env
    sts = limits.states(conn, cfg, ids["alice"], now=NOW)
    views = {v["kind"]: v for v in (limits.user_view(s) for s in sts)}
    assert set(views) == {"5h_limit", "today_limit"} and views["today_limit"]["limit"] == 100.0
    line = _status_line(user(conn, ids["alice"]), sts, None)
    assert line.startswith("alice · 5h 0% (resets in") and " · today 0% (resets in 23.9 h)" in line
