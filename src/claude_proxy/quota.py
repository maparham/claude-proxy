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
import statistics
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


def _window_index(rows: list) -> int:
    """Where the current window starts in `rows`: the index of the first snapshot after the last reset.

    A reset is the reset time moving forward. Headers and the usage endpoint round differently, so a
    small dip with an unchanged reset time is noise; a drop only means a reset when reset times are unknown.
    """
    start = 0
    for i in range(1, len(rows)):
        if _is_reset(rows[i - 1], rows[i]):
            start = i
    return start


def _window(rows: list) -> list:
    """The snapshots of the current window: everything after the last reset."""
    return rows[_window_index(rows):]


def _reset_time(prev, cur) -> float | None:
    """When the reset between two consecutive snapshots happened, if known: the old window's reset time, when it falls
    by the newer snapshot. Usage after it is the new window's rise from 0, owed to the requests that ended after it."""
    r = prev["resets_at"]
    return r if r and r <= cur["observed_at"] else None


def weighted_sql(pricing: Pricing, models) -> tuple[str, list]:
    """SQL for one request row's weighted tokens: its list-price cost in reference-input tokens, or its raw tokens for a
    model without a price. The one rule for every count that is turned into utilization points (attribution, the
    observed rate, a ticket's estimates in limits), so a token weighs the same on both sides of a division."""
    return priced_sql(pricing, models, pricing.reference_input(), unpriced=raw_tokens_sql())


def attribution(conn: sqlite3.Connection, pricing: Pricing, bucket: str, now: float | None = None,
                stale_after_s: int = 1800) -> dict:
    now = time.time() if now is None else now
    rows = conn.execute("SELECT observed_at, utilization_pct, resets_at FROM quota_snapshots WHERE bucket=? "
                        "AND observed_at >= ? ORDER BY observed_at", (bucket, now - 8 * 86400)).fetchall()
    if not rows:
        return {"bucket": bucket, "utilization_pct": None, "resets_at": None, "observed_at": None,
                "stale": True, "shares": {}, "unattributed": None, "window_start": None, "counted_from": None, "history": []}
    start = _window_index(rows)
    win = rows[start:]
    latest = win[-1]
    # The window's first snapshot already holds what was used since the reset; with the reset time known, that rise
    # from 0 goes to the requests that ended between the reset and that snapshot.
    reset_at = _reset_time(rows[start - 1], win[0]) if start else None
    span = "provider='anthropic' AND rejected_by IS NULL AND ended_at > ? AND ended_at <= ?"
    bounds = (win[0]["observed_at"] if reset_at is None else max(reset_at, rows[start - 1]["observed_at"]), latest["observed_at"])
    models = [m for (m,) in conn.execute(f"SELECT DISTINCT model FROM requests WHERE {span}", bounds)]
    # Weighted tokens, priced in SQL (this runs before every request under a share limit); unpriced models count raw tokens.
    w, args = weighted_sql(pricing, models)
    weighted = conn.execute(f"SELECT user_id, ended_at, {w} FROM requests WHERE {span} ORDER BY ended_at",
                            (*args, *bounds)).fetchall()
    shares: dict[int, float] = {}
    history = []
    high = 0.0 if reset_at is not None else win[0]["utilization_pct"]   # only rises above the high-water mark are new usage
    j = 0
    for cur in win:
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
        "counted_from": bounds[0],
        "history": history,
    }


_att_cache: dict[tuple[int, str], tuple[tuple, dict]] = {}   # (id(conn), bucket) -> (inputs, attribution as computed)


def _att_inputs(conn, pricing: Pricing, newest: float | None, att: dict) -> tuple:
    """What attribution() read, cheaply: the newest snapshot, and how many requests ended inside the window it walked
    (an index range count on idx_requests_ended)."""
    n = 0 if att["counted_from"] is None else conn.execute(
        "SELECT COUNT(*) FROM requests WHERE ended_at > ? AND ended_at <= ?", (att["counted_from"], att["observed_at"])).fetchone()[0]
    return newest, n, id(pricing)


