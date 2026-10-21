"""API keys on prepaid credit (2026-10-21 spec): storage, rates, ledger, keys, gate."""
import time

import pytest

from claude_proxy.config import Config, ConfigError
from claude_proxy.db import API_KEY_PREFIX, create_user, find_user_by_key, hash_key, init_db, revoke


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
