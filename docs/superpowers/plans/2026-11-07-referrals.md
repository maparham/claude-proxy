# Referrals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A referral link per user; a friend's first pass is discounted, and the referrer earns one discount unit per paying friend, used one per purchase and never below the pass's cost.

**Architecture:** One new module `src/claude_proxy/referrals.py` owns the settings, the cost floor, codes, units and rewards. It is called from the existing order, payment and grant code at a few fixed points, always inside their existing transactions. Admin and user endpoints live in `web.py`; the tab and the invite card in `static/app.js`.

**Tech Stack:** Python 3, SQLite, FastAPI (`web.py`), vanilla JS dashboard, pytest (`.venv/bin/python -m pytest`).

**Spec:** `docs/superpowers/specs/2026-11-07-referrals-design.md`

## Global Constraints

- Work on `master`; commit only the paths each task touches (other sessions share this repo).
- Never sell a pass below cost: what the gateway keeps = price before VAT × (1 − discount) − fee, fee = fee_pct × amount charged (VAT included), must be ≥ account cost × tier share × days ÷ 30.
- At most one unit per purchase. A first pass gets only the friend discount, never a unit.
- Left out of referrals entirely: upgrades (`orders.upgrade_of` set), admin payment links (`orders.create_link`), orders created with no user.
- No refunds and no waiting time: a unit is earned when the friend's pass is granted (or approved, with manual approval).
- Defaults: off; friend 10%; unit 10%; most held 5; most earned per Tehran calendar month 5; unit lifetime 90 days; approval automatic; account cost $100 per 30 days; fee 0%.
- Every user-visible string in both `en` and `fa` in `static/i18n.js`. Persian wording uses loanwords (لینک) and کلاد.
- Explanatory tooltips use the existing "?" icon helper (`tipI`), never dotted text.

## Review Focus

1. A discount whose floor leaves it at 0% must not use up a unit: the unit stays saved (test in Task 3).
2. Manual grant of a discounted order: the preview, the grant and the stored ticket price all show the discounted price, with no "price changed" refusal (test in Task 4).
3. A declined order whose ZarinPal payment verified but could not be granted (`paid_unfulfilled`) keeps its unit spent (test in Task 3).
4. Re-signing up with an email that already had a pass gets no friend discount (test in Task 2).
5. Changing a tier's share or price between ordering and granting re-checks the floor at grant time (test in Task 4).

---

### Task 1: Settings, schema and the cost floor

**Files:**
- Create: `src/claude_proxy/referrals.py`
- Modify: `src/claude_proxy/db.py` (SCHEMA list and `_migrate`)
- Test: `tests/test_referrals.py`

