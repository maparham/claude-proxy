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
        password_hash TEXT,
        routes_key_hash TEXT,       -- the routes-only key (OpenCode): third-party models only
        routes_key_prefix TEXT
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
    "CREATE INDEX IF NOT EXISTS idx_requests_ended ON requests(ended_at)",   # quota attribution
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
        expires_at INTEGER NOT NULL,
        key_id INTEGER              -- signed in with this machine's key (keys.id): ends with it, never acts as an admin
    )""",
    # When each usage limit's current window opened (limits.py): at the first request after the previous one ended.
    """CREATE TABLE IF NOT EXISTS limit_windows (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        scope TEXT NOT NULL,
        started_at REAL NOT NULL,
        PRIMARY KEY (user_id, kind, scope)
    )""",
    # Keys a user authorized from the browser, one per machine (sign-up design section 3). Their original key stays in users.
    """CREATE TABLE IF NOT EXISTS keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL,
        label TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_used_at INTEGER,
        revoked_at INTEGER
    )""",
    # `claude-gateway on` waiting for someone to authorize it in the browser (sign-up design section 4).
    """CREATE TABLE IF NOT EXISTS device_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_hash TEXT NOT NULL UNIQUE,
        user_code TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        last_poll_at REAL,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        decision TEXT CHECK(decision IN ('approved','denied')),
        ip TEXT                     -- where the request came from, shown on the Authorize page
    )""",
    # ---- paid tickets (design 2026-10-03) ----
    """CREATE TABLE IF NOT EXISTS ticket_prices (
        tier TEXT NOT NULL,
        length TEXT NOT NULL CHECK(length IN ('day','week','month')),
        usd REAL NOT NULL,
        updated_at INTEGER NOT NULL,
        updated_by INTEGER,
        PRIMARY KEY (tier, length)
    )""",
    """CREATE TABLE IF NOT EXISTS ticket_discounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tier TEXT NOT NULL,
        length TEXT NOT NULL CHECK(length IN ('day','week','month')),
        usd REAL NOT NULL,
        starts_at INTEGER NOT NULL,
        ends_at INTEGER NOT NULL,
        created_by INTEGER,
        created_at INTEGER NOT NULL,
        cancelled_at INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ticket_discounts_tier ON ticket_discounts(tier, length, starts_at)",
    # A history: the newest row per currency is the current rate (local units per 1 USD).
    """CREATE TABLE IF NOT EXISTS fx_rates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        currency TEXT NOT NULL,
        rate REAL NOT NULL,
        set_at INTEGER NOT NULL,
        set_by INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fx_rates_currency ON fx_rates(currency, set_at)",
    # A sales record: never deleted. Deleting the user leaves user_id NULL and user_name as it was.
    """CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        user_name TEXT NOT NULL,
        account_id INTEGER NOT NULL DEFAULT 1,
        tier TEXT NOT NULL,
        share_pct REAL NOT NULL,            -- copied at grant: a later config change never touches a sold ticket
        length TEXT NOT NULL CHECK(length IN ('day','week','month')),
        days INTEGER NOT NULL,
        starts_at INTEGER NOT NULL,
        ends_at INTEGER NOT NULL,           -- starts_at + days * 86400; bonus days come on top (TICKET_EFFECTIVE_END)
        list_usd REAL NOT NULL,
        usd REAL NOT NULL,
        discount_id INTEGER,
        currency TEXT NOT NULL,
        rate REAL NOT NULL,
        amount REAL NOT NULL,
        granted_by INTEGER,
        granted_at INTEGER NOT NULL,
        cancelled_at INTEGER,
        cancelled_by INTEGER,
        note TEXT,
        ungated_at INTEGER                  -- set by the Ungate action: this row no longer makes its user ticket-gated
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id, starts_at)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_account ON tickets(account_id, starts_at)",
    """CREATE TABLE IF NOT EXISTS ticket_bonuses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES tickets(id),
        share_pct REAL NOT NULL DEFAULT 0,   -- applies between starts_at and ends_at
        extra_days INTEGER NOT NULL DEFAULT 0,   -- extend the ticket's end at the ticket's own share
        starts_at INTEGER NOT NULL,
        ends_at INTEGER NOT NULL,
        note TEXT,
        granted_by INTEGER,
        granted_at INTEGER NOT NULL,
        cancelled_at INTEGER,
        cancelled_by INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ticket_bonuses_ticket ON ticket_bonuses(ticket_id)",
    # Written by the daily maintenance task (estimates.py), read by /pricing, which never computes anything itself.
    """CREATE TABLE IF NOT EXISTS usage_estimates (
        computed_at INTEGER NOT NULL,
        family TEXT NOT NULL,
        bucket TEXT NOT NULL,
        busy_hours INTEGER NOT NULL,
        p75_share_per_hour REAL,
        PRIMARY KEY (family, bucket)
    )""",
]

# A ticket's effective end: ends_at plus its non-cancelled bonus days. For queries that alias tickets as `t`.
TICKET_EFFECTIVE_END = ("(t.ends_at + 86400 * COALESCE((SELECT SUM(b.extra_days) FROM ticket_bonuses b "
                        "WHERE b.ticket_id=t.id AND b.cancelled_at IS NULL), 0))")


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
    if "routes_key_hash" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN routes_key_hash TEXT")
    if "routes_key_prefix" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN routes_key_prefix TEXT")
    # SQLite can't ADD COLUMN ... UNIQUE; a unique index gives the same guarantee, and NULLs never collide in it.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_routes_key ON users(routes_key_hash)")
    if "key_id" not in _columns(conn, "sessions"):
        conn.execute("ALTER TABLE sessions ADD COLUMN key_id INTEGER")
    if "email" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "clerk_id" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN clerk_id TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_clerk ON users(clerk_id)")


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


FULL_KEY_PREFIX = "sk-proxy-"
ROUTES_KEY_PREFIX = "sk-proxy-r-"   # only for people to tell the keys apart; scope comes from the column that matched


def generate_virtual_key(prefix: str = FULL_KEY_PREFIX) -> tuple[str, str, str]:
    """Returns (raw_key, key_hash, key_prefix). The raw key is shown once and never stored."""
    raw = prefix + secrets.token_urlsafe(32)
    return raw, hash_key(raw), raw[:len(prefix) + 3]


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


def find_user_by_key(conn: sqlite3.Connection, raw_key: str) -> dict | None:
    """The user a gateway key belongs to, with `key_scope`: "full" for their Claude Code key or a machine's key,
    "routes" for their routes-only key, which may only reach third-party routes (app.scope_allows)."""
    h = hash_key(raw_key)
    row = conn.execute("SELECT * FROM users WHERE key_hash=? OR routes_key_hash=?", (h, h)).fetchone()
    if row is not None:
        return {**dict(row), "key_scope": "full" if row["key_hash"] == h else "routes"}
    k = conn.execute("SELECT id, user_id, last_used_at FROM keys WHERE key_hash=? AND revoked_at IS NULL", (h,)).fetchone()
    row = conn.execute("SELECT * FROM users WHERE id=?", (k["user_id"],)).fetchone() if k else None
    if row is None:
        return None
    now = int(time.time())
    if (k["last_used_at"] or 0) < now - 3600:   # at most one write an hour: this runs before every request
        conn.execute("UPDATE keys SET last_used_at=? WHERE id=?", (now, k["id"]))
    # A computer authorized in the browser never carries admin powers, even an admin's: one click on a phished
    # Authorize link must not hand out the gateway.
    return {**dict(row), "key_scope": "full", "role": "user", "machine_key_id": k["id"]}


def add_machine_key(conn: sqlite3.Connection, user_id: int, label: str) -> str:
    """A new full key for one of the user's machines. Returns the raw key, which is never stored."""
    raw, h, prefix = generate_virtual_key()
    conn.execute("INSERT INTO keys(user_id, key_hash, key_prefix, label, created_at) VALUES(?,?,?,?,?)",
                 (user_id, h, prefix, label, int(time.time())))
    audit(conn, user_id, "machine_key_add", _user_name(conn, user_id), {"label": label, "prefix": prefix})
    return raw


