# Telegram Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Telegram bot on the dashboard app where a linked user buys a pass with ZarinPal, anyone sends feedback or
support messages, and admins answer support by replying in a private admin group.

**Architecture:** One new module, `telegram.py`, holds a thin Bot API client (`httpx`) and an update router. It runs
inline in a `POST /telegram/webhook` route on the dashboard app, on the dashboard's loop and connection. Buying reuses
`payments.start` / `payments.pay_order` (orders gain a `via` column). After payment, `/pay/callback` and reconcile call
`telegram.after_payment`. "My passes" uses the pass card, extracted from `/api/me/tickets` into `passcard.py`.

**Tech Stack:** Python 3, FastAPI, SQLite, httpx (`MockTransport` in tests), pytest (asyncio auto mode), vanilla JS
dashboard.

**Spec:** `docs/superpowers/specs/2026-11-05-telegram-bot-design.md` (revision 2). Read it before starting any task.

## Global Constraints

- Work directly on `master`: no branches, no worktrees, subagents included.
- Commit only the files your task touches. `src/claude_proxy/static/i18n.js`, `home.*`, `index.html`, `privacy.html`
  and `blog.py` have uncommitted edits from another session. Never `git add -A`, never stage their hunks, and never
  reset or checkout them. Task 10 shows how to stage only your own lines in `i18n.js`.
- No new dependencies: the Bot API is called with `httpx`, which is already a dependency.
- Secrets come from the environment only: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`. `[telegram]` in the TOML
  takes only `bot_username` and `admin_chat_id`.
- The bot is off unless `[telegram]`, both secrets and `[listener] dashboard_url` are all present. Off means no
  webhook route, `GET /api/telegram` returns `{"on": false}`, and `POST /api/telegram/link` returns 404.
- Persian wording: کلاد, never Claude Code; use the loanwords سیستم, سرویس and لینک; never "team" (تیم).
- User text forwarded to the admin group is sent without `parse_mode`.
- The bot token never appears in a log line, including httpx's own "HTTP Request:" INFO line.
- Never call the real Telegram API or run `telegram set-webhook` against the live bot while developing. Tests use
  `FakeTG`.
- Run tests with `.venv/bin/pytest`. Before each commit, run the whole suite (`.venv/bin/pytest -q`), which must pass.
- Commit messages follow the repo's style (a plain sentence on what changed and why) and end with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A Telegram user with no `username` or `first_name`:** the chat row stores NULLs, and the admin-group header leaves
   those parts out instead of printing "None" or "@None". Test in Task 9.
2. **An old button after prices or sales changed** (a tier now sold out, an unknown tier, a length no longer on sale):
   the bot shows the current tier list again, never an error or a stack trace. Test in Task 7.
3. **Admin-group traffic that is not a reply to a relayed message** (plain chatter, a reply to someone else's message):
   ignored silently, with no Telegram calls. Test in Task 9.
4. **A malformed webhook body** (not JSON, a JSON list, an update with no chat): the route answers 200 and nothing
   besides the update id is stored, so Telegram stops retrying. Test in Task 5.
5. **A buyer who blocked the bot before their payment settled:** `after_payment` marks the chat blocked, the payment
   stays paid, and the callback still redirects. Test in Task 8.

---

### Task 1: `[telegram]` config

**Files:**
- Modify: `src/claude_proxy/config.py` (new `TelegramConfig` after `ZarinpalConfig`, around line 202; a `telegram`
  field on `Config`; loading after the `[zarinpal]` block in `Config.load`, around line 520)
- Test: `tests/test_telegram_config.py`

**Interfaces:**
- Produces: `config.TelegramConfig(bot_username: str, admin_chat_id: int)` with `.token() -> str | None` and
  `.webhook_secret() -> str | None`. `Config.telegram: TelegramConfig | None` (None without the section).

- [ ] **Step 1: Write the failing test**

```python
"""Telegram bot design, section 2: the [telegram] section and its environment secrets."""
import pytest

from claude_proxy.config import Config, ConfigError, TelegramConfig


def write(tmp_path, body):
    p = tmp_path / "c.toml"
    p.write_text(body)
    return str(p)


def test_no_section_is_none(tmp_path):
    assert Config.load(write(tmp_path, "")).telegram is None


def test_section_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    cfg = Config.load(write(tmp_path, '[telegram]\nbot_username = "rahkar_bot"\nadmin_chat_id = -100123\n'))
    assert cfg.telegram == TelegramConfig(bot_username="rahkar_bot", admin_chat_id=-100123)
    assert (cfg.telegram.token(), cfg.telegram.webhook_secret()) == ("123:abc", "s3cret")


def test_secrets_missing_read_none(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    t = TelegramConfig(bot_username="b", admin_chat_id=-1)
    assert (t.token(), t.webhook_secret()) == (None, None)


@pytest.mark.parametrize("body", [
    '[telegram]\nbot_username = "b"\nadmin_chat_id = -1\nbot_token = "x"\n',   # a secret in the file: refused
    '[telegram]\nadmin_chat_id = -1\n',
    '[telegram]\nbot_username = ""\nadmin_chat_id = -1\n',
    '[telegram]\nbot_username = "@b"\nadmin_chat_id = -1\n',
    '[telegram]\nbot_username = "b"\n',
    '[telegram]\nbot_username = "b"\nadmin_chat_id = 0\n',
    '[telegram]\nbot_username = "b"\nadmin_chat_id = true\n',
    '[telegram]\nbot_username = "b"\nadmin_chat_id = "-1"\n',
])
def test_bad_section_refused(tmp_path, body):
    with pytest.raises(ConfigError, match=r"\[telegram\]"):
        Config.load(write(tmp_path, body))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_telegram_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'TelegramConfig'`

- [ ] **Step 3: Implement**

In `config.py`, after the `ZarinpalConfig` dataclass:

```python
@dataclass
class TelegramConfig:
    """[telegram] (Telegram bot design, section 2). The bot token and the webhook secret are secrets:
    TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET in the environment."""
    bot_username: str = ""
    admin_chat_id: int = 0   # the private admin group (a negative id)

    def token(self) -> str | None:
        return os.environ.get("TELEGRAM_BOT_TOKEN") or None

    def webhook_secret(self) -> str | None:
        return os.environ.get("TELEGRAM_WEBHOOK_SECRET") or None
```

On `Config`, after the `zarinpal` field:

```python
    telegram: TelegramConfig | None = None   # None without a [telegram] section: no bot
```

In `Config.load`, directly after the `if "zarinpal" in data:` block:

```python
        if "telegram" in data:
            tg = data["telegram"]
            if set(tg) - {"bot_username", "admin_chat_id"}:
                raise ConfigError(f"{toml_path}: [telegram] takes only bot_username and admin_chat_id; the token and webhook "
                                  "secret are TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET in the environment")
            name, chat = tg.get("bot_username"), tg.get("admin_chat_id")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,64}", name):
                raise ConfigError(f"{toml_path}: [telegram] bot_username must be the bot's username, without the @")
            if isinstance(chat, bool) or not isinstance(chat, int) or chat == 0:
                raise ConfigError(f"{toml_path}: [telegram] admin_chat_id must be the admin group's chat id (a number)")
            cfg.telegram = TelegramConfig(bot_username=name, admin_chat_id=chat)
```

If `config.py` does not already `import re`, add it to the imports at the top.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_telegram_config.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/config.py tests/test_telegram_config.py
git commit -m "Telegram bot config: a [telegram] section (bot_username, admin_chat_id), with the token and webhook secret read from TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Schema, `orders.via`, deletion and retention

**Files:**
- Modify: `src/claude_proxy/db.py`: SCHEMA list (add the five statements after the last `CREATE TABLE` in `SCHEMA`);
  the `orders` CREATE TABLE (add `via`); `_migrate` (add the `via` column); `delete_user`; `cleanup`
- Test: `tests/test_telegram_schema.py`

**Interfaces:**
- Produces: the tables `telegram_chats`, `telegram_link_codes`, `telegram_updates`, `inbox_messages` and
  `inbox_admin_msgs`, exactly as in spec section 3, and `orders.via TEXT NOT NULL DEFAULT 'web'`. `db.cleanup()`
  returns three new keys: `telegram_link_codes`, `telegram_updates`, `telegram_chats`.

- [ ] **Step 1: Write the failing test**

```python
"""Telegram bot design, section 3: the tables, deleting an account, and retention."""
import time

from claude_proxy import db as dbm
from claude_proxy.db import create_user


def chat(conn, chat_id, user_id=None, last_seen=None):
    now = int(time.time())
    conn.execute("INSERT INTO telegram_chats(chat_id, user_id, last_seen, created_at) VALUES(?,?,?,?)",
                 (chat_id, user_id, last_seen or now, now))


def message(conn, chat_id, direction="in", actor=None):
    iid = conn.execute("INSERT INTO inbox_messages(chat_id, kind, direction, text, actor, created_at) VALUES(?,?,?,?,?,?)",
                       (chat_id, "support", direction, "hi", actor, int(time.time()))).lastrowid
    conn.execute("INSERT INTO inbox_admin_msgs(admin_msg_id, inbox_id) VALUES(?,?)", (iid + 1000, iid))
    return iid


def test_orders_via_defaults_to_web(db):
    conn = db[1]
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(orders)")}
    assert cols["via"][4] == "'web'"


