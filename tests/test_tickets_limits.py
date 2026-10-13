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
WEEK_START = RESET7 - 7 * DAY                # the current Anthropic week: [NOW - 4d, NOW + 3d)


def fresh(conn, t, util5=0, util7=0):
    """A pair of account snapshots at t, so share limits are measured, not skipped."""
    snap(conn, t, util5, "5h", RESET5)
    snap(conn, t, util7, "7d", RESET7)


def check(conn, cfg, uid, now=NOW, model="claude-sonnet-5", path="/v1/messages"):
    return limits.evaluate(conn, cfg, uid, model, path, now=now)


def by_kind(conn, cfg, uid, now=NOW):
    return {s.kind: s for s in limits.states(conn, cfg, uid, now=now)}


def grant_week(conn, cfg, ids, uid, **kw):
    kw.setdefault("terms_version", "v1")
    return tickets.grant(conn, cfg, ids["admin"], user(conn, uid), "lite", "week", "EUR", **kw)


@pytest.fixture
def env(db):
    conn, cfg, ids = seeded(db)
    fresh(conn, NOW - 600)
    t = grant_week(conn, cfg, ids, ids["alice"], now=NOW - 300)   # 5%, current-week piece 5 x (3d + 300s) / 7d
    return conn, cfg, ids, t


def piece(share, seconds):
    return share * seconds / DAY / 7


def test_non_ticket_users_are_untouched(db):
    conn, cfg, ids = seeded(db)
    set_limit(conn, ids["alice"], "share_5h", 20, "pct")
    assert check(conn, cfg, ids["alice"]) is None
    assert list(by_kind(conn, cfg, ids["alice"])) == ["share_5h"]


def test_ticket_builds_5h_and_7d_states_in_place_of_admin_share_rows(env):
    conn, cfg, ids, t = env
    set_limit(conn, ids["alice"], "share_5h", 50, "pct")
    set_limit(conn, ids["alice"], "share_7d", 50, "pct")
    set_limit(conn, ids["alice"], "requests_daily", 100, "count")
    st = by_kind(conn, cfg, ids["alice"])
    assert set(st) == {"requests_daily", "share_5h", "share_7d"}
    assert (st["share_5h"].limit, st["share_5h"].tier) == (5, "Lite")
    assert st["share_7d"].limit == pytest.approx(piece(5, 3 * DAY + 300))
    assert (st["share_7d"].current, st["share_7d"].reset_in, st["share_7d"].resets_at) == (0, RESET7 - NOW, RESET7)
    assert st["share_7d"].to_dict()["no_live_data"] is False


def test_share_5h_measured_as_today_and_exceeded_is_the_usual_429(env):
    conn, cfg, ids, t = env
    req(conn, ids["alice"], NOW - 200, i=1000)
    fresh(conn, NOW - 100, util5=6)             # alice alone: +6 points of the 5-hour bucket
    d = check(conn, cfg, ids["alice"])
    assert (d.status, d.kind, d.retry_after) == (429, "share_5h", 3500)
    assert "5-hour limit" in d.body["error"]["message"]


def test_share_7d_counts_the_weekly_bucket_since_the_piece_began(env):
    """800 input tokens at the observed rate of 1000 per point: 0.8 points against a ~2.145 piece."""
    conn, cfg, ids, t = env
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)   # history: 1000 weighted tokens per point (before the ticket: not counted)
    req(conn, ids["alice"], NOW - 200, i=800)
    fresh(conn, NOW - 100, util5=1, util7=1)   # no new step yet: attribution says 0 since the piece began
    st = by_kind(conn, cfg, ids["alice"])
    assert st["share_7d"].current == pytest.approx(0.8) and st["share_7d"].no_live_data is False
    assert check(conn, cfg, ids["alice"]) is None   # 0.8 < ~2.145: the burst fits inside the piece
    req(conn, ids["alice"], NOW - 50, i=1500)
    assert check(conn, cfg, ids["alice"]).kind == "share_7d"   # 2.3 > ~2.145: past the piece


