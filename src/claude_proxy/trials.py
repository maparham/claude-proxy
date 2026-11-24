"""Free trial pass (design 2026-11-14): a short pass a new user claims once, free, and starts with their first request
or the dashboard's Start now; the admin can grant one to anyone. A started trial is an ordinary tickets row with
trial=1 and price 0, so limits, capacity and the pass card treat it as any pass. The discount on a first purchase
after it is a saved referral unit, which the order code already applies.
"""
from __future__ import annotations

import dataclasses
import json
import math
import sqlite3
import time
from dataclasses import dataclass

from . import db, placement, plans, tickets
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
    tier: str | None = None           # None: the tier with the smallest size
    size: float | None = None         # in Claude Pro weeks; None: the tier's weekly size
    size_5h: float | None = None      # None: the tier's 5-hour size
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
    return min(tiers, key=lambda t: tickets.tier_size(conn, cfg, t)) if tiers else None


def tier_label(cfg: Config, tier: str | None) -> str | None:
    return cfg.tickets.tiers[tier].label if tier in cfg.tickets.tiers else tier


def settings(conn: sqlite3.Connection, cfg: Config) -> Settings:
    """The admin's settings with the tier and sizes filled in from the tier when left unset (or the tier is gone)."""
    s = _stored(conn)
    tier = s.tier if s.tier in cfg.tickets.tiers else default_tier(cfg, conn)
    size = s.size if s.size is not None else (tickets.tier_size(conn, cfg, tier) if tier else None)
    size_5h = s.size_5h if s.size_5h is not None else (tickets.tier_size_5h(conn, cfg, tier) if tier else None)
    return dataclasses.replace(s, tier=tier, size=size, size_5h=size_5h)


