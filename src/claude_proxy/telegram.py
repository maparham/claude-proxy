"""Telegram bot (design 2026-11-05): buy a pass, send feedback, get support.

One Telegram object per dashboard app, on the dashboard's loop and connection (section 2), the same ones the payment
callback and reconcile use. Every update is handled inline, under its chat's lock, after its update_id is recorded,
so a delivery Telegram retries is handled once.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import sqlite3
import time
import weakref

import httpx

from .config import Config

logger = logging.getLogger("claude_proxy")

API = "https://api.telegram.org"
MSG_PER_HOUR, PAY_PER_HOUR = 20, 5
CONFIRM_EVERY_S = 600   # a support message is confirmed at most this often per chat
TEXT_MAX = 3500


class Blocked(Exception):
    """The user blocked the bot (a 403)."""


def on(cfg: Config) -> bool:
    t = cfg.telegram
    return t is not None and bool(t.token()) and bool(t.webhook_secret()) and bool(cfg.listener.dashboard_url)


class _MaskToken(logging.Filter):
    """httpx logs every request URL at INFO, and a Bot API URL holds the token (section 8)."""
    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and any(self.token in str(a) for a in record.args):
            record.args = tuple(str(a).replace(self.token, "<token>") for a in record.args)
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
        "menu": "چه کاری برایتان انجام دهم؟\nهر پیامی که اینجا بنویسید به پشتیبانی می‌رسد.",
        "btn_buy": "خرید بسته", "btn_mine": "بسته‌های من", "btn_feedback": "ارسال نظر", "btn_lang": "English",
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
        "paused": "خرید آنلاین فعلاً متوقف است. هر پیامی اینجا بنویسید به پشتیبانی می‌رسد.",
        "sold_out": "همه بسته‌ها فعلاً فروخته شده‌اند.",
        "choose_tier": "کدام بسته؟", "choose_length": "چه مدت؟", "choose_start": "از کی شروع شود؟",
        "btn_after": "بعد از بسته فعلی", "btn_now": "همین حالا، کنار بسته فعلی",
        "len_day": "یک روز", "len_week": "یک هفته", "len_month": "یک ماه",
        "summary": "{label}، {length}\nمبلغ: {amount}\nشروع: {start}\n\nبا پرداخت، شرایط استفاده را می‌پذیرید:\n{terms}",
        "btn_pay": "شرایط را می‌پذیرم، پرداخت",
        "pay_ready": "لینک پرداخت آماده است:", "btn_open_pay": "پرداخت با زرین‌پال",
        "open_order": "یک سفارش پرداخت‌نشده دارید: {label}، {length}، {amount}.",
        "btn_continue": "ادامه پرداخت", "btn_cancel_order": "لغو و انتخاب دوباره",
        "order_busy": "یک سفارش در جریان دارید. اگر سؤالی دارید همین‌جا بنویسید.",
        "ask_email": "حسابتان ایمیل ندارد. یک ایمیل برای رسید پرداخت بفرستید (یا /cancel):",
        "bad_email": "این ایمیل درست به نظر نمی‌رسد؛ دوباره بفرستید یا /cancel.",
        "paid": "پرداخت انجام شد (کد پیگیری زرین‌پال: {ref}). بسته شما از {start} تا {end} فعال است.",
        "unfulfilled": "پرداخت انجام شد (کد پیگیری زرین‌پال: {ref})، اما بسته خودکار شروع نشد. آن را شروع می‌کنیم یا مبلغ را برمی‌گردانیم؛ اگر سؤالی دارید همین‌جا بنویسید.",
        "mine_none": "بسته فعال یا در صفی ندارید.",
        "mine_running": "{label}: تا {end}", "mine_queued": "{label}: در صف، از {start}",
        "mine_more": "جزئیات در داشبورد:\n{url}",
        "ask_feedback": "نظرتان را بنویسید:",
        "got_support": "پیامتان به پشتیبانی رسید؛ جواب همین‌جا می‌آید.",
        "got_feedback": "ممنون، نظرتان رسید.",
        "too_long": "پیام طولانی است؛ لطفاً آن را در چند پیام کوتاه‌تر بفرستید.",
        "unsupported": "لطفاً متن، عکس، فایل یا پیام صوتی بفرستید.",
    },
    "en": {
        "menu": "What would you like to do?\nAny message you type here goes to support.",
        "btn_buy": "Buy a pass", "btn_mine": "My passes", "btn_feedback": "Feedback", "btn_lang": "فارسی",
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
        "paused": "Buying is paused for now. Any message you type here goes to support.",
        "sold_out": "All passes are sold out right now.",
        "choose_tier": "Which pass?", "choose_length": "How long?", "choose_start": "When should it start?",
        "btn_after": "After the current one", "btn_now": "Now, beside the current one",
        "len_day": "A day", "len_week": "A week", "len_month": "A month",
        "summary": "{label}, {length}\nAmount: {amount}\nStarts: {start}\n\nPaying means you accept the terms:\n{terms}",
        "btn_pay": "I agree, pay",
        "pay_ready": "Your payment link is ready:", "btn_open_pay": "Pay with ZarinPal",
        "open_order": "You have an unpaid order: {label}, {length}, {amount}.",
        "btn_continue": "Continue paying it", "btn_cancel_order": "Cancel it and choose again",
        "order_busy": "You have an order in progress. Write here if you have a question.",
        "ask_email": "Your account has no email. Send an address for the payment receipt (or /cancel):",
        "bad_email": "That email address does not look right; send it again or /cancel.",
        "paid": "Payment received (ZarinPal reference {ref}). Your pass is active from {start} until {end}.",
        "unfulfilled": "Payment received (ZarinPal reference {ref}), but the pass could not start automatically. We'll start it or refund you; reply here with any questions.",
        "mine_none": "You have no running or queued pass.",
        "mine_running": "{label}: until {end}", "mine_queued": "{label}: queued, from {start}",
        "mine_more": "Details in the dashboard:\n{url}",
        "ask_feedback": "Write your feedback:",
        "got_support": "Your message reached support; the answer will come here.",
        "got_feedback": "Thanks, your feedback arrived.",
        "too_long": "Please split it into shorter messages.",
        "unsupported": "Please send text, a photo, a file or a voice note.",
    },
}


def t(lang: str, key: str, **kw) -> str:
    return TEXTS["en" if lang == "en" else "fa"][key].format(**kw)


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

    # ---------- sending ----------

    def btn(self, chat: dict, key: str, data: str) -> dict:
        return {"text": t(chat["lang"], key), "callback_data": data}

    async def say(self, chat: dict, key: str, buttons: list | None = None, **kw):
        params = {"chat_id": chat["chat_id"], "text": t(chat["lang"], key, **kw)}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return await self.bot.call("sendMessage", **params)

    async def menu(self, chat: dict) -> None:
        b = lambda key, data: self.btn(chat, key, data)  # noqa: E731
        await self.say(chat, "menu", [[b("btn_buy", "buy"), b("btn_mine", "mine")], [b("btn_feedback", "feedback"), b("btn_lang", "lang")]])

    async def toggle_lang(self, chat: dict) -> None:
        chat["lang"] = "en" if chat["lang"] == "fa" else "fa"
        self.conn.execute("UPDATE telegram_chats SET lang=? WHERE chat_id=?", (chat["lang"], chat["chat_id"]))
        await self.say(chat, "lang_set")
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
            if cmd in ("/start", "/menu"):
                return await self.menu(chat)
            if cmd == "/lang":
                return await self.toggle_lang(chat)
            if cmd == "/cancel":
                self.set_state(chat, None, None)
                return await self.say(chat, "cancelled")
        await self.on_text(chat, msg)

    async def on_text(self, chat: dict, msg: dict) -> None:
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
        else:
            return False
        return True

    async def admin_message(self, msg: dict) -> None:
        """A message in the admin group (section 7); answering is added with the support relay."""
        return None
