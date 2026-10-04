"""Order requests (design 2026-10-04): a buyer asks for a ticket, the admin turns the request into one.

An order is a request, not a payment and not a reservation: it holds no capacity, and the grant re-checks and re-prices
as usual. Creation, the limits (section 4), the status changes (section 3), linking a visitor order to an account
(section 7), the buyer's view (section 8) and retention. Writes that check before they change run in one
BEGIN IMMEDIATE transaction, so two requests at once cannot both pass a limit.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time

from . import db, mail, tickets
from .config import LENGTHS, Config

logger = logging.getLogger("claude_proxy")

DAY = 86400
NAME_MAX = 100
MESSAGE_MAX = 1000
NOTE_MAX = 200
EMAIL_MAX = 254
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VISITOR_PER_IP = VISITOR_PER_EMAIL = 3   # per 24 hours
VISITOR_GLOBAL = 50                      # all visitor orders together, per 24 hours
BUYER_MAILS = 3                          # confirmations to one address per 24 hours
IP_KEEP_S = 30 * DAY

STATUSES = ("new", "contacted", "done", "declined", "withdrawn")
OPEN = ("new", "contacted")
MAIL_STATES = ("pending", "sent", "failed", "off", "skipped")
# Status changes other than `done`, which only a grant makes (close_for_grant). Anything else is refused.
TRANSITIONS = {"contacted": ("new",), "declined": OPEN, "withdrawn": OPEN}

TOO_MANY = "Too many orders today; try again tomorrow or sign in."
BUSY = "Orders are busy today; please sign in to order."
CHANGED = "This order was withdrawn or changed; reload."
# A visitor order: placed without an account (user_id NULL until linked) or still carrying its IP (cleared on close).
# One the admin has both linked and closed drops out of the visitor limits, which only loosens them for a handled buyer.
VISITOR = "(user_id IS NULL OR ip IS NOT NULL)"


class OrderError(Exception):
    """A refused order operation, with the HTTP status it maps to (400, 404, 409 or 429) and any extra JSON fields."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _now(now) -> int:
    return int(time.time() if now is None else now)


def _one_line(s) -> str:
    """No CR or LF: name and email may end up in mail headers."""
    return str(s or "").replace("\r", "").replace("\n", "").strip()


def _email(email) -> str:
    email = _one_line(email)
    if len(email) > EMAIL_MAX or not EMAIL.match(email):
        raise OrderError(400, "That email address does not look right.")
    return email


def _note(note) -> str:
    note = str(note or "").strip()
    if len(note) > NOTE_MAX:
        raise OrderError(400, f"A note is at most {NOTE_MAX} characters.")
    return note


def _count(conn, where: str, args: tuple) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM orders WHERE {where}", args).fetchone()[0]


def get(conn: sqlite3.Connection, order_id: int) -> dict:
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if row is None:
        raise OrderError(404, f"No order #{order_id}.")
    return dict(row)


# ---------- placing an order ----------