**Interfaces:**
- Produces:
  - Tables: `referral_units(id INTEGER PK, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, pct REAL NOT NULL, earned_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('saved','held','used','expired','cancelled')), order_id INTEGER, reward_id INTEGER, updated_at INTEGER NOT NULL)`; `referral_rewards(id INTEGER PK, referrer_id INTEGER NOT NULL, friend_id INTEGER, order_id INTEGER NOT NULL UNIQUE, created_at INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('waiting','earned','refused','over_limit')), decided_by INTEGER, decided_at INTEGER)`. `referrer_id` has no foreign key, so history survives a deleted referrer.
  - Columns via `_migrate`: `users.ref_code TEXT` (unique index, NULLs allowed), `users.referred_by INTEGER`; `orders.ref_kind TEXT` (`'friend'`, `'unit'` or NULL), `orders.ref_pct REAL` (the percentage asked for), `orders.ref_off_usd REAL`, `orders.ref_unit_id INTEGER`, `orders.list_usd REAL` (the price before the referral discount).
  - `@dataclass Settings(enabled=False, friend_pct=10.0, unit_pct=10.0, max_held=5, max_per_month=5, unit_days=90, approval="auto", account_cost_usd=100.0, fee_pct=0.0)`
  - `settings(conn) -> Settings`: read from `settings` key `"referrals"` (JSON), missing keys take defaults.
  - `set_settings(conn, actor: int | None, changes: dict) -> Settings`: validates (percentages 0–90, counts 0–1000 integers, days 1–3650, approval in `auto`/`manual`, cost > 0, fee 0–20), stores, and audits `referral_settings` with the changed keys. Raises `ReferralError(400, msg)`.
  - `class ReferralError(Exception)` with `.status` and `.message`, like `orders.OrderError`.
  - `pass_cost(conn, cfg, tier: str, days: int) -> float` = `account_cost_usd × tickets.effective_share(conn, cfg, tier) / 100 × days / 30`.
  - `apply_pct(conn, cfg, tier: str, length: str, usd: float, pct: float) -> tuple[float, float]`: returns `(new_usd, pct_applied)`, with `pct_applied ≤ pct` lowered so that `new_usd × (1 − fee × (1 + vat)) ≥ pass_cost`, and `new_usd` rounded **up** to the cent. A negative result clamps to `(usd, 0.0)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_defaults(env):            # conn from tests.tickets_helpers.seeded, shares set to lite 20, standard 40
    s = referrals.settings(conn)
    assert (s.enabled, s.friend_pct, s.unit_pct, s.max_held, s.max_per_month, s.unit_days, s.approval,
            s.account_cost_usd, s.fee_pct) == (False, 10, 10, 5, 5, 90, "auto", 100, 0)

def test_set_settings_validates_and_audits(env):
    referrals.set_settings(conn, admin, {"unit_pct": 15})
    assert referrals.settings(conn).unit_pct == 15
    with pytest.raises(referrals.ReferralError): referrals.set_settings(conn, admin, {"approval": "maybe"})
    assert audit_actions(conn)[-1] == "referral_settings"

def test_pass_cost(env):
    assert referrals.pass_cost(conn, cfg, "lite", 1) == pytest.approx(100 * 0.20 / 30)
    assert referrals.pass_cost(conn, cfg, "standard", 7) == pytest.approx(100 * 0.40 * 7 / 30)

def test_apply_pct_room(env):        # Standard week $15, cost $9.33: 10% fits
    assert referrals.apply_pct(conn, cfg, "standard", "week", 15.0, 10) == (13.5, 10)

def test_apply_pct_floored(env):     # 50% would go below $9.33: lowered to keep it at or above cost
    usd, pct = referrals.apply_pct(conn, cfg, "standard", "week", 15.0, 50)
    assert usd >= 100 * 0.40 * 7 / 30 and pct < 50

def test_apply_pct_fee_on_vat_included(env):   # 10% VAT, 5% fee: kept = usd × (1 − 0.05 × 1.1)
    referrals.set_settings(conn, admin, {"fee_pct": 5})
    usd, _ = referrals.apply_pct(conn, cfg, "standard", "week", 15.0, 50)
    assert usd * (1 - 0.05 * 1.1) >= 100 * 0.40 * 7 / 30 - 1e-9

def test_apply_pct_no_room(env):     # price already at cost
    tickets.set_price(conn, cfg, "lite", "day", 0.67, admin)
    assert referrals.apply_pct(conn, cfg, "lite", "day", 0.67, 10) == (0.67, 0.0)
```

