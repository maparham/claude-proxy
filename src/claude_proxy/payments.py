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

from . import db, mail, orders, referrals, tickets, zarinpal
from .config import Config, LENGTHS

logger = logging.getLogger(__name__)

CURRENCY = "IRT"
EXPIRE_S = 3600
LINK_EXPIRED_GRACE_S = 3600   # a payment link is declined this long after its deadline (links spec, section 5)
PAUSED = "Online payment is paused; try again later."
MANUAL_ONLY = "Online payment is off for now; send an order request instead."
UNAVAILABLE = "ZarinPal is not available right now; try again later."
ALREADY_PAID = "This order is already paid."
LINK_OTHER_ORDER = "You have another open order; cancel it first."


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


MODE_KEY = "pay_mode"         # settings row: the admin's Payment selector on the Orders tab
MODES = ("zarinpal", "manual", "both")
LEGACY_KEY = "manual_orders"  # the old on/off switch: "1" read as "both" until the selector is first set


def pay_mode(conn: sqlite3.Connection, cfg: Config) -> str | None:
    """The admin's Payment selector: ZarinPal only (the default), manual (unpaid orders) only, or both side by side.
    None without a [zarinpal] section: no selector then, and unpaid orders are the only way to buy."""
    if cfg.zarinpal is None:
        return None
    row = conn.execute("SELECT value FROM settings WHERE key=?", (MODE_KEY,)).fetchone()
    if row is not None and row[0] in MODES:
        return row[0]
    row = conn.execute("SELECT value FROM settings WHERE key=?", (LEGACY_KEY,)).fetchone()
    return "both" if row is not None and row[0] == "1" else "zarinpal"


def set_pay_mode(conn: sqlite3.Connection, actor: int, mode: str) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (MODE_KEY, mode))
    db.audit(conn, actor, "pay_mode", mode)


def online(mode: str | None) -> bool:
    """Whether the order dialog offers ZarinPal, given pay_mode(). A missing merchant ID or a stale Toman rate doesn't
    hide it; start() refuses then, and the Orders tab says why (ready())."""
    return mode in ("zarinpal", "both")


def prices(conn: sqlite3.Connection, cfg: Config, now: float) -> dict | None:
    """The Toman price list for the Pay button, None without an IRT currency or rate (price_table would quote USD)."""
    if CURRENCY not in cfg.tickets.currencies or tickets.current_rate(conn, CURRENCY) is None:
        return None
    return tickets.price_table(conn, cfg, now, CURRENCY)


def ready(conn: sqlite3.Connection, cfg: Config, now: float | None = None) -> bool:
    """Whether ZarinPal can take a payment now: set up and a fresh Toman rate."""
    return on(cfg) and rate_ok(conn, now)


def manual_orders(mode: str | None) -> bool:
    """Whether a buyer may order without paying online, given pay_mode(): the selector, ZarinPal down or not."""
    return mode != "zarinpal"


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


async def start(conn: sqlite3.Connection, cfg: Config, user, tier: str, length: str, email: str, now: float | None = None,
              terms_version: str | None = None, lang: str = "en", start_now: bool = False, via: str = "web") -> dict:
    """Create the order (the usual order rules, terms tick included) and open a ZarinPal payment for its quote.
    `start_now`: the ticket starts beside the running one (orders.create). `via`: where it was ordered (orders.create).
    Returns the payment id and the StartPay address the browser goes to."""
    now = _now(now)
    if pay_mode(conn, cfg) == "manual":
        raise PaymentError(409, MANUAL_ONLY)
    if not on(cfg):
        raise PaymentError(404, "Online payment is not set up on this gateway.")
    if not rate_ok(conn, now):
        raise PaymentError(409, PAUSED)
    o = orders.create(conn, cfg, tier=tier, length=length, currency=CURRENCY, name=user["name"], email=email,
                      user_id=user["id"], notify=False, now=now, terms_version=terms_version, lang=lang,
                      start_now=start_now, via=via)
    return await _open(conn, cfg, user, o, now, withdraw=True)


