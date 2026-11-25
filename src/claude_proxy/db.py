from __future__ import annotations

import hashlib
import os
import json
import logging
import re
import secrets
import sqlite3
import time
from pathlib import Path

from . import plans
from .plans import DEFAULT_FACTORS, detect

logger = logging.getLogger("claude_proxy")

# Named so the migration that rebuilds old single-grant tables reuses it verbatim.
CREDENTIALS_DDL = """CREATE TABLE IF NOT EXISTS credentials (
    backend TEXT,
    account TEXT DEFAULT 'default',
    encrypted_blob TEXT,
    expires_at INTEGER,
    updated_at INTEGER,
    state TEXT,
    label TEXT,               -- display name for the dashboard; defaults to the account slug
    anthropic_sub TEXT,       -- stable Anthropic account id for duplicate detection (NULL on old grants)
    email TEXT,               -- display only; the encrypted blob stays the source of truth
    PRIMARY KEY (backend, account)
)"""

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
        routes_key_prefix TEXT,
        account TEXT                -- the subscription account serving this user; NULL: unassigned
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
        rejected_by TEXT,
        api_key_id INTEGER,
        ip TEXT,
        account TEXT DEFAULT 'default'
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
        raw_json TEXT,
        account TEXT NOT NULL DEFAULT 'default'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_quota_bucket_time ON quota_snapshots(bucket, observed_at)",
    # The subscription's plan, a row each time it changes (quota.record_plan): tokens per point differ by plan.
    """CREATE TABLE IF NOT EXISTS account_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        observed_at REAL NOT NULL,
        organization_type TEXT,
        rate_limit_tier TEXT,
        account TEXT NOT NULL DEFAULT 'default'
    )""",
    # The subscription's latest profile from Anthropic, whole, for the admin's subscription details (quota.record_profile).
    """CREATE TABLE IF NOT EXISTS account_profiles (
        account TEXT PRIMARY KEY,
        observed_at REAL NOT NULL,
        raw_json TEXT NOT NULL
    )""",
    CREDENTIALS_DDL,
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
    # The full request and reply of API clients' requests (contents.py), zlib-compressed; gone with its request.
    """CREATE TABLE IF NOT EXISTS request_contents (
        request_id INTEGER PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
        request BLOB NOT NULL,
        reply BLOB NOT NULL,
        stream INTEGER NOT NULL,
        truncated INTEGER NOT NULL DEFAULT 0
    )""",
    # Latest title Claude Code gave each of its sessions (titles.py), or its first prompt (prompts.py).
    """CREATE TABLE IF NOT EXISTS session_titles (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL,
        title TEXT NOT NULL,
        updated_at REAL NOT NULL,
        from_prompt INTEGER NOT NULL DEFAULT 0,   -- 1: the start of the session's first prompt, not Claude Code's title
        PRIMARY KEY (user_id, session_id)
    )""",
    # What the person typed in a Claude Code session (prompts.py), for the admin only; never a reply.
    """CREATE TABLE IF NOT EXISTS prompts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL,
        request_id INTEGER,         -- the request it arrived in (requests.id)
        at REAL NOT NULL,
        seq INTEGER NOT NULL,       -- the turn's place in the conversation: with the text, tells a retry from a repeat
        text TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(user_id, session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_prompts_at ON prompts(at)",
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
        revoked_at INTEGER,
        client_version TEXT         -- the gclaude version its statusline last reported (set_client_version)
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
    """CREATE TABLE IF NOT EXISTS ticket_tier_shares (
        tier TEXT PRIMARY KEY,
        share_pct REAL NOT NULL,             -- unread since sizes (2026-11-24); new rows write 0
        updated_at INTEGER NOT NULL,
        updated_by INTEGER,
        share_5h_pct REAL,                   -- unread since sizes
        compare TEXT,                        -- unread since sizes: the pages compare with Claude Pro by size
        compare_ratio REAL,                  -- unread since sizes
        size REAL,                           -- weekly size in Claude Pro weeks; NULL: the config's
        size_5h REAL                         -- 5-hour size; NULL: the config's, else size
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
        ungated_at INTEGER,                 -- set by the Ungate action: this row no longer makes its user ticket-gated
        share_5h_pct REAL,                  -- copied at grant like share_pct; NULL (sold before the split): share_pct
        used_up_at INTEGER,                 -- set: used up before its end, it ended here and the user's next ticket took over
        ref_off_usd REAL,                   -- the referral discount alone, in USD, set at grant; NULL: no referral
        refunded_at INTEGER,                -- set: the buyer cancelled it unused and its payment was reversed (refunds.py)
        account TEXT NOT NULL DEFAULT 'default',   -- the subscription account holding the pass (replaces account_id)
        size REAL,                          -- in Pro weeks, weekly window; NULL: sold before sizes, share_pct rules
        size_5h REAL                        -- in Pro weeks, 5-hour window
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id, starts_at)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_account ON tickets(account_id, starts_at)",
    # A pass moved between accounts: what it had used is carried over in points of the account it went to.
    """CREATE TABLE IF NOT EXISTS ticket_moves (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES tickets(id),
        moved_at INTEGER NOT NULL,
        from_account TEXT NOT NULL,
        to_account TEXT NOT NULL,
        from_share_pct REAL NOT NULL,
        from_share_5h_pct REAL,
        carry_7d_pts REAL NOT NULL DEFAULT 0,
        carry_5h_pts REAL NOT NULL DEFAULT 0,
        moved_by INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ticket_moves_ticket ON ticket_moves(ticket_id, moved_at)",
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
        cancelled_by INTEGER,
        tier TEXT,                           -- set: a paid upgrade to this tier (2026-10-26 spec), its share the difference
        share_5h_pct REAL                    -- the 5-hour share it adds; NULL: share_pct
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ticket_bonuses_ticket ON ticket_bonuses(ticket_id)",
    # Written by the daily maintenance task (estimates.py), read by /pricing, which never computes anything itself.
    """CREATE TABLE IF NOT EXISTS usage_estimates (
        computed_at INTEGER NOT NULL,
        family TEXT NOT NULL,
        bucket TEXT NOT NULL,
        busy_hours INTEGER NOT NULL,
        p75_share_per_hour REAL,
        account TEXT NOT NULL,
        PRIMARY KEY (account, family, bucket)
    )""",
    # ---- order requests (design 2026-10-04) ----
    # A request to buy a ticket, never removed. Deleting the user leaves user_id NULL and name and email as they were.
    """CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,   -- NULL: a visitor order not linked to an account
        name TEXT NOT NULL,
        email TEXT NOT NULL,                -- for a signed-in user, the account email or the one typed (never written to users)
        tier TEXT NOT NULL,
        length TEXT NOT NULL CHECK(length IN ('day','week','month')),
        currency TEXT NOT NULL,
        quoted_usd REAL NOT NULL,           -- the price the dialog showed; for reference, a grant re-prices
        quoted_rate REAL NOT NULL,
        quoted_amount REAL NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK(status IN ('new','contacted','done','declined','withdrawn')),
        admin_note TEXT NOT NULL DEFAULT '',
        ticket_id INTEGER REFERENCES tickets(id),
        ip TEXT,                            -- a visitor's address, for the per-IP limit; cleared on close and after 30 days
        admin_mail TEXT NOT NULL CHECK(admin_mail IN ('pending','sent','failed','off','skipped')),
        buyer_mail TEXT NOT NULL CHECK(buyer_mail IN ('pending','sent','failed','off','skipped')),
        terms_version TEXT NOT NULL DEFAULT 'v1',   -- ticket terms the buyer accepted (2026-10-13 spec, §3)
        lang TEXT NOT NULL DEFAULT 'en',    -- the page's language at order time: the buyer's emails use it ('en' or 'fa')
        via TEXT NOT NULL DEFAULT 'web',    -- where the buyer ordered: 'web' or 'telegram' (where they hear back)
        seen_at INTEGER,                    -- the buyer dismissed this closed order
        upgrade_of INTEGER REFERENCES tickets(id),  -- set: an upgrade of this ticket to `tier` for its time left
        start_now INTEGER NOT NULL DEFAULT 0        -- 1: the ticket starts when granted, beside the buyer's running ones
    )""",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)",
    # Online payments (payments design, section 3): one row per attempt at paying an order through ZarinPal.
    """CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        amount INTEGER NOT NULL,            -- Toman, the order's quote when the payment started
        authority TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('started','cancelled','failed','expired','paid','paid_unfulfilled')),
        ref_id TEXT,
        card_pan TEXT,                      -- ZarinPal's masked card number; the card hash is never stored
        error TEXT,
        made_order INTEGER NOT NULL DEFAULT 1,  -- 1: the order was created for this payment; 0: it pays an order sent earlier
        paid_at INTEGER,                    -- when it was verified and granted (status paid); the refund window counts from here
        reverse_started_at INTEGER,         -- a ZarinPal reverse was asked for; still set with reversed_at NULL: its result is unknown
        reversed_at INTEGER                 -- ZarinPal reversed it: the money went back to the buyer's card
    )""",
    "CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status, created_at)",
    # ---- API keys on prepaid credit (design 2026-10-21) ----
    """CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL,
        label TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_used_at INTEGER,
        revoked_at INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)",
    # Pass app keys (design 2026-11-18): any Anthropic-format app on a running pass.
    """CREATE TABLE IF NOT EXISTS pass_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL,
        label TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_used_at INTEGER,
        revoked_at INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pass_keys_user ON pass_keys(user_id)",
    # USD per million tokens per model glob; the longest matching pattern wins.
    """CREATE TABLE IF NOT EXISTS api_rates (
        pattern TEXT PRIMARY KEY,
        input REAL NOT NULL,
        output REAL NOT NULL,
        cache_write_5m REAL NOT NULL,
        cache_write_1h REAL NOT NULL,
        cache_read REAL NOT NULL,
        updated_at INTEGER NOT NULL,
        updated_by INTEGER
    )""",
    # Append-only: a balance is the sum of its rows. Never touched by retention cleanup.
    """CREATE TABLE IF NOT EXISTS api_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at REAL NOT NULL,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        kind TEXT NOT NULL CHECK(kind IN ('grant','charge')),
        usd REAL NOT NULL,                  -- grants as given (+/-), charges negative
        api_key_id INTEGER,
        request_id INTEGER,
        model TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        actor_user_id INTEGER,
        note TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_api_ledger_user ON api_ledger(user_id, at)",
    "CREATE INDEX IF NOT EXISTS idx_api_ledger_key ON api_ledger(api_key_id, at)",
    """CREATE TABLE IF NOT EXISTS api_account_limits (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        max_inflight INTEGER,               -- NULL: [api].max_inflight
        rpm INTEGER,                        -- NULL: [api].rpm
        updated_at INTEGER NOT NULL,
        updated_by INTEGER
    )""",
    # A pass borrowing from its next part (2026-10-30 spec, §3): how to measure, after the reset, what it took.
    """CREATE TABLE IF NOT EXISTS pass_borrows (
        ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
        week_end INTEGER NOT NULL,          -- the Anthropic week it borrowed in: its reset time then
        own_pts REAL NOT NULL,              -- its own limit that week, in account points; below 0: debt carried in
        rate REAL NOT NULL,                 -- the observed 7-day rate it was measured at, weighted tokens per point
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (ticket_id, week_end)
    )""",
    # Telegram bot (design 2026-11-05, section 3). A private chat's id is the Telegram user's id.
    """CREATE TABLE IF NOT EXISTS telegram_chats (
        chat_id INTEGER PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,   -- NULL until linked; at most one chat per account
        username TEXT,
        first_name TEXT,
        lang TEXT NOT NULL DEFAULT 'fa',
        state TEXT,                       -- what the next plain text means: 'feedback' | 'email' | NULL (support)
        pending TEXT,                     -- JSON: the purchase being chosen (tier, length, start_now, nonce)
        blocked INTEGER NOT NULL DEFAULT 0,
        last_seen INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS telegram_chats_user ON telegram_chats(user_id) WHERE user_id IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS telegram_link_codes (
        code_hash TEXT PRIMARY KEY,       -- sha256 of the code; the code itself is never stored
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires_at INTEGER NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY, at INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS inbox_messages (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL REFERENCES telegram_chats(chat_id) ON DELETE CASCADE,
        kind TEXT NOT NULL,               -- 'support' | 'feedback'
        direction TEXT NOT NULL,          -- 'in' (from the user) | 'out' (an admin's answer)
        media TEXT,                       -- NULL (text) | 'photo' | 'document' | 'voice'
        text TEXT NOT NULL,               -- the text, or a file's caption ('' when none)
        actor INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS inbox_chat ON inbox_messages(chat_id, id)",
    # Each admin-group message posted for an incoming one (its header and its copy): a reply to either maps back.
    """CREATE TABLE IF NOT EXISTS inbox_admin_msgs (
        admin_msg_id INTEGER PRIMARY KEY,
        inbox_id INTEGER NOT NULL REFERENCES inbox_messages(id) ON DELETE CASCADE
    )""",
    # ---- referrals (design 2026-11-07) ----
    # A saved discount: one unit is a percentage off one pass, earned when a friend's purchase counts. `state`
    # tracks it through saved -> held (by an open order) -> used (the order's payment verified or it was granted),
    # or expired / cancelled. `order_id` is the order currently holding or that used it; `reward_id` is the
    # referral_rewards row it was earned from.
    """CREATE TABLE IF NOT EXISTS referral_units (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        pct REAL NOT NULL,
        earned_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('saved','held','used','expired','cancelled')),
        order_id INTEGER,
        reward_id INTEGER,
        updated_at INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_referral_units_user ON referral_units(user_id, state)",
    # Free trial claims (2026-11-14): one per email, ever. user_id goes NULL with the account; email stays, so signing
    # up again with it gets no second trial. ticket_id: set once started. granted_by: the admin; NULL: self-service.
    """CREATE TABLE IF NOT EXISTS trial_claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        email TEXT NOT NULL,
        claimed_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        granted_by INTEGER,
        ticket_id INTEGER,
        started_at INTEGER,
        cancelled_at INTEGER,
        cancelled_by INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_trial_claims_user ON trial_claims(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_trial_claims_email ON trial_claims(email)",
    # One row per friend purchase that could earn the referrer a unit. `referrer_id` has no foreign key, so the
    # history survives a deleted referrer (the referrer simply earns nothing further).
    """CREATE TABLE IF NOT EXISTS referral_rewards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER NOT NULL,
        friend_id INTEGER,
        order_id INTEGER NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('waiting','earned','refused','over_limit')),
        decided_by INTEGER,
        decided_at INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_referral_rewards_referrer ON referral_rewards(referrer_id, status)",
    # Where each user connects from (2026-11-08), for the admin only: one row per address and source, "gateway" for
    # Claude Code and API requests, "dashboard" for sign-ins to the dashboard.
    """CREATE TABLE IF NOT EXISTS user_ips (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        ip TEXT NOT NULL,
        source TEXT NOT NULL CHECK(source IN ('gateway','dashboard')),
        first_seen INTEGER NOT NULL,
        last_seen INTEGER NOT NULL,
        count INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (user_id, ip, source)
    )""",
    # Where an address is (geo.py), looked up once per address; an address without a row has no known place.
    """CREATE TABLE IF NOT EXISTS ip_geo (
        ip TEXT PRIMARY KEY,
        city TEXT,
        region TEXT,
        country TEXT NOT NULL,      -- ISO 3166 two-letter code; the dashboard names it in the page's language
        looked_up_at INTEGER NOT NULL
    )""",

]