def attribution_cached(conn: sqlite3.Connection, pricing: Pricing, bucket: str, now: float, stale_after_s: int = 1800) -> dict:
    """attribution(), recomputed only when what it reads changes; for the hot path (a ticket's 5-hour limit runs it on
    every request). Shares change when a new snapshot lands, and also when a request inside the walked window is
    recorded late (a stream that finished just before another response's headers were recorded) or deleted with its
    user, so the key is the newest snapshot time plus the count of requests that ended inside the window; requests
    ending after the newest snapshot, the usual case, leave it alone. `now` matters only through `stale`, recomputed
    on every call, and through the 8-day lookback, which matters once the window's first snapshot is older than that."""
    newest = conn.execute("SELECT MAX(observed_at) FROM quota_snapshots WHERE bucket=?", (bucket,)).fetchone()[0]
    hit = _att_cache.get((id(conn), bucket))
    if (hit is None or hit[0][0] != newest or (hit[1]["window_start"] is not None and hit[1]["window_start"] < now - 8 * 86400)
            or _att_inputs(conn, pricing, newest, hit[1]) != hit[0]):
        att = attribution(conn, pricing, bucket, now=now, stale_after_s=stale_after_s)
        hit = (_att_inputs(conn, pricing, newest, att), att)
        _att_cache[(id(conn), bucket)] = hit
    att = hit[1]
    return {**att, "stale": att["observed_at"] is None or now - att["observed_at"] > stale_after_s}


def _is_reset(prev, cur) -> bool:
    """The same rule _window uses between two consecutive snapshots."""
    if cur["resets_at"] and prev["resets_at"]:
        return cur["resets_at"] - prev["resets_at"] > RESET_TOLERANCE_S
    return cur["utilization_pct"] < prev["utilization_pct"]


def _pairs(conn, pricing: Pricing, bucket: str, lo: float, now: float):
    """Consecutive snapshot pairs of a bucket from `lo` to `now`, each with the forwarded Anthropic requests that ended
    in between as (id, user_id, model, ended_at, weighted). Yields (prev, cur, requests)."""
    rows = conn.execute("SELECT observed_at, utilization_pct, resets_at FROM quota_snapshots WHERE bucket=? AND observed_at>=? "
                        "AND observed_at<=? ORDER BY observed_at", (bucket, lo, now)).fetchall()
    if len(rows) < 2:
        return
    span = "provider='anthropic' AND rejected_by IS NULL AND ended_at > ? AND ended_at <= ?"
    bounds = (rows[0]["observed_at"], rows[-1]["observed_at"])
    models = [m for (m,) in conn.execute(f"SELECT DISTINCT model FROM requests WHERE {span}", bounds)]
    w, args = weighted_sql(pricing, models)
    reqs = conn.execute(f"SELECT id, user_id, model, ended_at, {w} AS w FROM requests WHERE {span} ORDER BY ended_at, id",
                        (*args, *bounds)).fetchall()
    j = 0
    for prev, cur in zip(rows, rows[1:]):
        batch = []
        while j < len(reqs) and reqs[j]["ended_at"] <= cur["observed_at"]:
            batch.append(reqs[j])
            j += 1
        yield prev, cur, batch


def request_shares(conn: sqlite3.Connection, pricing: Pricing, bucket: str, since: float, now: float) -> list[tuple]:
    """(request_id, user_id, model, ended_at, points): each forwarded Anthropic request that ended after `since`, with
    the percentage points of the bucket attributed to it: its weighted tokens' part of the rise over the snapshot pair
    it ended in. Walking starts at the last snapshot at or before `since`, so an interval straddling `since` is split by
    which requests ended after it. A reset restarts the high-water mark; its pair's rise from 0 goes to the requests that
    ended after the reset time, or to nobody when that time is unknown (attribution() does the same)."""
    first = conn.execute("SELECT observed_at FROM quota_snapshots WHERE bucket=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
                         (bucket, since)).fetchone()
    lo = first[0] if first else since
    out, high = [], None
    for prev, cur, batch in _pairs(conn, pricing, bucket, lo, now):
        if high is None:
            high = prev["utilization_pct"]
        if _is_reset(prev, cur):
            # The new window's rise from 0, owed to the requests that ended after the reset when its time is known.
            reset_at = _reset_time(prev, cur)
            high = cur["utilization_pct"]
            if reset_at is None:
                continue
            delta, batch = cur["utilization_pct"], [r for r in batch if r["ended_at"] > reset_at]
        else:
            delta = cur["utilization_pct"] - high
            high = max(high, cur["utilization_pct"])
        total_w = sum(r["w"] for r in batch)
        if delta <= 0 or total_w <= 0:
            continue
        out.extend((r["id"], r["user_id"], r["model"], r["ended_at"], delta * r["w"] / total_w) for r in batch if r["ended_at"] > since)
    return out


