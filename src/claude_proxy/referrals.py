"""Referrals (design 2026-11-07): a user's personal link, a discount for the friend who signs up through it, and a
saved discount unit for the referrer when that friend's first pass is paid for and granted.

This module holds the admin-controlled settings and the cost floor every referral discount is measured against:
no discount, friend or unit, may ever push a pass below what it costs the gateway on the Anthropic account.
"""
from __future__ import annotations

import dataclasses
import json
import math
import secrets
import sqlite3
from dataclasses import dataclass

from . import db, tickets
from .config import LENGTHS, Config

SETTINGS_KEY = "referrals"


class ReferralError(Exception):
    """A refused referral setting, with the HTTP status it maps to, like orders.OrderError."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Settings:
    enabled: bool = False
    friend_pct: float = 10.0
    unit_pct: float = 10.0
    max_held: int = 5
    max_per_month: int = 5
    unit_days: int = 90
    approval: str = "auto"
    account_cost_usd: float = 100.0
    fee_pct: float = 0.0


_FIELDS = {f.name for f in dataclasses.fields(Settings)}
_PERCENT_0_90 = ("friend_pct", "unit_pct")
_COUNT_0_1000 = ("max_held", "max_per_month")


def _validate(key: str, value):
    if key not in _FIELDS:
        raise ReferralError(400, f"Unknown referral setting {key!r}.")
    if key == "enabled":
        if not isinstance(value, bool):
            raise ReferralError(400, "enabled must be true or false.")
        return value
    if key in _PERCENT_0_90:
        value = float(value)
        if not (0 <= value <= 90):
            raise ReferralError(400, f"{key} must be from 0 to 90.")
        return value
    if key in _COUNT_0_1000:
        if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= 1000):
            raise ReferralError(400, f"{key} must be a whole number from 0 to 1000.")
        return value
    if key == "unit_days":
        if not isinstance(value, int) or isinstance(value, bool) or not (1 <= value <= 3650):
            raise ReferralError(400, "unit_days must be a whole number from 1 to 3650.")
        return value
    if key == "approval":
        if value not in ("auto", "manual"):
            raise ReferralError(400, "approval must be 'auto' or 'manual'.")
        return value
    if key == "account_cost_usd":
        value = float(value)
        if value <= 0:
            raise ReferralError(400, "account_cost_usd must be greater than 0.")
        return value
    if key == "fee_pct":
        value = float(value)
        if not (0 <= value <= 20):
            raise ReferralError(400, "fee_pct must be from 0 to 20.")
        return value
    raise ReferralError(400, f"Unknown referral setting {key!r}.")  # pragma: no cover (every field above is handled)


def settings(conn: sqlite3.Connection) -> Settings:
    """The admin's referral settings; keys never stored (or never changed) take the defaults above."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SETTINGS_KEY,)).fetchone()
    stored = json.loads(row[0]) if row else {}
    return Settings(**{k: v for k, v in stored.items() if k in _FIELDS})


def set_settings(conn: sqlite3.Connection, actor: int | None, changes: dict) -> Settings:
    """Validate every change before writing any of them, store the merged settings, and audit the changed keys
    (like the prices table, tickets.set_share)."""
    current = settings(conn)
    validated = {key: _validate(key, value) for key, value in changes.items()}
    merged = dataclasses.replace(current, **validated)
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                 (SETTINGS_KEY, json.dumps(dataclasses.asdict(merged))))
    db.audit(conn, actor, "referral_settings", "referrals", validated)
    return merged


def pass_cost(conn: sqlite3.Connection, cfg: Config, tier: str, days: int) -> float:
    """What a pass of this tier and length costs the gateway on the Anthropic account: the account's cost per 30
    days, times the tier's current share (tickets.effective_share, so a later share change keeps the floor right),
    times the days it covers."""
    cfg_settings = settings(conn)
    return cfg_settings.account_cost_usd * tickets.effective_share(conn, cfg, tier) / 100 * days / 30


