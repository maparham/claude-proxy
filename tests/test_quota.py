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
    })
    by = {s.bucket: s for s in snaps}
    assert by["5h"].utilization_pct == pytest.approx(1.0)
    assert by["5h"].resets_at == 1790089200
    assert by["7d"].utilization_pct == 38
    assert by["7d_sonnet"].utilization_pct == 12.5
    assert "7d_opus" not in by and "extra_usage" not in by


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
