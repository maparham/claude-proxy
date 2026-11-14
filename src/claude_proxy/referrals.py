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


def _number(key: str, value) -> float:
    """A JSON number, finite: bools (an int subclass in Python), strings, None, NaN and infinities are refused with
    400, never a 500 from float() or a NaN that slips through every range check (a NaN account cost would switch the
    cost floor off, final review #4)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ReferralError(400, f"{key} must be a number.")
    return float(value)


def _whole(key: str, value, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not (lo <= value <= hi):
        raise ReferralError(400, f"{key} must be a whole number from {lo} to {hi}.")
    return value


def _validate(key: str, value):
    if key not in _FIELDS:
        raise ReferralError(400, f"Unknown referral setting {key!r}.")
    if key == "enabled":
        if not isinstance(value, bool):
            raise ReferralError(400, "enabled must be true or false.")
        return value
    if key in _PERCENT_0_90:
        value = _number(key, value)
        if not (0 <= value <= 90):
            raise ReferralError(400, f"{key} must be from 0 to 90.")
        return value
    if key in _COUNT_0_1000:
        return _whole(key, value, 0, 1000)
    if key == "unit_days":
        return _whole(key, value, 1, 3650)
    if key == "approval":
        if value not in ("auto", "manual"):
            raise ReferralError(400, "approval must be 'auto' or 'manual'.")
        return value
    if key == "account_cost_usd":
        value = _number(key, value)
        if value <= 0:
            raise ReferralError(400, "account_cost_usd must be greater than 0.")
        return value
    if key == "fee_pct":
        value = _number(key, value)
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
    the audit log (controller ruling), so "delete the account, sign up again with the same email" gets nothing.
    A pass the buyer cancelled and got refunded (refunds.py) still counts: otherwise buying and refunding again and
    again would earn the referrer a unit each time (review of 40197c3)."""
    row = conn.execute("SELECT email, name FROM users WHERE id=?", (user_id,)).fetchone()
    email = (row["email"] or row["name"] or "").lower()
    # A free trial (2026-11-14) is not a pass here: a friend who tried first keeps their discount.
    if conn.execute("SELECT 1 FROM tickets WHERE (user_id=? OR lower(user_name)=?) AND trial=0 LIMIT 1", (user_id, email)).fetchone():
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


def local_quote(conn: sqlite3.Connection, cfg: Config, user_id: int, tier: str, length: str, currency: str,
                now: int) -> dict | None:
    """quote() on today's price (any sale taken off), priced like the price list in `currency` (round_local, VAT in):
    what the order dialog, the price list and the bot's summary show, and what orders.create charges. A currency of
    this gateway with no rate yet, or one it does not have, falls back to USD (always rated), never a 500. Raises
    tickets.TicketError for a tier or length with no price. No unit_id: the buyer has no use for it."""
    q = quote(conn, cfg, user_id, tier, length, tickets.price_now(conn, tier, length, now)["usd"], now)
    if q is None:
        return None
    steps = tickets.currencies(cfg)
    rate = tickets.current_rate(conn, currency) if currency in steps else None
    if rate is None:
        currency, rate = "USD", tickets.current_rate(conn, "USD")
    vat = tickets.vat_percent(cfg)
    q.pop("unit_id")
    return {**q, "currency": currency, "amount": tickets.round_local(q["usd"], rate["rate"], steps[currency], vat),
            "list_amount": tickets.round_local(q["list_usd"], rate["rate"], steps[currency], vat)}


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



