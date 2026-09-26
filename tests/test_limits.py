import pytest

from claude_proxy import limits
from claude_proxy.config import Config
from claude_proxy.db import create_user

NOW = 10_000_000.0


@pytest.fixture
def env(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    return conn, Config(), uid


def set_limit(conn, uid, kind, value, unit, scope="*"):
    conn.execute("INSERT OR REPLACE INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)",
                 (uid, kind, scope, str(value), unit))


def req(conn, uid, t, model="claude-sonnet-5", i=0, o=0, cr=0, path="/v1/messages", provider="anthropic", rejected=None):
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, input_tokens, output_tokens, cache_read_tokens, rejected_by) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uid, t, t, "POST", path, provider, model, i, o, cr, rejected))


def check(conn, cfg, uid, model="claude-sonnet-5", path="/v1/messages"):
    return limits.evaluate(conn, cfg, uid, model, path, now=NOW)


def test_no_limits_allows(env):
    conn, cfg, uid = env
    assert check(conn, cfg, uid) is None


def test_requests_daily_rejects_at_limit_with_retry_after(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "requests_daily", 2, "count")
    req(conn, uid, NOW - 5000)
    assert check(conn, cfg, uid) is None
    req(conn, uid, NOW - 100)
    d = check(conn, cfg, uid)
    assert d.status == 429 and d.kind == "requests_daily"
    assert d.retry_after == 86400 - 5000       # the oldest request leaves the window first
    assert d.body["error"]["type"] == "rate_limit_error"
    assert "requests_daily" in d.body["error"]["message"]


def test_rejected_requests_do_not_count(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "requests_daily", 1, "count")
    req(conn, uid, NOW - 10, rejected="requests_daily")
    assert check(conn, cfg, uid) is None


def test_requests_outside_window_do_not_count(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "requests_minute", 1, "count")
    req(conn, uid, NOW - 61)
    assert check(conn, cfg, uid) is None
    req(conn, uid, NOW - 30)
    assert check(conn, cfg, uid).retry_after == 30


def test_raw_token_limit(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "tokens_5h", 1000, "raw")
    req(conn, uid, NOW - 60, i=400, o=100, cr=499)
    assert check(conn, cfg, uid) is None
    req(conn, uid, NOW - 30, i=1)
    assert check(conn, cfg, uid).kind == "tokens_5h"


def test_weighted_token_limit_prices_output_higher(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "tokens_daily", 4999, "weighted")
    req(conn, uid, NOW - 60, o=1000)          # sonnet-5: output = 5x input
    assert check(conn, cfg, uid).kind == "tokens_daily"


def test_cost_monthly(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "cost_monthly", 1.0, "usd")
    req(conn, uid, NOW - 86400 * 20, model="claude-opus-5", i=100_000)   # $0.50
    assert check(conn, cfg, uid) is None
    req(conn, uid, NOW - 86400, model="claude-opus-5", o=20_000)          # $0.50
    assert check(conn, cfg, uid).kind == "cost_monthly"


def test_cost_daily(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "cost_daily", 1.0, "usd")
    req(conn, uid, NOW - 86400 - 60, model="claude-opus-5", i=200_000)   # $1.00, outside the window
    req(conn, uid, NOW - 3600, model="claude-opus-5", i=100_000)         # $0.50
    assert check(conn, cfg, uid) is None
    req(conn, uid, NOW - 60, model="claude-opus-5", o=20_000)            # $0.50
    assert check(conn, cfg, uid).kind == "cost_daily"


def test_model_scoped_limit_applies_only_to_matching_models(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "requests_daily", 1, "count", scope="claude-opus-*")
    req(conn, uid, NOW - 10, model="claude-opus-5")
    assert check(conn, cfg, uid, model="claude-sonnet-5") is None
    assert check(conn, cfg, uid, model="claude-opus-5-5").kind == "requests_daily"


def test_scoped_limit_counts_only_matching_usage(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "requests_daily", 1, "count", scope="muse-spark*")
    req(conn, uid, NOW - 10, model="claude-opus-5")
    assert check(conn, cfg, uid, model="muse-spark") is None