# A ticket's effective end: ends_at plus its non-cancelled bonus days, or the moment it was used up and handed over to
# the next ticket. For queries that alias tickets as `t`.
TICKET_EFFECTIVE_END = ("COALESCE(t.used_up_at, t.ends_at + 86400 * COALESCE((SELECT SUM(b.extra_days) FROM ticket_bonuses b "
                        "WHERE b.ticket_id=t.id AND b.cancelled_at IS NULL), 0))")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring databases created by earlier versions up to the current schema."""
    if "account" not in _columns(conn, "credentials"):
        # Single-grant databases (PRIMARY KEY on backend alone) become the 'default' account.
        # One transaction: a crash midway must not strand the only grant in credentials_old.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE credentials RENAME TO credentials_old")
            conn.execute(CREDENTIALS_DDL)
            conn.execute("INSERT INTO credentials(backend, account, encrypted_blob, expires_at, updated_at, state) "
                         "SELECT backend, 'default', encrypted_blob, expires_at, updated_at, state FROM credentials_old")
            conn.execute("DROP TABLE credentials_old")
            # The pre-account pending login is unreadable now (spec section 2); drop its verifier.
            conn.execute("DELETE FROM settings WHERE key='pending_login'")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    for col in ("label TEXT", "anthropic_sub TEXT", "email TEXT"):
        if col.split()[0] not in _columns(conn, "credentials"):
            conn.execute(f"ALTER TABLE credentials ADD COLUMN {col}")
    # One subscription, one slug: enforced here as well as checked in login.finish.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_sub ON credentials(backend, anthropic_sub) "
                 "WHERE anthropic_sub IS NOT NULL")
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
    if "from_prompt" not in _columns(conn, "session_titles"):
        conn.execute("ALTER TABLE session_titles ADD COLUMN from_prompt INTEGER NOT NULL DEFAULT 0")
        # Titles prompts.save wrote before the column existed: it gives a title its prompt's own time.
        conn.execute("UPDATE session_titles SET from_prompt=1 WHERE EXISTS (SELECT 1 FROM prompts p WHERE p.user_id="
                     "session_titles.user_id AND p.session_id=session_titles.session_id AND p.at=session_titles.updated_at)")
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
    if "client_version" not in _columns(conn, "keys"):
        conn.execute("ALTER TABLE keys ADD COLUMN client_version TEXT")
    if "terms_version" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN terms_version TEXT NOT NULL DEFAULT 'v1'")
    if "lang" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'")
    if "via" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN via TEXT NOT NULL DEFAULT 'web'")
    for table in ("ticket_tier_shares", "tickets", "ticket_bonuses"):
        if "share_5h_pct" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN share_5h_pct REAL")
    for col in ("compare TEXT", "compare_ratio REAL"):
        if col.split()[0] not in _columns(conn, "ticket_tier_shares"):
            conn.execute(f"ALTER TABLE ticket_tier_shares ADD COLUMN {col}")
    if "used_up_at" not in _columns(conn, "tickets"):
        conn.execute("ALTER TABLE tickets ADD COLUMN used_up_at INTEGER")
    if "tier" not in _columns(conn, "ticket_bonuses"):
        conn.execute("ALTER TABLE ticket_bonuses ADD COLUMN tier TEXT")
    if "upgrade_of" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN upgrade_of INTEGER REFERENCES tickets(id)")
    if "start_now" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN start_now INTEGER NOT NULL DEFAULT 0")
    # Admin payment links (spec 2026-11-04): NULL on every other order. SQLite cannot add a UNIQUE column.
    if "pay_code" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN pay_code TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_pay_code ON orders(pay_code)")
    if "pay_by" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN pay_by INTEGER")
    if "api_key_id" not in _columns(conn, "requests"):
        conn.execute("ALTER TABLE requests ADD COLUMN api_key_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_api_key ON requests(api_key_id, started_at)")
    if "pass_key_id" not in _columns(conn, "requests"):
        conn.execute("ALTER TABLE requests ADD COLUMN pass_key_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_pass_key ON requests(pass_key_id, started_at)")
    if "ticket_id" not in _columns(conn, "requests"):
        # The pass that served the request (2026-10-28): each pass's limits count only its own requests, as if another
        # user held it. Requests from before go to the pass that covered them, the earliest when passes ran side by side.
        conn.execute("ALTER TABLE requests ADD COLUMN ticket_id INTEGER")
        conn.execute(f"UPDATE requests SET ticket_id=(SELECT t.id FROM tickets t WHERE t.user_id=requests.user_id "
                     f"AND t.cancelled_at IS NULL AND t.starts_at<=requests.started_at AND {TICKET_EFFECTIVE_END}>requests.started_at "
                     f"ORDER BY t.starts_at, t.id LIMIT 1) WHERE user_id IN (SELECT user_id FROM tickets) AND rejected_by IS NULL "
                     f"AND api_key_id IS NULL AND started_at>?", (time.time() - 40 * 86400,))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_ticket ON requests(ticket_id, ended_at)")
    if "pass_pin" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN pass_pin INTEGER")   # the running pass the user chose to serve first
    if "pass_pin_hold" not in _columns(conn, "users"):
        # When the pinned pass was chosen while at a limit (epoch seconds; 0: no hold). Requests wait for it rather than
        # use another pass until it serves again, or a pass of the user's starts after that moment (2026-11-06)
        conn.execute("ALTER TABLE users ADD COLUMN pass_pin_hold INTEGER NOT NULL DEFAULT 0")
    if "made_order" not in _columns(conn, "payments"):
        conn.execute("ALTER TABLE payments ADD COLUMN made_order INTEGER NOT NULL DEFAULT 1")
    # ---- referrals (design 2026-11-07) ----
    if "ref_code" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN ref_code TEXT")   # the user's personal link code; NULL until given one
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_ref_code ON users(ref_code) WHERE ref_code IS NOT NULL")
    if "referred_by" not in _columns(conn, "users"):
        # Fixed at Clerk sign-up and never changed after; survives the referrer's own account being deleted later.
        conn.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
    if "ref_kind" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN ref_kind TEXT")   # 'friend', 'unit' or NULL: which discount applied
    if "ref_pct" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN ref_pct REAL")   # the percentage asked for, before the cost floor lowered it
    if "ref_off_usd" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN ref_off_usd REAL")
    if "ref_unit_id" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN ref_unit_id INTEGER")   # the referral_units row a unit discount held/used
    if "list_usd" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN list_usd REAL")   # the price before the referral discount
    if "ref_off_usd" not in _columns(conn, "tickets"):
        conn.execute("ALTER TABLE tickets ADD COLUMN ref_off_usd REAL")   # the referral part alone of list_usd - usd
    if "refunded_at" not in _columns(conn, "tickets"):
        conn.execute("ALTER TABLE tickets ADD COLUMN refunded_at INTEGER")
    if "windows" not in _columns(conn, "orders"):   # set by fe181f9 (a Windows browser), no longer read: every pass mail has the Windows setup
        conn.execute("ALTER TABLE orders ADD COLUMN windows INTEGER NOT NULL DEFAULT 0")
    if "ip" not in _columns(conn, "requests"):   # the address each request came from (2026-11-10); NULL before
        conn.execute("ALTER TABLE requests ADD COLUMN ip TEXT")
    if "source" not in _columns(conn, "referral_units"):   # 'trial': a free trial's follow-up discount; NULL: earned by referring
        conn.execute("ALTER TABLE referral_units ADD COLUMN source TEXT")
    if "pct" not in _columns(conn, "ticket_discounts"):   # a percentage off the regular price (2026-11-17); NULL: usd is the price
        conn.execute("ALTER TABLE ticket_discounts ADD COLUMN pct REAL")
    for col in ("cancelled_at", "cancelled_by"):   # an admin's cancel of a trial claim waiting to start (2026-11-17)
        if col not in _columns(conn, "trial_claims"):
            conn.execute(f"ALTER TABLE trial_claims ADD COLUMN {col} INTEGER")
    if "trial" not in _columns(conn, "tickets"):   # a free trial pass (2026-11-14): price 0, never upgraded or refunded
        conn.execute("ALTER TABLE tickets ADD COLUMN trial INTEGER NOT NULL DEFAULT 0")
    for col in ("paid_at", "reverse_started_at", "reversed_at"):   # refunds within 15 minutes (2026-11-08)
        if col not in _columns(conn, "payments"):
            conn.execute(f"ALTER TABLE payments ADD COLUMN {col} INTEGER")
    # One key type (2026-11-19): every live credit API key (sk-proxy-a-) becomes a pass app key with the same hash, so
    # the raw key keeps working, now on the user's pass. The api_keys rows stay, revoked, for history.
    live = conn.execute("SELECT id, user_id, key_hash, key_prefix, label, created_at, last_used_at FROM api_keys "
                        "WHERE revoked_at IS NULL").fetchall()
    if live:
        now = int(time.time())
        for a in live:
            conn.execute("INSERT INTO pass_keys(user_id, key_hash, key_prefix, label, created_at, last_used_at) "
                         "SELECT ?,?,?,?,?,? WHERE NOT EXISTS(SELECT 1 FROM pass_keys WHERE key_hash=?)",
                         (a["user_id"], a["key_hash"], a["key_prefix"], a["label"], a["created_at"], a["last_used_at"], a["key_hash"]))
        conn.execute(f"UPDATE api_keys SET revoked_at=? WHERE id IN ({','.join('?' * len(live))})", (now, *(a["id"] for a in live)))
        audit(conn, None, "api_keys_to_pass_keys", "gateway", {"keys": len(live)})
    # ---- serve from every linked subscription (design 2026-11-24): accounts named on each row ----
    if "account" not in _columns(conn, "requests"):
        conn.execute("ALTER TABLE requests ADD COLUMN account TEXT DEFAULT 'default'")
        conn.execute("UPDATE requests SET account='default'")
    if "account" not in _columns(conn, "quota_snapshots"):
        conn.execute("ALTER TABLE quota_snapshots ADD COLUMN account TEXT NOT NULL DEFAULT 'default'")
    if "account" not in _columns(conn, "account_plans"):
        conn.execute("ALTER TABLE account_plans ADD COLUMN account TEXT NOT NULL DEFAULT 'default'")
    if "account" not in _columns(conn, "tickets"):
        conn.execute("ALTER TABLE tickets ADD COLUMN account TEXT NOT NULL DEFAULT 'default'")   # account_id stays, unread
    for col in ("size", "size_5h"):
        if col not in _columns(conn, "tickets"):
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} REAL")
            # Pro weeks = share x f(default) / 100, when the default account's plan is known; else left NULL.
            row = conn.execute("SELECT organization_type, rate_limit_tier FROM account_plans WHERE account='default' "
                               "ORDER BY observed_at DESC, id DESC LIMIT 1").fetchone()
            p = detect(row[0], row[1]) if row else None
            if p:
                f = DEFAULT_FACTORS[p]["7d" if col == "size" else "5h"]
                src = "share_pct" if col == "size" else "COALESCE(share_5h_pct, share_pct)"
                conn.execute(f"UPDATE tickets SET {col}={src} * ? / 100", (f,))
    if "account" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN account TEXT")
        conn.execute("UPDATE users SET account='default'")
    # Once: users made between the release that added users.account and the one that assigns new users wrote none,
    # though every request was still served from 'default'. They keep it; later NULLs are left to placement.assign.
    if not conn.execute("SELECT 1 FROM settings WHERE key=?", (USERS_ACCOUNT_FIXED_KEY,)).fetchone():
        if conn.execute("SELECT 1 FROM credentials WHERE backend='oauth' AND account='default'").fetchone():
            conn.execute("UPDATE users SET account='default' WHERE account IS NULL")
        conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (USERS_ACCOUNT_FIXED_KEY, str(int(time.time()))))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_account_ended ON requests(account, ended_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quota_account_bucket_time ON quota_snapshots(account, bucket, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_acct ON tickets(account, starts_at)")
    conn.execute("INSERT INTO settings(key, value) SELECT 'account_plan_ack:default', COALESCE(rate_limit_tier, organization_type) "
                 "FROM account_plans WHERE account='default' AND COALESCE(rate_limit_tier, organization_type) IS NOT NULL "
                 "ORDER BY observed_at DESC, id DESC LIMIT 1 ON CONFLICT(key) DO NOTHING")
    # Account profiles, usage estimates and the subscription's end, per account (multi-subscription task 2).
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_profile'").fetchone():
        conn.execute("INSERT INTO account_profiles(account, observed_at, raw_json) SELECT 'default', observed_at, raw_json "
                     "FROM account_profile WHERE id=1 ON CONFLICT(account) DO NOTHING")
        conn.execute("DROP TABLE account_profile")
    if "account" not in _columns(conn, "usage_estimates"):
        # One transaction: a crash midway must not leave the estimates in usage_estimates_old, which the next start
        # would not rebuild from (the new table exists by then).
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE usage_estimates RENAME TO usage_estimates_old")
            conn.execute(next(s for s in SCHEMA if "TABLE IF NOT EXISTS usage_estimates" in s))
            conn.execute("INSERT INTO usage_estimates(computed_at, family, bucket, busy_hours, p75_share_per_hour, account) "
                         "SELECT computed_at, family, bucket, busy_hours, p75_share_per_hour, 'default' FROM usage_estimates_old")
            conn.execute("DROP TABLE usage_estimates_old")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    conn.execute("UPDATE OR IGNORE settings SET key='subscription_ends_at:default' WHERE key='subscription_ends_at'")
    conn.execute("DELETE FROM settings WHERE key='subscription_ends_at'")
    # Pass sizes in Claude Pro weeks (multi-subscription task 4): today's shares were of the default account, so with its
    # plan's factors each tier, the trial and the referral cost keep exactly what they hold there now. With its plan
    # unknown nothing is guessed: the conversion waits (SIZES_PENDING_KEY), those tiers sell nothing meanwhile
    # (tickets.tier_size), and convert_pending_sizes finishes it once the plan is known.
    if "size" not in _columns(conn, "ticket_tier_shares"):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for col in ("size", "size_5h"):
                conn.execute(f"ALTER TABLE ticket_tier_shares ADD COLUMN {col} REAL")
            p = plans.plan(conn, "default")
            if p is None:
                logger.warning("migration: the default account's plan is unknown, so the pass sizes wait for it; until it "
                               "is set on the Subscriptions page the tiers already in the database do not sell")
                # The referral cost as it stands, so the conversion later leaves one the admin has set meanwhile.
                conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                             (SIZES_PENDING_KEY, json.dumps({"at": int(time.time()), "account_cost_usd": _referral_cost(conn)})))
            else:
                _convert_sizes(conn, plans.factors(conn)[p])
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    convert_pending_sizes(conn)   # a plan picked or detected by another process since


USERS_ACCOUNT_FIXED_KEY = "migration:users_account_null_fixed"
SIZES_PENDING_KEY = "migration:sizes_pending"


def sizes_pending(conn: sqlite3.Connection) -> bool:
    """Whether the pass sizes still wait for the default account's plan (_migrate)."""
    return conn.execute("SELECT 1 FROM settings WHERE key=?", (SIZES_PENDING_KEY,)).fetchone() is not None


