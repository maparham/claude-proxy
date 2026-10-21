"""API keys on prepaid credit (2026-10-21 spec): storage, rates, ledger, keys, gate."""
import time

import pytest

from claude_proxy import api_credit
from claude_proxy.config import Config, ConfigError
from claude_proxy.db import API_KEY_PREFIX, create_user, find_user_by_key, hash_key, init_db, revoke
from claude_proxy.forwarder import COUNT_TOKENS_PATH


def add_api_key(conn, uid, label="laptop"):
    raw = API_KEY_PREFIX + "x" * 20 + str(uid) + label
    cur = conn.execute("INSERT INTO api_keys(user_id, key_hash, key_prefix, label, created_at) VALUES(?,?,?,?,?)",
                       (uid, hash_key(raw), raw[:14], label, int(time.time())))
    return cur.lastrowid, raw


def test_schema_has_the_api_tables_and_the_request_column(db):
    conn = db[1]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"api_keys", "api_rates", "api_ledger", "api_account_limits"} <= tables
    assert "api_key_id" in {r[1] for r in conn.execute("PRAGMA table_info(requests)")}


def test_old_database_gains_the_request_column(tmp_path):
    path = tmp_path / "old.db"
    conn = init_db(path)
    conn.execute("DROP INDEX idx_requests_api_key")   # SQLite won't drop an indexed column
    conn.execute("ALTER TABLE requests DROP COLUMN api_key_id")
    conn.close()
    conn = init_db(path)
    assert "api_key_id" in {r[1] for r in conn.execute("PRAGMA table_info(requests)")}


def test_an_api_key_maps_to_its_user_with_the_api_scope_and_never_admin(db):
    conn = db[1]
    uid, _ = create_user(conn, "boss", role="admin")
    kid, raw = add_api_key(conn, uid)
    u = find_user_by_key(conn, raw)
    assert (u["id"], u["key_scope"], u["role"], u["api_key_id"]) == (uid, "api", "user", kid)


def test_a_revoked_api_key_is_unknown(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    kid, raw = add_api_key(conn, uid)
    conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (int(time.time()), kid))
    assert find_user_by_key(conn, raw) is None


