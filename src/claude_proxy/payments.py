"""Online payment through ZarinPal (payments design, 2026-10-05): a signed-in user pays an order in Toman and the
ticket is granted as soon as ZarinPal confirms the payment.

The shared connection is used only on the event loop and never across an await inside a transaction: each ZarinPal
call happens between two short, separate writes.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from . import db, mail, orders, tickets, zarinpal
from .config import Config, LENGTHS

logger = logging.getLogger(__name__)

CURRENCY = "IRT"
EXPIRE_S = 3600
PAUSED = "Online payment is paused; try again later or send an order request."
UNAVAILABLE = "ZarinPal is not available right now; try again or send an order request."


class PaymentError(orders.OrderError):
    """A refused payment operation; the web layer maps it like any OrderError (status and message)."""


def _now(now) -> int:
    return int(time.time() if now is None else now)


def missing(cfg: Config) -> list[str]:
    """What a [zarinpal] section still needs before payments are on (section 2). Names only, never the merchant ID."""
    out = []
    if not cfg.tickets.enabled:
        out.append("[tickets] enabled")
    if cfg.zarinpal is None or not cfg.zarinpal.merchant_id():
        out.append("ZARINPAL_MERCHANT_ID in the environment")
    if CURRENCY not in cfg.tickets.currencies:
        out.append(f"{CURRENCY} in [tickets.currencies]")
    if not cfg.listener.home_url:
        out.append("[listener] home_url")
    return out


def on(cfg: Config) -> bool:
    """Payments design, section 2: tickets, a [zarinpal] section, the merchant ID, IRT and the home URL."""
    return cfg.zarinpal is not None and not missing(cfg)


def warn_if_off(cfg: Config) -> None:
    """At startup: a [zarinpal] section with payments still off logs one warning naming what is missing."""
    if cfg.zarinpal is not None and not on(cfg):
        logger.warning("[zarinpal] is set but online payment is off; missing: %s", ", ".join(missing(cfg)))


def rate_ok(conn: sqlite3.Connection, now: float | None = None) -> bool:
    rate = tickets.current_rate(conn, CURRENCY)
    return rate is not None and not tickets.rate_is_stale(rate, _now(now))


def get(conn: sqlite3.Connection, pid: int) -> dict:
    row = conn.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise PaymentError(404, "No such payment.")
    return dict(row)


def _set(conn: sqlite3.Connection, pid: int, status: str, now: int, **cols) -> None:
    sets = ", ".join(["status=?", "updated_at=?"] + [f"{k}=?" for k in cols])
    conn.execute(f"UPDATE payments SET {sets} WHERE id=?", (status, now, *cols.values(), pid))


def _label(cfg: Config, tier: str) -> str:
    t = cfg.tickets.tiers.get(tier)
    return t.label if t else tier


async def start(conn: sqlite3.Connection, cfg: Config, user, tier: str, length: str, email: str, now: float | None = None) -> dict:
    """Create the order (the usual order rules) and open a ZarinPal payment for its quote. Returns the payment id and
    the StartPay address the browser goes to."""
    now = _now(now)
    if not on(cfg):
        raise PaymentError(404, "Online payment is not set up on this gateway.")
    if not rate_ok(conn, now):
        raise PaymentError(409, PAUSED)
    o = orders.create(conn, cfg, tier=tier, length=length, currency=CURRENCY, name=user["name"], email=email,
                      user_id=user["id"], notify=False, now=now)
    amount = int(round(o["quoted_amount"]))
    try:
        authority = await zarinpal.request(cfg.zarinpal.merchant_id(), amount,
                                           f"Claude Gateway: {_label(cfg, tier)}, {length}, order #{o['id']}",
                                           f"{cfg.listener.home_url.rstrip('/')}/pay/callback", o["email"], o["id"],
                                           cfg.zarinpal.sandbox)
    except (zarinpal.ZarinpalUnavailable, zarinpal.ZarinpalRefused) as e:
        logger.warning("order #%d: ZarinPal payment request failed: %s", o["id"], e)
        orders.set_status(conn, user["id"], o["id"], "withdrawn")
        raise PaymentError(502, UNAVAILABLE) from e
    cur = conn.execute("INSERT INTO payments(order_id, user_id, created_at, updated_at, amount, authority, status) "
                       "VALUES(?,?,?,?,?,?,'started')", (o["id"], user["id"], now, now, amount, authority))
    db.audit(conn, user["id"], "payment_started", f"order #{o['id']}", {"payment_id": cur.lastrowid, "amount": amount})
    return {"payment_id": cur.lastrowid, "url": zarinpal.start_url(authority, cfg.zarinpal.sandbox)}


async def callback(conn: sqlite3.Connection, cfg: Config, authority: str, status: str, now: float | None = None) -> dict:
    """The buyer came back from ZarinPal (section 4, Callback). Idempotent: a payment that is no longer `started` is
    returned as it is, without asking ZarinPal again. The returned dict carries `"changed": True` only when THIS
    call is the one that moved the payment to paid or paid_unfulfilled (the _fulfil/_unfulfilled paths); every
    other return has no such key. Two callbacks racing on the same authority can both verify before either writes
    (both read `status == "started"` before either awaits ZarinPal), so the caller must use this flag, not the
    payment's status alone, to decide whether to send mail -- otherwise both would send it."""
    row = conn.execute("SELECT * FROM payments WHERE authority=?", (authority,)).fetchone()
    if row is None:
        raise PaymentError(404, "No such payment.")
    p = dict(row)
    if p["status"] != "started" or not on(cfg):
        return p
    if status != "OK":
        _close(conn, p, "cancelled", _now(now))
        return get(conn, p["id"])
    if _cancel_if_order_closed(conn, p, _now(now)):
        return get(conn, p["id"])
    try:
        v = await zarinpal.verify(cfg.zarinpal.merchant_id(), p["amount"], authority, cfg.zarinpal.sandbox)
    except zarinpal.ZarinpalUnavailable as e:
        logger.warning("payment #%d: verify unavailable: %s", p["id"], e)
        return get(conn, p["id"])   # still started: reconcile retries it within the hour
    except zarinpal.ZarinpalRefused as e:
        p = get(conn, p["id"])
        if p["status"] != "started":   # another callback finished while this one awaited ZarinPal
            return p
        _close(conn, p, "failed", _now(now), error=f"{e.code}: {e.message}")
        return get(conn, p["id"])
    p = get(conn, p["id"])
    ref_id, card_pan = str(v["ref_id"]), v.get("card_pan")
    if p["status"] in ("paid", "paid_unfulfilled"):   # another callback already fulfilled it
        return p
    if p["status"] != "started":   # closed (cancelled/failed/expired) while this one awaited ZarinPal: never lose a verified ref_id
        return _unfulfilled(conn, p, ref_id, card_pan, _now(now),
                            f"verified after the payment was closed ({p['status']})")
    return _fulfil(conn, cfg, p, ref_id, card_pan, _now(now))


