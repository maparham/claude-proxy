"""Order requests (design 2026-10-04): a buyer asks for a ticket, the admin turns the request into one.

An order is a request, not a payment and not a reservation: it holds no capacity, and the grant re-checks and re-prices
as usual. Creation, the limits (section 4), the status changes (section 3), linking a visitor order to an account
(section 7), the buyer's view (section 8) and retention. Writes that check before they change run in one
BEGIN IMMEDIATE transaction, so two requests at once cannot both pass a limit.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import sqlite3
import time

from . import db, mail, referrals, tickets
from .config import LENGTHS, Config

logger = logging.getLogger("claude_proxy")

DAY = 86400
NAME_MAX = 100
MESSAGE_MAX = 1000
NOTE_MAX = 200
EMAIL_MAX = 254
# No separators or address syntax (, ; : < > ( ) [ ] " \): a mail header would read them as several recipients.
EMAIL = re.compile(r'^[^@\s,;:<>()\[\]"\\]+@[^@\s,;:<>()\[\]"\\]+\.[^@\s,;:<>()\[\]"\\]+$')
VISITOR_PER_IP = VISITOR_PER_EMAIL = 10  # per 24 hours
VISITOR_GLOBAL = 200                     # all visitor orders together, per 24 hours
USER_PER_DAY = 10                        # a signed-in user's orders per 24 hours, withdrawn ones included: each mails the admin
BUYER_MAILS = 3                          # confirmations to one address per 24 hours
IP_KEEP_S = 30 * DAY
PAYLINK_MAX_S = 30 * DAY                 # a payment link's deadline is at most this far out

STATUSES = ("new", "contacted", "done", "declined", "withdrawn")
OPEN = ("new", "contacted")
MAIL_STATES = ("pending", "sent", "failed", "off", "skipped")
# Status changes other than `done`, which only a grant makes (close_for_grant). Anything else is refused.
TRANSITIONS = {"contacted": ("new",), "declined": OPEN, "withdrawn": OPEN}

TOO_MANY = "Too many orders today; try again tomorrow or sign in."
USER_TOO_MANY = "Too many orders today; try again tomorrow."
BUSY = "Orders are busy today; please sign in to order."
CHANGED = "This order was withdrawn or changed; reload."
LINK_INVALID = "This payment link is no longer valid."
UNAVAILABLE = "Passes are temporarily unavailable; try again in a few minutes."
# A visitor order: placed without an account (user_id NULL until linked) or still carrying its IP (cleared on close).
# One the admin has both linked and closed drops out of the visitor limits, which only loosens them for a handled buyer.
# A payment link is the admin's, never a visitor's, though it has no account until claimed.
VISITOR = "(pay_code IS NULL AND (user_id IS NULL OR ip IS NOT NULL))"


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

def visitor_limited(conn: sqlite3.Connection, ip: str | None, now: float | None = None) -> None:
    """The visitor limits that need no form data (per IP, global), read-only: checked before the Turnstile call so a
    flood costs no Cloudflare round trip. create() checks them again, with the per-email one, inside its transaction."""
    since = _now(now) - DAY
    if ip is not None and _count(conn, f"{VISITOR} AND ip=? AND created_at>?", (ip, since)) >= VISITOR_PER_IP:
        raise OrderError(429, TOO_MANY)
    if _count(conn, f"{VISITOR} AND created_at>?", (since,)) >= VISITOR_GLOBAL:
        raise OrderError(429, BUSY)


def _check_on_sale(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, now: float, user_id: int | None, start_now: bool) -> None:
    """Judged as the price list judges it (spec section 2, Sold out; 2026-11-04 spec §2): without a fresh weekly
    report nothing is on sale; else a pass starting now, without the buyer's used-up tickets, and one starting
    where the grant will put it. A pass queued after the buyer's current one is judged only at that start
    (2026-11-07): a full 5-hour window now says nothing about a pass starting after its reset."""
    from . import capacity
    if not capacity.context(conn, cfg, tickets.ACCOUNT_ID, now).fresh:
        raise OrderError(409, UNAVAILABLE)
    starts_at, replaces = tickets.next_start(conn, cfg, user_id, now, start_now) if user_id is not None else (now, [])
    if starts_at > now:
        if tickets.sold_out(conn, cfg, tier, length, now, starts_at, [t["id"] for t in replaces]):
            raise OrderError(409, "That pass is sold out for when your current pass ends.")
        return
    miss = tickets._misfit(conn, cfg, tier, length, now, None, [t["id"] for t in replaces] or None)
    if isinstance(miss, tickets.FiveHourFull):
        raise OrderError(409, f"That pass is sold out until the 5-hour reset; try again in "
                              f"{max(1, math.ceil((miss.resets_at - now) / 60))} minutes.")
    if miss is not None:
        raise OrderError(409, "That pass is sold out.")


def create(conn: sqlite3.Connection, cfg: Config, *, tier: str, length: str, currency: str, name: str, email: str, message: str = "",
           user_id: int | None = None, ip: str | None = None, notify: bool = True, now: float | None = None,
           terms_version: str | None = None, lang: str = "en", start_now: bool = False, via: str = "web") -> dict:
    """Store an order for a signed-in user (`user_id`) or a visitor (`ip`), quoted at today's price and rate.
    Refused: bad input (400), a user's second open order or a sold-out ticket (409), a visitor over a limit (429).
    `terms_version`: the ticket terms the buyer ticked; equality-checked against tickets.TERMS_VERSION, missing or
    stale → 400 (2026-10-13 spec, §3). `notify=False`: no new-order emails (an order paid online).
    `lang`: the page's language; the buyer's emails use it (anything but "fa" is English).
    `start_now`: the ticket is to start when granted, beside the buyer's
    running tickets, not after them (tickets.grant). A visitor has none: theirs never starts now.
    `via`: where the buyer ordered, 'web' or 'telegram' (the bot tells them when it is paid)."""
    now = _now(now)
    start_now = bool(start_now) and user_id is not None
    if terms_version != tickets.TERMS_VERSION:
        raise OrderError(400, tickets.TERMS_CHANGED)
    if tier not in cfg.tickets.tiers:
        raise OrderError(400, f"Unknown tier {tier!r}.")
    if length not in LENGTHS:
        raise OrderError(400, f"Unknown length {length!r}; one of {', '.join(LENGTHS)}.")
    steps = tickets.currencies(cfg)
    if currency not in steps:
        raise OrderError(400, f"Unknown currency {currency!r}.")
    name, email, message = _one_line(name), _email(email), str(message or "").strip()
    lang = "fa" if lang == "fa" else "en"
    via = "telegram" if via == "telegram" else "web"
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
            if _count(conn, "user_id=? AND created_at>?", (user_id, since)) >= USER_PER_DAY:   # or withdraw-and-reorder floods the admin
                raise OrderError(429, USER_TOO_MANY)
        else:
            # Limit messages carry no numbers (spec section 9).
            if _count(conn, f"{VISITOR} AND ip=? AND created_at>?", (ip, since)) >= VISITOR_PER_IP \
                    or _count(conn, f"{VISITOR} AND lower(email)=lower(?) AND created_at>?", (email, since)) >= VISITOR_PER_EMAIL:
                raise OrderError(429, TOO_MANY)
            if _count(conn, f"{VISITOR} AND created_at>?", (since,)) >= VISITOR_GLOBAL:
                raise OrderError(429, BUSY)
        rate = tickets.current_rate(conn, currency)
        _check_on_sale(conn, cfg, tier, length, now, user_id, start_now)
        if rate is None:
            raise OrderError(400, f"No exchange rate for {currency} yet; choose another currency.")
        usd = tickets.price_now(conn, tier, length, now)["usd"]
        # A signed-in buyer's referral discount (friend or unit), taken on top of any sale, inside this transaction so
        # the unit it holds cannot be taken twice (referrals design: no double use). VAT goes on after it.
        ref = referrals.quote(conn, cfg, user_id, tier, length, usd, now) if user_id is not None else None
        if ref is not None:
            usd = ref["usd"]   # unchanged when the cost floor left no room (apply_pct returns usd itself)
        amount = tickets.round_local(usd, rate["rate"], steps[currency], tickets.vat_percent(cfg))
        if cfg.email is None or not notify:   # notify=False: a payment order, mailed when paid instead (payments design, section 5)
            admin_mail = buyer_mail = "off"
        else:
            # The cap exists for signed-in users, who have no per-email order limit (spec section 5, Buyer cap).
            sent = _count(conn, "lower(email)=lower(?) AND buyer_mail IN ('sent','pending') AND created_at>?", (email, since))
            admin_mail, buyer_mail = "pending", "skipped" if sent >= BUYER_MAILS else "pending"
        cur = conn.execute(
            "INSERT INTO orders(created_at, updated_at, user_id, name, email, tier, length, currency, quoted_usd, quoted_rate, quoted_amount, "
            "message, status, ip, admin_mail, buyer_mail, terms_version, lang, start_now, via) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?,?,?,?,?,?)",
            (now, now, user_id, name, email, tier, length, currency, usd, rate["rate"], amount, message,
             None if user_id is not None else ip, admin_mail, buyer_mail, terms_version, lang, int(start_now), via))
        oid = cur.lastrowid
        referrals.hold(conn, oid, ref, now)
        db.audit(conn, user_id, "order_new", f"order #{oid}", {"tier": tier, "length": length, "currency": currency, "amount": amount,
                                                              "visitor": user_id is None, "terms_version": terms_version,
                                                              **({"start_now": True} if start_now else {})})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get(conn, oid)


# ---------- payment links (spec 2026-11-04) ----------

def mask_email(email: str) -> str:
    """bob@example.com -> bo…@example.com: enough for the right person to recognise, not to read off."""
    local, _, domain = str(email).partition("@")
    return f"{local[:1 if len(local) <= 2 else 2]}…@{domain}"


def link_price(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, currency: str, now: float) -> tuple[float, int, dict]:
    """What a pass bought now costs: (USD charged, whole local amount with VAT, the rate row). OrderError 400 for an
    unknown tier or length or a currency without a rate."""
    if tier not in cfg.tickets.tiers:
        raise OrderError(400, f"Unknown tier {tier!r}.")
    if length not in LENGTHS:
        raise OrderError(400, f"Unknown length {length!r}; one of {', '.join(LENGTHS)}.")
    rate = tickets.current_rate(conn, currency)
    if rate is None:
        raise OrderError(400, f"No exchange rate for {currency} yet.")
    usd = tickets.price_now(conn, tier, length, now)["usd"]
    return usd, int(tickets.round_local(usd, rate["rate"], tickets.currencies(cfg)[currency], tickets.vat_percent(cfg))), rate


def create_link(conn: sqlite3.Connection, cfg: Config, actor: int | None, *, email, tier, length, pay_by, amount=None,
                note="", currency: str = "IRT", now: float | None = None) -> dict:
    """An admin's payment link (spec section 3): an unclaimed order for `email` at the list price or `amount`, payable
    until `pay_by`. No mail. The caller checks that online payment can take it (payments.create_link). Without a
    fresh weekly report this says unavailable, not sold out, like _check_on_sale (final review, finding 2)."""
    now = _now(now)
    email, note = _email(email), _note(note)
    usd, listed, rate = link_price(conn, cfg, str(tier or ""), str(length or ""), currency, now)
    try:
        pay_by = int(pay_by)
    except (TypeError, ValueError):
        raise OrderError(400, "Give the deadline as a date and time.") from None
    if not now < pay_by <= now + PAYLINK_MAX_S:
        raise OrderError(400, "The deadline must be in the next 30 days.")
    edited = amount not in (None, "")
    if edited:
        try:
            amount = int(str(amount))
        except ValueError:
            raise OrderError(400, "The price is a whole number of Toman.") from None
        if not 1000 <= amount <= listed * 10:
            raise OrderError(400, f"The price must be between 1,000 and {listed * 10:,} Toman.")
        usd = amount / (rate["rate"] * (1 + tickets.vat_percent(cfg) / 100))
    else:
        amount = listed
    conn.execute("BEGIN IMMEDIATE")
    try:
        from . import capacity
        if not capacity.context(conn, cfg, tickets.ACCOUNT_ID, now).fresh:
            raise OrderError(409, UNAVAILABLE)
        if tickets.sold_out(conn, cfg, tier, length, now, None, None):
            raise OrderError(409, "That pass is sold out.")
        cur = conn.execute(
            "INSERT INTO orders(created_at, updated_at, user_id, name, email, tier, length, currency, quoted_usd, quoted_rate, "
            "quoted_amount, message, status, ip, admin_mail, buyer_mail, terms_version, lang, start_now, admin_note, pay_code, pay_by) "
            "VALUES(?,?,NULL,?,?,?,?,?,?,?,?,'','new',NULL,'off','off',?,'fa',0,?,?,?)",
            (now, now, email, email, tier, length, currency, usd, rate["rate"], amount, tickets.TERMS_VERSION, note,
             secrets.token_urlsafe(16), pay_by))
        oid = cur.lastrowid
        db.audit(conn, actor, "order_paylink", f"order #{oid}", {"amount": amount, "edited": edited, "pay_by": pay_by})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get(conn, oid)


# ---------- status changes (spec section 3) ----------

def _change(conn, actor, order_id: int, to: str, note, now: int) -> dict:
    """Inside a transaction: one allowed status change, the optional note for a decline, the IP cleared on close, the audit row."""
    o = get(conn, order_id)
    if o["status"] not in TRANSITIONS.get(to, ()):
        raise OrderError(409, f"An order that is {o['status']} cannot become {to}.")
    sets, args = ["status=?", "updated_at=?"], [to, now]
    if to == "declined":
        note = _note(note)
        if note:   # optional; a blank one keeps whatever note is already there
            sets.append("admin_note=?")
            args.append(note)
    if to not in OPEN:
        sets.append("ip=NULL")
    conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", (*args, order_id))
    db.audit(conn, actor, f"order_{to}", f"order #{order_id}", {"from": o["status"]})
    if to in ("declined", "withdrawn"):   # a held referral unit goes back, unless a payment on the order went through
        referrals.release(conn, order_id, now)
    return get(conn, order_id)


def set_status(conn: sqlite3.Connection, actor: int | None, order_id: int, to: str, *, note=None, now: float | None = None) -> dict:
    """`contacted` (from new), `declined` (optionally with a note) or `withdrawn` (from new or contacted); anything else 409."""
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
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE orders SET admin_note=?, updated_at=? WHERE id=?", (note, _now(now), order_id))
        db.audit(conn, actor, "order_note", f"order #{order_id}", {"note": note})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
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
    up = order.get("upgrade_of")
    head = (f"New order #{order['id']}: upgrade of pass #{up} to {label} for its time left" if up else
            f"New order #{order['id']}: {label}, {order['length']} ({LENGTHS.get(order['length'], '?')} days)")
    lines = [head, "",
             f"Name: {order['name']}", f"Email: {order['email']}", f"Account: {order.get('user_name') or 'none'}",
             f"Quoted: {_amount(order)} (${order['quoted_usd']:g} at {order['quoted_rate']:g})", "",
             "Message:", order["message"] or "(none)"]
    if cfg.listener.dashboard_url:
        lines += ["", f"Orders: {cfg.listener.dashboard_url.rstrip('/')}/dashboard#orders"]
    return f"New order: {label} {'upgrade' if up else order['length']}", "\n".join(lines) + "\n"


# Persian for the buyer's emails: the dashboard's own words (i18n.js tier.*, price.1_*, cur.toman).
FA_TIERS = {"lite": ("Lite", "اقتصادی"), "standard": ("Standard", "استاندارد")}   # translated only while the label is the default
FA_LENGTHS = {"day": "روز", "week": "هفته", "month": "ماه"}
fa_num = tickets.fa_num


def fa_label(cfg: Config, tier: str) -> str:
    t = cfg.tickets.tiers.get(tier)
    label = t.label if t else tier
    default, fa = FA_TIERS.get(tier, (None, None))
    return fa if label == default else label


def fa_amount(amount: float, currency: str) -> str:
    if currency == "IRT":
        return f"{fa_num(f'{round(amount):,}')} تومان"
    return f"{fa_num(f'{amount:,.2f}')} {currency}"


# ---------- the referral discount line in a buyer's email (referrals design, task 6) ----------

def ref_discount_usd(order: dict, ticket: dict | None = None) -> tuple[float, float]:
    """(the price the referral discount came off, the referral discount actually sold at), in USD. From the granted
    ticket's own ref_off_usd when there is one - the referral part alone, set at grant (re-floored on an unpaid grant,
    the order's own on a paid one) - never orders.ref_off_usd once a ticket exists: a unit floored to 0% at a manual
    grant keeps ref_off_usd on the order though the pass sold at full price (controller ruling). Not list_usd - usd
    either: during a sale (ticket_discounts) that also counts the sale (task 6 fix round 1). Without a ticket yet (the
    order-placed email), the order's own list_usd and ref_off_usd, recorded when the order was created. (0, 0) when
    the order carries no referral discount at all (ref_kind unset)."""
    if not order.get("ref_kind"):
        return 0.0, 0.0
    if ticket is not None:
        off = ticket["ref_off_usd"] or 0.0
        return round(ticket["usd"] + off, 2), off
    return order.get("list_usd") or 0.0, order.get("ref_off_usd") or 0.0


def ref_discount_line_en(order: dict, ticket: dict | None = None) -> str:
    """'Referral discount: X% (−$Y)\\n', or '' when the order carries no referral discount, or it came to 0."""
    before, off = ref_discount_usd(order, ticket)
    if off <= 0:
        return ""
    pct = round(off / before * 100) if before else 0
    return f"Referral discount: {pct}% (−${off:.2f})\n"


def ref_discount_line_fa(order: dict, ticket: dict | None = None) -> str:
    before, off = ref_discount_usd(order, ticket)
    if off <= 0:
        return ""
    pct = round(off / before * 100) if before else 0
    return f"تخفیف معرفی دوستان: {fa_num(pct)}٪ (−${fa_num(f'{off:.2f}')})\n"


def buyer_mail_text(conn: sqlite3.Connection, cfg: Config, order: dict) -> tuple[str, str]:
    """(subject, body) for the buyer, in the language of the page they ordered from. Nothing the buyer typed goes in
    (no name, no message), so the form cannot carry content to a stranger's inbox."""
    if order.get("lang") == "fa":
        return _buyer_mail_fa(conn, cfg, order)
    label = _label(cfg, order)
    up = order.get("upgrade_of")
    lines = [f"Your order: an upgrade of your pass to {label} for the rest of it." if up else
             f"Your order: a {label} pass for one {order['length']}{_days(order['length'])}.",
             f"Price at today's rate: {_amount(order)}.", *([ref] if (ref := ref_discount_line_en(order).rstrip("\n")) else []), ""]
    if not up:   # an upgrade keeps its ticket's weeks: the terms the buyer got with it still hold
        lines.append(tickets.terms_text(conn, cfg, "en", order["length"], None))   # it starts when granted: no dates yet
    lines.append("Your order is not paid yet. The admin will contact you about the next step.")
    return f"Your order: {label} {'upgrade' if up else order['length']}", "\n".join(lines) + "\n"