def apply_pct(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, usd: float, pct: float) -> tuple[float, float]:
    """Apply a referral discount of `pct` percent to `usd` (the price before VAT), lowered only as far as needed to
    keep what the gateway keeps - `new_usd` less the payment fee, taken on the full amount charged (VAT included) -
    at or above `pass_cost`. `new_usd` rounds up to the cent, so rounding can never push a sale back under the floor.
    No room at all (fee alone would exceed what is kept, or the price is already at or below cost) clamps to no
    discount at all, `(usd, 0.0)`, rather than a negative one."""
    cfg_settings = settings(conn)
    cost = pass_cost(conn, cfg, tier, LENGTHS[length])
    vat = tickets.vat_percent(cfg) / 100
    fee = cfg_settings.fee_pct / 100
    keep_factor = 1 - fee * (1 + vat)
    if keep_factor <= 0:
        return (usd, 0.0)
    wanted = usd * (1 - pct / 100)
    floor_usd = cost / keep_factor
    if wanted >= floor_usd:
        new_usd, pct_applied = wanted, pct
    else:
        new_usd, pct_applied = floor_usd, None
    new_usd = math.ceil(new_usd * 100) / 100
    if new_usd >= usd:
        return (usd, 0.0)
    if pct_applied is None:
        pct_applied = (1 - new_usd / usd) * 100
    return (new_usd, pct_applied)


# ---------- the link: codes, resolving them, and attaching a friend at sign-up ----------

CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/1/l/i/o: never misread off a screen or printout
CODE_LEN = 8


def code_for(conn: sqlite3.Connection, user_id: int) -> str:
    """The user's personal link code, created and stored on first call, stable after. Retried on a collision with
    another user's code (users_ref_code, the partial unique index that ignores NULL)."""
    row = conn.execute("SELECT ref_code FROM users WHERE id=?", (user_id,)).fetchone()
    if row["ref_code"]:
        return row["ref_code"]
    for _ in range(10):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        try:
            conn.execute("UPDATE users SET ref_code=? WHERE id=?", (code, user_id))
            return code
        except sqlite3.IntegrityError:   # another user already has this code; try again
            continue
    raise RuntimeError("Could not generate a free referral code after 10 tries.")


def user_by_code(conn: sqlite3.Connection, code: str) -> int | None:
    """The id of the enabled, not revoked user this code belongs to (case-insensitive), or None for an unknown,
    disabled or revoked one - the same as a code that resolves to nobody, as far as a friend's sign-up is concerned."""
    row = conn.execute("SELECT id FROM users WHERE lower(ref_code)=lower(?) AND enabled=1 AND revoked_at IS NULL",
                       (code,)).fetchone()
    return row["id"] if row else None


def attach(conn: sqlite3.Connection, friend_id: int, code: str | None) -> bool:
    """Sets users.referred_by on a brand-new account, when referrals are on, `code` resolves to another enabled
    user, and it is not the friend's own code (a user cannot refer themselves). Audits `referral_signup`. Must run
    inside the sign-up transaction (web.py clerk_account), before COMMIT: referred_by is fixed at Clerk sign-up and
    never changes after, so this is never called for an existing account."""
    if not code:
        return False
    if not settings(conn).enabled:
        return False
    referrer_id = user_by_code(conn, code)
    if referrer_id is None or referrer_id == friend_id:
        return False
    conn.execute("UPDATE users SET referred_by=? WHERE id=?", (referrer_id, friend_id))
    db.audit(conn, friend_id, "referral_signup", db._user_name(conn, friend_id), {"referrer_id": referrer_id})
    return True


def first_pass(conn: sqlite3.Connection, user_id: int) -> bool:
    """True when this account's email has never had a pass granted, by purchase or by the admin, on this account or
    an earlier one: no ticket for this user_id, no ticket whose user_name (a snapshot taken at grant time, so it
    survives the account being deleted) is this email, and no `done` order with that email. Checked by email, not
    the audit log (controller ruling), so "delete the account, sign up again with the same email" gets nothing."""
    row = conn.execute("SELECT email, name FROM users WHERE id=?", (user_id,)).fetchone()
    email = (row["email"] or row["name"] or "").lower()
    if conn.execute("SELECT 1 FROM tickets WHERE user_id=? OR lower(user_name)=? LIMIT 1", (user_id, email)).fetchone():
        return False
    if conn.execute("SELECT 1 FROM orders WHERE status='done' AND lower(email)=? LIMIT 1", (email,)).fetchone():
        return False
    return True