def _cancel_if_order_closed(conn: sqlite3.Connection, p: dict, now: int) -> bool:
    """Before verifying: an order that was withdrawn, declined or granted by hand can no longer be granted from, so
    the payment is cancelled unverified (ZarinPal returns an unverified payment's money) and True is returned."""
    o_status = orders.get(conn, p["order_id"])["status"]
    if o_status in orders.OPEN:
        return False
    logger.info("payment #%d: order #%d is %s; cancelled without verifying", p["id"], p["order_id"], o_status)
    _close(conn, p, "cancelled", now, error=f"order {o_status} before verify")
    return True


def _unfulfilled(conn: sqlite3.Connection, p: dict, ref_id: str, card_pan: str | None, now: int, reason: str) -> dict:
    """Record a ZarinPal-verified payment that could not (or no longer can) be granted, without losing its ref_id."""
    # Logged first: if the write below fails, the verified ref_id still has a trace.
    logger.warning("payment #%d (ref %s) verified but not granted: %s", p["id"], ref_id, reason)
    _set(conn, p["id"], "paid_unfulfilled", now, ref_id=ref_id, card_pan=card_pan, error=reason)
    db.audit(conn, p["user_id"], "payment_unfulfilled", f"order #{p['order_id']}", {"payment_id": p["id"], "ref_id": ref_id,
                                                                                     "reason": reason})
    return {**get(conn, p["id"]), "changed": True}


