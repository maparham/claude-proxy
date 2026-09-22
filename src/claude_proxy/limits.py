from __future__ import annotations

import fnmatch
import json
import time
import logging
import sqlite3

from .meter import weighted_from_row
from .config import Config

logger = logging.getLogger("claude_proxy")

WINDOWS = {
    "tokens_5h": 5 * 3600,
    "share_5h": 5 * 3600,
    "tokens_daily": 86400,
    "requests_daily": 86400,
    "tokens_weekly": 7 * 86400,
    "share_7d": 7 * 86400,
}

SHARE_STALE_SEC = 30 * 60  # 30m per spec 181


def _get_limits(conn: sqlite3.Connection, user_id: int) -> dict[str, tuple[str, str]]:
    rows = conn.execute("SELECT kind, value, unit FROM limits WHERE user_id=?", (user_id,)).fetchall()
    out: dict[str, tuple[str, str]] = {}
    for r in rows:
        out[r["kind"]] = (r["value"], r["unit"])
    return out


def _extract_model(body: bytes) -> str | None:
    if not body:
        return None
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
        m = data.get("model")
        if isinstance(m, str):
            return m
    except Exception:
        pass
    return None


def _sum_weighted(conn: sqlite3.Connection, user_id: int, since: int, weights) -> float:
    rows = conn.execute(
        "SELECT input_tokens, output_tokens, cache_creation_tokens, cache_creation_5m, cache_creation_1h, cache_read_tokens, model FROM requests WHERE user_id=? AND started_at>=? AND rejected_by IS NULL",
        (user_id, since),
    ).fetchall()
    total = 0.0
    for r in rows:
        total += weighted_from_row(dict(r), weights)
    return total


def _sum_raw(conn: sqlite3.Connection, user_id: int, since: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens + cache_creation_tokens + cache_creation_5m + cache_creation_1h + cache_read_tokens),0) FROM requests WHERE user_id=? AND started_at>=? AND rejected_by IS NULL",
        (user_id, since),
    ).fetchone()
    return int(row[0] or 0)


def _count_requests(conn: sqlite3.Connection, user_id: int, since: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM requests WHERE user_id=? AND started_at>=? AND rejected_by IS NULL",
        (user_id, since),
    ).fetchone()
    return int(row[0] or 0)


def _oldest_in_window(conn: sqlite3.Connection, user_id: int, since: int) -> int | None:
    row = conn.execute(
        "SELECT MIN(started_at) FROM requests WHERE user_id=? AND started_at>=? AND rejected_by IS NULL",
        (user_id, since),
    ).fetchone()
    return row[0] if row and row[0] else None


def _share_estimate_placeholder(conn: sqlite3.Connection, bucket: str) -> tuple[float | None, int | None, bool]:
    """Return (utilization_pct, resets_at, stale) for bucket if snapshot exists, else (None,None,True).
    Placeholder for full attribution (spec 7.2). Until Phase 4 poll, we return stale."""
    row = conn.execute(
        "SELECT utilization_pct, resets_at, observed_at FROM quota_snapshots WHERE bucket=? ORDER BY observed_at DESC LIMIT 1",
        (bucket,),
    ).fetchone()
    if not row:
        return None, None, True
    util, resets_at, observed_at = row["utilization_pct"], row["resets_at"], row["observed_at"]
    stale = (int(time.time()) - int(observed_at)) > SHARE_STALE_SEC
    return util, resets_at, stale


