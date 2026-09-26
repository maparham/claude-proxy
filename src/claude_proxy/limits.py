"""Per-user limits, evaluated before forwarding (spec sections 8 and 17.2).

Usage limits count only forwarded requests (never rejections) in a window that opens with the first request and
then resets all at once, like Claude's own 5-hour limit, optionally restricted to models matching a glob `scope`.
Share limits compare the user's estimated share of an account bucket (quota.attribution) with an allocation in
percentage points.
"""
from __future__ import annotations

import fnmatch
import sqlite3
import time
from dataclasses import dataclass

from . import quota
from .config import Config
from .forwarder import COUNT_TOKENS_PATH
from .usage import SUMS, Totals, price_totals

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

    @property
    def pct(self) -> float | None:
        if self.current is None or not self.limit:
            return None
        return 100.0 * self.current / self.limit

    def to_dict(self) -> dict:
        return {"kind": self.kind, "scope": self.scope, "value": self.value, "unit": self.unit,
                "current": self.current, "limit": self.limit, "remaining": self.remaining,
                "reset_in": self.reset_in, "exceeded": self.exceeded, "skipped": self.skipped,
                "estimated": self.estimated, "pct": self.pct}


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
                          f"AND started_at>=? AND COALESCE(requested_model, model) IS NOT NULL GROUP BY rm, model",
                          (*params, since)).fetchall()
    if scope != "*":
        firsts = [g for g in firsts if fnmatch.fnmatchcase(g["rm"] or "", scope) or fnmatch.fnmatchcase(g["model"] or "", scope)]
    if not firsts:
        return None
    start = min(g["first"] for g in firsts)
    conn.execute("INSERT OR REPLACE INTO limit_windows(user_id, kind, scope, started_at) VALUES(?,?,?,?)",
                 (user_id, kind, scope, start))
    return start


def _window_state(conn, cfg, user_id, row, now) -> LimitState:
    # Aggregated in SQL: this runs before every request, on the event loop that serves every stream.
    kind, scope, unit = row["kind"], row["scope"], row["unit"]
    limit = float(row["value"])
    where = "user_id=? AND rejected_by IS NULL AND path!=?"
    params = [user_id, COUNT_TOKENS_PATH]
    start = _window_start(conn, user_id, row, now, where, params)
    if start is None:
        return LimitState(kind, scope, row["value"], unit, current=0.0, limit=limit, remaining=limit, exceeded=limit <= 0)
    groups = conn.execute(f"SELECT requested_model AS rm, model, {SUMS} FROM requests "
                          f"WHERE {where} AND started_at>=? GROUP BY rm, model", (*params, start)).fetchall()
    if scope != "*":
        # A scope names what clients ask for; providers may answer with a longer model id.
        groups = [g for g in groups if fnmatch.fnmatchcase(g["rm"] or "", scope) or fnmatch.fnmatchcase(g["model"] or "", scope)]
    current = sum(_amount(kind, unit, price_totals(cfg.pricing, g["model"], Totals(g["n"], g["i"], g["o"], g["c5"], g["c1"], g["cr"])))
                  for g in groups)
    return LimitState(kind, scope, row["value"], unit, current=current, limit=limit, remaining=max(0.0, limit - current),
                      reset_in=max(1, int(round(start + WINDOWS[kind] - now))), exceeded=current >= limit)


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
        st.reset_in = max(1, int(att["resets_at"] - now))
    return st


def states(conn: sqlite3.Connection, cfg: Config, user_id: int, now: float | None = None) -> list[LimitState]:
    now = time.time() if now is None else now
    out = []
    for row in _rows(conn, user_id):
        if row["kind"] in WINDOWS:
            out.append(_window_state(conn, cfg, user_id, row, now))
        elif row["kind"] in TOTALS:
            out.append(_total_state(conn, cfg, user_id, row))
        elif row["kind"] in SHARE_BUCKETS:
            out.append(_share_state(conn, cfg, user_id, row, now))
        else:
            out.append(LimitState(row["kind"], row["scope"], row["value"], row["unit"]))
    return out


SHARE_LABELS = {"share_5h": "5-hour limit", "share_7d": "weekly limit"}
USER_KINDS = {"share_5h": "5h_limit", "share_7d": "weekly_limit"}   # what a non-admin calls a share limit


def _describe(st: LimitState) -> str:
    # Sent to the user, who never learns about the account behind the gateway: a share limit reads as
    # their own allowance, used up.
    scope = f" for models {st.scope}" if st.scope != "*" else ""
    wait = f"; retry in {human(st.reset_in)}" if st.reset_in else ""
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


def evaluate(conn: sqlite3.Connection, cfg: Config, user_id: int, model: str | None, path: str,
             now: float | None = None) -> Decision | None:
    """None to allow, or the rejection to send."""
    now = time.time() if now is None else now
    is_count_tokens = path == COUNT_TOKENS_PATH
    third_party = cfg.route_for(model) is not None
    for row in _rows(conn, user_id):
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
            st = _window_state(conn, cfg, user_id, row, now)
        elif kind in TOTALS:
            st = _total_state(conn, cfg, user_id, row)
        elif kind in SHARE_BUCKETS:
            if third_party:
                continue
            st = _share_state(conn, cfg, user_id, row, now)
        else:
            continue
        if st.exceeded and kind in TOTALS:   # it never frees up, so nothing for a client to wait for and retry
            return Decision(403, kind, {"type": "error", "error": {"type": "permission_error", "message": _describe(st)}})
        if st.exceeded:
            return Decision(429, kind, {"type": "error", "error": {"type": "rate_limit_error", "message": _describe(st)}},
                            st.reset_in or 60)
    return None
