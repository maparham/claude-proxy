"""Pass app keys (2026-11-18 spec): storage and key lookup."""
import pytest

from claude_proxy import api_credit, capacity, limits, passkeys, tickets, trials
from claude_proxy import db as dbm
from claude_proxy.config import Route
from claude_proxy.db import create_user, find_user_by_key
from tests.test_tickets_limits import NOW as TNOW, fresh, grant_week, req
from tests.tickets_helpers import live, seeded, user


def test_create_and_find(db):
    conn = db[1]
    for role in ("user", "admin"):
        uid, _ = create_user(conn, "u-" + role, role)
        kid, raw = passkeys.create_key(conn, uid, "Desktop")
        assert raw.startswith("sk-proxy-p-")
        u = find_user_by_key(conn, raw)
        assert u["key_scope"] == "pass" and u["role"] == "user" and u["pass_key_id"] == kid and u["id"] == uid
        assert passkeys.key_owner(conn, kid) == uid


def test_label_and_cap(db):
    conn = db[1]
    uid, _ = create_user(conn, "u")
    for bad in ("", "  ", "x" * 65):
        with pytest.raises(passkeys.PassKeyError):
            passkeys.create_key(conn, uid, bad)
    for i in range(passkeys.MAX_KEYS):
        passkeys.create_key(conn, uid, f"k{i}")
    with pytest.raises(passkeys.PassKeyError):
        passkeys.create_key(conn, uid, "one too many")
    assert len(passkeys.keys(conn, uid)) == 20


def test_revoke(db):
    conn = db[1]
    uid, own = create_user(conn, "u")
    other, _ = create_user(conn, "o")
    kid, raw = passkeys.create_key(conn, uid, "Desktop")
    assert passkeys.revoke_key(conn, other, kid, other) is False
    assert passkeys.revoke_key(conn, uid, kid, uid) is True
    assert find_user_by_key(conn, raw) is None
    assert passkeys.key_owner(conn, kid) is None
    assert find_user_by_key(conn, own)["key_scope"] == "full"
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log")]
    assert "pass_key_add" in actions and "pass_key_revoke" in actions


def test_migration_adds_column(db):
    conn = db[1]
    assert "pass_key_id" in dbm._columns(conn, "requests")
    assert {r[1] for r in conn.execute("PRAGMA index_list(requests)")} >= {"idx_requests_pass_key"}


def _req(conn, uid, kid, at, inp=10):
    conn.execute("INSERT INTO requests(user_id, started_at, method, path, input_tokens, output_tokens, cache_creation_tokens, "
                 "cache_read_tokens, pass_key_id) VALUES(?,?,?,?,?,?,?,?,?)", (uid, at, "POST", "/v1/messages", inp, 2, 3, 4, kid))


def test_usage_per_key_per_day(db):
    conn = db[1]
    uid, _ = create_user(conn, "u")
    kid, _ = passkeys.create_key(conn, uid, "Desktop")
    _req(conn, uid, kid, 86400 * 100 + 10)
    _req(conn, uid, kid, 86400 * 100 + 20)
    _req(conn, uid, kid, 86400 * 101 + 5)
    _req(conn, uid, None, 86400 * 101 + 6)   # not a pass-key request
    rows = passkeys.usage(conn, uid, 0)
    assert [(r["day"], r["requests"], r["input_tokens"], r["cache_write_tokens"], r["cache_read_tokens"]) for r in rows] == \
        [(86400 * 101, 1, 10, 3, 4), (86400 * 100, 2, 20, 6, 8)]
    assert rows[0]["pass_key_id"] == kid
    z = passkeys.usage(conn, uid, 0, tz="Asia/Tehran")
    assert sum(r["requests"] for r in z) == 3 and all(r["pass_key_id"] == kid for r in z)


# ---------- the gate (Task 2) ----------


MSG = "/v1/messages"
SONNET = "claude-sonnet-5"


