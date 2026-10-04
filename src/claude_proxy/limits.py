"""Per-user limits, evaluated before forwarding (spec sections 8 and 17.2).

Usage limits count only forwarded requests (never rejections) in a window that opens with the first request and
then resets all at once, like Claude's own 5-hour limit, optionally restricted to models matching a glob `scope`.
Share limits compare the user's estimated share of an account bucket (quota.attribution) with an allocation in
percentage points.
"""
from __future__ import annotations

import fnmatch
import math
import sqlite3
import time
from dataclasses import dataclass

from . import quota, tickets
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

    @property
    def pct(self) -> float | None:
        if self.current is None or not self.limit:
            return None
        return 100.0 * self.current / self.limit

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
    # A model-less request (GET /v1/models) costs nothing and is neither.
    where = "user_id=? AND rejected_by IS NULL AND path!=? AND COALESCE(requested_model, model) IS NOT NULL"
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
                          f"AND path!=? GROUP BY rm, model", (user_id, COUNT_TOKENS_PATH)).fetchall()
    if scope != "*":
        groups = [g for g in groups if fnmatch.fnmatchcase(g["rm"] or "", scope) or fnmatch.fnmatchcase(g["model"] or "", scope)]
    current = sum(_amount(kind, unit, price_totals(cfg.pricing, g["model"], Totals(g["n"], g["i"], g["o"], g["c5"], g["c1"], g["cr"])))
                  for g in groups)
    return LimitState(kind, scope, row["value"], unit, current=current, limit=limit,
                      remaining=max(0.0, limit - current), exceeded=current >= limit)


def _share_state(conn, cfg, user_id, row, now) -> LimitState:
    limit = float(row["value"])
    st = LimitState(row["kind"], row["scope"], row["value"], row["unit"], limit=limit, estimated=True)
    att = quota.attribution(conn, cfg.pricing, SHARE_BUCKETS[row["kind"]], now=now, stale_after_s=cfg.quota.stale_after_s)
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


def _weighted_since(conn, cfg, user_id, since, now) -> float:
    """The user's weighted Anthropic tokens that ended in (since, now], weighed as quota.observed_rate weighs them."""
    span = "user_id=? AND provider='anthropic' AND rejected_by IS NULL AND path!=? AND ended_at>? AND ended_at<=?"
    params = (user_id, COUNT_TOKENS_PATH, since, now)
    models = [m for (m,) in conn.execute(f"SELECT DISTINCT model FROM requests WHERE {span}", params)]
    w, args = quota.weighted_sql(cfg.pricing, models)
    return float(conn.execute(f"SELECT COALESCE(SUM({w}), 0) FROM requests WHERE {span}", (*args, *params)).fetchone()[0])


def _finish(st: LimitState) -> LimitState:
    st.remaining = max(0.0, st.limit - st.current)
    st.exceeded = st.current >= st.limit
    return st


def _ticket_5h(conn, cfg, user_id, share, label, now) -> LimitState:
    st = LimitState("share_5h", "*", f"{share:g}", "pct", limit=share, estimated=True, tier=label)
    att = quota.attribution_cached(conn, cfg.pricing, "5h", now=now, stale_after_s=cfg.quota.stale_after_s)
    if att["resets_at"] and att["resets_at"] > now:
        st.resets_at, st.reset_in = att["resets_at"], max(1, math.ceil(att["resets_at"] - now))
    if att["utilization_pct"] is not None and not att["stale"]:
        st.current = att["shares"].get(user_id, 0.0)
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
    st.current, st.no_live_data = _weighted_since(conn, cfg, user_id, start, now) / rate, True
    return _finish(st)


def _ticket_day(conn, cfg, user_id, share, label, day_start, day_end, now) -> LimitState:
    limit = share / 7
    st = LimitState("share_day", "*", f"{limit:g}", "pct", limit=limit, estimated=True, tier=label,
                    resets_at=day_end, reset_in=max(1, math.ceil(day_end - now)))
    # Measured from the user's own tokens at the observed rate, fresh snapshots or not: Anthropic reports utilization in
    # whole-percent steps, and a Lite day (0.71 points) is smaller than one step, so the share attributed since the day
    # began would read 0 for most of a day and then jump. The cheap staleness check only decides the "no live data" mark.
    newest = conn.execute("SELECT observed_at FROM quota_snapshots WHERE bucket='7d' ORDER BY observed_at DESC LIMIT 1").fetchone()
    st.no_live_data = newest is None or now - newest[0] > cfg.quota.stale_after_s
    used = _weighted_since(conn, cfg, user_id, day_start, now)
    if not used and not st.no_live_data:
        st.current = 0.0   # live data and nothing used today: no rate needed, so a young account isn't refused
        return _finish(st)
    rate = quota.observed_rate(conn, cfg.pricing, "7d", now)
    if rate is None:
        st.skipped, st.no_live_data = NO_RATE, False
        return st
    st.current = used / rate
    return _finish(st)