# ---------- the discount on an order, and the life of a unit ----------
# A unit goes saved -> held (by the open order that took its discount) -> used (a payment on that order verified, or
# the order was granted), or back to saved when the order is withdrawn or declined with no verified payment (design:
# The referrer's reward). Every function here writes inside the caller's transaction, except spend's own one when it
# is called outside any (payments._unfulfilled).

def expire(conn: sqlite3.Connection, now: int) -> None:
    """Saved units past their expires_at become `expired`, each audited. A held unit is left alone: it stays with its
    open order even past its expiry (the buyer already saw that price); release() decides its fate when the order closes."""
    rows = conn.execute("SELECT id, user_id FROM referral_units WHERE state='saved' AND expires_at<=?", (now,)).fetchall()
    for r in rows:
        conn.execute("UPDATE referral_units SET state='expired', updated_at=? WHERE id=? AND state='saved'", (now, r["id"]))
        db.audit(conn, None, "referral_unit_expired", db._user_name(conn, r["user_id"]), {"unit_id": r["id"]})


def quote(conn: sqlite3.Connection, cfg: Config, user_id: int, tier: str, length: str, usd: float, now: int) -> dict | None:
    """The referral discount a new order by `user_id` would get on `usd` (the price before VAT, any sale already
    taken off), without taking it: the friend discount on the first pass of an account signed up through a link while
    referrals are on; otherwise the user's oldest saved, unexpired unit (usable even with referrals off, design:
    Settings). Never both, and never more than one unit. None when neither applies. `pct_applied` is what the cost floor
    left of `pct` (apply_pct); 0 means no discount (hold() then takes no unit, but still marks a friend order). Expires overdue units first, so an
    expired one is never offered; nothing else is written."""
    expire(conn, now)
    s = settings(conn)
    row = conn.execute("SELECT referred_by FROM users WHERE id=?", (user_id,)).fetchone()
    if s.enabled and row is not None and row["referred_by"] is not None and first_pass(conn, user_id):
        kind, pct, unit_id = "friend", s.friend_pct, None
    else:
        unit = conn.execute("SELECT id, pct FROM referral_units WHERE user_id=? AND state='saved' AND expires_at>? "
                            "ORDER BY earned_at, id LIMIT 1", (user_id, now)).fetchone()
        if unit is None:
            return None
        kind, pct, unit_id = "unit", unit["pct"], unit["id"]
    new_usd, pct_applied = apply_pct(conn, cfg, tier, length, usd, pct)
    return {"kind": kind, "pct": pct, "pct_applied": pct_applied, "usd": new_usd, "list_usd": usd,
            "off_usd": round(usd - new_usd, 2), "unit_id": unit_id}


def hold(conn: sqlite3.Connection, order_id: int, q: dict | None, now: int) -> None:
    """Inside orders.create's transaction, right after the INSERT: record the discount `q` (quote()) on the order and,
    for a unit, hold it for this order. A friend's first order is always marked `friend`, even at 0% off (the cost
    floor, or friend_pct 0): it is the friend's first paid pass that earns the referrer a unit, discounted or not.
    A unit the cost floor took to zero records nothing and holds nothing: it stays saved (design: The cost floor), and
    the order has no ref_kind and no list_usd. The buyer's one open order rule, checked in the same transaction, means
    no unit is ever held twice."""
    if q is None or (q["kind"] == "unit" and not q["pct_applied"] > 0):
        return
    if q["kind"] == "unit":
        held = conn.execute("UPDATE referral_units SET state='held', order_id=?, updated_at=? WHERE id=? AND state='saved'",
                            (order_id, now, q["unit_id"])).rowcount
        if held != 1:   # pragma: no cover (quote and hold share one locked transaction)
            raise RuntimeError(f"Referral unit #{q['unit_id']} is no longer saved.")
    # ref_pct is the pct asked for, not pct_applied, on purpose: a grant re-floors it on the price used then (Task 4).
    conn.execute("UPDATE orders SET ref_kind=?, ref_pct=?, ref_off_usd=?, ref_unit_id=?, list_usd=? WHERE id=?",
                 (q["kind"], q["pct"], q["off_usd"], q["unit_id"], q["list_usd"], order_id))
    buyer = conn.execute("SELECT user_id FROM orders WHERE id=?", (order_id,)).fetchone()["user_id"]
    db.audit(conn, buyer, "referral_discount", f"order #{order_id}",
             {"kind": q["kind"], "pct": q["pct"], "pct_applied": q["pct_applied"], "off_usd": q["off_usd"],
              **({"unit_id": q["unit_id"]} if q["unit_id"] is not None else {})})


