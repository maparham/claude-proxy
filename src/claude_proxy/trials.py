"""Free trial pass (design 2026-11-14): a short pass a new user claims once, free, and starts with their first request
or the dashboard's Start now; the admin can grant one to anyone. A started trial is an ordinary tickets row with
trial=1 and price 0, so limits, capacity and the pass card treat it as any pass. The discount on a first purchase
after it is a saved referral unit, which the order code already applies.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from dataclasses import dataclass

from . import db, tickets
from .config import Config

SETTINGS_KEY = "trial"
NO_ROOM = "The free trial can't start right now; try again later."
DAY = 86400


class TrialError(Exception):
    """A refused trial operation, with the HTTP status it maps to."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Settings:
    enabled: bool = False
    duration_min: int = 60
    valid_hours: int = 24
    tier: str | None = None           # None: the tier with the smallest share
    share_pct: float | None = None    # None: the tier's weekly share
    share_5h_pct: float | None = None  # None: the tier's 5-hour share
    daily_cap: int = 5
    follow_pct: float = 10.0
    follow_days: int = 7


_FIELDS = {f.name for f in dataclasses.fields(Settings)}
_WHOLE = {"duration_min": (1, 10080), "valid_hours": (1, 720), "daily_cap": (0, 1000), "follow_days": (1, 90)}


def _stored(conn) -> Settings:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SETTINGS_KEY,)).fetchone()
    stored = json.loads(row[0]) if row else {}
    return Settings(**{k: v for k, v in stored.items() if k in _FIELDS})


def default_tier(cfg: Config, conn) -> str | None:
    tiers = list(cfg.tickets.tiers)
    return min(tiers, key=lambda t: tickets.effective_share(conn, cfg, t)) if tiers else None


def tier_label(cfg: Config, tier: str | None) -> str | None:
    return cfg.tickets.tiers[tier].label if tier in cfg.tickets.tiers else tier


def settings(conn: sqlite3.Connection, cfg: Config) -> Settings:
    """The admin's settings with the tier and shares filled in from the tier when left unset (or the tier is gone)."""
    s = _stored(conn)
    tier = s.tier if s.tier in cfg.tickets.tiers else default_tier(cfg, conn)
    share = s.share_pct if s.share_pct is not None else (tickets.effective_share(conn, cfg, tier) if tier else None)
    share_5h = s.share_5h_pct if s.share_5h_pct is not None else (tickets.effective_share_5h(conn, cfg, tier) if tier else None)
    return dataclasses.replace(s, tier=tier, share_pct=share, share_5h_pct=share_5h)


def _validate(cfg: Config, key: str, value):
    if key not in _FIELDS:
        raise TrialError(400, f"Unknown trial setting {key!r}.")
    if key == "enabled":
        if not isinstance(value, bool):
            raise TrialError(400, "enabled must be true or false.")
        return value
    if key == "tier":
        if value is not None and value not in cfg.tickets.tiers:
            raise TrialError(400, f"Unknown tier {value!r}.")
        return value
    if value is None and key in ("share_pct", "share_5h_pct"):
        return None   # back to the tier's own
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrialError(400, f"{key} must be a number.")
    if key in _WHOLE:
        lo, hi = _WHOLE[key]
        if value != int(value) or not lo <= value <= hi:
            raise TrialError(400, f"{key} must be a whole number from {lo} to {hi}.")
        return int(value)
    if key in ("share_pct", "share_5h_pct"):
        if not 0 < value <= 100:
            raise TrialError(400, f"{key} must be above 0 and at most 100.")
        return float(value)
    if key == "follow_pct":
        if not 0 <= value <= 90:
            raise TrialError(400, "follow_pct must be from 0 to 90.")
        return float(value)
    raise TrialError(400, f"Unknown trial setting {key!r}.")   # pragma: no cover


def set_settings(conn: sqlite3.Connection, cfg: Config, actor: int | None, changes: dict) -> Settings:
    """Validate every change before writing any, store the merged settings and audit the changed keys."""
    if not isinstance(changes, dict):
        raise TrialError(400, "Expected an object of settings.")
    validated = {k: _validate(cfg, k, v) for k, v in changes.items()}
    merged = dataclasses.replace(_stored(conn), **validated)
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (SETTINGS_KEY, json.dumps(dataclasses.asdict(merged))))
    db.audit(conn, actor, "trial_settings", "trial", validated)
    return settings(conn, cfg)


# ---------- claims ----------

def _email(user) -> str:
    return (user["email"] or user["name"] or "").lower()