def test_share_7d_exceeded_is_a_429_with_the_full_week_retry_after(env):
    conn, cfg, ids, t = env
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)
    req(conn, ids["alice"], NOW - 200, i=2500)
    d = check(conn, cfg, ids["alice"])
    assert (d.status, d.kind, d.retry_after) == (429, "share_7d", RESET7 - NOW)
    assert d.body["error"]["message"] == ("Weekly share of your Lite ticket is used up. "
                                          "It resets at 2027-01-18 08:00 UTC.")


def test_last_weeks_use_is_gone_after_the_reset(env):
    """Usage before the week start never counts, however the ticket straddles the reset."""
    conn, cfg, ids, t = env
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)   # rate: 1000 per point
    after = RESET7 + 600                        # the stale snapshot's reset has passed: the week rolled forward
    req(conn, ids["alice"], after - 100, i=500)
    st = by_kind(conn, cfg, ids["alice"], now=after)
    assert st["share_7d"].resets_at == RESET7 + 7 * DAY
    assert st["share_7d"].current == pytest.approx(0.5)   # only the new piece's use
    assert check(conn, cfg, ids["alice"], now=after) is None


def test_a_ticket_starting_at_the_reset_gets_the_full_share(db):
    conn, cfg, ids = seeded(db)
    fresh(conn, NOW - 600)
    t = grant_week(conn, cfg, ids, ids["alice"], now=NOW - 300)
    conn.execute("UPDATE tickets SET starts_at=?, ends_at=? WHERE id=?", (WEEK_START, WEEK_START + 7 * DAY, t["id"]))
    assert by_kind(conn, cfg, ids["alice"])["share_7d"].limit == pytest.approx(5)


def test_day_ticket_inside_one_week_gets_share_over_7(db):
    conn, cfg, ids = seeded(db)
    fresh(conn, NOW - 600)
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD",
                  now=WEEK_START + DAY, terms_version="v1")
    st = by_kind(conn, cfg, ids["alice"], now=WEEK_START + DAY + 3600)["share_7d"]
    assert st.limit == pytest.approx(5 / 7)


def test_month_ticket_covers_five_weeks(db):
    """week_split: full precision per week; the display rounds, enforcement does not."""
    conn, cfg, ids = seeded(db)
    fresh(conn, NOW - 600)
    parts = tickets.week_split(conn, cfg, 5, WEEK_START + 3 * DAY, WEEK_START + 33 * DAY, now=NOW)
    assert len(parts) == 5
    assert parts[0] == pytest.approx(piece(5, 4 * DAY)) and parts[-1] == pytest.approx(piece(5, 5 * DAY))
    assert sum(parts) == pytest.approx(piece(5, 30 * DAY))
    assert tickets.week_split(conn, cfg, 5, WEEK_START, RESET7, now=NOW) == pytest.approx([5])   # a full week: the whole share
    assert tickets.week_split(conn, cfg, 5, RESET7 - 5 * DAY, RESET7 + 2 * DAY, now=NOW) == pytest.approx([piece(5, 5 * DAY), piece(5, 2 * DAY)])
    assert tickets.week_split(conn, cfg, 5, RESET7 - 3600, RESET7 + 3600, now=NOW) == pytest.approx([piece(5, 3600), piece(5, 3600)])


def test_week_split_without_quota_data_is_none(db):
    conn, cfg, ids = seeded(db)
    assert tickets.week_split(conn, cfg, 5, NOW, NOW + 7 * DAY, now=NOW) is None


