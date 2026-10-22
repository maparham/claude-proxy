"""API keys on prepaid credit (2026-10-21 spec): Claude Code with ANTHROPIC_API_KEY, billed per token.

Rates are USD per million tokens per model glob (the longest matching glob wins). The ledger is append-only: grants
as the admin gave them, charges negative, one per forwarded request that reported tokens. A balance is its sum.
"""
from __future__ import annotations

import math
import sqlite3
import time

from . import db
from .config import Config, Price
from .forwarder import COUNT_TOKENS_PATH
from .limits import Decision

MAX_KEYS = 20
MAX_GRANT_USD = 1_000_000
REJECT_INFLIGHT, REJECT_UNPRICED, REJECT_CREDIT, REJECT_RPM = "api_inflight", "api_unpriced", "api_credit", "api_rpm"
REJECT_EXTRA = "api_extra"
LOW_CREDIT = "Your credit balance is too low to access the API. Ask the gateway admin to add credit."
RATE_FIELDS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")


class CreditError(ValueError):
    pass


def _err(kind: str, message: str) -> dict:
    return {"type": "error", "error": {"type": kind, "message": message}}


def _price(v, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 10_000:
        raise CreditError(f"{what} must be a number from 0 to 10000 (USD per million tokens).")
    return float(v)


# ---------- rates ----------

def _tidy(v: float) -> float:
    return round(v, 6)   # derived cache prices: 3 * 0.1 is 0.30000000000000004, which the rate editor would show

def rates(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT pattern, input, output, cache_write_5m, cache_write_1h, cache_read, updated_at FROM api_rates")
    return sorted((dict(r) for r in rows), key=lambda r: (-len(r["pattern"]), r["pattern"]))


def rate_for(conn: sqlite3.Connection, model: str | None) -> dict | None:
    if not model:
        return None
    # SQLite's GLOB is case-sensitive like fnmatchcase; lower() matches config.Pricing.price_for.
    row = conn.execute("SELECT * FROM api_rates WHERE lower(?) GLOB lower(pattern) ORDER BY length(pattern) DESC, pattern "
                       "LIMIT 1", (model,)).fetchone()
    return dict(row) if row else None


def set_rate(conn, pattern: str, input, output, cache_write_5m=None, cache_write_1h=None, cache_read=None,
             actor: int | None = None, now=None) -> dict:
    pattern = (pattern or "").strip()
    if not pattern or len(pattern) > 128 or any(c.isspace() for c in pattern):
        raise CreditError("A model pattern needs 1-128 characters and no spaces, such as claude-sonnet-5*.")
    inp, out = _price(input, "The input price"), _price(output, "The output price")
    p = Price(inp, out, *(None if v is None else _price(v, "A cache price") for v in (cache_write_5m, cache_write_1h, cache_read)))
    row = {"pattern": pattern, **{k: _tidy(getattr(p, k)) for k in RATE_FIELDS}}
    conn.execute("INSERT INTO api_rates(pattern, input, output, cache_write_5m, cache_write_1h, cache_read, updated_at, updated_by) "
                 "VALUES(:pattern,:input,:output,:cache_write_5m,:cache_write_1h,:cache_read,:at,:by) ON CONFLICT(pattern) DO UPDATE SET "
                 "input=excluded.input, output=excluded.output, cache_write_5m=excluded.cache_write_5m, "
                 "cache_write_1h=excluded.cache_write_1h, cache_read=excluded.cache_read, updated_at=excluded.updated_at, "
                 "updated_by=excluded.updated_by", {**row, "at": int(time.time() if now is None else now), "by": actor})
    db.audit(conn, actor, "api_rate_set", pattern, {k: row[k] for k in RATE_FIELDS})
    return row


def delete_rate(conn, pattern: str, actor: int | None) -> bool:
    n = conn.execute("DELETE FROM api_rates WHERE pattern=?", (pattern,)).rowcount
    if n:
        db.audit(conn, actor, "api_rate_delete", pattern)
    return bool(n)


def seed_rates(conn, cfg: Config, now=None) -> int:
    """Fill an empty rate table from the config's Claude list prices. Never refills: an admin's deletions stay."""
    if conn.execute("SELECT 1 FROM api_rates LIMIT 1").fetchone():
        return 0
    at = int(time.time() if now is None else now)
    n = 0
    for pattern, p in cfg.pricing.prices.items():
        if pattern.startswith("claude-"):
            conn.execute("INSERT INTO api_rates(pattern, input, output, cache_write_5m, cache_write_1h, cache_read, updated_at) "
                         "VALUES(?,?,?,?,?,?,?)", (pattern, *(_tidy(getattr(p, k)) for k in RATE_FIELDS), at))
            n += 1
    return n


def cost_usd(rate: dict, input_tokens: int, output_tokens: int, cache_write_5m: int, cache_write_1h: int, cache_read: int) -> float:
    return (input_tokens * rate["input"] + output_tokens * rate["output"] + cache_write_5m * rate["cache_write_5m"]
            + cache_write_1h * rate["cache_write_1h"] + cache_read * rate["cache_read"]) / 1e6


# ---------- ledger ----------

def balance(conn, user_id: int) -> float:
    return conn.execute("SELECT COALESCE(SUM(usd), 0) FROM api_ledger WHERE user_id=?", (user_id,)).fetchone()[0]


def grant(conn, user_id: int, usd, note: str, actor: int | None, now=None) -> float:
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not math.isfinite(usd) or usd == 0 or abs(usd) > MAX_GRANT_USD:
        raise CreditError(f"The amount must be a non-zero number of USD up to {MAX_GRANT_USD:,}.")
    note = (note or "").strip()
    if not note or len(note) > 200:
        raise CreditError("Give a note of 1-200 characters, such as how it was paid.")
    conn.execute("INSERT INTO api_ledger(at, user_id, kind, usd, actor_user_id, note) VALUES(?,?,'grant',?,?,?)",
                 (time.time() if now is None else now, user_id, float(usd), actor, note))
    db.audit(conn, actor, "api_credit_grant", db._user_name(conn, user_id), {"usd": usd, "note": note})
    return balance(conn, user_id)


def charge(conn, user_id: int, api_key_id: int, request_id: int, model: str | None, row: dict, now=None) -> float:
    """Charge one forwarded request from its `requests` row fields. Split as usage.CW5/CW1 split cache writes."""
    rate = rate_for(conn, model)
    if rate is None:
        return 0.0
    g = lambda k: int(row.get(k) or 0)  # noqa: E731
    split = g("cache_creation_5m") + g("cache_creation_1h") > 0
    cw5 = g("cache_creation_5m") if split else g("cache_creation_tokens")
    cw1 = g("cache_creation_1h") if split else 0
    usd = cost_usd(rate, g("input_tokens"), g("output_tokens"), cw5, cw1, g("cache_read_tokens"))
    if usd <= 0:
        return 0.0
    conn.execute("INSERT INTO api_ledger(at, user_id, kind, usd, api_key_id, request_id, model, input_tokens, output_tokens, "
                 "cache_write_tokens, cache_read_tokens) VALUES(?,?,'charge',?,?,?,?,?,?,?,?)",
                 (time.time() if now is None else now, user_id, -usd, api_key_id, request_id, model,
                  g("input_tokens"), g("output_tokens"), cw5 + cw1, g("cache_read_tokens")))
    return usd


def grants(conn, user_id: int, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT at, usd, note FROM api_ledger WHERE user_id=? AND kind='grant' ORDER BY at DESC, id DESC LIMIT ?",
                        (user_id, limit))
    return [dict(r) for r in rows]


def usage(conn, user_id: int, since: float, tz_offset_s: int = 0) -> list[dict]:
    off = int(tz_offset_s)
    day = f"(CAST((at + {off}) / 86400 AS INTEGER) * 86400 - {off})"
    rows = conn.execute(f"SELECT {day} AS day, api_key_id, COUNT(*) AS requests, SUM(input_tokens) AS input, "
                        "SUM(output_tokens) AS output, SUM(cache_write_tokens) AS cache_write, SUM(cache_read_tokens) AS cache_read, "
                        "-SUM(usd) AS usd FROM api_ledger WHERE user_id=? AND kind='charge' AND at>=? "
                        "GROUP BY day, api_key_id ORDER BY day DESC, api_key_id", (user_id, since))
    return [dict(r) for r in rows]


# ---------- keys ----------

def create_key(conn, user_id: int, label: str, actor: int | None = None) -> tuple[int, str]:
    label = (label or "").strip()
    if not label or len(label) > 64:
        raise CreditError("Name the key in 1-64 characters, such as the computer it is for.")
    n = conn.execute("SELECT COUNT(*) FROM api_keys WHERE user_id=? AND revoked_at IS NULL", (user_id,)).fetchone()[0]
    if n >= MAX_KEYS:
        raise CreditError(f"You have {MAX_KEYS} API keys already; revoke one first.")
    raw, h, prefix = db.generate_virtual_key(db.API_KEY_PREFIX)
    kid = conn.execute("INSERT INTO api_keys(user_id, key_hash, key_prefix, label, created_at) VALUES(?,?,?,?,?)",
                       (user_id, h, prefix, label, int(time.time()))).lastrowid
    db.audit(conn, actor if actor is not None else user_id, "api_key_add", db._user_name(conn, user_id), {"label": label, "prefix": prefix})
    return kid, raw


def keys(conn, user_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, key_prefix, label, created_at, last_used_at FROM api_keys WHERE user_id=? AND revoked_at IS NULL "
                        "ORDER BY created_at DESC, id DESC", (user_id,))
    return [dict(r) for r in rows]


def revoke_key(conn, user_id: int, key_id: int, actor: int | None) -> bool:
    n = conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                     (int(time.time()), key_id, user_id)).rowcount
    if n:
        db.audit(conn, actor, "api_key_revoke", db._user_name(conn, user_id), {"key_id": key_id})
    return bool(n)


# ---------- per-account limits ----------

def account_limits(conn, cfg: Config, user_id: int) -> dict:
    r = conn.execute("SELECT max_inflight, rpm FROM api_account_limits WHERE user_id=?", (user_id,)).fetchone()
    mi = r["max_inflight"] if r and r["max_inflight"] is not None else None
    rpm = r["rpm"] if r and r["rpm"] is not None else None
    return {"max_inflight": mi or cfg.api.max_inflight, "rpm": rpm or cfg.api.rpm, "custom": mi is not None or rpm is not None}


def set_account_limits(conn, user_id: int, max_inflight: int | None, rpm: int | None, actor: int | None, now=None) -> None:
    """None for either: back to the [api] default. Both None removes the row."""
    for v, what in ((max_inflight, "Concurrent requests"), (rpm, "Requests per minute")):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 10_000):
            raise CreditError(f"{what} must be a whole number from 1 to 10000, or empty for the default.")
    if max_inflight is None and rpm is None:
        conn.execute("DELETE FROM api_account_limits WHERE user_id=?", (user_id,))
    else:
        conn.execute("INSERT INTO api_account_limits(user_id, max_inflight, rpm, updated_at, updated_by) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(user_id) DO UPDATE SET max_inflight=excluded.max_inflight, rpm=excluded.rpm, "
                     "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                     (user_id, max_inflight, rpm, int(time.time() if now is None else now), actor))
    db.audit(conn, actor, "api_limits_set", db._user_name(conn, user_id), {"max_inflight": max_inflight, "rpm": rpm})


# ---------- the gate ----------

def evaluate(conn, cfg: Config, user_id: int, model: str | None, path: str, inflight: int = 0, now=None) -> Decision | None:
    """None to forward, or the refusal. Replaces limits.evaluate for API keys: tickets, shares and limit rows don't apply."""
    now = time.time() if now is None else now
    lim = account_limits(conn, cfg, user_id)
    if model is not None:   # GET /v1/models has nothing to price or charge, but it still gets the rpm check below
        is_count = path == COUNT_TOKENS_PATH
        if not is_count and inflight >= lim["max_inflight"]:
            return Decision(429, REJECT_INFLIGHT, _err("rate_limit_error", f"{inflight} of {lim['max_inflight']} requests are "
                            "already in flight for this account; wait for one to finish."), 1)
        if rate_for(conn, model) is None:
            return Decision(404, REJECT_UNPRICED, _err("not_found_error", f"model: {model} is not available on this API."))
        if not is_count and balance(conn, user_id) <= 0:
            return Decision(400, REJECT_CREDIT, _err("invalid_request_error", LOW_CREDIT))
    # Token counts and the model list are free but still reach the subscription, so they count here, the ones in flight
    # included (app.handle holds a slot for every API request): a key with no credit can't flood it.
    recent = conn.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND api_key_id IS NOT NULL AND rejected_by IS NULL "
                          "AND started_at>=?", (user_id, now - 60)).fetchone()[0]
    if recent + inflight >= lim["rpm"]:
        return Decision(429, REJECT_RPM, _err("rate_limit_error", f"This account may make {lim['rpm']} requests a minute; "
                        "try again shortly."), 60)
    return None


