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