def test_week_bounds_roll_forward_and_snapshot_wins(db):
    conn, cfg, ids = seeded(db)
    snap(conn, NOW - 2 * DAY, 10, "7d", NOW - DAY)   # reset a day ago: stale, but the boundaries are still known
    assert tickets.week_bounds(conn, cfg, NOW) == (NOW - DAY, NOW - DAY + 7 * DAY)
    snap(conn, NOW - 100, 11, "7d", NOW + 6 * DAY)   # a fresh snapshot disagrees: it wins, never the projection
    assert tickets.week_bounds(conn, cfg, NOW) == (NOW - DAY, NOW + 6 * DAY)
    assert tickets.week_bounds(conn, cfg, NOW + 7 * DAY) == (NOW + 6 * DAY, NOW + 13 * DAY)   # past again: rolled forward


def test_week_bounds_unknown_without_a_reset_time(db):
    conn, cfg, ids = seeded(db)
    assert tickets.week_bounds(conn, cfg, NOW) is None   # no snapshots at all
    snap(conn, NOW - 100, 0, "7d", None)                 # snapshots, but no reset time ever reported
    assert tickets.week_bounds(conn, cfg, NOW) is None


def test_a_slice_handed_over_mid_week_draws_at_most_its_share_from_one_week(db):
    """Alice's week ticket ends mid-week; Bob's starts then. Their pieces of the shared week add up to the slice share."""
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 7
    tickets.set_share(conn, cfg, "lite", 7, ids["admin"])
    fresh(conn, NOW - 600)
    bob, _ = create_user(conn, "bob")
    mid = WEEK_START + 4 * DAY
    a = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "USD", now=WEEK_START, terms_version="v1")
    conn.execute("UPDATE tickets SET starts_at=?, ends_at=? WHERE id=?", (WEEK_START, mid, a["id"]))
    b = tickets.grant(conn, cfg, ids["admin"], user(conn, bob), "lite", "week", "USD", now=mid, terms_version="v1")
    conn.execute("UPDATE tickets SET starts_at=?, ends_at=? WHERE id=?", (mid, mid + 7 * DAY, b["id"]))
    sa = by_kind(conn, cfg, ids["alice"], now=mid - 10)["share_7d"]
    sb = by_kind(conn, cfg, bob, now=mid + 10)["share_7d"]
    assert sa.limit == pytest.approx(4) and sb.limit == pytest.approx(3)
    assert sa.limit + sb.limit == pytest.approx(7)   # the shared week yields the slice share, not twice it


def test_consecutive_day_tickets_each_allow_share_over_7(db):
    """Seven day tickets back to back inside one Anthropic week: each piece is share/7, the week draws 7 x share/7."""
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 7
    tickets.set_share(conn, cfg, "lite", 7, ids["admin"])
    fresh(conn, NOW - 600)
    bob, _ = create_user(conn, "bob")
    for day in range(7):
        holder = ids["alice"] if day < 4 else bob
        tickets.grant(conn, cfg, ids["admin"], user(conn, holder), "lite", "day", "USD",
                      now=WEEK_START + day * DAY, terms_version="v1")
    for day in range(7):
        holder = ids["alice"] if day < 4 else bob
        st = by_kind(conn, cfg, holder, now=WEEK_START + day * DAY + 3600)["share_7d"]
        assert st.limit == pytest.approx(1)


def test_same_user_renewal_gets_a_fresh_piece(db):
    """Usage under the old ticket does not follow the user into the renewed ticket's piece."""
    conn, cfg, ids = seeded(db)
    fresh(conn, NOW - 600)
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)   # rate: 1000 per point
    req(conn, ids["alice"], NOW - 200, i=2000)   # 3 points: past any day-ticket piece
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW - 300, terms_version="v1")
    assert check(conn, cfg, ids["alice"], now=NOW - 100).kind == "share_7d"
    nxt = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD",
                        now=t["ends_at"], terms_version="v1")   # queued renewal
    at = nxt["starts_at"] + 100
    fresh(conn, at - 50, util7=1)
    st = by_kind(conn, cfg, ids["alice"], now=at)["share_7d"]
    assert st.current == 0.0 and check(conn, cfg, ids["alice"], now=at) is None


