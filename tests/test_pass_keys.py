"""Pass app keys (2026-11-18 spec): storage and key lookup."""
import pytest

from claude_proxy import db as dbm
from claude_proxy import passkeys
from claude_proxy.db import create_user, find_user_by_key


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