def create(conn: sqlite3.Connection, cfg: Config, *, tier: str, length: str, currency: str, name: str, email: str, message: str = "",
           user_id: int | None = None, ip: str | None = None, now: float | None = None) -> dict:
    """Store an order for a signed-in user (`user_id`) or a visitor (`ip`), quoted at today's price and rate.
    Refused: bad input (400), a user's second open order or a sold-out ticket (409), a visitor over a limit (429)."""
    now = _now(now)
    if tier not in cfg.tickets.tiers:
        raise OrderError(400, f"Unknown tier {tier!r}.")
    if length not in LENGTHS:
        raise OrderError(400, f"Unknown length {length!r}; one of {', '.join(LENGTHS)}.")
    steps = tickets.currencies(cfg)
    if currency not in steps:
        raise OrderError(400, f"Unknown currency {currency!r}.")
    name, email, message = _one_line(name), _email(email), str(message or "").strip()
    if not name:
        raise OrderError(400, "Please give your name.")
    if len(name) > NAME_MAX:
        raise OrderError(400, f"A name is at most {NAME_MAX} characters.")
    if len(message) > MESSAGE_MAX:
        raise OrderError(400, f"A message is at most {MESSAGE_MAX} characters.")
    since = now - DAY
    conn.execute("BEGIN IMMEDIATE")
    try:
        if user_id is not None:
            if _count(conn, f"user_id=? AND status IN {OPEN}", (user_id,)):
                raise OrderError(409, "You already have an open order.")
        else:
            # Limit messages carry no numbers (spec section 9).
            if _count(conn, f"{VISITOR} AND ip=? AND created_at>?", (ip, since)) >= VISITOR_PER_IP \
                    or _count(conn, f"{VISITOR} AND lower(email)=lower(?) AND created_at>?", (email, since)) >= VISITOR_PER_EMAIL:
                raise OrderError(429, TOO_MANY)
            if _count(conn, f"{VISITOR} AND created_at>?", (since,)) >= VISITOR_GLOBAL:
                raise OrderError(429, BUSY)
        # Judged as the price list judges it: for a ticket starting now (spec section 2, Sold out).
        if tickets.sold_out(conn, cfg, tier, length, now):
            raise OrderError(409, "That ticket is sold out.")
        rate = tickets.current_rate(conn, currency)
        if rate is None:
            raise OrderError(400, f"No exchange rate for {currency} yet; choose another currency.")
        usd = tickets.price_now(conn, tier, length, now)["usd"]
        amount = tickets.round_local(usd, rate["rate"], steps[currency])
        if cfg.email is None:
            admin_mail = buyer_mail = "off"
        else:
            # The cap exists for signed-in users, who have no per-email order limit (spec section 5, Buyer cap).
            sent = _count(conn, "lower(email)=lower(?) AND buyer_mail IN ('sent','pending') AND created_at>?", (email, since))
            admin_mail, buyer_mail = "pending", "skipped" if sent >= BUYER_MAILS else "pending"
        cur = conn.execute(
            "INSERT INTO orders(created_at, updated_at, user_id, name, email, tier, length, currency, quoted_usd, quoted_rate, quoted_amount, "
            "message, status, ip, admin_mail, buyer_mail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?,?)",
            (now, now, user_id, name, email, tier, length, currency, usd, rate["rate"], amount, message,
             None if user_id is not None else ip, admin_mail, buyer_mail))
        oid = cur.lastrowid
        db.audit(conn, user_id, "order_new", f"order #{oid}", {"tier": tier, "length": length, "currency": currency, "amount": amount,
                                                              "visitor": user_id is None})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get(conn, oid)


# ---------- status changes (spec section 3) ----------

def _change(conn, actor, order_id: int, to: str, note, now: int) -> dict:
    """Inside a transaction: one allowed status change, the note for a decline, the IP cleared on close, the audit row."""
    o = get(conn, order_id)
    if o["status"] not in TRANSITIONS.get(to, ()):
        raise OrderError(409, f"An order that is {o['status']} cannot become {to}.")
    sets, args = ["status=?", "updated_at=?"], [to, now]
    if to == "declined":
        note = _note(note)
        if not note:
            raise OrderError(400, "A decline needs a note.")
        sets.append("admin_note=?")
        args.append(note)
    if to not in OPEN:
        sets.append("ip=NULL")
    conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", (*args, order_id))
    db.audit(conn, actor, f"order_{to}", f"order #{order_id}", {"from": o["status"]})
    return get(conn, order_id)


