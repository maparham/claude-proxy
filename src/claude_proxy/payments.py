"""Online payment through ZarinPal (payments design, 2026-10-05): a signed-in user pays an order in Toman and the
ticket is granted as soon as ZarinPal confirms the payment.

The shared connection is used only on the event loop and never across an await inside a transaction: each ZarinPal
call happens between two short, separate writes.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from . import db, orders, tickets, zarinpal
from .config import Config

logger = logging.getLogger(__name__)

CURRENCY = "IRT"
EXPIRE_S = 3600
PAUSED = "Online payment is paused; try again later or send an order request."
UNAVAILABLE = "ZarinPal is not available right now; try again or send an order request."


class PaymentError(orders.OrderError):
    """A refused payment operation; the web layer maps it like any OrderError (status and message)."""


def _now(now) -> int:
    return int(time.time() if now is None else now)


def on(cfg: Config) -> bool:
    """Payments design, section 2: tickets, a [zarinpal] section, the merchant ID, IRT and the home URL."""
    return bool(cfg.tickets.enabled and cfg.zarinpal is not None and cfg.zarinpal.merchant_id()
                and CURRENCY in cfg.tickets.currencies and cfg.listener.home_url)


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
