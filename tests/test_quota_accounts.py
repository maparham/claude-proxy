"""Quota, capacity and limits measured per subscription account (multi-subscription routing, task 2)."""
import pytest

from claude_proxy import capacity, db as db_mod, limits, plans, quota, tickets
from claude_proxy.config import Pricing, QuotaConfig
from claude_proxy.db import create_user
from tests.tickets_helpers import NOW, DAY, tcfg

RESET5 = NOW + 3600
RESET7 = NOW + 3 * DAY


def snap(conn, account, t, util, bucket="7d", resets=None):
    resets = resets if resets is not None else (RESET7 if bucket == "7d" else RESET5)
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at, account) VALUES(?,?,?,?,?,?)",
                 (t, "header", bucket, util, resets, account))


def req(conn, account, uid, t, tokens, ticket_id=None):
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, input_tokens, ticket_id, account) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)", (uid, t - 1, t, "POST", "/v1/messages", "anthropic", "claude-sonnet-5", tokens,
                                                 ticket_id, account))


def test_attribution_is_per_account(db):
    conn = db[1]
    alice, _ = create_user(conn, "alice")
    bob, _ = create_user(conn, "bob")
    snap(conn, "a", NOW - 600, 0.0)
    snap(conn, "b", NOW - 600, 0.0)
    req(conn, "a", alice, NOW - 300, 10_000)
    req(conn, "b", bob, NOW - 300, 10_000)
    snap(conn, "a", NOW - 60, 10.0)
    snap(conn, "b", NOW - 60, 50.0)
    a = quota.attribution(conn, Pricing(), "7d", now=NOW, account="a")
    b = quota.attribution(conn, Pricing(), "7d", now=NOW, account="b")
    assert a["shares"] == {alice: pytest.approx(10.0)} and a["utilization_pct"] == 10.0
    assert b["shares"] == {bob: pytest.approx(50.0)} and b["utilization_pct"] == 50.0
    # The cached copy keys on the account too.
    assert quota.attribution_cached(conn, Pricing(), "7d", NOW, account="a")["shares"] == a["shares"]
    assert quota.attribution_cached(conn, Pricing(), "7d", NOW, account="b")["shares"] == b["shares"]


def test_observed_rate_cache_per_account(db):
    conn = db[1]
    u, _ = create_user(conn, "u")
    for acct, per_point in (("a", 1000), ("b", 200)):
        snap(conn, acct, NOW - 3000, 0.0, "5h")
        req(conn, acct, u, NOW - 2500, per_point * 2)
        snap(conn, acct, NOW - 2000, 2.0, "5h")
        req(conn, acct, u, NOW - 1500, per_point)
        snap(conn, acct, NOW - 1000, 3.0, "5h")
    assert quota.observed_rate(conn, Pricing(), "5h", NOW, account="a") == pytest.approx(1000)
    assert quota.observed_rate(conn, Pricing(), "5h", NOW, account="b") == pytest.approx(200)
    assert quota.observed_rate(conn, Pricing(), "5h", NOW, account="a") == pytest.approx(1000)


def test_record_tags_account(db):
    conn = db[1]
    h = {"anthropic-ratelimit-unified-5h-utilization": "0.10", "anthropic-ratelimit-unified-5h-reset": str(RESET5),
         "anthropic-ratelimit-unified-7d-utilization": "0.20", "anthropic-ratelimit-unified-7d-reset": str(RESET7)}
    quota.record(conn, quota.parse_headers(h), now=NOW, account="b")
    assert {r[0] for r in conn.execute("SELECT account FROM quota_snapshots")} == {"b"}
    quota.record(conn, quota.parse_headers(h), now=NOW + 10, account="a")   # another account: written
    quota.record(conn, quota.parse_headers(h), now=NOW + 20, account="b")   # same account within 60 s: deduped
    counts = dict(conn.execute("SELECT account, COUNT(*) FROM quota_snapshots GROUP BY account").fetchall())
    assert counts == {"a": 2, "b": 2}


def test_profiles_per_account(db):
    conn = db[1]
    quota.record_profile(conn, {"who": "a"}, NOW, account="a")
    quota.record_profile(conn, {"who": "b"}, NOW + 1, account="b")
    assert quota.latest_profile(conn, account="b") == {"observed_at": NOW + 1, "profile": {"who": "b"}}
    assert quota.latest_profile(conn, account="a")["profile"] == {"who": "a"}
    assert quota.latest_profile(conn, account="c") is None


def test_profile_migration_copies_the_old_row(db):
    conn = db[1]
    conn.execute("DROP TABLE account_profiles")
    conn.execute("CREATE TABLE account_profile (id INTEGER PRIMARY KEY CHECK (id = 1), observed_at REAL NOT NULL, "
                 "raw_json TEXT NOT NULL)")
    conn.execute("INSERT INTO account_profile(id, observed_at, raw_json) VALUES(1, 5.0, '{\"old\": true}')")
    for stmt in db_mod.SCHEMA:
        conn.execute(stmt)
    db_mod._migrate(conn)
    assert quota.latest_profile(conn, account="default") == {"observed_at": 5.0, "profile": {"old": True}}
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='account_profile'").fetchone() is None


