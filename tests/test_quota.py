import time

import pytest

from claude_proxy import quota
from claude_proxy.config import Pricing
from claude_proxy.db import create_user


def test_headers_are_fractions_converted_to_percent():
    snaps = quota.parse_headers({
        "anthropic-ratelimit-unified-5h-utilization": "0.01",
        "anthropic-ratelimit-unified-5h-reset": "1790000000",
        "anthropic-ratelimit-unified-5h-status": "allowed",
        "anthropic-ratelimit-unified-7d-utilization": "0.81",
        "anthropic-ratelimit-unified-7d-reset": "1790500000",
        "anthropic-ratelimit-unified-representative-claim": "seven_day",
        "content-type": "application/json",
    })
    by = {s.bucket: s for s in snaps}
    assert by["5h"].utilization_pct == pytest.approx(1.0)   # a true 1%, not 100%
    assert by["5h"].resets_at == 1790000000
    assert by["5h"].status == "allowed"
    assert by["7d"].utilization_pct == pytest.approx(81.0)
    assert all(s.source == "header" for s in snaps)


def test_headers_without_unified_fields_yield_nothing():
    assert quota.parse_headers({"content-type": "application/json"}) == []


def test_poll_body_is_percent_with_iso_resets():
    snaps = quota.parse_poll({
        "five_hour": {"utilization": 1.0, "resets_at": "2026-09-22T15:00:00+00:00"},
        "seven_day": {"utilization": 38, "resets_at": 1790500000},
        "seven_day_opus": None,
        "seven_day_sonnet": {"used_percentage": 12.5, "reset_at": "2026-09-25T00:00:00Z"},
        "extra_usage": {"is_enabled": False},
        "nimbus_quill": {"utilization": 0.0, "resets_at": None},   # live responses carry code-named entries
    })
    by = {s.bucket: s for s in snaps}
    assert by["5h"].utilization_pct == pytest.approx(1.0)
    assert by["5h"].resets_at == 1790089200
    assert by["7d"].utilization_pct == 38
    assert by["7d_sonnet"].utilization_pct == 12.5
    assert "7d_opus" not in by and "extra_usage" not in by and "nimbus_quill" not in by


def test_reset_in_milliseconds_is_normalised():
    assert quota.to_epoch(1790000000000) == 1790000000


def _snap(conn, t, util, resets=10_000_000):
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                 (t, "header", "5h", util, resets))


def _req(conn, uid, t, input_tokens, model="claude-sonnet-5", provider="anthropic"):
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, input_tokens) VALUES(?,?,?,?,?,?,?,?)",
                 (uid, t - 1, t, "POST", "/v1/messages", provider, model, input_tokens))