def set_status(conn: sqlite3.Connection, actor: int | None, order_id: int, to: str, *, note=None, now: float | None = None) -> dict:
    """`contacted` (from new), `declined` with a note or `withdrawn` (from new or contacted); anything else 409."""
    now = _now(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        o = _change(conn, actor, order_id, to, note, now)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return o


def set_note(conn: sqlite3.Connection, actor: int | None, order_id: int, note, now: float | None = None) -> dict:
    """The admin's note, in any status. Never shown to the buyer."""
    note = _note(note)
    get(conn, order_id)
    conn.execute("UPDATE orders SET admin_note=?, updated_at=? WHERE id=?", (note, _now(now), order_id))
    db.audit(conn, actor, "order_note", f"order #{order_id}", {"note": note})
    return get(conn, order_id)


def close_for_grant(conn: sqlite3.Connection, order_id: int, user_id: int, ticket_id: int, now: float | None = None,
                    actor: int | None = None) -> None:
    """Inside the grant's transaction: the order must still be open and linked to the user granted to, or the grant
    is refused, so a withdraw racing the open grant dialog never leaves a `done` order with no buyer."""
    row = conn.execute("SELECT status, user_id FROM orders WHERE id=?", (order_id,)).fetchone()
    if row is None or row["status"] not in OPEN or row["user_id"] != user_id:
        raise OrderError(409, CHANGED)
    conn.execute("UPDATE orders SET status='done', ticket_id=?, ip=NULL, updated_at=? WHERE id=?", (ticket_id, _now(now), order_id))
    db.audit(conn, actor, "order_done", f"order #{order_id}", {"from": row["status"], "ticket_id": ticket_id})


def set_mail(conn: sqlite3.Connection, order_id: int, which: str, state: str) -> None:
    if which not in ("admin_mail", "buyer_mail") or state not in MAIL_STATES:
        raise ValueError(f"bad mail state {which}={state}")
    conn.execute(f"UPDATE orders SET {which}=? WHERE id=?", (state, order_id))


# ---------- emails (spec section 5) ----------

def _label(cfg: Config, order: dict) -> str:
    t = cfg.tickets.tiers.get(order["tier"])
    return t.label if t else order["tier"]


def _amount(order: dict) -> str:
    return f"{order['quoted_amount']:,.2f} {order['currency']}"


def admin_mail_text(cfg: Config, order: dict) -> tuple[str, str]:
    """(subject, body) for the admin, the only recipient of what the buyer typed. `order` may carry `user_name`."""
    label = _label(cfg, order)
    lines = [f"New order #{order['id']}: {label}, {order['length']} ({LENGTHS.get(order['length'], '?')} days)", "",
             f"Name: {order['name']}", f"Email: {order['email']}", f"Account: {order.get('user_name') or 'none'}",
             f"Quoted: {_amount(order)} (${order['quoted_usd']:g} at {order['quoted_rate']:g})", "",
             "Message:", order["message"] or "(none)"]
    if cfg.listener.dashboard_url:
        lines += ["", f"Orders: {cfg.listener.dashboard_url.rstrip('/')}/dashboard#orders"]
    return f"New order: {label} {order['length']}", "\n".join(lines) + "\n"


def buyer_mail_text(cfg: Config, order: dict) -> tuple[str, str]:
    """(subject, body) for the buyer. Nothing the buyer typed goes in (no name, no message), so the form cannot carry
    content to a stranger's inbox."""
    label = _label(cfg, order)
    lines = [f"Your order: a {label} ticket for one {order['length']} ({LENGTHS.get(order['length'], '?')} days).",
             f"Price at today's rate: {_amount(order)}.", ""]
    if cfg.tickets.how_to_buy:
        lines += ["How to pay:", cfg.tickets.how_to_buy, ""]
    lines.append("This is a request; the admin will contact you.")
    return f"Your order: {label} {order['length']}", "\n".join(lines) + "\n"


async def dispatch(conn: sqlite3.Connection, cfg: Config, order_id: int) -> None:
    """Send the order's pending emails, after the response. Each send runs in a thread (a slow mail server never
    blocks the loop); its result is written back here, on the loop, the only place the shared connection is used."""
    if cfg.email is None:
        return
    o = get(conn, order_id)
    if o["user_id"] is not None:
        o["user_name"] = db._user_name(conn, o["user_id"])
    jobs = []
    if o["admin_mail"] == "pending":
        jobs.append(("admin_mail", cfg.email.admin_to, *admin_mail_text(cfg, o)))
    if o["buyer_mail"] == "pending":
        jobs.append(("buyer_mail", o["email"], *buyer_mail_text(cfg, o)))
    for which, to, subject, body in jobs:
        try:
            await asyncio.to_thread(mail.send, cfg.email, to, subject, body)
            state = "sent"
        except Exception as e:
            logger.warning("order #%d: %s to %s failed: %s", order_id, which.replace("_", " "), to, e)
            state = "failed"
        set_mail(conn, order_id, which, state)


# ---------- linking a visitor order (spec section 7) ----------

def suggest_user(conn: sqlite3.Connection, order: dict) -> dict | None:
    """A user whose email or name is the order's email, ignoring case. Only offered to the admin, never linked by itself."""
    row = conn.execute("SELECT id, name, email FROM users WHERE revoked_at IS NULL AND (lower(email)=lower(?) OR lower(name)=lower(?)) "
                       "ORDER BY id LIMIT 1", (order["email"], order["email"])).fetchone()
    return dict(row) if row else None


def link(conn: sqlite3.Connection, cfg: Config, actor: int | None, order_id: int, *, user_id: int | None = None, create: bool = False,
         now: float | None = None) -> dict:
    """Tie an open visitor order to an account: an existing user, or (`create`) a new one named after the order's email.
    Creating is safe though the address is unverified: only its owner can later sign in to that account through Clerk.
    The order's email is never written to users.email."""
    now = _now(now)
    if user_id is None and not create:
        raise OrderError(400, "Choose a user to link, or create one.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        o = get(conn, order_id)
        if o["status"] not in OPEN:
            raise OrderError(409, f"This order is {o['status']}; only an open order can be linked.")
        if o["user_id"] is not None:
            raise OrderError(409, "This order is already linked to an account.")
        if create:
            email = o["email"]
            taken = conn.execute("SELECT id FROM users WHERE lower(name)=lower(?) OR lower(email)=lower(?) ORDER BY id LIMIT 1",
                                 (email, email)).fetchone()
            if taken:
                raise OrderError(409, f"A user named {email} or with that email exists already.", existing_user_id=taken["id"])
            if len(email) > 64:   # the users' name limit
                raise OrderError(400, "The email is too long for a user name; add the user by hand.")
            user_id, _ = db.create_user(conn, email)   # its key is never shown; the buyer signs in with Clerk
            db.audit(conn, actor, "user_add", email, {"role": "user", "order_id": order_id})
        u = conn.execute("SELECT id, name FROM users WHERE id=?", (user_id,)).fetchone()
        if u is None:
            raise OrderError(404, f"No user #{user_id}.")
        conn.execute("UPDATE orders SET user_id=?, updated_at=? WHERE id=?", (u["id"], now, order_id))
        db.audit(conn, actor, "order_link", f"order #{order_id}", {"user": u["name"], "created": bool(create)})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get(conn, order_id)


# ---------- the buyer's view (spec section 8) ----------

BUYER_FIELDS = ("id", "created_at", "updated_at", "name", "email", "tier", "length", "currency", "quoted_usd", "quoted_rate",
                "quoted_amount", "message", "status")


def _own(conn, user_id: int, order_id: int) -> dict:
    o = conn.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user_id)).fetchone()
    if o is None:
        raise OrderError(404, "No such order.")
    return dict(o)


