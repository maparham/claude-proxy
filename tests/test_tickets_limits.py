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
    assert check(conn, cfg, ids["alice"], now=after).body["error"]["message"] == f"Your ticket ended on {limits._date(t['ends_at'])}."
    cfg.listener.dashboard_url = "https://dash.example/"   # with a dashboard: where to buy the next one
    d = check(conn, cfg, ids["alice"], now=after)
    assert (d.status, d.kind, d.body["error"]["type"]) == (403, "ticket", "permission_error")
    assert d.body["error"]["message"] == f"Your ticket ended on {limits._date(t['ends_at'])}. Buy one at https://dash.example/dashboard"
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


def test_status_line_shows_each_week_of_a_ticket_and_the_whole(env):
    """A week ticket bought 3 days before Anthropic's reset spans two weeks: the line gives each week's use and the
    ticket's, not the reset time."""
    from claude_proxy.web import _status_line
    conn, cfg, ids, t = env
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)   # 1000 weighted tokens per point
    sts = limits.states(conn, cfg, ids["alice"], now=NOW)
    weekly = next(s for s in sts if s.kind == "share_7d")
    weeks = limits.ticket_weeks(conn, cfg, ids["alice"], NOW, weekly)
    assert [w["state"] for w in weeks["weeks"]] == ["current", "future"]
    assert weeks["weeks"][0]["pct"] == pytest.approx(weekly.pct) and weeks["weeks"][1]["pct"] == 0
    assert weeks["ticket_pct"] == pytest.approx(weekly.pct * (3 * DAY + 300) / (7 * DAY))   # this week holds 3d + 300s of 7d
    line = _status_line(user(conn, ids["alice"]), sts, None, weeks)
    assert line.endswith(f" · {weekly.pct:.0f}% of first week · 0% of second week · {weeks['ticket_pct']:.0f}% used "
                         "(6d:23h left)")   # granted 300 s ago
    assert "First week ended" not in line and line.count("resets in") == 1   # only the 5-hour limit's
    later = limits.ticket_weeks(conn, cfg, ids["alice"], NOW + 4 * DAY)   # into the second week: the first is over
    assert [w["state"] for w in later["weeks"]] == ["past", "current"]
    assert "First week ended · " in limits_line(conn, cfg, ids, later)


def test_days_and_hours():
    from claude_proxy.web import _days_hours
    assert [_days_hours(s) for s in (3 * DAY + 5 * 3600 + 59, DAY, 2 * 3600, 2400)] == ["3d:5h", "1d:0h", "0d:2h", "0d:0h"]


def limits_line(conn, cfg, ids, weeks):
    from claude_proxy.web import _ticket_weeks_text
    return _ticket_weeks_text(weeks, "")


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
    assert d.body["error"]["message"] == "You have no ticket."


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


def test_set_share_5h_changes_only_the_5h_share(db):
    conn, cfg, ids = seeded(db)
    assert tickets.effective_share(conn, cfg, "lite") == 5
    assert tickets.effective_share_5h(conn, cfg, "lite") == 5
    tickets.set_share_5h(conn, cfg, "lite", 8, ids["admin"])
    assert tickets.effective_share_5h(conn, cfg, "lite") == 8
    assert tickets.effective_share(conn, cfg, "lite") == 5


def test_granted_ticket_stores_split_shares_for_5h_and_7d(db):
    conn, cfg, ids = seeded(db)
    tickets.set_share_5h(conn, cfg, "lite", 9, ids["admin"])
    fresh(conn, NOW - 600)
    t = grant_week(conn, cfg, ids, ids["alice"], now=NOW - 300)
    row = conn.execute("SELECT share_pct, share_5h_pct FROM tickets WHERE id=?", (t["id"],)).fetchone()
    assert (row["share_pct"], row["share_5h_pct"]) == (5, 9)
    st = by_kind(conn, cfg, ids["alice"])
    assert st["share_5h"].limit == 9
    assert st["share_7d"].limit == pytest.approx(piece(5, 3 * DAY + 300))


def test_null_share_5h_pct_falls_back_to_share_pct(db):
    conn, cfg, ids = seeded(db)
    tickets.set_share_5h(conn, cfg, "lite", 9, ids["admin"])
    fresh(conn, NOW - 600)
    t = grant_week(conn, cfg, ids, ids["alice"], now=NOW - 300)
    conn.execute("UPDATE tickets SET share_5h_pct=NULL WHERE id=?", (t["id"],))
    assert by_kind(conn, cfg, ids["alice"])["share_5h"].limit == 5


def test_set_share_5h_rejects_bad_values(db):
    conn, cfg, ids = seeded(db)
    with pytest.raises(tickets.TicketError):
        tickets.set_share_5h(conn, cfg, "lite", 0, ids["admin"])
    with pytest.raises(tickets.TicketError):
        tickets.set_share_5h(conn, cfg, "lite", 101, ids["admin"])
    with pytest.raises(tickets.TicketError):
        tickets.set_share_5h(conn, cfg, "nope", 5, ids["admin"])


