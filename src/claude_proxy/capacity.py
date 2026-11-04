"""Pass capacity in points of the Anthropic week (2026-11-04 spec).

A pass takes share × days ÷ 7 points of each week it covers (its piece). The gateway sells only what Anthropic reports
as unused this week and no running or queued pass is still owed, less a margin (the room); later weeks, which have no
usage yet, are capped by their promised pieces. The 5-hour bucket keeps a per-moment ceiling (tickets.check_capacity).
The helpers used by per-pass enforcement (_capacity, _pass_cap, _debt, _week_use, _weighted_since) live here so that
limits and the selling rule measure a pass the same way; limits imports them back.
"""
from __future__ import annotations

import math
import sqlite3

from . import db, quota, tickets
from .config import Config
from .forwarder import COUNT_TOKENS_PATH

DAY = 86400
WEEK = 7 * DAY


def _capacity(att, now) -> float:
    """The points of a bucket available to the gateway (2026-10-18 spec): 100 less what it can't pin on its users,
    until the window resets. Every share limit is a share of this, not of the whole account."""
    if att["unattributed"] is None or (att["resets_at"] and att["resets_at"] <= now):
        return 100.0
    return min(100.0, max(0.0, 100.0 - att["unattributed"]))


def _weighted_since(conn, cfg, user_id, since, now, ticket_id: int | None = None) -> float:
    """The user's weighted Anthropic tokens that ended in (since, now], weighed as quota.observed_rate weighs them;
    with `ticket_id`, only those that pass served."""
    span = ("user_id=? AND provider='anthropic' AND rejected_by IS NULL AND path!=? AND api_key_id IS NULL "
            "AND ended_at>? AND ended_at<=?")
    params = (user_id, COUNT_TOKENS_PATH, since, now)
    if ticket_id is not None:
        span, params = span + " AND ticket_id=?", (*params, ticket_id)
    models = [m for (m,) in conn.execute(f"SELECT DISTINCT model FROM requests WHERE {span}", params)]
    w, args = quota.weighted_sql(cfg.pricing, models)
    return float(conn.execute(f"SELECT COALESCE(SUM({w}), 0) FROM requests WHERE {span}", (*args, *params)).fetchone()[0])


def _pass_cap(conn, t, start, end, now) -> float:
    """The weekly points one pass gives between `start` and `end` (both inside one Anthropic week), before capacity
    scaling: its share × the days of the span it covers ÷ 7, plus its active bonus pieces."""
    cap = t["share_pct"] * max(0.0, min(end, t["effective_end"]) - max(start, t["starts_at"])) / DAY / 7
    for b in tickets.active_bonuses(conn, t["id"], now):
        cap += b["share_pct"] * max(0.0, min(end, b["ends_at"]) - max(start, b["starts_at"])) / DAY / 7
    return cap


def _debt(conn, cfg, t, week_start, capacity, now) -> float:
    """What the pass borrowed from the week starting at `week_start` in the week before (2026-10-30 spec, §3), in
    points: measured as it was recorded then (pass_borrows), so the reset's new capacity and rate don't move it. A
    day pass is the only one that borrows, and it has no third part, so nothing ever carries further."""
    if t["starts_at"] >= week_start - quota.RESET_TOLERANCE_S:
        return 0.0
    row = conn.execute("SELECT week_end, own_pts, rate FROM pass_borrows WHERE ticket_id=? AND ABS(week_end - ?)<=? "
                       "ORDER BY ABS(week_end - ?) LIMIT 1", (t["id"], week_start, quota.RESET_TOLERANCE_S, week_start)).fetchone()
    if row is None:
        return 0.0
    used = _weighted_since(conn, cfg, t["user_id"], max(week_start - 7 * DAY, t["starts_at"]), row["week_end"], t["id"]) / row["rate"]
    return max(0.0, used - row["own_pts"])


def _week_use(conn, cfg, ticket_ids, since, now) -> dict[int, float]:
    """Each pass's weighted Anthropic tokens that ended in (since, now], in one query, weighed as _weighted_since weighs them."""
    if not ticket_ids:
        return {}
    span = (f"ticket_id IN ({','.join('?' * len(ticket_ids))}) AND provider='anthropic' AND rejected_by IS NULL AND path!=? "
            "AND api_key_id IS NULL AND ended_at>? AND ended_at<=?")
    params = (*ticket_ids, COUNT_TOKENS_PATH, since, now)
    models = [m for (m,) in conn.execute(f"SELECT DISTINCT model FROM requests WHERE {span}", params)]
    w, args = quota.weighted_sql(cfg.pricing, models)
    return {r[0]: float(r[1]) for r in conn.execute(f"SELECT ticket_id, COALESCE(SUM({w}), 0) FROM requests WHERE {span} "
                                                    "GROUP BY ticket_id", (*args, *params))}


def fresh(att: dict, now: float) -> bool:
    """Anthropic's weekly report is usable for selling: a snapshot younger than stale_after_s (att["stale"] is False)
    whose window has not reset yet (spec §1, Fresh)."""
    return att["utilization_pct"] is not None and not att["stale"] and bool(att["resets_at"]) and att["resets_at"] > now