def test_bonus_share_raises_the_piece_while_active_only(env):
    conn, cfg, ids, t = env
    base = by_kind(conn, cfg, ids["alice"])["share_7d"].limit
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=2, starts_at=NOW, ends_at=NOW + DAY, now=NOW)
    st = by_kind(conn, cfg, ids["alice"], now=NOW + 10)
    assert st["share_5h"].limit == 7
    assert st["share_7d"].limit == pytest.approx(base + piece(2, DAY))
    st = by_kind(conn, cfg, ids["alice"], now=NOW + 2 * DAY)   # bonus share over
    assert st["share_5h"].limit == 5
    assert st["share_7d"].limit == pytest.approx(base)


def test_future_and_past_bonuses_do_not_widen_the_cap(env):
    conn, cfg, ids, t = env
    base = by_kind(conn, cfg, ids["alice"])["share_7d"].limit
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=2, starts_at=NOW + DAY, ends_at=NOW + 2 * DAY, now=NOW)
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=2, starts_at=NOW - 300, ends_at=NOW - 100, now=NOW)
    assert by_kind(conn, cfg, ids["alice"], now=NOW + 10)["share_7d"].limit == pytest.approx(base)


def test_spending_past_a_dead_bonus_blocks_the_rest_of_the_week(db):
    """Bonus-period spend stays drawn after the bonus ends: the narrower ticket-only piece stays exceeded,
    and the bar reads over 100% until the reset."""
    conn, cfg, ids = seeded(db)
    fresh(conn, NOW - 600)
    t = grant_week(conn, cfg, ids, ids["alice"], now=NOW - 300)
    base = by_kind(conn, cfg, ids["alice"])["share_7d"].limit
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=70, starts_at=NOW - 200, ends_at=NOW + 100, now=NOW - 200)
    wide = by_kind(conn, cfg, ids["alice"])["share_7d"].limit
    assert wide > base
    req(conn, ids["alice"], NOW - 150, i=1000)
    fresh(conn, NOW - 120, util5=1, util7=1)   # rate: 1000 per point
    req(conn, ids["alice"], NOW - 50, i=1160)   # 2.16 points: past the ~2.145 ticket-only piece, inside the widened one
    assert check(conn, cfg, ids["alice"]) is None
    st = by_kind(conn, cfg, ids["alice"], now=NOW + 200)   # the bonus is over, same week
    assert st["share_7d"].limit == pytest.approx(base)
    assert st["share_7d"].current == pytest.approx(2.16)
    assert st["share_7d"].pct > 100
    d = check(conn, cfg, ids["alice"], now=NOW + 200)
    assert (d.status, d.kind) == (429, "share_7d")


def test_use_before_a_bonus_eats_into_the_bonus_piece(db):
    """Pre-bonus spend counts against the widened cap once the bonus activates."""
    conn, cfg, ids = seeded(db)
    fresh(conn, NOW - 600)
    t = grant_week(conn, cfg, ids, ids["alice"], now=NOW - 300)
    base = by_kind(conn, cfg, ids["alice"])["share_7d"].limit
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)   # rate: 1000 per point (before the ticket: not counted)
    req(conn, ids["alice"], NOW - 200, i=2200)   # 2.2 points: past the ~2.145 ticket-only piece
    assert check(conn, cfg, ids["alice"]).kind == "share_7d"
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=7, starts_at=NOW, ends_at=NOW + DAY, now=NOW)
    st = by_kind(conn, cfg, ids["alice"], now=NOW + 10)
    assert st["share_7d"].current == pytest.approx(2.2)   # nothing was forgiven
    assert check(conn, cfg, ids["alice"], now=NOW + 10) is None   # but the bonus piece covers it


