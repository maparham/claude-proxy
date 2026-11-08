"""Telegram bot (design 2026-11-05): buy a pass, send feedback, get support.

One Telegram object per dashboard app, on the dashboard's loop and connection (section 2), the same ones the payment
callback and reconcile use. Every update is handled inline, under its chat's lock, after its update_id is recorded,
so a delivery Telegram retries is handled once.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import secrets
import sqlite3
import time
import weakref

import httpx

from . import blog, db, orders, passcard, payments, referrals, refunds, tickets
from .config import Config

logger = logging.getLogger("claude_proxy")

API = "https://api.telegram.org"
MSG_PER_HOUR, PAY_PER_HOUR = 20, 5
CONFIRM_EVERY_S = 600   # a support message is confirmed at most this often per chat
SUPPORT_WINDOW_S = 1800   # after the Support button (or an admin's reply), messages go to support for this long
TEXT_MAX = 3500

# The admin group reads English (section 7).
ANON = "Turn off 'Remain anonymous' to answer: the bot can't tell who you are."
NOT_DELIVERED = "Not delivered; try again."
NOT_DELIVERED_BLOCKED = "Not delivered: the user blocked the bot."
NOT_DELIVERED_UNSUPPORTED = "Not delivered: that message type isn't supported."


class Blocked(Exception):
    """The user blocked the bot (a 403)."""


def on(cfg: Config) -> bool:
    t = cfg.telegram
    return t is not None and bool(t.token()) and bool(t.webhook_secret()) and bool(cfg.listener.dashboard_url)


class _MaskToken(logging.Filter):
    """httpx logs every request URL at INFO, and a Bot API URL holds the token (section 8). Only the arg(s) that
    actually hold the token are rewritten (to a masked str); every other arg is left with its original type, since
    httpx's own format string mixes %s and %d placeholders and a stray str there breaks logging entirely."""
    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def _mask(self, value):
        s = str(value)
        return s.replace(self.token, "<token>") if self.token in s else value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and self.token in record.msg:
            record.msg = record.msg.replace(self.token, "<token>")
        if isinstance(record.args, dict):
            record.args = {k: self._mask(v) for k, v in record.args.items()}
        elif record.args:
            record.args = tuple(self._mask(a) for a in record.args)
        return True


class Bot:
    def __init__(self, token: str, http: httpx.AsyncClient):
        self.token, self.http = token, http
        log = logging.getLogger("httpx")
        if not any(isinstance(f, _MaskToken) and f.token == token for f in log.filters):
            log.addFilter(_MaskToken(token))

    async def call(self, method: str, **params):
        """The method's result, or None after a failure, which is logged by method name only. A 429 is retried once
        after its retry_after (at most 5 s); a 403 raises Blocked."""
        for attempt in range(2):
            try:
                r = await self.http.post(f"{API}/bot{self.token}/{method}", json=params, timeout=15)
                body = r.json()
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("telegram %s failed: %s", method, type(e).__name__)
                return None
            if body.get("ok"):
                return body.get("result")
            code = body.get("error_code")
            if code == 429 and attempt == 0:
                await asyncio.sleep(min(5, (body.get("parameters") or {}).get("retry_after", 1)))
                continue
            if code == 403:
                raise Blocked(str(body.get("description", "")))
            logger.warning("telegram %s: %s %s", method, code, body.get("description"))
            return None
        return None


