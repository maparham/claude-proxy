"""Paid tickets (design 2026-10-03): reserved slices of the account sold by the day, week or month.

Prices, discounts and exchange rates (spec section 6); capacity reserved in time (section 5); grants, cancels,
bonuses and the Ungate action (sections 5, 7, 8). Every change to reservations runs in one BEGIN IMMEDIATE
transaction, so two admins acting at once cannot oversell.
"""
from __future__ import annotations

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


# ---------- capacity (spec section 5) ----------

TICKET_COLS = f"t.*, {db.TICKET_EFFECTIVE_END} AS effective_end"


def _exclude(exclude) -> tuple[str, list]:
    return (" AND t.id!=?", [exclude]) if exclude is not None else ("", [])


def sold_at(conn: sqlite3.Connection, account_id: int, t: float, exclude: int | None = None) -> float:
    """sold(t): shares of the non-cancelled tickets and bonus shares covering `t` (half-open periods).
    `exclude` leaves one ticket and its bonuses out, for re-checking that ticket at a new place."""
    ex, args = _exclude(exclude)
    a = conn.execute(f"SELECT COALESCE(SUM(t.share_pct), 0) FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL "
                     f"AND t.starts_at<=? AND {db.TICKET_EFFECTIVE_END}>?{ex}", (account_id, t, t, *args)).fetchone()[0]
    b = conn.execute(f"SELECT COALESCE(SUM(b.share_pct), 0) FROM ticket_bonuses b JOIN tickets t ON t.id=b.ticket_id "
                     f"WHERE t.account_id=? AND t.cancelled_at IS NULL AND b.cancelled_at IS NULL AND b.starts_at<=? AND b.ends_at>?{ex}",
                     (account_id, t, t, *args)).fetchone()[0]
    return a + b


def _starts_within(conn, account_id: int, starts_at: int, ends_at: int, exclude: int | None = None) -> set[int]:
    """Every ticket or bonus start strictly inside (starts_at, ends_at): the only places sold(t) can rise."""
    ex, args = _exclude(exclude)
    pts = {r[0] for r in conn.execute(f"SELECT t.starts_at FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL "
                                      f"AND t.starts_at>? AND t.starts_at<?{ex}", (account_id, starts_at, ends_at, *args))}
    pts |= {r[0] for r in conn.execute(f"SELECT b.starts_at FROM ticket_bonuses b JOIN tickets t ON t.id=b.ticket_id WHERE t.account_id=? "
                                       f"AND t.cancelled_at IS NULL AND b.cancelled_at IS NULL AND b.share_pct>0 AND b.starts_at>? AND b.starts_at<?{ex}",
                                       (account_id, starts_at, ends_at, *args))}
    return pts


def check_capacity(conn: sqlite3.Connection, cfg: Config, account_id: int, share: float, starts_at: int, ends_at: int,
                   exclude: int | None = None) -> None:
    """sold(t) + share <= max_sold_pct at the period's start and at every start inside it, else CapacityError."""
    ceiling = cfg.tickets.max_sold_pct
    for point in sorted({starts_at, *_starts_within(conn, account_id, starts_at, ends_at, exclude)}):
        sold = sold_at(conn, account_id, point, exclude)
        if sold + share > ceiling + 1e-9:
            raise CapacityError(f"Not enough capacity: {sold:g}% is sold at {_date(point)}, and {share:g}% more would pass the "
                                f"{ceiling:g}% ceiling (max_sold_pct).")


# ---------- tickets ----------

def _row(conn, sql: str, args: tuple) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def get(conn: sqlite3.Connection, ticket_id: int) -> dict:
    t = _row(conn, f"SELECT {TICKET_COLS} FROM tickets t WHERE t.id=?", (ticket_id,))
    if t is None:
        raise TicketError(f"No ticket #{ticket_id}.")
    return t


