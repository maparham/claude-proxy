"""Pass capacity in points of the Anthropic week (2026-11-04 spec).

A pass takes share × days ÷ 7 points of each week it covers (its piece). The gateway sells only what Anthropic reports
as unused this week and no running or queued pass has reserved, less a margin (the room); later weeks, which have no
usage yet, are capped by their promised pieces. The 5-hour bucket keeps a per-moment ceiling (tickets.check_capacity).
The helpers used by per-pass enforcement (_capacity, _pass_cap, _debt, _week_use, _weighted_since) live here so that
limits and the selling rule measure a pass the same way; limits imports them back.
"""
from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass

from . import db, quota, tickets, usage
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


# Requests carry the pass that served them since the 2026-10-28 migration (c909c73, deployed 16:37 UTC), which tagged
# the 40 days of requests before it too: a pass that started earlier has requests with no pass.
TAGGED_SINCE = 1793205445 - 40 * DAY


def pass_tokens(conn, cfg, tickets_) -> dict[int, dict | None]:
    """The tokens each pass served over its whole life, weighted and raw, for the dashboard's pass cards and the
    admin's Tickets table. Every request the pass served counts, whatever its provider (its limits count Anthropic's
    only). None for a pass whose history may be incomplete, as a partial count would mislead: one that started before
    TAGGED_SINCE (its requests carry no pass) or before the oldest request kept (retention deleted the rest)."""
    tickets_ = list(tickets_)
    if not tickets_:
        return {}
    kept = conn.execute("SELECT MIN(started_at) FROM requests").fetchone()[0]
    since = max(TAGGED_SINCE, kept if kept is not None else 0)
    ids = tuple(t["id"] for t in tickets_)
    span = f"ticket_id IN ({','.join('?' * len(ids))}) AND rejected_by IS NULL AND path!=?"
    params = (*ids, COUNT_TOKENS_PATH)
    models = [m for (m,) in conn.execute(f"SELECT DISTINCT model FROM requests WHERE {span}", params)]
    w, args = quota.weighted_sql(cfg.pricing, models)
    sums = {r[0]: {"weighted": float(r[1]), "raw": int(r[2])} for r in conn.execute(
        f"SELECT ticket_id, COALESCE(SUM({w}), 0), COALESCE(SUM({usage.raw_tokens_sql()}), 0) FROM requests "
        f"WHERE {span} GROUP BY ticket_id", (*args, *params))}
    return {t["id"]: sums.get(t["id"], {"weighted": 0.0, "raw": 0}) if t["starts_at"] >= since else None
            for t in tickets_}


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


def reserved(conn: sqlite3.Connection, cfg: Config, account_id: int, week: tuple[float, float], att: dict,
         rate: float | None, scale: float, now: float, exclude=()) -> float:
    """What running and queued passes may still draw from the current week (spec §1): per pass, its piece over
    the week (every overlapping share bonus counted) scaled to the gateway's capacity, less its debt and its use this
    week. Use is weighted tokens at the observed rate, or the pass's attributed points without a rate. Ended passes,
    used up or not, reserve nothing."""
    week_start, week_end = week
    ex = _ids(exclude)
    others = [dict(r) for r in conn.execute(
        f"SELECT {tickets.TICKET_COLS} FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL "
        f"AND t.starts_at<? AND {db.TICKET_EFFECTIVE_END}>?", (account_id, week_end, now)) if r["id"] not in ex]
    if not others:
        return 0.0
    ids = [p["id"] for p in others]
    uses = _week_use(conn, cfg, ids, week_start, now) if rate else {}
    bonuses = _bonuses(conn, ids)
    total = 0.0
    for p in others:
        own = _own(conn, cfg, p, bonuses.get(p["id"], []), week, scale, now)
        use = uses.get(p["id"], 0.0) / rate if rate else att["ticket_shares"].get(p["id"], 0.0)
        total += max(0.0, own - use)
    return total