def _close(conn: sqlite3.Connection, p: dict, status: str, now: int, **cols) -> None:
    """A payment that will never be paid: its status, and its order withdrawn if still open."""
    _set(conn, p["id"], status, now, **cols)
    if orders.get(conn, p["order_id"])["status"] in orders.OPEN:
        orders.set_status(conn, p["user_id"], p["order_id"], "withdrawn", now=now)
    db.audit(conn, p["user_id"], f"payment_{status}", f"order #{p['order_id']}", {"payment_id": p["id"], **cols})


def _fulfil(conn: sqlite3.Connection, cfg: Config, p: dict, ref_id: str, card_pan: str | None, now: int) -> dict:
    """Verified: grant at the paid quote, and mark the payment paid inside the grant's transaction. A refused grant
    leaves the order open and the payment `paid_unfulfilled` (section 4, Grant)."""
    o = orders.get(conn, p["order_id"])
    buyer = conn.execute("SELECT * FROM users WHERE id=?", (p["user_id"],)).fetchone()

    def mark(tid):
        _set(conn, p["id"], "paid", now, ref_id=ref_id, card_pan=card_pan)
        db.audit(conn, p["user_id"], "payment_paid", f"order #{p['order_id']}", {"payment_id": p["id"], "ref_id": ref_id, "ticket_id": tid})
    try:
        if buyer is None:
            raise tickets.TicketError("the account no longer exists")
        tickets.grant(conn, cfg, None, buyer, o["tier"], o["length"], CURRENCY, note=f"ZarinPal {ref_id}",
                      order_id=o["id"], paid=True, after=mark, now=now)
    except Exception as e:
        try:
            committed = get(conn, p["id"])["status"] == "paid"
        except Exception:
            committed = False
        if committed:   # the grant committed, then something after it raised: keep paid
            logger.exception("payment #%d (ref %s): error after the grant committed", p["id"], ref_id)
            return {**get(conn, p["id"]), "changed": True}
        _unfulfilled(conn, p, ref_id, card_pan, now, str(e))
        if not isinstance(e, (tickets.TicketError, orders.OrderError)):
            raise   # unexpected (e.g. sqlite3.OperationalError): the payment is recorded, then the error still surfaces
    return {**get(conn, p["id"]), "changed": True}