def test_order_split_gives_each_week_its_fraction_of_the_ticket(db):
    """The order terms: a week ticket starting a day into Anthropic's week holds 1/7 there and 6/7 in the next."""
    conn, cfg, ids = seeded(db)
    assert tickets.order_split(conn, cfg, NOW, now=NOW) is None   # no weekly reset known yet
    fresh(conn, NOW - 600)
    s = tickets.order_split(conn, cfg, RESET7 - DAY, now=NOW)
    assert s["first_reset"] == RESET7 and s["week"] == pytest.approx([1 / 7, 6 / 7])
    assert s["day"] == pytest.approx([1.0]) and sum(s["month"]) == pytest.approx(1.0)
    later = tickets.order_split(conn, cfg, RESET7 + 2 * DAY, now=NOW)   # queued past this reset: the next one is first
    assert later["first_reset"] == RESET7 + 7 * DAY and later["week"] == pytest.approx([5 / 7, 2 / 7])


def test_terms_text_is_the_order_dialogs_terms(db):
    """The buyer's emails repeat the dialog's terms: the weekly split with dates (Tehran time), then the 5-hour line."""
    conn, cfg, ids = seeded(db)
    assert tickets.terms_text(conn, cfg, "en", "week", NOW, now=NOW) == (   # no weekly reset known: the general line
        "Ticket terms:\n- " + tickets.TERMS["en"]["l1"] + "\n- Anthropic also applies its own 5-hour limit.\n")
    fresh(conn, NOW - 600)
    # RESET7 - DAY is 2027-01-17 11:30 Tehran; the reset a day later, the ticket's end a week after its start
    assert tickets.terms_text(conn, cfg, "en", "week", RESET7 - DAY, now=NOW) == (
        "Ticket terms:\n- Your use of the ticket will be limited in two parts: 14% until Monday, January 18 at 11:30 AM "
        "and 86% until Sunday, January 24 at 11:30 AM.\n- Anthropic also applies its own 5-hour limit.\nTimes are Tehran time.\n")
    assert tickets.terms_text(conn, cfg, "fa", "week", RESET7 - DAY, now=NOW) == (
        "شرایط تیکت:\n- استفاده شما از تیکت در دو قسمت محدود خواهد بود: ۱۴٪ تا دوشنبه ۲۸ دی، ساعت ۱۱:۳۰ و ۸۶٪ تا یکشنبه ۴ بهمن، "
        "ساعت ۱۱:۳۰.\n- به علاوه، محدودیت‌های ۵ ساعته هم از طرف آنتروپیک اعمال می‌شود.\nساعت‌ها به وقت تهران است.\n")
    assert "کل تیکت شما تا" in tickets.terms_text(conn, cfg, "fa", "day", RESET7 - DAY, now=NOW)


def test_buyer_mail_html_draws_the_split(db):
    """The email's HTML copy: the body, with its terms' split drawn as a bar of one cell per week, as wide as its share."""
    conn, cfg, ids = seeded(db)
    fresh(conn, NOW - 600)
    terms = tickets.terms_text(conn, cfg, "fa", "week", RESET7 - DAY, now=NOW)
    body = f"سفارش شما <b>\n\n{terms}پایان\n"
    h = tickets.mail_html(conn, cfg, "fa", "week", RESET7 - DAY, body)
    assert h.startswith('<div dir="rtl"') and "&lt;b&gt;" in h and "پایان" in h
    assert h.count('width="14.3%"') == 2 and h.count('width="85.7%"') == 2   # a label cell and a bar cell per part
    assert ">۱۴٪</td>" in h and ">هفته دوم: ۸۶٪</td>" in h   # 14% is too narrow for its week
    assert "border-radius:2px\">&nbsp;</td>" in h
    assert tickets.mail_html(conn, cfg, "en", "week", RESET7 - DAY, "Hi\n").startswith('<div dir="ltr"')


def test_jalali_dates():
    assert tickets.jalali(2026, 10, 25) == (1405, 8, 3)
    assert tickets.jalali(2025, 3, 20) == (1403, 12, 30) and tickets.jalali(2025, 3, 21) == (1404, 1, 1)


def test_set_compare_sets_and_clears_the_plan_comparison(db):
    conn, cfg, ids = seeded(db)
    assert (tickets.effective_tiers(conn, cfg)["lite"]["compare"], tickets.effective_tiers(conn, cfg)["lite"]["compare_ratio"]) == ("", 0)
    assert tickets.set_compare(conn, cfg, "lite", " Claude Pro ", 10, ids["admin"]) == {"compare": "Claude Pro", "compare_ratio": 10}
    eff = tickets.effective_tiers(conn, cfg)["lite"]
    assert (eff["compare"], eff["compare_ratio"], eff["share_pct"]) == ("Claude Pro", 10, 5)   # the share is untouched
    tickets.set_share(conn, cfg, "lite", 6, ids["admin"])
    assert tickets.compare_plan(conn, "lite") == ("Claude Pro", 10)   # and a share change keeps the comparison
    assert tickets.set_compare(conn, cfg, "lite", "", None, ids["admin"]) == {"compare": "", "compare_ratio": 0}
    assert tickets.compare_plan(conn, "lite") is None
    assert [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action='compare_set'")] == ["compare_set", "compare_set"]


def test_set_compare_rejects_bad_values(db):
    conn, cfg, ids = seeded(db)
    for plan, ratio in (("Claude Pro", 0), ("Claude Pro", None), ("Claude Pro", float("nan")), ("x" * 41, 2), (5, 2)):
        with pytest.raises(tickets.TicketError):
            tickets.set_compare(conn, cfg, "lite", plan, ratio, ids["admin"])
    with pytest.raises(tickets.TicketError):
        tickets.set_compare(conn, cfg, "nope", "Claude Pro", 2, ids["admin"])