@pytest.fixture
def env(db):
    """alice with a running Lite week pass and fresh 5h/7d snapshots."""
    conn, cfg, ids = seeded(db)
    fresh(conn, TNOW - 600)
    t = grant_week(conn, cfg, ids, ids["alice"], now=TNOW - 300)
    return conn, cfg, ids, t


def pk_row(conn, uid, kid, t, ticket, i=0, o=0, rejected_by=None):
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, input_tokens, output_tokens, "
                 "ticket_id, pass_key_id, rejected_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (uid, t - 1, t, "POST", MSG, "anthropic", SONNET, i, o, ticket, kid, rejected_by))


def test_no_pass_refused(db):
    conn, cfg, ids = seeded(db)
    for path, model in ((MSG, SONNET), ("/v1/models", None)):
        d = passkeys.evaluate(conn, cfg, ids["alice"], model, path, now=TNOW)
        assert d.status == 403 and d.kind == "pass_key_no_pass" == passkeys.NO_PASS
        assert d.body["error"]["type"] == "permission_error"
        assert d.body["error"]["message"] == "This key works while you have a running pass."


def test_hand_set_limits_not_used(db):
    conn, cfg, ids = seeded(db)
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)",
                 (ids["alice"], "cost_total", "*", "5", "usd"))
    d = passkeys.evaluate(conn, cfg, ids["alice"], SONNET, MSG, now=TNOW)
    assert d.status == 403 and d.kind == "pass_key_no_pass"


@pytest.fixture
def waiting(tmp_path):
    from claude_proxy.db import init_db
    conn = init_db(str(tmp_path / "t.db"))
    conn, cfg, ids = seeded((None, conn))
    live(conn, TNOW)
    trials.set_settings(conn, cfg, ids["admin"], {"enabled": True})
    trials.claim(conn, cfg, user(conn, ids["alice"]), TNOW)
    yield conn, cfg, ids
    conn.close()


def unstarted(conn):
    return conn.execute("SELECT COUNT(*) FROM trial_claims WHERE ticket_id IS NULL AND started_at IS NULL").fetchone()[0] == 1


def test_waiting_trial_not_started(waiting):
    conn, cfg, ids = waiting
    d = passkeys.evaluate(conn, cfg, ids["alice"], SONNET, MSG, now=TNOW + 60)
    assert d.status == 403 and d.kind == "pass_key_no_pass"
    assert unstarted(conn)
    assert conn.execute("SELECT COUNT(*) FROM tickets WHERE user_id=?", (ids["alice"],)).fetchone()[0] == 0


def test_limits_evaluate_autostart_false(waiting):
    conn, cfg, ids = waiting
    limits.evaluate(conn, cfg, ids["alice"], SONNET, MSG, now=TNOW + 60, autostart=False)
    assert unstarted(conn)
    limits.evaluate(conn, cfg, ids["alice"], SONNET, MSG, now=TNOW + 61)   # the default starts it
    assert not unstarted(conn)


def test_running_pass_allowed_and_served(env):
    conn, cfg, ids, t = env
    served = {}
    assert passkeys.evaluate(conn, cfg, ids["alice"], SONNET, MSG, now=TNOW, served=served) is None
    assert served["ticket_id"] == t["id"]
    assert passkeys.evaluate(conn, cfg, ids["alice"], None, "/v1/models", now=TNOW) is None


def test_pass_limits_apply(env):
    conn, cfg, ids, t = env
    req(conn, ids["alice"], TNOW - 200, i=1000)
    fresh(conn, TNOW - 100, util5=6)
    d = passkeys.evaluate(conn, cfg, ids["alice"], SONNET, MSG, now=TNOW)
    assert d.status == 429 and d.kind == "share_5h"