def test_bonus_days_extend_the_ticket_period(env):
    conn, cfg, ids, t = env
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], extra_days=1, now=NOW)
    late = t["ends_at"] + 3600                                                 # inside the bonus day
    fresh(conn, late - 100)
    st = by_kind(conn, cfg, ids["alice"], now=late)
    assert st["share_7d"].limit == pytest.approx(piece(5, 5 * DAY - 300))   # the extra day counts: past the old end


def test_no_active_ticket_is_a_403_with_the_right_message(env):
    conn, cfg, ids, t = env
    after = t["ends_at"] + 100
    fresh(conn, after - 50)
    d = check(conn, cfg, ids["alice"], now=after)
    assert (d.status, d.kind, d.body["error"]["type"]) == (403, "ticket", "permission_error")
    assert d.body["error"]["message"] == f"Your ticket ended on {limits._date(t['ends_at'])}. Send the amount by bank transfer and email the admin."
    assert check(conn, cfg, ids["alice"], now=after, model=None, path="/v1/models").status == 403   # every request
    nxt = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=after, terms_version="v1")
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
    # History: 1000 weighted tokens moved both buckets by 1 point -> rate 1000 per point.
    req(conn, ids["alice"], NOW - 200, i=1000)
    fresh(conn, NOW - 100, util5=1, util7=1)
    mid = NOW + 2000                                                           # snapshots are now > 30 min old
    st = by_kind(conn, cfg, ids["alice"], now=mid)
    assert st["share_5h"].no_live_data and st["share_7d"].no_live_data
    assert st["share_5h"].current == pytest.approx(1.0)                        # counted from the window's first snapshot
    assert st["share_7d"].current == pytest.approx(1.0)                       # and from the piece's start (NOW-300)
    later = NOW + 7200                                                         # the stale snapshot's 5-hour reset (NOW+3500) has passed
    req(conn, ids["alice"], later - 60, i=600)
    req(conn, ids["alice"], later - 30, i=1000)
    st = by_kind(conn, cfg, ids["alice"], now=later)
    assert st["share_5h"].current == pytest.approx(1.6)                        # 5-hour count restarts at that reset time
    assert st["share_7d"].current == pytest.approx(2.6)                       # the piece keeps counting: 2.6 > ~2.145
    assert check(conn, cfg, ids["alice"], now=later).kind == "share_7d"


def test_ticket_user_with_only_an_8_day_old_rate_gets_an_estimate_not_a_503(env):
    conn, cfg, ids, t = env
    old = NOW - 8 * DAY                                                         # a rate observed well outside the 7-day lookback
    req(conn, ids["alice"], old - 25, i=1000)
    fresh(conn, old - 50)                                                       # util 0, both buckets
    fresh(conn, old, util5=1, util7=1)                                          # +1 point: rate 1000 per point, 8 days before NOW
    mid = NOW + 2000                                                            # the fixture's own fresh() snapshot is stale by now too
    st = by_kind(conn, cfg, ids["alice"], now=mid)
    assert st["share_5h"].no_live_data is True and st["share_5h"].skipped is None
    assert st["share_7d"].no_live_data is True and st["share_7d"].skipped is None
    assert st["share_7d"].current == 0.0                                       # the ticket's piece has no usage of its own
    assert check(conn, cfg, ids["alice"], now=mid) is None                       # estimated from the old rate, not a 503


def test_ticket_user_gets_503_when_no_rate_was_ever_observed(db):
    conn, cfg, ids = seeded(db)
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW, terms_version="v1")
    d = check(conn, cfg, ids["alice"], now=NOW + 10)                           # no snapshots at all
    assert (d.status, d.kind, d.retry_after) == (503, "ticket_no_data", 60)
    assert d.body["error"] == {"type": "api_error", "message": "Usage data is unavailable. Please retry in a minute."}
    assert by_kind(conn, cfg, ids["alice"], now=NOW + 10)["share_7d"].skipped == limits.NO_RATE
    assert check(conn, cfg, ids["alice"], now=NOW + 10, path="/v1/messages/count_tokens") is None   # consumes no quota