async def reconcile(conn: sqlite3.Connection, cfg: Config, now: float | None = None) -> dict:
    """Payments still `started` an hour after they began (section 4, Expiry): the buyer never came back, or their
    verify could not reach ZarinPal. Each is verified with its stored amount: confirmed -> granted (or
    paid_unfulfilled); refused -> expired, its order withdrawn; unreachable -> left `started` for the next run.
    With payments off, or its order closed, it is closed without verifying. Returns counts per outcome, and in
    `changed` the ids THIS run moved to paid/paid_unfulfilled (the caller mails those, once)."""
    now = _now(now)
    out = {"paid": 0, "paid_unfulfilled": 0, "expired": 0, "cancelled": 0, "pending": 0, "errors": 0, "changed": []}
    ids = [r[0] for r in conn.execute("SELECT id FROM payments WHERE status='started' AND created_at<? ORDER BY id",
                                      (now - EXPIRE_S,)).fetchall()]
    for pid in ids:
        verified = False
        try:
            p = get(conn, pid)
            if p["status"] != "started":   # a callback finished it while an earlier row awaited ZarinPal
                continue
            if not on(cfg):
                _close(conn, p, "expired", now)
                out["expired"] += 1
                continue
            if _cancel_if_order_closed(conn, p, now):
                out["cancelled"] += 1
                continue
            try:
                v = await zarinpal.verify(cfg.zarinpal.merchant_id(), p["amount"], p["authority"], cfg.zarinpal.sandbox)
            except zarinpal.ZarinpalUnavailable as e:
                logger.warning("payment #%d: verify still unavailable: %s", pid, e)
                out["pending"] += 1
                continue
            except zarinpal.ZarinpalRefused as e:
                p = get(conn, pid)
                if p["status"] == "started":
                    _close(conn, p, "expired", now, error=f"{e.code}: {e.message}")
                    out["expired"] += 1
                continue
            p = get(conn, pid)
            ref_id, card_pan = str(v["ref_id"]), v.get("card_pan")
            if p["status"] in ("paid", "paid_unfulfilled"):   # a callback fulfilled it while this awaited
                continue
            verified = True   # from here on, no await: a paid/paid_unfulfilled status is this run's doing
            if p["status"] != "started":
                r = _unfulfilled(conn, p, ref_id, card_pan, now, f"verified after the payment was closed ({p['status']})")
            else:
                r = _fulfil(conn, cfg, p, ref_id, card_pan, now)
            if r.get("changed"):
                out[r["status"]] += 1
                out["changed"].append(pid)
        except Exception:
            logger.exception("payment #%d: reconcile failed", pid)
            out["errors"] += 1
            # _fulfil re-raises an unexpected error after recording paid_unfulfilled: still mail the admin.
            try:
                if verified and get(conn, pid)["status"] in ("paid", "paid_unfulfilled") and pid not in out["changed"]:
                    out["changed"].append(pid)
            except Exception:
                logger.exception("payment #%d: re-reading after the failure failed", pid)
    return out


def _toman(n) -> str:
    return f"{int(round(n)):,} Toman"


def mails(cfg: Config, p: dict, o: dict, ticket: dict | None, reason: str | None) -> list[tuple[str, str, str]]:
    """(to, subject, body) for a payment that just became paid or paid_unfulfilled (section 5)."""
    if cfg.email is None:
        return []
    what = f"{_label(cfg, o['tier'])}, {o['length']} ({LENGTHS.get(o['length'], '?')} days)"
    head = f"Paid: {what}, {_toman(p['amount'])}, ZarinPal reference {p['ref_id']}, by {o['name']} <{o['email']}>."
    link = f"\n\nOrders: {cfg.listener.dashboard_url.rstrip('/')}/dashboard#orders" if cfg.listener.dashboard_url else ""
    if p["status"] == "paid_unfulfilled":
        return [(cfg.email.admin_to, f"Paid, ticket NOT granted: order #{o['id']}",
                 f"{head}\n\nThe ticket was NOT granted: {reason}. Grant it from the order or refund the payment.{link}\n")]
    out = [(cfg.email.admin_to, f"Paid: {_label(cfg, o['tier'])} {o['length']}", f"{head}\nThe ticket is live.{link}\n")]
    if ticket is not None:
        span = f"{tickets._date(ticket['starts_at'])} to {tickets._date(ticket['ends_at'])}"
        out.append((o["email"], f"Your payment: {_label(cfg, o['tier'])} {o['length']}",
                    f"Thank you. Your payment of {_toman(p['amount'])} went through.\n\nTicket: {what}, {span}.\n"
                    f"ZarinPal reference: {p['ref_id']}\n"))
    return out


async def send_mails(conn: sqlite3.Connection, cfg: Config, pid: int) -> None:
    """After the response: send what `mails` lists. A failure is logged and never touches the payment."""
    p = get(conn, pid)
    if p["status"] not in ("paid", "paid_unfulfilled"):
        return
    o = orders.get(conn, p["order_id"])
    t = conn.execute("SELECT * FROM tickets WHERE id=?", (o["ticket_id"],)).fetchone() if o["ticket_id"] else None
    for to, subject, body in mails(cfg, p, o, dict(t) if t else None, p["error"]):
        try:
            await asyncio.to_thread(mail.send, cfg.email, to, subject, body)
        except Exception as e:
            logger.warning("payment #%d: mail to %s failed: %s", pid, to, e)