def _check_fits(conn: sqlite3.Connection, cfg: Config, user, o: dict, now: int) -> None:
    """Just before ZarinPal takes the money, the order's pass (or upgrade) must still fit, judged as the order form
    judged it when the order was made, which may be days ago: the 5-hour window in particular can fill in minutes, and
    a payment the callback then could not grant leaves the order `paid_unfulfilled` (2026-11-07). A pass sold out only
    until the 5-hour reset says when to try again."""
    from . import capacity   # here, not at the top: keeps payments' import list to what it always needed
    if not capacity.context(conn, cfg, tickets.ACCOUNT_ID, now).fresh:
        raise PaymentError(409, orders.UNAVAILABLE)
    if o["upgrade_of"] is not None:
        try:
            q = tickets.upgrade_quote(conn, cfg, tickets.get(conn, o["upgrade_of"]), o["tier"], CURRENCY, now)
        except tickets.TicketError as e:
            raise PaymentError(409, str(e)) from e
        if q["sold_out"]:
            raise PaymentError(409, "That upgrade is sold out.")
        return
    try:
        orders._check_on_sale(conn, cfg, o["tier"], o["length"], now, user["id"], bool(o["start_now"]))
    except orders.OrderError as e:
        raise PaymentError(e.status, str(e)) from e


async def pay_order(conn: sqlite3.Connection, cfg: Config, user, order_id: int, now: float | None = None) -> dict:
    """Pay online for the buyer's own open Toman order at its quote: one sent unpaid, or one whose payment never came
    back (still `started` until reconcile; whichever payment verifies first grants, the other is then cancelled
    unverified). A ZarinPal failure leaves the order open, as it was. Returns what start() does.
    A payment link's order skips the stale-rate pause and refuses past its deadline; no order with a payment that
    went through is paid again. A pass that no longer fits (_check_fits) or a missing fresh weekly report refuses before opening a payment, same as the
    price list and orders.create: a payment the callback could not grant (tickets.grant's NO_REPORT) would otherwise
    take the buyer's money and leave the order `paid_unfulfilled` (final review, finding 1)."""
    now = _now(now)
    o = orders._own(conn, user["id"], order_id)
    if o["status"] not in orders.OPEN:
        raise PaymentError(409, orders.CHANGED)
    if o["currency"] != CURRENCY:
        raise PaymentError(409, "Only an order in Toman can be paid online.")
    if _has_paid(conn, o["id"]):
        raise PaymentError(409, ALREADY_PAID)
    if o["pay_code"] is not None and now > o["pay_by"]:   # a payment link past its deadline (links spec, section 5)
        raise PaymentError(404, orders.LINK_INVALID)
    if pay_mode(conn, cfg) == "manual":
        raise PaymentError(409, MANUAL_ONLY)
    if not on(cfg):
        raise PaymentError(404, "Online payment is not set up on this gateway.")
    if o["pay_code"] is None and not rate_ok(conn, now):
        raise PaymentError(409, PAUSED)
    _check_fits(conn, cfg, user, o, now)
    return await _open(conn, cfg, user, o, now, withdraw=False)


def create_link(conn: sqlite3.Connection, cfg: Config, actor: int | None, **kw) -> dict:
    """An admin's payment link (links spec, section 3): only while ZarinPal can take it, and at a fresh rate, so a link
    never freezes a stale price. The rest is orders.create_link."""
    if not on(cfg):
        raise PaymentError(404, "Online payment is not set up on this gateway.")
    if pay_mode(conn, cfg) == "manual":
        raise PaymentError(409, MANUAL_ONLY)
    if not rate_ok(conn):
        raise PaymentError(409, PAUSED)
    return orders.create_link(conn, cfg, actor, currency=CURRENCY, **kw)


def _has_paid(conn: sqlite3.Connection, order_id: int) -> bool:
    """A payment on the order went through (granted or not): it is never offered for paying again."""
    return conn.execute("SELECT 1 FROM payments WHERE order_id=? AND status IN ('paid','paid_unfulfilled') LIMIT 1",
                        (order_id,)).fetchone() is not None


def link_state(conn: sqlite3.Connection, user, code: str, now: float | None = None) -> dict:
    """What the payment link `code` is to the signed-in `user` (links spec, section 4). Checked in this order, so a
    stranger learns at most that the link exists and its masked email."""
    now = _now(now)
    row = conn.execute("SELECT * FROM orders WHERE pay_code=?", (str(code),)).fetchone()
    if row is None or row["user_id"] not in (None, user["id"]):
        return {"state": "invalid", "message": orders.LINK_INVALID}
    o = dict(row)
    if (user["email"] or "").lower() != o["email"].lower():
        masked = orders.mask_email(o["email"])
        return {"state": "other_email", "email": masked, "message": f"This link is for {masked}; sign in with that email."}
    if _has_paid(conn, o["id"]):
        unfulfilled = conn.execute("SELECT 1 FROM payments WHERE order_id=? AND status='paid' LIMIT 1", (o["id"],)).fetchone() is None
        return {"state": "paid", "unfulfilled": unfulfilled, "order": o, "message": ALREADY_PAID}
    if o["status"] not in orders.OPEN or now > o["pay_by"]:
        return {"state": "invalid", "message": orders.LINK_INVALID}
    if o["user_id"] is None and conn.execute(f"SELECT 1 FROM orders WHERE user_id=? AND status IN {orders.OPEN} LIMIT 1",
                                             (user["id"],)).fetchone():
        return {"state": "other_order", "message": LINK_OTHER_ORDER}
    return {"state": "open", "order": o}