TEXTS = {
    "fa": {
        "menu": "چه کاری برایتان انجام دهم؟\nبرای پیام به پشتیبانی، «پشتیبانی» را بزنید.",
        "btn_buy": "خرید بسته", "btn_mine": "بسته‌های من", "btn_feedback": "ارسال نظر", "btn_lang": "English",
        "btn_support": "پشتیبانی",
        "ask_support": "پیامتان را برای پشتیبانی بنویسید (متن، عکس، فایل یا پیام صوتی):",
        "btn_prices": "قیمت‌ها", "btn_site": "سایت رهکار",
        "prices_head": "💳 قیمت بسته‌ها",
        "prices_same": "هم‌اندازه {plan}", "prices_times": "{n} برابر {plan}", "prices_pct": "حدود {pct}٪ {plan}",
        "per_day": "روزانه", "per_week": "هفتگی", "per_month": "ماهانه",
        "prices_off": "🏷 {pct}٪ تخفیف", "prices_sold": "⛔ فعلاً تمام شده", "prices_vat": "قیمت‌ها با {vat}٪ مالیات",
        "prices_site": "قیمت‌ها را در سایت ببینید:\n{url}",
        "lang_set": "زبان: فارسی",
        "cancelled": "لغو شد.",
        "stale": "این دکمه دیگر معتبر نیست.",
        "too_many": "پیام‌ها زیاد شده؛ کمی بعد دوباره امتحان کنید.",
        "need_link": "برای خرید، اول حسابتان را در داشبورد به تلگرام وصل کنید: روی نامتان بزنید و «اتصال تلگرام» را انتخاب کنید.\n{url}",
        "link_bad": "این لینک منقضی شده است؛ در داشبورد دوباره «اتصال تلگرام» را بزنید.",
        "linked": "این تلگرام به حساب {who} وصل شد.",
        "relinked_away": "حساب شما به تلگرام دیگری وصل شد. اگر کار شما نبود، از داشبورد آن را تغییر دهید.",
        "unlinked": "اتصال این تلگرام به حسابتان برداشته شد.",
        "not_linked": "این تلگرام به حسابی وصل نیست.",
        "paused": "خرید آنلاین فعلاً متوقف است. اگر سؤالی دارید «پشتیبانی» را بزنید.",
        "sold_out": "همه بسته‌ها فعلاً فروخته شده‌اند.",
        "choose_tier": "کدام بسته؟", "choose_length": "چه مدت؟", "choose_start": "از کی شروع شود؟",
        "btn_after": "بعد از بسته فعلی", "btn_now": "همین حالا، کنار بسته فعلی",
        "len_day": "یک روز", "len_week": "یک هفته", "len_month": "یک ماه",
        "summary": "{label}، {length}\n{ref}مبلغ: {amount}\nشروع: {start}\n\nبا پرداخت، شرایط استفاده را می‌پذیرید:\n{terms}",
        "btn_pay": "شرایط را می‌پذیرم، پرداخت",
        "pay_ready": "لینک پرداخت آماده است:", "btn_open_pay": "پرداخت با زرین‌پال",
        "open_order": "یک سفارش پرداخت‌نشده دارید: {label}، {length}، {amount}.",
        "btn_continue": "ادامه پرداخت", "btn_cancel_order": "لغو و انتخاب دوباره",
        "order_busy": "یک سفارش در جریان دارید. اگر سؤالی دارید «پشتیبانی» را بزنید.",
        "ask_email": "حسابتان ایمیل ندارد. یک ایمیل برای رسید پرداخت بفرستید (یا /cancel):",
        "bad_email": "این ایمیل درست به نظر نمی‌رسد؛ دوباره بفرستید یا /cancel.",
        "paid": "پرداخت انجام شد (کد پیگیری زرین‌پال: {ref}). بسته شما از {start} تا {end} فعال است.",
        "unfulfilled": "پرداخت انجام شد (کد پیگیری زرین‌پال: {ref})، اما بسته خودکار شروع نشد. آن را شروع می‌کنیم یا مبلغ را برمی‌گردانیم؛ اگر سؤالی دارید «پشتیبانی» را بزنید.",
        "mine_none": "بسته فعال یا در صفی ندارید.",
        "mine_running": "{label}: تا {end}", "mine_queued": "{label}: در صف، از {start}",
        "btn_refund": "لغو و بازگشت وجه: {label}",
        "refund_ask": "{label} لغو شود؟ بسته همین حالا لغو می‌شود و زرین‌پال {amount} را به کارتی که با آن پرداخت کردید برمی‌گرداند. این کار قابل بازگشت نیست.",
        "btn_refund_yes": "بله، لغو شود", "btn_refund_no": "بسته را نگه دار",
        "refund_done": "انجام شد: بسته لغو شد و مبلغ به کارت شما برمی‌گردد. ممکن است نمایش آن در حساب بانکی‌تان کمی طول بکشد.",
        "mine_more": "جزئیات در داشبورد:\n{url}",
        "invite_off": "سیستم معرفی دوستان فعلاً خاموش است.",
        "invite_lede": "لینک‌تان را به اشتراک بگذارید: دوستی که از آن طریق ثبت‌نام کند {pct}٪ تخفیف در اولین بسته‌اش می‌گیرد.\n{link}",
        "invite_unit": "{pct}٪ تخفیف، انقضا {date}",
        "invite_waiting": "{n} در انتظار تأیید ادمین.",
        "summary_ref": "قیمت: {list}\nتخفیف معرفی دوستان ({pct}٪): −{off}",
        "ask_feedback": "نظرتان را بنویسید:",
        "got_support": "پیامتان به پشتیبانی رسید؛ جواب همین‌جا می‌آید.",
        "got_feedback": "ممنون، نظرتان رسید.",
        "too_long": "پیام طولانی است؛ لطفاً آن را در چند پیام کوتاه‌تر بفرستید.",
        "unsupported": "لطفاً متن، عکس، فایل یا پیام صوتی بفرستید.",
    },
    "en": {
        "menu": "What would you like to do?\nTo message support, tap Support.",
        "btn_buy": "Buy a pass", "btn_mine": "My passes", "btn_feedback": "Feedback", "btn_lang": "فارسی",
        "btn_support": "Support",
        "ask_support": "Write your message for support (text, a photo, a file or a voice note):",
        "btn_prices": "Prices", "btn_site": "Rahkar website",
        "prices_head": "💳 Pass prices",
        "prices_same": "same as {plan}", "prices_times": "{n}× {plan}", "prices_pct": "about {pct}% of {plan}",
        "per_day": "Daily", "per_week": "Weekly", "per_month": "Monthly",
        "prices_off": "🏷 {pct}% off", "prices_sold": "⛔ sold out for now", "prices_vat": "prices include {vat}% VAT",
        "prices_site": "See the prices on the website:\n{url}",
        "lang_set": "Language: English",
        "cancelled": "Cancelled.",
        "stale": "This button is no longer valid.",
        "too_many": "Too many messages; try again later.",
        "need_link": "To buy, first connect your account to Telegram in the dashboard: click your name, then Connect Telegram.\n{url}",
        "link_bad": "This link expired; press Connect Telegram again.",
        "linked": "This Telegram is now connected to the account {who}.",
        "relinked_away": "Your account was connected to another Telegram account. If this wasn't you, change it in the dashboard.",
        "unlinked": "This Telegram is no longer connected to your account.",
        "not_linked": "This Telegram is not connected to an account.",
        "paused": "Buying is paused for now. Tap Support if you have a question.",
        "sold_out": "All passes are sold out right now.",
        "choose_tier": "Which pass?", "choose_length": "How long?", "choose_start": "When should it start?",
        "btn_after": "After the current one", "btn_now": "Now, beside the current one",
        "len_day": "A day", "len_week": "A week", "len_month": "A month",
        "summary": "{label}, {length}\n{ref}Amount: {amount}\nStarts: {start}\n\nPaying means you accept the terms:\n{terms}",
        "btn_pay": "I agree, pay",
        "pay_ready": "Your payment link is ready:", "btn_open_pay": "Pay with ZarinPal",
        "open_order": "You have an unpaid order: {label}, {length}, {amount}.",
        "btn_continue": "Continue paying it", "btn_cancel_order": "Cancel it and choose again",
        "order_busy": "You have an order in progress. Tap Support if you have a question.",
        "ask_email": "Your account has no email. Send an address for the payment receipt (or /cancel):",
        "bad_email": "That email address does not look right; send it again or /cancel.",
        "paid": "Payment received (ZarinPal reference {ref}). Your pass is active from {start} until {end}.",
        "unfulfilled": "Payment received (ZarinPal reference {ref}), but the pass could not start automatically. We'll start it or refund you; tap Support with any questions.",
        "mine_none": "You have no running or queued pass.",
        "mine_running": "{label}: until {end}", "mine_queued": "{label}: queued, from {start}",
        "btn_refund": "Cancel and refund: {label}",
        "refund_ask": "Cancel {label}? The pass is cancelled right away and ZarinPal sends {amount} back to the card you paid with. This cannot be undone.",
        "btn_refund_yes": "Yes, cancel it", "btn_refund_no": "Keep the pass",
        "refund_done": "Done: the pass is cancelled and the money is on its way back to your card. Your bank may take a little while to show it.",
        "mine_more": "Details in the dashboard:\n{url}",
        "invite_off": "Referrals are off right now.",
        "invite_lede": "Share your link: a friend who signs up through it gets {pct}% off their first pass.\n{link}",
        "invite_unit": "{pct}% off, expires {date}",
        "invite_waiting": "{n} waiting for the admin's approval.",
        "summary_ref": "Price: {list}\nReferral discount ({pct}%): −{off}",
        "ask_feedback": "Write your feedback:",
        "got_support": "Your message reached support; the answer will come here.",
        "got_feedback": "Thanks, your feedback arrived.",
        "too_long": "Please split it into shorter messages.",
        "unsupported": "Please send text, a photo, a file or a voice note.",
    },
}