def _bonuses(conn, ids) -> dict[int, list]:
    out: dict[int, list] = {}
    for b in conn.execute(f"SELECT ticket_id, share_pct, starts_at, ends_at FROM ticket_bonuses WHERE ticket_id IN "
                          f"({','.join('?' * len(ids))}) AND cancelled_at IS NULL AND share_pct>0", ids):
        out.setdefault(b["ticket_id"], []).append(b)
    return out


def _own(conn, cfg, p, bonuses, week, scale, now) -> float:
    """A pass's points of the current week: its piece over the week (overlapping share bonuses counted) scaled to the
    gateway's capacity, less what it borrowed from this week in the last."""
    span = lambda s, e: max(0.0, min(week[1], e) - max(week[0], s)) / DAY / 7   # noqa: E731
    part = p["share_pct"] * span(p["starts_at"], p["effective_end"])
    part += sum(b["share_pct"] * span(b["starts_at"], b["ends_at"]) for b in bonuses)
    return part * scale / 100 - _debt(conn, cfg, p, week[0], scale, now)


def week_use(conn: sqlite3.Connection, cfg: Config, account_id: int, now: float) -> dict:
    """The admin's view of this week's points (passes tab): who used them, as Anthropic's figure splits by attribution
    (passes, each user's use off a pass, use outside the gateway), and per pass overlapping the week its points of it,
    what it used and what it may still draw (reserved()'s terms). Empty without a known week."""
    week = tickets.week_bounds(conn, cfg, now)
    if week is None:
        return {"by": None, "passes": {}}
    att = _att(conn, cfg, now)
    rate = quota.observed_rate(conn, cfg.pricing, "7d", now)
    scale = _capacity(att, now)
    rows = [dict(r) for r in conn.execute(
        f"SELECT {tickets.TICKET_COLS} FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL "
        f"AND t.starts_at<? AND {db.TICKET_EFFECTIVE_END}>?", (account_id, week[1], week[0]))]
    ids = [p["id"] for p in rows]
    bonuses = _bonuses(conn, ids) if ids else {}
    uses = _week_use(conn, cfg, ids, week[0], now) if rate and ids else {}
    ts = att["ticket_shares"]
    passes = {}
    for p in rows:
        own = _own(conn, cfg, p, bonuses.get(p["id"], []), week, scale, now)
        use = uses.get(p["id"], 0.0) / rate if rate else ts.get(p["id"], 0.0)
        passes[p["id"]] = {"sold": own, "used": ts.get(p["id"], 0.0),
                           "reserved": max(0.0, own - use) if p["effective_end"] > now else 0.0}
    if att["utilization_pct"] is None:
        return {"by": None, "passes": passes}
    on_pass: dict[int, float] = {}
    for tid, pts in ts.items():
        uid = next((p["user_id"] for p in rows if p["id"] == tid), None)
        if uid is None:
            row = conn.execute("SELECT user_id FROM tickets WHERE id=?", (tid,)).fetchone()
            uid = row[0] if row else None
        on_pass[uid] = on_pass.get(uid, 0.0) + pts
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM users")}
    off = [{"name": names.get(uid, "?"), "pts": v - on_pass.get(uid, 0.0)} for uid, v in att["shares"].items()]
    return {"by": {"passes": sum(ts.values()), "outside": att["unattributed"] or 0.0,
                   "users": sorted([u for u in off if u["pts"] >= 0.05], key=lambda u: -u["pts"])},
            "passes": passes}


def room(conn: sqlite3.Connection, cfg: Config, account_id: int, week, att: dict, rate: float | None, scale: float,
         now: float, ceiling: float, margin: float, exclude=()) -> float:
    """ceiling − U − Σ reserved − margin (spec §1, Free room), not floored: negative means the week is over-committed."""
    return ceiling - att["utilization_pct"] - reserved(conn, cfg, account_id, week, att, rate, scale, now, exclude) - margin


NO_REPORT = "No fresh usage report from Anthropic; try again in a few minutes."


