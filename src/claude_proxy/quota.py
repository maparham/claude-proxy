"""Account quota tracking (spec section 7).

Account utilization comes from Anthropic, never from our own meter:
- `anthropic-ratelimit-unified-<bucket>-{utilization,reset,status}` on every response (fractions 0-1);
- the usage endpoint, polled only when headers have gone quiet (percent 0-100).

Per-user share is an estimate: each rise in utilization between two snapshots is split across users
in proportion to the weighted tokens of their Anthropic-route requests that completed in between.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime

from .config import Pricing, QuotaConfig
from .usage import priced_sql, raw_tokens_sql

logger = logging.getLogger("claude_proxy")

HEADER_RE = re.compile(r"^anthropic-ratelimit-unified-(?P<bucket>[a-z0-9_]+)-(?P<field>utilization|reset|status)$")
POLL_BUCKETS = {"five_hour": "5h", "seven_day": "7d"}
RESET_TOLERANCE_S = 120
DEDUPE_S = 60


@dataclass
class Snapshot:
    bucket: str
    utilization_pct: float
    resets_at: int | None
    status: str | None
    source: str
    raw: dict


def to_epoch(v) -> int | None:
    """Epoch seconds from epoch seconds, epoch milliseconds, or an ISO-8601 string."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d+(\.\d+)?", v)):
        n = float(v)
        return int(n / 1000) if n > 1e11 else int(n)
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def parse_headers(headers) -> list[Snapshot]:
    fields: dict[str, dict] = {}
    raw = {}
    for k, v in headers.items():
        k = k.lower()
        if k.startswith("anthropic-ratelimit-unified-"):
            raw[k] = v
        m = HEADER_RE.match(k)
        if m:
            fields.setdefault(m["bucket"], {})[m["field"]] = v
    out = []
    for bucket, f in fields.items():
        if "utilization" not in f:
            continue
        try:
            util = float(f["utilization"]) * 100.0   # headers carry fractions
        except ValueError:
            continue
        out.append(Snapshot(bucket, util, to_epoch(f.get("reset")), f.get("status"), "header", raw))
    return out


def _poll_bucket_name(key: str) -> str:
    if key in POLL_BUCKETS:
        return POLL_BUCKETS[key]
    if key.startswith("seven_day_"):
        return "7d_" + key[len("seven_day_"):]
    if key.startswith("five_hour_"):
        return "5h_" + key[len("five_hour_"):]
    return key


def parse_poll(body: dict) -> list[Snapshot]:
    out = []
    for key, val in (body or {}).items():
        # Only the rolling-window buckets; the endpoint also returns unrelated code-named entries.
        if not isinstance(val, dict) or not key.startswith(("five_hour", "seven_day")):
            continue
        util = val.get("utilization", val.get("used_percentage"))
        if util is None:
            continue
        try:
            util = float(util)   # the usage endpoint reports percent
        except (TypeError, ValueError):
            continue
        resets = to_epoch(val.get("resets_at", val.get("reset_at", val.get("reset"))))
        out.append(Snapshot(_poll_bucket_name(key), util, resets, val.get("status"), "poll", {key: val}))
    return out


def record(conn: sqlite3.Connection, snaps: list[Snapshot], now: float | None = None) -> None:
    now = time.time() if now is None else now
    for s in snaps:
        last = conn.execute("SELECT observed_at, utilization_pct, resets_at FROM quota_snapshots WHERE bucket=? "
                            "ORDER BY observed_at DESC LIMIT 1", (s.bucket,)).fetchone()
        if last and now - last["observed_at"] < DEDUPE_S and last["utilization_pct"] == s.utilization_pct \
                and last["resets_at"] == s.resets_at:
            continue
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at, status, raw_json) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (now, s.source, s.bucket, s.utilization_pct, s.resets_at, s.status, json.dumps(s.raw)[:4000]))


def classify_429(headers) -> str:
    h = {k.lower(): v for k, v in headers.items()}
    if any(k.startswith("anthropic-ratelimit-unified-") and k.endswith("status") and v == "rejected" for k, v in h.items()):
        return "upstream_quota"
    if any(k.startswith("anthropic-ratelimit-") for k in h):
        return "upstream_throttle"
    return "upstream_request_scoped"


def _window(rows: list) -> list:
    """The snapshots of the current window: everything after the last reset.

    A reset is the reset time moving forward. Headers and the usage endpoint round differently, so a
    small dip with an unchanged reset time is noise; a drop only means a reset when reset times are unknown.
    """
    start = 0
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        if cur["resets_at"] and prev["resets_at"]:
            if cur["resets_at"] - prev["resets_at"] > RESET_TOLERANCE_S:
                start = i
        elif cur["utilization_pct"] < prev["utilization_pct"]:
            start = i
    return rows[start:]


