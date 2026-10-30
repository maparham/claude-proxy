"""Per-user limits, evaluated before forwarding (spec sections 8 and 17.2).

Usage limits count only forwarded requests (never rejections) in a window that opens with the first request and
then resets all at once, like Claude's own 5-hour limit, optionally restricted to models matching a glob `scope`.
Share limits compare the user's estimated share of an account bucket (quota.attribution) with an allocation in
percentage points of the gateway's capacity: the bucket less the points the gateway can't pin on its users
(2026-10-18 spec), so a share of 100 ends where Anthropic blocks the account.
"""
from __future__ import annotations

import fnmatch
import math
import sqlite3
import time
from dataclasses import dataclass

from . import db, quota, tickets
from .config import Config
from .forwarder import COUNT_TOKENS_PATH
from .usage import SUMS, Totals, price_totals, priced_sql

MINUTE, HOUR, DAY = 60, 3600, 86400

WINDOWS = {
    "requests_minute": MINUTE, "tokens_minute": MINUTE,
    "tokens_5h": 5 * HOUR,
    "requests_daily": DAY, "tokens_daily": DAY, "cost_daily": DAY,
    "tokens_weekly": 7 * DAY,
    "requests_monthly": 30 * DAY, "tokens_monthly": 30 * DAY, "cost_monthly": 30 * DAY,
}
SHARE_BUCKETS = {"share_5h": "5h", "share_7d": "7d"}
TOTALS = ("cost_total",)   # no window: everything the database still holds, e.g. a new account's one-time credit
FREE_CAP = "free_credit_cap"   # rejected_by for signup.free_daily_cap_usd, the ceiling on all credit accounts together
UNITS = {
    **{k: ("count",) for k in WINDOWS if k.startswith("requests_")},
    **{k: ("weighted", "raw") for k in WINDOWS if k.startswith("tokens_")},
    "cost_daily": ("usd",), "cost_monthly": ("usd",), "cost_total": ("usd",),
    "share_5h": ("pct",), "share_7d": ("pct",),
    "allowed_models": ("list",),
}
KINDS = tuple(UNITS)


@dataclass
class Decision:
    status: int
    kind: str
    body: dict
    retry_after: int | None = None


@dataclass
class LimitState:
    kind: str
    scope: str
    value: str
    unit: str
    current: float | None = None
    limit: float | None = None
    remaining: float | None = None
    reset_in: int | None = None
    exceeded: bool = False
    skipped: str | None = None
    estimated: bool = False
    no_live_data: bool = False    # estimated from the user's weighted tokens because account snapshots are stale
    resets_at: float | None = None
    tier: str | None = None       # the ticket's tier label, for messages
    own: float | None = None      # a pass's weekly limit without borrowing (2026-10-30 spec); `limit` adds what it may borrow
    own_pts: float | None = None  # that limit as recorded for the debt it runs up
    rate: float | None = None     # the 7-day rate its use was measured at
    pass_end: float | None = None # a pass's end, so a used-up pass that won't refill before it ends can say so

    @property
    def pct(self) -> float | None:
        if self.current is None or self.limit is None:
            return None
        return 100.0 * self.current / self.limit if self.limit else 100.0   # a share with no capacity left is used up

    def to_dict(self) -> dict:
        return {"kind": self.kind, "scope": self.scope, "value": self.value, "unit": self.unit,
                "current": self.current, "limit": self.limit, "remaining": self.remaining,
                "reset_in": self.reset_in, "exceeded": self.exceeded, "skipped": self.skipped,
                "estimated": self.estimated, "pct": self.pct,
                "no_live_data": self.no_live_data, "resets_at": self.resets_at, "tier": self.tier}


def validate(kind: str, scope: str, value: str, unit: str | None) -> tuple[str, str, str]:
    """Normalise and check an admin-supplied limit. Returns (scope, value, unit)."""
    if kind not in UNITS:
        raise ValueError(f"unknown limit kind {kind!r}; one of {', '.join(KINDS)}")
    unit = unit or UNITS[kind][0]
    if unit not in UNITS[kind]:
        raise ValueError(f"{kind} takes unit {' or '.join(UNITS[kind])}, not {unit!r}")
    scope = (scope or "*").strip() or "*"
    value = str(value).strip()
    if kind == "allowed_models":
        pats = [p.strip() for p in value.split(",") if p.strip()]
        if not pats:
            raise ValueError("allowed_models needs at least one model pattern")
        return "*", ",".join(pats), unit
    try:
        n = float(value)
    except ValueError:
        raise ValueError(f"{kind} needs a number, got {value!r}") from None
    if n < 0:
        raise ValueError(f"{kind} cannot be negative")
    if unit == "pct" and n > 100:
        raise ValueError("a share is at most 100 percentage points")
    return scope, value, unit