def _days(length: str) -> str:
    """` (7 days)` after a length, but nothing after a day: "one day (1 days)" says nothing."""
    return "" if length == "day" else f" ({LENGTHS.get(length, '?')} days)"


def _days_fa(length: str) -> str:
    return "" if length == "day" else f" ({fa_num(LENGTHS.get(length, '?'))} روز)"


def _buyer_mail_fa(conn: sqlite3.Connection, cfg: Config, order: dict) -> tuple[str, str]:
    label, length = fa_label(cfg, order["tier"]), FA_LENGTHS.get(order["length"], order["length"])
    up = order.get("upgrade_of")
    lines = [f"سفارش شما: ارتقای بسته شما به {label} برای باقی‌مانده آن." if up else
             f"سفارش شما: بسته {label} برای ۱ {length}{_days_fa(order['length'])}.",
             f"قیمت: {fa_amount(order['quoted_amount'], order['currency'])}.", *([ref] if (ref := ref_discount_line_fa(order).rstrip("\n")) else []), ""]
    if not up:
        lines.append(tickets.terms_text(conn, cfg, "fa", order["length"], None))
    lines.append("سفارش شما هنوز پرداخت نشده است. ادمین برای مرحله بعد با شما تماس می‌گیرد.")
    return f"سفارش شما: {label}، {'ارتقا' if up else f'۱ {length}'}", "\n".join(lines) + "\n"


