# Telegram bot: buy passes, feedback, support (design 2026-11-05)

## 1. Goal

Many buyers live on Telegram. One bot lets them:
- **buy a pass** and pay with ZarinPal, without opening the dashboard first;
- **send feedback**;
- **get support**, answered by the admin either in Telegram or in a new dashboard Inbox.

Success: a linked user goes from `/start` to a granted pass with only the ZarinPal page in between. Every support or
feedback message reaches the admin and can be answered from either place, and the user gets that answer in the bot.

Not in scope: creating accounts in the bot, payment other than ZarinPal, broadcasts, group chats for users.

## 2. Shape

- **`telegram.py`** (new): a thin Bot API client (`httpx`, no new dependency) and the update router.
- **Webhook** on the dashboard app: `POST /telegram/webhook`. It is accepted only when the
  `X-Telegram-Bot-Api-Secret-Token` header equals `[telegram] webhook_secret`. It is added to `PUBLIC` (no session).
  It answers 200 at once and handles the update in a task, so Telegram never retries because the handler was slow.
- **Config** `[telegram]`: `bot_token`, `bot_username`, `webhook_secret`, `admin_chat_id` (a private group the admin
  is in). With the section missing, the bot is off: no route, no dashboard button, and nothing changes. At startup
  the app calls `setWebhook` with `allowed_updates=["message","callback_query"]`, then `setMyCommands`.
- **Language**: a user's language is Persian unless their Telegram `language_code` is not `fa`. They can switch with
  `/lang`, and the choice is stored on the link row. All strings go in one `TEXTS = {"fa": …, "en": …}` dict in
  `telegram.py`, following the wording rules: کلاد, not Claude Code, plus the loanwords سیستم, سرویس and لینک.

## 3. Data

```sql
CREATE TABLE IF NOT EXISTS telegram_chats (
    chat_id    INTEGER PRIMARY KEY,          -- a private chat: equals the Telegram user id
    user_id    INTEGER REFERENCES users(id), -- NULL until linked; at most one chat per account
    username   TEXT, first_name TEXT,
    lang       TEXT NOT NULL DEFAULT 'fa',
    state      TEXT,                         -- what the next plain text means: 'support' | 'feedback' | NULL
    blocked    INTEGER NOT NULL DEFAULT 0,   -- the user blocked the bot (403 on send), or the admin muted them
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS telegram_chats_user ON telegram_chats(user_id) WHERE user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS telegram_link_codes (
    code TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_messages (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL REFERENCES telegram_chats(chat_id),
    kind       TEXT NOT NULL,                -- 'support' | 'feedback'
    direction  TEXT NOT NULL,                -- 'in' (from the user) | 'out' (the admin's answer)
    text       TEXT NOT NULL,                -- at most 4000 chars; a photo or file is kept as its caption plus "[photo]"
    tg_file_id TEXT,                         -- a photo or document the user sent, forwarded as is
    admin_msg_id INTEGER,                    -- the copy in the admin chat, which a reply there maps back to
    actor      INTEGER,                      -- the admin who answered ('out')
    read_at    INTEGER,                      -- when the admin opened or answered it ('in')
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS inbox_chat ON inbox_messages(chat_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS inbox_admin_msg ON inbox_messages(admin_msg_id) WHERE admin_msg_id IS NOT NULL;
```

`orders` gets `via TEXT NOT NULL DEFAULT 'web'`, which is `'telegram'` for a bot order. It records where the buyer
should hear back.

Retention: link codes are deleted once they expire, as part of the existing cleanup pass. Inbox messages are kept
(they are support history) until their account is deleted.

## 4. Linking

- The dashboard (Account section) gets **Connect Telegram**. `POST /api/telegram/link` makes a 16-byte url-safe code
  that is valid for 10 minutes and replaces any earlier code of that user. It returns
  `https://t.me/<bot_username>?start=<code>`, and the button opens it.
- In the bot, `/start <code>` with a valid code sets `telegram_chats.user_id`. If that account was linked to another
  chat, that link is removed (the newest chat wins), and the code is deleted. The bot replies with the account's
  masked email. A bad or expired code gets "This link expired; press Connect Telegram again", and nothing changes.
- The dashboard shows "Connected as @username" with **Disconnect**, and the bot's `/unlink` does the same thing.
  Both are written to the audit log.

## 5. The menu

`/start` with no code, or `/menu`, shows inline buttons: **Buy a pass** · **My passes** · **Support** ·
**Feedback** · **Language**. Each flow uses callback buttons. Only the free-text steps (support, feedback) depend on
`telegram_chats.state`, and `/cancel` clears it.

## 6. Buying

1. **Buy a pass**, when the chat is not linked: the bot explains linking and sends a dashboard link. Nothing else
   happens.