def attributed_since(conn: sqlite3.Connection, pricing: Pricing, bucket: str, user_id: int, since: float, now: float) -> float:
    """The bucket's share attributed to one user's requests since `since` (a ticket day's start, spec section 7)."""
    return sum(p for (_, uid, _, _, p) in request_shares(conn, pricing, bucket, since, now) if uid == user_id)


RATE_CACHE_S = 300   # observed_rate's cache lifetime, measured in the `now` argument so synthetic-clock tests behave
_rate_cache: dict[tuple[int, str], tuple[float, float | None]] = {}   # (id(conn), bucket) -> (computed_at_now, rate)


def clear_rate_cache() -> None:
    """Drop every cached observed_rate() and attribution_cached() value. Call before a test that reuses a connection id
    another test's cache entry might still reference (tests/conftest.py's `db` fixture does this for every test)."""
    _rate_cache.clear()
    _att_cache.clear()


def _median_ratio(conn: sqlite3.Connection, pricing: Pricing, bucket: str, lo: float, now: float) -> float | None:
    """Utilization moves in whole-percent steps, so most pairs show no rise and the tokens in them belong to the next
    step. Tokens are carried across pairs until utilization rises above its high-water mark; each rise yields carried
    tokens ÷ rise and starts the carry again. A reset drops the carry and restarts the high-water mark."""
    ratios, carry, high = [], 0.0, None
    for prev, cur, batch in _pairs(conn, pricing, bucket, lo, now):
        if high is None:
            high = prev["utilization_pct"]
        if _is_reset(prev, cur):
            carry, high = 0.0, cur["utilization_pct"]
            continue
        carry += sum(r["w"] for r in batch)
        rise = cur["utilization_pct"] - high
        if rise > 0:
            if carry > 0:
                ratios.append(carry / rise)
            carry, high = 0.0, cur["utilization_pct"]
    return statistics.median(ratios) if ratios else None


def observed_rate(conn: sqlite3.Connection, pricing: Pricing, bucket: str, now: float, days: int = 7) -> float | None:
    """Weighted tokens per utilization point: the median of weighted ÷ rise over the last `days` of rises above the
    high-water mark, each with the forwarded Anthropic requests since the previous rise (_median_ratio). When the account has been quiet for
    that long and no such pair falls in the window, falls back to the median over the whole retained history instead,
    so a ticket user whose account merely went quiet is never locked out. None only when no qualifying pair exists at
    all, which can only happen on an account that has never served a request. Used to estimate a share when snapshots
    are stale.

    Process-local cache: this runs on the hot path (every request a stale-snapshot ticket user makes), so the result
    is cached per (connection, bucket) for RATE_CACHE_S seconds of `now`-time (not wall-clock time, so tests with a
    synthetic clock get a correctly-expiring cache); clear_rate_cache() drops every entry."""
    key = (id(conn), bucket)
    cached = _rate_cache.get(key)
    if cached is not None and now - cached[0] < RATE_CACHE_S:
        return cached[1]
    rate = _median_ratio(conn, pricing, bucket, now - days * 86400, now)
    if rate is None:
        rate = _median_ratio(conn, pricing, bucket, 0, now)   # the whole retained history, not just the last `days`
    _rate_cache[key] = (now, rate)
    return rate


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