def windows_setup(cfg: Config, order: dict) -> str:
    """How to set up a Windows computer with the double-click installer (the dashboard's /install.cmd), as a block for
    the end of every pass mail: the buyer may pay on one device and install on another. Nothing on a gateway without
    the URLs the installer needs."""
    if not (cfg.listener.public_url and cfg.listener.dashboard_url):
        return ""
    url = f"{cfg.listener.dashboard_url.rstrip('/')}/install.cmd"
    if order.get("lang") == "fa":
        return ("\nنصب روی Windows:\n"
                f"۱. فایل نصب را دانلود کنید: {url}\n"
                "۲. فایل دانلودشده (claude-gateway-setup.cmd) را باز کنید. اگر Windows پیام «Windows protected your PC» نشان داد، "
                "روی «More info» و بعد «Run anyway» بزنید.\n"
                "۳. صفحه‌ای در مرورگر باز می‌شود: وارد شوید و تأیید را بزنید.\n"
                "۴. وقتی کار پنجره تمام شد، یک پنجره تازه PowerShell باز کنید و gclaude را بنویسید.\n")
    return ("\nSet up on Windows:\n"
            f"1. Download the installer: {url}\n"
            "2. Open the downloaded file (claude-gateway-setup.cmd). If Windows says \"Windows protected your PC\", "
            "click \"More info\", then \"Run anyway\".\n"
            "3. A page opens in your browser: sign in and click Authorize.\n"
            "4. When the window is done, open a new PowerShell window and type gclaude.\n")


