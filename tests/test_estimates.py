import pytest

from claude_proxy import estimates
from claude_proxy.config import Pricing
from claude_proxy.db import create_user
from tests.tickets_helpers import DAY, NOW

H = 3600
H0 = (NOW // H) * H - 48 * H     # three clock hours well inside the 30-day window


def req(conn, uid, t, model, i=1000):
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, input_tokens) VALUES(?,?,?,?,?,?,?,?)",
                 (uid, t, t + 1, "POST", "/v1/messages", "anthropic", model, i))


def snaps(conn, t, util):
    for bucket in ("5h", "7d"):
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                     (t, "header", bucket, util, NOW + 10 * DAY))


@pytest.fixture
def filled(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    snaps(conn, H0, 0)
    req(conn, a, H0 + 60, "claude-sonnet-5")             # hour 0: two Sonnet requests 25 minutes apart -> busy
    req(conn, a, H0 + 60 + 25 * 60, "claude-sonnet-5")
    snaps(conn, H0 + H - 1, 2)                           # +2 points in hour 0
    req(conn, a, H0 + H + 60, "claude-opus-5")           # hour 1: one request -> not busy
    snaps(conn, H0 + 2 * H - 1, 3)
    for k in range(10):                                  # hour 2: ten Opus requests in five minutes, and one Sonnet -> busy, mixed
        req(conn, a, H0 + 2 * H + 30 * k, "claude-opus-5")
    req(conn, a, H0 + 2 * H + 400, "claude-sonnet-5")
    snaps(conn, H0 + 3 * H - 1, 14)                      # +11 points in hour 2
    return conn, a


def test_busy_hours_need_spread_or_count(filled):
    conn, a = filled
    assert estimates.busy_hours(conn, H0 - DAY, NOW) == {(a, H0), (a, H0 + 2 * H)}


def test_refresh_records_share_per_busy_hour_per_family(filled):
    conn, a = filled
    out = estimates.refresh(conn, Pricing(), now=NOW)
    assert out == {"sonnet/5h": 2, "opus/5h": 1, "sonnet/7d": 2, "opus/7d": 1}
    rows = {(r["family"], r["bucket"]): r for r in conn.execute("SELECT * FROM usage_estimates")}
    # Hour 2: 11 points split by weighted tokens, Opus input at $5 vs Sonnet at $2: 10 × 2500 vs 1 × 1000.
    sonnet_hour2 = 11 * 1000 / 26000
    assert rows[("opus", "5h")]["p75_share_per_hour"] == pytest.approx(11 * 25000 / 26000)          # one value: itself
    assert rows[("sonnet", "5h")]["p75_share_per_hour"] == pytest.approx(sonnet_hour2 + 0.75 * (2 - sonnet_hour2))   # 75th of [0.42, 2]
    assert rows[("sonnet", "7d")]["busy_hours"] == 2 and rows[("sonnet", "7d")]["computed_at"] == NOW


def test_refresh_with_no_data_writes_zero_rows_and_hints_stay_hidden(db):
    conn = db[1]
    assert estimates.refresh(conn, Pricing(), now=NOW) == {"sonnet/5h": 0, "opus/5h": 0, "sonnet/7d": 0, "opus/7d": 0}
    assert estimates.hours_hint(conn, 5) == {"sonnet": {"per_5h": None, "per_day": None}, "opus": {"per_5h": None, "per_day": None}}


def test_hours_hint_is_a_floor_and_hidden_below_50_busy_hours(db):
    conn = db[1]
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (NOW, "sonnet", "5h", 50, 1.3))     # 5 / 1.3 = 3.846 -> 3.8
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (NOW, "sonnet", "7d", 50, 0.3))     # (5/7) / 0.3 = 2.38 -> 2.3
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (NOW, "opus", "5h", 49, 4.0))       # too little data
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (NOW, "opus", "7d", 60, 0.0))       # no share measured
    assert estimates.hours_hint(conn, 5) == {"sonnet": {"per_5h": 3.8, "per_day": 2.3}, "opus": {"per_5h": None, "per_day": None}}


def test_refresh_if_due_runs_once_a_day_and_only_with_tickets(db):
    from tests.tickets_helpers import tcfg
    conn, cfg = db[1], tcfg()
    assert estimates.refresh_if_due(conn, cfg, now=NOW) is not None
    assert estimates.refresh_if_due(conn, cfg, now=NOW + 6 * 3600) is None
    assert estimates.refresh_if_due(conn, cfg, now=NOW + 24 * 3600) is not None
    cfg.tickets.enabled = False
    assert estimates.refresh_if_due(conn, cfg, now=NOW + 3 * DAY) is None