def test_count_tokens_bypasses_usage_limits_and_is_not_counted(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "requests_daily", 1, "count")
    req(conn, uid, NOW - 10, path="/v1/messages/count_tokens")
    assert check(conn, cfg, uid) is None
    req(conn, uid, NOW - 5)
    assert check(conn, cfg, uid, path="/v1/messages/count_tokens") is None
    assert check(conn, cfg, uid).kind == "requests_daily"


def test_allowed_models_rejects_with_403(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "allowed_models", "claude-sonnet-*,muse-spark", "list")
    assert check(conn, cfg, uid, model="muse-spark") is None
    d = check(conn, cfg, uid, model="claude-opus-5")
    assert d.status == 403 and d.body["error"]["type"] == "permission_error"


def _snap(conn, t, util, bucket="5h", resets=NOW + 3600):
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                 (t, "header", bucket, util, resets))


def test_share_limit_enforced_from_attribution(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "share_5h", 20, "pct")
    _snap(conn, NOW - 300, 10)
    req(conn, uid, NOW - 200, i=1000)
    _snap(conn, NOW - 100, 35)       # alice was the only user active: +25 points
    d = check(conn, cfg, uid)
    assert d.status == 429 and d.kind == "share_5h"
    assert d.retry_after == 3600


def test_share_limit_skipped_when_snapshots_stale(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "share_5h", 1, "pct")
    _snap(conn, NOW - 7200, 0)
    req(conn, uid, NOW - 7100, i=1000)
    _snap(conn, NOW - 7000, 50)
    assert check(conn, cfg, uid) is None
    st = {s.kind: s for s in limits.states(conn, cfg, uid, now=NOW)}
    assert st["share_5h"].skipped == "no account snapshot in the last 30 min"


def test_share_limit_not_applied_to_third_party_model(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "share_5h", 1, "pct")
    _snap(conn, NOW - 300, 0)
    req(conn, uid, NOW - 200, i=1000)
    _snap(conn, NOW - 100, 50)
    assert check(conn, cfg, uid, model="muse-spark") is None


def test_states_report_current_and_remaining(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "requests_daily", 10, "count")
    req(conn, uid, NOW - 3600)
    req(conn, uid, NOW - 60)
    s = limits.states(conn, cfg, uid, now=NOW)[0]
    assert (s.kind, s.current, s.remaining, s.reset_in) == ("requests_daily", 2, 8, 86400 - 3600)
    assert s.pct == pytest.approx(20)


@pytest.mark.parametrize("kind,value,unit,ok", [
    ("tokens_daily", "1000", "weighted", True),
    ("tokens_daily", "1000", "pct", False),
    ("tokens_daily", "-5", "raw", False),
    ("requests_minute", "5", "count", True),
    ("share_7d", "150", "pct", False),
    ("cost_monthly", "12.5", "usd", True),
    ("cost_daily", "100", "usd", True),
    ("cost_daily", "100", "weighted", False),
    ("allowed_models", "claude-*", "list", True),
    ("bogus", "1", "count", False),
])
def test_validate(kind, value, unit, ok):
    if ok:
        limits.validate(kind, "*", value, unit)
    else:
        with pytest.raises(ValueError):
            limits.validate(kind, "*", value, unit)


def test_exact_scope_matches_requested_model_even_if_upstream_renames_it(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "requests_daily", 1, "count", scope="muse-spark")
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, requested_model) "
                 "VALUES(?,?,?,?,?,?,?,?)", (uid, NOW - 10, NOW - 9, "POST", "/v1/messages", "meta", "muse-spark-1.3", "muse-spark"))
    assert check(conn, cfg, uid, model="muse-spark").kind == "requests_daily"


def test_nothing_to_free_when_nothing_counted(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "cost_daily", 100, "usd")
    req(conn, uid, NOW - 60, model=None, path="/v1/models")              # listing models costs nothing
    s = limits.states(conn, cfg, uid, now=NOW)[0]
    assert (s.current, s.reset_in) == (0, None)


def test_room_frees_from_the_oldest_usage_that_counts(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "cost_daily", 100, "usd")
    req(conn, uid, NOW - 7200, model=None, path="/v1/models")            # free, and older
    req(conn, uid, NOW - 3600, model="claude-opus-5", i=100_000)         # $0.50
    assert limits.states(conn, cfg, uid, now=NOW)[0].reset_in == 86400 - 3600