def test_inflight_and_rpm(env):
    conn, cfg, ids, t = env
    uid = ids["alice"]
    d = passkeys.evaluate(conn, cfg, uid, SONNET, MSG, inflight=4, pass_inflight=4, now=TNOW)
    assert (d.status, d.kind, d.retry_after) == (429, "pass_key_inflight", 1)
    assert d.body["error"]["type"] == "rate_limit_error"
    assert passkeys.evaluate(conn, cfg, uid, SONNET, "/v1/messages/count_tokens", pass_inflight=4, now=TNOW) is None
    d = passkeys.evaluate(conn, cfg, uid, SONNET, MSG, inflight=cfg.limits.max_inflight, pass_inflight=0, now=TNOW)
    assert d.status == 429 and d.kind == "max_inflight"
    kid, _ = passkeys.create_key(conn, uid, "Desktop")
    for i in range(59):
        pk_row(conn, uid, kid, TNOW - 5, t["id"])
    pk_row(conn, uid, kid, TNOW - 5, t["id"], rejected_by="max_inflight")   # a refused one does not count
    pk_row(conn, uid, kid, TNOW - 120, t["id"])                             # nor one older than a minute
    assert passkeys.evaluate(conn, cfg, uid, SONNET, MSG, now=TNOW) is None
    d = passkeys.evaluate(conn, cfg, uid, SONNET, MSG, pass_inflight=1, inflight=1, now=TNOW)
    assert (d.status, d.kind, d.retry_after) == (429, "pass_key_rpm", 60)


def test_app_use_counts_as_pass_use(env):
    conn, cfg, ids, t = env
    uid = ids["alice"]
    kid, _ = passkeys.create_key(conn, uid, "Desktop")
    req(conn, uid, TNOW - 100, i=1000, ticket=t["id"])
    one = capacity._weighted_since(conn, cfg, uid, TNOW - 500, TNOW, ticket_id=t["id"])
    pk_row(conn, uid, kid, TNOW - 50, t["id"], i=1000)
    assert capacity._weighted_since(conn, cfg, uid, TNOW - 500, TNOW, ticket_id=t["id"]) == pytest.approx(2 * one)
    conn.execute("DELETE FROM requests WHERE pass_key_id IS NULL")
    assert capacity._weighted_since(conn, cfg, uid, TNOW - 500, TNOW, ticket_id=t["id"]) == pytest.approx(one)
    assert tickets.was_used(conn, tickets.get(conn, t["id"])) is True


@pytest.mark.parametrize("by_app", [True, False])
def test_limit_reached_by_apps_blocks_claude_code_and_back(env, by_app):
    conn, cfg, ids, t = env
    uid = ids["alice"]
    kid, _ = passkeys.create_key(conn, uid, "Desktop")
    if by_app:
        pk_row(conn, uid, kid, TNOW - 200, t["id"], i=1000)
    else:
        req(conn, uid, TNOW - 200, i=1000)
    fresh(conn, TNOW - 100, util5=6)
    d = limits.evaluate(conn, cfg, uid, SONNET, MSG, now=TNOW)
    assert d.status == 429 and d.kind == "share_5h"
    d = passkeys.evaluate(conn, cfg, uid, SONNET, MSG, now=TNOW)
    assert d.status == 429 and d.kind == "share_5h"


def test_refuse_extras_app_wording():
    d = api_credit.refuse_extras({"tools": [{"type": "web_search_20250305"}]}, None, app=True)
    assert "pass app keys" in d.body["error"]["message"] and "/fast" not in d.body["error"]["message"]
    assert "pass app keys" in api_credit.refuse_extras({"speed": "fast"}, None, app=True).body["error"]["message"]
    assert "pass app keys" in api_credit.refuse_extras({}, "context-1m-2025", app=True).body["error"]["message"]
    assert "API keys" in api_credit.refuse_extras({"tools": [{"type": "web_search_20250305"}]}, None).body["error"]["message"]


def test_routed_model_refused_even_with_cost_limit(env):
    conn, cfg, ids, t = env
    cfg.routes.append(Route(name="meta", base_url="http://m", api_key_env="M", models=["muse-*"]))
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)",
                 (ids["alice"], "cost_total", "*", "50", "usd"))
    d = passkeys.evaluate(conn, cfg, ids["alice"], "muse-spark", MSG, now=TNOW)
    assert d.status == 403 and d.kind == "pass_key_route"
    assert d.body["error"]["message"] == "Pass app keys work with Claude models only."