The test module's `env` fixture: `seeded(db)`, `live(conn, NOW)`, `cfg.tickets.vat_percent` (or the `[zarinpal] vat_percent` the code reads in `tickets.vat_percent`) set to 10, and `tickets.set_share` lite 20 / standard 40, standard week price 15.

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/test_referrals.py -q`
Expected: FAIL, `ModuleNotFoundError: claude_proxy.referrals`

- [ ] **Step 3: Implement the schema, `_migrate` columns and the functions above**
- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_referrals.py tests/test_tickets_schema.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `src/claude_proxy/referrals.py src/claude_proxy/db.py tests/test_referrals.py`

---

### Task 2: Codes, the link, and attaching a friend at sign-up

**Files:**
- Modify: `src/claude_proxy/referrals.py`, `src/claude_proxy/web.py` (new `GET /r/{code}`, `login_clerk`, `clerk_account` at web.py:300-366)
- Test: `tests/test_referrals.py`, `tests/test_signup.py`

**Interfaces:**
- Consumes: `settings`, the `users` columns (Task 1).
- Produces:
  - `code_for(conn, user_id: int) -> str`: the user's code, created on first call: 8 characters from `abcdefghjkmnpqrstuvwxyz23456789`, retried on a collision.
  - `user_by_code(conn, code: str) -> int | None`: an enabled, not revoked user with that code (case-insensitive).
  - `attach(conn, friend_id: int, code: str | None) -> bool`: sets `users.referred_by` when referrals are on, the code resolves, and it is not the friend's own. Audits `referral_signup`. Called inside the sign-up transaction.
  - `first_pass(conn, user_id: int) -> bool`: True when the user has no ticket, and no ticket with `lower(user_name)` = the user's email, and no `done` order with that email. That catches an email that had a pass on an earlier, deleted account.
  - `GET /r/{code}` (dashboard host; the home host already redirects unknown paths there, web.py:182): sets the cookie `ref=<code>` (30 days, `HttpOnly`, `SameSite=Lax`, `Secure` when https) **only if no `ref` cookie exists**, then 302 to `/`. An unknown code still redirects, with no cookie.
  - `clerk_account(clerk_id, email, ref)`: `login_clerk` passes `request.cookies.get("ref")`; the new-account branch calls `referrals.attach` before `COMMIT`. Linking an existing account never attaches.

- [ ] **Step 1: Write the failing tests**

```python
def test_code_stable_and_resolves(env):
    c = referrals.code_for(conn, alice); assert c == referrals.code_for(conn, alice)
    assert referrals.user_by_code(conn, c.upper()) == alice

def test_attach_needs_on_and_not_self(env):
    bob = add_user(conn, "bob@x.com"); code = referrals.code_for(conn, alice)
    assert referrals.attach(conn, bob, code) is False            # referrals off
    referrals.set_settings(conn, admin, {"enabled": True})
    assert referrals.attach(conn, alice, code) is False          # own code
    assert referrals.attach(conn, bob, code) is True
    assert user(conn, bob)["referred_by"] == alice

def test_first_pass_by_email_survives_account_delete(env):
    # grant bob a pass, revoke and delete bob, create bob2 with the same email: not a first pass
    ...
    assert referrals.first_pass(conn, bob2) is False
