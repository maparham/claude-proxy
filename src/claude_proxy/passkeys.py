"""Pass app keys (2026-11-18 spec): keys a pass holder hands to any Anthropic-format app (Claude Desktop, say).
Their requests are served and counted by the holder's running pass. This module stores and lists the keys and holds the
gate (evaluate) that decides whether a request on one is served."""
from __future__ import annotations

import datetime
import sqlite3
import time
import zoneinfo

from . import db, limits, tickets
from .forwarder import COUNT_TOKENS_PATH

MAX_KEYS = 20
NO_PASS = "pass_key_no_pass"          # rejected_by: no running pass to serve the key
REJECT_INFLIGHT = "pass_key_inflight"
REJECT_RPM = "pass_key_rpm"
REJECT_ROUTE = "pass_key_route"
PASS_KEY_MAX_INFLIGHT = 4             # the apps' share of the user's in-flight cap
PASS_KEY_RPM = 60


class PassKeyError(ValueError):
    pass


def create_key(conn, user_id: int, label: str, actor: int | None = None) -> tuple[int, str]:
    label = (label or "").strip()
    if not label or len(label) > 64:
        raise PassKeyError("Name the key in 1-64 characters, such as the app it is for.")
    n = conn.execute("SELECT COUNT(*) FROM pass_keys WHERE user_id=? AND revoked_at IS NULL", (user_id,)).fetchone()[0]
    if n >= MAX_KEYS:
        raise PassKeyError(f"You have {MAX_KEYS} app keys already; revoke one first.")
    raw, h, prefix = db.generate_virtual_key(db.PASS_KEY_PREFIX)
    kid = conn.execute("INSERT INTO pass_keys(user_id, key_hash, key_prefix, label, created_at) VALUES(?,?,?,?,?)",
                       (user_id, h, prefix, label, int(time.time()))).lastrowid
    db.audit(conn, actor if actor is not None else user_id, "pass_key_add", db._user_name(conn, user_id), {"label": label, "prefix": prefix})
    return kid, raw


def keys(conn, user_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, key_prefix, label, created_at, last_used_at FROM pass_keys WHERE user_id=? AND revoked_at IS NULL "
                        "ORDER BY created_at DESC, id DESC", (user_id,))
    return [dict(r) for r in rows]


def key_owner(conn, key_id: int) -> int | None:
    """The user a live pass key belongs to, or None."""
    r = conn.execute("SELECT user_id FROM pass_keys WHERE id=? AND revoked_at IS NULL", (key_id,)).fetchone()
    return r["user_id"] if r else None


def revoke_key(conn, user_id: int, key_id: int, actor: int | None) -> bool:
    n = conn.execute("UPDATE pass_keys SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                     (int(time.time()), key_id, user_id)).rowcount
    if n:
        db.audit(conn, actor, "pass_key_revoke", db._user_name(conn, user_id), {"key_id": key_id})
    return bool(n)


def usage(conn, user_id: int, since: float, tz_offset_s: int = 0, tz: str | None = None) -> list[dict]:
    """Requests and tokens per local day and key, bucketed like api_credit.usage: a zone's own clock when `tz` is known,
    else the fixed `tz_offset_s`."""
    try:
        zone = zoneinfo.ZoneInfo(tz) if tz else None
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        zone = None
    cols = "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens"
    if zone is not None:
        out: dict[tuple[int, int], dict] = {}
        for r in conn.execute(f"SELECT started_at, pass_key_id, {cols} FROM requests WHERE user_id=? AND pass_key_id IS NOT NULL "
                              "AND started_at>=?", (user_id, since)):
            local = datetime.datetime.fromtimestamp(r["started_at"], zone)
            day = int(datetime.datetime(local.year, local.month, local.day, tzinfo=zone).timestamp())
            u = out.setdefault((day, r["pass_key_id"]), {"day": day, "pass_key_id": r["pass_key_id"], "requests": 0,
                                                         "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0})
            u["requests"] += 1
            u["input_tokens"] += r["input_tokens"] or 0
            u["output_tokens"] += r["output_tokens"] or 0
            u["cache_write_tokens"] += r["cache_creation_tokens"] or 0
            u["cache_read_tokens"] += r["cache_read_tokens"] or 0
        return sorted(out.values(), key=lambda u: (-u["day"], u["pass_key_id"]))
    off = int(tz_offset_s)
    day = f"(CAST((started_at + {off}) / 86400 AS INTEGER) * 86400 - {off})"
    rows = conn.execute(f"SELECT {day} AS day, pass_key_id, COUNT(*) AS requests, COALESCE(SUM(input_tokens),0) AS input_tokens, "
                        "COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(cache_creation_tokens),0) AS cache_write_tokens, "
                        "COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens FROM requests "
                        "WHERE user_id=? AND pass_key_id IS NOT NULL AND started_at>=? "
                        "GROUP BY day, pass_key_id ORDER BY day DESC, pass_key_id", (user_id, since))
    return [dict(r) for r in rows]


def _err(status: int, kind: str, etype: str, msg: str, retry_after: int | None = None) -> limits.Decision:
    return limits.Decision(status, kind, {"type": "error", "error": {"type": etype, "message": msg}}, retry_after)


def evaluate(conn, cfg, user_id: int, model: str | None, path: str, inflight: int = 0, pass_inflight: int = 0,
             pass_metered_inflight: int = 0, now: float | None = None, served: dict | None = None) -> limits.Decision | None:
    """None to serve a pass-key request, or the rejection to send. Only a running pass serves it (never hand-set limits,
    never a free trial waiting to start: that starts with Claude Code's first request). `inflight`: the user's requests in
    flight, Claude Code and apps together; `pass_inflight`: all the apps' requests in flight (for the rpm); `pass_metered_inflight`: the metered ones (the apps' cap). The pass's own limits then apply as to Claude
    Code, so the apps can never get around them (they share the same use)."""
    now = time.time() if now is None else now
    if not (cfg.tickets.enabled and tickets.is_gated(conn, user_id) and tickets.running(conn, user_id, now)):
        return _err(403, NO_PASS, "permission_error", "This key works while you have a running pass.")
    if model is not None and cfg.route_for(model) is not None:
        # limits.evaluate lets a gated user's routed model through on a hand-set cost limit: that would be hand-set limits.
        return _err(403, REJECT_ROUTE, "permission_error", "Pass app keys work with Claude models only.")
    if path != COUNT_TOKENS_PATH and model is not None and pass_metered_inflight >= PASS_KEY_MAX_INFLIGHT:
        return _err(429, REJECT_INFLIGHT, "rate_limit_error",
                    f"At most {PASS_KEY_MAX_INFLIGHT} requests from app keys may run at once; wait for one to finish.", 1)
    recent = conn.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND pass_key_id IS NOT NULL AND rejected_by IS NULL "
                          "AND started_at>=?", (user_id, now - 60)).fetchone()[0]
    if recent + pass_inflight >= PASS_KEY_RPM:
        return _err(429, REJECT_RPM, "rate_limit_error", f"App keys may make {PASS_KEY_RPM} requests a minute; try again shortly.", 60)
    return limits.evaluate(conn, cfg, user_id, model, path, now=now, inflight=inflight, served=served, autostart=False)
