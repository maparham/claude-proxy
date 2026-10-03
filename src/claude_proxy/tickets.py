"""Paid tickets (design 2026-10-03): reserved slices of the account sold by the day, week or month.

Prices, discounts and exchange rates (spec section 6); capacity reserved in time (section 5); grants, cancels,
bonuses and the Ungate action (sections 5, 7, 8). Every change to reservations runs in one BEGIN IMMEDIATE
transaction, so two admins acting at once cannot oversell.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from decimal import ROUND_HALF_UP, Decimal

from . import db
from .config import LENGTHS, Config

DAY = 86400
ACCOUNT_ID = 1   # step 1 has exactly one account (spec section 1)

STALE_RATE_S = 36 * 3600
USD_ROUND_TO = 0.01


class TicketError(Exception):
    """A refused ticket operation. The message is written for the admin who asked."""


class CapacityError(TicketError):
    """The period would push what is sold over max_sold_pct."""


def _date(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def _now(now) -> int:
    return int(time.time() if now is None else now)


def _positive(v, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise TicketError(f"{what} must be a number above 0.")
    return float(v)


# ---------- money (spec section 6) ----------

def round_local(usd: float, rate: float, round_to: float) -> float:
    """round(usd × rate, round_to), ties rounded up: $20 at 0.92 with a 0.50 step is €18.50, and so is 18.25."""
    step = Decimal(str(round_to))
    steps = (Decimal(str(usd)) * Decimal(str(rate)) / step).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return float(steps * step)


def currencies(cfg: Config) -> dict[str, float]:
    """Currency code -> rounding step. USD is always there, at an implicit rate of 1."""
    return {"USD": USD_ROUND_TO, **{c: cur.round_to for c, cur in cfg.tickets.currencies.items()}}


def current_rate(conn: sqlite3.Connection, currency: str) -> dict | None:
    """The newest rate for a currency (local units per 1 USD), USD's built-in one, or None when none was ever set."""
    if currency == "USD":
        return {"currency": "USD", "rate": 1.0, "set_at": None, "set_by": None}
    row = conn.execute("SELECT currency, rate, set_at, set_by FROM fx_rates WHERE currency=? ORDER BY set_at DESC, id DESC LIMIT 1",
                       (currency,)).fetchone()
    return dict(row) if row else None


def rate_is_stale(rate: dict, now: float) -> bool:
    return rate["set_at"] is not None and now - rate["set_at"] > STALE_RATE_S


def set_rate(conn: sqlite3.Connection, cfg: Config, currency: str, rate, actor: int | None, now: float | None = None) -> dict:
    if currency not in cfg.tickets.currencies:
        raise TicketError(f"{currency!r} is not a configured currency; add [tickets.currencies.{currency}] to the config first.")
    rate = _positive(rate, "The rate (local units per 1 USD)")
    conn.execute("INSERT INTO fx_rates(currency, rate, set_at, set_by) VALUES(?,?,?,?)", (currency, rate, _now(now), actor))
    db.audit(conn, actor, "rate_set", currency, {"rate": rate})
    return current_rate(conn, currency)


# ---------- prices and discounts ----------

def _check_tier_length(cfg: Config, tier: str, length: str) -> None:
    if tier not in cfg.tickets.tiers:
        raise TicketError(f"Unknown tier {tier!r}.")
    if length not in LENGTHS:
        raise TicketError(f"Unknown length {length!r}; one of {', '.join(LENGTHS)}.")


def regular_price(conn: sqlite3.Connection, tier: str, length: str) -> float:
    row = conn.execute("SELECT usd FROM ticket_prices WHERE tier=? AND length=?", (tier, length)).fetchone()
    if row is None:
        raise TicketError(f"No price for {tier} {length}; the gateway seeds prices at start.")
    return row[0]


def set_price(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, usd, actor: int | None, now: float | None = None) -> None:
    _check_tier_length(cfg, tier, length)
    usd = _positive(usd, "The price")
    conn.execute("INSERT OR REPLACE INTO ticket_prices(tier, length, usd, updated_at, updated_by) VALUES(?,?,?,?,?)",
                 (tier, length, usd, _now(now), actor))
    db.audit(conn, actor, "price_set", f"{tier}:{length}", {"usd": usd})


