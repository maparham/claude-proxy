"""Which subscription account a sold pass goes on (2026-11-24 spec §4, Placing a pass).

An account may sell when its plan is known and acknowledged, its credential is active and its weekly report is fresh.
Of those where the pass fits (capacity.check), the one with the most free room in Claude Pro weeks (this week's room
× the plan's weekly factor ÷ 100) wins, ties by slug; it fits on none: sold out. Every sale goes through here:
payments, orders, trials and the admin's grant form, which preselects the pick and may name another account.
"""
from __future__ import annotations

import sqlite3

from . import capacity, db, plans, tickets
from .config import LENGTHS, Config

DAY = 86400
PLAN_CHANGED = "This subscription's plan changed; acknowledge it on the Subscriptions page."
NEEDS_LOGIN = "Credential needs login."


def _linked(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(slug, credential state) of every linked subscription account, by slug."""
    return [(r[0], r[1]) for r in conn.execute("SELECT account, state FROM credentials WHERE backend='oauth' ORDER BY account")]


def contexts(conn: sqlite3.Connection, cfg: Config, now: float, exclude=()) -> dict[str, capacity.Context]:
    """One capacity.context per linked account, for judging several passes against the same moment."""
    return {a: capacity.context(conn, cfg, a, now, exclude) for a, _ in _linked(conn)}


def unsellable(conn: sqlite3.Connection, account: str, ctx: capacity.Context, state: str | None = None) -> str | None:
    """Why the account sells nothing now, None when it may sell. `state`: its credential's, read when not given."""
    if state is None:
        row = conn.execute("SELECT state FROM credentials WHERE backend='oauth' AND account=?", (account,)).fetchone()
        state = row[0] if row else None
    why = plans.sells(conn, account)
    if why == "plan_unknown":
        return tickets.PLAN_UNKNOWN
    if why == "plan_changed":
        return PLAN_CHANGED
    if state != "active":
        return NEEDS_LOGIN
    if not ctx.fresh:
        return capacity.NO_REPORT
    return None


def candidates(conn: sqlite3.Connection, cfg: Config, now: float, ctxs: dict | None = None) -> list[str]:
    """The accounts that may sell now, by slug."""
    ctxs = ctxs if ctxs is not None else contexts(conn, cfg, now)
    return [a for a, state in _linked(conn) if a in ctxs and unsellable(conn, a, ctxs[a], state) is None]


def free_pro_weeks(conn: sqlite3.Connection, cfg: Config, account: str, now: float, ctx: capacity.Context | None = None) -> float:
    """This week's free room on the account in Claude Pro weeks; 0 without a fresh report or a plan."""
    ctx = ctx or capacity.context(conn, cfg, account, now)
    f = plans.factor(conn, account)
    return max(0.0, ctx.room or 0.0) * f / 100 if f is not None else 0.0


def best_of(conn: sqlite3.Connection, cfg: Config, accounts, now: float, ctxs: dict) -> str | None:
    """Of `accounts`, the one with the most free Pro weeks, ties by slug."""
    if not accounts:
        return None
    return min(accounts, key=lambda a: (-round(free_pro_weeks(conn, cfg, a, now, ctxs[a]), 9), a))


def judge(conn: sqlite3.Connection, cfg: Config, size: float, size_5h: float | None, starts_at: float, ends_at: float,
          now: float, exclude=(), ctxs: dict | None = None) -> list[tuple[dict, tickets.CapacityError | None]]:
    """Every linked account, by slug, with how a pass of `size` Pro weeks would sit on it: (row, the CapacityError it
    meets there or None). An account that may not sell gets a CapacityError with the reason."""
    ctxs = ctxs if ctxs is not None else contexts(conn, cfg, now, exclude)
    out = []
    for a, state in _linked(conn):
        ctx = ctxs[a] if a in ctxs else capacity.context(conn, cfg, a, now, exclude)
        sh = plans.shares(conn, a, size, size_5h)
        why = unsellable(conn, a, ctx, state)
        err: tickets.CapacityError | None = tickets.CapacityError(why) if why else None
        if err is None:
            try:
                capacity.check(conn, cfg, a, sh[0], sh[1], starts_at, ends_at, now, exclude=exclude, ctx=ctx)
            except tickets.CapacityError as e:
                err = e
        row = {"account": a, "share_pct": sh[0] if sh else None, "share_5h_pct": sh[1] if sh else None,
               "fits": err is None, "reason": str(err) if err else None,
               "free_pro_weeks": free_pro_weeks(conn, cfg, a, now, ctx)}
        if isinstance(err, tickets.FiveHourFull):
            row["available_at"] = err.resets_at
        out.append((row, err))
    return out


def pick(conn: sqlite3.Connection, cfg: Config, size: float, size_5h: float | None, starts_at: float, ends_at: float,
         now: float, exclude=(), ctxs: dict | None = None) -> tuple[str | None, list[dict]]:
    """(the account a pass of `size` Pro weeks goes on, None when it fits nowhere; every linked account's row)."""
    ctxs = ctxs if ctxs is not None else contexts(conn, cfg, now, exclude)
    rows = [r for r, _ in judge(conn, cfg, size, size_5h, starts_at, ends_at, now, exclude, ctxs)]
    return best_of(conn, cfg, [r["account"] for r in rows if r["fits"]], now, ctxs), rows


def pick_tier(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, starts_at: float, now: float, exclude=(),
              ctxs: dict | None = None) -> tuple[str | None, list[dict]]:
    """pick for a `length` pass of `tier`, sized as the tier is today."""
    size = tickets.tier_size(conn, cfg, tier)
    if size is None:
        raise tickets.TicketError(f"The {tier} tier has no size.")
    return pick(conn, cfg, size, tickets.tier_size_5h(conn, cfg, tier), starts_at, starts_at + LENGTHS[length] * DAY, now,
                exclude, ctxs)


def best_candidate(conn: sqlite3.Connection, cfg: Config, now: float, ctxs: dict | None = None) -> str | None:
    """The account that may sell with the most free Pro weeks, whether or not a given pass fits there."""
    ctxs = ctxs if ctxs is not None else contexts(conn, cfg, now)
    return best_of(conn, cfg, candidates(conn, cfg, now, ctxs), now, ctxs)


def best_account(conn: sqlite3.Connection, cfg: Config, now: float, ctxs: dict | None = None) -> str | None:
    """The account with the most free room for a new user or an undated split: the best that may sell, else the first
    active linked account by slug, else None."""
    best = best_candidate(conn, cfg, now, ctxs)
    if best is not None:
        return best
    return next((a for a, state in _linked(conn) if state == "active"), None)


def any_fresh(conn: sqlite3.Connection, cfg: Config, now: float) -> bool:
    """Whether any account may sell now: without one, nothing is on sale (the price lists' `unavailable`)."""
    return bool(candidates(conn, cfg, now))


def assign(conn: sqlite3.Connection, cfg: Config, user_id: int, now: float) -> str | None:
    """Assign the user the account with the most free room (spec §3, a new user's assignment); audited."""
    account = best_account(conn, cfg, now)
    conn.execute("UPDATE users SET account=? WHERE id=?", (account, user_id))
    name = conn.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
    db.audit(conn, None, "user_account_set", name[0] if name else str(user_id), {"account": account})
    return account