def attribution(conn: sqlite3.Connection, pricing: Pricing, bucket: str, now: float | None = None,
                stale_after_s: int = 1800) -> dict:
    now = time.time() if now is None else now
    rows = conn.execute("SELECT observed_at, utilization_pct, resets_at FROM quota_snapshots WHERE bucket=? "
                        "AND observed_at >= ? ORDER BY observed_at", (bucket, now - 8 * 86400)).fetchall()
    if not rows:
        return {"bucket": bucket, "utilization_pct": None, "resets_at": None, "observed_at": None,
                "stale": True, "shares": {}, "unattributed": None, "window_start": None, "history": []}
    win = _window(rows)
    latest = win[-1]
    span = "provider='anthropic' AND rejected_by IS NULL AND ended_at > ? AND ended_at <= ?"
    bounds = (win[0]["observed_at"], latest["observed_at"])
    models = [m for (m,) in conn.execute(f"SELECT DISTINCT model FROM requests WHERE {span}", bounds)]
    # Weighted tokens, priced in SQL (this runs before every request under a share limit); unpriced models count raw tokens.
    w, args = priced_sql(pricing, models, pricing.reference_input(), unpriced=raw_tokens_sql())
    weighted = conn.execute(f"SELECT user_id, ended_at, {w} FROM requests WHERE {span} ORDER BY ended_at",
                            (*args, *bounds)).fetchall()
    shares: dict[int, float] = {}
    history = [{"t": win[0]["observed_at"], "utilization_pct": win[0]["utilization_pct"], "shares": {}}]
    high = win[0]["utilization_pct"]   # only rises above the high-water mark are new usage
    j = 0
    for cur in win[1:]:
        by_user: dict[int, float] = {}
        while j < len(weighted) and weighted[j][1] <= cur["observed_at"]:
            uid, _, w = weighted[j]
            by_user[uid] = by_user.get(uid, 0.0) + w
            j += 1
        delta = cur["utilization_pct"] - high
        high = max(high, cur["utilization_pct"])
        total_w = sum(by_user.values())
        if delta > 0 and total_w > 0:
            for uid, w in by_user.items():
                shares[uid] = shares.get(uid, 0.0) + delta * w / total_w
        history.append({"t": cur["observed_at"], "utilization_pct": cur["utilization_pct"], "shares": dict(shares)})
    return {
        "bucket": bucket,
        "utilization_pct": latest["utilization_pct"],
        "resets_at": latest["resets_at"],
        "observed_at": latest["observed_at"],
        "stale": now - latest["observed_at"] > stale_after_s,
        "shares": shares,
        "unattributed": max(0.0, latest["utilization_pct"] - sum(shares.values())),
        "window_start": win[0]["observed_at"],
        "history": history,
    }


def buckets(conn: sqlite3.Connection, since: float) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT bucket FROM quota_snapshots WHERE observed_at>=? ORDER BY bucket", (since,))]


class Poller:
    """Polls the usage endpoint only when no snapshot has arrived recently (spec 7.1)."""

    def __init__(self, conn: sqlite3.Connection, backend, qc: QuotaConfig):
        self.conn = conn
        self.backend = backend
        self.qc = qc
        self.next_allowed = 0.0
        self.backoff = 0.0
        self.last_status: int | None = None

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        newest = self.conn.execute("SELECT MAX(observed_at) FROM quota_snapshots").fetchone()[0]
        if newest is not None and now - newest < self.qc.poll_idle_s:
            return
        if now < self.next_allowed:
            return
        try:
            status, body = await self.backend.poll_usage()
        except Exception:   # e.g. RefreshUnavailable: the request path reports it
            status, body = 0, None
        self.last_status = status
        if status == 429:
            self.backoff = min(self.qc.poll_max_backoff_s, max(self.backoff * 2, self.qc.poll_min_interval_s * 2))
            self.next_allowed = now + self.backoff
            logger.info("usage poll rate-limited; next attempt in %ds", self.backoff)
            return
        self.backoff = 0.0
        self.next_allowed = now + self.qc.poll_min_interval_s
        if status == 200 and body:
            record(self.conn, parse_poll(body), now=now)

    async def run(self, interval_s: float = 60.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("usage poll failed")
            await asyncio.sleep(interval_s)
