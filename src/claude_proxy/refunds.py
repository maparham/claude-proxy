"""Cancel an unused pass and get the money back (2026-11-08). ZarinPal can only reverse a payment within 30 minutes of
it and has no refund after that, so the buyer may do it themselves for REFUND_WINDOW_S after the payment, which
leaves a margin: a pass bought online through ZarinPal (not an upgrade, never upgraded) that has not served a
request. The reverse comes first; only once ZarinPal confirms it is the pass cancelled, its referral effects undone
and the payment marked reversed, in one transaction. The shared connection is never held across the ZarinPal await.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from . import db, mail, orders, payments, referrals, tickets, zarinpal
from .config import LENGTHS, Config

logger = logging.getLogger(__name__)

REFUND_WINDOW_S = 15 * 60
# ZarinPal's 30 minutes run from the payment itself, which lies between the payment's start (created_at) and its
# verify (paid_at). A verify that came late (reconcile grants an hour after) must not open a window ZarinPal has
# already closed, so the window also ends this long after the start: a margin under the 30 minutes.
REVERSE_LIMIT_S = 25 * 60
GONE = "This pass can no longer be refunded."
FAILED = "ZarinPal refused the refund; nothing was changed. Contact support."
UNKNOWN = "ZarinPal did not confirm the refund; support will check it and get back to you."


def _now(now) -> int:
    return int(time.time() if now is None else now)


def _candidates(conn: sqlite3.Connection, user_id: int, now: int, payment_id: int | None = None) -> list[dict]:
    sql = (f"SELECT p.id AS payment_id, p.amount AS paid, p.paid_at, p.created_at AS paid_from, p.authority, p.ref_id, "
           f"p.card_pan, o.id AS order_id, "
           f"{tickets.TICKET_COLS} FROM payments p JOIN orders o ON o.id=p.order_id JOIN tickets t ON t.id=o.ticket_id "
           "WHERE p.user_id=? AND p.status='paid' AND p.paid_at>? AND p.created_at>? AND p.reverse_started_at IS NULL AND p.reversed_at IS NULL "
           "AND o.upgrade_of IS NULL AND t.cancelled_at IS NULL AND t.user_id=p.user_id "
           "AND NOT EXISTS (SELECT 1 FROM ticket_bonuses b WHERE b.ticket_id=t.id AND b.tier IS NOT NULL AND b.cancelled_at IS NULL)")
    args = [user_id, now - REFUND_WINDOW_S, now - REVERSE_LIMIT_S]
    if payment_id is not None:
        sql += " AND p.id=?"
        args.append(payment_id)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY p.paid_at", args)]
    return [r for r in rows if not tickets.was_used(conn, r)]


def offers(conn: sqlite3.Connection, cfg: Config, user_id: int, now: float | None = None) -> list[dict]:
    """The buyer's passes they may cancel for their money back right now, earliest first; [] with payments off.
    Each is named by its payment's id, which the buyer already sees (/api/me/payments), never by the ticket's."""
    if not payments.on(cfg):
        return []
    now = _now(now)
    return [{"payment_id": r["payment_id"], "tier": r["tier"], "label": payments._label(cfg, r["tier"]), "length": r["length"], "starts_at": r["starts_at"],
             "amount": r["paid"], "currency": payments.CURRENCY, "until": min(r["paid_at"] + REFUND_WINDOW_S, r["paid_from"] + REVERSE_LIMIT_S)}
            for r in _candidates(conn, user_id, now)]


async def refund(conn: sqlite3.Connection, cfg: Config, user, payment_id: int, now: float | None = None) -> dict:
    """Reverse the buyer's own payment `payment_id` of a refundable pass, then cancel the pass. PaymentError(409, GONE)
    when it is not (or no longer) refundable; (502, FAILED) when ZarinPal refuses, nothing changed and the buyer may
    try again; (502, UNKNOWN) when ZarinPal could not be reached: the payment keeps reverse_started_at, so no second
    reverse is tried, and the admin is mailed to check it in the ZarinPal panel. Returns the cancelled ticket."""
    now = _now(now)
    if not payments.on(cfg):
        raise payments.PaymentError(409, GONE)
    conn.execute("BEGIN IMMEDIATE")
    try:
        found = _candidates(conn, user["id"], now, payment_id)
        if not found:
            raise payments.PaymentError(409, GONE)
        r = found[0]
        ticket_id = r["id"]
        conn.execute("UPDATE payments SET reverse_started_at=?, updated_at=? WHERE id=?", (now, now, r["payment_id"]))
        db.audit(conn, user["id"], "payment_reverse_started", f"order #{r['order_id']}",
                 {"payment_id": r["payment_id"], "ticket_id": ticket_id})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    try:
        await zarinpal.reverse(cfg.zarinpal.merchant_id(), r["authority"], cfg.zarinpal.sandbox)
    except zarinpal.ZarinpalRefused as e:
        logger.warning("payment #%d: reverse refused: %s", r["payment_id"], e)
        conn.execute("UPDATE payments SET reverse_started_at=NULL, error=?, updated_at=? WHERE id=?",
                     (f"reverse {e.code}: {e.message}", _now(None), r["payment_id"]))
        db.audit(conn, user["id"], "payment_reverse_refused", f"order #{r['order_id']}",
                 {"payment_id": r["payment_id"], "code": e.code, "message": e.message})
        raise payments.PaymentError(502, FAILED) from e
    except zarinpal.ZarinpalUnavailable as e:
        # Logged as an error: without [email] this line and the Orders tab are all the admin gets.
        logger.error("payment #%d (ref %s): reverse result unknown, check it in the ZarinPal panel: %s",
                     r["payment_id"], r["ref_id"], e)
        db.audit(conn, user["id"], "payment_reverse_unknown", f"order #{r['order_id']}", {"payment_id": r["payment_id"]})
        mail.spawn(send_mails(conn, cfg, r["payment_id"], unknown=True))
        raise payments.PaymentError(502, UNKNOWN) from e

    done = _now(None)

    def record(t: dict) -> None:
        conn.execute("UPDATE tickets SET refunded_at=? WHERE id=?", (done, t["id"]))
        conn.execute("UPDATE payments SET reversed_at=?, updated_at=? WHERE id=?", (done, done, r["payment_id"]))
        referrals.on_refunded(conn, r["order_id"], done)
        db.audit(conn, user["id"], "payment_reversed", f"order #{r['order_id']}",
                 {"payment_id": r["payment_id"], "ticket_id": t["id"], "amount": r["paid"], "ref_id": r["ref_id"]})
    try:
        tickets.cancel(conn, cfg, user["id"], ticket_id, done, after=record)
    except tickets.TicketError:
        if tickets.get(conn, ticket_id)["cancelled_at"] is None:
            raise
        # Cancelled meanwhile (the admin, while ZarinPal answered): the refund is still recorded in full.
        conn.execute("BEGIN IMMEDIATE")
        try:
            record(tickets.get(conn, ticket_id))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    except Exception:
        # The money is back with the buyer but the pass is still live: the admin must cancel it by hand.
        logger.exception("payment #%d reversed, but cancelling pass #%d failed", r["payment_id"], ticket_id)
        conn.execute("UPDATE payments SET reversed_at=?, updated_at=? WHERE id=?", (done, done, r["payment_id"]))
        mail.spawn(send_mails(conn, cfg, r["payment_id"], cancel_failed=True))
        return {"ticket": tickets.get(conn, ticket_id)}
    mail.spawn(send_mails(conn, cfg, r["payment_id"]))
    return {"ticket": tickets.get(conn, ticket_id)}


def mails(conn: sqlite3.Connection, cfg: Config, p: dict, o: dict, unknown: bool = False,
          cancel_failed: bool = False) -> list[tuple[str, str, str]]:
    """(to, subject, body): the admin's notice, and the buyer's once the refund is done."""
    if cfg.email is None:
        return []
    label = payments._label(cfg, o["tier"])
    what = f"{label}, {o['length']} ({LENGTHS.get(o['length'], '?')} days), pass #{o['ticket_id']}"
    head = f"{payments._toman(p['amount'])}, ZarinPal reference {p['ref_id']}, by {o['name']} <{o['email']}>: {what}."
    link = f"\n\nOrders: {cfg.listener.dashboard_url.rstrip('/')}/dashboard#orders" if cfg.listener.dashboard_url else ""
    if unknown:
        return [(cfg.email.admin_to, f"Refund NOT confirmed: order #{o['id']}",
                 f"The buyer asked to cancel and refund {head}\nZarinPal could not be reached, so it is not known "
                 f"whether the payment was reversed. Check it in the ZarinPal panel: if it was, cancel the pass; if "
                 f"not, tell the buyer the pass stays theirs.{link}\n")]
    if cancel_failed:
        return [(cfg.email.admin_to, f"Refunded, pass NOT cancelled: order #{o['id']}",
                 f"ZarinPal reversed {head}\nCancelling the pass failed: cancel it by hand.{link}\n")]
    out = [(cfg.email.admin_to, f"Refunded: {label} {o['length']}", f"Cancelled and refunded by the buyer: {head}{link}\n")]
    if o.get("lang") == "fa":
        what_fa = f"{orders.fa_label(cfg, o['tier'])}، ۱ {orders.FA_LENGTHS.get(o['length'], o['length'])}"
        out.append((o["email"], f"بازگشت وجه: {what_fa}",
                    f"بسته {what_fa} لغو شد و مبلغ {orders.fa_amount(p['amount'], 'IRT')} از طریق زرین‌پال به کارتی که با آن "
                    f"پرداخت کردید برگشت. ممکن است نمایش آن در حساب بانکی‌تان کمی طول بکشد.\nکد پیگیری زرین‌پال: {p['ref_id']}\n"))
    else:
        out.append((o["email"], f"Your refund: {label} {o['length']}",
                    f"Your {label} pass ({o['length']}) is cancelled and {payments._toman(p['amount'])} went back to the card you "
                    f"paid with, through ZarinPal. Your bank may take a little while to show it.\n"
                    f"ZarinPal reference: {p['ref_id']}\n"))
    return out


async def send_mails(conn: sqlite3.Connection, cfg: Config, pid: int, **kw) -> None:
    """Send what `mails` lists. A failure is logged and never touches the refund."""
    try:
        p = payments.get(conn, pid)
        o = orders.get(conn, p["order_id"])
        for to, subject, body in mails(conn, cfg, p, o, **kw):
            try:
                await asyncio.to_thread(mail.send, mail.servers(conn, cfg.email), to, subject, body, None)
            except Exception as e:
                logger.warning("refund of payment #%d: mail to %s failed: %s", pid, to, e)
    except Exception:
        logger.exception("refund of payment #%d: mails failed", pid)
