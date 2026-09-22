from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK(role IN ('admin','user')),
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    revoked_at INTEGER,
    password_hash TEXT
);
CREATE TABLE IF NOT EXISTS limits (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL CHECK(unit IN ('raw','weighted','pct','count','list')),
    updated_at INTEGER NOT NULL,
    updated_by INTEGER,
    PRIMARY KEY (user_id, kind)
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    model TEXT,
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
);
CREATE TABLE IF NOT EXISTS quota_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at INTEGER NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('header','poll')),
    bucket TEXT NOT NULL,
    utilization_pct REAL NOT NULL,
    resets_at INTEGER,
    status TEXT,
    raw_json TEXT
);
CREATE TABLE IF NOT EXISTS credentials (
    backend TEXT PRIMARY KEY,
    encrypted_blob TEXT,
    expires_at INTEGER,
    updated_at INTEGER,
    state TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at INTEGER NOT NULL,
    actor_user_id INTEGER,
    action TEXT NOT NULL,
    target TEXT,
    detail_json TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);
"""


def get_conn(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    conn = get_conn(db_path)
    for stmt in SCHEMA.split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    # WAL already set
    return conn


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_virtual_key() -> tuple[str, str, str]:
    """Returns (raw_key, key_hash, key_prefix). Raw shown once."""
    raw = "sk-proxy-" + secrets.token_urlsafe(32)
    h = hash_key(raw)
    prefix = raw[:12]  # enough to identify, spec says 8 chars — use 12 for safety
    return raw, h, prefix


def create_user(conn: sqlite3.Connection, name: str, role: str = "user", enabled: int = 1, password_hash: str | None = None) -> tuple[int, str]:
    raw, h, prefix = generate_virtual_key()
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO users(name, role, key_hash, key_prefix, enabled, created_at, password_hash) VALUES(?,?,?,?,?,?,?)",
        (name, role, h, prefix, enabled, now, password_hash),
    )
    return cur.lastrowid, raw


def find_user_by_key(conn: sqlite3.Connection, raw_key: str) -> sqlite3.Row | None:
    h = hash_key(raw_key)
    return conn.execute("SELECT * FROM users WHERE key_hash=?", (h,)).fetchone()


def rotate_key(conn: sqlite3.Connection, user_id: int) -> str:
    raw, h, prefix = generate_virtual_key()
    conn.execute("UPDATE users SET key_hash=?, key_prefix=? WHERE id=?", (h, prefix, user_id))
    conn.execute(
        "INSERT INTO audit_log(at, actor_user_id, action, target) VALUES(?,?,?,?)",
        (int(time.time()), None, "rotate_key", str(user_id)),
    )
    return raw


def insert_request(conn: sqlite3.Connection, **kw) -> int:
    cols = ",".join(kw.keys())
    placeholders = ",".join(["?"] * len(kw))
    cur = conn.execute(f"INSERT INTO requests({cols}) VALUES({placeholders})", tuple(kw.values()))
    return cur.lastrowid


def create_session(conn: sqlite3.Connection, user_id: int, ttl_s: int = 7*86400) -> str:
    raw = secrets.token_urlsafe(32)
    h = hash_key(raw)
    now = int(time.time())
    conn.execute("INSERT INTO sessions(user_id, token_hash, created_at, expires_at) VALUES(?,?,?,?)", (user_id, h, now, now+ttl_s))
    conn.commit()
    return raw


def find_user_by_session(conn: sqlite3.Connection, raw_token: str) -> sqlite3.Row | None:
    if not raw_token:
        return None
    h = hash_key(raw_token)
    row = conn.execute("SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1 AND u.revoked_at IS NULL", (h, int(time.time()))).fetchone()
    return row


def delete_session(conn: sqlite3.Connection, raw_token: str) -> None:
    h = hash_key(raw_token)
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (h,))
    conn.commit()


def cleanup_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at<?", (int(time.time()),))
    conn.commit()