def _day(t: float) -> str:
    return time.strftime("%d.%m", time.gmtime(t))


@dataclass
class Context:
    """One look at the account for judging hypothetical passes (preview, sold-out flags, price lists)."""
    week: tuple[float, float] | None
    fresh: bool
    scale: float
    used: float | None
    reserved: float
    room: float | None          # max_sold_pct − U − reserved − sell_margin_pct, not floored; None without a fresh report
    sold: dict[float, float]    # sold_by_week, no exclude
    exclude: tuple


def _att(conn, cfg, now):
    return quota.attribution_cached(conn, cfg.pricing, "7d", now=now, stale_after_s=cfg.quota.stale_after_s)


def context(conn: sqlite3.Connection, cfg: Config, account_id: int, now: float, exclude=()) -> Context:
    week = tickets.week_bounds(conn, cfg, now)
    att = _att(conn, cfg, now)
    ok = week is not None and fresh(att, now)
    scale = _capacity(att, now)
    if week is None:
        return Context(None, False, scale, att["utilization_pct"], 0.0, None, {}, _ids(exclude))
    rate = quota.observed_rate(conn, cfg.pricing, "7d", now)
    res = reserved(conn, cfg, account_id, week, att, rate, scale, now, exclude)
    room_ = cfg.tickets.max_sold_pct - att["utilization_pct"] - res - cfg.tickets.sell_margin_pct if ok else None
    return Context(week, ok, scale, att["utilization_pct"], res, room_, sold_by_week(conn, account_id, week), _ids(exclude))


def _week_full(w: float, total: float, ceiling: float) -> str:
    return f"The week of {_day(w)} would hold {total:.1f}% with this pass, over max_sold_pct ({ceiling:g}%)."


def _no_room(free: float, need: float) -> str:
    return f"This week has {max(0.0, free):.1f} points free and the pass needs {need:.1f}."


def reserved_5h(conn: sqlite3.Connection, account_id: int, att: dict, scale: float, now: float, exclude=()) -> float:
    """Σ over running passes (but `exclude`) of the 5-hour allowance each has reserved in Anthropic's current window (may still draw):
    its 5-hour share with bonuses, scaled as _ticket_5h scales it, less the points it moved there."""
    ex = _ids(exclude)
    total = 0.0
    for r in conn.execute(f"SELECT t.id, COALESCE(t.share_5h_pct, t.share_pct) FROM tickets t WHERE t.account_id=? "
                          f"AND t.cancelled_at IS NULL AND t.starts_at<=? AND {db.TICKET_EFFECTIVE_END}>?", (account_id, now, now)):
        if r[0] in ex:
            continue
        limit = (r[1] + tickets.bonus_share(conn, r[0], now)) * scale / 100
        total += max(0.0, limit - att["ticket_shares"].get(r[0], 0.0))
    return total


def check_5h_room(conn: sqlite3.Connection, cfg: Config, account_id: int, share_5h: float, starts_at: float, now: float,
                  exclude=()) -> None:
    """A pass (or added 5-hour share) starting inside Anthropic's current 5-hour window must find its whole 5-hour
    allowance free there: max_sold_5h_pct − U − Σ reserved_5h − sell_margin_pct ≥ share_5h (2026-11-07), else a buyer
    bought just after the window filled would wait for the reset. The need is the share as sold, not scaled by
    _capacity: use the gateway can't pin on a user shrinks every pass's limit, so a window filled that way must refuse
    too. Skipped without a fresh 5-hour report (the weekly check already refuses a stale one), or for a start at or
    after the window's reset."""
    if share_5h <= 1e-12:
        return
    att = quota.attribution_cached(conn, cfg.pricing, "5h", now=now, stale_after_s=cfg.quota.stale_after_s)
    if not (att["utilization_pct"] is not None and not att["stale"] and att["resets_at"] and now < att["resets_at"]) \
            or starts_at >= att["resets_at"]:
        return
    need = share_5h
    free = (cfg.tickets.max_sold_5h_pct - att["utilization_pct"]
            - reserved_5h(conn, account_id, att, _capacity(att, now), now, exclude) - cfg.tickets.sell_margin_pct)
    if need > free + 1e-9:
        raise tickets.FiveHourFull(f"The current 5-hour window has {max(0.0, free):.1f} points free and the pass needs "
                                    f"{need:.1f}; it resets at {time.strftime('%H:%M', time.gmtime(att['resets_at']))} UTC.", att["resets_at"])