def _convert_sizes(conn: sqlite3.Connection, f: dict, cost_was=None) -> None:
    """Today's shares of the default account become sizes with its plan's factors `f`: the tiers (a size set on the
    Pricing tab meanwhile stays), its passes sold before sizes (their shares untouched), the trial and the referral cost
    (`cost_was`: converted only while it is still that, the value at the migration; None: as it is)."""
    conn.execute("UPDATE ticket_tier_shares SET size=share_pct * ? / 100 WHERE size IS NULL", (f["7d"],))
    conn.execute("UPDATE ticket_tier_shares SET size_5h=share_5h_pct * ? / 100 WHERE size_5h IS NULL AND share_5h_pct IS NOT NULL",
                 (f["5h"],))
    conn.execute("UPDATE tickets SET size=share_pct * ? / 100 WHERE size IS NULL AND account='default'", (f["7d"],))
    conn.execute("UPDATE tickets SET size_5h=COALESCE(share_5h_pct, share_pct) * ? / 100 WHERE size_5h IS NULL AND account='default'",
                 (f["5h"],))
    _migrate_trial_sizes(conn, f)
    if cost_was is None or _referral_cost(conn) == cost_was:
        _migrate_referral_cost(conn, f["7d"])


def convert_pending_sizes(conn: sqlite3.Connection, actor: int | None = None) -> bool:
    """Finish the pass-size conversion _migrate left waiting, once the default account's plan is known (the admin picks
    it, or the poller detects or acknowledges it). Runs once, in one transaction; True when it ran now."""
    if not sizes_pending(conn) or plans.plan(conn, "default") is None:
        return False
    nested = conn.in_transaction
    conn.execute("SAVEPOINT convert_sizes" if nested else "BEGIN IMMEDIATE")
    try:
        p = plans.plan(conn, "default")
        ran = sizes_pending(conn) and p is not None   # again under the write lock: another process may have run it
        if ran:
            try:
                was = json.loads(conn.execute("SELECT value FROM settings WHERE key=?", (SIZES_PENDING_KEY,)).fetchone()[0])
            except ValueError:
                was = {}
            _convert_sizes(conn, plans.factors(conn)[p], was.get("account_cost_usd") if isinstance(was, dict) else None)
            conn.execute("DELETE FROM settings WHERE key=?", (SIZES_PENDING_KEY,))
            audit(conn, actor, "sizes_converted", "default", {"plan": p})
        conn.execute("RELEASE convert_sizes" if nested else "COMMIT")
    except BaseException:
        conn.execute("ROLLBACK TO convert_sizes" if nested else "ROLLBACK")
        if nested:
            conn.execute("RELEASE convert_sizes")
        raise
    if ran:
        logger.info("the pass sizes were converted with the default account's plan (%s)", p)
    return ran