def _claim_row(conn, claim_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM trial_claims WHERE id=?", (claim_id,)).fetchone())


def open_claim(conn: sqlite3.Connection, user_id: int, now: float) -> dict | None:
    """The user's claim still waiting to start, or None."""
    r = conn.execute("SELECT * FROM trial_claims WHERE user_id=? AND ticket_id IS NULL AND expires_at>? ORDER BY id DESC LIMIT 1",
                     (user_id, int(now))).fetchone()
    return dict(r) if r else None


def had_trial(conn, user) -> bool:
    """Any claim ever, by this account or its email (kept after the account is deleted)."""
    return conn.execute("SELECT 1 FROM trial_claims WHERE user_id=? OR email=? LIMIT 1", (user["id"], _email(user))).fetchone() is not None


def had_pass(conn, user) -> bool:
    from . import referrals   # here, not at the top: referrals imports tickets, as this module does
    return not referrals.first_pass(conn, user["id"])


def claims_today(conn, now: float) -> int:
    """Self-service claims in the last 24 hours: what daily_cap counts."""
    return conn.execute("SELECT COUNT(*) FROM trial_claims WHERE granted_by IS NULL AND claimed_at>?", (int(now) - DAY,)).fetchone()[0]


def _refusal(conn, cfg, user, now) -> str | None:
    """Why this user can't claim a trial themselves now, or None."""
    s = settings(conn, cfg)
    if not (cfg.tickets.enabled and s.enabled and s.tier):
        return "off"
    if user["revoked_at"] is not None or not user["enabled"]:
        return "disabled"
    if had_trial(conn, user):
        return "used"
    if had_pass(conn, user):
        return "had_pass"
    if claims_today(conn, now) >= s.daily_cap:
        return "cap"
    return None


REFUSALS = {"off": "The free trial is not offered right now.", "disabled": "This account is disabled.",
            "used": "You have already had your free trial.", "had_pass": "The free trial is for new users only.",
            "cap": "Today's free trials are all taken; try again tomorrow."}


def offer(conn: sqlite3.Connection, cfg: Config, user, now: float) -> dict:
    """What the user's dashboard shows: whether they may claim, their waiting claim, and the trial's length."""
    s = settings(conn, cfg)
    c = open_claim(conn, user["id"], now)
    reason = _refusal(conn, cfg, user, now)
    return {"can_claim": reason is None, "reason": reason, "duration_min": s.duration_min, "tier": s.tier, "label": tier_label(cfg, s.tier),
            "claim": {"claimed_at": c["claimed_at"], "expires_at": c["expires_at"]} if c else None}


def _insert_claim(conn, cfg, user, actor, now) -> dict:
    s = settings(conn, cfg)
    cid = conn.execute("INSERT INTO trial_claims(user_id, email, claimed_at, expires_at, granted_by) VALUES(?,?,?,?,?)",
                       (user["id"], _email(user), int(now), int(now) + s.valid_hours * 3600, actor)).lastrowid
    db.audit(conn, actor if actor is not None else user["id"], "trial_claim", user["name"],
             {"claim_id": cid, "by_admin": actor is not None, "expires_at": int(now) + s.valid_hours * 3600})
    return _claim_row(conn, cid)


def claim(conn: sqlite3.Connection, cfg: Config, user, now: float) -> dict:
    """The user's own claim. All checks and the insert in one BEGIN IMMEDIATE, so two clicks make one claim."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        reason = _refusal(conn, cfg, user, now)
        if reason is not None:
            raise TrialError(409, REFUSALS[reason])
        c = _insert_claim(conn, cfg, user, None, now)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return c


def admin_grant(conn: sqlite3.Connection, cfg: Config, actor: int, user, now: float, confirm: bool = False) -> dict:
    """The admin's grant to any user, whatever the settings say about offering it. A user who already had a trial or a
    pass needs `confirm`; one with a claim still waiting gets no second one. Not counted against daily_cap."""
    if not cfg.tickets.enabled:
        raise TrialError(409, "Tickets are not enabled: add a [tickets] section to the config.")
    if user["revoked_at"] is not None or not user["enabled"]:
        raise TrialError(409, f"{user['name']} is revoked or disabled.")
    if settings(conn, cfg).tier is None:
        raise TrialError(409, "There is no tier to file a trial under.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if open_claim(conn, user["id"], now):
            raise TrialError(409, f"{user['name']} already has a trial waiting to start.")
        if not confirm and (had_trial(conn, user) or had_pass(conn, user)):
            raise TrialError(409, f"{user['name']} already had a {'trial' if had_trial(conn, user) else 'pass'}; confirm to grant anyway.")
        c = _insert_claim(conn, cfg, user, actor, now)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return c


# ---------- starting ----------

def _start_locked(conn, cfg, user_id: int, now: int, auto: bool = False) -> dict | None:
    """`auto`: a request's start, which finds the claim gone or a pass running under the lock (another request of the
    user's started it meanwhile) and returns None rather than refuse."""
    from . import capacity   # here, not at the top: capacity imports tickets
    c = open_claim(conn, user_id, now)
    if auto and (c is None or tickets.running(conn, user_id, now)):
        return None
    if c is None:
        raise TrialError(409, "You have no free trial waiting to start.")
    s = settings(conn, cfg)
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user["revoked_at"] is not None or not user["enabled"]:
        raise TrialError(409, "This account is disabled.")
    end = now + s.duration_min * 60
    try:
        capacity.check(conn, cfg, tickets.ACCOUNT_ID, s.share_pct, s.share_5h_pct, now, end, now)
    except tickets.CapacityError as e:
        raise TrialError(409, NO_ROOM) from e
    first = conn.execute("SELECT 1 FROM tickets WHERE user_id=? LIMIT 1", (user_id,)).fetchone() is None
    tid = conn.execute(
        "INSERT INTO tickets(user_id, user_name, account_id, tier, share_pct, share_5h_pct, length, days, starts_at, ends_at, list_usd, "
        "usd, currency, rate, amount, granted_by, granted_at, note, trial) VALUES(?,?,?,?,?,?,'day',0,?,?,0,0,'USD',1,0,?,?,?,1)",
        (user_id, user["name"], tickets.ACCOUNT_ID, s.tier, s.share_pct, s.share_5h_pct, now, end, c["granted_by"], now,
         "Free trial")).lastrowid
    conn.execute("UPDATE trial_claims SET ticket_id=?, started_at=? WHERE id=?", (tid, now, c["id"]))
    if first and conn.execute("DELETE FROM limits WHERE user_id=? AND kind='cost_total'", (user_id,)).rowcount:
        db.audit(conn, None, "credit_removed", user["name"], {"ticket_id": tid})
    unit = None
    if s.follow_pct > 0:
        unit = conn.execute("INSERT INTO referral_units(user_id, pct, earned_at, expires_at, state, updated_at, source) VALUES(?,?,?,?,'saved',?,'trial')",
                            (user_id, s.follow_pct, now, end + s.follow_days * DAY, now)).lastrowid
    db.audit(conn, None, "trial_start", user["name"], {"claim_id": c["id"], "ticket_id": tid, "ends_at": end, "share_pct": s.share_pct,
                                                       **({"unit_id": unit} if unit else {})})
    return tickets.get(conn, tid)


def start(conn: sqlite3.Connection, cfg: Config, user_id: int, now: float, auto: bool = False) -> dict | None:
    """Turn the user's waiting claim into a running trial pass; TrialError when there is none or no room."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = _start_locked(conn, cfg, user_id, int(now), auto)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return t


_NO_ROOM: dict[int, float] = {}   # user id -> monotonic time until which a start found no room: not tried again before
NO_ROOM_RETRY_S = 60


def autostart(conn: sqlite3.Connection, cfg: Config, user_id: int, now: float) -> str | None:
    """On a metered request: a waiting claim starts when the user has no pass running. None: nothing to do or started;
    else the refusal (no room now), remembered for NO_ROOM_RETRY_S so a full gateway isn't rechecked on every request.
    One indexed query when there is no claim, as for nearly every request."""
    if open_claim(conn, user_id, now) is None or tickets.running(conn, user_id, now):
        return None
    if _NO_ROOM.get(user_id, 0) > time.monotonic():
        return NO_ROOM
    try:
        start(conn, cfg, user_id, now, auto=True)
    except TrialError as e:
        _NO_ROOM[user_id] = time.monotonic() + NO_ROOM_RETRY_S
        return e.message
    _NO_ROOM.pop(user_id, None)
    return None


# ---------- what the pages show ----------

def public_offer(conn: sqlite3.Connection, cfg: Config, now: float) -> dict | None:
    """The home page's: the trial's length while it is offered and today's claims aren't used up."""
    s = settings(conn, cfg)
    if not (cfg.tickets.enabled and s.enabled and s.tier) or claims_today(conn, now) >= s.daily_cap:
        return None
    return {"duration_min": s.duration_min, "tier": s.tier, "label": tier_label(cfg, s.tier)}


def admin_view(conn: sqlite3.Connection, cfg: Config, now: float) -> dict:
    s = settings(conn, cfg)
    stored = _stored(conn)
    counts = conn.execute("SELECT COUNT(*) AS claimed, COUNT(ticket_id) AS started, "
                          "SUM(CASE WHEN ticket_id IS NULL AND expires_at<=? THEN 1 ELSE 0 END) AS lapsed FROM trial_claims",
                          (int(now),)).fetchone()
    bought = conn.execute("SELECT COUNT(DISTINCT c.user_id) FROM trial_claims c JOIN tickets t ON t.user_id=c.user_id "
                          "WHERE c.ticket_id IS NOT NULL AND t.trial=0 AND t.granted_at>=c.started_at").fetchone()[0]
    return {"settings": dataclasses.asdict(s), "tier_default": {"share_pct": stored.share_pct is None, "share_5h_pct": stored.share_5h_pct is None,
                                                                "tier": stored.tier is None},
            "today": claims_today(conn, now), "claimed": counts["claimed"], "started": counts["started"],
            "lapsed": counts["lapsed"] or 0, "bought": bought}