def granted_mail_text(conn: sqlite3.Connection, cfg: Config, order: dict, ticket: dict) -> tuple[str, str]:
    """(subject, body) for the buyer once the admin grants their order: the ticket's dates and its terms, plus the
    referral discount line taken from the granted ticket itself (ref_discount_usd), not the order's own ref_off_usd -
    a unit floored to 0% at this manual grant must show nothing, not the discount the order was quoted before the
    floor caught up with it (controller ruling)."""
    if order.get("upgrade_of"):
        return upgraded_mail_text(cfg, order, ticket)
    terms = tickets.terms_text(conn, cfg, "fa" if order.get("lang") == "fa" else "en", order["length"], ticket["starts_at"])
    if order.get("lang") == "fa":
        what = f"{fa_label(cfg, order['tier'])}، ۱ {FA_LENGTHS.get(order['length'], order['length'])}"
        span = f"از {tickets.terms_date(ticket['starts_at'], 'fa')} تا {tickets.terms_date(ticket['ends_at'], 'fa')}"
        return f"بسته شما فعال شد: {what}", f"سفارش شما انجام شد.\n\nبسته: {what}، {span}.\n{ref_discount_line_fa(order, ticket)}\n{terms}{windows_setup(cfg, order)}"
    what = f"{_label(cfg, order)}, {order['length']}{_days(order['length'])}"
    span = f"{tickets._date(ticket['starts_at'])} to {tickets._date(ticket['ends_at'])}"
    return (f"Your pass is granted: {_label(cfg, order)} {order['length']}",
            f"Your order is done.\n\nPass: {what}, {span}.\n{ref_discount_line_en(order, ticket)}\n{terms}{windows_setup(cfg, order)}")