LINK_REFUSED = {"invalid": 404, "other_email": 403, "paid": 409, "other_order": 409}


async def pay_link(conn: sqlite3.Connection, cfg: Config, user, code: str, terms_version, now: float | None = None) -> dict:
    """Pay a payment link (links spec, section 4): claimed for `user` on the first pay, then paid like any open order,
    at its quote, without the stale-rate pause. A newer tick updates the order's `terms_version` on every pay, not
    only the first (section 4, step 2). Returns what start() does. Without a fresh weekly report this refuses before
    opening a payment, same as pay_order (final review, finding 1)."""
    now = _now(now)
    if terms_version != tickets.TERMS_VERSION:
        raise PaymentError(400, tickets.TERMS_CHANGED)
    if not on(cfg):
        raise PaymentError(404, "Online payment is not set up on this gateway.")
    if pay_mode(conn, cfg) == "manual":
        raise PaymentError(409, MANUAL_ONLY)
    conn.execute("BEGIN IMMEDIATE")
    try:
        s = link_state(conn, user, code, now)
        if s["state"] != "open":
            raise PaymentError(LINK_REFUSED[s["state"]], s["message"])
        o = s["order"]
        if o["user_id"] is None:
            conn.execute("UPDATE orders SET user_id=?, name=?, updated_at=? WHERE id=?",
                         (user["id"], user["name"], now, o["id"]))
            db.audit(conn, user["id"], "order_paylink_claimed", f"order #{o['id']}")
        if o["terms_version"] != terms_version:
            conn.execute("UPDATE orders SET terms_version=?, updated_at=? WHERE id=?", (terms_version, now, o["id"]))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    o = orders.get(conn, o["id"])
    _check_fits(conn, cfg, user, o, now)
    return await _open(conn, cfg, user, o, now, withdraw=False)


async def _open(conn: sqlite3.Connection, cfg: Config, user, o: dict, now: int, withdraw: bool) -> dict:
    """A ZarinPal payment for order `o`'s quote; `withdraw`: the order was made for this payment, so a refused
    request withdraws it, and so does the payment dying later (_close) once no payment on the order is still going."""
    amount = int(round(o["quoted_amount"]))
    try:
        authority = await zarinpal.request(cfg.zarinpal.merchant_id(), amount,
                                           f"Claude Gateway: {_label(cfg, o['tier'])}, {o['length']}, order #{o['id']}",
                                           f"{cfg.listener.home_url.rstrip('/')}/pay/callback", o["email"], o["id"],
                                           cfg.zarinpal.sandbox)
    except (zarinpal.ZarinpalUnavailable, zarinpal.ZarinpalRefused) as e:
        logger.warning("order #%d: ZarinPal payment request failed: %s", o["id"], e)
        if withdraw:
            orders.set_status(conn, user["id"], o["id"], "withdrawn")
        raise PaymentError(502, UNAVAILABLE) from e
    cur = conn.execute("INSERT INTO payments(order_id, user_id, created_at, updated_at, amount, authority, status, made_order) "
                       "VALUES(?,?,?,?,?,?,'started',?)", (o["id"], user["id"], now, now, amount, authority, int(withdraw)))
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
    referrals.spend(conn, p["order_id"], now)   # verified, so charged at the discounted price: the unit is used even ungranted
    return {**get(conn, p["id"]), "changed": True}


def _close(conn: sqlite3.Connection, p: dict, status: str, now: int, **cols) -> None:
    """A payment that will never be paid: its status, and its order withdrawn if still open, it was made for a
    payment (start, not pay_order on an order sent unpaid) and no other payment on it is still started (pay_order
    can open a second one; the order must outlive the first for the second to grant)."""
    _set(conn, p["id"], status, now, **cols)
    if orders.get(conn, p["order_id"])["status"] in orders.OPEN and _closes_order(conn, p["order_id"]):
        orders.set_status(conn, p["user_id"], p["order_id"], "withdrawn", now=now)
    db.audit(conn, p["user_id"], f"payment_{status}", f"order #{p['order_id']}", {"payment_id": p["id"], **cols})


def _closes_order(conn: sqlite3.Connection, order_id: int) -> bool:
    """Whether an order whose payments are all dead should be withdrawn: it was created for a payment, and none
    of its payments is still started."""
    made, started = conn.execute("SELECT MAX(made_order), SUM(status='started') FROM payments WHERE order_id=?",
                                 (order_id,)).fetchone()
    return bool(made) and not started


