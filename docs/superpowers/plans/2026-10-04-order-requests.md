# Order Requests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Buyers file order requests (visitors from the home page cards, signed-in users from their dashboard price list); admins get an email and manage orders on a new Orders tab, granting tickets from them.

**Architecture:** A new `orders` table and `orders.py` module (creation, limits, status transitions, linking, retention) beside `tickets.py`; a tiny `mail.py` SMTP sender and a Turnstile verifier; mail is sent after the response in an asyncio task via `asyncio.to_thread`, results written back on the loop. Endpoints in `web.py`, UI in `app.js` (Orders tab, buyer view) and `home.js` (visitor dialog).

**Tech Stack:** Python 3, FastAPI, sqlite3 (one shared connection, `isolation_level=None`), `smtplib` (stdlib), `httpx` (already a dependency) for Turnstile, vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-order-requests-design.md` — binding. Read it fully before any task.

## Global Constraints

- Statuses: `new`, `contacted`, `done`, `declined`, `withdrawn`. Open = `new`|`contacted`. Transitions exactly as spec §3's table; anything else → 409.
- Mail columns: `pending`, `sent`, `failed`, `off`, `skipped`.
- Messages (verbatim): `"You already have an open order."` (409); `"Too many orders today; try again tomorrow or sign in."` (429); `"Orders are busy today; please sign in to order."` (429); `"The verification failed; please try again."` (400); Turnstile unreachable → 503 `Retry-After: 30`; grant/order mismatch → 409 `"This order was withdrawn or changed; reload."`.
- Limits: 1 open order per signed-in user; visitors 3/24h per IP and 3/24h per email (case-insensitive); 50/24h visitor orders globally; name ≤ 100 chars, message ≤ 1000, admin note ≤ 200; email regex `^[^@\s]+@[^@\s]+\.[^@\s]+$`.
- Buyer mail cap: 3 per address per 24h → `skipped`. Buyer mail contains no buyer-typed text (no name, no message). CR/LF stripped from name and email.
- SMTP: socket timeout 20 s; Turnstile timeout 10 s.
- `ip` NULLed when an order closes and on open orders older than 30 days.
- A typed email is never written to `users.email`.
- All order endpoints 404 while tickets are off (`need_tickets()`).
- Every server string into innerHTML goes through `esc()`.
- Shared repo: commit only your own paths; never reset/force-push others' work. Commit messages end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

- A visitor submits the same form twice quickly (double click): the second is subject to the per-email limit only; both stored is acceptable, but no 500. Test: two rapid POSTs both return 200 or 429, never 500.
- SMTP server hangs: the HTTP response returns immediately and the order shows `pending` then `failed`. Test with a fake `mail.send` that sleeps/raises.
- A signed-in user deleted while an order is open: the order keeps name/email, `user_id` NULL, the admin list still renders. Test in Task 2.
- Unicode/HTML in name and message (`<img onerror>`): stored verbatim, escaped in the dashboard, absent from buyer mail. Test in Tasks 3 and 5 (grep rendered JS for `esc(`).
- Tickets switched off: every order endpoint 404, the home page keeps today's behaviour. Test in Task 4.

---

## File Structure

- Create `src/claude_proxy/orders.py` — order domain logic (no HTTP).
- Create `src/claude_proxy/mail.py` — `send(email_cfg, to, subject, body)` over SMTP.
- Create `src/claude_proxy/turnstile.py` — `async verify(secret, token, ip) -> bool`, raises `TurnstileUnavailable`.
- Modify `src/claude_proxy/config.py` — `EmailConfig`, `[email]` parsing, `TicketsConfig.turnstile_site_key`.
- Modify `src/claude_proxy/db.py` — `orders` table in `SCHEMA`.
- Modify `src/claude_proxy/tickets.py` — `grant(..., order_id=None)`.
- Modify `src/claude_proxy/web.py` — endpoints; `/api/pricing` gains `turnstile_site_key`.
- Modify `src/claude_proxy/cli.py` — `_maintenance` calls `orders.clear_old_ips`.
- Modify `src/claude_proxy/static/app.js`, `app.css` — Orders tab, Order buttons, buyer notice.
- Modify `src/claude_proxy/static/home.js`, `home.html`, `home.css` — visitor dialog.
- Modify `config.example.toml`, `README.md`, `deploy/lightsail/README.md` — docs.
- Tests: `tests/test_orders.py` (domain + mail), `tests/test_orders_web.py` (API/UI), `tests/test_orders_config.py`.

---

### Task 1: Config, mail sender, Turnstile verifier

**Files:** Modify `config.py`; Create `mail.py`, `turnstile.py`; Test `tests/test_orders_config.py`.

**Interfaces — Produces:**
- `config.EmailConfig(smtp_host: str, from_: str, admin_to: str, smtp_port: int = 587, smtp_user: str = "")` with `password() -> str | None` reading `SMTP_PASSWORD`. TOML key `from` maps to `from_`.
- `Config.email: EmailConfig | None` (None without `[email]`).
- `TicketsConfig.turnstile_site_key: str = ""` and `TicketsConfig.turnstile_secret() -> str | None` (env `TURNSTILE_SECRET`); `TicketsConfig.turnstile_on() -> bool` = both set.
- `mail.send(email: EmailConfig, to: str, subject: str, body: str) -> None` — raises on failure. Port 465 → `smtplib.SMTP_SSL`, else `SMTP` + `starttls()`; `timeout=20`; login only when `smtp_user`. Uses `email.message.EmailMessage`.
- `turnstile.verify(secret: str, token: str, ip: str | None) -> bool` (async, `httpx.AsyncClient(timeout=10)`, POST `https://challenges.cloudflare.com/turnstile/v0/siteverify` form `secret,response,remoteip`, returns `json["success"] is True`); network error/5xx → raises `turnstile.TurnstileUnavailable`.

- [ ] **Step 1: Failing tests** in `tests/test_orders_config.py`, using the `load(tmp_path, body)` helper pattern from `tests/test_tickets_config.py`:
  - `[email]` with `smtp_host`, `from`, `admin_to` loads; `smtp_port == 587`, `smtp_user == ""`.
  - missing `admin_to` → `ConfigError` mentioning `admin_to`; unknown key `[email].bogus` → `ConfigError` mentioning `bogus`.
  - no `[email]` → `cfg.email is None`.
  - `[tickets] turnstile_site_key = "x"` with/without `TURNSTILE_SECRET` env (monkeypatch) → `turnstile_on()` True/False; missing secret logs a warning at load.
  - `mail.send` with a monkeypatched `smtplib.SMTP` fake records `starttls`, no `login` when `smtp_user` empty, `login(user, pw)` when set, `send_message` with correct To/From/Subject; port 465 uses `SMTP_SSL`; `timeout=20` passed.
  - `turnstile.verify` with `httpx.MockTransport` (monkeypatch the client factory): success true/false; `httpx.ConnectError` → `TurnstileUnavailable`.
- [ ] **Step 2:** `uv run pytest tests/test_orders_config.py -q` → FAIL.
- [ ] **Step 3:** Implement. In `config.py`, parse `[email]` after the other sections: validate required keys, refuse unknown ones, `smtp_port` int. Add `"turnstile_site_key"` to the plain `[tickets]` keys. Keep the module's idioms (dataclasses, `ConfigError` with `toml_path` prefix).
- [ ] **Step 4:** tests PASS.
- [ ] **Step 5:** Commit `Orders: email config, SMTP sender and Turnstile verifier`.

### Task 2: Schema and orders domain logic

**Files:** Modify `db.py` (SCHEMA); Create `orders.py`; Test `tests/test_orders.py`.

**Interfaces — Consumes:** `tickets.price_now`, `tickets.current_rate`, `tickets.round_local`, `tickets.currencies`, `tickets.sold_out`, `db.audit`, `db.create_user`.

**Produces** (all take `conn`, `now` keyword optional defaulting to `time.time()`):
- `class OrderError(Exception)` with `.status` (400/404/409/429).
- `create(conn, cfg, *, tier, length, currency, name, email, message="", user_id=None, ip=None, now=None) -> dict` — validates and inserts in one `BEGIN IMMEDIATE`; enforces limits (signed-in: one open; visitor: per-IP, per-email, global); refuses sold-out (409, "That ticket is sold out."), unknown tier/length/currency (400); quotes `quoted_usd/rate/amount` like `tickets.price_table`; sets `admin_mail`/`buyer_mail` to `pending` when `cfg.email` else `off`, and `buyer_mail='skipped'` when 3 buyer mails to this address (status `sent`|`pending`) in 24h already; strips CR/LF from name/email; writes audit `order_new`. Returns the row as dict.
- `get(conn, order_id) -> dict` (404 if missing).
- `set_status(conn, actor, order_id, to, *, note=None, now=None) -> dict` — enforces the transition table for `contacted`, `declined` (note required), `withdrawn`; NULLs `ip` on close; audit `order_<to>`.
- `set_note(conn, actor, order_id, note) -> dict` — ≤ 200 chars; audit `order_note`.
- `link(conn, cfg, actor, order_id, *, user_id=None, create=False) -> dict` — only open, unlinked visitor orders; `create=True` makes a user named after the order email via `db.create_user(conn, email)` and refuses 409 with the existing user's id in the message payload if a user with that name or `users.email` exists; never writes `users.email`; audit `order_link`.
- `suggest_user(conn, order) -> dict | None` — user whose lower(email) or lower(name) equals lower(order email).
- `mine(conn, user_id) -> dict | None` — open order, else latest closed with `seen_at` NULL and status in (`done`,`declined`); fields without `ip`, `admin_note`, `admin_mail`, `buyer_mail`.
- `withdraw(conn, user_id, order_id)` / `dismiss(conn, user_id, order_id)` — 404 unless the order is the caller's.
- `close_for_grant(conn, order_id, user_id, ticket_id, now)` — called inside the grant's transaction: refuses with `OrderError(409, "This order was withdrawn or changed; reload.")` unless open and `user_id` matches; sets `done`, `ticket_id`, NULLs `ip`.
- `set_mail(conn, order_id, which: str, state: str)` — `which` in (`admin_mail`,`buyer_mail`).
- `clear_old_ips(conn, now=None) -> int` — NULL `ip` on open orders older than 30 days.
- `admin_list(conn, status: str | None) -> list[dict]` — `status` one of `open`, `all`, or a status; newest first, limit 500; adds `user_name` from users.
- `new_count(conn) -> int`.

Schema (append to `db.SCHEMA`):

```sql
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    name TEXT NOT NULL, email TEXT NOT NULL,
    tier TEXT NOT NULL, length TEXT NOT NULL, currency TEXT NOT NULL,
    quoted_usd REAL NOT NULL, quoted_rate REAL NOT NULL, quoted_amount REAL NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('new','contacted','done','declined','withdrawn')),
    admin_note TEXT NOT NULL DEFAULT '',
    ticket_id INTEGER REFERENCES tickets(id),
    ip TEXT,
    admin_mail TEXT NOT NULL, buyer_mail TEXT NOT NULL,
    seen_at INTEGER
)
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at)
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)
```

- [ ] **Step 1: Failing tests** in `tests/test_orders.py` (use `tests.tickets_helpers.seeded`, `NOW`, `user`): create visitor and signed-in orders with quoted figures (Lite week EUR = 7.5 at 0.92); sold-out → 409; one open per user → 409 message verbatim; per-IP 4th → 429, per-email 4th (different case) → 429, 51st global → 429 busy message; name 101 chars / message 1001 / bad email → 400; CR/LF stripped; every transition in the table allowed and every other refused 409; decline without note → 400; `ip` NULL after close and after `clear_old_ips` at +31 days; link to existing user; create user named after email; create refused 409 when name or `users.email` taken; `users.email` never changed by any function; `suggest_user` matches case-insensitively; `mine` hides admin fields and dismissed/withdrawn orders; deleting the user keeps the order with `user_id` NULL; buyer cap → `skipped` on the 4th signed-in order to one address across users (close earlier ones first); `cfg.email is None` → both mail `off`; audit rows written.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement `orders.py` following `tickets.py` idioms (`_now`, `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` on `BaseException`, `db.audit`).
- [ ] **Step 4:** PASS; also `uv run pytest tests/test_tickets_schema.py -q`.
- [ ] **Step 5:** Commit `Orders: schema and domain logic`.

### Task 3: Mail dispatch and grant with order_id

**Files:** Modify `orders.py`, `tickets.py`; Test `tests/test_orders.py`.

**Produces:**
- `orders.admin_mail_text(cfg, order) -> tuple[str, str]` and `orders.buyer_mail_text(cfg, order) -> tuple[str, str]` (subject, body) per spec §5. Buyer body: tier label, length, quoted amount with currency, `cfg.tickets.how_to_buy`, the request sentence. No name, no message.
- `async orders.dispatch(conn, cfg, order_id)` — for each of admin/buyer whose state is `pending`: `await asyncio.to_thread(mail.send, cfg.email, to, subject, body)`; on success `set_mail(..., "sent")`, on any exception `set_mail(..., "failed")` and `logger.warning`. DB writes happen after the await, on the loop.
- `tickets.grant(..., order_id: int | None = None)` — inside its transaction, after the insert, calls `orders.close_for_grant(conn, order_id, user["id"], ticket_id, now)`; an `OrderError` rolls back the grant.

- [ ] **Step 1: Failing tests:** buyer text contains no name/message even with `<script>` in both; admin text contains them; `dispatch` with a fake `mail.send` recording calls → both `sent`; raising → `failed`; `off`/`skipped` not sent; grant with `order_id` marks `done` and sets `ticket_id`; grant with a withdrawn order → `OrderError` 409 and no ticket row; grant for a different user → 409; grant capacity failure leaves order `new`.
- [ ] **Step 2:** FAIL. **Step 3:** implement (import `orders` lazily inside `grant` to avoid an import cycle if needed). **Step 4:** PASS + `uv run pytest tests/test_tickets_capacity.py -q`.
- [ ] **Step 5:** Commit `Orders: mail dispatch and grants that close an order`.

### Task 4: HTTP API

**Files:** Modify `web.py`, `cli.py`; Test `tests/test_orders_web.py`.

**Consumes:** Task 1–3 interfaces; web helpers `admin(request, write=True)`, `principal(request, write=True)`, `fail`, `_json`, `need_tickets`, `client_ip(request)`, `ticket_call`.

Endpoints (spec §10). Map `OrderError` → `fail(e.status, str(e))`; for `link` 409 include `existing_user_id` in the JSON body. After a successful create, schedule `asyncio.create_task(orders.dispatch(conn, cfg, id))` (keep a reference in a module-level set until done, as asyncio requires) — the response must not await it.
- `POST /api/orders`: 404 unless `cfg.tickets.turnstile_on()`; verify Turnstile first (`TurnstileUnavailable` → 503 with `Retry-After: 30`; false → 400 verbatim); then `orders.create(..., ip=client_ip(request))`. Returns `{"ok": true}` only.
- `POST /api/me/orders` (`principal(write=True)`, not routes-only keys): email = `users.email` or body `email` (validated; stored on the order only). Returns `orders.mine`.
- `GET /api/me/orders` → `{"order": orders.mine(...)}`; withdraw/dismiss endpoints.
- `GET /api/admin/orders?status=open` → `{"orders": [...each with "suggested_user"...], "new": new_count}`; `POST /api/admin/orders/{id}` with `action` ∈ `contacted|decline|note|link`.
- `POST /api/admin/tickets`: pass `order_id` (int or None) to `tickets.grant`; `OrderError` → 409.
- `/api/pricing`: add `"turnstile_site_key"` when `turnstile_on()`.
- `/api/session` for admins: add `"orders_new": orders.new_count(conn)` inside the tickets block (admins only).
- `cli._maintenance`: add a third independent try block calling `orders.clear_old_ips(conn)`.

- [ ] **Step 1: Failing tests** (pattern from `tests/test_tickets_web.py`: `env` fixture, `admin_client`, `bearer`, `grant` helper; monkeypatch `turnstile.verify` and `mail.send`): public order needs Turnstile config (404 without), 400 on failed verify, 503 + Retry-After on unavailable, 200 stores with ip; response arrives while a fake `mail.send` blocks on a `threading.Event` (set after asserting status, then await dispatch tasks and check `sent`); signed-in order uses account email, typed email when none and `users.email` stays NULL; `/api/me/orders` never contains `ip`, `admin_note`, mail fields, other users' orders; admin list/actions/audit; link 409 carries `existing_user_id`; grant with `order_id` → done; withdrawn-then-grant → 409, no ticket; non-admin gets 403 on admin endpoints; tickets off → every order endpoint 404; `/api/pricing` exposes the site key only when on; double public POST never 500.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS + full `uv run pytest -q`.
- [ ] **Step 5:** Commit `Orders: API`.

### Task 5: Dashboard — Orders tab and buyer view

**Files:** Modify `static/app.js`, `static/app.css`; Test `tests/test_orders_web.py` (static checks, like existing dashboard tests that fetch `/static/app.js`).

- Add `orders: { label: "Orders", render: renderOrders, admin: true, feature: "tickets" }` to `VIEWS` after `tickets`; tab label shows a badge with `S.tickets.orders_new` when > 0.
- `renderOrders`: status filter (`open` default, `all`, each status), table per spec §7, mail state badges ("admin email failed", etc.), actions: Contacted, Decline (prompt for note), Note, Grant ticket. Grant on an unlinked visitor order first opens a link dialog: suggested user preselected (never auto-submitted), a user picker, and "Create user <email>"; a 409 offers `existing_user_id`. Then open the existing `grantDialog` with user/tier/length/currency prefilled and `order_id` included in the POST body. After success `render()`.
- Buyer view in `renderOverview` for non-admins: fetch `/api/me/orders` alongside `/api/me/tickets`; an open order card above the price list with Withdraw; closed undismissed notice with Dismiss; `priceListCard` gets an Order button per non-sold-out cell (disabled while an order is open), the home-page pick pre-selected; the order dialog asks currency (defaults to the price list currency), optional message, and email only when the account has none.
- All server strings via `esc()`.

- [ ] **Step 1: Failing tests:** `/static/app.js` contains `renderOrders`, `"/api/admin/orders"`, `"/api/me/orders"`, `order_id`; `node --check` passes (run via `subprocess` if `node` is available, else skip).
- [ ] **Step 2:** FAIL. **Step 3:** implement, matching existing dialog/table helpers (`grantDialog`, `confirmInline`, `alertInline`, `infoInline`, `esc`, `fmtDate`, `money`). **Step 4:** PASS; `node --check src/claude_proxy/static/app.js`.
- [ ] **Step 5:** Commit `Orders: dashboard tab and buyer view`.

### Task 6: Home page visitor dialog

**Files:** Modify `static/home.js`, `static/home.html`, `static/home.css`; Test `tests/test_orders_web.py`.

- When `data.turnstile_site_key` is set, **Get it** is a button opening a `<dialog>`: chosen tier/length/price, name, email, currency select (the page's currency), message, a Turnstile widget (load `https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit` only when the dialog first opens; `turnstile.render(el, {sitekey})`), the request sentence, and "Or sign in to order" linking to today's `/dashboard?tier=&length=`. Submit POSTs `/api/orders` with `turnstile_token`; success shows "Order received. Check your email."; errors show the server message. Without the key, **Get it** stays today's link. Sold-out stays disabled.
- CSP: if `web.py` sets a Content-Security-Policy for the home page, allow `https://challenges.cloudflare.com` in `script-src` and `frame-src`.

- [ ] **Step 1: Failing tests:** `/static/home.js` contains `"/api/orders"` and `turnstile`; with Turnstile on, `/api/pricing` has the key (already Task 4); CSP header on `/` (if any) allows `challenges.cloudflare.com`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS; `node --check src/claude_proxy/static/home.js`.
- [ ] **Step 5:** Commit `Orders: home page order dialog`.

### Task 7: Docs

**Files:** `config.example.toml`, `README.md`, `deploy/lightsail/README.md`.

- [ ] `config.example.toml`: commented `[email]` block and `turnstile_site_key` as in spec §6.
- [ ] `README.md`: an "Order requests" paragraph (how buyers order, Orders tab, env vars `SMTP_PASSWORD`, `TURNSTILE_SECRET`).
- [ ] Deploy README: setting the two env vars; the per-IP limit caveat (proxy on another host, CGNAT).
- [ ] Full suite `uv run pytest -q` green; commit `Orders: docs`.