def upgraded_mail_text(cfg: Config, order: dict, ticket: dict, paid: str = "") -> tuple[str, str]:
    """(subject, body) for the buyer once their upgrade is in: the tier and when the ticket ends. `paid`: a line about
    the payment, put first."""
    end = ticket.get("effective_end") or ticket["ends_at"]
    if order.get("lang") == "fa":
        label = fa_label(cfg, order["tier"])
        return (f"بسته شما ارتقا یافت: {label}",
                f"{paid}سفارش شما انجام شد.\n\nبسته شما از حالا تا {tickets.terms_date(end, 'fa')} با سطح {label} کار می‌کند.\n"
                f"{windows_setup(cfg, order)}")
    label = _label(cfg, order)
    return (f"Your pass is upgraded: {label}",
            f"{paid}Your order is done.\n\nYour pass now runs at {label} until {tickets._date(end)}.\n{windows_setup(cfg, order)}")


def closed_mail_text(cfg: Config, order: dict) -> tuple[str, str]:
    """(subject, body) for the buyer once their order is withdrawn (by them) or declined (by the admin), in the
    language of the page they ordered from. Never the admin's note."""
    up, declined = order.get("upgrade_of"), order["status"] == "declined"
    if order.get("lang") == "fa":
        what = f"{fa_label(cfg, order['tier'])}، " + ("ارتقا" if up else f"۱ {FA_LENGTHS.get(order['length'], order['length'])}")
        if declined:
            return f"سفارش شما رد شد: {what}", f"سفارش شما ({what}) رد شد.\n"
        return f"سفارش شما کنسل شد: {what}", f"سفارش شما ({what}) کنسل شد. ادمین دیگر روی آن کاری نمی‌کند.\n"
    what = f"{_label(cfg, order)} {'upgrade' if up else order['length']}"
    if declined:
        return f"Your order is declined: {what}", f"Your order ({what}) was declined.\n"
    return f"Your order is withdrawn: {what}", f"Your order ({what}) is withdrawn. The admin will no longer act on it.\n"