def mine(conn: sqlite3.Connection, user_id: int) -> dict | None:
    """The user's open order; else their latest order while it is done or declined and not dismissed; else None.
    Without the IP, the admin's note, the mail states or the ticket id."""
    row = conn.execute(f"SELECT * FROM orders WHERE user_id=? AND status IN {OPEN} ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        if row is None or row["status"] not in ("done", "declined") or row["seen_at"] is not None:
            return None
    return {k: row[k] for k in BUYER_FIELDS}


def withdraw(conn: sqlite3.Connection, user_id: int, order_id: int, now: float | None = None) -> dict:
    _own(conn, user_id, order_id)
    return set_status(conn, user_id, order_id, "withdrawn", now=now)


def dismiss(conn: sqlite3.Connection, user_id: int, order_id: int, now: float | None = None) -> None:
    """Hide a closed order's notice from the buyer's dashboard."""
    o = _own(conn, user_id, order_id)
    if o["status"] in OPEN:
        raise OrderError(409, "This order is still open.")
    conn.execute("UPDATE orders SET seen_at=? WHERE id=? AND seen_at IS NULL", (_now(now), order_id))


# ---------- admin views and retention ----------

def admin_list(conn: sqlite3.Connection, status: str | None) -> list[dict]:
    """`open` (the default), `all` or one status; newest first, at most 500, with the linked user's current name."""
    status = status or "open"
    if status == "open":
        where, args = f"o.status IN {OPEN}", ()
    elif status == "all":
        where, args = "1=1", ()
    elif status in STATUSES:
        where, args = "o.status=?", (status,)
    else:
        raise OrderError(400, f"Unknown status {status!r}; open, all or one of {', '.join(STATUSES)}.")
    return [dict(r) for r in conn.execute(f"SELECT o.*, u.name AS user_name FROM orders o LEFT JOIN users u ON u.id=o.user_id "
                                          f"WHERE {where} ORDER BY o.created_at DESC, o.id DESC LIMIT 500", args)]


def new_count(conn: sqlite3.Connection) -> int:
    return _count(conn, "status='new'", ())


def clear_old_ips(conn: sqlite3.Connection, now: float | None = None) -> int:
    """The IP is kept only for the per-IP limit: cleared on every order older than 30 days. Returns the rows changed."""
    return conn.execute("UPDATE orders SET ip=NULL WHERE ip IS NOT NULL AND created_at<?", (_now(now) - IP_KEEP_S,)).rowcount