def on_refunded(conn: sqlite3.Connection, order_id: int, now: int) -> None:
    """Inside the refund's transaction (refunds.py): the pass of this order was cancelled and its payment reversed, so
    the referral effects of the sale are undone. The buyer's unit used by it goes back to saved (or expired, if past
    expires_at). The referrer's reward for it is refused, and the unit it earned cancelled while still saved; a unit
    already held or used by an order of the referrer's stays with that order, and a `referral_unit_conflict` row
    names both for the admin."""
    o = conn.execute("SELECT ref_kind, ref_unit_id FROM orders WHERE id=?", (order_id,)).fetchone()
    if o is not None and o["ref_kind"] == "unit" and o["ref_unit_id"] is not None:
        u = conn.execute("SELECT id, user_id, expires_at FROM referral_units WHERE id=? AND state='used' AND order_id=?",
                         (o["ref_unit_id"], order_id)).fetchone()
        if u is not None:
            state = "expired" if u["expires_at"] <= now else "saved"
            conn.execute("UPDATE referral_units SET state=?, order_id=NULL, updated_at=? WHERE id=?", (state, now, u["id"]))
            db.audit(conn, None, f"referral_unit_{'expired' if state == 'expired' else 'returned'}",
                     db._user_name(conn, u["user_id"]), {"unit_id": u["id"], "order_id": order_id, "refunded": True})
    r = conn.execute("SELECT id, referrer_id, status FROM referral_rewards WHERE order_id=?", (order_id,)).fetchone()
    if r is None or r["status"] not in ("waiting", "earned"):
        return
    detail = {"reward_id": r["id"], "order_id": order_id}
    if r["status"] == "earned":
        unit = conn.execute("SELECT id, state, order_id FROM referral_units WHERE reward_id=?", (r["id"],)).fetchone()
        if unit is not None and unit["state"] == "saved":
            conn.execute("UPDATE referral_units SET state='cancelled', updated_at=? WHERE id=?", (now, unit["id"]))
            detail["unit_id"] = unit["id"]
        elif unit is not None and unit["state"] in ("held", "used"):
            db.audit(conn, None, "referral_unit_conflict", db._user_name(conn, r["referrer_id"]),
                     {"unit_id": unit["id"], "order_id": order_id, "other_order_id": unit["order_id"], "state": unit["state"]})
    conn.execute("UPDATE referral_rewards SET status='refused', decided_by=NULL, decided_at=? WHERE id=?", (now, r["id"]))
    db.audit(conn, None, "referral_refunded", db._user_name(conn, r["referrer_id"]), detail)

# ---------- earning units: a friend's first pass granted ----------

