"""Lower-bound usage hints for /pricing (paid-tickets design, section 9).

Once a day: over the last 30 days, find each user's busy clock hours (UTC), attribute each hour's share of the 5-hour
and weekly buckets to the model family of its requests, and keep the 75th percentile of share per busy hour per
family. A tier's hint is its share divided by that: "at least N hours of steady use", a figure a heavy user will reach.
"""
from __future__ import annotations

import fnmatch
import math
import sqlite3
import statistics
import time

from . import quota
from .config import Config, Pricing
from .forwarder import COUNT_TOKENS_PATH

DAY, HOUR = 86400, 3600
FAMILIES = {"sonnet": "claude-sonnet-*", "opus": "claude-opus-*"}
BUCKETS = ("5h", "7d")
MIN_BUSY_HOURS = 50      # below this a family's hint is hidden
BUSY_SPREAD_S = 20 * 60  # a busy hour: first to last request at least this far apart ...
BUSY_REQUESTS = 10       # ... or at least this many requests
WINDOW_DAYS = 30
REFRESH_AFTER_S = 23 * HOUR


def family(model: str | None) -> str | None:
    m = (model or "").lower()
    return next((f for f, pat in FAMILIES.items() if fnmatch.fnmatchcase(m, pat)), None)


def busy_hours(conn: sqlite3.Connection, since: float, now: float) -> set[tuple[int, int]]:
    """(user_id, hour_start) for every UTC clock hour with 20 minutes between a user's first and last forwarded
    Anthropic request, or at least 10 of them. Single short bursts are left out: they would make an hour look cheap."""
    rows = conn.execute(
        f"SELECT user_id, CAST(started_at / {HOUR} AS INTEGER) * {HOUR} AS h, COUNT(*) AS n, MAX(started_at) - MIN(started_at) AS spread "
        f"FROM requests WHERE provider='anthropic' AND rejected_by IS NULL AND path!=? AND model IS NOT NULL AND user_id IS NOT NULL "
        f"AND started_at>=? AND started_at<? GROUP BY user_id, h HAVING spread>=? OR n>=?",
        (COUNT_TOKENS_PATH, since, now, BUSY_SPREAD_S, BUSY_REQUESTS)).fetchall()
    return {(r["user_id"], int(r["h"])) for r in rows}


def _p75(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=4, method="inclusive")[2]


def refresh(conn: sqlite3.Connection, pricing: Pricing, now: float | None = None) -> dict[str, int]:
    """Recompute usage_estimates from the last 30 days. Returns busy hours per family and bucket."""
    now = int(time.time() if now is None else now)
    since = now - WINDOW_DAYS * DAY
    hours = busy_hours(conn, since, now)
    meta = {r["id"]: (r["user_id"], int(r["started_at"] // HOUR) * HOUR, family(r["model"])) for r in conn.execute(
        "SELECT id, user_id, started_at, model FROM requests WHERE provider='anthropic' AND rejected_by IS NULL AND path!=? "
        "AND model IS NOT NULL AND started_at>=?", (COUNT_TOKENS_PATH, since))}
    out = {}
    for bucket in BUCKETS:
        per: dict[tuple, float] = {}   # (user_id, hour, family) -> points of this bucket
        for rid, _uid, _model, _ended, points in quota.request_shares(conn, pricing, bucket, since, now):
            m = meta.get(rid)
            if m is None or m[2] is None or (m[0], m[1]) not in hours:
                continue
            per[m] = per.get(m, 0.0) + points
        for fam in FAMILIES:
            values = [v for (_, _, f), v in per.items() if f == fam]
            conn.execute("INSERT OR REPLACE INTO usage_estimates(computed_at, family, bucket, busy_hours, p75_share_per_hour) VALUES(?,?,?,?,?)",
                         (now, fam, bucket, len(values), _p75(values)))
            out[f"{fam}/{bucket}"] = len(values)
    return out


def refresh_if_due(conn: sqlite3.Connection, cfg: Config, now: float | None = None) -> dict[str, int] | None:
    """Called by the maintenance loop every few hours; recomputes at most once a day, and only when tickets are on."""
    if not cfg.tickets.enabled:
        return None
    now = time.time() if now is None else now
    last = conn.execute("SELECT MAX(computed_at) FROM usage_estimates").fetchone()[0]
    if last is not None and now - last < REFRESH_AFTER_S:
        return None
    return refresh(conn, cfg.pricing, now)


def _hours(share: float, row) -> float | None:
    if row is None or row["busy_hours"] < MIN_BUSY_HOURS or not row["p75_share_per_hour"]:
        return None
    return math.floor(share / row["p75_share_per_hour"] * 10) / 10   # one decimal, rounded down: "at least"


def hours_hint(conn: sqlite3.Connection, share_pct: float) -> dict:
    """Per family: hours of steady use a tier share buys per 5-hour window and per ticket day (share ÷ 7 against the
    weekly bucket), or None while the family has under 50 busy hours of data."""
    rows = {(r["family"], r["bucket"]): r for r in conn.execute("SELECT * FROM usage_estimates")}
    return {fam: {"per_5h": _hours(share_pct, rows.get((fam, "5h"))), "per_day": _hours(share_pct / 7, rows.get((fam, "7d")))}
            for fam in FAMILIES}