def refuse_extras(data: dict | None, beta_header: str | None) -> Decision | None:
    """Features Anthropic prices above plain tokens, which the per-token rates can't charge: refused on API keys."""
    tools = data.get("tools") if isinstance(data, dict) else None
    betas = [b.strip() for b in (beta_header or "").split(",")]
    msg = None
    if isinstance(tools, list) and any(isinstance(t, dict) and str(t.get("type", "")).startswith("web_search") for t in tools):
        msg = "Web search is not available with API keys; turn off the WebSearch tool."
    elif (isinstance(data, dict) and data.get("speed") not in (None, "standard")) or any(b.startswith("fast-mode") for b in betas):
        msg = "Fast mode is not available with API keys; turn it off with /fast."
    elif any(b.startswith("context-1m") for b in betas):
        msg = "The 1M-token context is not available with API keys; pick a model without [1m]."
    return Decision(400, REJECT_EXTRA, _err("invalid_request_error", msg)) if msg else None


# ---------- admin view ----------

def accounts(conn, cfg: Config, now=None) -> list[dict]:
    """Every user with a ledger row or a live key."""
    since = (time.time() if now is None else now) - 30 * 86400
    rows = conn.execute(
        "SELECT u.id AS user_id, u.name, COALESCE((SELECT SUM(usd) FROM api_ledger l WHERE l.user_id=u.id), 0) AS balance, "
        "COALESCE((SELECT -SUM(usd) FROM api_ledger l WHERE l.user_id=u.id AND kind='charge' AND at>=?), 0) AS spent_30d, "
        "(SELECT COUNT(*) FROM api_keys k WHERE k.user_id=u.id AND k.revoked_at IS NULL) AS keys FROM users u "
        "WHERE EXISTS(SELECT 1 FROM api_ledger l WHERE l.user_id=u.id) OR EXISTS(SELECT 1 FROM api_keys k WHERE k.user_id=u.id "
        "AND k.revoked_at IS NULL) ORDER BY u.name", (since,))
    return [{**dict(r), **account_limits(conn, cfg, r["user_id"])} for r in rows]
