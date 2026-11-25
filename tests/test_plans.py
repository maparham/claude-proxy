import re
import sqlite3

import pytest

from claude_proxy import db, plans


@pytest.fixture
def conn(tmp_path):
    return db.init_db(tmp_path / "t.db")


def _plan(conn, acct, org, tier, at=1.0):
    conn.execute("INSERT INTO account_plans(observed_at, organization_type, rate_limit_tier, account) VALUES(?,?,?,?)",
                 (at, org, tier, acct))


def test_detect_map():
    assert plans.detect("claude_max", "default_claude_max_20x") == "max_20x"
    assert plans.detect("claude_max", "default_claude_max_5x") == "max_5x"
    assert plans.detect("claude_pro", "default_claude_ai") == "pro"
    assert plans.detect(None, "claude_pro_tier") == "pro"
    assert plans.detect("claude_max", "default_claude_max") is None
    assert plans.detect(None, None) is None


def test_factors_default_and_set(conn):
    assert plans.factors(conn)["max_20x"] == {"7d": 20.0, "5h": 20.0}
    _plan(conn, "b", "claude_max", "default_claude_max_5x")
    plans.set_factor(conn, "max_5x", 6, 4, None)
    assert plans.factor(conn, "b", "5h") == 4.0
    assert plans.factor(conn, "b") == 6.0
    for bad in (("max_5x", 0, 1), ("team", 1, 1), ("pro", 1, 1001), ("pro", float("nan"), 1)):
        with pytest.raises(ValueError):
            plans.set_factor(conn, *bad, None)
    assert conn.execute("SELECT count(*) FROM audit_log WHERE action='plan_factor_set'").fetchone()[0] == 1


def test_override_wins(conn):
    _plan(conn, "b", "claude_max", "default_claude_max_5x")
    plans.set_override(conn, "b", "pro", None)
    assert plans.plan(conn, "b") == "pro"
    plans.set_override(conn, "b", None, None)
    assert plans.plan(conn, "b") == "max_5x"


def test_shares(conn):
    _plan(conn, "b", "claude_max", "default_claude_max_20x")
    assert plans.shares(conn, "b", 0.5, None) == (2.5, 2.5)
    _plan(conn, "p", "claude_pro", "default_claude_ai")
    assert plans.shares(conn, "p", 0.5, None) == (50.0, 50.0)
    assert plans.pro_weeks(conn, "b", 10) == 2.0
    _plan(conn, "u", "claude_max", "default_claude_max")
    assert plans.shares(conn, "u", 0.5, None) is None


def test_pending_change_and_sells(conn):
    _plan(conn, "b", "claude_max", "default_claude_max_20x", 1)
    plans.acknowledge(conn, "b", None)
    assert plans.acknowledged(conn, "b") == "default_claude_max_20x"
    assert plans.sells(conn, "b") is None
    _plan(conn, "b", "claude_max", "default_claude_max_5x", 2)
    assert plans.pending_change(conn, "b") == {"from": "default_claude_max_20x", "to": "default_claude_max_5x",
                                            "from_plan": "max_20x", "to_plan": "max_5x", "downgrade": True}
    assert plans.sells(conn, "b") == "plan_changed"
    plans.acknowledge(conn, "b", None)
    assert plans.pending_change(conn, "b") is None
    assert plans.sells(conn, "b") is None
    _plan(conn, "u", "claude_max", "default_claude_max")
    assert plans.sells(conn, "u") == "plan_unknown"


NEW_COLS = ("account TEXT", "size REAL", "size_5h REAL")


def _old_shape(stmt: str) -> str:
    """A current CREATE TABLE without the columns this change added."""
    lines = [ln for ln in stmt.split("\n") if not any(ln.strip().startswith(c) for c in NEW_COLS)]
    # the last column keeps no trailing comma (a comment may follow it)
    return re.sub(r",([ \t]*(?:--[^\n]*)?)\n(\s*\))$", r"\1\n\2", "\n".join(lines))


OLD_TABLES = ("requests", "quota_snapshots", "account_plans", "tickets", "users")


def test_migration_backfills_default(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    for stmt in db.SCHEMA:
        old = next((t for t in OLD_TABLES if stmt.startswith(f"CREATE TABLE IF NOT EXISTS {t} (")), None)
        if old:
            conn.execute(_old_shape(stmt))
        elif not stmt.startswith("CREATE INDEX") or not any(f" ON {t}(" in stmt for t in OLD_TABLES):
            conn.execute(stmt)
    conn.execute("INSERT INTO requests(user_id, started_at, method, path) VALUES(1, 1, 'POST', '/v1/messages')")
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct) VALUES(1, 'poll', '7d', 5)")
    conn.execute("INSERT INTO account_plans(observed_at, organization_type, rate_limit_tier) VALUES(1, 'claude_max', 'default_claude_max_20x')")
    conn.execute("INSERT INTO tickets(user_id, user_name, tier, share_pct, share_5h_pct, length, days, starts_at, ends_at, list_usd, usd, currency, rate, amount, granted_at) VALUES(1, 'u', 't', 5, 10, 'week', 7, 1, 2, 1, 1, 'USD', 1, 1, 1)")
    conn.execute("INSERT INTO users(name, role, key_hash, key_prefix, created_at) VALUES('u', 'user', 'h', 'p', 1)")
    db._migrate(conn)
    one = lambda q: conn.execute(q).fetchone()[0]
    assert one("SELECT account FROM requests") == one("SELECT account FROM quota_snapshots") == "default"
    assert one("SELECT account FROM tickets") == one("SELECT account FROM users") == "default"
    assert one("SELECT account FROM account_plans") == "default"
    assert (one("SELECT size FROM tickets"), one("SELECT size_5h FROM tickets")) == (1.0, 2.0)
    assert one("SELECT value FROM settings WHERE key='account_plan_ack:default'") == "default_claude_max_20x"
    before = [tuple(r) for r in conn.execute("SELECT * FROM tickets")], one("SELECT count(*) FROM settings")
    db._migrate(conn)
    assert ([tuple(r) for r in conn.execute("SELECT * FROM tickets")], one("SELECT count(*) FROM settings")) == before
