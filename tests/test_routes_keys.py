"""Routes-only keys (spec 2.1-2.2): a second key per person that only reaches third-party routes."""
import sqlite3

import pytest

from claude_proxy.db import (create_user, find_session, create_session, find_user_by_key, init_db, remove_routes_key,
                             revoke, rotate_key, set_routes_key)


def audit_actions(conn, target):
    return [r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE target=? ORDER BY id", (target,))]


def test_each_key_maps_to_its_user_with_its_scope(db):
    conn = db[1]
    uid, full = create_user(conn, "alice")
    routes = set_routes_key(conn, uid)
    assert routes.startswith("sk-proxy-r-")
    assert find_user_by_key(conn, full)["key_scope"] == "full"
    u = find_user_by_key(conn, routes)
    assert (u["id"], u["name"], u["key_scope"]) == (uid, "alice", "routes")
    assert u["routes_key_prefix"] == routes[:14]
    assert find_user_by_key(conn, "sk-proxy-r-not-a-key") is None


def test_replacing_and_removing_the_routes_key(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    first = set_routes_key(conn, uid)
    second = set_routes_key(conn, uid)
    assert find_user_by_key(conn, first) is None
    assert find_user_by_key(conn, second)["id"] == uid
    assert remove_routes_key(conn, uid) is True
    assert find_user_by_key(conn, second) is None
    assert remove_routes_key(conn, uid) is False
    assert audit_actions(conn, "alice") == ["routes_key_issue", "routes_key_replace", "routes_key_remove"]


def test_rotating_one_key_leaves_the_other_working(db):
    conn = db[1]
    uid, full = create_user(conn, "alice")
    routes = set_routes_key(conn, uid)
    new_full = rotate_key(conn, uid)
    assert find_user_by_key(conn, full) is None
    assert find_user_by_key(conn, routes)["key_scope"] == "routes"
    set_routes_key(conn, uid)
    assert find_user_by_key(conn, new_full)["key_scope"] == "full"


def test_revoked_user_gets_no_routes_key(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    revoke(conn, uid)
    with pytest.raises(ValueError, match="revoked"):
        set_routes_key(conn, uid)


def test_two_users_cannot_share_a_routes_key_hash(db):
    conn = db[1]
    a, _ = create_user(conn, "alice")
    b, _ = create_user(conn, "bob")
    conn.execute("UPDATE users SET routes_key_hash='same' WHERE id=?", (a,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE users SET routes_key_hash='same' WHERE id=?", (b,))


def test_dashboard_session_user_has_full_scope(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    raw, _ = create_session(conn, uid)
    u = find_session(conn, raw)
    assert (u["id"], u["key_scope"]) == (uid, "full") and u["csrf_token"]


OLD_USERS = ("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
             "role TEXT NOT NULL CHECK(role IN ('admin','user')), key_hash TEXT NOT NULL UNIQUE, key_prefix TEXT NOT NULL, "
             "enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, revoked_at INTEGER, password_hash TEXT)")


def test_migration_adds_the_columns_and_index_to_an_old_database(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(OLD_USERS)
    old.execute("INSERT INTO users(name, role, key_hash, key_prefix, created_at) VALUES('alice','user','h','sk-proxy-abc',0)")
    old.commit()
    old.close()
    conn = init_db(path)
    assert {"routes_key_hash", "routes_key_prefix"} <= {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    assert "idx_users_routes_key" in {r[1] for r in conn.execute("PRAGMA index_list(users)")}
    assert find_user_by_key(conn, set_routes_key(conn, 1))["name"] == "alice"


def test_migration_adds_the_prefix_column_when_only_the_hash_column_is_missing_it(tmp_path):
    # A crash between the two ALTER TABLE statements of an earlier version would leave routes_key_hash
    # present but routes_key_prefix missing forever unless each column is guarded on its own.
    path = tmp_path / "half.db"
    old = sqlite3.connect(path)
    old.execute(OLD_USERS.replace(
        "password_hash TEXT)", "password_hash TEXT, routes_key_hash TEXT)"))
    old.execute("INSERT INTO users(name, role, key_hash, key_prefix, created_at) VALUES('alice','user','h','sk-proxy-abc',0)")
    old.commit()
    old.close()
    conn = init_db(path)
    assert {"routes_key_hash", "routes_key_prefix"} <= {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    assert find_user_by_key(conn, set_routes_key(conn, 1))["name"] == "alice"