def spend(conn: sqlite3.Connection, order_id: int, now: int) -> None:
    """The unit held by this order is used for good: a payment on it verified (paid, or paid_unfulfilled), or it was
    granted. Idempotent: a unit already used by this order is left as it is. Runs in the caller's transaction, or in
    its own when there is none (payments._unfulfilled), so the state change and its audit row land together.
    A payment can verify after its order was closed (payments._close withdrew it, release() gave the unit back): the
    buyer still paid the discounted price, so the order's ref_unit_id is used even though no longer held by it, unless
    another order now holds or used it - then it is left there and a `referral_unit_conflict` row names both orders
    for the admin to settle."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        unit = conn.execute("SELECT id, user_id FROM referral_units WHERE order_id=? AND state='held'", (order_id,)).fetchone()
        o = conn.execute("SELECT ref_kind, ref_unit_id, ref_off_usd FROM orders WHERE id=?", (order_id,)).fetchone()
        if unit is None and o is not None and o["ref_kind"] == "unit" and o["ref_unit_id"] is not None:
            late = conn.execute("SELECT id, user_id, state, order_id FROM referral_units WHERE id=?",
                                (o["ref_unit_id"],)).fetchone()
            if late is not None and late["state"] in ("saved", "expired", "cancelled"):
                unit = late
            elif late is not None and late["order_id"] != order_id:   # held or used by another order meanwhile
                db.audit(conn, None, "referral_unit_conflict", db._user_name(conn, late["user_id"]),
                         {"unit_id": late["id"], "order_id": order_id, "other_order_id": late["order_id"],
                          "state": late["state"]})
        if unit is not None:
            conn.execute("UPDATE referral_units SET state='used', order_id=?, updated_at=? WHERE id=?",
                         (order_id, now, unit["id"]))
            db.audit(conn, None, "referral_unit_used", db._user_name(conn, unit["user_id"]),
                     {"unit_id": unit["id"], "order_id": order_id, "off_usd": o["ref_off_usd"] if o else None})
        if own:
            conn.execute("COMMIT")
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise


def release(conn: sqlite3.Connection, order_id: int, now: int) -> None:
    """Inside the transaction that withdraws or declines the order: its held unit goes back to saved, or to expired if
    it is past expires_at meanwhile. If any payment on the order verified (paid or paid_unfulfilled), the buyer was
    charged the discounted price, so the unit is spent instead (design: used as soon as any payment is verified)."""
    if conn.execute("SELECT 1 FROM payments WHERE order_id=? AND status IN ('paid','paid_unfulfilled') LIMIT 1",
                    (order_id,)).fetchone():
        spend(conn, order_id, now)
        return
    unit = conn.execute("SELECT id, user_id, expires_at FROM referral_units WHERE order_id=? AND state='held'",
                        (order_id,)).fetchone()
    if unit is None:
        return
    if unit["expires_at"] <= now:
        conn.execute("UPDATE referral_units SET state='expired', updated_at=? WHERE id=?", (now, unit["id"]))
        db.audit(conn, None, "referral_unit_expired", db._user_name(conn, unit["user_id"]),
                 {"unit_id": unit["id"], "order_id": order_id})
    else:
        conn.execute("UPDATE referral_units SET state='saved', order_id=NULL, updated_at=? WHERE id=?", (now, unit["id"]))
        db.audit(conn, None, "referral_unit_returned", db._user_name(conn, unit["user_id"]),
                 {"unit_id": unit["id"], "order_id": order_id})