def test_delete_user_removes_their_chat_and_messages(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    chat(conn, 5001, uid)
    message(conn, 5001)
    conn.execute("INSERT INTO telegram_link_codes(code_hash, user_id, expires_at) VALUES('h', ?, ?)", (uid, int(time.time()) + 600))
    dbm.revoke(conn, uid)
    dbm.delete_user(conn, uid)
    for table in ("telegram_chats", "inbox_messages", "inbox_admin_msgs", "telegram_link_codes"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_deleting_an_admin_keeps_the_threads_they_answered(db):
    conn = db[1]
    admin, _ = create_user(conn, "boss", role="admin")
    chat(conn, 5002)
    message(conn, 5002, "out", actor=admin)
    dbm.revoke(conn, admin)
    dbm.delete_user(conn, admin)
    assert conn.execute("SELECT actor FROM inbox_messages").fetchone()[0] is None


def test_cleanup_prunes_codes_updates_and_silent_unlinked_chats(db):
    conn = db[1]
    now = int(time.time())
    uid, _ = create_user(conn, "alice")
    conn.execute("INSERT INTO telegram_link_codes(code_hash, user_id, expires_at) VALUES('old', ?, ?), ('new', ?, ?)",
                 (uid, now - 1, uid, now + 600))
    conn.execute("INSERT INTO telegram_updates(update_id, at) VALUES(1, ?), (2, ?)", (now - 2 * 86400, now))
    chat(conn, 1, None, now - 181 * 86400)   # unlinked, silent: deleted
    message(conn, 1)
    chat(conn, 2, uid, now - 181 * 86400)    # linked: kept
    chat(conn, 3, None, now - 10 * 86400)    # recent: kept
    out = dbm.cleanup(conn, 180)
    assert (out["telegram_link_codes"], out["telegram_updates"], out["telegram_chats"]) == (1, 1, 1)
    assert [r[0] for r in conn.execute("SELECT chat_id FROM telegram_chats ORDER BY chat_id")] == [2, 3]
    assert conn.execute("SELECT COUNT(*) FROM inbox_messages").fetchone()[0] == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_telegram_schema.py -v`
Expected: FAIL with `sqlite3.OperationalError: no such table: telegram_chats` (and `KeyError: 'via'`)

- [ ] **Step 3: Implement**

Append to `SCHEMA` in `db.py`, as the list's last entries:

```python
    # Telegram bot (design 2026-11-05, section 3). A private chat's id is the Telegram user's id.
    """CREATE TABLE IF NOT EXISTS telegram_chats (
        chat_id INTEGER PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,   -- NULL until linked; at most one chat per account
        username TEXT,
        first_name TEXT,
        lang TEXT NOT NULL DEFAULT 'fa',
        state TEXT,                       -- what the next plain text means: 'feedback' | 'email' | NULL (support)
        pending TEXT,                     -- JSON: the purchase being chosen (tier, length, start_now, nonce)
        blocked INTEGER NOT NULL DEFAULT 0,
        last_seen INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS telegram_chats_user ON telegram_chats(user_id) WHERE user_id IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS telegram_link_codes (
        code_hash TEXT PRIMARY KEY,       -- sha256 of the code; the code itself is never stored
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires_at INTEGER NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY, at INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS inbox_messages (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL REFERENCES telegram_chats(chat_id) ON DELETE CASCADE,
        kind TEXT NOT NULL,               -- 'support' | 'feedback'
        direction TEXT NOT NULL,          -- 'in' (from the user) | 'out' (an admin's answer)
        media TEXT,                       -- NULL (text) | 'photo' | 'document' | 'voice'
        text TEXT NOT NULL,               -- the text, or a file's caption ('' when none)
        actor INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS inbox_chat ON inbox_messages(chat_id, id)",
    # Each admin-group message posted for an incoming one (its header and its copy): a reply to either maps back.
    """CREATE TABLE IF NOT EXISTS inbox_admin_msgs (
        admin_msg_id INTEGER PRIMARY KEY,
        inbox_id INTEGER NOT NULL REFERENCES inbox_messages(id) ON DELETE CASCADE
    )""",
```

In the `orders` CREATE TABLE, directly after its `lang TEXT NOT NULL DEFAULT 'en', ...` line, add:

```python
        via TEXT NOT NULL DEFAULT 'web',    -- where the buyer ordered: 'web' or 'telegram' (where they hear back)
```

In `_migrate`, after the `lang` column migration:

```python
    if "via" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN via TEXT NOT NULL DEFAULT 'web'")
```

In `delete_user`, directly before `conn.execute("DELETE FROM users WHERE id=?", (user_id,))`:

```python
    # Their Telegram chat and its messages go too (cascade); the FK alone would only unlink the chat.
    conn.execute("DELETE FROM telegram_chats WHERE user_id=?", (user_id,))
```

In `cleanup`, add three entries to the `out` dict, after `"device_requests"`:

```python
        "telegram_link_codes": conn.execute("DELETE FROM telegram_link_codes WHERE expires_at<?", (int(now),)).rowcount,
        "telegram_updates": conn.execute("DELETE FROM telegram_updates WHERE at<?", (int(now) - 86400,)).rowcount,
        # A chat that never linked and has been silent 180 days, with its messages (cascade).
        "telegram_chats": conn.execute("DELETE FROM telegram_chats WHERE user_id IS NULL AND last_seen<?",
                                       (int(now) - 180 * 86400,)).rowcount,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_telegram_schema.py tests/test_tickets_delete.py tests/test_orders.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/db.py tests/test_telegram_schema.py
git commit -m "Telegram bot tables (chats, hashed link codes, seen update ids, inbox messages and their admin-group copies) and orders.via; deleting an account deletes its chat and messages, and cleanup prunes expired codes, day-old update ids and unlinked chats silent for 180 days

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `via` on orders and payments

**Files:**
- Modify: `src/claude_proxy/orders.py` (`create`, around lines 125-198)
- Modify: `src/claude_proxy/payments.py` (`start`, around lines 129-144)
- Test: `tests/test_orders.py` and `tests/test_payments.py` (append)

**Interfaces:**
- Produces: `orders.create(..., via: str = "web")` and `payments.start(..., via: str = "web")`. The value is stored in
  `orders.via`. Any value other than `"telegram"` is stored as `"web"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders.py`. It already has `seeded`/`NOW` fixtures; use the file's existing helper for creating an
order and pass `via`. If none exists, use this test as written:

```python
def test_order_records_where_it_came_from(db):
    from claude_proxy import orders, tickets
    from tests.tickets_helpers import NOW, live, seeded
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    kw = dict(tier="lite", length="week", currency="EUR", name="Alice", email="a@example.com", user_id=ids["alice"],
              notify=False, now=NOW, terms_version=tickets.TERMS_VERSION)
    o = orders.create(conn, cfg, **kw, via="telegram")
    assert o["via"] == "telegram"
    orders.withdraw(conn, ids["alice"], o["id"], now=NOW)
    assert orders.create(conn, cfg, **kw, via="nonsense")["via"] == "web"
```

Append to `tests/test_payments.py` (its `env` fixture yields `conn, cfg, ids` plus the fake; adapt the unpacking to
match the fixture at the top of that file):

```python
async def test_start_records_via(env):
    conn, cfg, ids, zp = env[:4]
    user = conn.execute("SELECT * FROM users WHERE id=?", (ids["alice"],)).fetchone()
    r = await payments.start(conn, cfg, dict(user) | {"email": "a@example.com"}, "lite", "week", "a@example.com",
                             terms_version=tickets.TERMS_VERSION, via="telegram")
    assert orders.get(conn, payments.get(conn, r["payment_id"])["order_id"])["via"] == "telegram"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_orders.py::test_order_records_where_it_came_from tests/test_payments.py::test_start_records_via -v`
Expected: FAIL with `TypeError: create() got an unexpected keyword argument 'via'`

- [ ] **Step 3: Implement**

In `orders.create`'s signature, add `via: str = "web"` after `start_now: bool = False`. Add this line to the docstring:
`` `via`: where the buyer ordered, 'web' or 'telegram' (the bot tells them when it is paid). ``
After `lang = "fa" if lang == "fa" else "en"`, add:

```python
    via = "telegram" if via == "telegram" else "web"
```

In the INSERT, add `via` to the column list after `start_now`, add one more `?` to the VALUES, and append `via` to the
parameter tuple after `int(start_now)`:

```python
            "message, status, ip, admin_mail, buyer_mail, terms_version, lang, start_now, via) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?,?,?,?,?,?)",
            (now, now, user_id, name, email, tier, length, currency, usd, rate["rate"], amount, message,
             None if user_id is not None else ip, admin_mail, buyer_mail, terms_version, lang, int(start_now), via))
```

In `payments.start`, add `via: str = "web"` after `start_now: bool = False`, and pass `via=via` to `orders.create`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_orders.py tests/test_payments.py tests/test_payments_web.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/orders.py src/claude_proxy/payments.py tests/test_orders.py tests/test_payments.py
git commit -m "Orders and online payments record where the buyer ordered (via: web or telegram), so the bot can tell a Telegram buyer when their payment settles

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Extract the pass card into `passcard.py`

**Files:**
- Create: `src/claude_proxy/passcard.py`
- Modify: `src/claude_proxy/web.py` (the `me_tickets` route, around lines 1356-1445; the `from . import` line 39)
- Test: `tests/test_passcard.py`

**Interfaces:**
- Produces: `passcard.build(conn, cfg, user_id: int, now: float) -> dict`, exactly what `GET /api/me/tickets`
  returned for an enabled gateway, including the `limits.hand_over` step. The route becomes a thin wrapper.

- [ ] **Step 1: Write the failing test**

```python
"""Telegram bot design, section 6 (My passes): the pass card is built in one place."""
import time

from claude_proxy import passcard, tickets
from tests.test_tickets_web import env  # noqa: F401  (the fixture)
from tests.test_web import bearer
from tests.conftest import asgi_client
from claude_proxy.web import create_dashboard_app
from tests.tickets_helpers import live, user


async def test_route_and_build_agree(env):
    gw, conn, cfg, ids, keys = env
    now = time.time()
    live(conn, now)
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "EUR", now=now)
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()
    b = passcard.build(conn, cfg, ids["alice"], r["now"])
    assert r["current"]["tier"] == "lite"
    assert {k: v for k, v in b.items() if k != "now"} == {k: v for k, v in r.items() if k != "now"}
```

If `tickets.grant`'s signature differs (check it in `tickets.py`), copy the grant call used in
`tests/test_tickets_web.py` instead.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_passcard.py -v`
Expected: FAIL with `ImportError: cannot import name 'passcard'`

- [ ] **Step 3: Move the code with a script** (it is a verbatim move, so no hand copying)

```bash
.venv/bin/python - <<'EOF'
import pathlib
web = pathlib.Path("src/claude_proxy/web.py")
s = web.read_text()
start = s.index('        limits.hand_over(conn, cfg, user["id"], now)')
end = s.index("        return out\n", start) + len("        return out\n")
body = s[start:end]
web.write_text(s[:start] + '        return passcard.build(conn, cfg, user["id"], now)\n' + s[end:])
lines = [l[4:] if l.startswith("    ") else l for l in body.splitlines(keepends=True)]
code = "".join(lines).replace('user["id"]', "user_id").replace("display_currency()", 'next(iter(cfg.tickets.currencies), "USD")')
header = '''"""What a user's pass card shows (GET /api/me/tickets), built in one place so that the dashboard and the Telegram
bot's "My passes" always agree (Telegram bot design, section 6). Moved unchanged from the route, side effect
included: used-up passes give way to a queued one here (limits.hand_over), as they do on a request."""
from __future__ import annotations

from . import limits, payments, tickets


def build(conn, cfg, user_id: int, now: float) -> dict:
    """The card for an enabled gateway; the caller checks cfg.tickets.enabled."""
'''
pathlib.Path("src/claude_proxy/passcard.py").write_text(header + code)
EOF
```

Then add `passcard` to `web.py`'s `from . import api_credit, blog, ...` line, in alphabetical order. The route must now
read:

```python
    @app.get("/api/me/tickets")
    async def me_tickets(request: Request):
        user = principal(request)
        if not cfg.tickets.enabled:
            return {"enabled": False}
        now = time.time()
        return passcard.build(conn, cfg, user["id"], now)
```

Check that nothing else is left behind: `.venv/bin/python -m pyflakes src/claude_proxy/passcard.py src/claude_proxy/web.py`.
If pyflakes is not installed, run `.venv/bin/python -c "import claude_proxy.web, claude_proxy.passcard"` and rely on the
tests. Fix an undefined name in `passcard.py` by adding its import. If `display_currency` is now unused in `web.py`,
keep it anyway, since `/api/pricing` still uses it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_passcard.py tests/test_tickets_web.py tests/test_pass_accounting.py tests/test_tickets_used_up.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/passcard.py src/claude_proxy/web.py tests/test_passcard.py
git commit -m "The pass card moves from the /api/me/tickets route into passcard.build, unchanged, so the Telegram bot's My passes shows exactly what the dashboard does

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Bot core: client, texts, webhook, menu

**Files:**
- Create: `src/claude_proxy/telegram.py`
- Modify: `src/claude_proxy/web.py` (import `telegram`; webhook route inside `create_dashboard_app`, after the order
  routes)
- Test: `tests/test_telegram.py` (helpers and fixture at the top; later tasks append tests)

**Interfaces:**
- Consumes: `Config.telegram` (Task 1), the tables (Task 2).
- Produces:
  - `telegram.on(cfg) -> bool`
  - `telegram.Blocked(Exception)`
  - `telegram.Bot(token: str, http: httpx.AsyncClient)` with `async call(method: str, **params) -> Any | None`
  - `telegram.t(lang: str, key: str, **kw) -> str`
  - `telegram.TEXTS`
  - `telegram.Telegram(conn, cfg, http)` with `async handle(update: dict) -> None` and these methods, which later
    tasks extend: `chat(frm, cid) -> dict`, `account(chat) -> dict | None`, `say(chat, key, buttons=None, **kw)`,
    `menu(chat)`, `set_state(chat, state, pending)`, `pending(chat) -> dict | None`, `allow(cid, what, per_hour) -> bool`,
    `too_many(chat)`, `private_message(msg)`, `callback(q)`, `route(chat, action, arg) -> bool`, `on_text(chat, msg)`,
    `admin_message(msg)`, `dashboard() -> str`
  - `POST /telegram/webhook`

- [ ] **Step 1: Write the failing tests** (create `tests/test_telegram.py`)

```python
"""Telegram bot (design 2026-11-05), with the Bot API faked by FakeTG and ZarinPal by FakeZP."""
import itertools
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from claude_proxy import telegram
from claude_proxy.config import TelegramConfig
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_payments_web import env  # noqa: F401  (the fixture: tickets, IRT rate, ZarinPal faked)
from tests.test_tickets_web import env as tickets_env  # noqa: F401  (test_payments_web's env needs it)

TOKEN, SECRET = "123456:SECRET-TOKEN", "hook-secret"
ADMIN_CHAT, ALICE, ADMIN_TG = -100123, 5001, 7001
_ids = itertools.count(1)


class FakeTG:
    """Stands in for api.telegram.org. `fail[method]`: a list of error bodies, one used up per call."""
    def __init__(self):
        self.calls, self.fail, self.webhook_url = [], {}, ""
        self.ids = itertools.count(9000)

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if self.fail.get(method):
            e = self.fail[method].pop(0)
            return httpx.Response(e["error_code"], json={"ok": False, **e})
        if method == "getWebhookInfo":
            result = {"url": self.webhook_url}
        elif method in ("sendMessage", "copyMessage"):
            result = {"message_id": next(self.ids)}
        else:
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    def sent(self, method="sendMessage", chat_id=None) -> list[dict]:
        return [b for m, b in self.calls if m == method and (chat_id is None or b.get("chat_id") == chat_id)]

    def texts(self, chat_id) -> list[str]:
        return [b["text"] for b in self.sent("sendMessage", chat_id)]


def frm(uid, username="alice_tg", first_name="Alice"):
    return {"id": uid, "is_bot": False, "language_code": "fa",
            **({"username": username} if username else {}), **({"first_name": first_name} if first_name else {})}


def msg(chat_id, text=None, sender=None, **extra) -> dict:
    m = {"message_id": next(_ids), "chat": {"id": chat_id, "type": "private"}, "from": sender or frm(chat_id), **extra}
    if text is not None:
        m["text"] = text
    return {"update_id": next(_ids), "message": m}


def cb(chat_id, data, sender=None) -> dict:
    return {"update_id": next(_ids), "callback_query": {
        "id": f"q{next(_ids)}", "from": sender or frm(chat_id), "data": data,
        "message": {"message_id": next(_ids), "chat": {"id": chat_id, "type": "private"}}}}


def group(text, reply_to=None, sender=None, **extra) -> dict:
    m = {"message_id": next(_ids), "chat": {"id": ADMIN_CHAT, "type": "supergroup"}, "from": sender or frm(ADMIN_TG, "boss"),
         "text": text, **extra}
    if reply_to is not None:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": next(_ids), "message": m}


def link(conn, chat_id, user_id, lang="fa"):
    conn.execute("INSERT INTO telegram_chats(chat_id, user_id, lang, last_seen, created_at) VALUES(?,?,?,0,0) "
                 "ON CONFLICT(chat_id) DO UPDATE SET user_id=excluded.user_id", (chat_id, user_id, lang))


@pytest.fixture
def tg(env, monkeypatch):
    gw, conn, cfg, ids, keys, zp = env
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    cfg.telegram = TelegramConfig(bot_username="rahkar_bot", admin_chat_id=ADMIN_CHAT)
    fake = FakeTG()
    gw.http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return SimpleNamespace(gw=gw, conn=conn, cfg=cfg, ids=ids, keys=keys, zp=zp, fake=fake)


async def post(c, update, secret=SECRET):
    return await c.post("/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def app(tg):
    return asgi_client(create_dashboard_app(tg.gw))


# ---------- Task 5: core ----------

async def test_webhook_needs_the_secret(tg):
    async with app(tg) as c:
        assert (await post(c, msg(ALICE, "/start"), secret="wrong")).status_code == 403
        assert (await c.post("/telegram/webhook", json=msg(ALICE, "/start"))).status_code == 403
    assert tg.fake.calls == []


async def test_no_route_when_off(tg, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    async with app(tg) as c:
        assert (await post(c, msg(ALICE, "/start"))).status_code in (404, 405)   # no route (a page catch-all may answer 405)


async def test_start_shows_the_menu_in_persian(tg):
    async with app(tg) as c:
        assert (await post(c, msg(ALICE, "/start"))).status_code == 200
    (sent,) = tg.fake.sent(chat_id=ALICE)
    assert sent["text"] == telegram.t("fa", "menu")
    assert [b["callback_data"] for row in sent["reply_markup"]["inline_keyboard"] for b in row] == ["buy", "mine", "feedback", "lang"]
    row = tg.conn.execute("SELECT * FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()
    assert (row["username"], row["first_name"], row["lang"], row["user_id"]) == ("alice_tg", "Alice", "fa", None)


async def test_a_repeated_update_is_handled_once(tg):
    u = msg(ALICE, "/menu")
    async with app(tg) as c:
        await post(c, u)
        await post(c, u)
    assert len(tg.fake.sent(chat_id=ALICE)) == 1


async def test_lang_switches_and_sticks(tg):
    async with app(tg) as c:
        await post(c, cb(ALICE, "lang"))
        await post(c, msg(ALICE, "/menu"))
    assert tg.fake.texts(ALICE)[0] == telegram.t("en", "lang_set")
    assert tg.fake.texts(ALICE)[-1] == telegram.t("en", "menu")
    assert tg.fake.sent("answerCallbackQuery")[0].get("text") is None


async def test_an_unknown_button_is_answered_stale(tg):
    async with app(tg) as c:
        await post(c, cb(ALICE, "zzz:1"))
    assert tg.fake.sent("answerCallbackQuery")[0]["text"] == telegram.t("fa", "stale")


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", json.dumps({"update_id": 77}).encode(),
                                  json.dumps({"update_id": 78, "message": {"text": "hi"}}).encode()])
async def test_a_malformed_update_gets_200_and_nothing_else(tg, body):   # Review Focus 4
    async with app(tg) as c:
        r = await c.post("/telegram/webhook", content=body, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET,
                                                                     "content-type": "application/json"})
    assert r.status_code == 200 and tg.fake.calls == []
    assert tg.conn.execute("SELECT COUNT(*) FROM telegram_chats").fetchone()[0] == 0


async def test_rate_limit_answers_once_then_drops(tg):
    async with app(tg) as c:
        for _ in range(telegram.MSG_PER_HOUR + 3):
            await post(c, msg(ALICE, "/menu"))
    texts = tg.fake.texts(ALICE)
    assert texts.count(telegram.t("fa", "menu")) == telegram.MSG_PER_HOUR
    assert texts.count(telegram.t("fa", "too_many")) == 1


async def test_429_is_retried_and_403_marks_blocked(tg):
    tg.fake.fail["sendMessage"] = [{"error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 0}}]
    async with app(tg) as c:
        await post(c, msg(ALICE, "/menu"))
        assert len(tg.fake.sent(chat_id=ALICE)) == 2   # the retry
        tg.fake.fail["sendMessage"] = [{"error_code": 403, "description": "Forbidden: bot was blocked by the user"}]
        await post(c, msg(ALICE, "/menu"))
    assert tg.conn.execute("SELECT blocked FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] == 1


async def test_the_token_is_never_logged(tg, caplog):
    caplog.set_level(logging.INFO)
    tg.fake.fail["sendMessage"] = [{"error_code": 400, "description": "Bad Request"}]
    async with app(tg) as c:
        await post(c, msg(ALICE, "/menu"))
    assert "sendMessage" in caplog.text and TOKEN not in caplog.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_telegram.py -v`
Expected: FAIL with `ImportError: cannot import name 'telegram'`

- [ ] **Step 3: Implement `src/claude_proxy/telegram.py`**

```python
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
```

In `web.py`, add `telegram` to the `from . import ...` line (alphabetical, after `quota`), and add this inside
`create_dashboard_app`, after the order routes (for example right before `@app.get("/api/admin/orders")`):

```python
    # ---------- Telegram bot (design 2026-11-05) ----------

    tg = telegram.Telegram(conn, cfg, gw.http) if telegram.on(cfg) else None
    app.state.telegram = tg

    if tg is not None:
        @app.post("/telegram/webhook")
        async def telegram_webhook(request: Request):
            # No session: Telegram's secret-token header is the only credential (section 2).
            if not hmac.compare_digest(request.headers.get("x-telegram-bot-api-secret-token", ""), cfg.telegram.webhook_secret()):
                fail(403, "Forbidden.")
            try:
                update = await request.json()
            except ValueError:
                return {"ok": True}   # nothing to handle, and a retry would not help
            await tg.handle(update)
            return {"ok": True}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_telegram.py -v`
Expected: PASS (all Task 5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/telegram.py src/claude_proxy/web.py tests/test_telegram.py
git commit -m "Telegram bot core: a Bot API client that masks its token in logs, retries a 429 once and marks a 403 blocked; a webhook on the dashboard checked by its secret header, each update handled once under its chat's lock; the menu in Persian or English

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Linking an account

**Files:**
- Modify: `src/claude_proxy/telegram.py` (module functions; `/start <code>` and `/unlink` in `private_message`)
- Modify: `src/claude_proxy/web.py` (three routes next to the webhook)
- Test: `tests/test_telegram.py` (append)

**Interfaces:**
- Consumes: `Telegram.chat`, `say`, `menu` (Task 5)
- Produces:
  - `telegram.LINK_TTL_S = 600`
  - `telegram.new_link_code(conn, user_id, now=None) -> str`
  - `telegram.claim(conn, code, chat_id, now=None) -> tuple[dict, int | None] | None`
  - `telegram.unlink(conn, user_id, actor) -> bool`
  - `telegram.connected(conn, user_id) -> str | None`
  - `GET /api/telegram` -> `{"on": false}` or `{"on": true, "bot": str, "connected": str | null}`
  - `POST /api/telegram/link` -> `{"url": "https://t.me/<bot>?start=<code>"}`
  - `POST /api/telegram/unlink` -> `{"ok": true}`

- [ ] **Step 1: Write the failing tests** (append)

```python
# ---------- Task 6: linking ----------

from tests.test_web import admin_client, bearer  # noqa: E402


async def link_url(c, tg):
    r = await c.post("/api/telegram/link", headers=bearer(tg.keys["alice"]))
    assert r.status_code == 200, r.text
    return r.json()["url"]


async def test_link_from_the_dashboard(tg):
    async with app(tg) as c:
        url = await link_url(c, tg)
        assert url.startswith("https://t.me/rahkar_bot?start=")
        code = url.split("=", 1)[1]
        assert tg.conn.execute("SELECT COUNT(*) FROM telegram_link_codes WHERE code_hash=?", (code,)).fetchone()[0] == 0   # hashed
        await post(c, msg(ALICE, f"/start {code}"))
        st = (await c.get("/api/telegram", headers=bearer(tg.keys["alice"]))).json()
    assert st == {"on": True, "bot": "rahkar_bot", "connected": "@alice_tg"}
    assert tg.fake.texts(ALICE)[0] == telegram.t("fa", "linked", who="al…@example.com")
    assert tg.conn.execute("SELECT user_id FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] == tg.ids["alice"]


async def test_a_code_works_once_and_expires(tg):
    async with app(tg) as c:
        code = (await link_url(c, tg)).split("=", 1)[1]
        await post(c, msg(ALICE, f"/start {code}"))
        await post(c, msg(5002, f"/start {code}"))        # used
        old = telegram.new_link_code(tg.conn, tg.ids["alice"], now=0)
        await post(c, msg(5003, f"/start {old}"))          # expired
    assert tg.fake.texts(5002) == [telegram.t("fa", "link_bad")]
    assert tg.fake.texts(5003) == [telegram.t("fa", "link_bad")]
    assert tg.conn.execute("SELECT chat_id FROM telegram_chats WHERE user_id=?", (tg.ids["alice"],)).fetchone()[0] == ALICE


async def test_relink_moves_the_account_and_tells_the_old_chat(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        code = (await link_url(c, tg)).split("=", 1)[1]
        await post(c, msg(5009, f"/start {code}"))
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "relinked_away")]
    assert tg.conn.execute("SELECT chat_id FROM telegram_chats WHERE user_id=?", (tg.ids["alice"],)).fetchone()[0] == 5009
    assert tg.conn.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0] == "telegram_relink"


async def test_unlink_from_bot_and_dashboard(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        await post(c, msg(ALICE, "/unlink"))
        await post(c, msg(ALICE, "/unlink"))
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "unlinked"), telegram.t("fa", "not_linked")]
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        assert (await c.post("/api/telegram/unlink", headers=bearer(tg.keys["alice"]))).json() == {"ok": True}
    assert tg.conn.execute("SELECT user_id FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] is None


async def test_link_needs_csrf(tg):
    async with admin_client(tg.gw) as c:
        del c.headers["x-csrf-token"]
        assert (await c.post("/api/telegram/link")).status_code == 403


async def test_link_api_off_without_the_bot(tg, monkeypatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET")
    async with app(tg) as c:
        assert (await c.post("/api/telegram/link", headers=bearer(tg.keys["alice"]))).status_code == 404
        assert (await c.get("/api/telegram", headers=bearer(tg.keys["alice"]))).json() == {"on": False}


async def test_a_revoked_account_counts_as_unlinked(tg):
    from claude_proxy import db as dbm
    link(tg.conn, ALICE, tg.ids["alice"])
    dbm.revoke(tg.conn, tg.ids["alice"])
    async with app(tg) as c:
        await post(c, cb(ALICE, "buy"))
    assert tg.fake.texts(ALICE)[0].startswith(telegram.t("fa", "need_link", url="").split("\n")[0])
```

The last test needs Task 7's `buy` route. Mark it `@pytest.mark.skip(reason="needs Task 7")` now, and remove the mark in
Task 7.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_telegram.py -k "link or relink or csrf" -v`
Expected: FAIL (404 on `/api/telegram/link`)

- [ ] **Step 3: Implement**

Add to the `telegram.py` imports: `import hashlib`, `import secrets`, `from . import db, orders`. Then add these
module-level functions after `t()`:

```python
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
        old = conn.execute("SELECT chat_id FROM telegram_chats WHERE user_id=? AND chat_id!=?", (user["id"], chat_id)).fetchone()
        conn.execute("UPDATE telegram_chats SET user_id=NULL WHERE user_id=?", (user["id"],))
        conn.execute("UPDATE telegram_chats SET user_id=? WHERE chat_id=?", (user["id"], chat_id))
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
```

In `Telegram.private_message`, inside `if text.startswith("/"):`, add these two branches **before** the
`if cmd in ("/start", "/menu"):` line:

```python
            if cmd == "/start" and arg.strip():
                return await self.start_code(chat, arg.strip())
            if cmd == "/unlink":
                if chat["user_id"] is not None and unlink(self.conn, chat["user_id"], chat["user_id"]):
                    chat["user_id"] = None
                    return await self.say(chat, "unlinked")
                return await self.say(chat, "not_linked")
```

Add this method to `Telegram`, after `toggle_lang`:

```python
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
```

In `web.py`, after the `if tg is not None:` webhook block (at the same indentation as `tg = ...`):

```python
    def need_telegram():
        if tg is None:
            fail(404, "The Telegram bot is not set up on this gateway.")

    @app.get("/api/telegram")
    async def telegram_status(request: Request):
        user = principal(request)
        if tg is None:
            return {"on": False}
        return {"on": True, "bot": cfg.telegram.bot_username, "connected": telegram.connected(conn, user["id"])}

    @app.post("/api/telegram/link")
    async def telegram_link(request: Request):
        user = principal(request, write=True)
        need_telegram()
        return {"url": f"https://t.me/{cfg.telegram.bot_username}?start={telegram.new_link_code(conn, user['id'])}"}

    @app.post("/api/telegram/unlink")
    async def telegram_unlink(request: Request):
        user = principal(request, write=True)
        need_telegram()
        telegram.unlink(conn, user["id"], user["id"])
        return {"ok": True}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_telegram.py -v`
Expected: PASS, except the one skipped test

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/telegram.py src/claude_proxy/web.py tests/test_telegram.py
git commit -m "Telegram linking: Connect Telegram makes a hashed, single-use, 10-minute start code; /start <code> links the chat, and a relink tells the chat it replaced; /unlink and the dashboard disconnect; link, relink and unlink are audited

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Buying and My passes

**Files:**
- Modify: `src/claude_proxy/telegram.py`
- Test: `tests/test_telegram.py` (append; un-skip the revoked-account test)

**Interfaces:**
- Consumes: `payments.start(..., via=)` (Task 3), `passcard.build` (Task 4), `Telegram` helpers (Task 5)
- Produces: `Telegram` methods `buy(chat)`, `pick_tier(chat, tier)`, `pick_length(chat, length)`,
  `pick_start(chat, arg)`, `summary(chat, pend)`, `pay(chat, nonce)`, `start_payment(chat, user, pend, email)`,
  `got_email(chat, msg)`, `continue_order(chat, oid)`, `cancel_order(chat, oid)`, `mine(chat)`,
  `error_text(lang, e) -> str`. Button data: `buy`, `mine`, `t:<tier>`, `l:<length>`, `s:a` / `s:n`, `p:<nonce>`,
  `oc:<order id>`, `ox:<order id>`.

- [ ] **Step 1: Write the failing tests** (append, and delete the `skip` mark from `test_a_revoked_account_counts_as_unlinked`)

```python
# ---------- Task 7: buying ----------

from claude_proxy import orders, payments, tickets  # noqa: E402


def buttons(sent) -> list[str]:
    return [b.get("callback_data") or b.get("url") for row in sent["reply_markup"]["inline_keyboard"] for b in row]


async def buy_week(c, tg, chat=ALICE):
    """Buy, Lite, a week, then return the summary's buttons."""
    await post(c, cb(chat, "buy"))
    await post(c, cb(chat, "t:lite"))
    await post(c, cb(chat, "l:week"))
    return buttons(tg.fake.sent(chat_id=chat)[-1])


async def test_buy_needs_a_linked_account(tg):
    async with app(tg) as c:
        await post(c, cb(ALICE, "buy"))
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "need_link", url="https://dash.rahkar.pro/dashboard")]