def user_tickets(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? ORDER BY t.starts_at, t.id", (user_id,))]


def last_end(conn: sqlite3.Connection, user_id: int) -> int | None:
    return conn.execute(f"SELECT MAX({db.TICKET_EFFECTIVE_END}) FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL", (user_id,)).fetchone()[0]


def covering(conn: sqlite3.Connection, user_id: int, now: float) -> dict | None:
    """The user's ticket covering now (starts_at <= now < effective end), or None."""
    return _row(conn, f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL AND t.starts_at<=? "
                      f"AND {db.TICKET_EFFECTIVE_END}>? ORDER BY t.starts_at LIMIT 1", (user_id, now, now))


def next_queued(conn: sqlite3.Connection, user_id: int, now: float) -> dict | None:
    return _row(conn, f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL AND t.starts_at>? "
                      f"ORDER BY t.starts_at LIMIT 1", (user_id, now))


def last_ended(conn: sqlite3.Connection, user_id: int, now: float) -> dict | None:
    """The user's most recently ended ticket, with `ended_at`: its effective end, or when it was cancelled if earlier."""
    best = None
    for t in user_tickets(conn, user_id):
        ended = min(t["effective_end"], t["cancelled_at"]) if t["cancelled_at"] is not None else t["effective_end"]
        if ended <= now and (best is None or ended > best["ended_at"]):
            best = {**t, "ended_at": ended}
    return best


def is_gated(conn: sqlite3.Connection, user_id: int) -> bool:
    """Ticket-gated: at least one ticket row not marked ungated_at, cancelled or not (spec section 7)."""
    return conn.execute("SELECT 1 FROM tickets WHERE user_id=? AND ungated_at IS NULL LIMIT 1", (user_id,)).fetchone() is not None


def current_day(ticket: dict, now: float) -> tuple[int, int]:
    """The ticket day containing now: whole 24-hour steps from starts_at, bonus days included."""
    n = int((now - ticket["starts_at"]) // DAY)
    start = ticket["starts_at"] + n * DAY
    return start, start + DAY


def bonuses(conn: sqlite3.Connection, ticket_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_bonuses WHERE ticket_id=? ORDER BY starts_at, id", (ticket_id,))]


def active_bonuses(conn: sqlite3.Connection, ticket_id: int, now: float) -> list[dict]:
    """Non-cancelled bonuses whose share period covers now."""
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_bonuses WHERE ticket_id=? AND cancelled_at IS NULL AND share_pct>0 "
                                          "AND starts_at<=? AND ends_at>? ORDER BY id", (ticket_id, now, now))]


def bonus_share(conn: sqlite3.Connection, ticket_id: int, now: float) -> float:
    return sum(b["share_pct"] for b in active_bonuses(conn, ticket_id, now))


# ---------- granting ----------

# Limit rows the grant form offers to remove. Share rows are replaced by the ticket while it gates the user and come
# back if the user is ever ungated; the sign-up credit goes with the first ticket anyway.
REMOVABLE = "kind NOT IN ('share_5h', 'share_7d', 'cost_total')"


def preview(conn: sqlite3.Connection, cfg: Config, user, tier: str, length: str, currency: str, now: float) -> dict:
    """Everything the grant form shows before the admin confirms. Raises TicketError for a bad tier, length or currency
    and when the currency has no rate; capacity trouble is reported in `available`, not raised."""
    now = _now(now)
    _check_tier_length(cfg, tier, length)
    steps = currencies(cfg)
    if currency not in steps:
        raise TicketError(f"Unknown currency {currency!r}.")
    rate = current_rate(conn, currency)
    if rate is None:
        raise TicketError(f"No exchange rate for {currency}; set one on the Rates panel first.")
    p = price_now(conn, tier, length, now)
    share = cfg.tickets.tiers[tier].share_pct
    starts_at = max(now, last_end(conn, user["id"]) or 0)
    ends_at = starts_at + LENGTHS[length] * DAY
    try:
        check_capacity(conn, cfg, ACCOUNT_ID, share, starts_at, ends_at)
        available, reason = True, None
    except CapacityError as e:
        available, reason = False, str(e)
    rows = [dict(r) for r in conn.execute(f"SELECT kind, scope, value, unit FROM limits WHERE user_id=? AND {REMOVABLE} ORDER BY kind, scope",
                                          (user["id"],))]
    return {"tier": tier, "label": cfg.tickets.tiers[tier].label, "length": length, "days": LENGTHS[length], "share_pct": share, **p,
            "currency": currency, "rate": rate["rate"], "rate_set_at": rate["set_at"], "stale_rate": rate_is_stale(rate, now),
            "amount": round_local(p["usd"], rate["rate"], steps[currency]),
            "starts_at": starts_at, "ends_at": ends_at, "queued": starts_at > now, "available": available, "reason": reason,
            "sold_out_now": sold_out(conn, cfg, tier, length, now) if starts_at > now else not available,
            "limit_rows": rows,
            "credit": conn.execute("SELECT 1 FROM limits WHERE user_id=? AND kind='cost_total'", (user["id"],)).fetchone() is not None,
            "first_ticket": conn.execute("SELECT 1 FROM tickets WHERE user_id=? LIMIT 1", (user["id"],)).fetchone() is None}


def grant(conn: sqlite3.Connection, cfg: Config, actor: int | None, user, tier: str, length: str, currency: str, note: str = "",
          remove_limits=(), confirm_stale_rate: bool = False, now: float | None = None) -> dict:
    """Sell a ticket. One BEGIN IMMEDIATE transaction: the price, the start (now, or after the user's last ticket), the
    capacity check, the insert, the credit removal and any ticked limit rows, each audited."""
    now = _now(now)
    if not cfg.tickets.enabled:
        raise TicketError("Tickets are not enabled: add a [tickets] section to the config.")
    if user["revoked_at"] is not None:
        raise TicketError(f"{user['name']} is revoked.")
    if not user["enabled"]:
        raise TicketError(f"{user['name']} is disabled; enable them first.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        p = preview(conn, cfg, user, tier, length, currency, now)
        if p["stale_rate"] and not confirm_stale_rate:
            raise TicketError(f"The {currency} rate is {int((now - p['rate_set_at']) // 3600)} hours old. Confirm to grant at it "
                              "anyway, or set today's rate first.")
        check_capacity(conn, cfg, ACCOUNT_ID, p["share_pct"], p["starts_at"], p["ends_at"])
        cur = conn.execute(
            "INSERT INTO tickets(user_id, user_name, account_id, tier, share_pct, length, days, starts_at, ends_at, list_usd, usd, "
            "discount_id, currency, rate, amount, granted_by, granted_at, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user["id"], user["name"], ACCOUNT_ID, tier, p["share_pct"], length, p["days"], p["starts_at"], p["ends_at"], p["list_usd"],
             p["usd"], p["discount_id"], currency, p["rate"], p["amount"], actor, now, (note or "").strip() or None))
        tid = cur.lastrowid
        if conn.execute("DELETE FROM limits WHERE user_id=? AND kind='cost_total'", (user["id"],)).rowcount:
            db.audit(conn, actor, "credit_removed", user["name"], {"ticket_id": tid})
        for kind, scope in remove_limits:
            if conn.execute(f"DELETE FROM limits WHERE user_id=? AND kind=? AND scope=? AND {REMOVABLE}", (user["id"], kind, scope)).rowcount:
                db.audit(conn, actor, "limit_clear", f"{user['name']}:{kind}:{scope}", {"ticket_id": tid})
        db.audit(conn, actor, "ticket_grant", user["name"], {"ticket_id": tid, "tier": tier, "length": length, "usd": p["usd"],
                                                            "list_usd": p["list_usd"], "currency": currency, "amount": p["amount"],
                                                            "starts_at": p["starts_at"], "ends_at": p["ends_at"]})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get(conn, tid)


# ---------- what the panels and /pricing ask ----------

def capacity(conn: sqlite3.Connection, cfg: Config, now: float) -> dict:
    """Share sold now and the highest sold(t) over the next 30 days, against max_sold_pct."""
    now = _now(now)
    points = {now, *_starts_within(conn, ACCOUNT_ID, now, now + 30 * DAY)}
    return {"sold_now_pct": sold_at(conn, ACCOUNT_ID, now), "peak_30d_pct": max(sold_at(conn, ACCOUNT_ID, p) for p in points),
            "max_sold_pct": cfg.tickets.max_sold_pct}


def sold_out(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, now: float) -> bool:
    """A tier and length is sold out when a ticket starting now would fail the capacity rule."""
    now = _now(now)
    try:
        check_capacity(conn, cfg, ACCOUNT_ID, cfg.tickets.tiers[tier].share_pct, now, now + LENGTHS[length] * DAY)
    except CapacityError:
        return True
    return False


# ---------- cancel, bonus, queue moves ----------

def _later_tickets(conn, user_id: int, from_t: int) -> list[dict]:
    """The user's non-cancelled tickets starting at or after `from_t`, earliest first."""
    return [dict(r) for r in conn.execute(f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL "
                                          f"AND t.starts_at>=? ORDER BY t.starts_at, t.id", (user_id, from_t))]


def _shift(conn, cfg: Config, chain: list[dict], delta: int) -> None:
    """Move a user's queued tickets by `delta` seconds, bonus periods with them, each re-checked at its new place.
    Moves toward the past go earliest-first and moves toward the future latest-first, so a moving ticket never meets
    its own neighbour. One savepoint: a move that does not fit undoes them all and raises CapacityError."""
    conn.execute("SAVEPOINT moves")
    try:
        for tk in (chain if delta < 0 else reversed(chain)):
            s, e = tk["starts_at"] + delta, tk["effective_end"] + delta
            check_capacity(conn, cfg, tk["account_id"], tk["share_pct"], s, e, exclude=tk["id"])
            for b in bonuses(conn, tk["id"]):
                if b["cancelled_at"] is None and b["share_pct"] > 0:
                    check_capacity(conn, cfg, tk["account_id"], tk["share_pct"] + b["share_pct"], b["starts_at"] + delta,
                                   b["ends_at"] + delta, exclude=tk["id"])
            conn.execute("UPDATE tickets SET starts_at=starts_at+?, ends_at=ends_at+? WHERE id=?", (delta, delta, tk["id"]))
            conn.execute("UPDATE ticket_bonuses SET starts_at=starts_at+?, ends_at=ends_at+? WHERE ticket_id=? AND cancelled_at IS NULL",
                         (delta, delta, tk["id"]))
        conn.execute("RELEASE moves")
    except BaseException:
        conn.execute("ROLLBACK TO moves")
        conn.execute("RELEASE moves")
        raise


def cancel(conn: sqlite3.Connection, cfg: Config, actor: int | None, ticket_id: int, now: float | None = None) -> dict:
    """Free the slice and end the ticket's bonuses. The user's queued tickets move forward to close the gap, each
    re-checked; if one would not fit they keep their dates and `dates_kept` says so. Never refused."""
    now = _now(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = get(conn, ticket_id)
        if t["cancelled_at"] is not None:
            raise TicketError("This ticket is already cancelled.")
        conn.execute("UPDATE tickets SET cancelled_at=?, cancelled_by=? WHERE id=?", (now, actor, ticket_id))
        conn.execute("UPDATE ticket_bonuses SET cancelled_at=?, cancelled_by=? WHERE ticket_id=? AND cancelled_at IS NULL", (now, actor, ticket_id))
        moved, kept, reason = 0, False, None
        chain = _later_tickets(conn, t["user_id"], t["effective_end"]) if t["user_id"] is not None else []
        if chain:
            shift = max(now, t["starts_at"]) - chain[0]["starts_at"]   # a queued ticket never starts in the past
            if shift < 0:
                try:
                    _shift(conn, cfg, chain, shift)
                    moved = len(chain)
                except CapacityError as e:
                    kept, reason = True, str(e)
        db.audit(conn, actor, "ticket_cancel", t["user_name"], {"ticket_id": ticket_id, "moved": moved, "dates_kept": kept})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"ticket": get(conn, ticket_id), "moved": moved, "dates_kept": kept, "reason": reason}


def add_bonus(conn: sqlite3.Connection, cfg: Config, actor: int | None, ticket_id: int, share_pct=0, extra_days=0, starts_at=None,
              ends_at=None, note: str = "", now: float | None = None) -> dict:
    """Extra share for a period inside the ticket, extra days at the ticket's share, or both. Extra days push the user's
    queued tickets forward; each move and the extension itself are checked against capacity, and one failure refuses
    the whole bonus."""
    now = _now(now)
    share_pct = float(share_pct or 0)
    extra_days = int(extra_days or 0)
    if share_pct < 0 or extra_days < 0 or (share_pct == 0 and extra_days == 0):
        raise TicketError("A bonus needs extra share above 0, extra days above 0, or both.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = get(conn, ticket_id)
        if t["cancelled_at"] is not None:
            raise TicketError("The ticket is cancelled; bonuses go on live tickets.")
        old_end, new_end, moved = t["effective_end"], t["effective_end"] + extra_days * DAY, 0
        if extra_days:
            chain = _later_tickets(conn, t["user_id"], old_end) if t["user_id"] is not None else []
            if chain and chain[0]["starts_at"] < new_end:
                try:
                    _shift(conn, cfg, chain, new_end - chain[0]["starts_at"])
                    moved = len(chain)
                except CapacityError as e:
                    raise CapacityError(f"The extra days would move {t['user_name']}'s queued ticket, which then does not fit: {e}") from e
            check_capacity(conn, cfg, t["account_id"], t["share_pct"], old_end, new_end)
        if share_pct:
            s = max(int(now if starts_at is None else starts_at), t["starts_at"])
            e = min(int(new_end if ends_at is None else ends_at), new_end)
            if s >= e:
                raise TicketError("The bonus share period must lie within the ticket's period.")
            check_capacity(conn, cfg, t["account_id"], share_pct, s, e)
        else:
            s, e = old_end, new_end
        cur = conn.execute("INSERT INTO ticket_bonuses(ticket_id, share_pct, extra_days, starts_at, ends_at, note, granted_by, granted_at) "
                           "VALUES(?,?,?,?,?,?,?,?)", (ticket_id, share_pct, extra_days, s, e, (note or "").strip() or None, actor, now))
        db.audit(conn, actor, "ticket_bonus", t["user_name"], {"ticket_id": ticket_id, "bonus_id": cur.lastrowid, "share_pct": share_pct,
                                                               "extra_days": extra_days, "starts_at": s, "ends_at": e, "moved": moved})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"bonus": dict(conn.execute("SELECT * FROM ticket_bonuses WHERE id=?", (cur.lastrowid,)).fetchone()),
            "ticket": get(conn, ticket_id), "moved": moved}


def ungate(conn: sqlite3.Connection, actor: int | None, user_id: int, now: float | None = None) -> int:
    """Stop the user's tickets from gating them (spec section 7, Exit). Only for a user with no active or queued
    ticket. The tickets remain as sales records. Returns how many rows were marked."""
    now = _now(now)
    name = db._user_name(conn, user_id)
    if covering(conn, user_id, now) or next_queued(conn, user_id, now):
        raise TicketError(f"{name} has an active or queued ticket; cancel it before ungating.")
    n = conn.execute("UPDATE tickets SET ungated_at=? WHERE user_id=? AND ungated_at IS NULL", (now, user_id)).rowcount
    if n == 0:
        raise TicketError(f"{name} has no tickets to ungate.")
    db.audit(conn, actor, "ungate", name, {"tickets": n})
    return n


def user_state(conn: sqlite3.Connection, user_id: int, now: float) -> dict:
    """For the Users page: whether the user is ticket-gated and which ticket covers now or is queued next."""
    current, queued = covering(conn, user_id, now), next_queued(conn, user_id, now)
    return {"gated": is_gated(conn, user_id), "current": current, "queued": queued, "live": current is not None or queued is not None}