def test_usage_estimates_migration_adds_account(db):
    conn = db[1]
    conn.execute("DROP TABLE usage_estimates")
    conn.execute("CREATE TABLE usage_estimates (computed_at INTEGER NOT NULL, family TEXT NOT NULL, bucket TEXT NOT NULL, "
                 "busy_hours INTEGER NOT NULL, p75_share_per_hour REAL, PRIMARY KEY (family, bucket))")
    conn.execute("INSERT INTO usage_estimates VALUES(1, 'sonnet', '5h', 60, 0.5)")
    db_mod._migrate(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM usage_estimates")]
    assert rows == [{"computed_at": 1, "family": "sonnet", "bucket": "5h", "busy_hours": 60, "p75_share_per_hour": 0.5,
                     "account": "default"}]


def _pass(conn, uid, account, share, starts_at, ends_at):
    return conn.execute(
        "INSERT INTO tickets(user_id, user_name, account, tier, share_pct, length, days, starts_at, ends_at, list_usd, usd, "
        "currency, rate, amount, granted_at) VALUES(?, 'u', ?, 'standard', ?, 'week', 7, ?, ?, 1, 1, 'USD', 1, 1, ?)",
        (uid, account, share, starts_at, ends_at, starts_at)).lastrowid


def test_capacity_context_per_account(db):
    conn = db[1]
    cfg = tcfg()
    u, _ = create_user(conn, "u")
    snap(conn, "a", NOW - 60, 0.0)
    snap(conn, "b", NOW - 60, 0.0)
    _pass(conn, u, "a", 20.0, NOW - DAY, NOW + 6 * DAY)
    assert capacity.context(conn, cfg, "b", NOW).reserved == 0.0
    assert capacity.context(conn, cfg, "a", NOW).reserved > 0
    assert tickets.sold_at(conn, "a", NOW) == 20.0 and tickets.sold_at(conn, "b", NOW) == 0.0


def test_subscription_end_per_account(db):
    conn = db[1]
    cfg = tcfg()
    snap(conn, "a", NOW - 60, 0.0)
    snap(conn, "b", NOW - 60, 0.0)
    tickets.set_subscription_end(conn, NOW + 30 * DAY, None, "a", now=NOW)
    tickets.set_subscription_end(conn, NOW + DAY, None, "b", now=NOW)
    assert tickets.subscription_end_set(conn, "b") == NOW + DAY
    with pytest.raises(tickets.SubscriptionEnds):
        capacity.check(conn, cfg, "b", 5.0, 5.0, NOW, NOW + 7 * DAY, NOW)
    capacity.check(conn, cfg, "a", 5.0, 5.0, NOW, NOW + 7 * DAY, NOW)


def test_subscription_end_key_migrates(db):
    conn = db[1]
    conn.execute("DELETE FROM settings WHERE key LIKE 'subscription_ends_at%'")
    conn.execute("INSERT INTO settings(key, value) VALUES('subscription_ends_at', '123')")
    db_mod._migrate(conn)
    keys = dict(conn.execute("SELECT key, value FROM settings WHERE key LIKE 'subscription_ends_at%'").fetchall())
    assert keys == {"subscription_ends_at:default": "123"}


class AccountBackend:
    account = "b"

    async def poll_usage(self):
        return 200, {"five_hour": {"utilization": 3, "resets_at": RESET5}, "seven_day": {"utilization": 9, "resets_at": RESET7}}

    async def fetch_profile(self):
        return {"organization": {"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_5x"}}


async def test_poller_records_its_account(db):
    conn = db[1]
    snap(conn, "a", NOW - 10, 1.0)   # another account's fresh snapshot does not stop b's poll
    p = quota.Poller(conn, AccountBackend(), QuotaConfig())
    await p.tick(now=NOW)
    assert {r[0] for r in conn.execute("SELECT account FROM quota_snapshots WHERE source='poll'")} == {"b"}
    assert [tuple(r) for r in conn.execute("SELECT account, rate_limit_tier FROM account_plans")] == [("b", "default_claude_max_5x")]
    assert plans.acknowledged(conn, "b") == "default_claude_max_5x"
    assert plans.pending_change(conn, "b") is None
    assert quota.latest_profile(conn, account="b")["profile"]["organization"]["organization_type"] == "claude_max"


def test_hand_set_share_on_assigned_account(db):
    conn = db[1]
    cfg = tcfg()
    carol, _ = create_user(conn, "carol")
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?, 'share_5h', '*', '10', 'pct', ?)",
                 (carol, NOW))
    for acct, pts in (("a", 30.0), ("b", 5.0)):
        snap(conn, acct, NOW - 600, 0.0, "5h")
        req(conn, acct, carol, NOW - 300, 10_000)
        snap(conn, acct, NOW - 60, pts, "5h")
    row = {"kind": "share_5h", "scope": "*", "value": "10", "unit": "pct"}
    conn.execute("UPDATE users SET account='b' WHERE id=?", (carol,))
    st = limits._share_state(conn, cfg, carol, row, NOW)
    assert st.skipped is None and st.current == pytest.approx(5.0) and not st.exceeded
    conn.execute("UPDATE users SET account='a' WHERE id=?", (carol,))
    assert limits._share_state(conn, cfg, carol, row, NOW).exceeded
    conn.execute("UPDATE users SET account=NULL WHERE id=?", (carol,))
    assert limits._share_state(conn, cfg, carol, row, NOW).skipped == "no subscription assigned"