def _rows(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT kind, scope, value, unit FROM limits WHERE user_id=? ORDER BY kind, scope", (user_id,)).fetchall()


def _amount(kind: str, unit: str, t: Totals) -> float:
    if kind.startswith("requests_"):
        return float(t.requests)
    if unit == "raw":
        return float(t.raw)
    return t.cost_usd if kind.startswith("cost_") else t.weighted


def _window_start(conn, user_id, row, now, where, params) -> float | None:
    """When the limit's current window opened, or None while none is open. A window opens at the first request
    for a model (in scope) after the previous one ended and lasts its length; then the count starts again from zero."""
    kind, scope = row["kind"], row["scope"]
    window = WINDOWS[kind]
    stored = conn.execute("SELECT started_at FROM limit_windows WHERE user_id=? AND kind=? AND scope=?",
                          (user_id, kind, scope)).fetchone()
    if stored and stored[0] + window > now:
        return stored[0]
    since = max(now - window, stored[0] + window) if stored else now - window
    firsts = conn.execute(f"SELECT requested_model AS rm, model, MIN(started_at) AS first FROM requests WHERE {where} "
                          f"AND started_at>=? GROUP BY rm, model", (*params, since)).fetchall()
    if scope != "*":
        firsts = [g for g in firsts if fnmatch.fnmatchcase(g["rm"] or "", scope) or fnmatch.fnmatchcase(g["model"] or "", scope)]
    if not firsts:
        return None
    start = min(g["first"] for g in firsts)
    conn.execute("INSERT OR REPLACE INTO limit_windows(user_id, kind, scope, started_at) VALUES(?,?,?,?)",
                 (user_id, kind, scope, start))
    return start


def _window_state(conn, cfg, user_id, row, now, inflight: int = 0) -> LimitState:
    """`inflight`: the user's requests being served right now, not yet recorded; they count as requests, but their
    tokens and cost are unknown until they finish."""
    # Aggregated in SQL: this runs before every request, on the event loop that serves every stream.
    kind, scope, unit = row["kind"], row["scope"], row["unit"]
    limit = float(row["value"])
    # The rows a usage limit sees, for opening its window and for filling it alike: forwarded requests for a model.
    # A model-less request (GET /v1/models) costs nothing and is neither. API-key requests are paid from API credit.
    where = ("user_id=? AND rejected_by IS NULL AND path!=? AND COALESCE(requested_model, model) IS NOT NULL "
             "AND api_key_id IS NULL")
    params = [user_id, COUNT_TOKENS_PATH]
    start = _window_start(conn, user_id, row, now, where, params)
    pending = float(inflight) if kind.startswith("requests_") else 0.0
    if start is None:
        return LimitState(kind, scope, row["value"], unit, current=pending, limit=limit, remaining=max(0.0, limit - pending),
                          exceeded=pending >= limit)
    groups = conn.execute(f"SELECT requested_model AS rm, model, {SUMS} FROM requests "
                          f"WHERE {where} AND started_at>=? GROUP BY rm, model", (*params, start)).fetchall()
    if scope != "*":
        # A scope names what clients ask for; providers may answer with a longer model id.
        groups = [g for g in groups if fnmatch.fnmatchcase(g["rm"] or "", scope) or fnmatch.fnmatchcase(g["model"] or "", scope)]
    current = pending + sum(_amount(kind, unit, price_totals(cfg.pricing, g["model"], Totals(g["n"], g["i"], g["o"], g["c5"], g["c1"], g["cr"])))
                            for g in groups)
    return LimitState(kind, scope, row["value"], unit, current=current, limit=limit, remaining=max(0.0, limit - current),
                      reset_in=max(1, math.ceil(start + WINDOWS[kind] - now)), exceeded=current >= limit)


def _total_state(conn, cfg, user_id, row) -> LimitState:
    kind, scope, unit = row["kind"], row["scope"], row["unit"]
    limit = float(row["value"])
    groups = conn.execute(f"SELECT requested_model AS rm, model, {SUMS} FROM requests WHERE user_id=? AND rejected_by IS NULL "
                          f"AND path!=? AND api_key_id IS NULL GROUP BY rm, model", (user_id, COUNT_TOKENS_PATH)).fetchall()
    if scope != "*":
        groups = [g for g in groups if fnmatch.fnmatchcase(g["rm"] or "", scope) or fnmatch.fnmatchcase(g["model"] or "", scope)]
    current = sum(_amount(kind, unit, price_totals(cfg.pricing, g["model"], Totals(g["n"], g["i"], g["o"], g["c5"], g["c1"], g["cr"])))
                  for g in groups)
    return LimitState(kind, scope, row["value"], unit, current=current, limit=limit,
                      remaining=max(0.0, limit - current), exceeded=current >= limit)


def _capacity(att, now) -> float:
    """The points of a bucket available to the gateway (2026-10-18 spec): 100 less what it can't pin on its users,
    until the window resets. Every share limit is a share of this, not of the whole account."""
    if att["unattributed"] is None or (att["resets_at"] and att["resets_at"] <= now):
        return 100.0
    return min(100.0, max(0.0, 100.0 - att["unattributed"]))


def _share_state(conn, cfg, user_id, row, now) -> LimitState:
    att = quota.attribution(conn, cfg.pricing, SHARE_BUCKETS[row["kind"]], now=now, stale_after_s=cfg.quota.stale_after_s)
    limit = float(row["value"]) * _capacity(att, now) / 100
    st = LimitState(row["kind"], row["scope"], row["value"], row["unit"], limit=limit, estimated=True)
    if att["utilization_pct"] is None or att["stale"]:
        st.skipped = f"no account snapshot in the last {cfg.quota.stale_after_s // 60} min"
        return st
    st.current = att["shares"].get(user_id, 0.0)
    st.remaining = max(0.0, limit - st.current)
    st.exceeded = st.current >= limit
    if att["resets_at"]:
        st.reset_in = max(1, math.ceil(att["resets_at"] - now))
    return st


def _date(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def _gated(conn, cfg, user_id) -> bool:
    return cfg.tickets.enabled and tickets.is_gated(conn, user_id)


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


def _finish(st: LimitState) -> LimitState:
    st.remaining = max(0.0, st.limit - st.current)
    st.exceeded = st.current >= st.limit
    return st


def _label(conn, cfg, t) -> str:
    tier = tickets.tier_now(conn, t)
    return cfg.tickets.tiers[tier].label if tier in cfg.tickets.tiers else tier


def _ticket_5h(conn, cfg, t, now) -> LimitState:
    """One pass's 5-hour limit (2026-10-28): its share of Anthropic's 5-hour window against the points its own requests
    moved, as if another user held it, so a pass bought past a limit starts with all of its own."""
    share = (t["share_5h_pct"] if t["share_5h_pct"] is not None else t["share_pct"]) + tickets.bonus_share(conn, t["id"], now)
    att = quota.attribution_cached(conn, cfg.pricing, "5h", now=now, stale_after_s=cfg.quota.stale_after_s)
    st = LimitState("share_5h", "*", f"{share:g}", "pct", limit=share * _capacity(att, now) / 100, estimated=True,
                    tier=_label(conn, cfg, t))
    if att["resets_at"] and att["resets_at"] > now:
        st.resets_at, st.reset_in = att["resets_at"], max(1, math.ceil(att["resets_at"] - now))
    if att["utilization_pct"] is not None and not att["stale"]:
        st.current = att["ticket_shares"].get(t["id"], 0.0)
        return _finish(st)
    rate = quota.observed_rate(conn, cfg.pricing, "5h", now)
    if rate is None:
        st.skipped = NO_RATE
        return st
    # Counting starts at the last known window start, or at the stale snapshot's reset time once that has passed; never
    # more than 5 hours back, since a reset long past may have been followed by more windows than one.
    start = att["window_start"] if att["window_start"] is not None else now - 5 * HOUR
    if att["resets_at"] and att["resets_at"] <= now:
        start = max(att["resets_at"], now - 5 * HOUR)
    st.current, st.no_live_data = _weighted_since(conn, cfg, t["user_id"], start, now, t["id"]) / rate, True
    return _finish(st)


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


def _free_room(conn, cfg, t, week, att, rate, capacity, now) -> float | None:
    """The points of the current week no one is owed (2026-10-30 spec, §1): 100 less the account's use, what the other
    passes and the hand-set weekly shares may still draw, and the margin. None without live data to say so."""
    if cfg.tickets.borrow_margin_pct >= 100 or att["utilization_pct"] is None or att["stale"] or not att["resets_at"] \
            or att["resets_at"] <= now:
        return None
    week_start, week_end = week
    others = [dict(r) for r in conn.execute(
        f"SELECT {tickets.TICKET_COLS} FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL AND t.id!=? "
        f"AND t.starts_at<? AND {db.TICKET_EFFECTIVE_END}>?", (t["account_id"], t["id"], week_end, now))]   # running or queued: an ended one draws nothing more
    owed = 0.0
    if others:
        ids = [p["id"] for p in others]
        uses = _week_use(conn, cfg, ids, week_start, now)
        bonuses: dict[int, list] = {}
        for b in conn.execute(f"SELECT ticket_id, share_pct, starts_at, ends_at FROM ticket_bonuses WHERE ticket_id IN "
                              f"({','.join('?' * len(ids))}) AND cancelled_at IS NULL AND share_pct>0", ids):
            bonuses.setdefault(b["ticket_id"], []).append(b)
        for p in others:
            span = lambda s, e: max(0.0, min(week_end, e) - max(week_start, s)) / DAY / 7   # noqa: E731
            part = p["share_pct"] * span(p["starts_at"], p["effective_end"])
            part += sum(b["share_pct"] * span(b["starts_at"], b["ends_at"]) for b in bonuses.get(p["id"], []))
            own = part * capacity / 100 - _debt(conn, cfg, p, week_start, capacity, now)
            owed += max(0.0, own - uses.get(p["id"], 0.0) / rate)
    for r in conn.execute("SELECT user_id, value FROM limits WHERE kind='share_7d'"):
        if not _gated(conn, cfg, r["user_id"]):
            owed += max(0.0, float(r["value"]) * capacity / 100 - att["shares"].get(r["user_id"], 0.0))
    return max(0.0, 100.0 - att["utilization_pct"] - owed - cfg.tickets.borrow_margin_pct)


def _ticket_7d(conn, cfg, t, now, borrow: bool = False) -> LimitState:
    """One pass's prorated piece of Anthropic's current weekly window (2026-10-13 spec, §1; per pass since 2026-10-28).

    Cap = its share × days of this week it covers ÷ 7 + active bonus pieces (_pass_cap), scaled to the gateway's
    capacity (2026-10-18 spec), less what it borrowed from this week in the last (_debt). Measured from the weighted
    tokens of the requests it served since max(week_start, its start) at the 7d observed rate. Unknown week (no
    resets_at ever) → NO_RATE. With `borrow`, a day pass running past the reset may go over its own limit into its
    next part, as far as the old week's free room goes (2026-10-30 spec, §2)."""
    label = _label(conn, cfg, t)
    week = tickets.week_bounds(conn, cfg, now)
    if week is None:
        st = LimitState("share_7d", "*", "", "pct", limit=None, estimated=True, tier=label)
        st.skipped, st.no_live_data = NO_RATE, False
        return st
    week_start, week_end = week
    piece_start = max(week_start, t["starts_at"])
    cap = _pass_cap(conn, t, piece_start, week_end, now)
    att = quota.attribution_cached(conn, cfg.pricing, "7d", now=now, stale_after_s=cfg.quota.stale_after_s)
    capacity = _capacity(att, now)
    own = cap * capacity / 100 - _debt(conn, cfg, t, week_start, capacity, now)
    st = LimitState("share_7d", "*", f"{cap:g}", "pct", limit=max(0.0, own), estimated=True, tier=label,
                    resets_at=week_end, reset_in=max(1, math.ceil(week_end - now)), own=max(0.0, own), own_pts=own,
                    pass_end=t["effective_end"])
    # Measured from the pass's own tokens at the observed rate, fresh snapshots or not: Anthropic reports utilization in
    # whole-percent steps, and a day-ticket piece (~0.71 points) is smaller than one step, so the share attributed since
    # the piece began would read 0 for most of a week and then jump. The cheap staleness check only decides the "no live data" mark.
    newest = conn.execute("SELECT observed_at FROM quota_snapshots WHERE bucket='7d' ORDER BY observed_at DESC LIMIT 1").fetchone()
    st.no_live_data = newest is None or now - newest[0] > cfg.quota.stale_after_s
    used = _weighted_since(conn, cfg, t["user_id"], piece_start, now, t["id"])
    if not used and not st.no_live_data:
        st.current = 0.0   # live data and nothing used in this piece: no rate needed, so a young account isn't refused
        return _finish(st)
    rate = quota.observed_rate(conn, cfg.pricing, "7d", now)
    if rate is None and not st.no_live_data:
        # A young account: utilization has never risen a whole point, so there is no rate yet. Live snapshots still say
        # who moved it, and until the first rise that is nobody, so a lone first user isn't locked out by their own usage.
        st.current = quota.attributed_since(conn, cfg.pricing, "7d", t["user_id"], piece_start, now, t["id"])
        return _finish(st)
    if rate is None:
        st.skipped, st.no_live_data = NO_RATE, False
        return st
    st.current, st.rate = used / rate, rate
    # Only a day pass borrows: it is one allowance to its holder, however the reset cuts it. A week or month pass keeps
    # each week's share, so one week's use never eats the next's.
    if borrow and t["length"] == "day" and t["effective_end"] > week_end and st.current >= st.own and not st.no_live_data:
        free = _free_room(conn, cfg, t, week, att, rate, capacity, now)
        if free is not None:
            nxt = _pass_cap(conn, t, week_end, week_end + 7 * DAY, now) * capacity / 100
            st.limit = min(st.own + nxt, max(st.own, st.current + free))
    return _finish(st)


def pass_states(conn, cfg: Config, t: dict, now: float) -> list[LimitState]:
    """One pass's two share limits, measured on the requests it served alone."""
    return [_ticket_5h(conn, cfg, t, now), _ticket_7d(conn, cfg, t, now)]


def pass_order(conn, user_id: int, run: list[dict]) -> list[dict]:
    """The running passes in the order they serve: the one the user pinned, then the rest oldest first."""
    pin = conn.execute("SELECT pass_pin FROM users WHERE id=?", (user_id,)).fetchone()
    pin = pin[0] if pin else None
    return sorted(run, key=lambda t: t["id"] != pin)


def serving(conn, cfg: Config, user_id: int, now: float, run: list[dict] | None = None) -> tuple[dict | None, list[LimitState]]:
    """The pass that serves the user's next request and its limits (2026-10-28): the first in pass_order not at a
    limit, so requests spill to the next pass when one is at a limit and come back once it refills. A pass without
    usage data is returned as it is (evaluate answers 503 then). (None, the limits that free up soonest) when every
    pass is at a limit; (None, []) without a pass."""
    run = run if run is not None else tickets.running(conn, user_id, now)
    best, at_limit = None, []
    for t in pass_order(conn, user_id, run):
        sts = pass_states(conn, cfg, t, now)
        if any(st.skipped == NO_RATE for st in sts) or not any(st.exceeded for st in sts):
            return t, sts
        at_limit.append((t, sts))
    # Every pass is at its own limits: a day pass running past the weekly reset may borrow from its next part (2026-10-30
    # spec, §4), the first in pass_order that can. Only then, so no pass borrows while another still has room of its own.
    for t, (s5, s7) in at_limit:
        if not s5.exceeded:
            s7 = _ticket_7d(conn, cfg, t, now, borrow=True)
            if not s7.exceeded:
                return t, [s5, s7]
        wait = max(st.reset_in or 60 for st in (s5, s7) if st.exceeded)
        if best is None or wait < best[0]:
            best = (wait, [s5, s7])
    return None, best[1] if best else []


def ticket_weeks(conn, cfg: Config, user_id: int, now: float, current: LimitState | None = None) -> dict | None:
    """For the statusline: the serving pass's use of each Anthropic week it spans and of the whole pass, e.g.
    {"weeks": [{"pct": 57.0, "state": "current", "share": 0.43}, {"pct": 0.0, "state": "future", "share": 0.57}],
    "ticket_pct": 30.0, "ends_in": 3600, "length": "week"} (seconds until the pass ends; share: the piece's part of the pass). Each week's piece is measured as share_7d measures the current one (`current`,
    that state, is reused for it), at today's rate and capacity. None without a pass, a known week or a rate."""
    t = serving(conn, cfg, user_id, now)[0]
    return pass_weeks(conn, cfg, t, now, current) if t is not None else None


def pass_weeks(conn, cfg: Config, t: dict, now: float, current: LimitState | None = None) -> dict | None:
    """ticket_weeks for one pass, `t`: each Anthropic week it spans and its whole use. `current`: its share_7d state now,
    else measured from its tokens. None without a known week or a rate."""
    week = tickets.week_bounds(conn, cfg, now)
    if week is None:
        return None
    user_id = t["user_id"]
    rate = quota.observed_rate(conn, cfg.pricing, "7d", now)
    capacity = _capacity(quota.attribution_cached(conn, cfg.pricing, "7d", now=now, stale_after_s=cfg.quota.stale_after_s), now)
    t_start, t_end = t["starts_at"], t["effective_end"]
    start = week[0]
    while start > t_start:
        start -= 7 * DAY
    weeks, used_all, limit_all = [], 0.0, 0.0
    while start < t_end:
        end = start + 7 * DAY
        ps, pe = max(start, t_start), min(end, t_end)
        limit = _pass_cap(conn, t, ps, pe, now) * capacity / 100
        state = "past" if end <= now else "future" if start > now else "current"
        if state == "future":
            used = 0.0
        elif state == "current" and current is not None and current.current is not None:
            used = current.current
        elif rate:
            used = _weighted_since(conn, cfg, user_id, ps, min(pe, now), t["id"]) / rate
        else:
            return None
        # What it borrowed from this week in the last (2026-10-30 spec, §3); a future week, what the current one has taken.
        debt = _debt(conn, cfg, t, start, capacity, now) if state != "future" else weeks[-1]["took"] if weeks else 0.0
        own = max(0.0, limit - debt)
        took = max(0.0, used - own) if end < t_end else 0.0
        meter = current.limit if state == "current" and current is not None and current.limit else own + took
        weeks.append({"pct": 100.0 * used / meter if meter else 100.0, "state": state, "limit": limit, "debt": debt, "took": took})
        used_all, limit_all = used_all + used, limit_all + limit
        start = end
    for w in weeks:   # each part as it is spent: its own piece, less what the week before took from it, plus what it took
        w["share"] = max(0.0, w.pop("limit") - w.pop("debt") + w.pop("took")) / limit_all if limit_all else 0.0
    return {"weeks": weeks, "ticket_pct": 100.0 * used_all / limit_all if limit_all else 100.0, "ends_in": max(0, int(t_end - now)),
            "length": t["length"]}


def ticket_states(conn, cfg: Config, user_id: int, now: float, run: list[dict] | None = None) -> list[LimitState]:
    """The two share limits of the pass serving the user now (serving): share_5h against Anthropic's 5-hour window, and
    share_7d, its prorated piece of Anthropic's current weekly window. With every pass at a limit, the ones that free up soonest."""
    return serving(conn, cfg, user_id, now, run)[1]


def used_up(conn, cfg: Config, user_id: int, now: float, run: list[dict] | None = None) -> bool:
    """Whether the user's running passes have nothing left before they end: each one's weekly piece is used up and it
    ends before that refills. The 5-hour limit is not counted: that one refills within hours."""
    run = run if run is not None else tickets.running(conn, user_id, now)
    if not run:
        return False
    for t in run:
        st = _ticket_7d(conn, cfg, t, now)
        if not (st.exceeded and st.resets_at is not None and t["effective_end"] <= st.resets_at):
            return False
    return True


def hand_over(conn, cfg: Config, user_id: int, now: float, run: list[dict] | None = None) -> bool:
    """Used-up tickets give way to the user's queued one at once (tickets.hand_over); True when they did."""
    return tickets.next_queued(conn, user_id, now) is not None and used_up(conn, cfg, user_id, now, run) \
        and tickets.hand_over(conn, cfg, user_id, now)


def _no_ticket_message(conn, cfg, user_id, now) -> str:
    nxt = tickets.next_queued(conn, user_id, now)
    if nxt:
        return f"Your next pass starts on {_date(nxt['starts_at'])}."
    last = tickets.last_ended(conn, user_id, now)
    buy = f" Buy one at {cfg.listener.dashboard_url.rstrip('/')}/dashboard" if cfg.listener.dashboard_url else ""
    if last is None:   # e.g. their only ticket was cancelled before it started
        return f"You have no pass.{buy}"
    return f"Your pass ended on {_date(last['ended_at'])}.{buy}"


def _perm(message: str) -> dict:
    return {"type": "error", "error": {"type": "permission_error", "message": message}}


def states(conn: sqlite3.Connection, cfg: Config, user_id: int, now: float | None = None) -> list[LimitState]:
    now = time.time() if now is None else now
    gated = _gated(conn, cfg, user_id)
    out = []
    for row in _rows(conn, user_id):
        if gated and row["kind"] in SHARE_BUCKETS:
            continue   # the ticket's own share limits stand in for these while it gates the user
        if row["kind"] in WINDOWS:
            out.append(_window_state(conn, cfg, user_id, row, now))
        elif row["kind"] in TOTALS:
            out.append(_total_state(conn, cfg, user_id, row))
        elif row["kind"] in SHARE_BUCKETS:
            out.append(_share_state(conn, cfg, user_id, row, now))
        else:
            out.append(LimitState(row["kind"], row["scope"], row["value"], row["unit"]))
    if gated:
        out.extend(ticket_states(conn, cfg, user_id, now))
    return out


SHARE_LABELS = {"share_5h": "5-hour limit", "share_7d": "weekly limit"}
USER_KINDS = {"share_5h": "5h_limit", "share_7d": "weekly_limit"}   # what a non-admin calls a share limit
TICKET = "ticket"                  # rejected_by: a ticket-gated user with no active ticket, or on a third-party model
TICKET_NO_DATA = "ticket_no_data"  # rejected_by: the 503 while no usage rate was ever observed
NO_RATE = "no usage rate observed yet"
PAUSED = "Passes are paused; ask the admin."   # a ticket-gated user while [tickets] is switched off


def _ends_first(st: LimitState) -> bool:
    """A pass's weekly limit that would refill only after the pass has ended."""
    return st.pass_end is not None and st.resets_at is not None and st.pass_end <= st.resets_at


def _describe(st: LimitState) -> str:
    # Sent to the user, who never learns about the account behind the gateway: a share limit reads as
    # their own allowance, used up.
    scope = f" for models {st.scope}" if st.scope != "*" else ""
    wait = f"; retry in {human(st.reset_in)}" if st.reset_in else ""
    if st.kind in SHARE_LABELS:
        if st.tier is not None and st.kind == "share_7d" and _ends_first(st):   # it ends before it refills: no point in waiting
            return f"Your {st.tier} pass is used up. Buy another pass on your dashboard to continue."
        whose = f"Your {st.tier} pass's" if st.tier is not None else "Your"
        what = {"share_5h": "5-hour allowance", "share_7d": "allowance"}[st.kind] if st.tier is not None else \
            {"share_5h": "5-hour allowance", "share_7d": "weekly allowance"}[st.kind]
        when = f"; you can continue in {human_wait(st.reset_in)}" if st.reset_in else ""
        return f"{whose} {what}{scope} is used up{' for now' if st.kind == 'share_7d' and st.tier is not None else ''}{when}."
    if st.kind in TOTALS:
        return (f"Your gateway credit{scope} is used up (${st.current:.2f} of ${st.limit:.2f}). "
                "Ask the gateway admin for more.")
    if st.unit == "usd":
        amount = f"${st.current:.2f} of ${st.limit:.2f}"
    else:
        amount = f"{st.current:,.0f} of {st.limit:,.0f} {'requests' if st.unit == 'count' else st.unit + ' tokens'}"
    return f"Gateway limit {st.kind}{scope} reached: {amount}{wait}."


def user_view(st: LimitState) -> dict:
    """A limit as a non-admin sees it. Share limits become the user's own allowance (0-100% of it used),
    with nothing about the account they are a share of."""
    d = st.to_dict()
    d.pop("resets_at", None)   # an account-side reset time; reset_in (already relative) is what a non-admin gets,
    if st.kind == "share_7d":   # nor the weekly one's, unless it is what the user waits for: when they are back
        d["used_up"] = st.exceeded and _ends_first(st)
        if not st.exceeded or d["used_up"]:
            d["reset_in"] = None
    if st.kind not in SHARE_LABELS:
        return d
    used = None if st.skipped or st.current is None else st.pct
    return d | {"kind": USER_KINDS[st.kind], "value": "100", "current": used, "limit": 100.0, "remaining": None if used is None else max(0.0, 100.0 - used),
                "pct": used, "estimated": False, "skipped": "not measured right now" if st.skipped else None}


def human(s: int) -> str:
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"


def human_wait(s: int) -> str:
    """A wait as a person says it, for a message: `45 seconds`, `58 min`, `2 h 18 min`, `3 days`, `2 days 5 h`."""
    s = int(s)
    if s < 120:
        return f"{s} seconds"
    if s < 7200:
        return f"{s // 60} min"
    if s < 172800:
        h, m = divmod(s // 60, 60)
        return f"{h} h {m} min" if m else f"{h} h"
    d, h = divmod(s // 3600, 24)
    return f"{d} days {h} h" if h else f"{d} days"


def free_credit_spend(conn: sqlite3.Connection, cfg: Config, now: float) -> tuple[float, int | None]:
    """What all accounts on a one-time credit (a cost_total limit) spent together in the last 24 hours, and, when
    that is at the cap, the seconds until enough of it has left the window to be under it."""
    cap = cfg.signup.free_daily_cap_usd
    where = ("user_id IN (SELECT user_id FROM limits WHERE kind='cost_total') AND started_at>? "
             "AND rejected_by IS NULL AND path!=? AND api_key_id IS NULL")
    params: list = [now - DAY, COUNT_TOKENS_PATH]
    groups = conn.execute(f"SELECT model, {SUMS} FROM requests WHERE {where} GROUP BY model", params).fetchall()
    spent = sum(price_totals(cfg.pricing, g["model"], Totals(g["n"], g["i"], g["o"], g["c5"], g["c1"], g["cr"])).cost_usd
                for g in groups)
    if spent < cap or not groups:
        return spent, None
    amount, args = priced_sql(cfg.pricing, {g["model"] for g in groups}, 1e6)   # one request's cost in USD
    r = conn.execute(f"SELECT started_at FROM (SELECT started_at, SUM({amount}) OVER (ORDER BY started_at, id) AS cum "
                     f"FROM requests WHERE {where}) WHERE ? - cum < ? ORDER BY started_at LIMIT 1",
                     (*args, *params, spent, cap)).fetchone()
    return spent, max(1, math.ceil(r[0] + DAY - now)) if r else None


MAX_INFLIGHT = "max_inflight"   # rejected_by for [limits] max_inflight, the per-user concurrency cap
UNPRICED = "unpriced_model"     # rejected_by for a model with no [pricing] entry: no limit could meter it


def evaluate(conn: sqlite3.Connection, cfg: Config, user_id: int, model: str | None, path: str,
             now: float | None = None, inflight: int = 0, served: dict | None = None) -> Decision | None:
    """None to allow, or the rejection to send. `inflight`: how many of the user's metered requests are being served
    right now; recorded only when they finish, they would otherwise slip past every limit. `served`: given, an allowed
    request's pass goes in as served["ticket_id"], for its row (each pass counts only its own requests)."""
    now = time.time() if now is None else now
    is_count_tokens = path == COUNT_TOKENS_PATH
    third_party = cfg.route_for(model) is not None
    if not is_count_tokens and inflight >= cfg.limits.max_inflight:
        # Bounds how far a token or cost limit can be overshot: at most max_inflight requests are ever unaccounted for.
        return Decision(429, MAX_INFLIGHT, {"type": "error", "error": {"type": "rate_limit_error", "message":
                        f"Gateway limit max_inflight reached: {inflight} of {cfg.limits.max_inflight} requests in "
                        "flight; wait for one to finish."}}, 1)
    if model and not is_count_tokens and cfg.pricing.price_for(model) is None:
        # Every cost, share and weighted limit is measured in price-weighted tokens; a model without a price would be free.
        return Decision(403, UNPRICED, _perm(f"Model {model!r} has no price on this gateway; ask the admin to add one."))
    if not cfg.tickets.enabled and tickets.is_gated(conn, user_id):
        # Their sign-up credit went with the first ticket and they may have no other limit: switching tickets off must
        # not open the account to them. The admin ungates them to hand them back to hand-set limits.
        return Decision(403, TICKET, _perm(PAUSED))
    gated = _gated(conn, cfg, user_id)
    run, t = None, None
    if gated:
        run = tickets.running(conn, user_id, now)
        if not run:   # every request, the model list included: there is no ticket to serve it on
            return Decision(403, TICKET, _perm(_no_ticket_message(conn, cfg, user_id, now)))
    rows = _rows(conn, user_id)
    if gated and third_party and not any(r["kind"].startswith("cost_") and (r["scope"] == "*" or fnmatch.fnmatchcase(model or "", r["scope"]))
                                         for r in rows):
        # Third-party models are real money per request, which a ticket does not cover; a cost limit the admin set for
        # this model governs instead (scoped as the loop below scopes rows).
        return Decision(403, TICKET, _perm("Your pass covers Claude models only."))
    for row in rows:
        kind = row["kind"]
        if kind == "allowed_models":
            if model and not any(fnmatch.fnmatchcase(model, p.strip()) for p in row["value"].split(",")):
                return Decision(403, kind, {"type": "error", "error": {"type": "permission_error",
                                "message": f"Model {model!r} is not allowed for this key by the gateway admin."}})
            continue
        if is_count_tokens:
            continue
        if row["scope"] != "*" and not fnmatch.fnmatchcase(model or "", row["scope"]):
            continue
        if kind in WINDOWS:
            st = _window_state(conn, cfg, user_id, row, now, inflight)
        elif kind in TOTALS:
            st = _total_state(conn, cfg, user_id, row)
        elif kind in SHARE_BUCKETS:
            if third_party or gated:
                continue
            st = _share_state(conn, cfg, user_id, row, now)
        else:
            continue
        if st.exceeded and kind in TOTALS:   # it never frees up, so nothing for a client to wait for and retry
            return Decision(403, kind, {"type": "error", "error": {"type": "permission_error", "message": _describe(st)}})
        if st.exceeded:
            return Decision(429, kind, {"type": "error", "error": {"type": "rate_limit_error", "message": _describe(st)}},
                            st.reset_in or 60)
    if gated and not is_count_tokens and not third_party:
        t, sts = serving(conn, cfg, user_id, now, run)
        if t is None and hand_over(conn, cfg, user_id, now, run):
            t, sts = serving(conn, cfg, user_id, now)
        for st in sts:
            if st.skipped == NO_RATE:
                return Decision(503, TICKET_NO_DATA, {"type": "error", "error": {"type": "api_error", "message":
                                "Your pass's use can't be checked right now. Please try again in a few minutes."}}, 60)
            if st.exceeded:
                return Decision(429, st.kind, {"type": "error", "error": {"type": "rate_limit_error", "message": _describe(st)}},
                                st.reset_in or 60)
    # Everyone still on their sign-up credit shares one daily ceiling, so a crowd of new accounts can't use up the
    # subscription even if each stays within its own credit.
    if cfg.signup.free_daily_cap_usd > 0 and not is_count_tokens and any(r["kind"] in TOTALS for r in rows):
        spent, wait = free_credit_spend(conn, cfg, now)
        if spent >= cfg.signup.free_daily_cap_usd:
            ask = f"Try again in {human(wait)}, or ask" if wait else "Ask"
            return Decision(429, FREE_CAP, {"type": "error", "error": {"type": "rate_limit_error", "message":
                            f"The gateway's free credit is used up for today across all new accounts. {ask} the "
                            "gateway admin to upgrade your account."}}, wait or 3600)
    if t is not None:
        wk = next((st for st in sts if st.kind == "share_7d"), None)
        if wk is not None and wk.rate and wk.limit > wk.own:
            # Borrowing from its next part: what it takes is measured after the reset at today's rate and own limit (§3).
            # One row per week however Anthropic's reported reset time wavers: the one within the tolerance is that week's.
            row = conn.execute("SELECT week_end FROM pass_borrows WHERE ticket_id=? AND ABS(week_end - ?)<=? LIMIT 1",
                               (t["id"], wk.resets_at, quota.RESET_TOLERANCE_S)).fetchone()
            conn.execute("INSERT INTO pass_borrows(ticket_id, week_end, own_pts, rate, updated_at) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(ticket_id, week_end) DO UPDATE SET own_pts=excluded.own_pts, rate=excluded.rate, "
                         "updated_at=excluded.updated_at", (t["id"], row[0] if row else wk.resets_at, wk.own_pts, wk.rate, now))
        if served is not None:
            served["ticket_id"] = t["id"]
    return None