def pieces(week: tuple[float, float], share: float, starts_at: float, ends_at: float) -> dict[float, float]:
    """The unscaled points `share` over [starts_at, ends_at) takes from each Anthropic week, keyed by week start.
    Weeks project from `week` in 7-day steps, so every caller keys the same week with the same number."""
    ws = week[0]
    start = ws + math.floor((starts_at - ws) / WEEK) * WEEK
    out: dict[float, float] = {}
    while start < ends_at:
        overlap = max(0.0, min(start + WEEK, ends_at) - max(start, starts_at))
        if overlap > 0:
            out[start] = out.get(start, 0.0) + share * overlap / DAY / 7
        start += WEEK
    return out


SOLD_END = "t.ends_at + 86400 * COALESCE((SELECT SUM(b.extra_days) FROM ticket_bonuses b WHERE b.ticket_id=t.id AND b.cancelled_at IS NULL), 0)"


def _sold_rows(conn: sqlite3.Connection, account_id: int, since: float) -> list[tuple[float, float, float]]:
    """(share, starts_at, sold end) of every non-cancelled pass and share bonus of the account still selling time after
    `since`. A pass's sold end is ends_at plus its bonus days: used_up_at never shrinks it (spec §1, Sold period)."""
    rows = [(r[0], r[1], r[2]) for r in conn.execute(
        f"SELECT t.share_pct, t.starts_at, {SOLD_END} FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL "
        f"AND {SOLD_END} > ?", (account_id, since))]
    rows += [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT b.share_pct, b.starts_at, b.ends_at FROM ticket_bonuses b JOIN tickets t ON t.id=b.ticket_id "
        "WHERE t.account_id=? AND t.cancelled_at IS NULL AND b.cancelled_at IS NULL AND b.share_pct>0 AND b.ends_at>?",
        (account_id, since))]
    return rows


def sold_by_week(conn: sqlite3.Connection, account_id: int, week: tuple[float, float]) -> dict[float, float]:
    """Σ unscaled pieces over sold periods per week, from the current week on (spec §2 check 2, §6). No exclude: what
    a replaced pass was sold for in the weeks ahead stays sold there."""
    out: dict[float, float] = {}
    for share, s, e in _sold_rows(conn, account_id, week[0]):
        for w, p in pieces(week, share, max(s, week[0]), e).items():
            out[w] = out.get(w, 0.0) + p
    return out


def _ids(exclude) -> tuple[int, ...]:
    return () if exclude is None else (exclude,) if isinstance(exclude, int) else tuple(exclude)


def owed(conn: sqlite3.Connection, cfg: Config, account_id: int, week: tuple[float, float], att: dict,
         rate: float | None, scale: float, now: float, exclude=()) -> float:
    """What running and queued passes may still draw from the current week (spec §1, Owed): per pass, its piece over
    the week (every overlapping share bonus counted) scaled to the gateway's capacity, less its debt and its use this
    week. Use is weighted tokens at the observed rate, or the pass's attributed points without a rate. Ended passes,
    used up or not, owe nothing."""
    week_start, week_end = week
    ex = _ids(exclude)
    others = [dict(r) for r in conn.execute(
        f"SELECT {tickets.TICKET_COLS} FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL "
        f"AND t.starts_at<? AND {db.TICKET_EFFECTIVE_END}>?", (account_id, week_end, now)) if r["id"] not in ex]
    if not others:
        return 0.0
    ids = [p["id"] for p in others]
    uses = _week_use(conn, cfg, ids, week_start, now) if rate else {}
    bonuses: dict[int, list] = {}
    for b in conn.execute(f"SELECT ticket_id, share_pct, starts_at, ends_at FROM ticket_bonuses WHERE ticket_id IN "
                          f"({','.join('?' * len(ids))}) AND cancelled_at IS NULL AND share_pct>0", ids):
        bonuses.setdefault(b["ticket_id"], []).append(b)
    total = 0.0
    for p in others:
        span = lambda s, e: max(0.0, min(week_end, e) - max(week_start, s)) / DAY / 7   # noqa: E731
        part = p["share_pct"] * span(p["starts_at"], p["effective_end"])
        part += sum(b["share_pct"] * span(b["starts_at"], b["ends_at"]) for b in bonuses.get(p["id"], []))
        own = part * scale / 100 - _debt(conn, cfg, p, week_start, scale, now)
        use = uses.get(p["id"], 0.0) / rate if rate else att["ticket_shares"].get(p["id"], 0.0)
        total += max(0.0, own - use)
    return total


def room(conn: sqlite3.Connection, cfg: Config, account_id: int, week, att: dict, rate: float | None, scale: float,
         now: float, ceiling: float, margin: float, exclude=()) -> float:
    """ceiling − U − Σ owed − margin (spec §1, Free room), not floored: negative means the week is over-committed."""
    return ceiling - att["utilization_pct"] - owed(conn, cfg, account_id, week, att, rate, scale, now, exclude) - margin