def admin_withdrawn_mail_text(cfg: Config, order: dict) -> tuple[str, str]:
    """(subject, body) for the admin once the buyer withdraws. `order` may carry `user_name`."""
    what = f"{_label(cfg, order)} {'upgrade' if order.get('upgrade_of') else order['length']}"
    lines = [f"Order #{order['id']} withdrawn by the buyer: {what}", "",
             f"Name: {order['name']}", f"Email: {order['email']}", f"Account: {order.get('user_name') or 'none'}"]
    if cfg.listener.dashboard_url:
        lines += ["", f"Orders: {cfg.listener.dashboard_url.rstrip('/')}/dashboard#orders"]
    return f"Order withdrawn: {what}", "\n".join(lines) + "\n"


async def send_closed(conn: sqlite3.Connection, cfg: Config, order_id: int) -> None:
    """After a withdraw's or a decline's response: tell the buyer, and on a withdraw the admin. The buyer not when their
    order confirmation was over the cap (buyer_mail skipped), so withdraw-and-reorder cannot flood an inbox, nor for an
    unpaid online-payment order (mails off); the admin not when never mailed the order (admin_mail off).
    A failure is logged and never touches the order."""
    if cfg.email is None:
        return
    o = get(conn, order_id)
    if o["user_id"] is not None:
        o["user_name"] = db._user_name(conn, o["user_id"])
    jobs = []
    if o["buyer_mail"] not in ("skipped", "off"):
        jobs.append(("buyer", o["email"], *closed_mail_text(cfg, o)))
    if o["status"] == "withdrawn" and o["admin_mail"] != "off":
        jobs.append(("admin", cfg.email.admin_to, *admin_withdrawn_mail_text(cfg, o)))
    for who, to, subject, body in jobs:
        try:
            await asyncio.to_thread(mail.send, mail.servers(conn, cfg.email), to, subject, body)
        except Exception as e:
            logger.warning("order #%d: %s mail to the %s (%s) failed: %s", order_id, o["status"], who, to, e)