async def test_buy_paused_when_zarinpal_cannot_take_it(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    payments.set_pay_mode(tg.conn, tg.ids["admin"], "manual")
    async with app(tg) as c:
        await post(c, cb(ALICE, "buy"))
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "paused")]


async def test_buy_to_a_zarinpal_link(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        await post(c, cb(ALICE, "buy"))
        tiers = buttons(tg.fake.sent(chat_id=ALICE)[-1])
        assert "t:lite" in tiers
        await post(c, cb(ALICE, "t:lite"))
        assert "l:week" in buttons(tg.fake.sent(chat_id=ALICE)[-1])
        await post(c, cb(ALICE, "l:week"))
        summary = tg.fake.sent(chat_id=ALICE)[-1]
        (pay,) = buttons(summary)
        assert pay.startswith("p:") and "/privacy#terms" in summary["text"]
        await post(c, cb(ALICE, pay))
    ready = tg.fake.sent(chat_id=ALICE)[-1]
    assert ready["text"] == telegram.t("fa", "pay_ready")
    assert buttons(ready)[0].startswith("https://payment.zarinpal.com/pg/StartPay/")
    o = tg.conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1").fetchone()
    assert (o["via"], o["tier"], o["length"], o["lang"], o["email"]) == ("telegram", "lite", "week", "fa", "alice@example.com")


async def test_a_double_tap_opens_one_payment(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
        await post(c, cb(ALICE, pay))
    assert len(tg.zp.requests) == 1
    assert tg.fake.sent("answerCallbackQuery")[-1]["text"] == telegram.t("fa", "stale")


async def test_an_open_order_offers_continue_or_cancel(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
        await post(c, cb(ALICE, "buy"))
        offer = tg.fake.sent(chat_id=ALICE)[-1]
        oid = tg.conn.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert buttons(offer) == [f"oc:{oid}", f"ox:{oid}"]
        await post(c, cb(ALICE, f"oc:{oid}"))
        assert tg.fake.sent(chat_id=ALICE)[-1]["text"] == telegram.t("fa", "pay_ready")
        await post(c, cb(ALICE, f"ox:{oid}"))
    assert orders.get(tg.conn, oid)["status"] == "withdrawn"
    assert "t:lite" in buttons(tg.fake.sent(chat_id=ALICE)[-1])   # back to choosing


async def test_no_email_asks_for_one(tg):
    tg.conn.execute("UPDATE users SET email=NULL WHERE id=?", (tg.ids["alice"],))
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
        assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "ask_email")
        await post(c, msg(ALICE, "not an email"))
        assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "bad_email")
        await post(c, msg(ALICE, "alice@mail.example"))
    assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "pay_ready")
    assert tg.conn.execute("SELECT email FROM orders ORDER BY id DESC LIMIT 1").fetchone()[0] == "alice@mail.example"
    assert tg.conn.execute("SELECT email FROM users WHERE id=?", (tg.ids["alice"],)).fetchone()[0] is None


