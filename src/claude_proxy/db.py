from __future__ import annotations

import hashlib
import os
import json
import secrets
import sqlite3
import time
from pathlib import Path

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL CHECK(role IN ('admin','user')),
        key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        revoked_at INTEGER,
        password_hash TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS limits (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT '*',
        value TEXT NOT NULL,
        unit TEXT NOT NULL CHECK(unit IN ('raw','weighted','pct','count','list','usd','bool')),
        updated_at INTEGER NOT NULL,
        updated_by INTEGER,
        PRIMARY KEY (user_id, kind, scope)
    )""",
    """CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        started_at REAL NOT NULL,
        ended_at REAL,
        method TEXT NOT NULL,
        path TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'anthropic',
        model TEXT,
        requested_model TEXT,
        status INTEGER,
        stream INTEGER,
        complete INTEGER,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_creation_tokens INTEGER DEFAULT 0,
        cache_creation_5m INTEGER DEFAULT 0,
        cache_creation_1h INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        upstream_request_id TEXT,
        session_id TEXT,
        client_version TEXT,
        error_type TEXT,
        meter_error INTEGER DEFAULT 0,
        rejected_by TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_requests_user_time ON requests(user_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_requests_time ON requests(started_at)",
    """CREATE TABLE IF NOT EXISTS quota_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        observed_at REAL NOT NULL,
        source TEXT NOT NULL CHECK(source IN ('header','poll')),
        bucket TEXT NOT NULL,
        utilization_pct REAL NOT NULL,
        resets_at INTEGER,
        status TEXT,
        raw_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_quota_bucket_time ON quota_snapshots(bucket, observed_at)",
    """CREATE TABLE IF NOT EXISTS credentials (
        backend TEXT PRIMARY KEY,
        encrypted_blob TEXT,
        expires_at INTEGER,
        updated_at INTEGER,
        state TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at INTEGER NOT NULL,
        actor_user_id INTEGER,
        action TEXT NOT NULL,
        target TEXT,
        detail_json TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    # Latest title Claude Code gave each of its sessions (titles.py); the only conversation content kept.
    """CREATE TABLE IF NOT EXISTS session_titles (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL,
        title TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (user_id, session_id)
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE,
        csrf_token TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    )""",
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring databases created by earlier versions up to the current schema."""
    cols = _columns(conn, "limits")
    if "scope" not in cols:
        conn.execute("ALTER TABLE limits RENAME TO limits_old")
        conn.execute(SCHEMA[1])
        conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at, updated_by) "
                     "SELECT user_id, kind, '*', value, unit, updated_at, updated_by FROM limits_old")
        conn.execute("DROP TABLE limits_old")
    if "provider" not in _columns(conn, "requests"):
        conn.execute("ALTER TABLE requests ADD COLUMN provider TEXT NOT NULL DEFAULT 'anthropic'")
    if "requested_model" not in _columns(conn, "requests"):
        conn.execute("ALTER TABLE requests ADD COLUMN requested_model TEXT")
    if "csrf_token" not in _columns(conn, "sessions"):
        conn.execute("ALTER TABLE sessions ADD COLUMN csrf_token TEXT NOT NULL DEFAULT ''")


def get_conn(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    # The database holds the encrypted grant and key hashes: owner-only, including the WAL files.
    old_umask = os.umask(0o077)
    try:
        conn = get_conn(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    finally:
        os.umask(old_umask)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(f"{db_path}{suffix}"):
            os.chmod(f"{db_path}{suffix}", 0o600)
    for stmt in SCHEMA:
        conn.execute(stmt)
    _migrate(conn)
    return conn


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_virtual_key() -> tuple[str, str, str]:
    """Returns (raw_key, key_hash, key_prefix). The raw key is shown once and never stored."""
    raw = "sk-proxy-" + secrets.token_urlsafe(32)
    return raw, hash_key(raw), raw[:12]


def audit(conn: sqlite3.Connection, actor_user_id: int | None, action: str, target: str, detail: dict | None = None) -> None:
    conn.execute("INSERT INTO audit_log(at, actor_user_id, action, target, detail_json) VALUES(?,?,?,?,?)",
                 (int(time.time()), actor_user_id, action, target, json.dumps(detail) if detail else None))


def create_user(conn: sqlite3.Connection, name: str, role: str = "user", password_hash: str | None = None) -> tuple[int, str]:
    raw, h, prefix = generate_virtual_key()
    cur = conn.execute(
        "INSERT INTO users(name, role, key_hash, key_prefix, enabled, created_at, password_hash) VALUES(?,?,?,?,1,?,?)",
        (name, role, h, prefix, int(time.time()), password_hash),
    )
    return cur.lastrowid, raw


def find_user_by_key(conn: sqlite3.Connection, raw_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE key_hash=?", (hash_key(raw_key),)).fetchone()


def find_user(conn: sqlite3.Connection, ref: str | int) -> sqlite3.Row | None:
    """Look a user up by id or name."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        row = conn.execute("SELECT * FROM users WHERE id=?", (int(ref),)).fetchone()
        if row:
            return row
    return conn.execute("SELECT * FROM users WHERE name=?", (str(ref),)).fetchone()


def _user_name(conn: sqlite3.Connection, user_id: int) -> str:
    row = conn.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
    return row["name"] if row else str(user_id)


def rotate_key(conn: sqlite3.Connection, user_id: int, actor: int | None = None) -> str:
    raw, h, prefix = generate_virtual_key()
    conn.execute("UPDATE users SET key_hash=?, key_prefix=? WHERE id=?", (h, prefix, user_id))
    audit(conn, actor, "rotate_key", _user_name(conn, user_id))
    return raw


def set_enabled(conn: sqlite3.Connection, user_id: int, enabled: bool, actor: int | None = None) -> None:
    conn.execute("UPDATE users SET enabled=? WHERE id=?", (1 if enabled else 0, user_id))
    audit(conn, actor, "enable" if enabled else "disable", _user_name(conn, user_id))


def revoke(conn: sqlite3.Connection, user_id: int, actor: int | None = None) -> None:
    conn.execute("UPDATE users SET revoked_at=?, enabled=0 WHERE id=?", (int(time.time()), user_id))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    audit(conn, actor, "revoke", _user_name(conn, user_id))


def delete_user(conn: sqlite3.Connection, user_id: int, actor: int | None = None) -> int:
    """Remove a revoked user and their usage history. Returns the number of requests deleted."""
    u = conn.execute("SELECT name, revoked_at FROM users WHERE id=?", (user_id,)).fetchone()
    if u is None or u["revoked_at"] is None:
        raise ValueError("Only a revoked user can be deleted; revoke them first.")
    n = conn.execute("DELETE FROM requests WHERE user_id=?", (user_id,)).rowcount
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))   # limits and sessions cascade
    audit(conn, actor, "delete_user", u["name"], {"deleted_requests": n})
    return n