def test_api_key_of_a_revoked_user_is_still_found_so_auth_can_refuse_it(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    _, raw = add_api_key(conn, uid)
    revoke(conn, uid)
    assert find_user_by_key(conn, raw)["revoked_at"] is not None   # authenticate() turns this into a 401


def test_api_section_switches_the_api_on(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[api]\nmax_inflight = 2\nrpm = 30\n")
    c = Config.load(str(p))
    assert (c.api.enabled, c.api.max_inflight, c.api.rpm) == (True, 2, 30)
    assert Config().api.enabled is False


@pytest.mark.parametrize("body,msg", [
    ("[api]\nenabled = true\n", "remove the [api] section"),
    ("[api]\nfoo = 1\n", "unknown key [api].foo"),
    ("[api]\nrpm = 0\n", "[api].rpm must be a whole number of at least 1"),
    ("[api]\nmax_inflight = 1.5\n", "[api].max_inflight must be a whole number of at least 1"),
])
def test_bad_api_section_is_refused(tmp_path, body, msg):
    p = tmp_path / "c.toml"
    p.write_text(body)
    with pytest.raises(ConfigError, match=msg.replace("[", r"\[").replace("]", r"\]").replace(".", r"\.")):
        Config.load(str(p))

@pytest.fixture
def api_cfg(cfg):
    cfg.api.enabled = True
    cfg.api.max_inflight, cfg.api.rpm = 2, 3
    return cfg


def test_seed_copies_claude_list_prices_once(db, cfg):
    conn = db[1]
    n = api_credit.seed_rates(conn, cfg)
    pats = [r["pattern"] for r in api_credit.rates(conn)]
    assert n == len(pats) and all(p.startswith("claude-") for p in pats) and "muse-spark*" not in pats
    api_credit.delete_rate(conn, "claude-opus-5*", None)
    assert api_credit.seed_rates(conn, cfg) == 0    # never refills an edited table


def test_longest_matching_pattern_wins_and_defaults_fill_cache_prices(db):
    conn = db[1]
    api_credit.set_rate(conn, "claude-opus-5*", 5, 25)
    api_credit.set_rate(conn, "claude-opus-5-5*", 4, 20, cache_read=0.2)
    assert api_credit.rate_for(conn, "claude-opus-5-5-20261001")["input"] == 4
    assert api_credit.rate_for(conn, "claude-opus-5-1")["input"] == 5
    assert api_credit.rate_for(conn, "claude-haiku-4") is None
    r = api_credit.rate_for(conn, "claude-opus-5-1")
    assert (r["cache_write_5m"], r["cache_write_1h"], r["cache_read"]) == (6.25, 10.0, 0.5)


@pytest.mark.parametrize("args", [("", 1, 1), ("claude-*", -1, 1), ("claude-*", 1, float("nan")), ("x" * 129, 1, 1)])
def test_bad_rates_are_refused(db, args):
    with pytest.raises(api_credit.CreditError):
        api_credit.set_rate(db[1], *args)


def test_cost_counts_every_token_kind():
    r = {"input": 3, "output": 15, "cache_write_5m": 3.75, "cache_write_1h": 6, "cache_read": 0.3}
    assert api_credit.cost_usd(r, 1_000_000, 100_000, 0, 0, 0) == pytest.approx(4.5)
    assert api_credit.cost_usd(r, 0, 0, 1_000_000, 1_000_000, 1_000_000) == pytest.approx(10.05)


def test_grants_and_charges_make_the_balance(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    api_credit.set_rate(conn, "claude-*", 3, 15)
    assert api_credit.grant(conn, uid, 10, "bank transfer", None) == 10
    kid, _ = api_credit.create_key(conn, uid, "laptop")
    usd = api_credit.charge(conn, uid, kid, 1, "claude-sonnet-5",
                            {"input_tokens": 1_000_000, "output_tokens": 0, "cache_creation_tokens": 0,
                             "cache_creation_5m": 0, "cache_creation_1h": 0, "cache_read_tokens": 0})
    assert usd == pytest.approx(3) and api_credit.balance(conn, uid) == pytest.approx(7)
    assert api_credit.grant(conn, uid, -2, "correction", None) == pytest.approx(5)
    assert [g["note"] for g in api_credit.grants(conn, uid)] == ["correction", "bank transfer"]


def test_charge_uses_the_cache_breakdown_like_the_ledger_does(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    api_credit.set_rate(conn, "claude-*", 1, 1, cache_write_5m=2, cache_write_1h=4, cache_read=0)
    row = {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 3_000_000,
           "cache_creation_5m": 1_000_000, "cache_creation_1h": 2_000_000, "cache_read_tokens": 0}
    assert api_credit.charge(conn, uid, 1, 1, "claude-x", row) == pytest.approx(10)
    row = {**row, "cache_creation_5m": 0, "cache_creation_1h": 0}   # no breakdown: all priced as 5-minute writes
    assert api_credit.charge(conn, uid, 1, 2, "claude-x", row) == pytest.approx(6)


def test_a_request_with_no_tokens_or_no_rate_writes_no_charge(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    api_credit.set_rate(conn, "claude-*", 3, 15)
    zero = {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_creation_5m": 0,
            "cache_creation_1h": 0, "cache_read_tokens": 0}
    assert api_credit.charge(conn, uid, 1, 1, "claude-x", zero) == 0.0
    assert api_credit.charge(conn, uid, 1, 2, "gpt-4", {**zero, "input_tokens": 5}) == 0.0
    assert conn.execute("SELECT COUNT(*) FROM api_ledger").fetchone()[0] == 0


@pytest.mark.parametrize("usd,note", [(0, "x"), (float("inf"), "x"), (1e7, "x"), (5, ""), (5, "x" * 201)])
def test_bad_grants_are_refused(db, usd, note):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    with pytest.raises(api_credit.CreditError):
        api_credit.grant(conn, uid, usd, note, None)


def test_keys_create_list_revoke(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    kid, raw = api_credit.create_key(conn, uid, "  laptop ")
    other, raw2 = api_credit.create_key(conn, uid, "ci")
    assert raw.startswith("sk-proxy-a-") and find_user_by_key(conn, raw)["api_key_id"] == kid
    assert [k["label"] for k in api_credit.keys(conn, uid)] == ["ci", "laptop"]
    assert "key_hash" not in api_credit.keys(conn, uid)[0]
    assert api_credit.revoke_key(conn, uid, kid, uid) is True
    assert api_credit.revoke_key(conn, uid, kid, uid) is False
    assert find_user_by_key(conn, raw) is None and find_user_by_key(conn, raw2)["api_key_id"] == other
    bob, _ = create_user(conn, "bob")
    assert api_credit.revoke_key(conn, bob, other, bob) is False   # not bob's


def test_key_count_and_label_are_bounded(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    with pytest.raises(api_credit.CreditError):
        api_credit.create_key(conn, uid, " ")
    for i in range(api_credit.MAX_KEYS):
        api_credit.create_key(conn, uid, f"k{i}")
    with pytest.raises(api_credit.CreditError, match="20"):
        api_credit.create_key(conn, uid, "one more")


def test_account_limits_default_to_config_and_can_be_overridden(db, api_cfg):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    assert api_credit.account_limits(conn, api_cfg, uid) == {"max_inflight": 2, "rpm": 3, "custom": False}
    api_credit.set_account_limits(conn, uid, 10, None, None)
    assert api_credit.account_limits(conn, api_cfg, uid) == {"max_inflight": 10, "rpm": 3, "custom": True}
    api_credit.set_account_limits(conn, uid, None, None, None)
    assert api_credit.account_limits(conn, api_cfg, uid)["custom"] is False


def _forwarded(conn, uid, kid, ago=1, path="/v1/messages"):
    t = time.time() - ago
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, model, status, api_key_id) "
                 "VALUES(?,?,?,?,?,?,?,?)", (uid, t, t, "POST", path, "claude-x", 200, kid))


def test_gate_order_and_messages(db, api_cfg):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    kid, _ = api_credit.create_key(conn, uid, "k")
    api_credit.set_rate(conn, "claude-*", 3, 15)
    ev = lambda model="claude-x", path="/v1/messages", inflight=0: api_credit.evaluate(conn, api_cfg, uid, model, path, inflight)  # noqa: E731
    assert ev(inflight=2).kind == api_credit.REJECT_INFLIGHT and ev(inflight=2).status == 429
    assert ev(model="gpt-4").status == 404 and ev(model="gpt-4").body["error"]["type"] == "not_found_error"
    d = ev()
    assert (d.status, d.kind, d.body["error"]["message"]) == (400, api_credit.REJECT_CREDIT, api_credit.LOW_CREDIT)
    api_credit.grant(conn, uid, 1, "x", None)
    assert ev() is None
    for _ in range(3):
        _forwarded(conn, uid, kid)
    d = ev()
    assert (d.status, d.kind, d.retry_after) == (429, api_credit.REJECT_RPM, 60)


def test_rpm_counts_in_flight_requests_and_ignores_old_and_rejected_ones(db, api_cfg):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    kid, _ = api_credit.create_key(conn, uid, "k")
    api_credit.set_rate(conn, "claude-*", 3, 15)
    api_credit.grant(conn, uid, 1, "x", None)
    _forwarded(conn, uid, kid, ago=61)
    _forwarded(conn, uid, kid)
    conn.execute("UPDATE requests SET rejected_by='api_credit' WHERE id=(SELECT MAX(id) FROM requests)")
    _forwarded(conn, uid, kid)
    assert api_credit.evaluate(conn, api_cfg, uid, "claude-x", "/v1/messages", inflight=1) is None   # 1 + 1 < 3
    assert api_credit.evaluate(conn, api_cfg, uid, "claude-x", "/v1/messages", inflight=2).kind == api_credit.REJECT_INFLIGHT


def test_count_tokens_needs_a_priced_model_and_rpm_room_but_no_credit(db, api_cfg):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    kid, _ = api_credit.create_key(conn, uid, "k")
    api_credit.set_rate(conn, "claude-*", 3, 15)
    assert api_credit.evaluate(conn, api_cfg, uid, "claude-x", COUNT_TOKENS_PATH, inflight=0) is None   # no credit needed
    assert api_credit.evaluate(conn, api_cfg, uid, "gpt-4", COUNT_TOKENS_PATH).status == 404
    for _ in range(3):
        _forwarded(conn, uid, kid, path=COUNT_TOKENS_PATH)
    assert api_credit.evaluate(conn, api_cfg, uid, "claude-x", COUNT_TOKENS_PATH).kind == api_credit.REJECT_RPM


def test_the_model_list_needs_no_credit_but_counts_for_rpm(db, api_cfg):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    assert api_credit.evaluate(conn, api_cfg, uid, None, "/v1/models", inflight=2) is None   # no credit, no price
    assert api_credit.evaluate(conn, api_cfg, uid, None, "/v1/models", inflight=3).kind == api_credit.REJECT_RPM


def test_usage_by_key_and_day_and_admin_accounts(db, api_cfg):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    kid, _ = api_credit.create_key(conn, uid, "k")
    api_credit.set_rate(conn, "claude-*", 1, 1)
    api_credit.grant(conn, uid, 5, "x", None)
    row = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_creation_tokens": 0, "cache_creation_5m": 0,
           "cache_creation_1h": 0, "cache_read_tokens": 0}
    api_credit.charge(conn, uid, kid, 1, "claude-x", row)
    api_credit.charge(conn, uid, kid, 2, "claude-x", row)
    [u] = api_credit.usage(conn, uid, time.time() - 86400)
    assert (u["api_key_id"], u["requests"], u["input"], u["usd"]) == (kid, 2, 2_000_000, pytest.approx(2))
    [a] = api_credit.accounts(conn, api_cfg)
    assert (a["name"], a["balance"], a["spent_30d"], a["keys"]) == ("alice", pytest.approx(3), pytest.approx(2), 1)


def test_gateway_seeds_rates_only_with_the_api_on(db, cfg):
    from tests.conftest import make_gateway
    conn = db[1]
    make_gateway(cfg, conn)
    assert api_credit.rates(conn) == []
    cfg.api.enabled = True
    make_gateway(cfg, conn)
    assert api_credit.rates(conn) != []


def test_rates_are_stored_without_float_noise(db, cfg):
    conn = db[1]
    api_credit.seed_rates(conn, cfg)
    assert api_credit.set_rate(conn, "claude-z*", 3, 15)["cache_read"] == 0.3   # 3 * 0.1 is 0.30000000000000004
    assert all(r[f] == round(r[f], 6) for r in api_credit.rates(conn) for f in api_credit.RATE_FIELDS)
    assert any(r["cache_read"] == 0.3 for r in api_credit.rates(conn))