async def send_granted(conn: sqlite3.Connection, cfg: Config, order_id: int) -> None:
    """After the grant's response: tell the buyer their ticket is in. A failure is logged and never touches the order."""
    if cfg.email is None:
        return
    o = get(conn, order_id)
    t = conn.execute(f"SELECT {tickets.TICKET_COLS} FROM tickets t WHERE t.id=?", (o["ticket_id"],)).fetchone() if o["ticket_id"] else None
    if t is None:
        return
    subject, body = granted_mail_text(conn, cfg, o, dict(t))
    rich = tickets.mail_html(conn, cfg, o["lang"], o["length"], t["starts_at"], body)
    try:
        await asyncio.to_thread(mail.send, mail.servers(conn, cfg.email), o["email"], subject, body, rich)
    except Exception as e:
        logger.warning("order #%d: granted mail to %s failed: %s", order_id, o["email"], e)


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
        jobs.append(("buyer_mail", o["email"], *buyer_mail_text(conn, cfg, o)))
    for which, to, subject, body in jobs:
        rich = tickets.mail_html(conn, cfg, o["lang"], o["length"], None, body) if which == "buyer_mail" else None   # as its text: undated
        try:
            await asyncio.to_thread(mail.send, mail.servers(conn, cfg.email), to, subject, body, rich)
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
    Creating is safe though the address is unverified: only its owner can later sign in to that account through Clerk,
    and it has a $0 credit, so nothing is served before a ticket is granted.
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
            # No credit until a ticket is granted: the first ticket removes this, as it does a sign-up's.
            conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at, updated_by) VALUES(?,?,?,?,?,?,?)",
                         (user_id, "cost_total", "*", "0", "usd", int(now), actor))
            db.audit(conn, actor, "user_add", email, {"role": "user", "order_id": order_id})
        u = conn.execute("SELECT id, name, revoked_at FROM users WHERE id=?", (user_id,)).fetchone()
        if u is None:
            raise OrderError(404, f"No user #{user_id}.")
        if u["revoked_at"] is not None:
            raise OrderError(409, f"{u['name']} is revoked; link the order to an active account.")
        conn.execute("UPDATE orders SET user_id=?, updated_at=? WHERE id=?", (u["id"], now, order_id))
        db.audit(conn, actor, "order_link", f"order #{order_id}", {"user": u["name"], "created": bool(create)})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get(conn, order_id)


# ---------- the buyer's view (spec section 8) ----------

# Not name, email or message: an order linked to the wrong account must not show that account the visitor's details.
BUYER_FIELDS = ("id", "created_at", "updated_at", "tier", "length", "currency", "quoted_usd", "quoted_rate", "quoted_amount", "status")


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
    return {k: row[k] for k in BUYER_FIELDS} | {"upgrade": row["upgrade_of"] is not None}   # no ticket ids: they number every sale


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