def t(lang: str, key: str, **kw) -> str:
    return TEXTS["en" if lang == "en" else "fa"][key].format(**kw)


def _content(msg: dict) -> tuple[str | None, str] | None:
    """(media, text) of a message the bot relays; None for a kind it does not (stickers, polls, locations...)."""
    if isinstance(msg.get("text"), str):
        return None, msg["text"]
    for media in ("photo", "document", "voice"):
        if media in msg:
            return media, str(msg.get("caption") or "")
    return None


# Persian for the refusals a buyer in the bot can meet; any other message is shown as it is.
FA_ERRORS = {
    refunds.GONE: "دیگر نمی‌توان این بسته را لغو کرد و وجه آن را برگرداند.",
    refunds.FAILED: "زرین‌پال بازگشت وجه را نپذیرفت و چیزی تغییر نکرد. با پشتیبانی تماس بگیرید.",
    refunds.UNKNOWN: "زرین‌پال بازگشت وجه را تأیید نکرد؛ پشتیبانی آن را بررسی می‌کند و به شما خبر می‌دهد.",
    payments.PAUSED: "پرداخت آنلاین فعلاً متوقف است؛ کمی بعد دوباره امتحان کنید.",
    payments.MANUAL_ONLY: "پرداخت آنلاین فعلاً خاموش است.",
    payments.UNAVAILABLE: "زرین‌پال الان در دسترس نیست؛ کمی بعد دوباره امتحان کنید.",
    payments.ALREADY_PAID: "این سفارش قبلاً پرداخت شده است.",
    orders.UNAVAILABLE: "بسته‌ها موقتاً در دسترس نیستند؛ چند دقیقه دیگر امتحان کنید.",
    orders.CHANGED: "این سفارش لغو یا عوض شده است.",
    orders.USER_TOO_MANY: "امروز سفارش زیادی ثبت شده؛ فردا دوباره امتحان کنید.",
    orders.LINK_INVALID: "این لینک پرداخت دیگر معتبر نیست.",
    "You already have an open order.": "یک سفارش باز دارید.",
}