def _migrate_trial_sizes(conn: sqlite3.Connection, f: dict) -> None:
    """The trial's share overrides become sizes with the default account's factors."""
    row = conn.execute("SELECT value FROM settings WHERE key='trial'").fetchone()
    if not row:
        return
    s = json.loads(row[0])
    for old, new, b in (("share_pct", "size", "7d"), ("share_5h_pct", "size_5h", "5h")):
        v = s.pop(old, None)
        if v is not None:
            s[new] = v * f[b] / 100
    conn.execute("UPDATE settings SET value=? WHERE key='trial'", (json.dumps(s),))


def _referral_cost(conn: sqlite3.Connection):
    """The stored referral account_cost_usd, the old $100 default when never saved."""
    row = conn.execute("SELECT value FROM settings WHERE key='referrals'").fetchone()
    return (json.loads(row[0]) if row else {}).get("account_cost_usd", 100.0)


def _migrate_referral_cost(conn: sqlite3.Connection, f7: float) -> None:
    """account_cost_usd was the default account's cost per 30 days (default $100); it is now one Claude Pro's worth of
    usage per 30 days (default $20), so the floor a pass is sold above stays where it was. A gateway that never saved
    the setting had $100 too, so it is stored converted."""
    row = conn.execute("SELECT value FROM settings WHERE key='referrals'").fetchone()
    s = json.loads(row[0]) if row else {}
    cost = s.get("account_cost_usd", 100.0)
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        s["account_cost_usd"] = cost / f7
        conn.execute("INSERT INTO settings(key, value) VALUES('referrals', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (json.dumps(s),))


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
PASS_KEY_PREFIX = "sk-proxy-p-"     # a pass app key (2026-11-18): any Anthropic-format app, served while the user's pass runs



def generate_virtual_key(prefix: str = FULL_KEY_PREFIX) -> tuple[str, str, str]:
    """Returns (raw_key, key_hash, key_prefix). The raw key is shown once and never stored."""
    raw = prefix + secrets.token_urlsafe(32)
    return raw, hash_key(raw), raw[:len(prefix) + 3]


def audit(conn: sqlite3.Connection, actor_user_id: int | None, action: str, target: str, detail: dict | None = None) -> None:
    conn.execute("INSERT INTO audit_log(at, actor_user_id, action, target, detail_json) VALUES(?,?,?,?,?)",
                 (int(time.time()), actor_user_id, action, target, json.dumps(detail) if detail else None))


AUTO_ACCOUNT = object()   # create_user's default: the account a new user is assigned is picked for them


def _auto_account(conn: sqlite3.Connection) -> str | None:
    """The account a new user gets when none is given: the only linked one, while it is active. With several linked
    (or the only one needing a login) none: the caller picks, or the admin assigns one."""
    rows = conn.execute("SELECT account, state FROM credentials WHERE backend='oauth'").fetchall()
    return rows[0][0] if len(rows) == 1 and rows[0][1] == "active" else None


def create_user(conn: sqlite3.Connection, name: str, role: str = "user", password_hash: str | None = None,
                account=AUTO_ACCOUNT) -> tuple[int, str]:
    """A new user and their gateway key, shown once. `account`: the subscription account serving them (None:
    unassigned; AUTO_ACCOUNT: _auto_account's pick)."""
    if account is AUTO_ACCOUNT:
        account = _auto_account(conn)
    raw, h, prefix = generate_virtual_key()
    cur = conn.execute(
        "INSERT INTO users(name, role, key_hash, key_prefix, enabled, created_at, password_hash, account) "
        "VALUES(?,?,?,?,1,?,?,?)",
        (name, role, h, prefix, int(time.time()), password_hash, account),
    )
    return cur.lastrowid, raw


def find_user_by_key(conn: sqlite3.Connection, raw_key: str) -> dict | None:
    """The user a gateway key belongs to, with `key_scope`: "full" for their Claude Code key or a machine's key,
    "routes" for their routes-only key, which may only reach third-party routes (app.scope_allows), "pass" for a pass app key
    (old sk-proxy-a- API keys became pass app keys in _migrate)."""
    h = hash_key(raw_key)
    row = conn.execute("SELECT * FROM users WHERE key_hash=? OR routes_key_hash=?", (h, h)).fetchone()
    if row is not None:
        return {**dict(row), "key_scope": "full" if row["key_hash"] == h else "routes"}
    p = conn.execute("SELECT id, user_id, last_used_at FROM pass_keys WHERE key_hash=? AND revoked_at IS NULL", (h,)).fetchone()
    if p is not None:
        row = conn.execute("SELECT * FROM users WHERE id=?", (p["user_id"],)).fetchone()
        now = int(time.time())
        if (p["last_used_at"] or 0) < now - 3600:
            conn.execute("UPDATE pass_keys SET last_used_at=? WHERE id=?", (now, p["id"]))
        # Never admin powers: it is pasted into other apps' settings.
        return {**dict(row), "key_scope": "pass", "role": "user", "pass_key_id": p["id"]}

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


def note_ip(conn: sqlite3.Connection, user_id: int | None, ip: str | None, source: str) -> bool:
    """Count one request or sign-in by `user_id` from `ip` (user_ips). True the first time this user is seen there
    from this source: the moment to look the address's place up (geo.locate)."""
    if user_id is None or not ip or ip == "unknown":
        return False
    now = int(time.time())
    count = conn.execute("INSERT INTO user_ips(user_id, ip, source, first_seen, last_seen) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(user_id, ip, source) DO UPDATE SET last_seen=excluded.last_seen, count=count+1 "
                         "RETURNING count", (user_id, ip[:64], source, now, now)).fetchone()[0]
    return count == 1


def user_ips(conn: sqlite3.Connection, user_id: int, limit: int = 200) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT u.ip, source, first_seen, last_seen, count, city, region, country "
                                          "FROM user_ips u LEFT JOIN ip_geo g ON g.ip=u.ip WHERE user_id=? "
                                          "ORDER BY last_seen DESC LIMIT ?", (user_id, limit))]