def check(conn: sqlite3.Connection, cfg: Config, account_id: int, share: float, share_5h: float, starts_at: float,
          ends_at: float, now: float, exclude=(), ctx: Context | None = None) -> None:
    """The three checks of spec §2 for a pass (or bonus share) not written yet; CapacityError names the first that fails."""
    ctx = ctx or context(conn, cfg, account_id, now, exclude)
    if not ctx.fresh:
        raise tickets.CapacityError(NO_REPORT)
    ps = pieces(ctx.week, share, starts_at, ends_at)
    ws = ctx.week[0]
    need = ps.get(ws, 0.0) * ctx.scale / 100
    if need > 1e-12 and need > ctx.room + 1e-9:
        raise tickets.CapacityError(_no_room(ctx.room, need))
    for w, p in sorted(ps.items()):
        if w > ws and ctx.sold.get(w, 0.0) + p > cfg.tickets.max_sold_pct + 1e-9:
            raise tickets.CapacityError(_week_full(w, ctx.sold.get(w, 0.0) + p, cfg.tickets.max_sold_pct))
    tickets.check_capacity(conn, cfg, account_id, share_5h, int(starts_at), int(ends_at), list(ctx.exclude) or None)
    check_5h_room(conn, cfg, account_id, share_5h, starts_at, now, ctx.exclude)


class Guard:
    """Spec §2 for a change written row by row (a bonus with share and days, a queue move): take it before the change,
    call verify() after every row is written, inside the caller's transaction or savepoint. The change is judged by what
    it adds: more reserved this week must fit the room that was there, a later week it raises must stay under max_sold_pct,
    and the 5-hour sum over its period must stay under max_sold_5h_pct. A change that adds nothing to reserved and raises
    no later week passes the first two without needing a fresh report."""

    def __init__(self, conn: sqlite3.Connection, cfg: Config, account_id: int, now: float):
        self.conn, self.cfg, self.account_id, self.now = conn, cfg, account_id, now
        self.week = tickets.week_bounds(conn, cfg, now)
        self.reserved0, self.sold0 = self._state() if self.week else (0.0, {})

    def _state(self) -> tuple[float, dict[float, float]]:
        att = _att(self.conn, self.cfg, self.now)
        rate = quota.observed_rate(self.conn, self.cfg.pricing, "7d", self.now)
        return (reserved(self.conn, self.cfg, self.account_id, self.week, att, rate, _capacity(att, self.now), self.now),
                sold_by_week(self.conn, self.account_id, self.week))

    def verify(self, starts_at: float, ends_at: float) -> None:
        if self.week is not None:
            reserved1, sold1 = self._state()
            ws, ceiling = self.week[0], self.cfg.tickets.max_sold_pct
            added = reserved1 - self.reserved0
            later = [w for w in sorted(sold1) if w > ws and sold1[w] > self.sold0.get(w, 0.0) + 1e-9]
            if added > 1e-9 or later:
                att = _att(self.conn, self.cfg, self.now)
                if not fresh(att, self.now):
                    raise tickets.CapacityError(NO_REPORT)
                free = ceiling - att["utilization_pct"] - self.reserved0 - self.cfg.tickets.sell_margin_pct
                if added > 1e-9 and added > free + 1e-9:
                    raise tickets.CapacityError(_no_room(free, added))
                for w in later:
                    if sold1[w] > ceiling + 1e-9:
                        raise tickets.CapacityError(_week_full(w, sold1[w], ceiling))
        elif ends_at > starts_at:
            raise tickets.CapacityError(NO_REPORT)
        if ends_at > starts_at:
            tickets.verify_capacity(self.conn, self.cfg, self.account_id, int(starts_at), int(ends_at))