def machine_keys(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, key_prefix, label, created_at, last_used_at FROM keys WHERE user_id=? AND revoked_at IS NULL "
                        "ORDER BY created_at DESC, id DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def remove_machine_key(conn: sqlite3.Connection, user_id: int, key_id: int, actor: int | None = None) -> bool:
    n = conn.execute("UPDATE keys SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                     (int(time.time()), key_id, user_id)).rowcount
    if n:
        conn.execute("DELETE FROM sessions WHERE key_id=?", (key_id,))   # a session made from the key ends with it
        audit(conn, actor, "machine_key_remove", _user_name(conn, user_id), {"key_id": key_id})
    return bool(n)


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
    """A new first key. Every machine key goes with the old one: whoever held a leaked first key could have
    minted them, so rotating must leave nothing of theirs behind. All the user's sessions end too."""
    raw, h, prefix = generate_virtual_key()
    conn.execute("UPDATE users SET key_hash=?, key_prefix=? WHERE id=?", (h, prefix, user_id))
    n = conn.execute("UPDATE keys SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                     (int(time.time()), user_id)).rowcount
    # A dashboard session made from the old key, or from a machine key, would otherwise outlive it.
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    audit(conn, actor, "rotate_key", _user_name(conn, user_id), {"machine_keys_revoked": n} if n else None)
    return raw


def set_routes_key(conn: sqlite3.Connection, user_id: int, actor: int | None = None) -> str:
    """Issue or replace a user's routes-only key. Returns the raw key, shown once."""
    u = conn.execute("SELECT revoked_at, routes_key_hash FROM users WHERE id=?", (user_id,)).fetchone()
    if u is None or u["revoked_at"] is not None:
        raise ValueError("No such user, or the user is revoked.")
    raw, h, prefix = generate_virtual_key(ROUTES_KEY_PREFIX)
    conn.execute("UPDATE users SET routes_key_hash=?, routes_key_prefix=? WHERE id=?", (h, prefix, user_id))
    audit(conn, actor, "routes_key_replace" if u["routes_key_hash"] else "routes_key_issue", _user_name(conn, user_id))
    return raw


def remove_routes_key(conn: sqlite3.Connection, user_id: int, actor: int | None = None) -> bool:
    """Delete a user's routes-only key. False when they had none."""
    n = conn.execute("UPDATE users SET routes_key_hash=NULL, routes_key_prefix=NULL "
                     "WHERE id=? AND routes_key_hash IS NOT NULL", (user_id,)).rowcount
    if n:
        audit(conn, actor, "routes_key_remove", _user_name(conn, user_id))
    return bool(n)


def set_enabled(conn: sqlite3.Connection, user_id: int, enabled: bool, actor: int | None = None) -> None:
    conn.execute("UPDATE users SET enabled=? WHERE id=?", (1 if enabled else 0, user_id))
    audit(conn, actor, "enable" if enabled else "disable", _user_name(conn, user_id))


def revoke(conn: sqlite3.Connection, user_id: int, actor: int | None = None) -> None:
    conn.execute("UPDATE users SET revoked_at=?, enabled=0 WHERE id=?", (int(time.time()), user_id))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    audit(conn, actor, "revoke", _user_name(conn, user_id))


def delete_user(conn: sqlite3.Connection, user_id: int, actor: int | None = None, now: float | None = None) -> int:
    """Remove a revoked user and their usage history. Returns the number of requests deleted. Tickets stay as sales
    records with user_id NULL (the FK's ON DELETE SET NULL); a user with an active or queued ticket is not deleted."""
    now = time.time() if now is None else now
    u = conn.execute("SELECT name, revoked_at FROM users WHERE id=?", (user_id,)).fetchone()
    if u is None or u["revoked_at"] is None:
        raise ValueError("Only a revoked user can be deleted; revoke them first.")
    if conn.execute(f"SELECT 1 FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL AND {TICKET_EFFECTIVE_END}>? LIMIT 1",
                    (user_id, now)).fetchone():
        raise ValueError("Cancel the user's active or queued ticket first.")
    n = conn.execute("DELETE FROM requests WHERE user_id=?", (user_id,)).rowcount
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))   # limits and sessions cascade; tickets keep user_name
    audit(conn, actor, "delete_user", u["name"], {"deleted_requests": n})
    return n


def rename_user(conn: sqlite3.Connection, user_id: int, new: str, actor: int | None) -> str:
    """Returns the trimmed name. The name is what the status line and dashboard show; an admin also signs in with it."""
    new = new.strip()
    if not new or len(new) > 64:
        raise ValueError("Need a name of 1-64 characters.")
    old = _user_name(conn, user_id)
    if new == old:
        return new
    if conn.execute("SELECT 1 FROM users WHERE name=? AND id!=?", (new, user_id)).fetchone():
        raise LookupError(f"The name {new!r} is taken.")
    conn.execute("UPDATE users SET name=? WHERE id=?", (new, user_id))
    audit(conn, actor, "rename", f"{old}->{new}")
    return new


def set_session_title(conn: sqlite3.Connection, user_id: int, session_id: str, title: str) -> None:
    # Keyed by user too: the session id comes from the client, so one user can never rename another's session.
    conn.execute("INSERT INTO session_titles(user_id, session_id, title, updated_at) VALUES(?,?,?,?) "
                 "ON CONFLICT(user_id, session_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at",
                 (user_id, session_id, title, time.time()))


def insert_request(conn: sqlite3.Connection, **kw) -> int:
    cols = ",".join(kw.keys())
    placeholders = ",".join(["?"] * len(kw))
    return conn.execute(f"INSERT INTO requests({cols}) VALUES({placeholders})", tuple(kw.values())).lastrowid


def create_session(conn: sqlite3.Connection, user_id: int, ttl_s: int = 7 * 86400, key_id: int | None = None) -> tuple[str, str]:
    """Returns (session_token, csrf_token). `key_id`: signed in with that machine key."""
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = int(time.time())
    conn.execute("INSERT INTO sessions(user_id, token_hash, csrf_token, created_at, expires_at, key_id) VALUES(?,?,?,?,?,?)",
                 (user_id, hash_key(raw), csrf, now, now + ttl_s, key_id))
    return raw, csrf


def find_session(conn: sqlite3.Connection, raw_token: str) -> dict | None:
    if not raw_token:
        return None
    row = conn.execute(
        "SELECT u.*, s.csrf_token AS csrf_token, s.key_id AS session_key_id FROM users u JOIN sessions s ON s.user_id=u.id "
        "WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1 AND u.revoked_at IS NULL AND (s.key_id IS NULL OR "
        "EXISTS(SELECT 1 FROM keys k WHERE k.id=s.key_id AND k.revoked_at IS NULL))",
        (hash_key(raw_token), int(time.time())),
    ).fetchone()
    if row is None:
        return None
    return {**dict(row), "key_scope": "full"} | ({"role": "user"} if row["session_key_id"] else {})


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
        "device_requests": conn.execute("DELETE FROM device_requests WHERE expires_at<?", (int(now),)).rowcount,
    }
    return out