```

In `tests/test_signup.py`: `test_ref_cookie_attaches_new_clerk_account` (GET `/r/<alice code>`, then sign in a new Clerk user with that cookie, so `referred_by == alice`), `test_ref_cookie_first_wins` (a second `/r/` does not overwrite it), and `test_existing_account_not_attached`.

- [ ] **Step 2: Run them and see them fail.** `.venv/bin/python -m pytest tests/test_referrals.py tests/test_signup.py -q`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run them and see them pass**, plus `tests/test_home_host.py` (the `/r/` redirect from the home host)
- [ ] **Step 5: Commit**

---

### Task 3: The discount on an order, and the life of a unit

**Files:**
- Modify: `src/claude_proxy/referrals.py`, `src/claude_proxy/orders.py` (`create` at :125-199, `_change` at :271), `src/claude_proxy/payments.py` (`_fulfil`'s `mark`, `_unfulfilled`)
- Test: `tests/test_referrals.py`

**Interfaces:**
- Consumes: `apply_pct`, `first_pass`, `settings` (Tasks 1–2).
- Produces:
  - `expire(conn, now: int) -> None`: saved units past `expires_at` become `expired` (audited `referral_unit_expired`). Called at the top of `quote`, `my_status` and the admin listing. Held units are left alone.
  - `quote(conn, cfg, user_id: int, tier: str, length: str, usd: float, now: int) -> dict | None`: `{"kind": "friend"|"unit", "pct": requested, "pct_applied", "usd": new, "list_usd": usd, "off_usd", "unit_id": int|None}`. Friend: referrals on, `referred_by` set, `first_pass`. Otherwise unit: the oldest saved, unexpired unit. Returns None when neither applies. Changes nothing.
  - `hold(conn, order_id: int, q: dict | None, now: int) -> None`: writes the order's `ref_*` and `list_usd` columns. For a unit with `pct_applied > 0`, marks it `held` with `order_id`. A unit whose `pct_applied` is 0 is **not** held, and the order gets no `ref_kind`.
  - `spend(conn, order_id: int, now: int) -> None`: a held unit becomes `used` (audited `referral_unit_used` with `off_usd`). Idempotent.
  - `release(conn, order_id: int, now: int) -> None`: when no payment on the order has `status IN ('paid','paid_unfulfilled')`, a held unit goes back to `saved`, or to `expired` if past `expires_at`. Otherwise it calls `spend`.
- Hook points:
  - `orders.create`: after `usd = tickets.price_now(...)["usd"]`, when `user_id is not None`, call `q = referrals.quote(...)` and use `q["usd"]` for `quoted_usd` and the amount. After the INSERT, inside the same transaction, call `referrals.hold(conn, oid, q, now)`. The buyer's one-open-order rule (orders.py:159) already rules out a double hold.
  - `orders._change`: when `to in ("declined", "withdrawn")`, call `referrals.release`.
  - `payments`: `mark(tid)` and `_unfulfilled` call `referrals.spend(conn, p["order_id"], now)`.
  - `orders.create_link` is untouched (payment links stay out).

- [ ] **Step 1: Write the failing tests**

```python
def test_friend_discount_on_first_order(env_on):      # bob referred by alice, lite week $8
    o = signed_in(conn, cfg, bob)
    assert (o["ref_kind"], o["quoted_usd"], o["list_usd"]) == ("friend", 7.2, 8.0)

def test_second_order_no_friend_discount(env_on): ...  # after bob's first pass is granted

def test_unit_held_then_used_on_grant(env_on):         # alice has 1 saved unit
    o = signed_in(conn, cfg, alice); assert o["ref_kind"] == "unit" and unit_state(conn, o["ref_unit_id"]) == "held"
    grant_from_order(conn, cfg, o); assert unit_state(...) == "used"

def test_one_unit_per_order(env_on):                   # 3 saved units: the order takes 10%, two stay saved

def test_withdraw_returns_unit(env_on): ...
def test_withdraw_after_expiry_expires_unit(env_on): ...
def test_floored_to_zero_keeps_unit_saved(env_on):     # price set to cost: no ref_kind, the unit stays saved
def test_paid_unfulfilled_then_declined_keeps_unit_spent(env_on):
    # insert a payment row with status 'paid_unfulfilled' on the order, then decline: the unit is 'used'