def check_limits(
    conn: sqlite3.Connection,
    cfg: Config,
    user: dict | sqlite3.Row,
    body: bytes,
    path: str,
) -> tuple[bool, int | None, dict | None, int | None, str | None]:
    """Check all limits pre-request.
    Returns (allowed, status_code, error_body, retry_after, rejected_by_kind)
    If allowed True, other values None.
    """
    user_id = user["id"] if isinstance(user, dict) else user["id"]
    # Also need Row access
    if isinstance(user, sqlite3.Row):
        user_id = user["id"]
    else:
        user_id = user["id"]

    limits = _get_limits(conn, user_id)
    now = int(time.time())

    # enabled check first
    if "enabled" in limits:
        val, unit = limits["enabled"]
        # value "0","false","off" means disabled
        if val.lower() in ("0", "false", "off", "no"):
            return (
                False,
                403,
                {"type": "error", "error": {"type": "permission_error", "message": "Account disabled by admin"}},
                None,
                "enabled",
            )

    # allowed_models check
    if "allowed_models" in limits:
        val, unit = limits["allowed_models"]
        model = _extract_model(body)
        if model is not None:
            # value is comma-separated glob patterns or JSON list
            patterns: list[str] = []
            v = val.strip()
            if v.startswith("["):
                try:
                    arr = json.loads(v)
                    if isinstance(arr, list):
                        patterns = [str(p) for p in arr]
                except:
                    patterns = [p.strip() for p in v.strip("[]").split(",")]
            else:
                patterns = [p.strip() for p in v.split(",") if p.strip()]
            if patterns:
                allowed = any(fnmatch.fnmatch(model, pat) or fnmatch.fnmatch(model.lower(), pat.lower()) for pat in patterns)
                if not allowed:
                    return (
                        False,
                        403,
                        {"type": "error", "error": {"type": "permission_error", "message": f"Model '{model}' not allowed by admin"}},
                        None,
                        "allowed_models",
                    )

    # count_tokens never rejected by token/share limits (spec 184)
    is_count_tokens = "count_tokens" in path

    # Determine if share limits should be skipped due to missing poll capability / staleness
    # Check backend descriptor presence via config check? We infer from quota_snapshots existence.
    # For now, skip share if stale per spec 181 — still enforce token limits and show warning.
    share_skipped_warning = False

    # Token limits
    for kind in ("tokens_5h", "tokens_daily", "tokens_weekly"):
        if kind not in limits or is_count_tokens:
            continue
        val, unit = limits[kind]
        try:
            limit_val = float(val)
        except:
            continue
        window = WINDOWS[kind]
        since = now - window
        # unit weighted vs raw
        use_weighted = unit == "weighted"
        # default weighted per spec 180
        if unit not in ("raw", "weighted"):
            use_weighted = True
        if use_weighted:
            current = _sum_weighted(conn, user_id, since, cfg.weights)
        else:
            current = float(_sum_raw(conn, user_id, since))
        if current >= limit_val:
            oldest = _oldest_in_window(conn, user_id, since)
            retry_after = (oldest + window - now) if oldest else window
            retry_after = max(1, int(retry_after))
            msg = f"Token limit '{kind}' exceeded: {current:.0f}/{limit_val:.0f} (resets in {retry_after}s)"
            logger.info("limit reject user=%s kind=%s current=%s limit=%s", user["name"] if "name" in user.keys() else user_id, kind, current, limit_val)
            return (
                False,
                429,
                {"type": "error", "error": {"type": "rate_limit_error", "message": msg}},
                retry_after,
                kind,
            )

    # Requests daily
    if "requests_daily" in limits and not is_count_tokens:
        val, unit = limits["requests_daily"]
        try:
            limit_val = int(float(val))
        except:
            limit_val = None
        if limit_val is not None:
            window = WINDOWS["requests_daily"]
            since = now - window
            current = _count_requests(conn, user_id, since)
            if current >= limit_val:
                oldest = _oldest_in_window(conn, user_id, since)
                retry_after = (oldest + window - now) if oldest else window
                retry_after = max(1, int(retry_after))
                msg = f"Request limit 'requests_daily' exceeded: {current}/{limit_val} (resets in {retry_after}s)"
                return (
                    False,
                    429,
                    {"type": "error", "error": {"type": "rate_limit_error", "message": msg}},
                    retry_after,
                    "requests_daily",
                )

    # Share limits — skip if no snapshot <30m or backend poll_usage is None (spec 181)
    # We check snapshots; if none or stale, skip enforcement and let caller surface warning.
    for kind, bucket in (("share_5h", "5h"), ("share_7d", "7d")):
        if kind not in limits or is_count_tokens:
            continue
        val, unit = limits[kind]
        try:
            limit_pct = float(val)
        except:
            continue
        util, resets_at, stale = _share_estimate_placeholder(conn, bucket)
        if util is None or stale:
            # Skip share limit, but token limits already enforced; dashboard should show warning
            share_skipped_warning = True
            continue
        # Placeholder attribution: until full 7.2 implemented, we approximate share as 0
        # So no enforcement yet — when attribution ready, compare estimated share to limit_pct
        # For now, do not reject on share if we have util but no per-user estimate
        # This keeps share limits non-blocking until attribution is implemented
        estimated_share = 0.0  # TODO Phase 5 full attribution
        if estimated_share >= limit_pct:
            retry_after = max(1, int((resets_at - now) if resets_at and resets_at > now else 3600))
            msg = f"Share limit '{kind}' exceeded: {estimated_share:.1f}%/{limit_pct:.1f}% of account {bucket} (resets in {retry_after}s)"
            return (
                False,
                429,
                {"type": "error", "error": {"type": "rate_limit_error", "message": msg}},
                retry_after,
                kind,
            )

    return True, None, None, None, None