def ticket_states(conn, cfg: Config, user_id: int, now: float, ticket: dict | None = None) -> list[LimitState]:
    """The two share limits a ticket builds (spec section 7): share_5h against Anthropic's 5-hour window, and share_day,
    one seventh of the share, against the user's weighted tokens since the current ticket day began at the weekly
    bucket's observed rate."""
    t = ticket or tickets.covering(conn, user_id, now)
    if t is None:
        return []
    share = t["share_pct"] + tickets.bonus_share(conn, t["id"], now)
    label = cfg.tickets.tiers[t["tier"]].label if t["tier"] in cfg.tickets.tiers else t["tier"]
    day_start, day_end = tickets.current_day(t, now)
    return [_ticket_5h(conn, cfg, user_id, share, label, now), _ticket_day(conn, cfg, user_id, share, label, day_start, day_end, now)]


def _no_ticket_message(conn, cfg, user_id, now) -> str:
    nxt = tickets.next_queued(conn, user_id, now)
    if nxt:
        return f"Your next ticket starts on {_date(nxt['starts_at'])}."
    last = tickets.last_ended(conn, user_id, now)
    if last is None:   # e.g. their only ticket was cancelled before it started
        return f"You have no ticket. {cfg.tickets.how_to_buy}".strip()
    return f"Your ticket ended on {_date(last['ended_at'])}. {cfg.tickets.how_to_buy}".strip()


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


SHARE_LABELS = {"share_5h": "5-hour limit", "share_7d": "weekly limit", "share_day": "today's share"}
USER_KINDS = {"share_5h": "5h_limit", "share_7d": "weekly_limit", "share_day": "today_limit"}   # what a non-admin calls a share limit
TICKET = "ticket"                  # rejected_by: a ticket-gated user with no active ticket, or on a third-party model
TICKET_NO_DATA = "ticket_no_data"  # rejected_by: the 503 while no usage rate was ever observed
NO_RATE = "no usage rate observed yet"
PAUSED = "Tickets are paused; ask the admin."   # a ticket-gated user while [tickets] is switched off


def _describe(st: LimitState) -> str:
    # Sent to the user, who never learns about the account behind the gateway: a share limit reads as
    # their own allowance, used up.
    scope = f" for models {st.scope}" if st.scope != "*" else ""
    wait = f"; retry in {human(st.reset_in)}" if st.reset_in else ""
    if st.kind == "share_day":
        return (f"Today's share of your {st.tier} ticket is used up. It resets at "
                f"{time.strftime('%H:%M UTC', time.gmtime(st.resets_at))}.")
    if st.kind in SHARE_LABELS:
        return f"Gateway {SHARE_LABELS[st.kind]}{scope} reached: {st.pct or 0:.0f}% used{wait}."
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
    d.pop("resets_at", None)   # an account-side reset time; reset_in (already relative) is what a non-admin gets
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


def free_credit_spend(conn: sqlite3.Connection, cfg: Config, now: float) -> tuple[float, int | None]:
    """What all accounts on a one-time credit (a cost_total limit) spent together in the last 24 hours, and, when
    that is at the cap, the seconds until enough of it has left the window to be under it."""
    cap = cfg.signup.free_daily_cap_usd
    where = ("user_id IN (SELECT user_id FROM limits WHERE kind='cost_total') AND started_at>? "
             "AND rejected_by IS NULL AND path!=?")
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
             now: float | None = None, inflight: int = 0) -> Decision | None:
    """None to allow, or the rejection to send. `inflight`: how many of the user's metered requests are being served
    right now; recorded only when they finish, they would otherwise slip past every limit."""
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
    ticket = None
    if gated:
        ticket = tickets.covering(conn, user_id, now)
        if ticket is None:   # every request, the model list included: there is no ticket to serve it on
            return Decision(403, TICKET, _perm(_no_ticket_message(conn, cfg, user_id, now)))
    rows = _rows(conn, user_id)
    if gated and third_party and not any(r["kind"].startswith("cost_") and (r["scope"] == "*" or fnmatch.fnmatchcase(model or "", r["scope"]))
                                         for r in rows):
        # Third-party models are real money per request, which a ticket does not cover; a cost limit the admin set for
        # this model governs instead (scoped as the loop below scopes rows).
        return Decision(403, TICKET, _perm("Your ticket covers Claude models only."))
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
        for st in ticket_states(conn, cfg, user_id, now, ticket):
            if st.skipped == NO_RATE:
                return Decision(503, TICKET_NO_DATA, {"type": "error", "error": {"type": "api_error", "message":
                                "Usage data is unavailable. Please retry in a minute."}}, 60)
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
    return None
