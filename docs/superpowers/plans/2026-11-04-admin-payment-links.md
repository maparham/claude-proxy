# Admin Payment Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The admin issues a payment link for a pass to an email, with a price and a deadline; the recipient signs in
with that email and pays it through ZarinPal from the dashboard, and the pass is granted at once.

**Architecture:** A link is an order with two new columns (`pay_code`, `pay_by`), created unclaimed (`user_id` NULL)
and claimed by the signed-in user whose email matches when they pay. Paying reuses the existing `payments._open` /
callback / `_fulfil` path unchanged. `reconcile` declines link orders an hour past their deadline. The dashboard opens
`#paylink/<code>` (kept through sign-in like `#authorize/…`) as a dialog over the overview and pays with `goPay`.

**Tech Stack:** Python 3.12, FastAPI, SQLite (raw SQL), pytest + pytest-asyncio, vanilla JS (`static/app.js`),
`static/i18n.js` (English and Persian).

**Spec:** `docs/superpowers/specs/2026-11-04-admin-payment-links-design.md`

## Global Constraints

- Work directly on `master`; commit only the files your task names (other sessions share this checkout).
- Run tests with `.venv/bin/python -m pytest -q <paths>`; the whole suite with `.venv/bin/python -m pytest -q`.
- Currency of every link: `IRT` (`payments.CURRENCY`). Amounts are whole Toman.
- `pay_code`: `secrets.token_urlsafe(16)` (22 characters). `pay_by`: Unix seconds, `now < pay_by <= now + 30 days`.
- An edited amount: whole Toman, `1000 <= amount <= 10 × list price`; `quoted_usd = amount / (rate × (1 + vat/100))`.
- A link's `admin_mail` and `buyer_mail` are `off`; `lang` is `fa`; `start_now` is 0; `terms_version` the current one.
- Expired link orders are declined with the note `Payment link expired`, `pay_by` more than 3600 s past, no payment
  `started`, `paid` or `paid_unfulfilled` on them.
- Every user-visible string goes in `i18n.js` under both `en` and `fa` (a test checks the key sets match). Persian
  wording: loanwords like لینک, never "team"; the product is کلاد.
- Every value put into HTML goes through `esc()` (or `tierName`/`lengthName`, which escape).
- ZarinPal StartPay is reached only through `goPay(url)` (it sends `Referrer-Policy: origin`).

## Review Focus

1. A signed-in user whose account email is NULL (key-only users) opening a link: must get `other_email`, not a crash.
2. Two tabs pressing Pay on one link: both payments open, the first to verify grants and the second never grants twice
   (existing `pay_order` rule), and the claim is not redone.
3. The buyer cancelling (withdrawing) a claimed link order from the dashboard card: the link then answers `invalid`.
4. A link whose `pay_by` passed while the dialog was open: Pay is refused with the `invalid` message, no ZarinPal call.
5. Upper/lower case differences between the link's email and the account's email must still match.

Each is pinned by a test in the task that owns the code (Tasks 2 and 4).

---

### Task 1: Data and creating a link (orders + payments)

**Files:**
- Modify: `src/claude_proxy/db.py` (migrations block, after the `upgrade_of` migration near line 437)
- Modify: `src/claude_proxy/orders.py` (constants near line 35; `VISITOR`; new `mask_email`, `link_price`, `create_link` after `create`)
- Modify: `src/claude_proxy/payments.py` (new `create_link` after `pay_order`)
- Test: `tests/test_paylinks.py` (new)

**Interfaces:**
- Produces:
  - `orders.LINK_INVALID: str = "This payment link is no longer valid."`
  - `orders.PAYLINK_MAX_S: int = 30 * DAY`
  - `orders.mask_email(email: str) -> str`
  - `orders.link_price(conn, cfg, tier: str, length: str, currency: str, now: float) -> tuple[float, int, dict]` → `(list_usd_charged, amount_toman, rate_row)`
  - `orders.create_link(conn, cfg, actor: int | None, *, email, tier, length, pay_by, amount=None, note="", currency="IRT", now=None) -> dict` (the order row)
  - `payments.create_link(conn, cfg, actor: int | None, **kw) -> dict` (checks payments are on, not manual, rate fresh; then `orders.create_link`)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_paylinks.py`:

```python
"""Admin payment links (spec 2026-11-04): creating, viewing, paying and expiring a link."""
import time

import pytest

from claude_proxy import db as dbm, orders, payments, tickets
from tests.test_payments import env, later  # noqa: F401  (the fixture and helper)
from tests.tickets_helpers import user


def make(conn, cfg, ids, **kw):
    args = {"email": "Bob@Example.com", "tier": "lite", "length": "week", "pay_by": int(time.time()) + 3 * 86400} | kw
    return payments.create_link(conn, cfg, ids["admin"], **args)