def shares(conn: sqlite3.Connection, s: Settings, account: str) -> tuple[float, float] | None:
    """The trial's (weekly, 5-hour) shares on `account`; None when a size or the account's plan is unknown."""
    return plans.shares(conn, account, s.size, s.size_5h) if s.size is not None else None


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
    if value is None and key in ("size", "size_5h"):
        return None   # back to the tier's own
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrialError(400, f"{key} must be a number.")
    if key in _WHOLE:
        lo, hi = _WHOLE[key]
        if value != int(value) or not lo <= value <= hi:
            raise TrialError(400, f"{key} must be a whole number from {lo} to {hi}.")
        return int(value)
    if key in ("size", "size_5h"):
        if not math.isfinite(value) or not 0 < value <= 1000:
            raise TrialError(400, f"{key} must be above 0 and at most 1000 Claude Pro weeks.")
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
    r = conn.execute("SELECT * FROM trial_claims WHERE user_id=? AND ticket_id IS NULL AND cancelled_at IS NULL AND expires_at>? "
                     "ORDER BY id DESC LIMIT 1",
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



def waiting_claims(conn: sqlite3.Connection, cfg: Config, now: float, user_id: int | None = None) -> list[dict]:
    """Claims still waiting to start, newest first, for the admin's pass list: the tier and share they would start with."""
    s = settings(conn, cfg)
    where, params = ("AND c.user_id=?", (int(now), user_id)) if user_id is not None else ("", (int(now),))
    rows = conn.execute("SELECT c.id, c.user_id, c.email, c.claimed_at, c.expires_at, c.granted_by, u.name AS user_name FROM trial_claims c "
                        "LEFT JOIN users u ON u.id=c.user_id WHERE c.user_id IS NOT NULL AND c.ticket_id IS NULL AND c.cancelled_at IS NULL AND c.expires_at>? "
                        f"{where} ORDER BY c.id DESC", params).fetchall()
    best = placement.best_account(conn, cfg, now)   # where it would most likely start
    sh = shares(conn, s, best) if best else None
    return [dict(r) | {"user_name": r["user_name"] or r["email"], "tier": s.tier, "label": tier_label(cfg, s.tier), "share_pct": sh[0] if sh else None,
                       "duration_min": s.duration_min} for r in rows]


def cancel_claim(conn: sqlite3.Connection, actor: int, claim_id: int, now: float) -> dict:
    """The admin's cancel of a claim still waiting to start. A started trial is a pass, cancelled as any pass is."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        r = conn.execute("SELECT c.*, u.name AS user_name FROM trial_claims c LEFT JOIN users u ON u.id=c.user_id WHERE c.id=?",
                         (claim_id,)).fetchone()
        if r is None:
            raise TrialError(404, "No such trial.")
        if r["cancelled_at"] is not None:
            raise TrialError(409, "This trial is already cancelled.")
        if r["ticket_id"] is not None:
            raise TrialError(409, "This trial has started: cancel its pass instead.")
        if r["expires_at"] <= now:
            raise TrialError(409, "This trial has already lapsed.")
        conn.execute("UPDATE trial_claims SET cancelled_at=?, cancelled_by=? WHERE id=?", (int(now), actor, claim_id))
        db.audit(conn, actor, "trial_claim_cancel", r["user_name"] or r["email"], {"claim_id": claim_id})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return _claim_row(conn, claim_id)


# ---------- starting ----------

def _in_the_way(conn, cfg, user_id: int, now: float) -> tuple[bool, list[dict]]:
    """Whether a running pass holds a waiting claim back, and the running passes the trial replaces instead: used-up
    ones (limits.used_up) end as it starts, as a granted pass replaces them (tickets.next_start)."""
    from . import limits   # here, not at the top: limits imports this module
    run = tickets.running(conn, user_id, now)
    if run and limits.used_up(conn, cfg, user_id, now, run):
        return False, run
    return bool(run), []


def _start_locked(conn, cfg, user_id: int, now: int, auto: bool = False) -> dict | None:
    """`auto`: a request's start, which finds the claim gone or a pass running under the lock (another request of the
    user's started it meanwhile) and returns None rather than refuse."""
    c = open_claim(conn, user_id, now)
    held, replaces = _in_the_way(conn, cfg, user_id, now)
    if auto and (c is None or held):
        return None
    if c is None:
        raise TrialError(409, "You have no free trial waiting to start.")
    s = settings(conn, cfg)
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user["revoked_at"] is not None or not user["enabled"]:
        raise TrialError(409, "This account is disabled.")
    end = now + s.duration_min * 60
    # Placed as any sold pass is: where it fits with the most free Pro weeks.
    account, rows = placement.pick(conn, cfg, s.size, s.size_5h, now, end, now, [t["id"] for t in replaces]) \
        if s.size is not None else (None, [])
    if account is None:
        raise TrialError(409, NO_ROOM)
    row = next(r for r in rows if r["account"] == account)
    sh = (row["share_pct"], row["share_5h_pct"])
    for t in replaces:   # the user's passes are used up: they end now and the trial starts in their place
        tickets.end_used_up(conn, t, now)
        db.audit(conn, None, "ticket_used_up", user["name"], {"ticket_id": t["id"]})
    first = conn.execute("SELECT 1 FROM tickets WHERE user_id=? LIMIT 1", (user_id,)).fetchone() is None
    tid = conn.execute(
        "INSERT INTO tickets(user_id, user_name, account, tier, share_pct, share_5h_pct, size, size_5h, length, days, starts_at, ends_at, "
        "list_usd, usd, currency, rate, amount, granted_by, granted_at, note, trial) VALUES(?,?,?,?,?,?,?,?,'day',0,?,?,0,0,'USD',1,0,?,?,?,1)",
        (user_id, user["name"], account, s.tier, *sh, s.size, s.size_5h if s.size_5h is not None else s.size, now, end, c["granted_by"], now,
         "Free trial")).lastrowid
    conn.execute("UPDATE trial_claims SET ticket_id=?, started_at=? WHERE id=?", (tid, now, c["id"]))
    if first and conn.execute("DELETE FROM limits WHERE user_id=? AND kind='cost_total'", (user_id,)).rowcount:
        db.audit(conn, None, "credit_removed", user["name"], {"ticket_id": tid})
    unit = None
    if s.follow_pct > 0:
        unit = conn.execute("INSERT INTO referral_units(user_id, pct, earned_at, expires_at, state, updated_at, source) VALUES(?,?,?,?,'saved',?,'trial')",
                            (user_id, s.follow_pct, now, end + s.follow_days * DAY, now)).lastrowid
    db.audit(conn, None, "trial_start", user["name"], {"claim_id": c["id"], "ticket_id": tid, "ends_at": end, "share_pct": sh[0],
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
    """On a metered request: a waiting claim starts when the user has no pass running, or only used-up ones, which
    it replaces. None: nothing to do or started;
    else the refusal (no room now), remembered for NO_ROOM_RETRY_S so a full gateway isn't rechecked on every request.
    One indexed query when there is no claim, as for nearly every request."""
    if open_claim(conn, user_id, now) is None or _in_the_way(conn, cfg, user_id, now)[0]:
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


# ---------- the claim's email (design 2026-11-17) ----------

def _length(minutes: int, lang: str) -> str:
    """As the dashboard's trialLen: whole hours as hours, else minutes."""
    n, unit = (minutes // 60, ("hour", "ساعت")) if minutes % 60 == 0 else (minutes, ("minute", "دقیقه"))
    if lang == "fa":
        return f"{tickets.fa_num(n)} {unit[1]}"
    return f"{n} {unit[0]}{'' if n == 1 else 's'}"


def claim_mail_text(conn: sqlite3.Connection, cfg: Config, claim: dict, lang: str) -> tuple[str, str]:
    """(subject, body) for the user once a trial is claimed or granted to them: its length and tier, how it starts,
    the date it must start by, the discount on a first purchase after it, and the setup guide link every pass mail has."""
    from . import orders   # here, not at the top: orders imports referrals, which imports tickets, as this module does
    s = settings(conn, cfg)
    dash = cfg.listener.dashboard_url.rstrip("/")
    setup = orders.setup_guide(cfg, {"lang": lang})
    if lang == "fa":
        tier = orders.fa_label(cfg, s.tier)
        lines = [f"آزمایش رایگان شما آماده است: {_length(s.duration_min, 'fa')} با بسته {tier}، بدون پرداخت.", "",
                 "با اولین درخواست شما در کلاد شروع می‌شود" + (f"، یا وقتی در داشبورد ({dash}) «شروع از الان» را بزنید." if dash else "."),
                 f"تا {tickets.terms_date(claim['expires_at'], 'fa')} شروعش کنید، وگرنه از دست می‌رود."]
        if s.follow_pct > 0:
            lines.append(f"پس از آن، تا {tickets.fa_num(s.follow_days)} روز {tickets.fa_num(f'{s.follow_pct:g}')}٪ تخفیف روی اولین خرید خود دارید.")
        return f"{_length(s.duration_min, 'fa')} کلاد رایگان", "\n".join(lines) + "\n" + setup
    tier = tier_label(cfg, s.tier)
    lines = [f"Your free trial is ready: {_length(s.duration_min, 'en')} on a {tier} pass, no payment.", "",
             "It starts with your first request in Claude Code" + (f", or when you press Start now on the dashboard ({dash})." if dash else "."),
             f"Start it by {tickets.terms_date(claim['expires_at'], 'en')}, or it lapses."]
    if s.follow_pct > 0:
        lines.append(f"After it, your first purchase gets {s.follow_pct:g}% off for {s.follow_days} days.")
    return "Your free trial is ready", "\n".join(lines) + "\n" + setup


def _mail_to(user) -> str | None:
    from . import orders
    to = (user["email"] or user["name"] or "").strip()
    return to if orders.EMAIL.match(to) else None


async def send_claim_mail(conn: sqlite3.Connection, cfg: Config, claim_id: int, lang: str | None) -> None:
    """After the claim's response: tell the user their trial is ready. `lang` None (an admin grant): the language of
    their latest order, else English. No address or no [email]: nothing. A failure is logged and never touches the claim."""
    import asyncio
    import logging
    from . import mail
    if cfg.email is None:
        return
    c = _claim_row(conn, claim_id)
    user = conn.execute("SELECT * FROM users WHERE id=?", (c["user_id"],)).fetchone() if c["user_id"] else None
    to = _mail_to(user) if user else None
    if to is None:
        return
    if lang is None:
        row = conn.execute("SELECT lang FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
        lang = row[0] if row else "en"
    subject, body = claim_mail_text(conn, cfg, c, "fa" if lang == "fa" else "en")
    try:
        await asyncio.to_thread(mail.send, mail.servers(conn, cfg.email), to, subject, body)
    except Exception as e:
        logging.getLogger("claude_proxy").warning("trial claim #%d: mail to %s failed: %s", claim_id, to, e)


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
                          "SUM(CASE WHEN ticket_id IS NULL AND cancelled_at IS NULL AND expires_at<=? THEN 1 ELSE 0 END) AS lapsed FROM trial_claims",
                          (int(now),)).fetchone()
    bought = conn.execute("SELECT COUNT(DISTINCT c.user_id) FROM trial_claims c JOIN tickets t ON t.user_id=c.user_id "
                          "WHERE c.ticket_id IS NOT NULL AND t.trial=0 AND t.granted_at>=c.started_at").fetchone()[0]
    return {"settings": dataclasses.asdict(s), "tier_default": {"size": stored.size is None, "size_5h": stored.size_5h is None,
                                                                "tier": stored.tier is None},
            "today": claims_today(conn, now), "claimed": counts["claimed"], "started": counts["started"],
            "lapsed": counts["lapsed"] or 0, "bought": bought}