def _fulfil(conn: sqlite3.Connection, cfg: Config, p: dict, ref_id: str, card_pan: str | None, now: int) -> dict:
    """Verified: grant at the paid quote, and mark the payment paid inside the grant's transaction. A refused grant
    leaves the order open and the payment `paid_unfulfilled` (section 4, Grant)."""
    o = orders.get(conn, p["order_id"])
    buyer = conn.execute("SELECT * FROM users WHERE id=?", (p["user_id"],)).fetchone()

    def mark(tid):
        _set(conn, p["id"], "paid", now, ref_id=ref_id, card_pan=card_pan)
        db.audit(conn, p["user_id"], "payment_paid", f"order #{p['order_id']}", {"payment_id": p["id"], "ref_id": ref_id, "ticket_id": tid})
        referrals.spend(conn, p["order_id"], now)   # a verified payment uses the order's referral unit for good
    try:
        if buyer is None:
            raise tickets.TicketError("the account no longer exists")
        if o["upgrade_of"] is not None:
            tickets.upgrade(conn, cfg, None, buyer, o["upgrade_of"], o["tier"], CURRENCY, note=f"ZarinPal {ref_id}",
                            order_id=o["id"], paid=True, after=mark, now=now)
        else:
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


def expire_links(conn: sqlite3.Connection, now: int) -> int:
    """Decline open payment links an hour past their deadline with no payment going or gone through (links spec,
    section 5). Returns how many."""
    rows = conn.execute(
        f"SELECT o.id, o.admin_note FROM orders o WHERE o.pay_code IS NOT NULL AND o.status IN {orders.OPEN} AND o.pay_by<? AND NOT EXISTS "
        "(SELECT 1 FROM payments p WHERE p.order_id=o.id AND p.status IN ('started','paid','paid_unfulfilled'))",
        (now - LINK_EXPIRED_GRACE_S,)).fetchall()
    for oid, mine in rows:   # the admin's own note stays, trimmed so the reason still fits
        why = "Payment link expired"
        mine = (mine or "").strip()
        note = f"{mine[:orders.NOTE_MAX - len(why) - 3].rstrip()} · {why}" if mine else why
        orders.set_status(conn, None, oid, "declined", note=note, now=now)
    return len(rows)


async def reconcile(conn: sqlite3.Connection, cfg: Config, now: float | None = None) -> dict:
    """Payments still `started` an hour after they began (section 4, Expiry): the buyer never came back, or their
    verify could not reach ZarinPal. Each is verified with its stored amount: confirmed -> granted (or
    paid_unfulfilled); refused -> expired, its order withdrawn (_close's rule); unreachable -> left `started` for the next run.
    With payments off, or its order closed, it is closed without verifying. Returns counts per outcome, and in
    `changed` the ids THIS run moved to paid/paid_unfulfilled (the caller mails those, once). Then payment links an
    hour past their deadline are declined (expire_links)."""
    now = _now(now)
    out = {"paid": 0, "paid_unfulfilled": 0, "expired": 0, "cancelled": 0, "pending": 0, "errors": 0, "changed": [], "links_expired": 0}
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
    try:
        out["links_expired"] = expire_links(conn, now)
    except Exception:
        logger.exception("expiring payment links failed")
        out["links_expired"], out["errors"] = 0, out["errors"] + 1
    return out


def _toman(n) -> str:
    return f"{int(round(n)):,} Toman"


def vat(cfg: Config, amount) -> int:
    """The VAT inside a Toman amount: prices include it, so it is amount × p / (100 + p)."""
    p = cfg.zarinpal.vat_percent if cfg.zarinpal else 0
    return int(round(amount * p / (100 + p))) if p else 0


def _pct(p: float) -> str:
    return f"{p:g}%"


def _vat_line(cfg: Config, amount, lang: str) -> str:
    """'Includes … VAT.' for a receipt, or nothing without a VAT rate."""
    v = vat(cfg, amount)
    if not v:
        return ""
    p = cfg.zarinpal.vat_percent
    if lang == "fa":
        return f"این مبلغ شامل {orders.fa_amount(v, 'IRT')} مالیات بر ارزش افزوده ({orders.fa_num(_pct(p))}) است.\n"
    return f"Includes {_toman(v)} VAT ({_pct(p)}).\n"