@pytest.mark.parametrize("data", ["t:nope", "l:year"])   # a tier, a length no longer (or never) on sale
async def test_an_old_button_shows_the_tiers_again(tg, data):   # Review Focus 2
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        await post(c, cb(ALICE, "buy"))
        await post(c, cb(ALICE, "t:lite"))
        await post(c, cb(ALICE, data))
    assert "t:lite" in buttons(tg.fake.sent(chat_id=ALICE)[-1])


async def test_a_zarinpal_failure_is_shown_in_persian(tg):
    from claude_proxy import zarinpal
    link(tg.conn, ALICE, tg.ids["alice"])
    tg.zp.request_answer = zarinpal.ZarinpalUnavailable("down")
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
    assert tg.fake.texts(ALICE)[-1] == telegram.FA_ERRORS[payments.UNAVAILABLE]


async def test_payment_starts_are_rate_limited(tg, monkeypatch):
    monkeypatch.setattr(telegram, "PAY_PER_HOUR", 1)
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
        oid = tg.conn.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 1").fetchone()[0]
        await post(c, cb(ALICE, f"oc:{oid}"))
    assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "too_many")
    assert len(tg.zp.requests) == 1


async def test_my_passes(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        await post(c, cb(ALICE, "mine"))
        assert tg.fake.texts(ALICE)[-1].startswith(telegram.t("fa", "mine_none"))
        user = tg.conn.execute("SELECT * FROM users WHERE id=?", (tg.ids["alice"],)).fetchone()
        tickets.grant(tg.conn, tg.cfg, tg.ids["admin"], user, "lite", "week", "IRT")
        await post(c, cb(ALICE, "mine"))
    assert tg.fake.texts(ALICE)[-1].startswith("Lite: تا ")
```

If `tickets.grant`'s argument order differs in this checkout, copy the grant call `tests/test_tickets_web.py` uses.
Keep each assertion as written.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_telegram.py -k "buy or tap or open_order or email or old_button or zarinpal or rate_limited or passes or revoked" -v`
Expected: FAIL (the `buy` button is answered stale)

- [ ] **Step 3: Implement**

Add to the `telegram.py` imports: `from . import db, orders, passcard, payments, tickets` (this replaces the
`from . import db, orders` line from Task 6). After `TEXTS`, add:

```python
# Persian for the refusals a buyer in the bot can meet; any other message is shown as it is.
FA_ERRORS = {
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
```

Replace `Telegram.on_text` with:

```python
    async def on_text(self, chat: dict, msg: dict) -> None:
        if chat["state"] == "email":
            return await self.got_email(chat, msg)
        await self.menu(chat)
```

In `Telegram.route`, add these branches before the final `else:`:

```python
        elif action == "buy":
            await self.buy(chat)
        elif action == "mine":
            await self.mine(chat)
        elif action == "t":
            await self.pick_tier(chat, arg)
        elif action == "l":
            return await self.pick_length(chat, arg)
        elif action == "s" and arg in ("a", "n"):
            return await self.pick_start(chat, arg)
        elif action == "p":
            return await self.pay(chat, arg)
        elif action in ("oc", "ox") and arg.isdigit():
            await (self.continue_order if action == "oc" else self.cancel_order)(chat, int(arg))
```

Add the buying section to `Telegram`, after `route`:

```python
    # ---------- buying (section 6) ----------

    def error_text(self, lang: str, e: Exception) -> str:
        return FA_ERRORS.get(str(e), str(e)) if lang != "en" else str(e)

    def amount(self, lang: str, n) -> str:
        return orders.fa_amount(n, payments.CURRENCY) if lang != "en" else payments._toman(n)

    def length_name(self, lang: str, length: str) -> str:
        return t(lang, f"len_{length}")

    def label(self, tier: str) -> str:
        x = self.cfg.tickets.tiers.get(tier)
        return x.label if x else tier

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
        user = await self.need_account(chat)
        if user is None:
            return
        o = self.open_order(user["id"])
        if o is not None:
            if o["currency"] == payments.CURRENCY and not payments._has_paid(self.conn, o["id"]):
                return await self.say(chat, "open_order", [[self.btn(chat, "btn_continue", f"oc:{o['id']}")],
                                                           [self.btn(chat, "btn_cancel_order", f"ox:{o['id']}")]],
                                      label=self.label(o["tier"]), length=self.length_name(chat["lang"], o["length"]),
                                      amount=self.amount(chat["lang"], o["quoted_amount"]))
            return await self.say(chat, "order_busy")
        tiers = self.tiers_on_sale(time.time())
        if tiers is None:
            return await self.say(chat, "paused")
        if not tiers:
            return await self.say(chat, "sold_out")
        self.set_state(chat, None, None)
        await self.say(chat, "choose_tier", [[{"text": x["label"], "callback_data": f"t:{x['tier']}"}] for x in tiers])

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
        _, replaces = tickets.next_start(self.conn, self.cfg, user["id"], now)
        if tickets.running(self.conn, user["id"], now) and not replaces:   # as the dashboard's order dialog offers it
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
        start, _ = tickets.next_start(self.conn, self.cfg, user["id"], now, pend["start_now"])
        pend = pend | {"nonce": secrets.token_urlsafe(6)}
        self.set_state(chat, None, pend)
        site = (self.cfg.listener.home_url or self.cfg.listener.dashboard_url).rstrip("/")
        await self.say(chat, "summary", [[self.btn(chat, "btn_pay", f"p:{pend['nonce']}")]],
                       label=x["label"], length=self.length_name(lang, pend["length"]), amount=self.amount(lang, p["amount"]),
                       start=tickets.terms_date(start, lang), terms=f"{site}/privacy#terms")

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
        user = await self.need_account(chat)
        if user is None:
            return
        lang, lines = chat["lang"], []
        if self.cfg.tickets.enabled:
            card = passcard.build(self.conn, self.cfg, user["id"], time.time())
            cur = card["current"]
            if cur and cur["passes"]:
                for p in cur["passes"]:
                    lines.append(t(lang, "mine_queued", label=p["label"], start=tickets.terms_date(p["starts_at"], lang)) if p.get("queued")
                                 else t(lang, "mine_running", label=p["label"], end=tickets.terms_date(p["effective_end"], lang)))
            else:
                if cur:
                    lines.append(t(lang, "mine_running", label=cur["label"], end=tickets.terms_date(cur["effective_end"], lang)))
                if card["queued"]:
                    q = card["queued"]
                    lines.append(t(lang, "mine_queued", label=q["label"], start=tickets.terms_date(q["starts_at"], lang)))
        text = "\n".join(lines) if lines else t(lang, "mine_none")
        await self.bot.call("sendMessage", chat_id=chat["chat_id"], text=f"{text}\n\n{t(lang, 'mine_more', url=self.dashboard())}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_telegram.py -v`
Expected: PASS (no skips left)

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/telegram.py tests/test_telegram.py
git commit -m "Telegram buying: a linked user picks a pass, a length and, beside a running pass, when it starts, then pays with ZarinPal through the usual order rules (one payment per summary, an open order offers continue or cancel, an account without an email is asked for one); My passes shows the dashboard card's passes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Telling the buyer when a payment settles

**Files:**
- Modify: `src/claude_proxy/telegram.py` (`after_payment`)
- Modify: `src/claude_proxy/web.py` (`pay_callback`)
- Modify: `src/claude_proxy/cli.py` (`_reconcile_payments`, `_reconcile_once`, and their call in `cmd_serve`)
- Test: `tests/test_telegram.py` (append)

**Interfaces:**
- Consumes: `orders.via` (Task 3), `Bot`, `Blocked`, `t` (Task 5)
- Produces: `telegram.after_payment(conn, cfg, http, pid) -> None`, which never raises.
  `cli._reconcile_once(conn, cfg, http=None)` and `cli._reconcile_payments(conn, cfg, http=None)`.

- [ ] **Step 1: Write the failing tests** (append)

```python
# ---------- Task 8: after payment ----------

import asyncio  # noqa: E402

from claude_proxy import cli, web  # noqa: E402


async def paid_via_bot(c, tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    (pay,) = await buy_week(c, tg)
    await post(c, cb(ALICE, pay))
    return buttons(tg.fake.sent(chat_id=ALICE)[-1])[0].rsplit("/", 1)[1]   # the authority


async def test_callback_tells_the_buyer_without_email(tg):
    assert tg.cfg.email is None
    async with app(tg) as c:
        auth = await paid_via_bot(c, tg)
        r = await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        await asyncio.gather(*list(web._mail_tasks))
    assert r.status_code == 302
    assert tg.fake.texts(ALICE)[-1].startswith("پرداخت انجام شد (کد پیگیری زرین‌پال: 201). بسته شما از ")


async def test_a_web_order_gets_no_telegram_message(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        r = await c.post("/api/orders/pay", headers=bearer(tg.keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        auth = r.json()["url"].rsplit("/", 1)[1]
        await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        await asyncio.gather(*list(web._mail_tasks))
    assert tg.fake.sent(chat_id=ALICE) == []


async def test_reconcile_tells_the_buyer(tg):
    async with app(tg) as c:
        await paid_via_bot(c, tg)
    tg.conn.execute("UPDATE payments SET created_at=created_at-7200")
    await cli._reconcile_once(tg.conn, tg.cfg, tg.gw.http)
    assert tg.fake.texts(ALICE)[-1].startswith("پرداخت انجام شد")


async def test_unfulfilled_payment_message(tg):
    async with app(tg) as c:
        auth = await paid_via_bot(c, tg)
    pid = tg.conn.execute("SELECT id FROM payments WHERE authority=?", (auth,)).fetchone()[0]
    tg.conn.execute("UPDATE payments SET status='paid_unfulfilled', ref_id='555' WHERE id=?", (pid,))
    await telegram.after_payment(tg.conn, tg.cfg, tg.gw.http, pid)
    assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "unfulfilled", ref="555")


async def test_a_blocked_buyer_does_not_break_the_payment(tg):   # Review Focus 5
    async with app(tg) as c:
        auth = await paid_via_bot(c, tg)
        tg.fake.fail["sendMessage"] = [{"error_code": 403, "description": "Forbidden: bot was blocked by the user"}]
        r = await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        await asyncio.gather(*list(web._mail_tasks))
    assert r.status_code == 302
    assert tg.conn.execute("SELECT status FROM payments WHERE authority=?", (auth,)).fetchone()[0] == "paid"
    assert tg.conn.execute("SELECT blocked FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_telegram.py -k "callback or web_order or reconcile or unfulfilled or blocked_buyer" -v`
Expected: FAIL with `AttributeError: module 'claude_proxy.telegram' has no attribute 'after_payment'`

- [ ] **Step 3: Implement**

Add to `telegram.py` as a module function, after `connected`:

```python
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
        if p["status"] == "paid" and tk is not None:
            text = t(lang, "paid", ref=p["ref_id"], start=tickets.terms_date(tk["starts_at"], lang),
                     end=tickets.terms_date(tk["effective_end"], lang))
        elif p["status"] in ("paid", "paid_unfulfilled"):
            text = t(lang, "unfulfilled", ref=p["ref_id"])
        else:
            return
        try:
            await Bot(cfg.telegram.token(), http).call("sendMessage", chat_id=chat["chat_id"], text=text)
        except Blocked:
            conn.execute("UPDATE telegram_chats SET blocked=1 WHERE chat_id=?", (chat["chat_id"],))
    except Exception:
        logger.exception("telegram notice for payment #%d failed", pid)
```

In `web.py` `pay_callback`, directly after the `if p.get("changed") and ... cfg.email is not None:` block and before
the `return RedirectResponse(...)`:

```python
        if p.get("changed") and p["status"] in ("paid", "paid_unfulfilled") and tg is not None:
            task = asyncio.create_task(telegram.after_payment(conn, cfg, gw.http, p["id"]))
            _mail_tasks.add(task)
            task.add_done_callback(_mail_done)
```

`tg` is defined further down in `create_dashboard_app` than `pay_callback`. Python looks it up when the route runs, so
this works as long as both are in the same function scope. If a linter objects, move the `tg = ...` line (and
`app.state.telegram = tg`) above the payment routes.

In `cli.py`, add `telegram` to `from . import db, estimates, limits, orders, payments`. Then change:

```python
async def _reconcile_payments(conn, cfg: Config, http=None):
    """Payments design, section 4 (Expiry): settle payments left `started`, every few minutes."""
    while True:
        await _reconcile_once(conn, cfg, http)
        await asyncio.sleep(RECONCILE_EVERY_S)


async def _reconcile_once(conn, cfg: Config, http=None):
    try:
        out = await payments.reconcile(conn, cfg)
    except Exception:
        logger.exception("reconciling payments failed")
        return
    if any(v for k, v in out.items() if k != "changed"):
        logger.info("reconciled unfinished payments: %s", out)
    if cfg.email is not None:
        for pid in out["changed"]:   # each moved to paid/paid_unfulfilled by this run only: mail once
            await payments.send_mails(conn, cfg, pid)
    if http is not None:
        for pid in out["changed"]:   # a bot order's buyer hears it in Telegram too (Telegram bot design, 6.6)
            await telegram.after_payment(conn, cfg, http, pid)
```

In `cmd_serve`'s `serve()`, change the reconcile task to pass the dashboard's client:

```python
            reconcile = asyncio.create_task(_reconcile_payments(dash_conn, cfg, dash_gw.http))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_telegram.py tests/test_payments.py tests/test_payments_web.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/telegram.py src/claude_proxy/web.py src/claude_proxy/cli.py tests/test_telegram.py
git commit -m "A Telegram order's buyer hears in the bot when their payment settles, from the ZarinPal callback or reconcile, with or without [email]: the pass's dates, or that it was paid but not started; a buyer who blocked the bot is marked blocked and the payment is unaffected

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Support and feedback relay

**Files:**
- Modify: `src/claude_proxy/telegram.py`
- Test: `tests/test_telegram.py` (append)

**Interfaces:**
- Consumes: the `inbox_messages` and `inbox_admin_msgs` tables (Task 2), `Telegram` (Tasks 5 and 7)
- Produces: `telegram._content(msg) -> tuple[str | None, str] | None`, `Telegram.incoming(chat, msg, kind)`, a real
  `Telegram.admin_message(msg)`, the `feedback` button, and the module constants `ANON`, `NOT_DELIVERED`,
  `NOT_DELIVERED_BLOCKED`.

- [ ] **Step 1: Write the failing tests** (append)

```python
# ---------- Task 9: support and feedback ----------

def admin_linked(tg):
    link(tg.conn, ADMIN_TG, tg.ids["admin"])


def inbox(tg):
    return [dict(r) for r in tg.conn.execute("SELECT * FROM inbox_messages ORDER BY id")]


async def test_a_message_goes_to_the_admin_group(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        u = msg(ALICE, "my key does not work")
        await post(c, u)
        await post(c, msg(ALICE, "still broken"))
    (header, header2) = tg.fake.sent("sendMessage", ADMIN_CHAT)
    assert header["text"] == "#support · Alice · @alice_tg · alice@example.com" and "parse_mode" not in header
    copy = tg.fake.sent("copyMessage")[0]
    assert (copy["chat_id"], copy["from_chat_id"], copy["message_id"]) == (ADMIN_CHAT, ALICE, u["message"]["message_id"])
    assert [(m["kind"], m["direction"], m["text"]) for m in inbox(tg)] == [("support", "in", "my key does not work"), ("support", "in", "still broken")]
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "got_support")]   # confirmed once per 10 minutes
    assert tg.conn.execute("SELECT COUNT(*) FROM inbox_admin_msgs").fetchone()[0] == 4