def test_ticket_user_gets_503_when_no_reset_was_ever_reported(db):
    """Snapshots without a reset time: the week is unknown, so the piece cannot be capped."""
    conn, cfg, ids = seeded(db)
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW, terms_version="v1")
    snap(conn, NOW - 100, 0, "5h", None)
    snap(conn, NOW - 100, 0, "7d", None)
    d = check(conn, cfg, ids["alice"], now=NOW + 10)
    assert (d.status, d.kind, d.retry_after) == (503, "ticket_no_data", 60)
    assert by_kind(conn, cfg, ids["alice"], now=NOW + 10)["share_7d"].skipped == limits.NO_RATE


def test_ungated_users_keep_the_old_stale_behaviour(db):
    conn, cfg, ids = seeded(db)
    set_limit(conn, ids["alice"], "share_5h", 1, "pct")
    assert check(conn, cfg, ids["alice"]) is None                              # stale: skipped, as today
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW, terms_version="v1")
    tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW + 1)
    tickets.ungate(conn, ids["admin"], ids["alice"], now=NOW + 2)
    assert check(conn, cfg, ids["alice"], now=NOW + 3) is None
    assert by_kind(conn, cfg, ids["alice"], now=NOW + 3)["share_5h"].skipped.startswith("no account snapshot")


def test_user_view_and_status_line_name_the_week_limit(env):
    from claude_proxy.web import _status_line
    conn, cfg, ids, t = env
    sts = limits.states(conn, cfg, ids["alice"], now=NOW)
    views = {v["kind"]: v for v in (limits.user_view(s) for s in sts)}
    assert set(views) == {"5h_limit", "weekly_limit"} and views["weekly_limit"]["limit"] == 100.0
    line = _status_line(user(conn, ids["alice"]), sts, None)
    assert line.startswith("alice · 5h 0% (resets in") and " · week 0% (resets in 3.0 days)" in line


def test_status_line_marks_shares_estimated_without_live_data(env):
    from claude_proxy.web import _status_line
    conn, cfg, ids, t = env
    req(conn, ids["alice"], NOW - 200, i=1000)
    fresh(conn, NOW - 100, util5=1, util7=1)
    sts = limits.states(conn, cfg, ids["alice"], now=NOW + 2000)   # stale: estimated from weighted tokens
    line = _status_line(user(conn, ids["alice"]), sts, None)
    assert " · 5h 20% est. (resets in" in line and " · week 47% est. (resets in" in line


def test_share_7d_counts_the_users_own_tokens_while_snapshots_are_fresh(env):
    """Whole-percent steps: a day-ticket piece (0.71 points) is smaller than one step, so attribution could show 0
    all week. The piece is measured from the user's own tokens at the observed rate, live data or not."""
    conn, cfg, ids, t = env
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)   # history: 1000 weighted tokens per point
    req(conn, ids["alice"], NOW - 200, i=800)
    fresh(conn, NOW - 100, util5=1, util7=1)   # no new step yet: attribution says 0 since the piece began
    st = by_kind(conn, cfg, ids["alice"])
    assert st["share_7d"].current == pytest.approx(0.8) and st["share_7d"].no_live_data is False
    assert check(conn, cfg, ids["alice"]) is None   # 0.8 fits in the ~2.145 piece


def test_share_7d_counts_unpriced_models_like_the_rate_does(env):
    conn, cfg, ids, t = env
    req(conn, ids["alice"], NOW - 500, i=1000, model="mystery-model")
    fresh(conn, NOW - 400, util5=1, util7=1)
    req(conn, ids["alice"], NOW - 200, i=500, model="mystery-model")
    fresh(conn, NOW - 100, util5=1, util7=1)
    assert by_kind(conn, cfg, ids["alice"])["share_7d"].current == pytest.approx(0.5)


