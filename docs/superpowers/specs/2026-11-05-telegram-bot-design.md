# Telegram bot: buy passes, feedback, support (design 2026-11-05)

- **Status:** revision 2, 2026-11-05, after review.
- **Builds on:** `2026-10-04-order-requests-design.md` (orders), `2026-10-05-zarinpal-payments-design.md` (payments),
  `2026-11-04-admin-payment-links-design.md` (the `changed` hand-off after a payment settles).

## 1. Goal

Many buyers live on Telegram. One bot lets them:
- **buy a pass** and pay with ZarinPal, without opening the dashboard first;
- **send feedback**;
- **get support**, answered by the admin in a private Telegram admin group.

Success: a linked user goes from `/start` to an active pass with only the ZarinPal page in between. Every support or
feedback message reaches the admin group, and an admin's reply there reaches the user.

**Phase 2 (its own spec):** a dashboard Inbox for reading and answering these messages. Phase 1 already stores every
message in the DB, so that Inbox can be built on it later.

**Not in scope:**
- creating accounts in the bot;
- pass upgrades;
- payment other than ZarinPal;
- broadcasts;
- group chats for users.

## 2. Shape

- **`telegram.py`** (new): a thin Bot API client (`httpx`, no new dependency) and the update router.
- **Webhook:** `POST /telegram/webhook` on the dashboard app, served directly on the `dashboard_url` host.
  - Telegram does not follow redirects, so this route is not on the home host. (`web.py`'s `PUBLIC` list is the home
    host's redirect list, not an auth list, so it is not involved.)
  - There is no session. The route checks the `X-Telegram-Bot-Api-Secret-Token` header against the secret with
    `hmac.compare_digest`, and answers 403 on a mismatch.
- **Handling runs inline**, and the route answers 200 after it finishes. It runs on the dashboard's loop and
  connection (`dash_conn`), the same one the payment callback and reconcile share, so their single-loop
  "no await between check and grant" guard still holds (`cli.py`, `run_dashboard`).
  - Telegram retries a failed or timed-out delivery. Every update is first recorded in
    `telegram_updates(update_id PRIMARY KEY, at)` with `INSERT OR IGNORE`, and an update already there is skipped.
    Rows older than a day are pruned.
  - Updates from one chat are handled one at a time, under a per-chat `asyncio.Lock`. Two quick taps then cannot race
    on `state` or start two payments.
- **Config:**
  - The secrets live in the environment, as `ZARINPAL_MERCHANT_ID` does: `TELEGRAM_BOT_TOKEN` and
    `TELEGRAM_WEBHOOK_SECRET`.
  - `[telegram]` in the TOML takes only `bot_username` and `admin_chat_id` (a private group). Unknown keys are a
    `ConfigError`.
  - With the section or either secret missing, the bot is off: there is no route, no dashboard button, and nothing
    else changes.
- **Registering the webhook** is a CLI command, never done at startup:
  `claude-proxy telegram set-webhook` calls `setWebhook` (URL `<dashboard_url>/telegram/webhook`, the secret,
  `allowed_updates=["message","callback_query"]`), then `setMyCommands`. A dev gateway with a `[telegram]` section
  therefore cannot take the live webhook away. At startup the app only calls `getWebhookInfo`, and logs a warning if
  the URL is not its own.
- **Language:** Persian for everyone at first; `/lang` switches between Persian and English, and the choice is stored
  on the chat row. All strings go in one `TEXTS = {"fa": …, "en": …}` dict in `telegram.py`, following the wording
  rules: کلاد, not Claude Code, plus the loanwords سیستم, سرویس and لینک.
- **Every update re-reads the linked account.** An account that is disabled or revoked
  (`enabled=0 OR revoked_at IS NOT NULL`, the same test dashboard sessions use) counts as not linked.

## 3. Data

```sql
CREATE TABLE IF NOT EXISTS telegram_chats (
    chat_id    INTEGER PRIMARY KEY,          -- a private chat: equals the Telegram user id
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,   -- NULL until linked; at most one chat per account
    username   TEXT, first_name TEXT,
    lang       TEXT NOT NULL DEFAULT 'fa',
    state      TEXT,                         -- what the next plain text means: 'feedback' | 'email' | NULL (support)
    pending    TEXT,                         -- JSON of the purchase being chosen (tier, length, start_now, nonce)
    blocked    INTEGER NOT NULL DEFAULT 0,   -- the user blocked the bot (403 on send)
    last_seen  INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS telegram_chats_user ON telegram_chats(user_id) WHERE user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS telegram_link_codes (
    code_hash  TEXT PRIMARY KEY,             -- sha256 of the code; the code itself is never stored
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY, at INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS inbox_messages (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL REFERENCES telegram_chats(chat_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,                -- 'support' | 'feedback'
    direction  TEXT NOT NULL,                -- 'in' (from the user) | 'out' (the admin's answer)
    media      TEXT,                         -- NULL (text) | 'photo' | 'document' | 'voice'
    text       TEXT NOT NULL,                -- the text, or a file's caption ('' when none)
    actor      INTEGER REFERENCES users(id) ON DELETE SET NULL,   -- the admin who answered ('out')
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS inbox_chat ON inbox_messages(chat_id, id);

-- Each message the bot posts in the admin group for an incoming message (the header and the copy), so a reply to
-- either one maps back to the user.
CREATE TABLE IF NOT EXISTS inbox_admin_msgs (
    admin_msg_id INTEGER PRIMARY KEY,
    inbox_id     INTEGER NOT NULL REFERENCES inbox_messages(id) ON DELETE CASCADE
);
```

`orders` gets `via TEXT NOT NULL DEFAULT 'web'`, which is `'telegram'` for a bot order. It records where the buyer
should hear back.

**Retention and deletion:**
- `db.delete_user` deletes the account's `telegram_chats` row. Its inbox messages and admin-message mappings go with
  it through the cascades.
- Unlinking keeps the chat and its messages, since the person can still write to support.
- A chat that never linked and has been silent for 180 days (`last_seen`) is deleted, along with its messages, in the
  existing cleanup pass.
- Link codes are deleted once they expire, and update ids once they are a day old, in the same pass.
- The admin group keeps its own copies; deleting those is outside the bot.

## 4. Linking

- The dashboard (Account section) gets **Connect Telegram**.
  - `POST /api/telegram/link` uses `principal(request, write=True)`, so it is CSRF-checked, and requires an enabled
    account.
  - It makes a 16-byte url-safe code that is valid for 10 minutes and single use, and stores its sha256. Any earlier
    code of that user is replaced.
  - It returns `https://t.me/<bot_username>?start=<code>`, and the button opens it. The dashboard warns: "This link
    connects whoever opens it. Don't forward it."
- In the bot, `/start <code>` with a valid code links the chat and deletes the code.
  - If that account was linked to another chat, that chat is unlinked and gets a message: "Your account was connected
    to another Telegram account. If this wasn't you, change it in the dashboard."
  - The bot replies with the account's masked email (`orders.mask_email`).
  - A bad or expired code gets "This link expired; press Connect Telegram again", and nothing changes.
- The dashboard shows "Connected: @username" with **Disconnect**, and the bot's `/unlink` does the same thing. Link,
  relink and unlink are written to the audit log.

## 5. The menu

`/start` with no code, or `/menu`, shows inline buttons: **Buy a pass** · **My passes** · **Feedback** ·
**Language** (later also **Prices** and **Support**). A line under the buttons says to tap Support to message support.

- The flows use callback buttons. Callback data is a short action plus at most an id or nonce, always under
  64 bytes. The choices made so far are kept in `pending`.
- Free text depends on `state`: `feedback`, `email`, or NULL, which means support. `/cancel` clears `state` and
  `pending`.

## 6. Buying

1. **Not linked:** the bot explains linking and links to the dashboard. Nothing else happens.
2. **An open order already exists.** `orders.create` refuses a second open order, and an abandoned ZarinPal page keeps
   its order open until reconcile.
   - For an open Toman order with no payment that went through, the bot offers **Continue paying it**
     (`payments.pay_order`) or **Cancel it and choose again** (`orders.withdraw`).
   - Any other open order (manual, or another currency) gets "You have an order in progress", and Support is offered.
3. **On sale:** the tiers and lengths on sale, at Toman prices including VAT, come from `payments.prices()`, shown as
   on the site.
   - When ZarinPal can't take a payment (`payments.ready()` false, or `pay_mode` is `manual`), the bot says buying is
     paused and to tap Support with a question.
4. **Choosing:** the user picks a tier, then a length.
   - **Start after the current one** / **Start now** is offered exactly when the dashboard's order dialog offers it,
     and with the same `replaces` meaning. Building that dialog's offer is extracted from `/api/me/tickets` into a
     shared function the bot also calls.
   - The bot then shows a summary, a link to the terms (`/privacy#terms`), and **I agree, pay**, whose callback data
     carries a fresh nonce kept in `pending`.
5. **I agree, pay** goes ahead only when its nonce matches `pending`, and clears `pending` first, so a double tap or
   an old button does nothing.
   - The email is the account's. With none, `state='email'`: the bot asks for an address, validates it as the site
     does (`orders._email`) and stores it on the order only, as the dashboard does.
   - The bot calls `payments.start(... terms_version=tickets.TERMS_VERSION, lang=chat.lang, via='telegram')` and
     replies with a URL button to the StartPay address.
   - Every `PaymentError` or `OrderError` is shown as its message, translated when `telegram.py` has a Persian string
     for it.
6. **After payment:** the payment settles through the existing `/pay/callback` and reconcile (`cli._reconcile_once`).
   Wherever either one reports a payment as `changed`, it also awaits `telegram.after_payment(conn, cfg, pid)`, which
   does not depend on `cfg.email`. The function does nothing unless the order's `via` is `'telegram'` and the account
   still has a linked chat. It sends text written for the bot:
   - `paid`: "Payment received (ZarinPal reference …). Your pass is active from … until …" in the user's language.
   - `paid_unfulfilled`: "Payment received (ZarinPal reference …), but the pass could not start automatically. We'll
     start it or refund you; reply here with any questions." No buyer email exists for this case.
   The browser redirect to the dashboard stays as it is.

`orders.create` and `payments.start` get a `via: str = "web"` keyword, stored on the order. Otherwise every rule
applies as it does on the site: the limits, capacity and terms checks.

**My passes** lists the account's running and queued passes with what the dashboard card shows, plus a dashboard
link. The card is built inline in the `/api/me/tickets` route today, including its `limits.hand_over` step. That code
is extracted unchanged, side effect included, into one function that both the route and the bot call, so the two
always agree.

## 7. Support and feedback

- **Feedback** sets `state='feedback'`, and the next message is stored as feedback. **Support** sets
  `state='support'` for 30 minutes, renewed by each message and by an admin's reply; messages in that window are
  support. Any other message, with no `state`, gets the menu and reaches no one (changed 2026-11-05 after launch). Linked and unlinked users alike can send both.
- **What is accepted:** text up to 3500 characters (longer gets "Please split it into shorter messages"), a photo, a
  document, or a voice note. Each message of an album arrives and is stored separately. Stickers, polls and the like
  get "Please send text, a photo, a file or a voice note".
- **Copying to the admin group.** Each accepted message becomes an `inbox_messages` row (`direction='in'`), and the
  bot confirms receipt once per 10 minutes per chat. It then posts two messages to `admin_chat_id`, both without
  `parse_mode`, so user text cannot inject formatting:
  - a header: `#support` or `#feedback` · name · @username · account email, or "not linked";
  - the message itself via `copyMessage`. Telegram caps captions at 1024 characters, and `copyMessage` keeps the
    original.
  Both message ids go into `inbox_admin_msgs`.
- **Answering:** an admin replies, in the group, to either the header or the copy.
  - The reply counts only when its sender's Telegram id is a chat linked to an account with `role='admin'` that is
    enabled and not revoked, all re-read on each reply. Replies from anyone else are ignored.
  - An anonymous group admin (sender `GroupAnonymousBot`) gets "Turn off 'Remain anonymous' to answer".
  - The bot sends the reply to the user (text, or `copyMessage` for media), writes an `out` row, and reacts 👍 on the
    admin's message. If the user has blocked the bot (a 403), it replies "Not delivered: the user blocked the bot" and
    sets `blocked`.
  - A later message from the user clears `blocked`.
- The bot only needs to see replies to its own messages in the group, which Telegram delivers even with privacy mode
  on.

## 8. Limits and safety

- **Rate limits per chat:** 20 incoming messages per hour and 5 payment starts per hour. The counters live in memory
  in `telegram.py`, since one dashboard loop handles every update, and a restart resets them.
  - Past either limit, the bot answers "Too many messages; try again later" once per hour and stores nothing.
  - The order and payment rate limits apply on top, as they are.
- **Sending** goes through one helper.
  - On a 429 it waits `retry_after` once (at most 5 s) and retries.
  - A 403 sets `blocked`.
  - Other errors are logged, and the update still counts as handled.
  - The bot token never appears in a log line: the helper logs the method name, never the URL.
- **Ignored updates:** messages from groups other than `admin_chat_id`, from channels, and edited messages.

## 9. Testing

The tests use a fake Bot API, an `httpx.MockTransport` that records calls and returns canned results, and feed
updates to the webhook route through the FastAPI test client:

- the webhook, and its secret check:
  - no secret header, or a wrong one, gets 403;
  - a repeated `update_id` is handled once.
- linking:
  - a valid code links the chat;
  - an expired or already used code is refused;
  - a relink unlinks the old chat and notifies it;
  - `POST /api/telegram/link` without CSRF is refused;
  - a revoked account counts as not linked.
- buying:
  - an unlinked chat is refused;
  - with ZarinPal off, buying is paused;
  - the happy path reaches a StartPay URL with `via='telegram'`;
  - a double-tapped **I agree, pay** opens one payment;
  - an open order offers continue or cancel;
  - an account with no email is asked for one;
  - `after_payment` sends only for a telegram order and runs with `cfg.email` unset, from both the callback and
    reconcile.
- support and feedback:
  - a message is stored and posted to the admin group as header plus copy;
  - a reply to either one reaches the user;
  - a reply from a non-admin or a demoted admin is ignored;
  - a 403 marks the chat blocked;
  - an unsupported message type gets the "please send" answer;
  - `delete_user` removes the account's chat and messages.
- the rate limit;
- with no `[telegram]` section or no secrets: no route, and the link API returns 404;
- `telegram set-webhook` sends the expected URL, and startup never calls `setWebhook`.