def test_attribution_splits_delta_by_weighted_tokens(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    b, _ = create_user(conn, "b")
    _snap(conn, 100, 10)
    _req(conn, a, 150, 3000)
    _req(conn, b, 160, 1000)
    _snap(conn, 200, 30)   # +20 split 3:1
    res = quota.attribution(conn, Pricing(), "5h", now=210)
    assert res["shares"][a] == pytest.approx(15)
    assert res["shares"][b] == pytest.approx(5)
    assert res["unattributed"] == pytest.approx(10)   # the 10% present before the first snapshot
    assert res["utilization_pct"] == 30


def test_attribution_single_active_user_gets_whole_delta(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    _snap(conn, 100, 0)
    _req(conn, a, 150, 10)
    _snap(conn, 200, 7)
    assert quota.attribution(conn, Pricing(), "5h", now=210)["shares"][a] == pytest.approx(7)


def test_attribution_no_activity_leaves_delta_unattributed(db):
    conn = db[1]
    _snap(conn, 100, 5)
    _snap(conn, 200, 9)
    res = quota.attribution(conn, Pricing(), "5h", now=210)
    assert res["shares"] == {}
    assert res["unattributed"] == pytest.approx(9)


def test_attribution_restarts_after_reset(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    _snap(conn, 100, 10, resets=1000)
    _req(conn, a, 150, 10)
    _snap(conn, 200, 50, resets=1000)
    _snap(conn, 1100, 2, resets=19000)     # new window
    _req(conn, a, 1150, 10)
    _snap(conn, 1200, 6, resets=19000)
    res = quota.attribution(conn, Pricing(), "5h", now=1210)
    assert res["shares"][a] == pytest.approx(4)
    assert res["unattributed"] == pytest.approx(2)
    assert res["resets_at"] == 19000


def test_attribution_ignores_third_party_requests(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    b, _ = create_user(conn, "b")
    _snap(conn, 100, 0)
    _req(conn, a, 150, 10)
    _req(conn, b, 150, 10_000_000, model="muse-spark-1.3", provider="meta")
    _snap(conn, 200, 4)
    res = quota.attribution(conn, Pricing(), "5h", now=210)
    assert res["shares"] == {a: pytest.approx(4)}


def test_attribution_is_stale_without_recent_snapshot(db):
    conn = db[1]
    _snap(conn, 100, 5)
    assert quota.attribution(conn, Pricing(), "5h", now=100 + 3600, stale_after_s=1800)["stale"] is True
    assert quota.attribution(conn, Pricing(), "7d", now=100)["utilization_pct"] is None


def test_record_skips_unchanged_snapshots_within_a_minute(db):
    conn = db[1]
    s = quota.Snapshot("5h", 10.0, 1000, "allowed", "header", {})
    quota.record(conn, [s], now=100)
    quota.record(conn, [s], now=130)
    quota.record(conn, [quota.Snapshot("5h", 11.0, 1000, "allowed", "header", {})], now=140)
    quota.record(conn, [s], now=300)
    assert conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0] == 3


@pytest.mark.parametrize("headers,kind", [
    ({"anthropic-ratelimit-unified-status": "rejected", "retry-after": "100"}, "upstream_quota"),
    ({"anthropic-ratelimit-requests-remaining": "0", "retry-after": "5"}, "upstream_throttle"),
    ({}, "upstream_request_scoped"),
])
def test_classify_429(headers, kind):
    assert quota.classify_429(headers) == kind


class FakeBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def poll_usage(self):
        self.calls += 1
        return self.responses.pop(0)


async def test_poller_only_polls_when_idle_and_backs_off_on_429(db):
    from claude_proxy.config import QuotaConfig
    conn = db[1]
    qc = QuotaConfig(poll_idle_s=600, poll_min_interval_s=180, poll_max_backoff_s=1800)
    be = FakeBackend([(429, None), (429, None), (200, {"five_hour": {"utilization": 3, "resets_at": 5000}})])
    p = quota.Poller(conn, be, qc)
    _snap(conn, 1000, 1)
    await p.tick(now=1100)             # a header snapshot is fresh: no poll
    assert be.calls == 0
    await p.tick(now=1700)             # idle 700s: poll -> 429, backoff 360
    assert be.calls == 1
    await p.tick(now=1900)             # within backoff
    assert be.calls == 1
    await p.tick(now=2061)             # after 360s: poll -> 429, backoff 720
    assert be.calls == 2
    await p.tick(now=2700)
    assert be.calls == 2
    await p.tick(now=2782)             # poll -> 200, snapshot recorded
    assert be.calls == 3
    row = conn.execute("SELECT source, utilization_pct FROM quota_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == ("poll", 3)


def test_rounding_dip_between_sources_is_not_a_reset(db):
    # Live data: the usage endpoint said 2% while headers said 1% for the same 7d window.
    conn = db[1]
    a, _ = create_user(conn, "a")
    _snap(conn, 100, 10, resets=5000)
    _req(conn, a, 150, 10)
    _snap(conn, 200, 30, resets=5000)
    _snap(conn, 250, 29, resets=5000)   # other source, rounded down
    _req(conn, a, 300, 10)
    _snap(conn, 350, 35, resets=5000)
    res = quota.attribution(conn, Pricing(), "5h", now=360)
    assert res["shares"][a] == pytest.approx(25)       # +20, then +5 above the 30 high-water mark
    assert res["window_start"] == 100


def test_drop_without_reset_times_still_starts_a_window(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    _snap(conn, 100, 40, resets=None)
    _snap(conn, 200, 2, resets=None)
    _req(conn, a, 250, 10)
    _snap(conn, 300, 5, resets=None)
    res = quota.attribution(conn, Pricing(), "5h", now=310)
    assert res["shares"][a] == pytest.approx(3)


def _snap7(conn, t, util, resets=10_000_000):
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                 (t, "header", "7d", util, resets))


def test_request_shares_attribute_each_request_its_part_of_the_rise(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    b, _ = create_user(conn, "b")
    _snap7(conn, 100, 10)
    _req(conn, a, 150, 3000)       # id 1
    _req(conn, b, 160, 1000)       # id 2
    _snap7(conn, 200, 30)          # +20 split 3:1
    _req(conn, a, 250, 500)        # id 3
    _snap7(conn, 300, 32)          # +2, a alone
    rows = quota.request_shares(conn, Pricing(), "7d", since=0, now=310)
    assert [(r[0], r[1], round(r[4], 3)) for r in rows] == [(1, a, 15.0), (2, b, 5.0), (3, a, 2.0)]
    assert quota.attributed_since(conn, Pricing(), "7d", a, 0, 310) == pytest.approx(17)
    assert quota.attributed_since(conn, Pricing(), "7d", b, 0, 310) == pytest.approx(5)


def test_request_shares_split_a_straddling_interval_by_the_requests_after_since(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    _snap7(conn, 100, 0)
    _req(conn, a, 120, 1000)       # before the day boundary at 150
    _req(conn, a, 180, 3000)       # after it
    _snap7(conn, 200, 8)           # +8 over an interval that straddles 150
    assert quota.attributed_since(conn, Pricing(), "7d", a, since=150, now=210) == pytest.approx(6)   # 3000/4000 of 8
    assert quota.attributed_since(conn, Pricing(), "7d", a, since=0, now=210) == pytest.approx(8)
    assert quota.attributed_since(conn, Pricing(), "7d", a, since=190, now=210) == 0


def test_request_shares_count_both_sides_of_a_weekly_reset(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    _snap7(conn, 100, 90, resets=1000)
    _req(conn, a, 150, 10)
    _snap7(conn, 200, 95, resets=1000)      # +5 before the reset
    _snap7(conn, 1100, 1, resets=700_000)   # the week reset: a drop with the reset time moving forward
    _req(conn, a, 1150, 10)
    _snap7(conn, 1200, 4, resets=700_000)   # +3 after it
    assert quota.attributed_since(conn, Pricing(), "7d", a, since=0, now=1210) == pytest.approx(8)
    # A dip with an unchanged reset time is rounding noise, not usage and not a reset.
    _snap7(conn, 1300, 3.5, resets=700_000)
    _req(conn, a, 1350, 10)
    _snap7(conn, 1400, 5, resets=700_000)   # high-water was 4: +1
    assert quota.attributed_since(conn, Pricing(), "7d", a, since=0, now=1410) == pytest.approx(9)


def test_request_shares_with_fewer_than_two_snapshots_is_empty(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    _req(conn, a, 150, 10)
    assert quota.request_shares(conn, Pricing(), "7d", 0, 200) == []
    _snap7(conn, 100, 0)
    assert quota.attributed_since(conn, Pricing(), "7d", a, 0, 200) == 0


def test_observed_rate_is_the_median_tokens_per_point(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    now = 1_000_000
    _snap(conn, now - 5000, 0)
    _req(conn, a, now - 4500, 1000)            # 1000 weighted (sonnet input = reference)
    _snap(conn, now - 4000, 2)                 # 500 per point
    _req(conn, a, now - 3500, 3000)
    _snap(conn, now - 3000, 3)                 # 3000 per point
    _snap(conn, now - 2500, 3)                 # no rise: ignored
    _snap(conn, now - 2000, 5)                 # a rise with no request in between: ignored
    _req(conn, a, now - 1500, 4000)
    _snap(conn, now - 1000, 7)                 # 2000 per point
    assert quota.observed_rate(conn, Pricing(), "5h", now) == 2000
    assert quota.observed_rate(conn, Pricing(), "7d", now) is None   # no 7d snapshots at all: no fallback can help
    # Quiet for the last 7 days: falls back to the median over the whole retained history instead of None.
    assert quota.observed_rate(conn, Pricing(), "5h", now + 8 * 86400) == 2000


def test_observed_rate_falls_back_to_the_whole_history_when_quiet_for_7_days(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    now = 2_000_000
    old = now - 10 * 86400   # a rate observed 10 days ago; nothing since
    _snap(conn, old - 1000, 0)
    _req(conn, a, old - 500, 1000)
    _snap(conn, old, 2)      # 500 per point
    assert quota.observed_rate(conn, Pricing(), "5h", now) == 500


def test_observed_rate_carries_tokens_across_pairs_without_a_rise(db):
    """Anthropic reports whole-percent steps: one point of rise follows many snapshot pairs with no rise. All the
    tokens since the last rise belong to that point, not only the last pair's."""
    conn = db[1]
    a, _ = create_user(conn, "a")
    now = 1_000_000
    t = now - 5000
    util = 0
    _snap(conn, t, util)
    for step in range(12):                     # a 250-token request in every pair; a rise every fourth pair
        _req(conn, a, t + 50, 250)
        t += 100
        if step % 4 == 3:
            util += 1
        _snap(conn, t, util)
    assert quota.observed_rate(conn, Pricing(), "5h", now) == 1000


def test_observed_rate_drops_the_carry_at_a_reset(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    now = 1_000_000
    _snap(conn, now - 900, 50, resets=now)
    _req(conn, a, now - 850, 9000)             # spent before the reset: never reaches a rise of this window
    _snap(conn, now - 800, 50, resets=now)
    _snap(conn, now - 700, 0, resets=now + 18000)   # the reset
    _req(conn, a, now - 650, 1000)
    _snap(conn, now - 600, 0, resets=now + 18000)
    _req(conn, a, now - 550, 1000)
    _snap(conn, now - 500, 1, resets=now + 18000)
    assert quota.observed_rate(conn, Pricing(), "5h", now) == 2000


def test_unpriced_models_weigh_their_raw_tokens_everywhere(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    now = 1_000_000
    _snap(conn, now - 300, 0)
    _req(conn, a, now - 250, 1000, model="mystery-model")
    _snap(conn, now - 200, 1)
    assert quota.observed_rate(conn, Pricing(), "5h", now) == 1000
    assert quota.attribution(conn, Pricing(), "5h", now=now)["shares"][a] == pytest.approx(1)


def test_attribution_cache_recomputes_only_when_its_inputs_change(db, monkeypatch):
    conn = db[1]
    a, _ = create_user(conn, "a")
    _snap(conn, 100, 0)
    _req(conn, a, 150, 1000)
    _snap(conn, 200, 4)
    calls = []
    real = quota.attribution
    monkeypatch.setattr(quota, "attribution", lambda *args, **kw: calls.append(1) or real(*args, **kw))
    first = quota.attribution_cached(conn, Pricing(), "5h", now=210, stale_after_s=1800)
    again = quota.attribution_cached(conn, Pricing(), "5h", now=2100, stale_after_s=1800)
    assert len(calls) == 1 and first["shares"] == {a: 4} and (first["stale"], again["stale"]) == (False, True)   # stale follows now
    _req(conn, a, 250, 1000)                     # ended after the newest snapshot: shares unchanged, nothing recomputed
    assert quota.attribution_cached(conn, Pricing(), "5h", now=215, stale_after_s=1800)["shares"] == {a: 4} and len(calls) == 1
    _req(conn, a, 190, 1000)                     # a request recorded late, inside the last pair
    assert quota.attribution_cached(conn, Pricing(), "5h", now=220, stale_after_s=1800)["shares"] == {a: 4} and len(calls) == 2
    _snap(conn, 300, 6)
    assert quota.attribution_cached(conn, Pricing(), "5h", now=310, stale_after_s=1800)["utilization_pct"] == 6 and len(calls) == 3
    quota.clear_rate_cache()
    quota.attribution_cached(conn, Pricing(), "5h", now=310, stale_after_s=1800)
    assert len(calls) == 4


def test_attribution_gives_the_rise_after_a_known_reset_to_the_requests_after_it(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    b, _ = create_user(conn, "b")
    _snap(conn, 100, 40, resets=1000)
    _req(conn, b, 900, 10)                  # before the reset at 1000: the old window's
    _req(conn, a, 1050, 10)                 # after it
    _snap(conn, 1100, 3, resets=19000)      # the new window's first snapshot already holds 3 points
    res = quota.attribution(conn, Pricing(), "5h", now=1110)
    assert res["shares"] == {a: pytest.approx(3)} and res["unattributed"] == pytest.approx(0)
    # Without a reset time the reset is only a drop, and the first point stays unattributed as before.
    conn.execute("UPDATE quota_snapshots SET resets_at=NULL")
    res = quota.attribution(conn, Pricing(), "5h", now=1110)
    assert res["shares"] == {} and res["unattributed"] == pytest.approx(3)


def test_request_shares_give_the_rise_after_a_known_reset_to_the_requests_after_it(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    b, _ = create_user(conn, "b")
    _snap7(conn, 100, 90, resets=1000)
    _req(conn, b, 900, 10)                  # the old week's
    _req(conn, a, 1050, 10)
    _snap7(conn, 1100, 2, resets=700_000)   # the reset at 1000; 2 points used since
    assert quota.attributed_since(conn, Pricing(), "7d", a, since=0, now=1110) == pytest.approx(2)
    assert quota.attributed_since(conn, Pricing(), "7d", b, since=0, now=1110) == 0
    assert quota.attributed_since(conn, Pricing(), "7d", a, since=1060, now=1110) == 0