def test_expired_saved_unit_not_offered(env_on): ...
def test_payment_link_untouched(env_on):               # orders.create_link: ref_kind is None, the unit stays saved
```

- [ ] **Step 2: Run them and see them fail.** `.venv/bin/python -m pytest tests/test_referrals.py -q`
- [ ] **Step 3: Implement the functions and the hooks**
- [ ] **Step 4: Run** `tests/test_referrals.py tests/test_orders.py tests/test_payments.py tests/test_payments_core.py tests/test_paylinks.py -q`. Expected: PASS.
- [ ] **Step 5: Commit**

---

### Task 4: Grants keep the discount; earning units

**Files:**
- Modify: `src/claude_proxy/tickets.py` (`preview` at :758, `grant` at :800), `src/claude_proxy/web.py` (`/api/admin/tickets/preview` at :1336 takes `order_id`), `src/claude_proxy/static/app.js:1381` (the grant dialog sends `order_id` with the preview when it is opened from an order), `src/claude_proxy/referrals.py`
- Test: `tests/test_referrals.py`

**Interfaces:**
- Consumes: `apply_pct`, `spend` (Tasks 1, 3).
- Produces:
  - `tickets.preview(..., order_id: int | None = None)`: when the order has `ref_pct`, it sets `p["usd"], p["ref_pct_applied"] = referrals.apply_pct(conn, cfg, tier, length, p["usd"], order.ref_pct)`, sets `p["ref_off_usd"]`, and recomputes `amount` from the new usd. The floor is checked again on today's price and share.
  - `tickets.grant`: passes `order_id` to `preview` on the unpaid path, so `expect_usd` compares against the discounted price. The paid path is unchanged (`quoted_usd` is already discounted). After `orders.close_for_grant`, it calls `referrals.on_granted(conn, order_id, now)`.
  - `on_granted(conn, order_id: int, now: int) -> None`: `spend(...)`. If the order is `ref_kind='friend'` and has no `upgrade_of`, it inserts a `referral_rewards` row for `users.referred_by` of the buyer, then:
    - when the referrer is gone, revoked or disabled: no row;
    - when approval is `manual`: status `waiting`;
    - else `earn(...)`.
  - `earn(conn, reward_id: int, actor: int | None, now: int) -> str`: refuses with `over_limit` when the referrer holds `max_held` saved plus held units, or already earned `max_per_month` units this Tehran calendar month (`tickets.TEHRAN`). Otherwise it inserts a unit (`pct = settings.unit_pct`, `expires_at = now + unit_days × 86400`) with status `earned`. Audits `referral_unit_earned` or `referral_over_limit`. Returns the status.
  - `approve(conn, actor, reward_id, now) -> str` (calls `earn`), and `refuse(conn, actor, reward_id, now)` (status `refused`, audited). Both only act on a `waiting` reward, else `ReferralError(409)`.
  - Referrals off: `on_granted` still spends a held unit but creates no reward (no new units). `approve` still works on rewards already waiting, and `quote` still offers saved units (spec: saved units stay usable).

- [ ] **Step 1: Write the failing tests**

```python
def test_manual_grant_keeps_friend_discount(env_on):
    o = signed_in(conn, cfg, bob)                       # $7.20
    pv = tickets.preview(conn, cfg, user(conn, bob), "lite", "week", "EUR", NOW, order_id=o["id"])
    assert pv["usd"] == 7.2
    t = tickets.grant(conn, cfg, admin, user(conn, bob), "lite", "week", "EUR", expect_usd=7.2, expect_rate=pv["rate"],
                      order_id=o["id"], now=NOW)
    assert t["usd"] == 7.2 and t["list_usd"] == 8.0

def test_share_raised_before_grant_refloors(env_on):   # set_share standard 90 after ordering; the preview usd ≥ new cost
def test_friend_grant_earns_unit(env_on): ...          # alice gets one saved unit, 10%, expires NOW + 90 days
def test_manual_approval_waits(env_on): ...            # status waiting, no unit; approve leads to earned + unit
def test_refuse(env_on): ...
def test_over_limit_held(env_on): ...                  # max_held 1, alice already holds 1: over_limit
def test_month_limit_tehran(env_on): ...               # max_per_month 1; 2nd earned the same Tehran month: over_limit; next month: earned
def test_deleted_referrer_friend_keeps_discount_no_reward(env_on): ...
def test_paid_online_grant_earns(env_on): ...          # through payments._fulfil with a faked verify
def test_off_no_new_unit_but_approve_works(env_on): ...
```

- [ ] **Step 2: Run them and see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `tests/test_referrals.py tests/test_tickets_*.py tests/test_orders*.py tests/test_payments*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit**

---

### Task 5: Admin API and the Referrals tab