2. When it is linked: the tiers and lengths on sale come from `payments.prices()`, with Toman prices including VAT,
   shown the same way as the site. With ZarinPal not ready (`payments.ready()` false, or `pay_mode` is `manual`),
   the bot says buying is paused and points to Support.
3. The user picks a tier, then a length. If they have a running pass, they also choose **Start after the current
   one** or **Start now** (the site's `start_now`). The bot shows a summary, a link to the terms
   (`/privacy#terms`), and an **I agree, pay** button.
4. **I agree, pay** calls `payments.start(... terms_version=tickets.TERMS_VERSION, lang=chat.lang, via='telegram')`.
   The account email is used. An account without an email is told to add one in the dashboard. The bot replies with
   a URL button to the StartPay address. Every `PaymentError` or `OrderError` is shown as its message, translated
   when there is a Persian string for it.
5. Payment comes back through the existing `/pay/callback` and `reconcile`. Where those already queue
   `payments.send_mails` on a change to `paid`/`paid_unfulfilled`, they also queue `telegram.notify_payment(pid)`
   when the order's `via` is `'telegram'` and the account has a linked chat. That sends "Your pass is active until
   …" (or, if the payment went through but no pass could be granted, the same wording as the unfulfilled email).
   The browser redirect to the dashboard stays as it is.

`orders.create` and `payments.start` get a `via: str = "web"` keyword, stored on the order. No other order rules
change: the limits, capacity checks and terms checks all apply as they do on the site.

**My passes** lists the account's running and queued passes, with the same fields and remaining amounts the
dashboard card shows (from the function that card already uses), plus a link to the dashboard.

## 7. Support and feedback

- **Support** or **Feedback** sets `state`, and the bot asks for the message. The user's next text, photo or
  document becomes an `inbox_messages` row (`direction='in'`), then `state` is cleared and the bot confirms.
  Unlinked users can do this too.
- Each incoming message is copied to `admin_chat_id` with a header:
  `#support` or `#feedback` · name · @username · account email or "not linked" · a dashboard Inbox link. A file is
  sent with `copyMessage`. The copy's message id is stored in `admin_msg_id`.
- **Answering in Telegram:** in the admin chat, a reply to such a copy, made by a member who is a gateway admin, is
  sent to the user's chat, and an `out` row is written. A Telegram user counts as an admin when their linked account
  is an admin. Replies from anyone else are ignored. The bot reacts ✅ on the admin's reply once it is delivered, or
  replies "Not delivered: the user blocked the bot" (and sets `blocked`).
- **Answering in the dashboard:** a new admin **Inbox** page. On the left is a thread list (one per chat: newest
  first, an unread count, a kind badge, linked account). On the right is the thread with a reply box. The API:
  `GET /api/admin/inbox`, `GET /api/admin/inbox/{chat_id}` (marks it read), `POST /api/admin/inbox/{chat_id}`
  `{text}`. A reply sent here is also posted in the admin chat ("↩︎ answered in dashboard: …"), so both places show
  the whole conversation.
- Both answer paths call one function, `telegram.send_reply(conn, cfg, actor, chat_id, text)`: it sends the text,
  writes the `out` row and marks the thread read.
- A plain message with no `state` set is treated as support, so nothing a user writes is lost.

## 8. Limits and safety

- Per chat: 20 incoming messages per hour and 5 payment starts per hour. Past that, the bot answers "Too many
  messages; try again later" once and stores nothing. Order and payment rate limits still apply as they are.
- Outgoing sends go through one helper. On a 429 it waits `retry_after` once and retries. A 403 sets `blocked`.
  Other errors are logged, and the update counts as handled. The bot token never appears in a log line
  (`httpx` URLs are logged with the token masked).
- Updates from groups other than `admin_chat_id`, and from channels or inline queries, are ignored.
- User text forwarded to the admin chat is sent without `parse_mode`, so it cannot inject formatting or links.

## 9. Testing

The tests use a fake Bot API, an `httpx.MockTransport` that records calls and returns canned results, and feed
updates to the webhook route through the FastAPI test client:

- a webhook without the secret header, or with a wrong one, gets 403;
- linking: a valid code, an expired code, and relinking (the old chat is unlinked);
- buying: unlinked; linked with ZarinPal off; the full happy path to a StartPay URL with `via='telegram'`; the
  callback queues `notify_payment` only for a telegram order;
- support and feedback: an incoming message is stored and copied to the admin chat; an admin's reply there reaches
  the user, while a non-admin's reply is ignored; a dashboard reply reaches the user and is echoed to the admin chat;
  a 403 marks the chat blocked;
- the rate limit;
- with no `[telegram]` section: no route, and the link API returns 404.