def _month_start(now: int) -> int:
    """The start of the calendar month `now` falls in, in Tehran time (design: most units earned per calendar month
    in Tehran time)."""
    from datetime import datetime
    t = datetime.fromtimestamp(now, tickets.TEHRAN)
    return int(t.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def _active_user(conn: sqlite3.Connection, user_id: int | None):
    """The user row when it still exists, enabled and not revoked; else None (a referrer who earns nothing)."""
    if user_id is None:
        return None
    return conn.execute("SELECT id FROM users WHERE id=? AND enabled=1 AND revoked_at IS NULL", (user_id,)).fetchone()


def on_granted(conn: sqlite3.Connection, order_id: int, now: int, floored_to_zero: bool = False) -> None:
    """Inside tickets.grant's transaction, right after orders.close_for_grant, for paid and manual grants alike: the
    order's held unit is used (spend, idempotent, so a paid grant's own spend changes nothing), and a friend's first
    pass records a referral_rewards row for the referrer - `waiting` for the admin with manual approval, else earned
    on the spot (earn). Nothing is recorded for an upgrade, with referrals off (no new units), when the referrer is
    gone, disabled or revoked, or when the friend has had another pass since ordering (no longer their first).
    `floored_to_zero`: an unpaid grant whose re-floored discount came to 0% (the share rose since the order): the pass
    sold at full price, so its unit is not used and goes back to saved, or expired (design: The cost floor). release()
    still spends it if a payment on the order verified."""
    if floored_to_zero:
        release(conn, order_id, now)
    else:
        spend(conn, order_id, now)
    o = conn.execute("SELECT user_id, ref_kind, upgrade_of, ticket_id FROM orders WHERE id=?", (order_id,)).fetchone()
    if o is None or o["ref_kind"] != "friend" or o["upgrade_of"] is not None or o["user_id"] is None:
        return
    s = settings(conn)
    if not s.enabled:
        return
    friend = conn.execute("SELECT referred_by, email, name FROM users WHERE id=?", (o["user_id"],)).fetchone()
    if friend is None or _active_user(conn, friend["referred_by"]) is None:
        return
    email = (friend["email"] or friend["name"] or "").lower()
    if conn.execute("SELECT 1 FROM tickets WHERE (user_id=? OR lower(user_name)=?) AND id IS NOT ? AND trial=0 LIMIT 1",
                    (o["user_id"], email, o["ticket_id"])).fetchone():
        return
    rid = conn.execute("INSERT INTO referral_rewards(referrer_id, friend_id, order_id, created_at, status) "
                       "VALUES(?,?,?,?,'waiting')", (friend["referred_by"], o["user_id"], order_id, now)).lastrowid
    if s.approval == "manual":
        db.audit(conn, None, "referral_waiting", db._user_name(conn, friend["referred_by"]),
                 {"reward_id": rid, "order_id": order_id, "friend_id": o["user_id"]})
        return
    earn(conn, rid, None, now)


def earn(conn: sqlite3.Connection, reward_id: int, actor: int | None, now: int) -> str:
    """Decide a reward in the caller's transaction: `over_limit` when the referrer already holds max_held saved plus
    held units, or has earned max_per_month units this Tehran calendar month; otherwise a saved unit of today's
    unit_pct, good for unit_days. Audited either way; returns the reward's new status. A referrer no longer active
    cannot be given a unit: ReferralError(409), and the admin refuses the reward instead."""
    r = conn.execute("SELECT * FROM referral_rewards WHERE id=?", (reward_id,)).fetchone()
    if r is None:
        raise ReferralError(404, f"Referral reward #{reward_id} does not exist.")
    if _active_user(conn, r["referrer_id"]) is None:
        raise ReferralError(409, "The referrer's account is gone, disabled or revoked; refuse this reward instead.")
    expire(conn, now)   # an overdue saved unit no longer counts as held
    s = settings(conn)
    referrer = r["referrer_id"]
    # A free trial's follow-up unit (source 'trial') was not earned by referring: it counts toward neither limit.
    held = conn.execute("SELECT COUNT(*) FROM referral_units WHERE user_id=? AND state IN ('saved','held') AND source IS NOT 'trial'",
                        (referrer,)).fetchone()[0]
    this_month = conn.execute("SELECT COUNT(*) FROM referral_units WHERE user_id=? AND earned_at>=? AND source IS NOT 'trial'",
                              (referrer, _month_start(now))).fetchone()[0]
    detail = {"reward_id": reward_id, "order_id": r["order_id"], "friend_id": r["friend_id"]}
    if held >= s.max_held or this_month >= s.max_per_month:
        status = "over_limit"
        db.audit(conn, actor, "referral_over_limit", db._user_name(conn, referrer),
                 detail | {"held": held, "this_month": this_month})
    else:
        status = "earned"
        uid = conn.execute("INSERT INTO referral_units(user_id, pct, earned_at, expires_at, state, reward_id, updated_at) "
                           "VALUES(?,?,?,?,'saved',?,?)",
                           (referrer, s.unit_pct, now, now + s.unit_days * 86400, reward_id, now)).lastrowid
        db.audit(conn, actor, "referral_unit_earned", db._user_name(conn, referrer),
                 detail | {"unit_id": uid, "pct": s.unit_pct})
    conn.execute("UPDATE referral_rewards SET status=?, decided_by=?, decided_at=? WHERE id=?",
                 (status, actor, now, reward_id))
    return status


def _decide(conn: sqlite3.Connection, reward_id: int, decide) -> str:
    """Run `decide()` on a `waiting` reward, in the caller's transaction or in a BEGIN IMMEDIATE of its own (an admin
    action from the Referrals tab). Anything else is ReferralError(409): a reward is decided once."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        r = conn.execute("SELECT status FROM referral_rewards WHERE id=?", (reward_id,)).fetchone()
        if r is None:
            raise ReferralError(404, f"Referral reward #{reward_id} does not exist.")
        if r["status"] != "waiting":
            raise ReferralError(409, f"This reward is already {r['status'].replace('_', ' ')}.")
        status = decide()
        if own:
            conn.execute("COMMIT")
        return status
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise


def approve(conn: sqlite3.Connection, actor: int | None, reward_id: int, now: int) -> str:
    """The admin approves a waiting reward (manual approval): earn() decides it, limits checked now. Works with
    referrals off too (design: rewards already waiting can still be approved)."""
    return _decide(conn, reward_id, lambda: earn(conn, reward_id, actor, now))


def refuse(conn: sqlite3.Connection, actor: int | None, reward_id: int, now: int) -> None:
    """The admin refuses a waiting reward: no unit, status `refused`, audited."""
    def decide():
        r = conn.execute("SELECT referrer_id, order_id, friend_id FROM referral_rewards WHERE id=?", (reward_id,)).fetchone()
        conn.execute("UPDATE referral_rewards SET status='refused', decided_by=?, decided_at=? WHERE id=?",
                     (actor, now, reward_id))
        db.audit(conn, actor, "referral_refused", db._user_name(conn, r["referrer_id"]),
                 {"reward_id": reward_id, "order_id": r["order_id"], "friend_id": r["friend_id"]})
        return "refused"
    _decide(conn, reward_id, decide)


# ---------- what the user sees: the invite card and /invite (task 6) ----------

def my_status(conn: sqlite3.Connection, cfg: Config, user_id: int, now: int) -> dict:
    """What the dashboard's "Invite friends" card and the Telegram bot's /invite show: the user's own link
    (cfg.listener.home_url if set, else dashboard_url, '/r/<code>'), the friend discount, their own saved and held
    units (pct, expiry, state - held meaning an open order of theirs is already using it), and how many of their
    referrals still wait on the admin's manual approval. With referrals off there is no link and no friend
    discount (no code is made), but the units and waiting count stay: saved units stay usable and waiting rewards
    can still be approved (design: Settings; final review #8). Expires overdue units first, so one about to lapse
    is never offered as still usable (controller ruling: my_status must call expire() first)."""
    expire(conn, now)
    s = settings(conn)
    units = [dict(r) for r in conn.execute(
        "SELECT pct, expires_at, state FROM referral_units WHERE user_id=? AND state IN ('saved','held') "
        "ORDER BY earned_at, id", (user_id,)).fetchall()]
    waiting = conn.execute("SELECT COUNT(*) FROM referral_rewards WHERE referrer_id=? AND status='waiting'",
                          (user_id,)).fetchone()[0]
    if not s.enabled:
        return {"enabled": False, "units": units, "waiting": waiting}
    code = code_for(conn, user_id)
    base = (cfg.listener.home_url or cfg.listener.dashboard_url).rstrip("/")
    return {"enabled": True, "link": f"{base}/r/{code}", "friend_pct": s.friend_pct, "units": units, "waiting": waiting}


# ---------- the admin's own actions, and the Referrals tab's one call ----------

def cancel_unit(conn: sqlite3.Connection, actor: int | None, unit_id: int, now: int) -> None:
    """The admin cancels one of a user's saved units, from the Referrers section. Only a `saved` unit may be
    cancelled: a unit already held by an open order stays with that order (design: a unit held by an open order
    stays with it even if the admin cancels it meanwhile), refused with ReferralError(409) instead of taking it
    away from a buyer who already saw that price.

    Locked (BEGIN IMMEDIATE, like _decide): the read and the write must not straddle hold() taking this very unit
    for a new order in between (orders.create's own transaction). The UPDATE's own rowcount, not the state the
    first SELECT saw, decides whether it actually cancelled a saved unit - closing that race rather than just
    narrowing it (fix round 1)."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        u = conn.execute("SELECT id, user_id, state FROM referral_units WHERE id=?", (unit_id,)).fetchone()
        if u is None:
            raise ReferralError(404, f"Referral unit #{unit_id} does not exist.")
        if u["state"] != "saved":
            raise ReferralError(409, f"Referral unit #{unit_id} is {u['state'].replace('_', ' ')}, not saved; it cannot be cancelled.")
        changed = conn.execute("UPDATE referral_units SET state='cancelled', updated_at=? WHERE id=? AND state='saved'",
                               (now, unit_id)).rowcount
        if changed != 1:   # taken by hold() between the SELECT above and this UPDATE
            raise ReferralError(409, f"Referral unit #{unit_id} is no longer saved; it cannot be cancelled.")
        db.audit(conn, actor, "referral_unit_cancelled", db._user_name(conn, u["user_id"]), {"unit_id": unit_id})
        if own:
            conn.execute("COMMIT")
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise


def admin_view(conn: sqlite3.Connection, now: int) -> dict:
    """Everything the Referrals tab shows in one call (design: Admin: the Referrals tab). Expires overdue saved
    units first, so every count below (and the cost floor, on the next quote) is current.

    - `waiting`: each reward still waiting on the admin (manual approval), with who referred whom, the pass, and
      the order's quoted price, for Approve / Refuse.
    - `referrers`: every user with a link code: friends signed up (`referred_by`) and friends who bought (a
      reward of any status - waiting counts as "bought", design: Referrers), then their saved units in full (so
      the tab can offer Cancel on each one) and how many are held, used and expired.
    - `history`: the last 200 `referral_*` audit rows, newest first, with the actor's current name."""
    expire(conn, now)
    waiting = [dict(r) for r in conn.execute(
        "SELECT rw.id AS reward_id, rw.referrer_id, ur.name AS referrer_name, rw.friend_id, uf.name AS friend_name, "
        "rw.order_id, o.tier, o.length, o.quoted_usd, o.currency, rw.created_at "
        "FROM referral_rewards rw JOIN orders o ON o.id = rw.order_id "
        "LEFT JOIN users ur ON ur.id = rw.referrer_id LEFT JOIN users uf ON uf.id = rw.friend_id "
        "WHERE rw.status='waiting' ORDER BY rw.id").fetchall()]
    referrers = []
    for u in conn.execute("SELECT id, name, ref_code FROM users WHERE ref_code IS NOT NULL ORDER BY name").fetchall():
        signed_up = conn.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (u["id"],)).fetchone()[0]
        bought = conn.execute("SELECT COUNT(*) FROM referral_rewards WHERE referrer_id=?", (u["id"],)).fetchone()[0]
        saved = [dict(r) for r in conn.execute(
            "SELECT id, pct, earned_at, expires_at FROM referral_units WHERE user_id=? AND state='saved' ORDER BY earned_at, id",
            (u["id"],)).fetchall()]
        counts = {"held": 0, "used": 0, "expired": 0}
        for r in conn.execute("SELECT state, COUNT(*) AS n FROM referral_units WHERE user_id=? AND state IN "
                              "('held','used','expired') GROUP BY state", (u["id"],)).fetchall():
            counts[r["state"]] = r["n"]
        referrers.append({"id": u["id"], "name": u["name"], "ref_code": u["ref_code"], "signed_up": signed_up,
                          "bought": bought, "saved": saved, **counts})
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM users")}
    history = [{**dict(r), "actor": names.get(r["actor_user_id"])} for r in conn.execute(
        "SELECT id, at, actor_user_id, action, target, detail_json FROM audit_log "
        "WHERE action LIKE 'referral\\_%' ESCAPE '\\' ORDER BY id DESC LIMIT 200").fetchall()]
    return {"settings": dataclasses.asdict(settings(conn)), "waiting": waiting, "referrers": referrers, "history": history}