**Files:**
- Modify: `src/claude_proxy/referrals.py`, `src/claude_proxy/web.py`, `src/claude_proxy/static/app.js` (the `VIEWS` map at :485, a new `renderReferrals`), `src/claude_proxy/static/i18n.js`
- Test: `tests/test_referrals_web.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `cancel_unit(conn, actor, unit_id, now)`: only a `saved` unit becomes `cancelled`; a held one is refused with 409 (it stays with its order, per the spec).
  - `admin_view(conn, now) -> dict`: `{"settings": asdict(Settings), "waiting": [...], "referrers": [...], "history": [...]}`.
    - `waiting`: rewards with status `waiting`, with referrer, friend, tier, length and the order's `quoted_usd`.
    - `referrers`: each user with a `ref_code`, counting friends signed up (`referred_by`), friends who bought (rewards of any status), and units saved, held, used and expired.
    - `history`: the last 200 `audit_log` rows with `action LIKE 'referral_%'`.
  - Endpoints, all `admin(request)` (writes take `write=True`):
    - `GET /api/admin/referrals` returns `admin_view`
    - `PUT /api/admin/referrals/settings` takes a body of changed keys and returns the settings
    - `POST /api/admin/referrals/rewards/{rid}/approve` and `.../refuse`
    - `POST /api/admin/referrals/units/{uid}/cancel`
    - `ReferralError` maps to its status, like `_order_error`.
  - Tab `referrals: { render: renderReferrals, admin: true, feature: "tickets" }`, with four sections as sub-tabs (`subTabs`): Settings (a form with one row per setting and a tooltip each), Waiting for approval, Referrers (with a Cancel button on saved units), History.

- [ ] **Step 1: Write the failing tests.** `test_admin_only` (a user gets 403), `test_settings_put_roundtrip`, `test_approve_refuse_endpoints`, `test_cancel_saved_unit`, `test_cancel_held_unit_409`, `test_view_counts`.
- [ ] **Step 2: Run them and see them fail.** `.venv/bin/python -m pytest tests/test_referrals_web.py -q`
- [ ] **Step 3: Implement the API, the tab and the strings**
- [ ] **Step 4: Run** `tests/test_referrals_web.py tests/test_i18n.py -q` and check the tab in the browser using the local dashboard (memory: seeded dev gateway on :18081). The Referrals tab shows, settings save, and approve/refuse work.
- [ ] **Step 5: Commit**

---

### Task 6: What users see: invite card, prices, emails, Telegram

**Files:**
- Modify: `src/claude_proxy/referrals.py`, `src/claude_proxy/web.py`, `src/claude_proxy/static/app.js` (overview), `src/claude_proxy/static/i18n.js`, `src/claude_proxy/orders.py` (`buyer_mail_text`, `_buyer_mail_fa`), `src/claude_proxy/payments.py` (the paid receipt text near :495-517), `src/claude_proxy/telegram.py` (the `COMMANDS` list and a `/invite` command)
- Test: `tests/test_referrals_web.py`, `tests/test_telegram.py`, `tests/test_mail.py`

**Interfaces:**
- Produces:
  - `my_status(conn, cfg, user_id, now) -> dict`: `{"enabled", "link": f"{home_url or dashboard_url}/r/{code}", "friend_pct", "units": [{"pct", "expires_at", "state"}], "waiting": int}`. When off: `{"enabled": False}` and no link.
  - `GET /api/referrals/me` returns `my_status`.
  - `GET /api/referrals/quote?tier=&length=` returns `quote(...)` or `null`, so the order dialog shows the original price, the discount and the final price (the dialog's amount in Toman uses the same `round_local` the server uses, via the response's `amount`).
  - The overview gets an "Invite friends" card: the link, a copy button, the friend's discount, saved units with expiry dates (Persian dates via the existing date formatting), and how many are waiting for approval.
  - Emails: when the order has `ref_off_usd > 0`, one line "Referral discount: X% (−$Y)", in Persian «تخفیف معرفی دوستان: …».
  - Telegram: `/invite` (in both `COMMANDS` lists) replies with the same link and units for a linked chat, and with the existing "not linked" message otherwise.

- [ ] **Step 1: Write the failing tests.** `test_me_off_hides_link`, `test_me_shows_link_and_units`, `test_quote_endpoint_friend_and_unit`, `test_buyer_mail_shows_discount_line` (en and fa), `test_telegram_invite_linked_and_unlinked`.
- [ ] **Step 2: Run them and see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the full suite** `.venv/bin/python -m pytest -q -n auto`. Expected: all pass. Check the card and the order dialog in the local dashboard, in English and Persian.
- [ ] **Step 5: Commit**