def set_session_title(conn: sqlite3.Connection, user_id: int, session_id: str, title: str) -> None:
    # Keyed by user too: the session id comes from the client, so one user can never rename another's session.
    conn.execute("INSERT INTO session_titles(user_id, session_id, title, updated_at) VALUES(?,?,?,?) "
                 "ON CONFLICT(user_id, session_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at",
                 (user_id, session_id, title, time.time()))


def insert_request(conn: sqlite3.Connection, **kw) -> int:
    cols = ",".join(kw.keys())
    placeholders = ",".join(["?"] * len(kw))
    return conn.execute(f"INSERT INTO requests({cols}) VALUES({placeholders})", tuple(kw.values())).lastrowid


def create_session(conn: sqlite3.Connection, user_id: int, ttl_s: int = 7 * 86400) -> tuple[str, str]:
    """Returns (session_token, csrf_token)."""
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = int(time.time())
    conn.execute("INSERT INTO sessions(user_id, token_hash, csrf_token, created_at, expires_at) VALUES(?,?,?,?,?)",
                 (user_id, hash_key(raw), csrf, now, now + ttl_s))
    return raw, csrf


def find_session(conn: sqlite3.Connection, raw_token: str) -> sqlite3.Row | None:
    if not raw_token:
        return None
    return conn.execute(
        "SELECT u.*, s.csrf_token AS csrf_token FROM users u JOIN sessions s ON s.user_id=u.id "
        "WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1 AND u.revoked_at IS NULL",
        (hash_key(raw_token), int(time.time())),
    ).fetchone()


def delete_session(conn: sqlite3.Connection, raw_token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (hash_key(raw_token),))


def cleanup(conn: sqlite3.Connection, retention_days: int) -> dict[str, int]:
    """Delete expired sessions and events older than the retention period."""
    now = time.time()
    cutoff = now - retention_days * 86400
    out = {
        "sessions": conn.execute("DELETE FROM sessions WHERE expires_at<?", (int(now),)).rowcount,
        "requests": conn.execute("DELETE FROM requests WHERE started_at<?", (cutoff,)).rowcount,
        "quota_snapshots": conn.execute("DELETE FROM quota_snapshots WHERE observed_at<?", (cutoff,)).rowcount,
        "session_titles": conn.execute("DELETE FROM session_titles WHERE updated_at<?", (cutoff,)).rowcount,
    }
    return out