LINK_TTL_S = 600


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def new_link_code(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> str:
    """A single-use code for t.me/<bot>?start=<code>, valid 10 minutes; replaces the user's earlier one (section 4).
    Only its hash is stored."""
    now = int(time.time() if now is None else now)
    code = secrets.token_urlsafe(16)   # 22 characters of [A-Za-z0-9_-]: what a start parameter allows
    conn.execute("DELETE FROM telegram_link_codes WHERE user_id=?", (user_id,))
    conn.execute("INSERT INTO telegram_link_codes(code_hash, user_id, expires_at) VALUES(?,?,?)", (_hash(code), user_id, now + LINK_TTL_S))
    return code


def claim(conn: sqlite3.Connection, code: str, chat_id: int, now: float | None = None) -> tuple[dict, int | None] | None:
    """Link `chat_id` (its row exists) to the code's account and use the code up. Returns (the account, the chat it
    was linked to before, if another), or None for a bad, used or expired code, or an account no longer enabled."""
    now = int(time.time() if now is None else now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM telegram_link_codes WHERE code_hash=?", (_hash(code),)).fetchone()
        user = None
        if row is not None:
            conn.execute("DELETE FROM telegram_link_codes WHERE code_hash=?", (row["code_hash"],))
            if row["expires_at"] > now:
                user = conn.execute("SELECT * FROM users WHERE id=? AND enabled=1 AND revoked_at IS NULL", (row["user_id"],)).fetchone()
        if user is None:
            conn.execute("COMMIT")
            return None
        displaced = conn.execute("SELECT user_id FROM telegram_chats WHERE chat_id=?", (chat_id,)).fetchone()
        old = conn.execute("SELECT chat_id FROM telegram_chats WHERE user_id=? AND chat_id!=?", (user["id"], chat_id)).fetchone()
        conn.execute("UPDATE telegram_chats SET user_id=NULL WHERE user_id=?", (user["id"],))
        conn.execute("UPDATE telegram_chats SET user_id=? WHERE chat_id=?", (user["id"], chat_id))
        # This chat was already linked to a different account (B): claiming it for this code's account (A) drops
        # B's link silently unless audited here (final review, finding 2).
        if displaced is not None and displaced["user_id"] is not None and displaced["user_id"] != user["id"]:
            db.audit(conn, user["id"], "telegram_unlink", db._user_name(conn, displaced["user_id"]), {"chat_id": chat_id})
        db.audit(conn, user["id"], "telegram_relink" if old else "telegram_link", user["name"], {"chat_id": chat_id})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return dict(user), old["chat_id"] if old else None


def unlink(conn: sqlite3.Connection, user_id: int, actor: int | None) -> bool:
    n = conn.execute("UPDATE telegram_chats SET user_id=NULL WHERE user_id=?", (user_id,)).rowcount
    if n:
        db.audit(conn, actor, "telegram_unlink", db._user_name(conn, user_id))
    return bool(n)


def connected(conn: sqlite3.Connection, user_id: int) -> str | None:
    """How the dashboard names the linked Telegram account: @username, else its first name."""
    row = conn.execute("SELECT username, first_name FROM telegram_chats WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        return None
    return f"@{row['username']}" if row["username"] else row["first_name"] or "Telegram"


async def after_payment(conn: sqlite3.Connection, cfg: Config, http: httpx.AsyncClient, pid: int) -> None:
    """Section 6.6: a bot order's payment just settled (paid or paid_unfulfilled); tell the buyer in Telegram.
    Does not depend on [email]. Never raises: the payment is settled whatever happens here."""
    if not on(cfg):
        return
    try:
        p = payments.get(conn, pid)
        o = orders.get(conn, p["order_id"])
        chat = conn.execute("SELECT * FROM telegram_chats WHERE user_id=?", (p["user_id"],)).fetchone()
        if o["via"] != "telegram" or chat is None:
            return
        lang = chat["lang"]
        tk = next((x for x in tickets.user_tickets(conn, p["user_id"]) if x["id"] == o["ticket_id"]), None) if o["ticket_id"] else None
        if p["status"] == "paid":
            if tk is None:
                # A settled payment with no ticket to show: the payment stands either way, so this is only ever
                # logged, never shown as "unfulfilled" (which is reserved for paid_unfulfilled itself).
                logger.warning("telegram notice for payment #%d: status is 'paid' but no ticket (order #%d, ticket_id=%r) was found",
                               pid, o["id"], o["ticket_id"])
                return
            text = t(lang, "paid", ref=p["ref_id"], start=tickets.terms_date(tk["starts_at"], lang),
                     end=tickets.terms_date(tk["effective_end"], lang))
        elif p["status"] == "paid_unfulfilled":
            text = t(lang, "unfulfilled", ref=p["ref_id"])
        else:
            return
        try:
            await Bot(cfg.telegram.token(), http).call("sendMessage", chat_id=chat["chat_id"], text=text)
        except Blocked:
            conn.execute("UPDATE telegram_chats SET blocked=1 WHERE chat_id=?", (chat["chat_id"],))
    except Exception:
        logger.exception("telegram notice for payment #%d failed", pid)


COMMANDS = {
    "fa": [("menu", "منو"), ("prices", "قیمت‌ها"), ("invite", "دعوت دوستان"), ("lang", "English / فارسی"),
          ("cancel", "لغو"), ("unlink", "قطع اتصال حساب")],
    "en": [("menu", "Menu"), ("prices", "Prices"), ("invite", "Invite friends"), ("lang", "English / فارسی"),
          ("cancel", "Cancel"), ("unlink", "Disconnect the account")],
}


def webhook_url(cfg: Config) -> str:
    return f"{cfg.listener.dashboard_url.rstrip('/')}/telegram/webhook"


async def set_webhook(cfg: Config, http: httpx.AsyncClient) -> bool:
    """`claude-proxy telegram set-webhook` (section 2): point the bot at this gateway. Never called at startup, so a dev
    gateway with a [telegram] section cannot take the live bot's webhook away."""
    bot = Bot(cfg.telegram.token(), http)
    ok = await bot.call("setWebhook", url=webhook_url(cfg), secret_token=cfg.telegram.webhook_secret(),
                        allowed_updates=["message", "callback_query"]) is True
    for lang, cmds in COMMANDS.items():   # Persian is the default list; English for a Telegram set to English
        await bot.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in cmds],
                       **({"language_code": "en"} if lang == "en" else {}))
    return ok


async def check_webhook(cfg: Config, http: httpx.AsyncClient) -> None:
    """At startup: say so when the bot's webhook points somewhere else; change nothing."""
    try:
        info = await Bot(cfg.telegram.token(), http).call("getWebhookInfo")
    except Blocked:
        info = None
    url = (info or {}).get("url") if isinstance(info, dict) else None
    if url != webhook_url(cfg):
        logger.warning("telegram: the bot's webhook is %r, not %s; run `claude-proxy telegram set-webhook` on the live gateway",
                       url, webhook_url(cfg))


class Telegram:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, http: httpx.AsyncClient):
        self.conn, self.cfg = conn, cfg
        self.bot = Bot(cfg.telegram.token(), http)
        self.admin_chat = cfg.telegram.admin_chat_id
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
        self._hits: dict[tuple[int, str], collections.deque] = collections.defaultdict(collections.deque)
        self._warned: dict[int, float] = {}
        self._confirmed: dict[int, float] = {}

    async def handle(self, update) -> None:
        """One webhook update. Never raises: Telegram would retry the delivery."""
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
            return
        if self.conn.execute("INSERT OR IGNORE INTO telegram_updates(update_id, at) VALUES(?,?)",
                             (update["update_id"], int(time.time()))).rowcount == 0:
            return   # a retried delivery
        msg, q = update.get("message"), update.get("callback_query")
        chat = ((msg if isinstance(msg, dict) else None) or ((q or {}).get("message") if isinstance(q, dict) else None) or {}).get("chat") or {}
        cid = chat.get("id")
        if not isinstance(cid, int):
            return
        lock = self._locks.get(cid)
        if lock is None:
            lock = self._locks[cid] = asyncio.Lock()
        async with lock:
            try:
                if cid == self.admin_chat:
                    if isinstance(msg, dict):
                        await self.admin_message(msg)
                elif chat.get("type") == "private":
                    if isinstance(msg, dict):
                        await self.private_message(msg)
                    elif isinstance(q, dict):
                        await self.callback(q)
            except Blocked:
                self.conn.execute("UPDATE telegram_chats SET blocked=1 WHERE chat_id=?", (cid,))
            except Exception:
                logger.exception("telegram update %d failed", update["update_id"])

    # ---------- chat state ----------

    def chat(self, frm: dict, cid: int) -> dict:
        """The chat's row, created on first contact; a message from the user also means they no longer block the bot."""
        now = int(time.time())
        self.conn.execute("INSERT INTO telegram_chats(chat_id, username, first_name, last_seen, created_at) VALUES(?,?,?,?,?) "
                          "ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, "
                          "last_seen=excluded.last_seen, blocked=0",
                          (cid, frm.get("username"), frm.get("first_name"), now, now))
        return dict(self.conn.execute("SELECT * FROM telegram_chats WHERE chat_id=?", (cid,)).fetchone())

    def account(self, chat: dict) -> dict | None:
        """The linked account, re-read each time: None when unlinked, disabled or revoked (section 2)."""
        if chat["user_id"] is None:
            return None
        row = self.conn.execute("SELECT * FROM users WHERE id=? AND enabled=1 AND revoked_at IS NULL", (chat["user_id"],)).fetchone()
        return dict(row) if row else None

    def set_state(self, chat: dict, state: str | None, pending: dict | None) -> None:
        chat["state"], chat["pending"] = state, None if pending is None else json.dumps(pending)
        self.conn.execute("UPDATE telegram_chats SET state=?, pending=? WHERE chat_id=?", (chat["state"], chat["pending"], chat["chat_id"]))

    def pending(self, chat: dict) -> dict | None:
        try:
            p = json.loads(chat["pending"]) if chat["pending"] else None
        except ValueError:
            return None
        return p if isinstance(p, dict) else None

    def allow(self, cid: int, what: str, per_hour: int) -> bool:
        q, now = self._hits[(cid, what)], time.time()
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= per_hour:
            return False
        q.append(now)
        return True

    async def too_many(self, chat: dict) -> None:
        if time.time() - self._warned.get(chat["chat_id"], 0) > 3600:
            self._warned[chat["chat_id"]] = time.time()
            await self.say(chat, "too_many")

    def dashboard(self) -> str:
        return f"{self.cfg.listener.dashboard_url.rstrip('/')}/dashboard"

    def site(self) -> str:
        return (self.cfg.listener.home_url or self.cfg.listener.dashboard_url).rstrip("/")

    # ---------- sending ----------

    def btn(self, chat: dict, key: str, data: str) -> dict:
        return {"text": t(chat["lang"], key), "callback_data": data}

    async def say(self, chat: dict, key: str, buttons: list | None = None, **kw):
        params = {"chat_id": chat["chat_id"], "text": t(chat["lang"], key, **kw)}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return await self.bot.call("sendMessage", **params)

    async def menu(self, chat: dict) -> None:
        # A top-level action: see buy()'s comment (review finding 2).
        self.set_state(chat, None, None)
        b = lambda key, data: self.btn(chat, key, data)  # noqa: E731
        await self.say(chat, "menu", [[b("btn_buy", "buy"), b("btn_prices", "prices")], [b("btn_mine", "mine"), b("btn_support", "support")],
                                      [b("btn_feedback", "feedback"), b("btn_lang", "lang")]])

    async def toggle_lang(self, chat: dict) -> None:
        chat["lang"] = "en" if chat["lang"] == "fa" else "fa"
        self.conn.execute("UPDATE telegram_chats SET lang=? WHERE chat_id=?", (chat["lang"], chat["chat_id"]))
        await self.say(chat, "lang_set")
        await self.menu(chat)

    async def start_code(self, chat: dict, code: str) -> None:
        got = claim(self.conn, code, chat["chat_id"])
        if got is None:
            return await self.say(chat, "link_bad")
        user, old = got
        chat["user_id"] = user["id"]
        if old is not None:
            row = self.conn.execute("SELECT * FROM telegram_chats WHERE chat_id=?", (old,)).fetchone()
            try:
                await self.say(dict(row), "relinked_away")
            except Blocked:
                self.conn.execute("UPDATE telegram_chats SET blocked=1 WHERE chat_id=?", (old,))
        await self.say(chat, "linked", who=orders.mask_email(user["email"]) if user["email"] else user["name"])
        await self.menu(chat)

    # ---------- incoming ----------

    async def private_message(self, msg: dict) -> None:
        chat = self.chat(msg.get("from") or {}, msg["chat"]["id"])
        if not self.allow(chat["chat_id"], "msg", MSG_PER_HOUR):
            return await self.too_many(chat)
        text = msg.get("text") or ""
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0]
            if cmd == "/start" and arg.strip() == "support":   # the site's contact link opens Support straight away
                self.open_support(chat)
                return await self.say(chat, "ask_support")
            if cmd == "/start" and arg.strip():
                self.set_state(chat, None, None)
                return await self.start_code(chat, arg.strip())
            if cmd == "/unlink":
                self.set_state(chat, None, None)
                if chat["user_id"] is not None and unlink(self.conn, chat["user_id"], chat["user_id"]):
                    chat["user_id"] = None
                    return await self.say(chat, "unlinked")
                return await self.say(chat, "not_linked")
            if cmd in ("/start", "/menu"):
                return await self.menu(chat)
            if cmd == "/lang":
                return await self.toggle_lang(chat)
            if cmd == "/prices":
                return await self.prices(chat)
            if cmd == "/invite":
                return await self.invite(chat)
            if cmd == "/cancel":
                self.set_state(chat, None, None)
                return await self.say(chat, "cancelled")
        await self.on_text(chat, msg)

    def open_support(self, chat: dict) -> None:
        self.set_state(chat, "support", {"until": time.time() + SUPPORT_WINDOW_S})

    async def on_text(self, chat: dict, msg: dict) -> None:
        if chat["state"] == "email":
            return await self.got_email(chat, msg)
        if chat["state"] == "feedback":
            self.set_state(chat, None, None)
            return await self.incoming(chat, msg, "feedback")
        if chat["state"] == "support" and (self.pending(chat) or {}).get("until", 0) > time.time():
            self.open_support(chat)   # each message keeps the window open
            return await self.incoming(chat, msg, "support")
        # Only messages after the Support (or Feedback) button reach the admins; anything else gets the menu.
        await self.menu(chat)

    async def callback(self, q: dict) -> None:
        chat = self.chat(q.get("from") or {}, q["message"]["chat"]["id"])
        action, _, arg = str(q.get("data") or "").partition(":")
        handled = await self.route(chat, action, arg)
        await self.bot.call("answerCallbackQuery", callback_query_id=q.get("id"),
                            **({} if handled else {"text": t(chat["lang"], "stale")}))

    async def route(self, chat: dict, action: str, arg: str) -> bool:
        """A button's action; False when it is not (or no longer) valid."""
        if action == "menu":
            await self.menu(chat)
        elif action == "lang":
            await self.toggle_lang(chat)
        elif action == "buy":
            await self.buy(chat)
        elif action == "mine":
            await self.mine(chat)
        elif action == "prices":
            await self.prices(chat)
        elif action == "t":
            await self.pick_tier(chat, arg)
        elif action == "l":
            return await self.pick_length(chat, arg)
        elif action == "s" and arg in ("a", "n"):
            return await self.pick_start(chat, arg)
        elif action == "p":
            return await self.pay(chat, arg)
        elif action in ("rf", "rfy") and arg.isdigit():
            return await (self.ask_refund if action == "rf" else self.refund)(chat, int(arg))
        elif action in ("oc", "ox") and arg.isdigit():
            await (self.continue_order if action == "oc" else self.cancel_order)(chat, int(arg))
        elif action == "feedback":
            self.set_state(chat, "feedback", None)
            await self.say(chat, "ask_feedback")
        elif action == "support":
            self.open_support(chat)
            await self.say(chat, "ask_support")
        else:
            return False
        return True

    # ---------- prices ----------

    async def prices(self, chat: dict) -> None:
        """The Toman price list the site shows, for anyone: a compact table of each tier's lengths on sale."""
        lang, table = chat["lang"], payments.prices(self.conn, self.cfg, time.time())
        if table is None:
            return await self.say(chat, "prices_site", url=self.site())
        num = (lambda n: tickets.fa_num(str(n))) if lang != "en" else str
        lines, sold = [t(lang, "prices_head")], False
        for x in table["tiers"]:
            on_sale = [(n, p) for n, p in x["lengths"].items() if p is not None]
            if not on_sale:
                continue
            head, pct, plan = self.label(x["tier"], lang), x.get("compare_pct"), self.plan(x["compare"], lang)
            if pct is not None:   # a whole number of plans reads as plans, as on the home page
                share = (t(lang, "prices_same", plan=plan) if pct == 100 else
                         t(lang, "prices_times", n=num(pct // 100), plan=plan) if pct > 100 and pct % 100 == 0 else
                         t(lang, "prices_pct", pct=num(pct), plan=plan))
                head += f" ({share})"
            lines += ["", head]
            for n, p in on_sale:
                line = f"  {t(lang, f'per_{n}')}  {self.amount(lang, p['amount'])}"
                if p["sold_out"]:
                    line, sold = line + "  ⛔", True
                elif p["list_amount"] > p["amount"] and p["list_usd"] > 0:
                    line += "  " + t(lang, "prices_off", pct=num(round((1 - p["usd"] / p["list_usd"]) * 100)))
                lines.append(line)
        foot = ([t(lang, "prices_sold")] if sold else []) + ([t(lang, "prices_vat", vat=num(round(table["vat_percent"])))]
                                                             if table.get("vat_percent") else [])
        if foot:
            lines += ["", " · ".join(foot)]
        await self.bot.call("sendMessage", chat_id=chat["chat_id"], text="\n".join(lines), reply_markup={"inline_keyboard": [
            [self.btn(chat, "btn_buy", "buy"), {"text": t(lang, "btn_site"), "url": self.site()}]]})

    # ---------- buying (section 6) ----------

    def error_text(self, lang: str, e: Exception) -> str:
        return FA_ERRORS.get(str(e), str(e)) if lang != "en" else str(e)

    def amount(self, lang: str, n) -> str:
        return orders.fa_amount(n, payments.CURRENCY) if lang != "en" else payments._toman(n)

    def length_name(self, lang: str, length: str) -> str:
        return t(lang, f"len_{length}")

    def label(self, tier: str, lang: str) -> str:
        """The tier's name in the chat's language: Persian as the site words it while the label is the default."""
        if lang != "en":
            return orders.fa_label(self.cfg, tier)
        x = self.cfg.tickets.tiers.get(tier)
        return x.label if x else tier

    @staticmethod
    def plan(name: str, lang: str) -> str:
        return blog.FA_PLANS.get(name, name) if lang != "en" else name

    @staticmethod
    def _buyable(p) -> bool:
        return p is not None and not p["sold_out"] and not p["unavailable"]

    def tiers_on_sale(self, now: float) -> list[dict] | None:
        """The Toman price list's tiers with something buyable; None while ZarinPal can't take a payment."""
        if not self.cfg.tickets.enabled or not payments.online(payments.pay_mode(self.conn, self.cfg)) \
                or not payments.ready(self.conn, self.cfg, now):
            return None
        table = payments.prices(self.conn, self.cfg, now)
        if table is None:
            return None
        # callback_data is at most 64 bytes: "t:" + a tier id of at most 40
        return [x for x in table["tiers"] if len(x["tier"]) <= 40 and any(self._buyable(p) for p in x["lengths"].values())]

    def open_order(self, user_id: int) -> dict | None:
        row = self.conn.execute(f"SELECT * FROM orders WHERE user_id=? AND status IN {orders.OPEN} ORDER BY id DESC LIMIT 1",
                                (user_id,)).fetchone()
        return dict(row) if row else None

    async def need_account(self, chat: dict) -> dict | None:
        user = self.account(chat)
        if user is None:
            await self.say(chat, "need_link", url=self.dashboard())
        return user

    async def buy(self, chat: dict) -> None:
        # A top-level action: whatever the chat was waiting for (an email for an earlier attempt, a pending tier or
        # length) is dropped, so stray text afterwards is never read as that email (review finding 2).
        self.set_state(chat, None, None)
        user = await self.need_account(chat)
        if user is None:
            return
        o = self.open_order(user["id"])
        if o is not None:
            if o["currency"] == payments.CURRENCY and not payments._has_paid(self.conn, o["id"]):
                return await self.say(chat, "open_order", [[self.btn(chat, "btn_continue", f"oc:{o['id']}")],
                                                           [self.btn(chat, "btn_cancel_order", f"ox:{o['id']}")]],
                                      label=self.label(o["tier"], chat["lang"]), length=self.length_name(chat["lang"], o["length"]),
                                      amount=self.amount(chat["lang"], o["quoted_amount"]))
            return await self.say(chat, "order_busy")
        tiers = self.tiers_on_sale(time.time())
        if tiers is None:
            return await self.say(chat, "paused")
        if not tiers:
            return await self.say(chat, "sold_out")
        await self.say(chat, "choose_tier", [[{"text": self.label(x["tier"], chat["lang"]), "callback_data": f"t:{x['tier']}"}] for x in tiers])

    def _tier(self, tier: str) -> dict | None:
        return next((x for x in self.tiers_on_sale(time.time()) or [] if x["tier"] == tier), None)

    async def pick_tier(self, chat: dict, tier: str) -> None:
        if await self.need_account(chat) is None:
            return
        x = self._tier(tier)
        if x is None:
            return await self.buy(chat)
        self.set_state(chat, None, {"tier": tier})
        lang = chat["lang"]
        await self.say(chat, "choose_length", [[{"text": f"{self.length_name(lang, n)} · {self.amount(lang, p['amount'])}",
                                                 "callback_data": f"l:{n}"}] for n, p in x["lengths"].items() if self._buyable(p)])

    async def pick_length(self, chat: dict, length: str) -> bool:
        user, pend = await self.need_account(chat), self.pending(chat)
        if user is None:
            return True
        if not pend or "tier" not in pend:
            return False
        x = self._tier(pend["tier"])
        if x is None or not self._buyable(x["lengths"].get(length)):
            await self.buy(chat)
            return True
        pend = {"tier": pend["tier"], "length": length}
        now = time.time()
        if passcard.start_offer(self.conn, self.cfg, user["id"], now)["offer"]:   # as the dashboard's order dialog offers it
            self.set_state(chat, None, pend)
            await self.say(chat, "choose_start", [[self.btn(chat, "btn_after", "s:a")], [self.btn(chat, "btn_now", "s:n")]])
            return True
        await self.summary(chat, pend | {"start_now": False})
        return True

    async def pick_start(self, chat: dict, arg: str) -> bool:
        pend = self.pending(chat)
        if not pend or "length" not in pend:
            return False
        await self.summary(chat, pend | {"start_now": arg == "n"})
        return True

    async def summary(self, chat: dict, pend: dict) -> None:
        user = await self.need_account(chat)
        if user is None:
            return
        x = self._tier(pend["tier"])
        p = x and x["lengths"].get(pend["length"])
        if not self._buyable(p):
            return await self.buy(chat)
        now, lang = time.time(), chat["lang"]
        # The bot offers no choice to split from a used-up running pass (start_offer's "offer" False): next_start
        # already means "now" then, same as start_now=True (tickets.next_start's own start_now=True branch).
        start = now if pend["start_now"] else passcard.start_offer(self.conn, self.cfg, user["id"], now)["next_start"]
        pend = pend | {"nonce": secrets.token_urlsafe(6)}
        self.set_state(chat, None, pend)
        site = self.site()
        # The referral discount orders.create will take (final review #2): the same local_quote as the dashboard's
        # order dialog, in Toman, so the summary's amount is what ZarinPal charges.
        amount, ref = p["amount"], ""
        try:
            q = referrals.local_quote(self.conn, self.cfg, user["id"], pend["tier"], pend["length"], payments.CURRENCY, now)
        except tickets.TicketError:
            q = None
        if q is not None and q["off_usd"] > 0 and q["currency"] == payments.CURRENCY:
            num = (lambda n: tickets.fa_num(str(n))) if lang != "en" else str
            amount = q["amount"]
            ref = t(lang, "summary_ref", list=self.amount(lang, q["list_amount"]), pct=num(round(q["pct_applied"])),
                    off=self.amount(lang, q["list_amount"] - q["amount"])) + "\n"
        await self.say(chat, "summary", [[self.btn(chat, "btn_pay", f"p:{pend['nonce']}")]],
                       label=self.label(x["tier"], lang), length=self.length_name(lang, pend["length"]), amount=self.amount(lang, amount),
                       ref=ref, start=tickets.terms_date(start, lang), terms=f"{site}/privacy#terms")

    async def pay(self, chat: dict, nonce: str) -> bool:
        pend = self.pending(chat)
        if not pend or not nonce or pend.get("nonce") != nonce:
            return False   # a second tap, or an old summary
        self.set_state(chat, None, None)
        user = await self.need_account(chat)
        if user is None:
            return True
        if not user["email"]:
            self.set_state(chat, "email", {k: v for k, v in pend.items() if k != "nonce"})
            await self.say(chat, "ask_email")
            return True
        await self.start_payment(chat, user, pend, user["email"])
        return True

    async def got_email(self, chat: dict, msg: dict) -> None:
        try:
            email = orders._email(msg.get("text") or "")
        except orders.OrderError:
            return await self.say(chat, "bad_email")
        pend = self.pending(chat) or {}
        self.set_state(chat, None, None)
        user = await self.need_account(chat)
        if user is None or "length" not in pend:
            return await self.menu(chat) if user is not None else None
        await self.start_payment(chat, user, pend, email)

    async def send_pay_link(self, chat: dict, url: str) -> None:
        await self.say(chat, "pay_ready", [[{"text": t(chat["lang"], "btn_open_pay"), "url": url}]])

    async def start_payment(self, chat: dict, user: dict, pend: dict, email: str) -> None:
        if not self.allow(chat["chat_id"], "pay", PAY_PER_HOUR):
            return await self.too_many(chat)
        try:
            r = await payments.start(self.conn, self.cfg, user, pend["tier"], pend["length"], email,
                                     terms_version=tickets.TERMS_VERSION, lang=chat["lang"],
                                     start_now=bool(pend.get("start_now")), via="telegram")
        except orders.OrderError as e:
            return await self.bot.call("sendMessage", chat_id=chat["chat_id"], text=self.error_text(chat["lang"], e))
        await self.send_pay_link(chat, r["url"])

    async def continue_order(self, chat: dict, oid: int) -> None:
        user = await self.need_account(chat)
        if user is None:
            return
        if not self.allow(chat["chat_id"], "pay", PAY_PER_HOUR):
            return await self.too_many(chat)
        try:
            r = await payments.pay_order(self.conn, self.cfg, user, oid)
        except orders.OrderError as e:
            return await self.bot.call("sendMessage", chat_id=chat["chat_id"], text=self.error_text(chat["lang"], e))
        # The order may have been placed from the dashboard (via='web'); paying it from the bot means its buyer
        # now expects the settlement notice here too (final review, finding 1). payments.pay_order itself is
        # untouched: only the bot stamps this, and only for the order's own user.
        self.conn.execute("UPDATE orders SET via='telegram' WHERE id=? AND user_id=?", (oid, user["id"]))
        await self.send_pay_link(chat, r["url"])

    async def cancel_order(self, chat: dict, oid: int) -> None:
        user = await self.need_account(chat)
        if user is None:
            return
        try:
            orders.withdraw(self.conn, user["id"], oid)
        except orders.OrderError as e:
            return await self.bot.call("sendMessage", chat_id=chat["chat_id"], text=self.error_text(chat["lang"], e))
        await self.buy(chat)

    async def mine(self, chat: dict) -> None:
        # A top-level action: see buy()'s comment (review finding 2).
        self.set_state(chat, None, None)
        user = await self.need_account(chat)
        if user is None:
            return
        lang, lines = chat["lang"], []
        if self.cfg.tickets.enabled:
            card = passcard.build(self.conn, self.cfg, user["id"], time.time())
            cur = card["current"]
            if cur and cur["passes"]:
                for p in cur["passes"]:
                    lines.append(t(lang, "mine_queued", label=self.label(p["tier"], lang), start=tickets.terms_date(p["starts_at"], lang)) if p.get("queued")
                                 else t(lang, "mine_running", label=self.label(p["tier"], lang), end=tickets.terms_date(p["effective_end"], lang)))
            else:
                if cur:
                    lines.append(t(lang, "mine_running", label=self.label(cur["tier"], lang), end=tickets.terms_date(cur["effective_end"], lang)))
                if card["queued"]:
                    q = card["queued"]
                    lines.append(t(lang, "mine_queued", label=self.label(q["tier"], lang), start=tickets.terms_date(q["starts_at"], lang)))
        text = "\n".join(lines) if lines else t(lang, "mine_none")
        # A pass paid in the last 15 minutes and not used yet: one button each to cancel it for the money back.
        offers = card["refunds"] if self.cfg.tickets.enabled else []
        params = {"reply_markup": {"inline_keyboard": [[{"text": t(lang, "btn_refund", label=self.pass_name(r, lang)),
                                                          "callback_data": f"rf:{r['payment_id']}"}] for r in offers]}} if offers else {}
        await self.bot.call("sendMessage", chat_id=chat["chat_id"], text=f"{text}\n\n{t(lang, 'mine_more', url=self.dashboard())}", **params)

    def pass_name(self, r: dict, lang: str) -> str:
        """A refundable pass's tier and length, so two passes of one tier tell apart."""
        return f"{self.label(r['tier'], lang)} ({self.length_name(lang, r['length'])})"

    def _refund_offer(self, user_id: int, pid: int) -> dict | None:
        return next((r for r in refunds.offers(self.conn, self.cfg, user_id) if r["payment_id"] == pid), None)

    async def ask_refund(self, chat: dict, pid: int) -> bool:
        user = await self.need_account(chat)
        if user is None:
            return True
        r = self._refund_offer(user["id"], pid)
        if r is None:
            return False
        lang = chat["lang"]
        await self.say(chat, "refund_ask", [[self.btn(chat, "btn_refund_yes", f"rfy:{pid}"), self.btn(chat, "btn_refund_no", "mine")]],
                       label=self.pass_name(r, lang), amount=self.amount(lang, r["amount"]))
        return True

    async def refund(self, chat: dict, pid: int) -> bool:
        user = await self.need_account(chat)
        if user is None:
            return True
        try:
            await refunds.refund(self.conn, self.cfg, user, pid)
        except payments.PaymentError as e:
            await self.bot.call("sendMessage", chat_id=chat["chat_id"], text=self.error_text(chat["lang"], e))
            return True
        await self.say(chat, "refund_done")
        return True

    async def invite(self, chat: dict) -> None:
        """/invite (referrals design, task 6): the same link and units the dashboard's "Invite friends" card shows,
        for the account this chat is linked to. Unlinked gets the bot's existing "not connected" message (not
        need_link/need_account: /invite is read-only, nothing here needs the dashboard to set up first)."""
        self.set_state(chat, None, None)
        user = self.account(chat)
        if user is None:
            return await self.say(chat, "not_linked")
        lang = chat["lang"]
        status = referrals.my_status(self.conn, self.cfg, user["id"], time.time())
        num = (lambda n: tickets.fa_num(str(n))) if lang != "en" else str
        # Off: no link, but saved units stay usable and waiting rewards can still be approved (final review #8).
        lines = [t(lang, "invite_lede", pct=num(round(status["friend_pct"])), link=status["link"])] if status["enabled"] \
            else [t(lang, "invite_off")]
        for u in status["units"]:
            lines.append(t(lang, "invite_unit", pct=num(round(u["pct"])), date=tickets.terms_date(u["expires_at"], lang)))
        if status["waiting"]:
            lines.append(t(lang, "invite_waiting", n=num(status["waiting"])))
        await self.bot.call("sendMessage", chat_id=chat["chat_id"], text="\n".join(lines))

    # ---------- support and feedback (section 7) ----------

    def _record_copy(self, sent, inbox_id: int) -> None:
        if isinstance(sent, dict) and isinstance(sent.get("message_id"), int):
            self.conn.execute("INSERT OR IGNORE INTO inbox_admin_msgs(admin_msg_id, inbox_id) VALUES(?,?)", (sent["message_id"], inbox_id))

    async def incoming(self, chat: dict, msg: dict, kind: str) -> None:
        got = _content(msg)
        if got is None:
            return await self.say(chat, "unsupported")
        media, text = got
        if len(text) > TEXT_MAX:
            return await self.say(chat, "too_long")
        cid = chat["chat_id"]
        iid = self.conn.execute("INSERT INTO inbox_messages(chat_id, kind, direction, media, text, created_at) VALUES(?,?,'in',?,?,?)",
                                (cid, kind, media, text, int(time.time()))).lastrowid
        user = self.account(chat)
        header = " · ".join(x for x in (f"#{kind}", chat["first_name"], f"@{chat['username']}" if chat["username"] else None,
                                        (user["email"] or user["name"]) if user else "not linked") if x)
        try:   # plain text, no parse_mode: nothing the user typed can format the admin's view
            h = await self.bot.call("sendMessage", chat_id=self.admin_chat, text=header)
            self._record_copy(h, iid)
            c = await self.bot.call("copyMessage", chat_id=self.admin_chat, from_chat_id=cid, message_id=msg["message_id"],
                                    **({"reply_to_message_id": h["message_id"]} if isinstance(h, dict) else {}))
            self._record_copy(c, iid)
        except Blocked:
            logger.error("telegram: the bot cannot post in the admin group %d", self.admin_chat)
        if kind == "feedback":
            await self.say(chat, "got_feedback")
        elif time.time() - self._confirmed.get(cid, 0) > CONFIRM_EVERY_S:
            self._confirmed[cid] = time.time()
            await self.say(chat, "got_support")

    async def admin_message(self, msg: dict) -> None:
        """A reply, in the admin group, to a relayed message's header or copy: sent back to its user when the sender is
        an enabled admin, by their linked Telegram account. Anything else in the group is ignored."""
        r = msg.get("reply_to_message")
        if not isinstance(r, dict):
            return
        row = self.conn.execute("SELECT i.* FROM inbox_admin_msgs a JOIN inbox_messages i ON i.id=a.inbox_id WHERE a.admin_msg_id=?",
                                (r.get("message_id"),)).fetchone()
        if row is None:
            return
        frm = msg.get("from") or {}
        if msg.get("sender_chat") or frm.get("username") == "GroupAnonymousBot":
            return await self.bot.call("sendMessage", chat_id=self.admin_chat, text=ANON, reply_to_message_id=msg["message_id"])
        admin = self.conn.execute("SELECT u.* FROM telegram_chats c JOIN users u ON u.id=c.user_id WHERE c.chat_id=? "
                                  "AND u.role='admin' AND u.enabled=1 AND u.revoked_at IS NULL", (frm.get("id"),)).fetchone()
        if admin is None:
            return
        got = _content(msg)
        if got is None:
            return await self.bot.call("sendMessage", chat_id=self.admin_chat, text=NOT_DELIVERED_UNSUPPORTED,
                                       reply_to_message_id=msg["message_id"])
        note = NOT_DELIVERED
        try:
            sent = await self.bot.call("copyMessage", chat_id=row["chat_id"], from_chat_id=self.admin_chat, message_id=msg["message_id"])
        except Blocked:
            self.conn.execute("UPDATE telegram_chats SET blocked=1 WHERE chat_id=?", (row["chat_id"],))
            sent, note = None, NOT_DELIVERED_BLOCKED
        if sent is None:
            return await self.bot.call("sendMessage", chat_id=self.admin_chat, text=note, reply_to_message_id=msg["message_id"])
        media, text = got
        self.conn.execute("INSERT INTO inbox_messages(chat_id, kind, direction, media, text, actor, created_at) VALUES(?,?,'out',?,?,?,?)",
                          (row["chat_id"], row["kind"], media, text, admin["id"], int(time.time())))
        # The user's next messages go to support again, unless they are in the middle of something else.
        self.conn.execute("UPDATE telegram_chats SET state='support', pending=? WHERE chat_id=? AND (state IS NULL OR state='support')",
                          (json.dumps({"until": time.time() + SUPPORT_WINDOW_S}), row["chat_id"]))
        await self.bot.call("setMessageReaction", chat_id=self.admin_chat, message_id=msg["message_id"],
                            reaction=[{"type": "emoji", "emoji": "👍"}])
