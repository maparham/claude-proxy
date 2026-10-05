# ZarinPal Payments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A signed-in user pays for a ticket in Toman through ZarinPal and the ticket is granted as soon as the payment is verified.

**Architecture:** A thin HTTP client (`zarinpal.py`) talks to ZarinPal. A `payments.py` module owns the `payments` table and the start / callback / expire logic, reusing `orders.create` and `tickets.grant` (which gains a `paid` mode that grants at the order's quote). `web.py` adds three endpoints and serves the callback on the home host; the dashboard's order dialog gets a Pay button.

**Tech Stack:** Python 3, FastAPI, SQLite, httpx, pytest (asyncio); vanilla JS dashboard with `i18n.js` (en + fa).

**Spec:** `docs/superpowers/specs/2026-10-05-zarinpal-payments-design.md`

## Global Constraints

- Work on `master`; commit only your own paths. Another session may have uncommitted edits in `src/claude_proxy/static/app.js` and `i18n.js`: never `git add -A`, never stash/reset others' work; stage hunks you wrote (`git add -p`) or wait until those files are clean.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Run tests with `uv run pytest -q <paths>`; the full suite (`uv run pytest -q`) takes ~3 minutes; run it in the last task.
- Payments are on only when: tickets enabled, `ZARINPAL_MERCHANT_ID` set, `IRT` in `[tickets.currencies]`, `[listener] home_url` set, and a `[zarinpal]` section exists.
- Currency is `"IRT"` (Toman), amounts sent to ZarinPal are integers.
- Production endpoints: `https://payment.zarinpal.com/pg/v4/payment/request.json`, `.../verify.json`, StartPay `https://payment.zarinpal.com/pg/StartPay/<authority>`. Sandbox: same paths on `https://sandbox.zarinpal.com`.
- Callback URL: `<home_url>/pay/callback`. Result page: `<dashboard_url>/dashboard#payment/<id>`.
- Stale rate: older than 36 h (`tickets.rate_is_stale`) → no payment starts (409).
- The merchant ID is never logged or sent to the browser; only `card_pan` is stored, never `card_hash`.
- User-facing dashboard strings go through `t()` with keys in both `I18N.en` and `I18N.fa` in `i18n.js`.

## Review Focus

1. **A reload or double callback after a successful payment** must not grant twice or call verify again → test in Task 4 (`test_repeated_callback_grants_once`).
2. **The admin changes the IRT rate or the price during the buyer's payment** → ticket granted at the paid amount → test in Task 2 (`test_paid_grant_uses_the_order_quote`) and Task 4.
3. **The admin declines the order while the buyer is on ZarinPal** → verified payment becomes `paid_unfulfilled`, never a silent loss → test in Task 4 (`test_declined_meanwhile_is_unfulfilled`).
4. **IRT amount as a float** (`round_local` returns `1250000.0`) → ZarinPal gets an int → test in Task 3 (`test_start_sends_integer_amount`).
5. **A signed-in user without an account email** → may pay with a typed email, an invalid one is a 400 before ZarinPal is called → test in Task 5 (`test_pay_needs_an_email`).

---

### Task 1: Config and the ZarinPal client

**Files:**
- Modify: `src/claude_proxy/config.py` (new `ZarinpalConfig`, `Config.zarinpal`, loading)
- Create: `src/claude_proxy/zarinpal.py`
- Test: `tests/test_zarinpal.py`
- Modify: `config.example.toml` (commented `[zarinpal]` block)

**Interfaces:**
- Produces: `config.ZarinpalConfig(sandbox: bool = False)` with `.merchant_id() -> str | None`; `Config.zarinpal: ZarinpalConfig | None`.
- Produces: `zarinpal.ZarinpalUnavailable(Exception)`, `zarinpal.ZarinpalRefused(Exception)` with `.code: int`, `.message: str`;
  `async zarinpal.request(merchant_id, amount: int, description: str, callback_url: str, email: str, order_id: int, sandbox: bool) -> str` (the authority);
  `async zarinpal.verify(merchant_id, amount: int, authority: str, sandbox: bool) -> dict` (`{"code", "ref_id", "card_pan"}`);
  `zarinpal.start_url(authority: str, sandbox: bool) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
"""ZarinPal client (payments design, section 4): request, verify and the StartPay address, against a fake server."""
import json

import httpx
import pytest

from claude_proxy import zarinpal
from claude_proxy.config import Config, ConfigError

MID = "00000000-0000-0000-0000-000000000000"


def fake(monkeypatch, handler):
    """Route the client's calls to `handler(request) -> httpx.Response`; returns the list of (url, json body) seen."""
    seen = []

    def wrapped(req):
        seen.append((str(req.url), json.loads(req.content)))
        return handler(req)
    monkeypatch.setattr(zarinpal, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
    return seen


async def test_request_returns_the_authority(monkeypatch):
    seen = fake(monkeypatch, lambda r: httpx.Response(200, json={"data": {"code": 100, "authority": "A0001", "fee": 0}, "errors": []}))
    a = await zarinpal.request(MID, 1250000, "Lite week", "https://rahkar.pro/pay/callback", "a@b.c", 7, sandbox=False)
    assert a == "A0001"
    url, body = seen[0]
    assert url == "https://payment.zarinpal.com/pg/v4/payment/request.json"
    assert body == {"merchant_id": MID, "amount": 1250000, "currency": "IRT", "description": "Lite week",
                    "callback_url": "https://rahkar.pro/pay/callback", "metadata": {"email": "a@b.c", "order_id": "7"}}


async def test_sandbox_uses_the_sandbox_host(monkeypatch):
    seen = fake(monkeypatch, lambda r: httpx.Response(200, json={"data": {"code": 100, "authority": "S1"}, "errors": []}))
    await zarinpal.request(MID, 1000, "d", "https://x/cb", "a@b.c", 1, sandbox=True)
    assert seen[0][0] == "https://sandbox.zarinpal.com/pg/v4/payment/request.json"
    assert zarinpal.start_url("S1", sandbox=True) == "https://sandbox.zarinpal.com/pg/StartPay/S1"
    assert zarinpal.start_url("A1", sandbox=False) == "https://payment.zarinpal.com/pg/StartPay/A1"


async def test_refusal_carries_code_and_message(monkeypatch):
    fake(monkeypatch, lambda r: httpx.Response(200, json={"data": [], "errors": {"code": -9, "message": "The input params invalid"}}))
    with pytest.raises(zarinpal.ZarinpalRefused) as e:
        await zarinpal.request(MID, 1000, "d", "https://x/cb", "a@b.c", 1, sandbox=False)
    assert (e.value.code, e.value.message) == (-9, "The input params invalid")


@pytest.mark.parametrize("handler", [lambda r: httpx.Response(502, text="bad gateway"),
                                     lambda r: httpx.Response(200, text="<html>"),
                                     lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow"))])
async def test_unreachable_or_garbled_is_unavailable(monkeypatch, handler):
    fake(monkeypatch, handler)
    with pytest.raises(zarinpal.ZarinpalUnavailable):
        await zarinpal.verify(MID, 1000, "A1", sandbox=False)


async def test_verify_100_and_101(monkeypatch):
    for code in (100, 101):
        seen = fake(monkeypatch, lambda r, c=code: httpx.Response(200, json={"data": {"code": c, "ref_id": 201, "card_pan": "502229******5995",
                                                                                       "card_hash": "X", "fee": 0}, "errors": []}))
        v = await zarinpal.verify(MID, 1250000, "A1", sandbox=False)
        assert v == {"code": code, "ref_id": 201, "card_pan": "502229******5995"}
        assert seen[0] == ("https://payment.zarinpal.com/pg/v4/payment/verify.json", {"merchant_id": MID, "amount": 1250000, "authority": "A1"})


def test_config_section_and_secret(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text("[zarinpal]\nsandbox = true\n")
    monkeypatch.setenv("ZARINPAL_MERCHANT_ID", MID)
    cfg = Config.load(str(p))
    assert cfg.zarinpal.sandbox is True and cfg.zarinpal.merchant_id() == MID
    assert Config().zarinpal is None
    p.write_text("[zarinpal]\nmerchant_id = \"x\"\n")
    with pytest.raises(ConfigError, match=r"\[zarinpal\]"):
        Config.load(str(p))
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_zarinpal.py`
Expected: FAIL (`cannot import name 'zarinpal'`).

- [ ] **Step 3: Add the config** — in `config.py`, after `EmailConfig`:

```python
@dataclass
class ZarinpalConfig:
    """[zarinpal] (payments design, section 2). The merchant ID is a secret: ZARINPAL_MERCHANT_ID in the environment."""
    sandbox: bool = False

    def merchant_id(self) -> str | None:
        return os.environ.get("ZARINPAL_MERCHANT_ID") or None
```

Add the field to `Config` after `email`:

```python
    zarinpal: ZarinpalConfig | None = None   # None without a [zarinpal] section: no online payment
```

In `Config.load`, after the `if "email" in data:` block:

```python
        if "zarinpal" in data:
            z = data["zarinpal"]
            if set(z) - {"sandbox"} or not isinstance(z.get("sandbox", False), bool):
                raise ConfigError(f"{toml_path}: [zarinpal] takes only sandbox = true/false; the merchant ID is "
                                  "ZARINPAL_MERCHANT_ID in the environment")
            cfg.zarinpal = ZarinpalConfig(sandbox=z.get("sandbox", False))
```

- [ ] **Step 4: Write the client** — `src/claude_proxy/zarinpal.py`:

```python
"""ZarinPal payment gateway client (payments design, section 4): request a payment, verify it, the StartPay address.
https://www.zarinpal.com/docs/paymentGateway/connectToGateway.html"""
from __future__ import annotations

import httpx

TIMEOUT_S = 10


class ZarinpalUnavailable(Exception):
    """ZarinPal could not be asked (network error, timeout, 5xx or an unreadable answer): try again later."""


class ZarinpalRefused(Exception):
    """ZarinPal answered with an error code (bad merchant, amount, authority, an unpaid payment...)."""

    def __init__(self, code: int, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def _host(sandbox: bool) -> str:
    return "https://sandbox.zarinpal.com" if sandbox else "https://payment.zarinpal.com"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S)


def start_url(authority: str, sandbox: bool) -> str:
    return f"{_host(sandbox)}/pg/StartPay/{authority}"


async def _call(path: str, body: dict, sandbox: bool) -> dict:
    """The `data` object of a successful answer; ZarinpalRefused for an error code, ZarinpalUnavailable otherwise."""
    try:
        async with _client() as c:
            r = await c.post(f"{_host(sandbox)}/pg/v4/payment/{path}", json=body, headers={"Accept": "application/json"})
        if r.status_code >= 500:
            raise ZarinpalUnavailable(f"{path} answered {r.status_code}")
        out = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ZarinpalUnavailable(f"{path}: {e}") from e
    data, errors = (out.get("data"), out.get("errors")) if isinstance(out, dict) else (None, None)
    if isinstance(data, dict) and data.get("code") in (100, 101):
        return data
    err = errors if isinstance(errors, dict) and "code" in errors else data if isinstance(data, dict) else {}
    if "code" not in err:
        raise ZarinpalUnavailable(f"{path}: unreadable answer")
    raise ZarinpalRefused(int(err["code"]), str(err.get("message") or ""))


async def request(merchant_id: str, amount: int, description: str, callback_url: str, email: str, order_id: int,
                  sandbox: bool) -> str:
    """Open a payment of `amount` Toman; returns its authority."""
    data = await _call("request.json", {"merchant_id": merchant_id, "amount": int(amount), "currency": "IRT",
                                        "description": description, "callback_url": callback_url,
                                        "metadata": {"email": email, "order_id": str(order_id)}}, sandbox)
    return str(data["authority"])


async def verify(merchant_id: str, amount: int, authority: str, sandbox: bool) -> dict:
    """Confirm a payment the buyer came back from with Status=OK. Code 100 the first time, 101 after."""
    data = await _call("verify.json", {"merchant_id": merchant_id, "amount": int(amount), "authority": authority}, sandbox)
    return {"code": data["code"], "ref_id": data.get("ref_id"), "card_pan": data.get("card_pan")}
```

- [ ] **Step 5: Document it** — in `config.example.toml`, after the order-request comments, append:

```toml
# Online payment through ZarinPal (docs/superpowers/specs/2026-10-05-zarinpal-payments-design.md): signed-in users pay
# in Toman and get their ticket at once. Needs ZARINPAL_MERCHANT_ID in the environment, [tickets.currencies.IRT]
# (round_to = 1000) with a rate set on the Pricing tab, and [listener] home_url (the callback's domain).
# [zarinpal]
# sandbox = false
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest -q tests/test_zarinpal.py`
Expected: PASS (8 tests).

- [ ] **Step 7: Commit**

```bash
git add src/claude_proxy/config.py src/claude_proxy/zarinpal.py tests/test_zarinpal.py config.example.toml
git commit -m "ZarinPal: [zarinpal] config and the payment client

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Grants at the paid price, orders without mail, the payments table

**Files:**
- Modify: `src/claude_proxy/tickets.py` (`grant`: `paid` and `after` parameters)
- Modify: `src/claude_proxy/orders.py` (`create`: `notify` parameter)
- Modify: `src/claude_proxy/db.py` (`payments` table in `SCHEMA`)
- Test: `tests/test_payments_core.py`

**Interfaces:**
- Produces: `tickets.grant(..., paid: bool = False, after=None)`. With `paid=True` (requires `order_id`): price, rate and amount are the order's `quoted_usd`, `quoted_rate`, `quoted_amount`; no stale-rate check, no quote check. `after(tid)` is called inside the transaction after the order is closed.
- Produces: `orders.create(..., notify: bool = True)`; `notify=False` stores `admin_mail` and `buyer_mail` as `'off'`.
- Produces: table `payments(id, order_id, user_id, created_at, updated_at, amount, authority UNIQUE, status, ref_id, card_pan, error)`.

- [ ] **Step 1: Write the failing tests** — `tests/test_payments_core.py`:

```python
"""Payments design, section 4 (Grant) and section 3 (Data): the paid grant and the payments table."""
import pytest

from claude_proxy import orders, tickets
from claude_proxy.config import Currency
from tests.tickets_helpers import NOW, seeded, user


@pytest.fixture
def env(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.currencies["IRT"] = Currency(round_to=1000)
    tickets.set_rate(conn, cfg, "IRT", 100000, ids["admin"], now=NOW - 3600)
    return conn, cfg, ids


def irt_order(conn, cfg, ids, notify=False):
    return orders.create(conn, cfg, tier="lite", length="week", currency="IRT", name="alice", email="a@example.com",
                         user_id=ids["alice"], notify=notify, now=NOW)


def test_order_without_notify_sends_no_mail(env):
    conn, cfg, ids = env
    from claude_proxy.config import EmailConfig
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    o = irt_order(conn, cfg, ids)
    assert (o["admin_mail"], o["buyer_mail"], o["quoted_amount"]) == ("off", "off", 800000)


def test_paid_grant_uses_the_order_quote(env):
    conn, cfg, ids = env
    o = irt_order(conn, cfg, ids)
    tickets.set_rate(conn, cfg, "IRT", 120000, ids["admin"], now=NOW - 60)          # the rate moved during the payment
    tickets.set_price(conn, cfg, "lite", "week", 9, ids["admin"], now=NOW - 60)      # and the price
    seen = []
    t = tickets.grant(conn, cfg, None, user(conn, ids["alice"]), "lite", "week", "IRT", order_id=o["id"], paid=True,
                      after=seen.append, now=NOW)
    assert (t["usd"], t["rate"], t["amount"]) == (8, 100000, 800000)
    assert seen == [t["id"]]
    assert orders.get(conn, o["id"])["status"] == "done"


def test_paid_grant_ignores_a_stale_rate(env):
    conn, cfg, ids = env
    o = irt_order(conn, cfg, ids)
    t = tickets.grant(conn, cfg, None, user(conn, ids["alice"]), "lite", "week", "IRT", order_id=o["id"], paid=True,
                      now=NOW + 40 * 3600)
    assert t["amount"] == 800000


def test_after_failing_rolls_the_grant_back(env):
    conn, cfg, ids = env
    o = irt_order(conn, cfg, ids)
    def boom(tid):
        raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        tickets.grant(conn, cfg, None, user(conn, ids["alice"]), "lite", "week", "IRT", order_id=o["id"], paid=True, after=boom, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
    assert orders.get(conn, o["id"])["status"] == "new"


def test_paid_needs_an_order(env):
    conn, cfg, ids = env
    with pytest.raises(tickets.TicketError):
        tickets.grant(conn, cfg, None, user(conn, ids["alice"]), "lite", "week", "IRT", paid=True, now=NOW)


def test_payments_table(env):
    conn, cfg, ids = env
    o = irt_order(conn, cfg, ids)
    conn.execute("INSERT INTO payments(order_id, user_id, created_at, updated_at, amount, authority, status) VALUES(?,?,?,?,?,?,'started')",
                 (o["id"], ids["alice"], NOW, NOW, 800000, "A1"))
    with pytest.raises(Exception):   # authority is unique
        conn.execute("INSERT INTO payments(order_id, user_id, created_at, updated_at, amount, authority, status) VALUES(?,?,?,?,?,?,'started')",
                     (o["id"], ids["alice"], NOW, NOW, 800000, "A1"))
    with pytest.raises(Exception):   # unknown status
        conn.execute("INSERT INTO payments(order_id, user_id, created_at, updated_at, amount, authority, status) VALUES(?,?,?,?,?,?,'weird')",
                     (o["id"], ids["alice"], NOW, NOW, 800000, "A2"))
```

Before running, check that `tickets.set_price(conn, cfg, tier, length, usd, actor, now=...)` is the real name and signature (`grep -n "^def set_price" src/claude_proxy/tickets.py`); adjust the call in the test to match it if it differs.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_payments_core.py`
Expected: FAIL (`unexpected keyword argument 'notify'` / `'paid'`, `no such table: payments`).

- [ ] **Step 3: `orders.create` gains `notify`** — change the signature to

```python
def create(conn: sqlite3.Connection, cfg: Config, *, tier: str, length: str, currency: str, name: str, email: str, message: str = "",
           user_id: int | None = None, ip: str | None = None, notify: bool = True, now: float | None = None) -> dict:
```

and the mail decision to

```python
        if cfg.email is None or not notify:   # notify=False: a payment order, mailed when paid instead (payments design, section 5)
            admin_mail = buyer_mail = "off"
```

Add to the docstring: "`notify=False`: no new-order emails (an order paid online)."

- [ ] **Step 4: `tickets.grant` gains `paid` and `after`** — new signature:

```python
def grant(conn: sqlite3.Connection, cfg: Config, actor: int | None, user, tier: str, length: str, currency: str, note: str = "",
          remove_limits=(), confirm_stale_rate: bool = False, expect_usd=None, expect_rate=None, order_id: int | None = None,
          paid: bool = False, after=None, now: float | None = None) -> dict:
```

Docstring addition: "`paid`: the order was paid online (payments design, section 4): it sells at the order's quote, whatever the price and rate are now, with no stale-rate confirmation. `after(ticket_id)`: called inside the transaction, last."

Before `conn.execute("BEGIN IMMEDIATE")` add:

```python
    if paid and order_id is None:
        raise TicketError("A paid grant needs the order it pays for.")
```

Replace the three checks after `p = preview(...)` with:

```python
        p = preview(conn, cfg, user, tier, length, currency, now)
        if paid:
            q = conn.execute("SELECT quoted_usd, quoted_rate, quoted_amount FROM orders WHERE id=?", (order_id,)).fetchone()
            if q is None:
                raise TicketError(f"Order #{order_id} does not exist.")
            p = p | {"usd": q["quoted_usd"], "rate": q["quoted_rate"], "amount": q["quoted_amount"]}
        else:
            if expect_usd is not None and abs(p["usd"] - expect_usd) > 1e-9:
                raise QuoteChanged(f"The price is now ${p['usd']:g}, not ${expect_usd:g} as shown; check the new price and grant again.")
            if expect_rate is not None and abs(p["rate"] - expect_rate) > 1e-12:
                raise QuoteChanged(f"The {currency} rate is now {p['rate']:g}, not {expect_rate:g} as shown; check the new amount and grant again.")
            if p["stale_rate"] and not confirm_stale_rate:
                raise TicketError(f"The {currency} rate is {int((now - p['rate_set_at']) // 3600)} hours old. Confirm to grant at it "
                                  "anyway, or set today's rate first.")
```

After the `if order_id is not None:` block (still inside `try`, before `COMMIT`):

```python
        if after is not None:
            after(tid)
```

- [ ] **Step 5: The table** — in `db.py`, append to `SCHEMA` after the orders indexes:

```python
    # Online payments (payments design, section 3): one row per attempt at paying an order through ZarinPal.
    """CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        amount INTEGER NOT NULL,            -- Toman, the order's quote when the payment started
        authority TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('started','cancelled','failed','expired','paid','paid_unfulfilled')),
        ref_id TEXT,
        card_pan TEXT,                      -- ZarinPal's masked card number; the card hash is never stored
        error TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status, created_at)",
```

- [ ] **Step 6: Run the tests, then the ticket and order suites**

Run: `uv run pytest -q tests/test_payments_core.py tests/test_orders.py tests/test_orders_web.py tests/test_tickets_web.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/claude_proxy/tickets.py src/claude_proxy/orders.py src/claude_proxy/db.py tests/test_payments_core.py
git commit -m "Payments: paid grants at the order's quote, orders without mail, the payments table

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Starting a payment

**Files:**
- Create: `src/claude_proxy/payments.py`
- Test: `tests/test_payments.py`

**Interfaces:**
- Consumes: `zarinpal.request`, `zarinpal.start_url`, `ZarinpalUnavailable`, `ZarinpalRefused` (Task 1); `orders.create(..., notify=False)` (Task 2); `tickets.current_rate`, `tickets.rate_is_stale`.
- Produces: `payments.on(cfg) -> bool`; `payments.PaymentError(status: int, message: str)` (subclass of `orders.OrderError`, so web's existing handler maps it); `async payments.start(conn, cfg, user, tier: str, length: str, email: str, now=None) -> dict` returning `{"payment_id": int, "url": str}`; `payments.get(conn, pid) -> dict`.

- [ ] **Step 1: Write the failing tests** — `tests/test_payments.py`:

```python
"""Payments design, section 4: start, callback, expiry, with ZarinPal faked."""
import time

import pytest

from claude_proxy import orders, payments, tickets, zarinpal
from claude_proxy.config import Currency, ZarinpalConfig
from tests.tickets_helpers import seeded, user

MID = "00000000-0000-0000-0000-000000000000"


class FakeZP:
    """Stands in for zarinpal.request/verify. Set `.request_answer` / `.verify_answer` to a value or an exception."""
    def __init__(self):
        self.requests, self.verifies = [], []
        self.request_answer, self.verify_answer = "A1", {"code": 100, "ref_id": 201, "card_pan": "502229******5995"}
        self.n = 0

    async def request(self, merchant_id, amount, description, callback_url, email, order_id, sandbox):
        self.requests.append({"amount": amount, "callback_url": callback_url, "email": email, "order_id": order_id,
                              "description": description, "merchant_id": merchant_id})
        if isinstance(self.request_answer, Exception):
            raise self.request_answer
        self.n += 1
        return f"{self.request_answer}-{self.n}"

    async def verify(self, merchant_id, amount, authority, sandbox):
        self.verifies.append((amount, authority))
        if isinstance(self.verify_answer, Exception):
            raise self.verify_answer
        return self.verify_answer


@pytest.fixture
def env(db, monkeypatch):
    conn, cfg, ids = seeded(db)
    now = int(time.time())
    conn.execute("UPDATE fx_rates SET set_at=?", (now - 3600,))
    cfg.tickets.currencies["IRT"] = Currency(round_to=1000)
    tickets.set_rate(conn, cfg, "IRT", 156250, ids["admin"], now=now - 3600)   # $8 -> 1,250,000 T
    cfg.zarinpal = ZarinpalConfig()
    cfg.listener.home_url = "https://rahkar.pro"
    cfg.listener.dashboard_url = "https://claude-dash.rahkar.pro"
    monkeypatch.setenv("ZARINPAL_MERCHANT_ID", MID)
    zp = FakeZP()
    monkeypatch.setattr(zarinpal, "request", zp.request)
    monkeypatch.setattr(zarinpal, "verify", zp.verify)
    return conn, cfg, ids, zp


async def start(conn, cfg, ids, **kw):
    return await payments.start(conn, cfg, user(conn, ids["alice"]), kw.get("tier", "lite"), kw.get("length", "week"),
                                kw.get("email", "alice@example.com"))


def test_on_needs_everything(env, monkeypatch):
    conn, cfg, ids, zp = env
    assert payments.on(cfg)
    monkeypatch.delenv("ZARINPAL_MERCHANT_ID")
    assert not payments.on(cfg)
    monkeypatch.setenv("ZARINPAL_MERCHANT_ID", MID)
    cfg.listener.home_url = ""
    assert not payments.on(cfg)
    cfg.listener.home_url = "https://rahkar.pro"
    del cfg.tickets.currencies["IRT"]
    assert not payments.on(cfg)


async def test_start_sends_integer_amount(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    req = zp.requests[0]
    assert req["amount"] == 1250000 and type(req["amount"]) is int
    assert req["callback_url"] == "https://rahkar.pro/pay/callback" and req["merchant_id"] == MID
    assert req["description"] == f"Claude Gateway: Lite, week, order #{req['order_id']}"
    assert r["url"] == "https://payment.zarinpal.com/pg/StartPay/A1-1"
    p = payments.get(conn, r["payment_id"])
    assert (p["status"], p["amount"], p["authority"], p["user_id"]) == ("started", 1250000, "A1-1", ids["alice"])
    o = orders.get(conn, p["order_id"])
    assert (o["status"], o["currency"], o["admin_mail"]) == ("new", "IRT", "off")


async def test_stale_or_missing_rate_starts_nothing(env):
    conn, cfg, ids, zp = env
    conn.execute("UPDATE fx_rates SET set_at=? WHERE currency='IRT'", (int(time.time()) - 40 * 3600,))
    with pytest.raises(payments.PaymentError) as e:
        await start(conn, cfg, ids)
    assert e.value.status == 409 and "paused" in str(e.value)
    conn.execute("DELETE FROM fx_rates WHERE currency='IRT'")
    with pytest.raises(payments.PaymentError):
        await start(conn, cfg, ids)
    assert zp.requests == [] and conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


async def test_payments_off_is_404(env, monkeypatch):
    conn, cfg, ids, zp = env
    monkeypatch.delenv("ZARINPAL_MERCHANT_ID")
    with pytest.raises(payments.PaymentError) as e:
        await start(conn, cfg, ids)
    assert e.value.status == 404


@pytest.mark.parametrize("err", [zarinpal.ZarinpalUnavailable("down"), zarinpal.ZarinpalRefused(-9, "invalid")])
async def test_zarinpal_failure_withdraws_the_order(env, err):
    conn, cfg, ids, zp = env
    zp.request_answer = err
    with pytest.raises(payments.PaymentError) as e:
        await start(conn, cfg, ids)
    assert e.value.status == 502
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "withdrawn"
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0


async def test_order_rules_still_apply(env):
    conn, cfg, ids, zp = env
    await start(conn, cfg, ids)
    with pytest.raises(orders.OrderError) as e:   # one open order
        await start(conn, cfg, ids)
    assert e.value.status == 409
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_payments.py`
Expected: FAIL (`cannot import name 'payments'`).

- [ ] **Step 3: Write `payments.py` (start part)**

```python
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
```

Check `orders.OrderError.__init__(self, status, message, **extra)` (orders.py line ~50) — `PaymentError` inherits it unchanged, so `PaymentError(409, PAUSED)` works and `str(e)` is the message.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q tests/test_payments.py`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/payments.py tests/test_payments.py
git commit -m "Payments: start a ZarinPal payment for a new IRT order

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: The callback, mail and expiry

**Files:**
- Modify: `src/claude_proxy/payments.py`
- Modify: `src/claude_proxy/cli.py` (`_maintenance` calls `payments.expire`)
- Test: `tests/test_payments.py` (append)

**Interfaces:**
- Consumes: Task 2's `tickets.grant(..., paid=True, after=...)`, Task 3's `start`, `get`, `_set`.
- Produces: `async payments.callback(conn, cfg, authority: str, status: str, now=None) -> dict` (the payment row after handling; raises `PaymentError(404)` for an unknown authority); `payments.expire(conn, now=None) -> int`; `payments.mails(cfg, payment: dict, order: dict, ticket: dict | None, reason: str | None) -> list[tuple[str, str, str]]` (to, subject, body); `async payments.send_mails(conn, cfg, pid: int) -> None`.

- [ ] **Step 1: Append the failing tests** to `tests/test_payments.py`:

```python
async def paid_flow(conn, cfg, ids):
    r = await start(conn, cfg, ids)
    p = payments.get(conn, r["payment_id"])
    return p, await payments.callback(conn, cfg, p["authority"], "OK")


async def test_paid_grants_the_ticket(env):
    conn, cfg, ids, zp = env
    p0, p = await paid_flow(conn, cfg, ids)
    assert (p["status"], p["ref_id"], p["card_pan"]) == ("paid", "201", "502229******5995")
    assert zp.verifies == [(1250000, p0["authority"])]
    o = orders.get(conn, p["order_id"])
    t = conn.execute("SELECT * FROM tickets WHERE id=?", (o["ticket_id"],)).fetchone()
    assert o["status"] == "done" and (t["user_id"], t["currency"], t["amount"]) == (ids["alice"], "IRT", 1250000)


async def test_repeated_callback_grants_once(env):
    conn, cfg, ids, zp = env
    p0, p = await paid_flow(conn, cfg, ids)
    again = await payments.callback(conn, cfg, p0["authority"], "OK")
    assert again["status"] == "paid" and len(zp.verifies) == 1
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


async def test_verify_101_counts_as_paid(env):
    conn, cfg, ids, zp = env
    zp.verify_answer = {"code": 101, "ref_id": 201, "card_pan": "x"}
    _, p = await paid_flow(conn, cfg, ids)
    assert p["status"] == "paid"


async def test_cancelled(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p = await payments.callback(conn, cfg, payments.get(conn, r["payment_id"])["authority"], "NOK")
    assert p["status"] == "cancelled" and zp.verifies == []
    assert orders.get(conn, p["order_id"])["status"] == "withdrawn"


async def test_verify_refused_fails_and_withdraws(env):
    conn, cfg, ids, zp = env
    zp.verify_answer = zarinpal.ZarinpalRefused(-51, "Session is not valid, session is not active paid try.")
    _, p = await paid_flow(conn, cfg, ids)
    assert p["status"] == "failed" and "-51" in p["error"]
    assert orders.get(conn, p["order_id"])["status"] == "withdrawn"


async def test_verify_unreachable_stays_started_and_retries(env):
    conn, cfg, ids, zp = env
    zp.verify_answer = zarinpal.ZarinpalUnavailable("down")
    p0, p = await paid_flow(conn, cfg, ids)
    assert p["status"] == "started"
    zp.verify_answer = {"code": 101, "ref_id": 201, "card_pan": "x"}
    assert (await payments.callback(conn, cfg, p0["authority"], "OK"))["status"] == "paid"


async def test_capacity_gone_is_unfulfilled_and_order_stays_open(env, monkeypatch):
    conn, cfg, ids, zp = env
    def full(*a, **k):
        raise tickets.CapacityError("sold out")
    monkeypatch.setattr(tickets, "check_capacity", full)
    _, p = await paid_flow(conn, cfg, ids)
    assert p["status"] == "paid_unfulfilled" and p["ref_id"] == "201" and "sold out" in p["error"]
    assert orders.get(conn, p["order_id"])["status"] == "new"
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


async def test_declined_meanwhile_is_unfulfilled(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p0 = payments.get(conn, r["payment_id"])
    orders.set_status(conn, ids["admin"], p0["order_id"], "declined", note="no")
    p = await payments.callback(conn, cfg, p0["authority"], "OK")
    assert p["status"] == "paid_unfulfilled"


async def test_rate_change_during_payment_keeps_the_paid_amount(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    tickets.set_rate(conn, cfg, "IRT", 200000, ids["admin"])
    p = await payments.callback(conn, cfg, payments.get(conn, r["payment_id"])["authority"], "OK")
    o = orders.get(conn, p["order_id"])
    assert conn.execute("SELECT amount FROM tickets WHERE id=?", (o["ticket_id"],)).fetchone()[0] == 1250000


async def test_unknown_authority(env):
    conn, cfg, ids, zp = env
    with pytest.raises(payments.PaymentError) as e:
        await payments.callback(conn, cfg, "nope", "OK")
    assert e.value.status == 404


async def test_expire(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    assert payments.expire(conn, now=time.time() + 30) == 0
    assert payments.expire(conn, now=time.time() + payments.EXPIRE_S + 1) == 1
    p = payments.get(conn, r["payment_id"])
    assert p["status"] == "expired" and orders.get(conn, p["order_id"])["status"] == "withdrawn"


async def test_mails(env):
    conn, cfg, ids, zp = env
    from claude_proxy.config import EmailConfig
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    _, p = await paid_flow(conn, cfg, ids)
    o = orders.get(conn, p["order_id"])
    t = dict(conn.execute("SELECT * FROM tickets WHERE id=?", (o["ticket_id"],)).fetchone())
    m = payments.mails(cfg, p, o, t, None)
    assert [x[0] for x in m] == ["admin@example.com", "alice@example.com"]
    assert "1,250,000 Toman" in m[0][2] and "201" in m[0][2] and "201" in m[1][2]
    m = payments.mails(cfg, p | {"status": "paid_unfulfilled"}, o, None, "sold out")
    assert [x[0] for x in m] == ["admin@example.com"] and "NOT granted: sold out" in m[0][2]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_payments.py`
Expected: the new tests FAIL (`module 'payments' has no attribute 'callback'`).

- [ ] **Step 3: Implement** — append to `payments.py` (add `import asyncio` and `from . import mail` and `from .config import LENGTHS` at the top):

```python
async def callback(conn: sqlite3.Connection, cfg: Config, authority: str, status: str, now: float | None = None) -> dict:
    """The buyer came back from ZarinPal (section 4, Callback). Idempotent: a payment that is no longer `started` is
    returned as it is, without asking ZarinPal again."""
    row = conn.execute("SELECT * FROM payments WHERE authority=?", (authority,)).fetchone()
    if row is None:
        raise PaymentError(404, "No such payment.")
    p = dict(row)
    if p["status"] != "started" or not on(cfg):
        return p
    if status != "OK":
        _close(conn, p, "cancelled", _now(now))
        return get(conn, p["id"])
    try:
        v = await zarinpal.verify(cfg.zarinpal.merchant_id(), p["amount"], authority, cfg.zarinpal.sandbox)
    except zarinpal.ZarinpalUnavailable as e:
        logger.warning("payment #%d: verify unavailable: %s", p["id"], e)
        return get(conn, p["id"])   # still started: a reload retries
    except zarinpal.ZarinpalRefused as e:
        _close(conn, get(conn, p["id"]), "failed", _now(now), error=f"{e.code}: {e.message}")
        return get(conn, p["id"])
    p = get(conn, p["id"])
    if p["status"] != "started":   # another callback finished while this one awaited ZarinPal
        return p
    return _fulfil(conn, cfg, p, str(v["ref_id"]), v.get("card_pan"), _now(now))


def _close(conn: sqlite3.Connection, p: dict, status: str, now: int, **cols) -> None:
    """A payment that will never be paid: its status, and its order withdrawn if still open."""
    _set(conn, p["id"], status, now, **cols)
    if orders.get(conn, p["order_id"])["status"] in orders.OPEN:
        orders.set_status(conn, p["user_id"], p["order_id"], "withdrawn", now=now)
    db.audit(conn, p["user_id"], f"payment_{status}", f"order #{p['order_id']}", {"payment_id": p["id"], **cols})


def _fulfil(conn: sqlite3.Connection, cfg: Config, p: dict, ref_id: str, card_pan: str | None, now: int) -> dict:
    """Verified: grant at the paid quote, and mark the payment paid inside the grant's transaction. A refused grant
    leaves the order open and the payment `paid_unfulfilled` (section 4, Grant)."""
    o = orders.get(conn, p["order_id"])
    buyer = conn.execute("SELECT * FROM users WHERE id=?", (p["user_id"],)).fetchone()

    def mark(tid):
        _set(conn, p["id"], "paid", now, ref_id=ref_id, card_pan=card_pan)
        db.audit(conn, p["user_id"], "payment_paid", f"order #{p['order_id']}", {"payment_id": p["id"], "ref_id": ref_id, "ticket_id": tid})
    try:
        if buyer is None:
            raise tickets.TicketError("the account no longer exists")
        tickets.grant(conn, cfg, None, buyer, o["tier"], o["length"], CURRENCY, note=f"ZarinPal {ref_id}",
                      order_id=o["id"], paid=True, after=mark, now=now)
    except (tickets.TicketError, orders.OrderError) as e:
        reason = str(e)
        _set(conn, p["id"], "paid_unfulfilled", now, ref_id=ref_id, card_pan=card_pan, error=reason)
        db.audit(conn, p["user_id"], "payment_unfulfilled", f"order #{p['order_id']}", {"payment_id": p["id"], "ref_id": ref_id,
                                                                                         "reason": reason})
        logger.warning("payment #%d (ref %s) verified but not granted: %s", p["id"], ref_id, reason)
    return get(conn, p["id"])


def expire(conn: sqlite3.Connection, now: float | None = None) -> int:
    """Payments still `started` an hour after they began: the buyer never came back (section 4, Expiry)."""
    now = _now(now)
    rows = conn.execute("SELECT * FROM payments WHERE status='started' AND created_at<?", (now - EXPIRE_S,)).fetchall()
    for r in rows:
        _close(conn, dict(r), "expired", now)
    return len(rows)


def _toman(n) -> str:
    return f"{int(round(n)):,} Toman"


def mails(cfg: Config, p: dict, o: dict, ticket: dict | None, reason: str | None) -> list[tuple[str, str, str]]:
    """(to, subject, body) for a payment that just became paid or paid_unfulfilled (section 5)."""
    if cfg.email is None:
        return []
    what = f"{_label(cfg, o['tier'])}, {o['length']} ({LENGTHS.get(o['length'], '?')} days)"
    head = f"Paid: {what}, {_toman(p['amount'])}, ZarinPal reference {p['ref_id']}, by {o['name']} <{o['email']}>."
    link = f"\n\nOrders: {cfg.listener.dashboard_url.rstrip('/')}/dashboard#orders" if cfg.listener.dashboard_url else ""
    if p["status"] == "paid_unfulfilled":
        return [(cfg.email.admin_to, f"Paid, ticket NOT granted: order #{o['id']}",
                 f"{head}\n\nThe ticket was NOT granted: {reason}. Grant it from the order or refund the payment.{link}\n")]
    out = [(cfg.email.admin_to, f"Paid: {_label(cfg, o['tier'])} {o['length']}", f"{head}\nThe ticket is live.{link}\n")]
    if ticket is not None:
        span = f"{tickets._date(ticket['starts_at'])} to {tickets._date(ticket['ends_at'])}"
        out.append((o["email"], f"Your payment: {_label(cfg, o['tier'])} {o['length']}",
                    f"Thank you. Your payment of {_toman(p['amount'])} went through.\n\nTicket: {what}, {span}.\n"
                    f"ZarinPal reference: {p['ref_id']}\n"))
    return out


async def send_mails(conn: sqlite3.Connection, cfg: Config, pid: int) -> None:
    """After the response: send what `mails` lists. A failure is logged and never touches the payment."""
    p = get(conn, pid)
    if p["status"] not in ("paid", "paid_unfulfilled"):
        return
    o = orders.get(conn, p["order_id"])
    t = conn.execute("SELECT * FROM tickets WHERE id=?", (o["ticket_id"],)).fetchone() if o["ticket_id"] else None
    for to, subject, body in mails(cfg, p, o, dict(t) if t else None, p["error"]):
        try:
            await asyncio.to_thread(mail.send, cfg.email, to, subject, body)
        except Exception as e:
            logger.warning("payment #%d: mail to %s failed: %s", pid, to, e)
```

- [ ] **Step 4: Expire from the maintenance loop** — in `cli.py` `_maintenance`, after the `orders.clear_old_ips` block, add (and `from . import payments` beside the existing `orders` import):

```python
        try:
            expired = payments.expire(conn)
            if expired:
                logger.info("expired %d unfinished payments", expired)
        except Exception:
            logger.exception("expiring payments failed")
```

The loop runs every 6 hours; that is fine: expiry only tidies, a late callback on an expired payment is answered with its status.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -q tests/test_payments.py tests/test_payments_core.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/claude_proxy/payments.py src/claude_proxy/cli.py tests/test_payments.py
git commit -m "Payments: callback verifies and grants, mail, expiry

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Web endpoints

**Files:**
- Modify: `src/claude_proxy/web.py`
- Test: `tests/test_payments_web.py`

**Interfaces:**
- Consumes: `payments.on`, `rate_ok`, `start`, `callback`, `get`, `send_mails`, `PaymentError` (Tasks 3–4).
- Produces (HTTP):
  - `POST /api/orders/pay {tier, length, email?}` (signed in, CSRF as other `write=True` calls) → `{"payment_id", "url"}`.
  - `GET /pay/callback?Authority=&Status=` → 302 to `<dashboard_url>/dashboard#payment/<id>`; unknown authority → 404 HTML page.
  - `GET /api/me/payments/{pid}` (own payments only) → `{"id","status","amount","ref_id","tier","label","length","error_shown"}`.
  - `/api/me/tickets` gains `"pay": {"currency": "IRT", "prices": <price_table in IRT>} | null`.
  - `/api/admin/orders` rows gain `"payment": {"status","amount","ref_id","card_pan"} | null` (the order's newest payment).
  - `HomeHost.PUBLIC` gains `"/pay/callback"`.

- [ ] **Step 1: Write the failing tests** — `tests/test_payments_web.py`:

```python
"""Payments design, sections 4 and 6: the endpoints, with ZarinPal faked."""
import time

import pytest

from claude_proxy import payments, tickets, zarinpal
from claude_proxy.config import Currency, ZarinpalConfig
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_payments import MID, FakeZP
from tests.test_tickets_web import env as tickets_env  # noqa: F401  (the fixture)
from tests.test_web import admin_client, bearer


@pytest.fixture
def env(tickets_env, monkeypatch):
    gw, conn, cfg, ids, keys = tickets_env
    cfg.tickets.currencies["IRT"] = Currency(round_to=1000)
    tickets.set_rate(conn, cfg, "IRT", 156250, ids["admin"], now=int(time.time()) - 3600)
    cfg.zarinpal = ZarinpalConfig()
    cfg.listener.home_url = "https://rahkar.pro"
    cfg.listener.dashboard_url = "https://claude-dash.rahkar.pro"
    monkeypatch.setenv("ZARINPAL_MERCHANT_ID", MID)
    zp = FakeZP()
    monkeypatch.setattr(zarinpal, "request", zp.request)
    monkeypatch.setattr(zarinpal, "verify", zp.verify)
    conn.execute("UPDATE users SET email='alice@example.com' WHERE id=?", (ids["alice"],))
    return gw, conn, cfg, ids, keys, zp


def client(gw):
    return asgi_client(create_dashboard_app(gw))


async def test_pay_redirect_callback_and_result(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week"})
        assert r.status_code == 200, r.text
        pid, url = r.json()["payment_id"], r.json()["url"]
        assert url.startswith("https://payment.zarinpal.com/pg/StartPay/")
        auth = url.rsplit("/", 1)[1]
        r = await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == f"https://claude-dash.rahkar.pro/dashboard#payment/{pid}"
        me = (await c.get(f"/api/me/payments/{pid}", headers=bearer(keys["alice"]))).json()
        assert (me["status"], me["amount"], me["ref_id"], me["label"]) == ("paid", 1250000, "201", "Lite")
        assert "card_pan" not in me and "authority" not in me
        assert (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["current"]["tier"] == "lite"
    async with admin_client(gw) as c:
        o = (await c.get("/api/admin/orders?status=all")).json()["orders"][0]
        assert o["payment"] == {"status": "paid", "amount": 1250000, "ref_id": "201", "card_pan": "502229******5995"}


async def test_pay_needs_an_email(env):
    gw, conn, cfg, ids, keys, zp = env
    conn.execute("UPDATE users SET email=NULL WHERE id=?", (ids["alice"],))
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week"})
        assert r.status_code == 400
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "email": "a@b.co"})
        assert r.status_code == 200
    assert len(zp.requests) == 1 and zp.requests[0]["email"] == "a@b.co"


async def test_pay_needs_sign_in_and_payments_on(env, monkeypatch):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        assert (await c.post("/api/orders/pay", json={"tier": "lite", "length": "week"})).status_code == 401
        monkeypatch.delenv("ZARINPAL_MERCHANT_ID")
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week"})
        assert r.status_code == 404
        assert (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["pay"] is None


async def test_pay_prices_offered_only_with_a_fresh_rate(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        pay = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["pay"]
        assert pay["currency"] == "IRT"
        lite = next(t for t in pay["prices"]["tiers"] if t["tier"] == "lite")
        assert lite["lengths"]["week"]["amount"] == 1250000
        conn.execute("UPDATE fx_rates SET set_at=? WHERE currency='IRT'", (int(time.time()) - 40 * 3600,))
        assert (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["pay"] is None


async def test_callback_unknown_and_cancel(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        assert (await c.get("/pay/callback?Authority=nope&Status=OK")).status_code == 404
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week"})
        auth = r.json()["url"].rsplit("/", 1)[1]
        assert (await c.get(f"/pay/callback?Authority={auth}&Status=NOK", follow_redirects=False)).status_code == 302
        me = (await c.get(f"/api/me/payments/{r.json()['payment_id']}", headers=bearer(keys["alice"]))).json()
        assert me["status"] == "cancelled"


async def test_someone_elses_payment_is_404(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week"})
    async with admin_client(gw) as c:
        assert (await c.get(f"/api/me/payments/{r.json()['payment_id']}")).status_code == 404


async def test_callback_is_served_on_the_home_host(env):
    gw, conn, cfg, ids, keys, zp = env
    import httpx
    app = create_dashboard_app(gw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://rahkar.pro") as c:
        assert (await c.get("/pay/callback?Authority=nope&Status=OK")).status_code == 404   # served, not redirected
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_payments_web.py`
Expected: FAIL (404s / `KeyError: 'pay'`).

- [ ] **Step 3: Implement in `web.py`**

1. Import: `from . import clerk, db, limits, orders, payments, quota, tickets, turnstile, usage`.
2. `HomeHost.PUBLIC = ("/", "/privacy", "/api/pricing", "/api/orders", "/pay/callback")`.
3. In `me_tickets`, before `return out`:

```python
        # Online payment (payments design, section 4): the Toman prices, only while the IRT rate is fresh.
        out["pay"] = ({"currency": payments.CURRENCY, "prices": tickets.price_table(conn, cfg, now, payments.CURRENCY)}
                      if payments.on(cfg) and payments.rate_ok(conn, now) else None)
```

4. In `orders_list`, inside the `for o in rows:` loop:

```python
            pay = conn.execute("SELECT status, amount, ref_id, card_pan FROM payments WHERE order_id=? ORDER BY id DESC LIMIT 1",
                               (o["id"],)).fetchone()
            o["payment"] = dict(pay) if pay else None
```

5. After `orders_mine`, add the endpoints:

```python
    # ---------- online payment (payments design, 2026-10-05) ----------

    @app.post("/api/orders/pay")
    async def order_pay(request: Request):
        user = principal(request, write=True)
        need_tickets()
        body = await _json(request)
        # As for an order: the account email, else one typed in the dialog, stored on the order only.
        email = user["email"] or str(body.get("email") or "")
        return await payments.start(conn, cfg, user, str(body.get("tier") or ""), str(body.get("length") or ""), email)

    @app.get("/pay/callback")
    async def pay_callback(Authority: str = "", Status: str = ""):
        # No session: the authority names the payment, and its ticket only ever goes to the payment's own user.
        try:
            p = await payments.callback(conn, cfg, Authority, Status)
        except payments.PaymentError:
            return HTMLResponse("<!doctype html><title>Payment not found</title><p>This payment link is not known.</p>",
                                status_code=404, headers=PAGE_HEADERS)
        if p["status"] in ("paid", "paid_unfulfilled") and cfg.email is not None:
            task = asyncio.create_task(payments.send_mails(conn, cfg, p["id"]))
            _mail_tasks.add(task)
            task.add_done_callback(_mail_done)
        return RedirectResponse(f"{cfg.listener.dashboard_url.rstrip('/')}/dashboard#payment/{p['id']}", status_code=302)

    @app.get("/api/me/payments/{pid}")
    async def my_payment(request: Request, pid: int):
        user = principal(request)
        row = conn.execute("SELECT p.*, o.tier, o.length FROM payments p JOIN orders o ON o.id=p.order_id WHERE p.id=? AND p.user_id=?",
                           (pid, user["id"])).fetchone()
        if row is None:
            fail(404, "No such payment.")
        # The buyer sees ZarinPal's message for a failed payment; never the card, the authority or an internal reason.
        return {"id": row["id"], "status": row["status"], "amount": row["amount"], "ref_id": row["ref_id"], "tier": row["tier"],
                "label": tier_label(row["tier"]), "length": row["length"],
                "error_shown": row["error"] if row["status"] == "failed" else None}
```

Note: `send_mails` runs only after a callback that changed the status — guard against re-sending on a repeated callback by checking that the payment was `started` before the call. Change the call site to:

```python
        before = conn.execute("SELECT status FROM payments WHERE authority=?", (Authority,)).fetchone()
        try:
            p = await payments.callback(conn, cfg, Authority, Status)
        ...
        if before and before["status"] == "started" and p["status"] in ("paid", "paid_unfulfilled") and cfg.email is not None:
```

Add a test for it in this file:

```python
async def test_mail_once_per_payment(env, monkeypatch):
    gw, conn, cfg, ids, keys, zp = env
    from claude_proxy import mail, web
    from claude_proxy.config import EmailConfig
    import asyncio
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    sent = []
    monkeypatch.setattr(mail, "send", lambda email, to, subject, body: sent.append(to))
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week"})
        auth = r.json()["url"].rsplit("/", 1)[1]
        for _ in range(2):
            await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        await asyncio.gather(*list(web._mail_tasks))
    assert sorted(sent) == ["admin@example.com", "alice@example.com"]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q tests/test_payments_web.py tests/test_home_host.py tests/test_orders_web.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/web.py tests/test_payments_web.py
git commit -m "Payments: pay, callback and result endpoints; payment on admin order rows

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Dashboard UI

**Files:**
- Modify: `src/claude_proxy/static/app.js` (order dialog, result page `#payment/<id>`, Orders tab payment cell)
- Modify: `src/claude_proxy/static/i18n.js` (keys in `en` and `fa`)

**Interfaces:**
- Consumes: Task 5's `/api/me/tickets` `pay`, `POST /api/orders/pay`, `GET /api/me/payments/{id}`, admin rows' `payment`.

Before editing: `git status --short src/claude_proxy/static/`. If `app.js` or `i18n.js` show changes you did not make, another session is editing them: wait until they are committed (`git log -1 -- src/claude_proxy/static/app.js`), then `git pull --rebase` if needed. Never commit their hunks.

- [ ] **Step 1: i18n keys** — add to `I18N.en` (near the other `ord.*` keys):

```js
    "pay.button": "Pay {amount} with ZarinPal",
    "pay.or": "or",
    "pay.going": "Going to ZarinPal…",
    "pay.paid": "Paid. ZarinPal reference {ref}. Your {what} ticket is live.",
    "pay.unfulfilled": "Your payment went through (reference {ref}), but the ticket could not be issued automatically. The admin has been told and will sort it out.",
    "pay.cancelled": "Payment cancelled; nothing was charged.",
    "pay.failed": "The payment did not go through: {msg}. If money left your account, ZarinPal returns it within 72 hours.",
    "pay.expired": "This payment was not finished.",
    "pay.pending": "We could not confirm your payment yet; reload this page in a minute.",
    "pay.col": "Payment",
    "pay.st_paid": "Paid",
    "pay.st_paid_unfulfilled": "Paid, ticket not granted",
    "pay.st_started": "Paying",
    "pay.st_cancelled": "Cancelled",
    "pay.st_failed": "Failed",
    "pay.st_expired": "Expired",
```

and to `I18N.fa`:

```js
    "pay.button": "پرداخت {amount} با زرین‌پال",
    "pay.or": "یا",
    "pay.going": "در حال رفتن به زرین‌پال…",
    "pay.paid": "پرداخت شد. شماره پیگیری زرین‌پال {ref}. بلیت {what} شما فعال است.",
    "pay.unfulfilled": "پرداخت شما انجام شد (شماره پیگیری {ref})، اما بلیت به‌طور خودکار صادر نشد. به مدیر خبر داده شد و رسیدگی می‌کند.",
    "pay.cancelled": "پرداخت لغو شد؛ مبلغی کسر نشد.",
    "pay.failed": "پرداخت انجام نشد: {msg}. اگر مبلغی از حساب شما کسر شده، زرین‌پال ظرف ۷۲ ساعت آن را برمی‌گرداند.",
    "pay.expired": "این پرداخت تمام نشد.",
    "pay.pending": "هنوز نتوانستیم پرداخت شما را تأیید کنیم؛ یک دقیقه دیگر این صفحه را دوباره باز کنید.",
    "pay.col": "پرداخت",
    "pay.st_paid": "پرداخت‌شده",
    "pay.st_paid_unfulfilled": "پرداخت‌شده، بلیت صادر نشده",
    "pay.st_started": "در حال پرداخت",
    "pay.st_cancelled": "لغوشده",
    "pay.st_failed": "ناموفق",
    "pay.st_expired": "منقضی",
```

Check how `I18N.fa` keys are laid out (`grep -n '"ord.send"' src/claude_proxy/static/i18n.js`) and put these beside the `ord.*` block in each table.

- [ ] **Step 2: Pay button in the order dialog** — `orderFormHtml(p, tier, len, email)` gains a fifth parameter `pay` (the `tk.pay` object or null). After the submit button add, when `pay` has a price for this tier and length that is not sold out:

```js
  const payL = pay && pay.prices.tiers.find((x) => x.tier === tier.tier)?.lengths[len];
  const payBtn = payL && !payL.sold_out
    ? `<p class="pay-or muted">${t("pay.or")}</p><button class="btn primary" type="button" id="order-pay">${t("pay.button", { amount: esc(money(payL.amount, pay.currency)) })}</button>` : "";
```

and render `${payBtn}` right after `<button class="btn primary" type="submit">${t("ord.send")}</button>`. In `orderDialog(p, tier, len, pay)` wire it:

```js
  const pb = $("#order-pay", d);
  if (pb) pb.onclick = async () => {
    if (f.email && !f.email.reportValidity()) return;
    pb.disabled = true; pb.textContent = t("pay.going");
    try {
      const r = await api("/api/orders/pay", { method: "POST", body: { tier: tier.tier, length: len, ...(f.email ? { email: f.email.value } : {}) } });
      location.href = r.url;
    } catch (err) { pb.disabled = false; pb.textContent = t("pay.button", { amount: esc(money(payL.amount, pay.currency)) }); $("#order-err", d).textContent = err.message; }
  };
```

(`payL` must be in scope in `orderDialog`: compute it there the same way and pass it to `orderFormHtml`, or have `orderFormHtml` return it — keep one computation.) In `wireOrdering`, call `orderDialog(tk.prices, tier, len, tk.pay)`.

- [ ] **Step 3: Result page** — in `route()` add before the `VIEWS[h]` line:

```js
  const pm = /^payment\/(\d+)$/.exec(h);
  if (pm) { S.tab = "overview"; S.paymentId = +pm[1]; history.replaceState(null, "", "#overview"); return true; }
```

and in the overview render (where the buyer's `my-order` card is built, `renderOverview`), when `S.paymentId` is set, fetch and show it once:

```js
  if (S.paymentId) {
    const id = S.paymentId; S.paymentId = null;
    api(`/api/me/payments/${id}`).then((p) => {
      const what = t("ord.what", { label: esc(p.label), len: lengthName(p.length) });
      const msg = { paid: t("pay.paid", { ref: esc(p.ref_id), what }), paid_unfulfilled: t("pay.unfulfilled", { ref: esc(p.ref_id) }),
        cancelled: t("pay.cancelled"), failed: t("pay.failed", { msg: esc(p.error_shown || "") }), expired: t("pay.expired"),
        started: t("pay.pending") }[p.status];
      if (msg) alertInline(msg);
    }).catch(() => {});
  }
```

Use the dashboard's existing notice helper (`alertInline`, as `wireOrdering` does); if the overview has a better place for a persistent notice (e.g. the `my-order` card slot), put a `<div class="card order-note">` there instead.

- [ ] **Step 4: Orders tab** — in `renderOrders` add a `<th>${t("pay.col")}</th>` after the status column header and bump the empty-row `colspan` from 9 to 10; in `orderRow` add after the status cell:

```js
    <td class="nowrap">${o.payment ? `${stateBadge2(o.payment.status)}${o.payment.ref_id ? `<div class="muted" dir="ltr">${esc(o.payment.ref_id)} · ${esc(o.payment.card_pan || "")}</div>` : ""}` : `<span class="muted">—</span>`}</td>
```

with a helper next to `mailState`:

```js
function stateBadge2(s) {
  const cls = { paid: " state-active", paid_unfulfilled: " state-cancelled", started: " state-queued" };
  return `<span class="badge${cls[s] || ""}">${t(`pay.st_${s}`)}</span>`;
}
```

- [ ] **Step 5: Check it in the browser** — use the `run` skill to start the app locally with a config that has `[tickets]`, `[tickets.currencies.IRT] round_to = 1000`, `[zarinpal] sandbox = true`, `[listener] home_url = "http://127.0.0.1:<port>"` and `ZARINPAL_MERCHANT_ID` set to any 36-character UUID; set an IRT rate on the Pricing tab; sign in as a user; open the order dialog: the Pay button shows the Toman amount; click it and confirm the browser goes to `sandbox.zarinpal.com/pg/StartPay/...` (the sandbox answers even for an unknown merchant? If it refuses, the dialog must show the "ZarinPal is not available" error — both are acceptable here). Then open `#payment/<id>` for a cancelled payment and check the notice, in English and Persian.

- [ ] **Step 6: Commit (only your hunks)**

```bash
git add -p src/claude_proxy/static/app.js src/claude_proxy/static/i18n.js
git commit -m "Payments: Pay with ZarinPal in the order dialog, the result notice, payments on the Orders tab

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Deploy config, docs, full suite

**Files:**
- Modify: `deploy/lightsail/config.toml`, `deploy/lightsail/README.md`, `README.md`

- [ ] **Step 1: Production config** — in `deploy/lightsail/config.toml` add after `[tickets.currencies.EUR]`:

```toml
[tickets.currencies.IRT]   # Toman, for ZarinPal; set the rate on the Pricing tab
round_to = 1000
```

and at the end:

```toml
# ZarinPal (docs/superpowers/specs/2026-10-05-zarinpal-payments-design.md). Terminal registered for rahkar.pro and
# 3.139.146.5. Off until ZARINPAL_MERCHANT_ID is in gateway.env.
[zarinpal]
sandbox = false
```

- [ ] **Step 2: Docs** — in `deploy/lightsail/README.md`, add a short "Online payment (ZarinPal)" section: put `ZARINPAL_MERCHANT_ID=<36-char id>` in `gateway.env`, restart (`docker compose up -d`), set the IRT rate on the Pricing tab; the callback is `https://rahkar.pro/pay/callback`; the server's outgoing IP must be the one registered with ZarinPal (`3.139.146.5`). In `README.md`, next to the `[tickets]` documentation, one paragraph pointing to the spec and listing the four requirements from Global Constraints.

- [ ] **Step 3: Full suite**

Run: `uv run pytest -q`
Expected: all pass (812 + the new ones), 48 skipped.

- [ ] **Step 4: Commit and push**

```bash
git add deploy/lightsail/config.toml deploy/lightsail/README.md README.md
git commit -m "Payments: production config (IRT, [zarinpal]) and docs

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git push origin master
```

Payments stay off in production until `ZARINPAL_MERCHANT_ID` is added to `gateway.env` and an IRT rate is set. After that: one sandbox payment (`sandbox = true`), then one small real payment.