def _returned(conn, cfg, account_id: int, week, att: dict, rate: float | None, now: float) -> float:
    """Unused points of passes that ended this week: their sold piece of the week, their non-cancelled share bonuses'
    pieces of it included (sold_by_week counts those too), less their use."""
    ws, we = week
    rows = [dict(r) for r in conn.execute(
        f"SELECT {tickets.TICKET_COLS}, {SOLD_END} AS sold_end FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL "
        f"AND t.starts_at<? AND {db.TICKET_EFFECTIVE_END}<=? AND {db.TICKET_EFFECTIVE_END}>?", (account_id, we, now, ws))]
    ids = [p["id"] for p in rows]
    bonuses: dict[int, list] = {}
    if ids:
        for b in conn.execute(f"SELECT ticket_id, share_pct, starts_at, ends_at FROM ticket_bonuses WHERE ticket_id IN "
                              f"({','.join('?' * len(ids))}) AND cancelled_at IS NULL AND share_pct>0", ids):
            bonuses.setdefault(b["ticket_id"], []).append(b)
    uses = _week_use(conn, cfg, ids, ws, now) if rate and rows else {}
    total = 0.0
    for p in rows:
        piece = pieces(week, p["share_pct"], max(ws, p["starts_at"]), p["sold_end"]).get(ws, 0.0)
        for b in bonuses.get(p["id"], []):
            piece += pieces(week, b["share_pct"], max(ws, b["starts_at"]), b["ends_at"]).get(ws, 0.0)
        use = uses.get(p["id"], 0.0) / rate if rate else att["ticket_shares"].get(p["id"], 0.0)
        total += max(0.0, piece - use)
    return total


def summary(conn: sqlite3.Connection, cfg: Config, account_id: int, now: float) -> dict:
    """The capacity panel (spec §6)."""
    from . import limits   # here, not at the top: limits imports capacity
    ctx = context(conn, cfg, account_id, now)
    att = _att(conn, cfg, now)
    rate = quota.observed_rate(conn, cfg.pricing, "7d", now) if ctx.week else None
    ws = ctx.week[0] if ctx.week else None
    later = {w: v for w, v in ctx.sold.items() if ws is not None and ws < w < now + 30 * DAY}
    peak = max(later.items(), key=lambda kv: kv[1]) if later else None
    points = {now, *tickets._starts_within(conn, account_id, now, now + 30 * DAY)}
    hand = sum(float(r["value"]) for r in conn.execute("SELECT user_id, value FROM limits WHERE kind='share_7d'")
               if not limits._gated(conn, cfg, r["user_id"]))
    return {
        "week": {"start": ws, "end": ctx.week[1] if ctx.week else None, "fresh": ctx.fresh, "used": ctx.used,
                 "reserved": ctx.reserved, "margin": cfg.tickets.sell_margin_pct,
                 "free": max(0.0, ctx.room) if ctx.room is not None else 0.0,
                 "sold": ctx.sold.get(ws, 0.0) if ws is not None else 0.0,
                 "returned": _returned(conn, cfg, account_id, ctx.week, att, rate, now) if ctx.week else 0.0,
                 "max_sold_pct": cfg.tickets.max_sold_pct},
        "peak_week": {"start": peak[0], "sold": peak[1]} if peak else None,
        "five_hour": {"now": tickets.sold_at(conn, account_id, now),
                      "peak_30d": max(tickets.sold_at(conn, account_id, p) for p in points),
                      "max_sold_5h_pct": cfg.tickets.max_sold_5h_pct},
        "hand_set_7d_pct": hand,
        "used_by": week_use(conn, cfg, account_id, now)["by"],
    }