def prices(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_prices ORDER BY tier, length")]


def active_discount(conn: sqlite3.Connection, tier: str, length: str, now: float) -> dict | None:
    """The discount covering `now`: not cancelled and starts_at <= now < ends_at."""
    row = conn.execute("SELECT * FROM ticket_discounts WHERE tier=? AND length=? AND cancelled_at IS NULL AND starts_at<=? AND ends_at>? "
                       "ORDER BY id LIMIT 1", (tier, length, now, now)).fetchone()
    return dict(row) if row else None


def create_discount(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, usd, starts_at, ends_at, actor: int | None,
                    now: float | None = None) -> dict:
    now = _now(now)
    _check_tier_length(cfg, tier, length)
    usd = _positive(usd, "The discounted price")
    starts_at, ends_at = int(starts_at), int(ends_at)
    if starts_at >= ends_at:
        raise TicketError("A discount must start before it ends.")
    if ends_at <= now:
        raise TicketError("A discount must end in the future.")
    regular = regular_price(conn, tier, length)
    if usd >= regular:
        raise TicketError(f"A discount must be below the regular price (${regular:g}).")
    # Interval overlap against every non-cancelled discount of the tier and length, not a check at `now`.
    clash = conn.execute("SELECT id FROM ticket_discounts WHERE tier=? AND length=? AND cancelled_at IS NULL AND starts_at<? AND ends_at>? "
                         "ORDER BY id LIMIT 1", (tier, length, ends_at, starts_at)).fetchone()
    if clash:
        raise TicketError(f"Discount #{clash[0]} already covers part of that period; cancel it first.")
    cur = conn.execute("INSERT INTO ticket_discounts(tier, length, usd, starts_at, ends_at, created_by, created_at) VALUES(?,?,?,?,?,?,?)",
                       (tier, length, usd, starts_at, ends_at, actor, now))
    db.audit(conn, actor, "discount_create", f"{tier}:{length}", {"id": cur.lastrowid, "usd": usd, "starts_at": starts_at, "ends_at": ends_at})
    return dict(conn.execute("SELECT * FROM ticket_discounts WHERE id=?", (cur.lastrowid,)).fetchone())


def cancel_discount(conn: sqlite3.Connection, discount_id: int, actor: int | None, now: float | None = None) -> None:
    row = conn.execute("SELECT tier, length FROM ticket_discounts WHERE id=? AND cancelled_at IS NULL", (discount_id,)).fetchone()
    if row is None:
        raise TicketError("No such active discount.")
    conn.execute("UPDATE ticket_discounts SET cancelled_at=? WHERE id=?", (_now(now), discount_id))
    db.audit(conn, actor, "discount_cancel", f"{row['tier']}:{row['length']}", {"id": discount_id})


def discounts(conn: sqlite3.Connection, now: float, include_ended: bool = False) -> list[dict]:
    """Non-cancelled discounts, current and future; with `include_ended`, everything, newest first."""
    if include_ended:
        return [dict(r) for r in conn.execute("SELECT * FROM ticket_discounts ORDER BY starts_at DESC, id DESC")]
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_discounts WHERE cancelled_at IS NULL AND ends_at>? ORDER BY starts_at, id", (now,))]


def price_now(conn: sqlite3.Connection, tier: str, length: str, now: float) -> dict:
    """The regular price, what is charged now, and the discount when it is the lower one. A regular price since
    lowered to or below an active discount is charged as is, and no strike-through is shown."""
    list_usd = regular_price(conn, tier, length)
    d = active_discount(conn, tier, length, now)
    if d and d["usd"] < list_usd:
        return {"list_usd": list_usd, "usd": d["usd"], "discount_id": d["id"], "discount_ends_at": d["ends_at"]}
    return {"list_usd": list_usd, "usd": list_usd, "discount_id": None, "discount_ends_at": None}


def seed_prices(conn: sqlite3.Connection, cfg: Config, now: float | None = None) -> int:
    """Insert each tier's default_usd for every length that has no price yet. Returns the rows added."""
    now = int(time.time() if now is None else now)
    n = 0
    for tier, t in cfg.tickets.tiers.items():
        for length in LENGTHS:
            n += conn.execute("INSERT OR IGNORE INTO ticket_prices(tier, length, usd, updated_at, updated_by) VALUES(?,?,?,?,NULL)",
                              (tier, length, float(t.default_usd[length]), now)).rowcount
    return n