def mails(conn: sqlite3.Connection, cfg: Config, p: dict, o: dict, ticket: dict | None, reason: str | None) -> list[tuple[str, str, str]]:
    """(to, subject, body) for a payment that just became paid or paid_unfulfilled (section 5)."""
    if cfg.email is None:
        return []
    up = o.get("upgrade_of")
    what = f"upgrade of pass #{up} to {_label(cfg, o['tier'])}" if up else f"{_label(cfg, o['tier'])}, {o['length']} ({LENGTHS.get(o['length'], '?')} days)"
    head = f"Paid: {what}, {_toman(p['amount'])}, ZarinPal reference {p['ref_id']}, by {o['name']} <{o['email']}>."
    if vat(cfg, p["amount"]):
        head += f"\n{_vat_line(cfg, p['amount'], 'en').strip()}"
    link = f"\n\nOrders: {cfg.listener.dashboard_url.rstrip('/')}/dashboard#orders" if cfg.listener.dashboard_url else ""
    if p["status"] == "paid_unfulfilled":
        return [(cfg.email.admin_to, f"Paid, pass NOT granted: order #{o['id']}",
                 f"{head}\n\nThe pass was NOT granted: {reason}. Grant it from the order or refund the payment.{link}\n")]
    out = [(cfg.email.admin_to, f"Paid: {_label(cfg, o['tier'])} {'upgrade' if up else o['length']}",
            f"{head}\nThe {'upgrade' if up else 'pass'} is live.{link}\n")]
    if ticket is not None and up:
        paid = (f"ممنون. پرداخت {orders.fa_amount(p['amount'], 'IRT')} شما انجام شد. کد پیگیری زرین‌پال: {p['ref_id']}\n{_vat_line(cfg, p['amount'], 'fa')}\n"
                if o.get("lang") == "fa" else f"Thank you. Your payment of {_toman(p['amount'])} went through. ZarinPal reference: {p['ref_id']}\n{_vat_line(cfg, p['amount'], 'en')}\n")
        return out + [(o["email"], *orders.upgraded_mail_text(cfg, o, ticket, paid))]
    if ticket is not None and o.get("lang") == "fa":   # the buyer's mail in the language of the page they paid from
        what_fa = f"{orders.fa_label(cfg, o['tier'])}، ۱ {orders.FA_LENGTHS.get(o['length'], o['length'])}"
        span = f"از {tickets.terms_date(ticket['starts_at'], 'fa')} تا {tickets.terms_date(ticket['ends_at'], 'fa')}"
        out.append((o["email"], f"پرداخت شما: {what_fa}",
                    f"ممنون. پرداخت {orders.fa_amount(p['amount'], 'IRT')} شما انجام شد.\n{_vat_line(cfg, p['amount'], 'fa')}\nبسته: {what_fa}، {span}.\n"
                    f"{orders.ref_discount_line_fa(o, ticket)}"
                    f"کد پیگیری زرین‌پال: {p['ref_id']}\n\n{tickets.terms_text(conn, cfg, 'fa', o['length'], ticket['starts_at'])}"))
    elif ticket is not None:
        span = f"{tickets._date(ticket['starts_at'])} to {tickets._date(ticket['ends_at'])}"
        out.append((o["email"], f"Your payment: {_label(cfg, o['tier'])} {o['length']}",
                    f"Thank you. Your payment of {_toman(p['amount'])} went through.\n{_vat_line(cfg, p['amount'], 'en')}\nPass: {what}, {span}.\n"
                    f"{orders.ref_discount_line_en(o, ticket)}"
                    f"ZarinPal reference: {p['ref_id']}\n\n{tickets.terms_text(conn, cfg, 'en', o['length'], ticket['starts_at'])}"))
    return out


async def send_mails(conn: sqlite3.Connection, cfg: Config, pid: int) -> None:
    """After the response: send what `mails` lists. A failure is logged and never touches the payment."""
    p = get(conn, pid)
    if p["status"] not in ("paid", "paid_unfulfilled"):
        return
    o = orders.get(conn, p["order_id"])
    t = conn.execute(f"SELECT {tickets.TICKET_COLS} FROM tickets t WHERE t.id=?", (o["ticket_id"],)).fetchone() if o["ticket_id"] else None
    for i, (to, subject, body) in enumerate(mails(conn, cfg, p, o, dict(t) if t else None, p["error"])):
        buyer = i == 1   # after the admin's: the buyer's, which carries the ticket's terms
        rich = tickets.mail_html(conn, cfg, o["lang"], o["length"], t["starts_at"], body) if buyer else None
        try:
            await asyncio.to_thread(mail.send, mail.servers(conn, cfg.email), to, subject, body, rich)
        except Exception as e:
            logger.warning("payment #%d: mail to %s failed: %s", pid, to, e)