def add_machine_key(conn: sqlite3.Connection, user_id: int, label: str) -> str:
    """A new full key for one of the user's machines. Returns the raw key, which is never stored."""
    raw, h, prefix = generate_virtual_key()
    conn.execute("INSERT INTO keys(user_id, key_hash, key_prefix, label, created_at) VALUES(?,?,?,?,?)",
                 (user_id, h, prefix, label, int(time.time())))
    audit(conn, user_id, "machine_key_add", _user_name(conn, user_id), {"label": label, "prefix": prefix})
    return raw


CLIENT_VERSION = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,6}")


def set_client_version(conn: sqlite3.Connection, key_id: int, version: str) -> None:
    """The gclaude version a machine's statusline reported (X-Gclaude-Version); anything not MAJOR.MINOR.N is ignored.
    Written only when it changes: the statusline asks every half minute."""
    if CLIENT_VERSION.fullmatch(version or ""):
        conn.execute("UPDATE keys SET client_version=? WHERE id=? AND client_version IS NOT ?", (version, key_id, version))


def machine_keys(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, key_prefix, label, created_at, last_used_at, client_version FROM keys WHERE user_id=? AND revoked_at IS NULL "
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
    """Remove a revoked user. Their requests stay, anonymized, for gateway-wide cost analysis: no user, session,
    client version, address or content, but the time, model, tokens and cost. Returns how many were anonymized. Tickets stay as sales
    records with user_id NULL (the FK's ON DELETE SET NULL); a user with an active or queued ticket is not deleted."""
    now = time.time() if now is None else now
    u = conn.execute("SELECT name, revoked_at FROM users WHERE id=?", (user_id,)).fetchone()
    if u is None or u["revoked_at"] is None:
        raise ValueError("Only a revoked user can be deleted; revoke them first.")
    if conn.execute(f"SELECT 1 FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL AND {TICKET_EFFECTIVE_END}>? LIMIT 1",
                    (user_id, now)).fetchone():
        raise ValueError("Cancel the user's active or queued pass first.")
    conn.execute("DELETE FROM request_contents WHERE request_id IN (SELECT id FROM requests WHERE user_id=?)", (user_id,))
    n = conn.execute("UPDATE requests SET user_id=NULL, session_id=NULL, client_version=NULL, ip=NULL WHERE user_id=?",
                     (user_id,)).rowcount
    # Their Telegram chat and its messages go too (cascade); the FK alone would only unlink the chat.
    conn.execute("DELETE FROM telegram_chats WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))   # limits, sessions, titles and prompts cascade; tickets keep user_name
    audit(conn, actor, "delete_user", u["name"], {"anonymized_requests": n})
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
                 "ON CONFLICT(user_id, session_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at, from_prompt=0",
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


def cleanup(conn: sqlite3.Connection, retention_days: int, keep_usage: bool = False) -> dict[str, int]:
    """Delete expired sessions and events older than the retention period; with `keep_usage`, only the expired.
    Request contents and prompts go after the retention period even with `keep_usage`: they are what the usage is
    kept without."""
    now = time.time()
    cutoff = float("-inf") if keep_usage else now - retention_days * 86400
    out = {
        "sessions": conn.execute("DELETE FROM sessions WHERE expires_at<?", (int(now),)).rowcount,
        "request_contents": conn.execute("DELETE FROM request_contents WHERE request_id IN (SELECT id FROM requests "
                                         "WHERE started_at<?)", (now - retention_days * 86400,)).rowcount,
        "prompts": conn.execute("DELETE FROM prompts WHERE at<?", (now - retention_days * 86400,)).rowcount,
        "requests": conn.execute("DELETE FROM requests WHERE started_at<?", (cutoff,)).rowcount,
        "quota_snapshots": conn.execute("DELETE FROM quota_snapshots WHERE observed_at<?", (cutoff,)).rowcount,
        # A title taken from a prompt is prompt text: it goes when the prompt does.
        "session_titles": conn.execute("DELETE FROM session_titles WHERE updated_at<? OR (from_prompt=1 AND updated_at<?)",
                                       (cutoff, now - retention_days * 86400)).rowcount,
        "user_ips": conn.execute("DELETE FROM user_ips WHERE last_seen<?", (cutoff,)).rowcount,
        "ip_geo": conn.execute("DELETE FROM ip_geo WHERE ip NOT IN (SELECT ip FROM user_ips)").rowcount,
        "device_requests": conn.execute("DELETE FROM device_requests WHERE expires_at<?", (int(now),)).rowcount,
        "telegram_link_codes": conn.execute("DELETE FROM telegram_link_codes WHERE expires_at<?", (int(now),)).rowcount,
        "telegram_updates": conn.execute("DELETE FROM telegram_updates WHERE at<?", (int(now) - 86400,)).rowcount,
        # A chat that never linked and has been silent 180 days, with its messages (cascade).
        "telegram_chats": conn.execute("DELETE FROM telegram_chats WHERE user_id IS NULL AND last_seen<?",
                                       (int(now) - 180 * 86400,)).rowcount,
    }
    return out