async def test_feedback_button_then_text(tg):
    async with app(tg) as c:
        await post(c, cb(ALICE, "feedback"))
        await post(c, msg(ALICE, "love it", sender=frm(ALICE, username=None, first_name=None)))   # Review Focus 1
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "ask_feedback"), telegram.t("fa", "got_feedback")]
    assert tg.fake.sent("sendMessage", ADMIN_CHAT)[0]["text"] == "#feedback · not linked"
    assert inbox(tg)[0]["kind"] == "feedback"
    assert tg.conn.execute("SELECT state FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] is None


async def test_photo_long_text_and_stickers(tg):
    async with app(tg) as c:
        await post(c, msg(ALICE, None, photo=[{"file_id": "F"}], caption="screenshot"))
        await post(c, msg(ALICE, "x" * (telegram.TEXT_MAX + 1)))
        await post(c, msg(ALICE, None, sticker={"file_id": "S"}))
    assert [(m["media"], m["text"]) for m in inbox(tg)] == [("photo", "screenshot")]
    assert tg.fake.texts(ALICE)[1:] == [telegram.t("fa", "too_long"), telegram.t("fa", "unsupported")]


async def admin_reply(c, tg, text="try again now", sender=None, **extra):
    await post(c, msg(ALICE, "help"))
    copy_id = tg.conn.execute("SELECT MAX(admin_msg_id) FROM inbox_admin_msgs").fetchone()[0]
    u = group(text, reply_to=copy_id, sender=sender, **extra)
    await post(c, u)
    return u


async def test_an_admin_reply_reaches_the_user(tg):
    admin_linked(tg)
    async with app(tg) as c:
        u = await admin_reply(c, tg)
    back = tg.fake.sent("copyMessage", ALICE)[0]
    assert (back["from_chat_id"], back["message_id"]) == (ADMIN_CHAT, u["message"]["message_id"])
    out = inbox(tg)[-1]
    assert (out["direction"], out["text"], out["actor"]) == ("out", "try again now", tg.ids["admin"])
    assert tg.fake.sent("setMessageReaction")[0]["reaction"] == [{"type": "emoji", "emoji": "👍"}]


async def test_a_reply_to_the_header_works_too(tg):
    admin_linked(tg)
    async with app(tg) as c:
        await post(c, msg(ALICE, "help"))
        header_id = tg.conn.execute("SELECT MIN(admin_msg_id) FROM inbox_admin_msgs").fetchone()[0]
        await post(c, group("on it", reply_to=header_id))
    assert len(tg.fake.sent("copyMessage", ALICE)) == 1


@pytest.mark.parametrize("who", ["stranger", "demoted", "anonymous"])
async def test_replies_that_do_not_count(tg, who):
    admin_linked(tg)
    sender = None
    if who == "stranger":
        sender = frm(8888, "rando")
    elif who == "demoted":
        tg.conn.execute("UPDATE users SET role='user' WHERE id=?", (tg.ids["admin"],))
    async with app(tg) as c:
        if who == "anonymous":
            await admin_reply(c, tg, sender=frm(1087968824, "GroupAnonymousBot"), sender_chat={"id": ADMIN_CHAT})
        else:
            await admin_reply(c, tg, sender=sender)
    assert tg.fake.sent("copyMessage", ALICE) == []
    group_texts = tg.fake.texts(ADMIN_CHAT)
    assert (telegram.ANON in group_texts) == (who == "anonymous")


async def test_reply_to_a_user_who_blocked_the_bot(tg):
    admin_linked(tg)
    async with app(tg) as c:
        await post(c, msg(ALICE, "help"))
        copy_id = tg.conn.execute("SELECT MAX(admin_msg_id) FROM inbox_admin_msgs").fetchone()[0]
        tg.fake.fail["copyMessage"] = [{"error_code": 403, "description": "Forbidden: bot was blocked by the user"}]
        await post(c, group("hello?", reply_to=copy_id))
        assert tg.fake.texts(ADMIN_CHAT)[-1] == telegram.NOT_DELIVERED_BLOCKED
        assert tg.conn.execute("SELECT blocked FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] == 1
        await post(c, msg(ALICE, "back"))
    assert tg.conn.execute("SELECT blocked FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] == 0


async def test_group_chatter_is_ignored(tg):   # Review Focus 3
    admin_linked(tg)
    async with app(tg) as c:
        await post(c, group("morning all"))
        await post(c, group("re: that", reply_to=424242))
    assert tg.fake.calls == []


async def test_other_groups_are_ignored(tg):
    async with app(tg) as c:
        u = group("hi")
        u["message"]["chat"] = {"id": -100999, "type": "supergroup"}
        await post(c, u)
    assert tg.fake.calls == [] and inbox(tg) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_telegram.py -k "admin or feedback or photo or reply or chatter or groups" -v`
Expected: FAIL (nothing is posted to the admin group)

- [ ] **Step 3: Implement**

Add to `telegram.py`, after `TEXT_MAX`:

```python
# The admin group reads English (section 7).
ANON = "Turn off 'Remain anonymous' to answer: the bot can't tell who you are."
NOT_DELIVERED = "Not delivered; try again."
NOT_DELIVERED_BLOCKED = "Not delivered: the user blocked the bot."
```

Add a module function after `t()`:

```python
def _content(msg: dict) -> tuple[str | None, str] | None:
    """(media, text) of a message the bot relays; None for a kind it does not (stickers, polls, locations...)."""
    if isinstance(msg.get("text"), str):
        return None, msg["text"]
    for media in ("photo", "document", "voice"):
        if media in msg:
            return media, str(msg.get("caption") or "")
    return None
```

Replace `Telegram.on_text` with:

```python
    async def on_text(self, chat: dict, msg: dict) -> None:
        if chat["state"] == "email":
            return await self.got_email(chat, msg)
        kind = "feedback" if chat["state"] == "feedback" else "support"
        if chat["state"] == "feedback":
            self.set_state(chat, None, None)
        await self.incoming(chat, msg, kind)
```

In `Telegram.route`, add before the final `else:`:

```python
        elif action == "feedback":
            self.set_state(chat, "feedback", None)
            await self.say(chat, "ask_feedback")
```

Add the support section to `Telegram`, after `mine`, replacing the placeholder `admin_message`:

```python
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
        got = _content(msg)
        if admin is None or got is None:
            return
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
        await self.bot.call("setMessageReaction", chat_id=self.admin_chat, message_id=msg["message_id"],
                            reaction=[{"type": "emoji", "emoji": "👍"}])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_telegram.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/telegram.py tests/test_telegram.py
git commit -m "Telegram support and feedback: any message (text, photo, file or voice) is stored and posted to the admin group as a header and a copy; an enabled admin's reply to either goes back to the user and is stored, an anonymous admin is told to show themselves, and a reply to a user who blocked the bot says so

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Webhook registration and the dashboard's Connect Telegram

**Files:**
- Modify: `src/claude_proxy/telegram.py` (`webhook_url`, `set_webhook`, `check_webhook`)
- Modify: `src/claude_proxy/cli.py` (the `telegram set-webhook` command; the startup check in `serve()`)
- Modify: `src/claude_proxy/static/app.js` (`nameDialog`; new `telegramRow`)
- Modify: `src/claude_proxy/static/i18n.js` (six `tg.*` keys in each language; staged alone)
- Test: `tests/test_telegram.py` (append), `tests/test_i18n.py` (already checks key parity, so it just has to pass)

**Interfaces:**
- Produces:
  - `telegram.webhook_url(cfg) -> str`
  - `telegram.set_webhook(cfg, http) -> bool`
  - `telegram.check_webhook(cfg, http) -> None` (logs a warning when the registered URL is not ours)
  - CLI `claude-proxy telegram set-webhook`

- [ ] **Step 1: Write the failing tests** (append)

```python
# ---------- Task 10: registration ----------

async def test_set_webhook_registers_our_url(tg):
    assert await telegram.set_webhook(tg.cfg, tg.gw.http) is True
    (hook,) = tg.fake.sent("setWebhook")
    assert hook == {"url": "https://dash.rahkar.pro/telegram/webhook", "secret_token": SECRET,
                    "allowed_updates": ["message", "callback_query"]}
    assert len(tg.fake.sent("setMyCommands")) == 2


async def test_startup_only_checks(tg, caplog):
    tg.fake.webhook_url = "https://elsewhere.example/telegram/webhook"
    await telegram.check_webhook(tg.cfg, tg.gw.http)
    assert [m for m, _ in tg.fake.calls] == ["getWebhookInfo"]
    assert "elsewhere.example" in caplog.text
    tg.fake.calls.clear()
    caplog.clear()
    tg.fake.webhook_url = telegram.webhook_url(tg.cfg)
    await telegram.check_webhook(tg.cfg, tg.gw.http)
    assert caplog.text == ""


def test_cli_refuses_when_off(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="The bot is off"):
        cli.main(["telegram", "set-webhook"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_telegram.py -k "webhook or startup or cli" -v`
Expected: FAIL with `AttributeError: ... 'set_webhook'`

- [ ] **Step 3: Implement**

Add to `telegram.py`, after `after_payment`:

```python
COMMANDS = {
    "fa": [("menu", "منو"), ("lang", "English / فارسی"), ("cancel", "لغو"), ("unlink", "قطع اتصال حساب")],
    "en": [("menu", "Menu"), ("lang", "English / فارسی"), ("cancel", "Cancel"), ("unlink", "Disconnect the account")],
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
```

In `cli.py`, add `import re` to the imports, add this command function before `main`:

```python
def cmd_telegram_set_webhook(args, cfg):
    if not telegram.on(cfg):
        sys.exit("The bot is off: it needs [telegram], TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET and [listener] dashboard_url.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", cfg.telegram.webhook_secret()):
        sys.exit("TELEGRAM_WEBHOOK_SECRET may use only A-Z, a-z, 0-9, _ and -, at most 256 characters.")

    async def run():
        async with httpx.AsyncClient() as http:
            return await telegram.set_webhook(cfg, http)
    if not asyncio.run(run()):
        sys.exit("Telegram refused the webhook; see the log above.")
    print(f"Webhook set: {telegram.webhook_url(cfg)}")
```

and register it in `main`, after the `limit` subcommands:

```python
    tgp = sub.add_parser("telegram", help="the Telegram bot").add_subparsers(dest="sub", required=True)
    tgp.add_parser("set-webhook", help="point the bot at this gateway's dashboard_url (the live gateway only)").set_defaults(func=cmd_telegram_set_webhook)
```

In `cmd_serve`'s `serve()`, after the `reconcile = ...` line:

```python
            if telegram.on(cfg):
                await telegram.check_webhook(cfg, dash_gw.http)
```

In `app.js` `nameDialog`, add a Telegram row for the signed-in user. Change the `<p><button class="btn" data-close>...`
line, the last line of the dialog's HTML template, so that a `<div id="tg-row"></div>` comes before it:

```js
    ${self ? `<div id="tg-row"></div>` : ""}
    <p><button class="btn" data-close>${t("app.cancel")}</button>${self && !admin ? ` <button class="btn danger" type="button" id="delete-me">${t("me.delete")}</button>` : ""}</p>`);
```

After `$("#f-name input", d).select();`, add:

```js
  if (self) telegramRow($("#tg-row", d));
```

Add this function directly after `nameDialog`:

```js
// Telegram bot design, section 4: connect this account to the bot (a 10-minute link), or disconnect it.
async function telegramRow(el) {
  let s;
  try { s = await api("/api/telegram"); } catch { return; }
  if (!s.on) return;
  el.innerHTML = s.connected
    ? `<p>${t("tg.connected", { who: bdi(s.connected) })} <button class="btn small" type="button" id="tg-unlink">${t("tg.disconnect")}</button></p>`
    : `<p>${t("tg.lede")}</p><p id="tg-go"><button class="btn small primary" type="button" id="tg-link">${t("tg.connect")}</button></p><p class="sub">${t("tg.warn")}</p>`;
  const link = $("#tg-link", el), unlink = $("#tg-unlink", el);
  if (link) link.onclick = async () => {
    const r = await apiPost("/api/telegram/link", {}, null, link);
    if (r) $("#tg-go", el).innerHTML = `<a class="btn small primary" href="${esc(r.url)}" target="_blank" rel="noopener">${t("tg.open")}</a>`;
  };
  if (unlink) unlink.onclick = async () => { if (await apiPost("/api/telegram/unlink", {}, null, unlink)) telegramRow(el); };
}
```

In `i18n.js`, add the English keys directly after `    "home.support_telegram": "Telegram",`:

```js
    "tg.lede": "Connect Telegram to buy passes and get support in the bot.",
    "tg.connect": "Connect Telegram",
    "tg.open": "Open Telegram",
    "tg.warn": "This link connects whoever opens it, for 10 minutes. Don't forward it.",
    "tg.connected": "Telegram: {who}",
    "tg.disconnect": "Disconnect",
```

and the Persian keys directly after `    "home.support_telegram": "تلگرام",`:

```js
    "tg.lede": "تلگرام را وصل کنید تا در ربات بسته بخرید و پشتیبانی بگیرید.",
    "tg.connect": "اتصال تلگرام",
    "tg.open": "باز کردن تلگرام",
    "tg.warn": "این لینک هر کسی را که بازش کند وصل می‌کند و ۱۰ دقیقه اعتبار دارد. آن را برای کسی نفرستید.",
    "tg.connected": "تلگرام: {who}",
    "tg.disconnect": "قطع اتصال",
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_telegram.py tests/test_i18n.py tests/test_client_scripts.py -q`
Expected: PASS

Then check the dashboard by hand, following the "Local dashboard check" memory: run the seeded dev gateway on :18081
with a `[telegram]` section and fake env secrets. Do **not** run `set-webhook`. Open your own name dialog, press
Connect Telegram, and confirm that "Open Telegram" appears with a `t.me/<bot>?start=` link. Check it in both languages.

- [ ] **Step 5: Commit, staging only your own lines in `i18n.js`**

`i18n.js` carries another session's uncommitted edits. Stage a copy of `HEAD`'s version with only your twelve lines
added:

```bash
.venv/bin/python - <<'EOF'
import subprocess
path = "src/claude_proxy/static/i18n.js"
base = subprocess.run(["git", "show", f"HEAD:{path}"], capture_output=True, text=True, check=True).stdout
work = open(path, encoding="utf-8").read()
out = []
for anchor in ('    "home.support_telegram": "Telegram",\n', '    "home.support_telegram": "تلگرام",\n'):
    i = work.index(anchor) + len(anchor)
    j = work.index('\n', work.index('"tg.disconnect"', i)) + 1
    base = base.replace(anchor, anchor + work[i:j], 1)
blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=base, capture_output=True, text=True, check=True).stdout.strip()
subprocess.run(["git", "update-index", "--cacheinfo", f"100644,{blob},{path}"], check=True)
EOF
git diff --cached src/claude_proxy/static/i18n.js   # must show exactly the twelve tg.* lines
git add src/claude_proxy/telegram.py src/claude_proxy/cli.py src/claude_proxy/static/app.js tests/test_telegram.py
git commit -m "Telegram bot registration and the dashboard: claude-proxy telegram set-webhook points the bot at dashboard_url (startup only warns when it points elsewhere), and the name dialog gets Connect Telegram (a 10-minute link) or Disconnect

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short src/claude_proxy/static/i18n.js   # still " M": the other session's edits stay unstaged
```

---

## Deployment notes (for the admin, after merge)

1. Create the bot with @BotFather and note its username. Turn off "Allow groups" except for adding it to the admin
   group. Add the bot to a private admin group and get that group's chat id.
2. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET` (A-Z, a-z, 0-9, `_`, `-`) in the live gateway's environment.
   Add `[telegram] bot_username = "..."` and `admin_chat_id = -100...` to its config.
3. On the live gateway only: `claude-proxy telegram set-webhook`.
4. Each admin opens their own name dialog, presses Connect Telegram, and opens the link, so their replies in the group
   count.