def test_a_cost_limit_for_other_models_does_not_open_third_party_models(env):
    conn, cfg, ids, t = env
    set_limit(conn, ids["alice"], "cost_daily", 1.0, "usd", scope="claude-*")
    d = check(conn, cfg, ids["alice"], model="muse-spark")
    assert (d.status, d.body["error"]["message"]) == (403, "Your ticket covers Claude models only.")
    set_limit(conn, ids["alice"], "cost_monthly", 5.0, "usd", scope="muse-*")
    assert check(conn, cfg, ids["alice"], model="muse-spark") is None


def test_tickets_switched_off_refuse_gated_users_instead_of_failing_open(env):
    conn, cfg, ids, t = env
    cfg.tickets.enabled = False                 # the grant removed alice's credit: no other limit would hold her
    d = check(conn, cfg, ids["alice"])
    assert (d.status, d.kind, d.body["error"]["message"]) == (403, "ticket", "Tickets are paused; ask the admin.")
    assert check(conn, cfg, ids["alice"], model=None, path="/v1/models").status == 403
    tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW)
    tickets.ungate(conn, ids["admin"], ids["alice"], now=NOW + 1)
    assert check(conn, cfg, ids["alice"], now=NOW + 2) is None   # ungated: back to hand-set limits
    bob, _ = create_user(conn, "bob")
    assert check(conn, cfg, bob) is None        # never had a ticket: untouched


def test_a_ticket_cancelled_before_it_started_is_like_having_no_ticket(db):
    conn, cfg, ids = seeded(db)
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW, terms_version="v1")
    conn.execute("UPDATE tickets SET starts_at=?, ends_at=? WHERE id=?", (NOW + DAY, NOW + 2 * DAY, t["id"]))   # queued
    tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW + 10)
    d = check(conn, cfg, ids["alice"], now=NOW + 20)
    assert d.status == 403
    assert d.body["error"]["message"] == "You have no ticket. Send the amount by bank transfer and email the admin."


def test_retry_after_and_reset_in_round_up(env):
    conn, cfg, ids, t = env
    fresh(conn, NOW - 60, util5=10)
    req(conn, ids["alice"], NOW - 120, i=1000)
    fresh(conn, NOW - 30, util5=20)
    now = NOW + 0.5                                                            # RESET5 - now = 3499.5
    st = by_kind(conn, cfg, ids["alice"], now=now)
    assert st["share_5h"].reset_in == 3500 and st["share_7d"].reset_in == RESET7 - NOW
    assert check(conn, cfg, ids["alice"], now=now).retry_after == 3500


def test_stale_share_5h_counts_at_most_the_last_5_hours(env):
    conn, cfg, ids, t = env
    req(conn, ids["alice"], NOW - 200, i=1000)
    fresh(conn, NOW - 100, util5=1, util7=1)                                   # rate: 1000 per point
    later = RESET5 + 7 * 3600                                                  # the stale reset passed 7 hours ago
    req(conn, ids["alice"], later - 6 * 3600, i=500)                           # 6 hours ago: an earlier 5-hour window
    req(conn, ids["alice"], later - 3600, i=300)
    assert by_kind(conn, cfg, ids["alice"], now=later)["share_5h"].current == pytest.approx(0.3)


def test_a_young_account_with_fresh_snapshots_does_not_lock_out_its_first_ticket_user(db):
    # Utilization has never risen a whole point, so there is no rate; the user's own requests are all that could raise it.
    conn, cfg, ids = seeded(db)
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW - 300, terms_version="v1")
    for k in range(5):
        fresh(conn, NOW - 240 + 60 * k)
        req(conn, ids["alice"], NOW - 230 + 60 * k, i=20000)
    assert check(conn, cfg, ids["alice"], now=NOW + 10) is None
    st = by_kind(conn, cfg, ids["alice"], now=NOW + 10)["share_7d"]
    assert (st.current, st.skipped, st.no_live_data) == (0, None, False)