def test_create_link_stores_an_unclaimed_order(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids, note="for Bob")
    assert o["user_id"] is None and o["name"] == o["email"] == "Bob@Example.com"
    assert (o["currency"], o["quoted_amount"], o["status"], o["lang"], o["start_now"]) == ("IRT", 1250000, "new", "fa", 0)
    assert (o["admin_mail"], o["buyer_mail"], o["admin_note"], o["ip"]) == ("off", "off", "for Bob", None)
    assert len(o["pay_code"]) == 22 and o["pay_by"] > time.time()
    row = conn.execute("SELECT action, actor_user_id, detail_json FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["action"], row["actor_user_id"]) == ("order_paylink", ids["admin"]) and '"edited": false' in row["detail_json"]


def test_codes_are_unique_and_links_are_not_visitor_orders(env):
    conn, cfg, ids, zp = env
    codes = {make(conn, cfg, ids)["pay_code"] for _ in range(3)}
    assert len(codes) == 3
    assert orders._count(conn, f"{orders.VISITOR}", ()) == 0


def test_edited_amount_records_usd_without_vat(env):
    conn, cfg, ids, zp = env
    cfg.zarinpal.vat_percent = 10
    o = make(conn, cfg, ids, amount=1100000)
    assert o["quoted_amount"] == 1100000
    assert o["quoted_usd"] == pytest.approx(1100000 / (156250 * 1.1))


@pytest.mark.parametrize("kw, msg", [
    ({"email": "not-an-email"}, "email"),
    ({"tier": "gold"}, "Unknown tier"),
    ({"length": "year"}, "Unknown length"),
    ({"pay_by": 0}, "next 30 days"),
    ({"pay_by": int(time.time()) + 31 * 86400}, "next 30 days"),
    ({"pay_by": "soon"}, "date and time"),
    ({"amount": 999}, "between"),
    ({"amount": 12500001}, "between"),
    ({"amount": "12.5"}, "whole number"),
])
def test_create_link_refuses_bad_input(env, kw, msg):
    conn, cfg, ids, zp = env
    with pytest.raises(orders.OrderError) as e:
        make(conn, cfg, ids, **kw)
    assert e.value.status == 400 and msg in str(e.value)


def test_create_link_needs_payments_on_not_manual_and_a_fresh_rate(env, monkeypatch):
    conn, cfg, ids, zp = env
    payments.set_pay_mode(conn, ids["admin"], "manual")
    with pytest.raises(payments.PaymentError) as e:
        make(conn, cfg, ids)
    assert e.value.status == 409
    payments.set_pay_mode(conn, ids["admin"], "zarinpal")
    conn.execute("UPDATE fx_rates SET set_at=? WHERE currency='IRT'", (int(time.time()) - 40 * 3600,))
    with pytest.raises(payments.PaymentError) as e:
        make(conn, cfg, ids)
    assert e.value.status == 409 and str(e.value) == payments.PAUSED
    monkeypatch.delenv("ZARINPAL_MERCHANT_ID")
    with pytest.raises(payments.PaymentError) as e:
        make(conn, cfg, ids)
    assert e.value.status == 404


def test_create_link_refuses_a_sold_out_pass(env, monkeypatch):
    conn, cfg, ids, zp = env
    monkeypatch.setattr(tickets, "sold_out", lambda *a, **k: True)
    with pytest.raises(orders.OrderError) as e:
        make(conn, cfg, ids)
    assert e.value.status == 409 and "sold out" in str(e.value)


@pytest.mark.parametrize("email, masked", [("bob@example.com", "bo…@example.com"), ("ab@x.io", "a…@x.io"),
                                           ("a@x.io", "a…@x.io")])
def test_mask_email(email, masked):
    assert orders.mask_email(email) == masked
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_paylinks.py`
Expected: FAIL (`AttributeError: module 'claude_proxy.payments' has no attribute 'create_link'`).

- [ ] **Step 3: Add the columns**

In `src/claude_proxy/db.py`, after the `upgrade_of` migration (`if "upgrade_of" not in _columns(conn, "orders"): ...`):

```python
    # Admin payment links (spec 2026-11-04): NULL on every other order. SQLite cannot add a UNIQUE column.
    if "pay_code" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN pay_code TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_pay_code ON orders(pay_code)")
    if "pay_by" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN pay_by INTEGER")
```

If `start_now` is migrated after `upgrade_of`, put this block after the last `orders` migration instead.

- [ ] **Step 4: orders: constants, VISITOR, mask_email, link_price, create_link**

In `src/claude_proxy/orders.py`, add `import secrets` with the other imports. Beside the other constants:

```python
PAYLINK_MAX_S = 30 * DAY
LINK_INVALID = "This payment link is no longer valid."
```

Replace the `VISITOR` line (and keep its comment, adding one sentence):

```python
# A visitor order: placed without an account (user_id NULL until linked) or still carrying its IP (cleared on close).
# One the admin has both linked and closed drops out of the visitor limits, which only loosens them for a handled buyer.
# A payment link is the admin's, never a visitor's, though it has no account until claimed.
VISITOR = "(pay_code IS NULL AND (user_id IS NULL OR ip IS NOT NULL))"
```

After `create(...)`:

```python
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
    until `pay_by`. No mail. The caller checks that online payment can take it (payments.create_link)."""
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
```

- [ ] **Step 5: payments.create_link**

In `src/claude_proxy/payments.py`, after `pay_order`:

```python
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
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_paylinks.py tests/test_orders.py tests/test_payments.py`
Expected: all PASS. (If `test_create_link_needs_payments_on_not_manual_and_a_fresh_rate` fails on the stale-rate step,
check `tickets.rate_is_stale`'s window and set `set_at` further back.)

- [ ] **Step 7: Commit**

```bash
git add src/claude_proxy/db.py src/claude_proxy/orders.py src/claude_proxy/payments.py tests/test_paylinks.py
git commit -m "Payment links: an admin creates an unclaimed order for an email with pay_code and pay_by, at the list price or an edited one whose USD excludes VAT"
```

---

### Task 2: Viewing and paying a link; no order paid twice

**Files:**
- Modify: `src/claude_proxy/payments.py` (constants; new `_has_paid`, `link_state`, `pay_link`; `pay_order`)
- Test: `tests/test_paylinks.py` (append), `tests/test_payments.py` (append)

**Interfaces:**
- Consumes: `orders.LINK_INVALID`, `orders.mask_email`, `orders.OPEN`, `payments._open`, `payments.on`, `payments.pay_mode`
- Produces:
  - `payments.ALREADY_PAID: str = "This order is already paid."`
  - `payments.LINK_OTHER_ORDER: str = "You have another open order; cancel it first."`
  - `payments.link_state(conn, user, code: str, now=None) -> dict`: always returns a dict with `state` in
    `invalid | other_email | paid | other_order | open`, plus `message` (str, for every state but `open`), `email`
    (masked, for `other_email`), `unfulfilled` (bool, for `paid`), `order` (the row, for `paid` and `open`).
  - `payments.pay_link(conn, cfg, user, code: str, terms_version, now=None) -> dict` → `{"payment_id", "url"}`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_paylinks.py`:

```python
def bob(conn, email="bob@example.com"):
    uid, _ = dbm.create_user(conn, "bob")
    conn.execute("UPDATE users SET email=? WHERE id=?", (email, uid))
    return user(conn, uid)


def test_link_state_for_each_case(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    b = bob(conn)
    s = payments.link_state(conn, b, o["pay_code"])
    assert s["state"] == "open" and s["order"]["id"] == o["id"]           # case differs: Bob@ vs bob@
    assert payments.link_state(conn, b, "nope")["state"] == "invalid"
    other = user(conn, ids["alice"])                                       # alice has no email at all
    s = payments.link_state(conn, other, o["pay_code"])
    assert (s["state"], s["email"]) == ("other_email", "Bo…@Example.com") and "Bo…@Example.com" in s["message"]
    assert "order" not in s
    conn.execute("UPDATE orders SET pay_by=? WHERE id=?", (int(time.time()) - 1, o["id"]))
    assert payments.link_state(conn, b, o["pay_code"])["state"] == "invalid"


def test_link_state_other_order_and_withdrawn(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    b = bob(conn)
    own = orders.create(conn, cfg, tier="lite", length="week", currency="IRT", name="bob", email="bob@example.com",
                        user_id=b["id"], notify=False, terms_version="v1")
    assert payments.link_state(conn, b, o["pay_code"])["state"] == "other_order"
    orders.withdraw(conn, b["id"], own["id"])
    assert payments.link_state(conn, b, o["pay_code"])["state"] == "open"
    orders.set_status(conn, ids["admin"], o["id"], "declined")
    assert payments.link_state(conn, b, o["pay_code"])["state"] == "invalid"


async def test_pay_link_claims_then_pays_and_grants_at_the_link_price(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids, amount=1000000)
    b = bob(conn)
    r = await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    claimed = orders.get(conn, o["id"])
    assert (claimed["user_id"], claimed["name"]) == (b["id"], "bob")
    assert zp.requests[0]["amount"] == 1000000 and r["url"].startswith("https://payment.zarinpal.com/pg/StartPay/")
    assert conn.execute("SELECT 1 FROM audit_log WHERE action='order_paylink_claimed'").fetchone()
    p = payments.get(conn, r["payment_id"])
    assert (await payments.callback(conn, cfg, p["authority"], "OK"))["status"] == "paid"
    t = conn.execute("SELECT * FROM tickets WHERE user_id=?", (b["id"],)).fetchone()
    assert t["usd"] == pytest.approx(o["quoted_usd"])
    s = payments.link_state(conn, b, o["pay_code"])
    assert (s["state"], s["unfulfilled"]) == ("paid", False)
    with pytest.raises(payments.PaymentError) as e:
        await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    assert e.value.status == 409 and str(e.value) == payments.ALREADY_PAID


async def test_pay_link_refusals(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    b = bob(conn)
    with pytest.raises(orders.OrderError) as e:
        await payments.pay_link(conn, cfg, b, o["pay_code"], "v0")
    assert e.value.status == 400
    with pytest.raises(payments.PaymentError) as e:
        await payments.pay_link(conn, cfg, user(conn, ids["alice"]), o["pay_code"], "v1")
    assert e.value.status == 403
    conn.execute("UPDATE orders SET pay_by=? WHERE id=?", (int(time.time()) - 1, o["id"]))
    with pytest.raises(payments.PaymentError) as e:
        await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    assert e.value.status == 404 and str(e.value) == orders.LINK_INVALID
    assert zp.requests == [] and orders.get(conn, o["id"])["user_id"] is None


async def test_pay_link_skips_the_stale_rate_and_survives_zarinpal_failing(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    b = bob(conn)
    conn.execute("UPDATE fx_rates SET set_at=? WHERE currency='IRT'", (int(time.time()) - 40 * 3600,))
    zp.request_answer = payments.zarinpal.ZarinpalUnavailable("down")
    with pytest.raises(payments.PaymentError) as e:
        await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    assert e.value.status == 502 and orders.get(conn, o["id"])["status"] == "new"
    zp.request_answer = "A1"
    assert (await payments.pay_link(conn, cfg, b, o["pay_code"], "v1"))["url"]


async def test_two_tabs_pay_one_link_once(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    b = bob(conn)
    first = await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    second = await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='order_paylink_claimed'").fetchone()[0] == 1
    for r in (first, second):
        await payments.callback(conn, cfg, payments.get(conn, r["payment_id"])["authority"], "OK")
    assert conn.execute("SELECT COUNT(*) FROM tickets WHERE user_id=?", (b["id"],)).fetchone()[0] == 1


async def test_withdrawn_claimed_link_is_invalid(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    b = bob(conn)
    await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    orders.withdraw(conn, b["id"], o["id"])
    assert payments.link_state(conn, b, o["pay_code"])["state"] == "invalid"
```

Append to `tests/test_payments.py`:

```python
async def test_pay_order_refuses_an_order_already_paid(env):
    conn, cfg, ids, zp = env
    o = unpaid_order(conn, cfg, ids)
    r = await payments.pay_order(conn, cfg, user(conn, ids["alice"]), o["id"])
    conn.execute("UPDATE payments SET status='paid_unfulfilled', ref_id='9' WHERE id=?", (r["payment_id"],))
    with pytest.raises(payments.PaymentError) as e:
        await payments.pay_order(conn, cfg, user(conn, ids["alice"]), o["id"])
    assert e.value.status == 409 and str(e.value) == payments.ALREADY_PAID


async def test_pay_order_on_a_link_skips_the_rate_check_and_keeps_the_deadline(env):
    conn, cfg, ids, zp = env
    o = payments.create_link(conn, cfg, ids["admin"], email="alice@example.com", tier="lite", length="week",
                             pay_by=int(time.time()) + 86400)
    conn.execute("UPDATE orders SET user_id=? WHERE id=?", (ids["alice"], o["id"]))
    conn.execute("UPDATE fx_rates SET set_at=? WHERE currency='IRT'", (int(time.time()) - 40 * 3600,))
    assert (await payments.pay_order(conn, cfg, user(conn, ids["alice"]), o["id"]))["url"]
    conn.execute("UPDATE orders SET pay_by=? WHERE id=?", (int(time.time()) - 1, o["id"]))
    with pytest.raises(payments.PaymentError) as e:
        await payments.pay_order(conn, cfg, user(conn, ids["alice"]), o["id"])
    assert e.value.status == 404
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_paylinks.py tests/test_payments.py -k "link or already_paid"`
Expected: FAIL (`AttributeError: ... 'link_state'` / `'ALREADY_PAID'`).

- [ ] **Step 3: Implement**

In `src/claude_proxy/payments.py`, beside the other messages:

```python
ALREADY_PAID = "This order is already paid."
LINK_OTHER_ORDER = "You have another open order; cancel it first."
```

After `create_link`:

```python
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
    at its quote, without the stale-rate pause. Returns what start() does."""
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
            conn.execute("UPDATE orders SET user_id=?, name=?, terms_version=?, updated_at=? WHERE id=?",
                         (user["id"], user["name"], terms_version, now, o["id"]))
            db.audit(conn, user["id"], "order_paylink_claimed", f"order #{o['id']}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return await _open(conn, cfg, user, orders.get(conn, o["id"]), now, withdraw=False)
```

In `pay_order`, after the `o["currency"] != CURRENCY` check, add:

```python
    if _has_paid(conn, o["id"]):
        raise PaymentError(409, ALREADY_PAID)
    if o["pay_code"] is not None and now > o["pay_by"]:   # a payment link past its deadline (links spec, section 5)
        raise PaymentError(404, orders.LINK_INVALID)
```

and change its rate check to skip link orders (their price was fixed when the link was made):

```python
    if o["pay_code"] is None and not rate_ok(conn, now):
        raise PaymentError(409, PAUSED)
```

Also append to `pay_order`'s docstring: "A payment link's order skips the stale-rate pause and refuses past its deadline; no order with a payment that went through is paid again."

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_paylinks.py tests/test_payments.py tests/test_payments_web.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/payments.py tests/test_paylinks.py tests/test_payments.py
git commit -m "Payment links: link_state and pay_link claim the link for the signed-in user with its email and pay it without the stale-rate pause; no order with a payment that went through is paid again"
```

---

### Task 3: The deadline in reconcile

**Files:**
- Modify: `src/claude_proxy/payments.py` (new `expire_links`; `reconcile`)
- Test: `tests/test_paylinks.py` (append)

**Interfaces:**
- Consumes: `orders.set_status(conn, actor, order_id, to, *, note=None, now=None)`, `orders.OPEN`
- Produces: `payments.LINK_EXPIRED_GRACE_S = 3600`; `payments.expire_links(conn, now: int) -> int`; `reconcile(...)["links_expired"]: int`

- [ ] **Step 1: Write the failing tests**

```python
async def test_reconcile_declines_a_link_an_hour_past_its_deadline(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    t = o["pay_by"] + payments.LINK_EXPIRED_GRACE_S + 1
    assert (await payments.reconcile(conn, cfg, now=o["pay_by"] + 60))["links_expired"] == 0
    assert (await payments.reconcile(conn, cfg, now=t))["links_expired"] == 1
    d = orders.get(conn, o["id"])
    assert (d["status"], d["admin_note"]) == ("declined", "Payment link expired")


async def test_reconcile_leaves_an_expired_link_with_a_payment_going_or_through(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    await payments.pay_link(conn, cfg, bob(conn), o["pay_code"], "v1")
    t = o["pay_by"] + payments.LINK_EXPIRED_GRACE_S + 1
    assert payments.expire_links(conn, t) == 0                              # started
    conn.execute("UPDATE payments SET status='paid_unfulfilled', ref_id='1'")
    assert payments.expire_links(conn, t) == 0                              # went through: the admin's to handle
    assert orders.get(conn, o["id"])["status"] == "new"
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_paylinks.py -k reconcile`
Expected: FAIL (`KeyError: 'links_expired'`).

- [ ] **Step 3: Implement**

In `src/claude_proxy/payments.py`, beside `EXPIRE_S`:

```python
LINK_EXPIRED_GRACE_S = 3600   # a payment link is declined this long after its deadline (links spec, section 5)
```

Before `reconcile`:

```python
def expire_links(conn: sqlite3.Connection, now: int) -> int:
    """Decline open payment links an hour past their deadline with no payment going or gone through (links spec,
    section 5). Returns how many."""
    ids = [r[0] for r in conn.execute(
        f"SELECT o.id FROM orders o WHERE o.pay_code IS NOT NULL AND o.status IN {orders.OPEN} AND o.pay_by<? AND NOT EXISTS "
        "(SELECT 1 FROM payments p WHERE p.order_id=o.id AND p.status IN ('started','paid','paid_unfulfilled'))",
        (now - LINK_EXPIRED_GRACE_S,))]
    for oid in ids:
        orders.set_status(conn, None, oid, "declined", note="Payment link expired", now=now)
    return len(ids)
```

At the end of `reconcile`, just before `return out`:

```python
    try:
        out["links_expired"] = expire_links(conn, now)
    except Exception:
        logger.exception("expiring payment links failed")
        out["links_expired"], out["errors"] = 0, out["errors"] + 1
```

and add `"links_expired": 0` to the `out = {...}` initialiser. Append to `reconcile`'s docstring: "Then payment links an hour past their deadline are declined (expire_links)."

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_paylinks.py tests/test_payments.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/payments.py tests/test_paylinks.py
git commit -m "Payment links: reconcile declines a link an hour past its deadline once no payment on it is going or went through"
```

---

### Task 4: HTTP endpoints

**Files:**
- Modify: `src/claude_proxy/web.py` (admin routes before `@app.post("/api/admin/orders/{oid}")`; `orders_list`; `/pay/{code}` after `/pay/callback`; `/api/me/paylinks/...` after `my_payment`)
- Test: `tests/test_paylinks_web.py` (new)

**Interfaces:**
- Consumes: `payments.create_link`, `payments.link_state`, `payments.pay_link`, `orders.link_price`, `payments.CURRENCY`
- Produces (HTTP):
  - `GET /api/admin/orders/link_quote?tier=&length=` → `{"amount": int}`
  - `POST /api/admin/orders/link` `{email, tier, length, pay_by, amount?, note?}` → `{"ok": true, "order": {...}, "url": str}`
  - `GET /api/admin/orders` rows gain `pay_url` (str or null)
  - `GET /pay/{code}` → 302 to `{dashboard_url}/dashboard#paylink/{code}` (no lookup); 404 for a malformed code
  - `GET /api/me/paylinks/{code}` → 200 `{"state", "message"?, "email"?, "unfulfilled"?, "tier"?, "label"?, "length"?, "amount"?, "currency"?, "pay_by"?, "terms_version"?}` (pass fields only for `open` and `paid`)
  - `POST /api/me/paylinks/{code}/pay` `{terms_version}` → `{"payment_id", "url"}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_paylinks_web.py`:

```python
"""Admin payment links (spec 2026-11-04): the endpoints."""
import time

from claude_proxy import db as dbm, orders
from tests.test_payments_web import client, env  # noqa: F401  (the fixture)
from tests.test_web import admin_client, bearer


async def make(gw, **kw):
    async with admin_client(gw) as c:
        r = await c.post("/api/admin/orders/link", json={"email": "alice@example.com", "tier": "lite", "length": "week",
                                                         "pay_by": int(time.time()) + 86400} | kw)
    assert r.status_code == 200, r.text
    return r.json()


async def test_admin_quotes_creates_and_lists_a_link(env):
    gw, conn, cfg, ids, keys, zp = env
    async with admin_client(gw) as c:
        assert (await c.get("/api/admin/orders/link_quote?tier=lite&length=week")).json() == {"amount": 1250000}
        assert (await c.get("/api/admin/orders/link_quote?tier=gold&length=week")).status_code == 400
    r = await make(gw, note="hi")
    assert r["url"] == f"https://dash.rahkar.pro/pay/{r['order']['pay_code']}"
    async with admin_client(gw) as c:
        row = (await c.get("/api/admin/orders")).json()["orders"][0]
        assert row["pay_url"] == r["url"]
        assert (await c.post("/api/admin/orders/link", json={"email": "x", "tier": "lite", "length": "week",
                                                             "pay_by": int(time.time()) + 60})).status_code == 400


async def test_creating_needs_the_admin(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        r = await c.post("/api/admin/orders/link", headers=bearer(keys["alice"]), json={})
    assert r.status_code == 403


async def test_pay_code_redirects_without_a_lookup(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        r = await c.get("/pay/anything-at-all", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "https://dash.rahkar.pro/dashboard#paylink/anything-at-all"
        assert (await c.get("/pay/bad%20code", follow_redirects=False)).status_code == 404
        r = await c.get("/pay/callback?Authority=nope&Status=OK", follow_redirects=False)
        assert r.status_code == 404   # still the callback, not the link redirect


async def test_view_and_pay_a_link(env):
    gw, conn, cfg, ids, keys, zp = env
    code = (await make(gw, note="secret note"))["order"]["pay_code"]
    async with client(gw) as c:
        v = (await c.get(f"/api/me/paylinks/{code}", headers=bearer(keys["alice"]))).json()
        assert (v["state"], v["amount"], v["label"], v["length"], v["terms_version"]) == ("open", 1250000, "Lite", "week", "v1")
        assert "secret note" not in str(v) and "pay_code" not in v and "email" not in v
        assert (await c.get("/api/me/paylinks/nope", headers=bearer(keys["alice"]))).json()["state"] == "invalid"
        r = await c.post(f"/api/me/paylinks/{code}/pay", headers=bearer(keys["alice"]), json={"terms_version": "v1"})
        assert r.status_code == 200 and r.json()["url"].startswith("https://payment.zarinpal.com/pg/StartPay/")
        auth = r.json()["url"].rsplit("/", 1)[1]
        r = await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        assert r.headers["location"].startswith("https://dash.rahkar.pro/dashboard#payment/")
        v = (await c.get(f"/api/me/paylinks/{code}", headers=bearer(keys["alice"]))).json()
        assert (v["state"], v["unfulfilled"]) == ("paid", False)


async def test_someone_else_sees_only_the_masked_email(env):
    gw, conn, cfg, ids, keys, zp = env
    code = (await make(gw, email="bob@example.com"))["order"]["pay_code"]
    async with client(gw) as c:
        v = (await c.get(f"/api/me/paylinks/{code}", headers=bearer(keys["alice"]))).json()
        assert v == {"state": "other_email", "email": "bo…@example.com",
                     "message": "This link is for bo…@example.com; sign in with that email."}
        r = await c.post(f"/api/me/paylinks/{code}/pay", headers=bearer(keys["alice"]), json={"terms_version": "v1"})
        assert r.status_code == 403
        assert (await c.get(f"/api/me/paylinks/{code}")).status_code == 401
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_paylinks_web.py`
Expected: FAIL (404/405 on the new routes).

- [ ] **Step 3: Admin routes**

In `src/claude_proxy/web.py`, add a helper beside `tier_label` (in the order section):

```python
    def pay_url(o: dict) -> str | None:
        """A payment link's address (links spec, section 3): the dashboard host, which sends the buyer to sign in."""
        return f"{(cfg.listener.dashboard_url or cfg.listener.home_url).rstrip('/')}/pay/{o['pay_code']}" if o.get("pay_code") else None
```

Immediately **before** `@app.post("/api/admin/orders/{oid}")` (FastAPI would otherwise route `link` into `{oid}` and
answer 422):

```python
    # ---------- payment links (spec 2026-11-04) ----------

    @app.get("/api/admin/orders/link_quote")
    async def paylink_quote(request: Request, tier: str = "", length: str = ""):
        """The list price in Toman the link dialog prefills."""
        admin(request)
        need_tickets()
        return {"amount": ticket_call(orders.link_price, conn, cfg, tier, length, payments.CURRENCY, time.time())[1]}

    @app.post("/api/admin/orders/link")
    async def paylink_create(request: Request):
        actor = admin(request, write=True)
        need_tickets()
        b = await _json(request)
        o = ticket_call(payments.create_link, conn, cfg, actor["id"], email=b.get("email"), tier=str(b.get("tier") or ""),
                        length=str(b.get("length") or ""), pay_by=b.get("pay_by"), amount=b.get("amount"), note=b.get("note"))
        return {"ok": True, "order": o, "url": pay_url(o)}
```

In `orders_list`, inside the `for o in rows:` loop, add:

```python
            o["pay_url"] = pay_url(o)
```

- [ ] **Step 4: The link address and the buyer's routes**

After the `@app.get("/pay/callback")` handler:

```python
    @app.get("/pay/{code}")
    async def paylink_open(code: str):
        """A payment link: straight to the dashboard, which keeps #paylink/<code> through sign-in. No lookup here, so
        the address says nothing about the code."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code):
            fail(404, "Not found.")
        return RedirectResponse(f"{(cfg.listener.dashboard_url or cfg.listener.home_url).rstrip('/')}/dashboard#paylink/{code}",
                                status_code=302)
```

(`re` is imported at the top of `web.py`; if not, add `import re`.)

After `my_payment`:

```python
    @app.get("/api/me/paylinks/{code}")
    async def my_paylink(request: Request, code: str):
        """What a payment link is to the signed-in user (links spec, section 4); never the admin's note or the code."""
        user = principal(request)
        need_tickets()
        s = payments.link_state(conn, user, code)
        out = {k: s[k] for k in ("state", "message", "email", "unfulfilled") if k in s}
        if "order" in s:
            o = s["order"]
            out |= {"tier": o["tier"], "label": tier_label(o["tier"]), "length": o["length"], "amount": o["quoted_amount"],
                    "currency": o["currency"], "pay_by": o["pay_by"], "terms_version": tickets.TERMS_VERSION}
        return out

    @app.post("/api/me/paylinks/{code}/pay")
    async def pay_my_paylink(request: Request, code: str):
        user = principal(request, write=True)
        need_tickets()
        body = await _json(request)
        return await payments.pay_link(conn, cfg, user, code, body.get("terms_version"))
```

Note: `"message"` is absent for `open`; the `other_email` test expects exactly `{state, email, message}`.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_paylinks_web.py tests/test_payments_web.py tests/test_orders_web.py`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/claude_proxy/web.py tests/test_paylinks_web.py
git commit -m "Payment links: the admin's quote and create endpoints, /pay/<code> to the dashboard's #paylink, and the signed-in user's view and pay"
```

---

### Task 5: Dashboard (admin dialog, order rows, the buyer's link dialog)

**Files:**
- Modify: `src/claude_proxy/static/app.js` (`renderOrders`, `orderRow`, `orderActions`, `orderAction`; new `payLinkNewDialog`, `linkShownDialog`, `payLinkDialog`, `payLinkMsg`; `route`, `showLogin`, `boot`, `renderOverview`)
- Modify: `src/claude_proxy/static/i18n.js` (new `pl.*` keys in `en` and `fa`)
- Test: `tests/test_paylinks_web.py` (append JS tests through `tests.test_tickets_web._app_fn`)

**Interfaces:**
- Consumes: the HTTP endpoints of Task 4; existing `openDialog`, `goPay`, `orderTerms`, `tierName`, `lengthName`, `money`, `fmtDate`, `toLocal`, `esc`, `api`, `alertInline`
- Produces: `payLinkMsg(r) -> string` (HTML; the email escaped), the `#paylink/<code>` route

- [ ] **Step 1: Write the failing JS tests**

Append to `tests/test_paylinks_web.py`:

```python
def test_paylink_messages_escape_the_email(tmp_path):
    from tests.test_tickets_web import _app_fn
    out = _app_fn(tmp_path, ["payLinkMsg", "esc"],
                  'var out = [payLinkMsg({state: "other_email", email: "<b>@x.io"}), payLinkMsg({state: "paid", unfulfilled: true}),'
                  ' payLinkMsg({state: "invalid"}), payLinkMsg({state: "other_order"}), payLinkMsg({state: "paid", unfulfilled: false})];')
    assert "&lt;b&gt;@x.io" in out[0] and "<b>@" not in out[0]
    assert len(set(out)) == 5 and all(out)


def test_link_orders_show_their_deadline_and_copy_action(tmp_path):
    import json
    from tests.test_orders_web import APP_STUBS
    from tests.test_tickets_web import _app_fn
    o = {"id": 7, "created_at": 1800000000, "name": "a@x.io", "email": "a@x.io", "user_id": None, "tier": "lite", "label": "Lite",
         "length": "week", "currency": "IRT", "quoted_amount": 1250000, "message": "", "status": "new", "payment": None,
         "admin_mail": "off", "buyer_mail": "off", "admin_note": "", "pay_code": "c", "pay_url": "https://d/pay/c", "pay_by": 1800086400}
    html = _app_fn(tmp_path, ["orderRow", "mailState", "orderActions", "tierName"], APP_STUBS + f"var out = orderRow({json.dumps(o)});")
    assert 'data-oact="copylink"' in html and 'data-oact="grant"' not in html and 'data-oact="decline"' in html
```

(`APP_STUBS` in `tests/test_orders_web.py` defines `fmtDate`, `fmtAgo`, `money`, `bdi`, `esc`, `t` and friends for
`orderRow`. If `fmtDate` is missing from it, add `fmtDate` to the `_app_fn` name list instead.)

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_paylinks_web.py -k "messages or copy_action"`
Expected: FAIL (`not found: payLinkMsg`, then the missing `copylink`).

- [ ] **Step 3: i18n keys**

In `src/claude_proxy/static/i18n.js`, add to the `en` table (near the `ord.*` keys):

```js
    "pl.new": "Payment link",
    "pl.tier": "Pass",
    "pl.length": "Length",
    "pl.price": "Price (Toman)",
    "pl.pay_by_label": "Pay by",
    "pl.create": "Create link",
    "pl.made": "Payment link created",
    "pl.send_it": "Send this link to the buyer. They sign in with the email you entered and pay from their dashboard.",
    "pl.copy": "Copy link",
    "pl.row": "Payment link, pay by {date}",
    "pl.unclaimed": "not opened yet",
    "pl.title": "Payment link",
    "pl.pay_by": "Pay by {date}",
    "pl.invalid": "This payment link is no longer valid.",
    "pl.paid": "This link is paid; your pass is on your dashboard.",
    "pl.paid_unfulfilled": "Your payment was received; the admin will be in touch about your pass.",
    "pl.other_email": "This link is for {email}; sign in with that email.",
    "pl.other_order": "You have another open order; cancel it first, then open the link again.",
```

and to the `fa` table:

```js
    "pl.new": "لینک پرداخت",
    "pl.tier": "بسته",
    "pl.length": "مدت",
    "pl.price": "مبلغ (تومان)",
    "pl.pay_by_label": "مهلت پرداخت",
    "pl.create": "ساخت لینک",
    "pl.made": "لینک پرداخت ساخته شد",
    "pl.send_it": "این لینک را برای خریدار بفرستید. او با همین ایمیل وارد می‌شود و از داشبورد خودش پرداخت می‌کند.",
    "pl.copy": "کپی لینک",
    "pl.row": "لینک پرداخت، مهلت تا {date}",
    "pl.unclaimed": "هنوز باز نشده",
    "pl.title": "لینک پرداخت",
    "pl.pay_by": "مهلت پرداخت تا {date}",
    "pl.invalid": "این لینک پرداخت دیگر معتبر نیست.",
    "pl.paid": "این لینک پرداخت شده است؛ بسته شما در داشبوردتان است.",
    "pl.paid_unfulfilled": "پرداخت شما دریافت شد؛ ادمین برای بسته‌تان با شما تماس می‌گیرد.",
    "pl.other_email": "این لینک برای {email} است؛ با همان ایمیل وارد شوید.",
    "pl.other_order": "یک سفارش باز دیگر دارید؛ اول آن را لغو کنید و بعد دوباره لینک را باز کنید.",
```

- [ ] **Step 4: Admin side**

In `renderOrders`, in the controls row right after the `</select>` of `#order-status`, add a button shown while
payments can take a link:

```js
${mode && mode !== "manual" && !down ? `<button class="btn small" id="paylink-new">${t("pl.new")}</button>` : ""}
```

and after the `#pay-mode` wiring:

```js
  if ($("#paylink-new")) $("#paylink-new").onclick = () => payLinkNewDialog();
```

In `orderRow`, replace the `const account = …` line with:

```js
  const account = o.pay_code ? `<div class="muted">${t("pl.row", { date: esc(fmtDate(o.pay_by)) })}${o.user_id == null ? ` · ${t("pl.unclaimed")}` : ""}</div>`
    : o.user_id != null ? `<div class="muted">${t("ord.account")} <a class="user-link" href="#user/${o.user_id}">${bdi(o.user_name || `#${o.user_id}`)}</a></div>`
    : `<div class="muted">${t("ord.visitor")}</div>`;
```

Replace `orderActions` with (a link is paid by its buyer, so no Grant; its actions are Copy link, Decline and Note):

```js
// Exactly the status table: Contacted from new; Decline and Grant while open; Note always. A payment link: Copy link
// while open instead of Contacted and Grant, since its buyer pays it.
function orderActions(o) {
  const b = (act, label, cls = "") => `<button class="btn small${cls}" data-oact="${act}" data-id="${o.id}">${label}</button>`;
  const open = o.status === "new" || o.status === "contacted";
  if (o.pay_code) return `<div class="row-actions">${open ? b("copylink", t("pl.copy")) + b("decline", t("ord.act_decline"), " danger") : ""}${b("note", t("tk.note"))}</div>`;
  return `<div class="row-actions">${o.status === "new" ? b("contacted", t("ord.act_contacted")) : ""}${open ? b("decline", t("ord.act_decline"), " danger") : ""}${b("note", t("tk.note"))}${open ? (o.upgrade_of ? b("upgrade", t("up.act"), " primary") : b("grant", t("ord.act_grant"), " primary")) : ""}</div>`;
}
```

At the top of `orderAction`, after `if (!o) return;`:

```js
  if (act === "copylink") return linkShownDialog(o.pay_url);
```

New functions, after `orderAction`:

```js
// A payment link (spec 2026-11-04): email, pass, the list price (editable), a deadline 3 days ahead by default, a note.
async function payLinkNewDialog() {
  let tiers, lengths;
  try { ({ tiers, lengths } = await api("/api/admin/prices")); } catch (e) { return alertInline(e.message); }
  const d = openDialog(`<h3>${t("pl.new")}</h3>
    <form id="f-pl" class="form-grid">
      <label class="wide">${t("ord.email")}<input type="email" name="email" dir="ltr" required maxlength="254"></label>
      <label>${t("pl.tier")}<select name="tier">${Object.entries(tiers).map(([id, x]) => `<option value="${esc(id)}">${tierName(id, x.label)}</option>`).join("")}</select></label>
      <label>${t("pl.length")}<select name="length">${lengths.map((k) => `<option value="${esc(k)}">${lengthName(k)}</option>`).join("")}</select></label>
      <label>${t("pl.price")}<input type="number" name="amount" min="1000" step="1" dir="ltr" required></label>
      <label>${t("pl.pay_by_label")}<input type="datetime-local" name="pay_by" required value="${toLocal(Date.now() / 1000 + 3 * 86400)}"></label>
      <label class="wide">${t("tk.note")}<input type="text" name="note" maxlength="200"></label>
      <div class="error wide" id="pl-err"></div>
      <div class="dialog-actions wide"><button class="btn" type="button" data-close>${t("app.cancel")}</button>
        <button class="btn primary" type="submit">${t("pl.create")}</button></div>
    </form>`);
  const f = $("#f-pl", d);
  let listed = null;
  const quote = async () => {
    try { listed = (await api(`/api/admin/orders/link_quote?tier=${encodeURIComponent(f.tier.value)}&length=${encodeURIComponent(f.length.value)}`)).amount; f.amount.value = listed; $("#pl-err", d).textContent = ""; }
    catch (e) { $("#pl-err", d).textContent = e.message; }
  };
  f.tier.onchange = f.length.onchange = quote;
  quote();
  f.onsubmit = async (e) => {
    e.preventDefault();
    const go = f.querySelector('button[type="submit"]'), amount = +f.amount.value;
    go.disabled = true;
    try {
      const r = await api("/api/admin/orders/link", { method: "POST", body: { email: f.email.value, tier: f.tier.value, length: f.length.value,
        pay_by: Math.floor(new Date(f.pay_by.value).getTime() / 1000), note: f.note.value, ...(amount !== listed ? { amount } : {}) } });
      linkShownDialog(r.url);
    } catch (err) { go.disabled = false; $("#pl-err", d).textContent = err.message; }
  };
}
function linkShownDialog(url) {
  openDialog(`<h3>${t("pl.made")}</h3><p>${t("pl.send_it")}</p><code class="key" dir="ltr">${esc(url)}</code>
    <p><button class="btn" id="pl-copy">${t("pl.copy")}</button> <button class="btn primary" data-close>${t("app.done")}</button></p>`);
  $("#pl-copy").onclick = async () => { try { await navigator.clipboard.writeText(url); $("#pl-copy").textContent = t("key.copied"); } catch { /* clipboard blocked */ } };
}
```

- [ ] **Step 5: The buyer's side**

In `route()`, after the `#payment/<id>` handling:

```js
  const pl = /^paylink\/([A-Za-z0-9_-]{1,64})$/.exec(h);
  if (pl) { S.tab = "overview"; S.payLink = pl[1]; forgetPayLink(); history.replaceState(null, "", "#overview"); return true; }
```

Beside `rememberAuthorize` / `forgetAuthorize` / `rememberedAuthorize`:

```js
// #paylink/<code> survives Clerk's sign-in (which may drop the hash) in this tab for 30 minutes, as #authorize does.
function rememberPayLink(code) { try { sessionStorage.setItem("cp-paylink", JSON.stringify({ code, at: Date.now() })); } catch { /* private mode */ } }
function forgetPayLink() { try { sessionStorage.removeItem("cp-paylink"); } catch { /* private mode */ } }
function rememberedPayLink() {
  try { const v = JSON.parse(sessionStorage.getItem("cp-paylink")); return v && Date.now() - v.at < 1800000 ? v.code : null; } catch { return null; }
}
```

In `showLogin()`, after `if (m) rememberAuthorize(m[1]);`:

```js
  const pm = /^#paylink\/([A-Za-z0-9_-]{1,64})$/.exec(location.hash);
  if (pm) rememberPayLink(pm[1]);
```

In `boot()`, after the `pending` authorize line:

```js
  const link = rememberedPayLink();
  if (link && !pending && !location.hash.startsWith("#paylink/")) history.replaceState(null, "", `#paylink/${link}`);
```

Add `payLink: null,` to the `S` state object beside `paymentId` with the comment `// #paylink/<code>: opened once over the overview`.

In `renderOverview`, after the `if (S.paymentId) {…}` block:

```js
  if (S.payLink) { const code = S.payLink; S.payLink = null; payLinkDialog(code, tk); }
```

New functions, after `goPay`:

```js
// A payment link's notice for every state but open. HTML: the masked email is escaped here.
function payLinkMsg(r) {
  if (r.state === "paid") return t(r.unfulfilled ? "pl.paid_unfulfilled" : "pl.paid");
  if (r.state === "other_email") return t("pl.other_email", { email: esc(r.email) });
  if (r.state === "other_order") return t("pl.other_order");
  return t("pl.invalid");
}
// The link's pass at its price, with the order dialog's terms for this account: it starts after the current passes.
async function payLinkDialog(code, tk) {
  let r;
  try { r = await api(`/api/me/paylinks/${encodeURIComponent(code)}`); } catch (e) { return alertInline(e.message); }
  if (r.state !== "open") return openDialog(`<h3>${t("pl.title")}</h3><p>${payLinkMsg(r)}</p><p><button class="btn primary" data-close>${t("app.ok")}</button></p>`);
  const tier = tk && tk.prices && tk.prices.tiers.find((x) => x.tier === r.tier);
  const after = !!(tk && tk.current && !tk.replaces);
  const terms = tier ? orderTerms(tk.prices, tier, r.length, after ? { date: tk.next_start, splitNow: tk.split_now } : null, after) : "";
  const price = esc(money(r.amount, r.currency));
  const d = openDialog(`<h3>${t("ord.title", { what: t("ord.what", { label: tierName(r.tier, r.label), len: lengthName(r.length) }) })}</h3>
    <p class="order-price"><b>${price}</b></p><p class="muted">${t("pl.pay_by", { date: esc(fmtDate(r.pay_by)) })}</p>
    <form id="f-paylink" class="form-grid order-form">${terms ? `<p class="terms muted wide">${terms}</p>` : ""}
      <label class="check wide"><input type="checkbox" name="terms"> ${t("ord.terms_accept")}</label>
      <div class="error wide" id="pl-err"></div>
      <div class="dialog-actions wide"><button class="btn" type="button" data-close>${t("app.cancel")}</button>
        <button class="btn primary" type="submit">${t("pay.button", { amount: price })}</button></div></form>`);
  const f = $("#f-paylink", d);
  f.onsubmit = async (e) => {
    e.preventDefault();
    if (!f.terms.checked) { $("#pl-err", d).textContent = t("ord.terms_needed"); return; }
    const b = f.querySelector('button[type="submit"]');
    b.disabled = true; b.textContent = t("pay.going");
    try { goPay((await api(`/api/me/paylinks/${encodeURIComponent(code)}/pay`, { method: "POST", body: { terms_version: r.terms_version } })).url); }
    catch (err) { b.disabled = false; b.textContent = t("pay.button", { amount: price }); $("#pl-err", d).textContent = err.message; }
  };
}
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_paylinks_web.py tests/test_i18n.py tests/test_orders_web.py tests/test_tickets_web.py`
Expected: all PASS (`test_i18n` checks the `pl.*` keys exist in both languages).

- [ ] **Step 7: Check it in the browser**

Start the seeded local dashboard as in the "Local dashboard check" memory (`:18081`), sign in as the admin, open
Orders → **Payment link**, check the price prefills, the deadline defaults to 3 days ahead, and creating shows the link.
Open the link in a session signed in as a user whose email matches: the dialog shows the pass, price, deadline and
terms; Pay reaches ZarinPal (sandbox or the faked local gateway). Open it as a different user: the masked-email message.

- [ ] **Step 8: Commit**

```bash
git add src/claude_proxy/static/app.js src/claude_proxy/static/i18n.js tests/test_paylinks_web.py
git commit -m "Payment links in the dashboard: the admin's Payment link dialog and Copy link, and the buyer's #paylink dialog kept through sign-in, paid with goPay"
```

---

### Task 6: Whole suite, then push

- [ ] **Step 1:** Run: `.venv/bin/python -m pytest -q` — Expected: no failures.
- [ ] **Step 2:** `git pull --rebase origin master` only if the tree is clean apart from other sessions' files (otherwise
  push without pulling when `master` is not behind), then `git push origin master`. Pushes to `master` deploy: watch
  `gh run list --workflow deploy.yml -L 1` until it succeeds.
- [ ] **Step 3:** On the live dashboard, create one link to your own email, open it signed in, and stop at ZarinPal's
  card page (do not pay); then decline it from the Orders tab and check the link answers "no longer valid".
