# Paid Tickets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sell reserved slices of the Claude subscription as 1-day, 1-week and 1-month tickets: the admin sets prices, rates, discounts and bonuses in the dashboard, grants tickets, and the gateway enforces each ticket's share per 5-hour window and per ticket day, never selling the same capacity twice.

**Architecture:** A new `tickets.py` module owns prices, exchange rates, discounts, capacity (`sold(t)` as a step function checked at start points inside one `BEGIN IMMEDIATE` transaction) and the grant / cancel / bonus / ungate operations. `quota.py` gains a per-request attribution (`request_shares`) that `limits.py` uses to measure a ticket user's share since their current ticket day began, and an observed tokens-per-point rate for the stale-snapshot fallback. `limits.evaluate` and `limits.states` treat a user with any ticket row not marked `ungated_at` as ticket-gated: no active ticket is a 403, an active one builds `share_5h` and `share_day` in place of admin share rows. A new `estimates.py` computes the lower-bound usage hints from busy hours once a day inside the existing maintenance loop. `web.py` adds the admin endpoints, `/api/me/tickets`, the public `/api/pricing` and `/pricing` page; `app.js` gets Tickets and Pricing tabs for admins and a ticket card for users.

**Tech Stack:** Python 3.12, FastAPI, SQLite (WAL, `BEGIN IMMEDIATE`, savepoints), pytest (+ pytest-asyncio, `asyncio_mode = "auto"`), vanilla JS dashboard, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-03-paid-tickets-design.md`. Read it first; this plan argues from it. Its companion `docs/superpowers/specs/2026-10-03-ticket-window-alignment-note.md` explains why the day is the unit. Base design: `docs/superpowers/specs/2026-09-21-claude-proxy-design.md` sections 7 and 8.

## Global Constraints

- Run tests with `.venv/bin/python -m pytest` from the repo root. Before Task 1 the suite shows `477 passed, 47 skipped`; it must pass after every task (skips are the Windows VM tests).
- No new Python or JS dependencies. Scripts on the dashboard pages come only from `'self'` and the pinned echarts CDN (CSP in `web.SecurityHeaders`): no inline `<script>`.
- Work on branch `paid-tickets`, created from `paid-tickets-spec`. Commit messages follow the repo's style (`Area: what changed`, sentence case, no `feat:` prefix) and end with the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- All ticket times are integer epoch seconds, UTC. A day is `86400` s from the ticket's `starts_at`. Lengths: `day = 1`, `week = 7`, `month = 30` days. One account, `account_id = 1`.
- `max_sold_pct` defaults to `80`. Default tiers: `lite` (label `Lite`, 5%, compare `Claude Pro`, USD 3 / 8 / 20) and `standard` (label `Standard`, 25%, compare `Claude Max 5x`, USD 12 / 35 / 100). USD is always available at rate 1 with `round_to = 0.01` and may not be configured as a currency. A rate older than `36` hours is stale.
- Local price: `round(usd × rate, round_to)` with ties rounded up (`ROUND_HALF_UP`): $20 × 0.92 = 18.40 → €18.50; 18.25 → €18.50.
- Tickets are enabled when the config file has a `[tickets]` section (`cfg.tickets.enabled`). Without it nothing changes for anyone: no tabs, `/pricing` is 404, grants are refused.
- Refusal messages, verbatim:
  - no active ticket: `Your ticket ended on <date>. <how_to_buy>` or `Your next ticket starts on <date>.`, HTTP 403, error type `permission_error`, `rejected_by = "ticket"`; `<date>` is `YYYY-MM-DD HH:MM UTC`.
  - third-party model for a ticket user without a `cost_*` row: 403 `permission_error`, `Your ticket covers Claude models only.`, `rejected_by = "ticket"`.
  - no usage rate ever observed: 503, error type `api_error`, `Usage data is unavailable. Please retry in a minute.`, `Retry-After: 60`, `rejected_by = "ticket_no_data"`.
  - `share_day` exceeded: 429 `rate_limit_error`, `Today's share of your <tier label> ticket is used up. It resets at <HH:MM UTC>.`, `Retry-After` = seconds to the end of the ticket day, `rejected_by = "share_day"`.
  - `share_5h` exceeded: the existing wording (`Gateway 5-hour limit reached: N% used; retry in …`).
- Audit actions added: `rate_set`, `price_set`, `discount_create`, `discount_cancel`, `ticket_grant`, `ticket_cancel`, `ticket_bonus`, `credit_removed`, `ungate`; `limit_clear` is reused for rows removed with a grant (detail carries `ticket_id`). Target is the user name for ticket actions and `tier:length` for price actions.
- Dashboard HTTP statuses: `TicketError` → 400, `CapacityError` → 409, both with `{"error": message}`.
- Non-admins never receive account utilization, the number of tickets sold or other users' data. `/api/pricing` and `/pricing` show only prices, discounts, the rate date, the usage hints and a sold-out flag per tier and length.

## Review Focus

1. **A ticket user on a gateway that has no quota snapshot at all** (fresh install, or snapshots purged). `attribution` returns no utilization, `observed_rate` returns None. Expected: a 503 with `Retry-After: 60`, not a crash and not an unlimited pass. Pinned in Task 7, `test_ticket_user_gets_503_when_no_rate_was_ever_observed`.
2. **Anthropic's weekly window resets in the middle of a ticket day.** Usage before and after the reset in that day must both count toward `share_day`; the drop must not count as negative usage and the rise after it must not be lost. Pinned in Task 6, `test_request_shares_count_both_sides_of_a_weekly_reset`.
3. **An exchange rate set for a currency that is not configured** (`GBP` when only `EUR` is in the config). Expected: refused with 400 and no `fx_rates` row. Pinned in Task 3, `test_set_rate_refuses_an_unconfigured_currency`, and Task 9 at the HTTP layer.
4. **Bonus days on a ticket that has already ended.** The extension starts at the old end in the past; if the new end is in the future the ticket is active again and its day boundaries keep stepping from the original start. Expected: allowed, `covering(now)` finds it, `current_day` is aligned to `starts_at`. Pinned in Task 5, `test_bonus_days_on_an_ended_ticket_make_it_cover_now_with_the_old_day_boundaries`.
5. **`/pricing` before any EUR rate exists.** Expected: the page falls back to USD prices with no "converted at" line, and shows nothing broken. Pinned in Task 12, `test_pricing_falls_back_to_usd_without_a_rate`.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/claude_proxy/config.py` | modify | `Tier`, `Currency`, `TicketsConfig`, `LENGTHS`, loading and validation of `[tickets]` |
| `src/claude_proxy/db.py` | modify | six new tables and indexes, `TICKET_EFFECTIVE_END`, live-ticket check in `delete_user` |
| `src/claude_proxy/tickets.py` | create | rates, prices, discounts, rounding; `sold_at`, `check_capacity`; grant, cancel, bonus, ungate, queue moves; `preview`, `capacity`, `sold_out`, `price_table`, `user_state` |
| `src/claude_proxy/quota.py` | modify | `request_shares`, `attributed_since`, `observed_rate` |
| `src/claude_proxy/limits.py` | modify | ticket gating, `ticket_states`, 403/429/503 decisions, labels for `share_day` |
| `src/claude_proxy/estimates.py` | create | busy hours, per-family share per hour, 75th percentile, `refresh`, `refresh_if_due`, `hours_hint` |
| `src/claude_proxy/gateway.py` | modify | seed prices at start |
| `src/claude_proxy/cli.py` | modify | maintenance loop calls `estimates.refresh_if_due`; `user delete --yes` |
| `src/claude_proxy/web.py` | modify | admin endpoints (rates, prices, discounts, tickets, capacity, ungate), typed delete confirmation, `/api/me/tickets`, `/api/pricing`, `/pricing`, session and users additions |
| `src/claude_proxy/static/app.js` | modify | Tickets and Pricing tabs, grant / bonus / cancel dialogs, Ungate, delete-by-name dialog, user ticket card and today bar |
| `src/claude_proxy/static/app.css` | modify | a few classes for the ticket card, strike-through and badges |
| `src/claude_proxy/static/pricing.html`, `pricing.js` | create | the public page and its countdown |
| `config.example.toml`, `README.md` | modify | `[tickets]` example and the admin's how-to |
| `tests/test_tickets_config.py` | create | Task 1 |
| `tests/test_tickets_schema.py` | create | Task 2 |
| `tests/test_tickets_prices.py` | create | Task 3 |
| `tests/test_tickets_capacity.py` | create | Tasks 4 and 5 |
| `tests/test_quota.py` | modify | Task 6 |
| `tests/test_tickets_limits.py` | create | Task 7 |
| `tests/test_tickets_delete.py` | create | Task 8 |
| `tests/test_tickets_web.py` | create | Tasks 9, 10, 12 |
| `tests/test_estimates.py` | create | Task 11 |

Shared test helpers live in `tests/tickets_helpers.py` (created in Task 3) so later tasks import, not copy, the fixtures.

---

### Task 1: Ticket configuration

**Files:**
- Modify: `src/claude_proxy/config.py`
- Test: `tests/test_tickets_config.py`

**Interfaces:**
- Produces: `config.LENGTHS = {"day": 1, "week": 7, "month": 30}`; `config.Tier(label, share_pct, compare="", default_usd={})`; `config.Currency(round_to=0.01)`; `config.TicketsConfig(enabled, how_to_buy, max_sold_pct, tiers: dict[str, Tier], currencies: dict[str, Currency])`; `Config.tickets: TicketsConfig`. `ConfigError` on bad values.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tickets_config.py
import pytest

from claude_proxy.config import LENGTHS, Config, ConfigError, Tier, TicketsConfig


def load(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return Config.load(str(p))


def test_defaults_are_the_spec_tiers_and_disabled():
    t = TicketsConfig()
    assert t.enabled is False and t.max_sold_pct == 80 and t.currencies == {}
    assert t.tiers["lite"].share_pct == 5 and t.tiers["lite"].default_usd == {"day": 3, "week": 8, "month": 20}
    assert t.tiers["standard"].share_pct == 25 and t.tiers["standard"].compare == "Claude Max 5x"
    assert LENGTHS == {"day": 1, "week": 7, "month": 30}


def test_tickets_section_enables_and_overrides(tmp_path):
    cfg = load(tmp_path, """
[tickets]
how_to_buy = "Pay by bank transfer."
max_sold_pct = 60
[tickets.tiers.mini]
label = "Mini"
share_pct = 2.5
compare = "a trial"
default_usd = { day = 1, week = 4, month = 10 }
[tickets.currencies.eur]
round_to = 0.5
""")
    assert cfg.tickets.enabled is True and cfg.tickets.how_to_buy == "Pay by bank transfer."
    assert cfg.tickets.max_sold_pct == 60
    assert list(cfg.tickets.tiers) == ["mini"] and cfg.tickets.tiers["mini"].share_pct == 2.5
    assert cfg.tickets.currencies["EUR"].round_to == 0.5   # codes are upper-cased


def test_empty_tickets_section_keeps_default_tiers(tmp_path):
    cfg = load(tmp_path, "[tickets]\nhow_to_buy = 'x'\n")
    assert cfg.tickets.enabled and set(cfg.tickets.tiers) == {"lite", "standard"}


@pytest.mark.parametrize("body,needle", [
    ("[tickets]\nmax_sold_pct = 0\n", "max_sold_pct"),
    ("[tickets]\nmax_sold_pct = 101\n", "max_sold_pct"),
    ("[tickets]\nbogus = 1\n", "bogus"),
    ("[tickets.tiers.x]\nlabel='X'\nshare_pct=0\ndefault_usd={day=1,week=2,month=3}\n", "share_pct"),
    ("[tickets.tiers.x]\nlabel='X'\nshare_pct=5\ndefault_usd={day=1,week=2}\n", "default_usd"),
    ("[tickets.tiers.x]\nlabel='X'\nshare_pct=5\ndefault_usd={day=0,week=2,month=3}\n", "default_usd"),
    ("[tickets.tiers.Bad-Id]\nlabel='X'\nshare_pct=5\ndefault_usd={day=1,week=2,month=3}\n", "tier id"),
    ("[tickets.currencies.USD]\nround_to=0.01\n", "USD"),
    ("[tickets.currencies.EUR]\nround_to=0\n", "round_to"),
])
def test_bad_tickets_config_is_refused(tmp_path, body, needle):
    with pytest.raises(ConfigError) as e:
        load(tmp_path, body)
    assert needle in str(e.value)


def test_tier_dataclass_validates_directly():
    with pytest.raises(TypeError):
        Tier(label="X", share_pct=5, default_usd={"day": 1})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tickets_config.py -q`
Expected: FAIL with `ImportError: cannot import name 'LENGTHS'`.

- [ ] **Step 3: Add the dataclasses and loader**

In `src/claude_proxy/config.py`, after `LimitsConfig`:

```python
LENGTHS = {"day": 1, "week": 7, "month": 30}   # ticket lengths in days (paid-tickets design, section 3)


@dataclass
class Tier:
    """A size of slice sold as tickets: its share of the account and its default USD prices per length."""
    label: str
    share_pct: float
    compare: str = ""
    default_usd: dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.share_pct, bool) or not isinstance(self.share_pct, (int, float)) or not 0 < self.share_pct <= 100:
            raise TypeError("share_pct must be a number above 0 and at most 100")
        if set(self.default_usd) != set(LENGTHS):
            raise TypeError(f"default_usd needs exactly the keys {', '.join(LENGTHS)}")
        for k, v in self.default_usd.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                raise TypeError(f"default_usd.{k} must be a price above 0")


@dataclass
class Currency:
    round_to: float = 0.01

    def __post_init__(self):
        if isinstance(self.round_to, bool) or not isinstance(self.round_to, (int, float)) or self.round_to <= 0:
            raise TypeError("round_to must be a step above 0, e.g. 0.50")


def _default_tiers() -> dict[str, Tier]:
    return {
        "lite": Tier("Lite", 5, "Claude Pro", {"day": 3, "week": 8, "month": 20}),
        "standard": Tier("Standard", 25, "Claude Max 5x", {"day": 12, "week": 35, "month": 100}),
    }


@dataclass
class TicketsConfig:
    """Paid tickets (design 2026-10-03). `enabled` is set when the config file has a [tickets] section."""
    enabled: bool = False
    how_to_buy: str = ""
    max_sold_pct: float = 80      # ceiling on what tickets and bonuses may reserve; the rest is headroom
    tiers: dict[str, Tier] = field(default_factory=_default_tiers)
    currencies: dict[str, Currency] = field(default_factory=dict)   # USD is built in and must not appear here
```

Add `tickets: TicketsConfig = field(default_factory=TicketsConfig)` to `Config` (after `signup`). In `Config.load`, after the `routes` block and before `cfg.config_file = toml_path`:

```python
        if "tickets" in data:
            cfg.tickets = _load_tickets(toml_path, data["tickets"])
```

And a module-level function after `_default_routes`:

```python
_TIER_ID = re.compile(r"^[a-z][a-z0-9_]*$")


def _load_tickets(toml_path: str, data: dict) -> TicketsConfig:
    t = TicketsConfig(enabled=True)
    data = dict(data)
    try:
        tiers = data.pop("tiers", None)
        if tiers is not None:
            for tier_id in tiers:
                if not _TIER_ID.match(tier_id):
                    raise ConfigError(f"{toml_path}: tier id {tier_id!r} must be lowercase letters, digits and underscores")
            t.tiers = {k: Tier(**v) for k, v in tiers.items()}
        t.currencies = {k.upper(): Currency(**v) for k, v in data.pop("currencies", {}).items()}
    except TypeError as e:
        raise ConfigError(f"{toml_path}: bad [tickets] entry: {e}") from e
    if "USD" in t.currencies:
        raise ConfigError(f"{toml_path}: USD is built in; do not configure it under [tickets.currencies]")
    for k, v in data.items():
        if k == "enabled" or not hasattr(t, k):
            raise ConfigError(f"{toml_path}: unknown key [tickets].{k}")
        setattr(t, k, v)
    if isinstance(t.max_sold_pct, bool) or not isinstance(t.max_sold_pct, (int, float)) or not 0 < t.max_sold_pct <= 100:
        raise ConfigError(f"{toml_path}: [tickets].max_sold_pct must be above 0 and at most 100")
    if not t.tiers:
        raise ConfigError(f"{toml_path}: [tickets] needs at least one tier")
    return t
```

Add `import re` to the imports at the top of `config.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tickets_config.py tests/test_units.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/config.py tests/test_tickets_config.py
git commit -m "Config: [tickets] section with tiers, currencies and max_sold_pct

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Schema and price seeding

**Files:**
- Modify: `src/claude_proxy/db.py`, `src/claude_proxy/gateway.py`
- Create: `src/claude_proxy/tickets.py` (module skeleton with `seed_prices` only; Task 3 fills it)
- Test: `tests/test_tickets_schema.py`

**Interfaces:**
- Produces: tables `ticket_prices`, `ticket_discounts`, `fx_rates`, `tickets`, `ticket_bonuses`, `usage_estimates`; `db.TICKET_EFFECTIVE_END` (SQL expression over alias `t`); `tickets.seed_prices(conn, cfg, now=None) -> int`; `Gateway.__init__` seeds prices when tickets are enabled.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tickets_schema.py
import sqlite3

from claude_proxy import tickets
from claude_proxy.config import Config
from claude_proxy.db import init_db
from tests.conftest import make_gateway


def cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_ticket_tables_exist_with_the_spec_columns(db):
    conn = db[1]
    assert cols(conn, "ticket_prices") == {"tier", "length", "usd", "updated_at", "updated_by"}
    assert cols(conn, "ticket_discounts") == {"id", "tier", "length", "usd", "starts_at", "ends_at", "created_by", "created_at", "cancelled_at"}
    assert cols(conn, "fx_rates") == {"id", "currency", "rate", "set_at", "set_by"}
    assert cols(conn, "tickets") >= {"id", "user_id", "user_name", "account_id", "tier", "share_pct", "length", "days", "starts_at",
                                      "ends_at", "list_usd", "usd", "discount_id", "currency", "rate", "amount", "granted_by",
                                      "granted_at", "cancelled_at", "cancelled_by", "note", "ungated_at"}
    assert cols(conn, "ticket_bonuses") == {"id", "ticket_id", "share_pct", "extra_days", "starts_at", "ends_at", "note", "granted_by",
                                            "granted_at", "cancelled_at", "cancelled_by"}
    assert cols(conn, "usage_estimates") == {"computed_at", "family", "bucket", "busy_hours", "p75_share_per_hour"}


def test_init_is_idempotent_on_an_existing_database(tmp_path):
    path = tmp_path / "x.db"
    init_db(path).close()
    conn = init_db(path)   # second start: CREATE IF NOT EXISTS, no error
    conn.execute("INSERT INTO ticket_prices(tier, length, usd, updated_at) VALUES('lite','day',3,0)")
    conn.close()


def test_deleting_a_user_keeps_their_tickets_with_user_id_null(db):
    conn = db[1]
    from claude_proxy.db import create_user
    uid, _ = create_user(conn, "alice")
    conn.execute("INSERT INTO tickets(user_id, user_name, account_id, tier, share_pct, length, days, starts_at, ends_at, list_usd, usd, "
                 "currency, rate, amount, granted_at) VALUES(?,?,1,'lite',5,'day',1,0,86400,3,3,'USD',1,3,0)", (uid, "alice"))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    row = conn.execute("SELECT user_id, user_name FROM tickets").fetchone()
    assert (row["user_id"], row["user_name"]) == (None, "alice")


def test_seed_prices_fills_only_missing_rows(db):
    conn = db[1]
    cfg = Config()
    cfg.tickets.enabled = True
    assert tickets.seed_prices(conn, cfg, now=100) == 6
    conn.execute("UPDATE ticket_prices SET usd=4 WHERE tier='lite' AND length='day'")
    assert tickets.seed_prices(conn, cfg, now=200) == 0
    assert conn.execute("SELECT usd FROM ticket_prices WHERE tier='lite' AND length='day'").fetchone()[0] == 4


def test_gateway_seeds_prices_when_tickets_are_enabled(cfg, db):
    conn = db[1]
    make_gateway(cfg, conn)
    assert conn.execute("SELECT COUNT(*) FROM ticket_prices").fetchone()[0] == 0
    cfg.tickets.enabled = True
    make_gateway(cfg, conn)
    assert conn.execute("SELECT COUNT(*) FROM ticket_prices").fetchone()[0] == 6
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tickets_schema.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'claude_proxy.tickets'`.

- [ ] **Step 3: Add the tables**

Append to `SCHEMA` in `src/claude_proxy/db.py` (after the `device_requests` table):

```python
    # ---- paid tickets (design 2026-10-03) ----
    """CREATE TABLE IF NOT EXISTS ticket_prices (
        tier TEXT NOT NULL,
        length TEXT NOT NULL CHECK(length IN ('day','week','month')),
        usd REAL NOT NULL,
        updated_at INTEGER NOT NULL,
        updated_by INTEGER,
        PRIMARY KEY (tier, length)
    )""",
    """CREATE TABLE IF NOT EXISTS ticket_discounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tier TEXT NOT NULL,
        length TEXT NOT NULL CHECK(length IN ('day','week','month')),
        usd REAL NOT NULL,
        starts_at INTEGER NOT NULL,
        ends_at INTEGER NOT NULL,
        created_by INTEGER,
        created_at INTEGER NOT NULL,
        cancelled_at INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ticket_discounts_tier ON ticket_discounts(tier, length, starts_at)",
    # A history: the newest row per currency is the current rate (local units per 1 USD).
    """CREATE TABLE IF NOT EXISTS fx_rates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        currency TEXT NOT NULL,
        rate REAL NOT NULL,
        set_at INTEGER NOT NULL,
        set_by INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fx_rates_currency ON fx_rates(currency, set_at)",
    # A sales record: never deleted. Deleting the user leaves user_id NULL and user_name as it was.
    """CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        user_name TEXT NOT NULL,
        account_id INTEGER NOT NULL DEFAULT 1,
        tier TEXT NOT NULL,
        share_pct REAL NOT NULL,            -- copied at grant: a later config change never touches a sold ticket
        length TEXT NOT NULL CHECK(length IN ('day','week','month')),
        days INTEGER NOT NULL,
        starts_at INTEGER NOT NULL,
        ends_at INTEGER NOT NULL,           -- starts_at + days * 86400; bonus days come on top (TICKET_EFFECTIVE_END)
        list_usd REAL NOT NULL,
        usd REAL NOT NULL,
        discount_id INTEGER,
        currency TEXT NOT NULL,
        rate REAL NOT NULL,
        amount REAL NOT NULL,
        granted_by INTEGER,
        granted_at INTEGER NOT NULL,
        cancelled_at INTEGER,
        cancelled_by INTEGER,
        note TEXT,
        ungated_at INTEGER                  -- set by the Ungate action: this row no longer makes its user ticket-gated
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id, starts_at)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_account ON tickets(account_id, starts_at)",
    """CREATE TABLE IF NOT EXISTS ticket_bonuses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES tickets(id),
        share_pct REAL NOT NULL DEFAULT 0,   -- applies between starts_at and ends_at
        extra_days INTEGER NOT NULL DEFAULT 0,   -- extend the ticket's end at the ticket's own share
        starts_at INTEGER NOT NULL,
        ends_at INTEGER NOT NULL,
        note TEXT,
        granted_by INTEGER,
        granted_at INTEGER NOT NULL,
        cancelled_at INTEGER,
        cancelled_by INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ticket_bonuses_ticket ON ticket_bonuses(ticket_id)",
    # Written by the daily maintenance task (estimates.py), read by /pricing, which never computes anything itself.
    """CREATE TABLE IF NOT EXISTS usage_estimates (
        computed_at INTEGER NOT NULL,
        family TEXT NOT NULL,
        bucket TEXT NOT NULL,
        busy_hours INTEGER NOT NULL,
        p75_share_per_hour REAL,
        PRIMARY KEY (family, bucket)
    )""",
```

Below `SCHEMA`, add the shared SQL fragment (used by `tickets.py` and by `delete_user` in Task 8):

```python
# A ticket's effective end: ends_at plus its non-cancelled bonus days. For queries that alias tickets as `t`.
TICKET_EFFECTIVE_END = ("(t.ends_at + 86400 * COALESCE((SELECT SUM(b.extra_days) FROM ticket_bonuses b "
                        "WHERE b.ticket_id=t.id AND b.cancelled_at IS NULL), 0))")
```

- [ ] **Step 4: Create the module skeleton and seed at start**

`src/claude_proxy/tickets.py`:

```python
"""Paid tickets (design 2026-10-03): reserved slices of the account sold by the day, week or month.

Prices, discounts and exchange rates (spec section 6); capacity reserved in time (section 5); grants, cancels,
bonuses and the Ungate action (sections 5, 7, 8). Every change to reservations runs in one BEGIN IMMEDIATE
transaction, so two admins acting at once cannot oversell.
"""
from __future__ import annotations

import sqlite3
import time

from .config import LENGTHS, Config

DAY = 86400
ACCOUNT_ID = 1   # step 1 has exactly one account (spec section 1)


def seed_prices(conn: sqlite3.Connection, cfg: Config, now: float | None = None) -> int:
    """Insert each tier's default_usd for every length that has no price yet. Returns the rows added."""
    now = int(time.time() if now is None else now)
    n = 0
    for tier, t in cfg.tickets.tiers.items():
        for length in LENGTHS:
            n += conn.execute("INSERT OR IGNORE INTO ticket_prices(tier, length, usd, updated_at, updated_by) VALUES(?,?,?,?,NULL)",
                              (tier, length, float(t.default_usd[length]), now)).rowcount
    return n
```

In `src/claude_proxy/gateway.py`, import `from . import tickets` and at the end of `Gateway.__init__`:

```python
        if cfg.tickets.enabled:
            tickets.seed_prices(conn, cfg)
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_tickets_schema.py tests/test_web.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/claude_proxy/db.py src/claude_proxy/gateway.py src/claude_proxy/tickets.py tests/test_tickets_schema.py
git commit -m "Tickets: tables for prices, discounts, rates, tickets, bonuses and usage estimates

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Rates, prices, discounts and rounding

**Files:**
- Modify: `src/claude_proxy/tickets.py`
- Create: `tests/tickets_helpers.py`, `tests/test_tickets_prices.py`

**Interfaces:**
- Produces, all in `tickets.py`:
  - `class TicketError(Exception)` (admin-facing message), `class CapacityError(TicketError)` (declared here, raised from Task 4).
  - `STALE_RATE_S = 36 * 3600`, `USD_ROUND_TO = 0.01`.
  - `round_local(usd: float, rate: float, round_to: float) -> float`
  - `currencies(cfg) -> dict[str, float]` code → round_to, USD included.
  - `current_rate(conn, currency) -> dict | None` with keys `currency, rate, set_at, set_by` (USD: rate 1, set_at None).
  - `rate_is_stale(rate: dict, now) -> bool`
  - `set_rate(conn, cfg, currency, rate, actor, now=None) -> dict`
  - `regular_price(conn, tier, length) -> float`, `set_price(conn, cfg, tier, length, usd, actor, now=None) -> None`, `prices(conn) -> list[dict]`
  - `active_discount(conn, tier, length, now) -> dict | None`, `create_discount(conn, cfg, tier, length, usd, starts_at, ends_at, actor, now=None) -> dict`, `cancel_discount(conn, discount_id, actor, now=None) -> None`, `discounts(conn, now, include_ended=False) -> list[dict]`
  - `price_now(conn, tier, length, now) -> dict` with keys `list_usd, usd, discount_id, discount_ends_at`.
- Test helpers in `tests/tickets_helpers.py`: `NOW = 1_800_000_000`, `tcfg()` → a `Config` with tickets enabled and EUR at `round_to 0.5`, `seeded(db)` → `(conn, cfg)` with prices seeded and admin + alice created, returns ids too.

- [ ] **Step 1: Write the helpers and failing tests**

```python
# tests/tickets_helpers.py
"""Shared fixtures for the paid-tickets tests."""
from claude_proxy import tickets
from claude_proxy.config import Config, Currency
from claude_proxy.db import create_user

NOW = 1_800_000_000          # 2027-01-15 08:00:00 UTC
DAY = 86400


def tcfg() -> Config:
    cfg = Config()
    cfg.tickets.enabled = True
    cfg.tickets.how_to_buy = "Send the amount by bank transfer and email the admin."
    cfg.tickets.currencies = {"EUR": Currency(round_to=0.5)}
    return cfg


def seeded(db):
    """(conn, cfg, ids): prices seeded from the default tiers, an admin and alice created, EUR at 0.92."""
    conn = db[1]
    cfg = tcfg()
    tickets.seed_prices(conn, cfg, now=NOW - DAY)
    admin, _ = create_user(conn, "admin", role="admin")
    alice, _ = create_user(conn, "alice")
    tickets.set_rate(conn, cfg, "EUR", 0.92, admin, now=NOW - 3600)
    return conn, cfg, {"admin": admin, "alice": alice}


def user(conn, uid):
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
```

```python
# tests/test_tickets_prices.py
import pytest

from claude_proxy import tickets
from tests.tickets_helpers import DAY, NOW, seeded, tcfg


@pytest.mark.parametrize("usd,rate,step,expected", [
    (20, 0.92, 0.5, 18.5),      # 18.40 -> nearest 0.50 is 18.50
    (18.25, 1, 0.5, 18.5),      # a tie rounds up
    (18.24, 1, 0.5, 18.0),
    (3, 1, 0.01, 3.0),
    (3, 0.915, 0.01, 2.75),     # 2.745 -> ties up
    (100, 1.3333, 0.01, 133.33),
])
def test_round_local(usd, rate, step, expected):
    assert tickets.round_local(usd, rate, step) == pytest.approx(expected)


def test_usd_is_always_available_at_rate_one(db):
    conn, cfg, ids = seeded(db)
    assert tickets.currencies(cfg) == {"USD": 0.01, "EUR": 0.5}
    assert tickets.current_rate(conn, "USD") == {"currency": "USD", "rate": 1.0, "set_at": None, "set_by": None}
    assert tickets.rate_is_stale(tickets.current_rate(conn, "USD"), NOW) is False


def test_newest_rate_wins_and_history_is_kept(db):
    conn, cfg, ids = seeded(db)
    tickets.set_rate(conn, cfg, "EUR", 0.95, ids["admin"], now=NOW)
    r = tickets.current_rate(conn, "EUR")
    assert (r["rate"], r["set_at"], r["set_by"]) == (0.95, NOW, ids["admin"])
    assert conn.execute("SELECT COUNT(*) FROM fx_rates WHERE currency='EUR'").fetchone()[0] == 2
    assert conn.execute("SELECT action, target, detail_json FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[:2] == ("rate_set", "EUR")


def test_set_rate_refuses_an_unconfigured_currency(db):
    conn, cfg, ids = seeded(db)
    with pytest.raises(tickets.TicketError, match="GBP"):
        tickets.set_rate(conn, cfg, "GBP", 0.8, ids["admin"], now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM fx_rates WHERE currency='GBP'").fetchone()[0] == 0
    for bad in (0, -1, float("nan"), float("inf"), "0.9"):
        with pytest.raises(tickets.TicketError):
            tickets.set_rate(conn, cfg, "EUR", bad, ids["admin"], now=NOW)


def test_rate_is_stale_after_36_hours(db):
    conn, cfg, ids = seeded(db)
    r = tickets.current_rate(conn, "EUR")   # set an hour ago
    assert tickets.rate_is_stale(r, NOW) is False
    assert tickets.rate_is_stale(r, NOW + 36 * 3600) is True
    assert tickets.current_rate(conn, "CHF") is None


def test_prices_seeded_and_editable_with_audit(db):
    conn, cfg, ids = seeded(db)
    assert tickets.regular_price(conn, "lite", "week") == 8
    tickets.set_price(conn, cfg, "lite", "week", 9.5, ids["admin"], now=NOW)
    assert tickets.regular_price(conn, "lite", "week") == 9.5
    row = conn.execute("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == ("price_set", "lite:week")
    assert {(p["tier"], p["length"]) for p in tickets.prices(conn)} == {(t, l) for t in ("lite", "standard") for l in ("day", "week", "month")}
    with pytest.raises(tickets.TicketError):
        tickets.set_price(conn, cfg, "gold", "week", 9, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError):
        tickets.set_price(conn, cfg, "lite", "year", 9, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError):
        tickets.set_price(conn, cfg, "lite", "week", 0, ids["admin"], now=NOW)


def test_discount_replaces_the_price_while_active(db):
    conn, cfg, ids = seeded(db)
    d = tickets.create_discount(conn, cfg, "lite", "week", 6, NOW + 100, NOW + DAY, ids["admin"], now=NOW)
    assert tickets.price_now(conn, "lite", "week", NOW) == {"list_usd": 8, "usd": 8, "discount_id": None, "discount_ends_at": None}
    assert tickets.price_now(conn, "lite", "week", NOW + 100) == {"list_usd": 8, "usd": 6, "discount_id": d["id"], "discount_ends_at": NOW + DAY}
    assert tickets.price_now(conn, "lite", "week", NOW + DAY)["usd"] == 8          # half-open: over at ends_at
    assert conn.execute("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[:] == ("discount_create", "lite:week")


def test_discount_must_be_below_the_regular_price_and_in_the_future(db):
    conn, cfg, ids = seeded(db)
    with pytest.raises(tickets.TicketError, match="below the regular price"):
        tickets.create_discount(conn, cfg, "lite", "week", 8, NOW, NOW + DAY, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError, match="below the regular price"):
        tickets.create_discount(conn, cfg, "lite", "week", 9, NOW, NOW + DAY, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError, match="end in the future"):
        tickets.create_discount(conn, cfg, "lite", "week", 5, NOW - 2 * DAY, NOW - DAY, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError, match="before it ends"):
        tickets.create_discount(conn, cfg, "lite", "week", 5, NOW + DAY, NOW + DAY, ids["admin"], now=NOW)


def test_overlapping_discount_refused_but_touching_allowed(db):
    conn, cfg, ids = seeded(db)
    tickets.create_discount(conn, cfg, "lite", "week", 6, NOW, NOW + DAY, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError, match="already covers"):
        tickets.create_discount(conn, cfg, "lite", "week", 5, NOW + DAY - 1, NOW + 2 * DAY, ids["admin"], now=NOW)
    with pytest.raises(tickets.TicketError, match="already covers"):   # a future one overlapping a future one
        tickets.create_discount(conn, cfg, "lite", "week", 5, NOW - 10, NOW + 10, ids["admin"], now=NOW)
    tickets.create_discount(conn, cfg, "lite", "week", 5, NOW + DAY, NOW + 2 * DAY, ids["admin"], now=NOW)   # touching
    tickets.create_discount(conn, cfg, "lite", "day", 2, NOW, NOW + DAY, ids["admin"], now=NOW)             # another length
    assert len(tickets.discounts(conn, NOW)) == 3


def test_cancelled_discount_frees_the_period(db):
    conn, cfg, ids = seeded(db)
    d = tickets.create_discount(conn, cfg, "lite", "week", 6, NOW, NOW + DAY, ids["admin"], now=NOW)
    tickets.cancel_discount(conn, d["id"], ids["admin"], now=NOW + 10)
    assert tickets.price_now(conn, "lite", "week", NOW + 20)["usd"] == 8
    tickets.create_discount(conn, cfg, "lite", "week", 7, NOW, NOW + DAY, ids["admin"], now=NOW + 20)
    with pytest.raises(tickets.TicketError):
        tickets.cancel_discount(conn, d["id"], ids["admin"], now=NOW + 30)   # already cancelled
    assert [x["usd"] for x in tickets.discounts(conn, NOW + 20)] == [7]
    assert len(tickets.discounts(conn, NOW + 20, include_ended=True)) == 2


def test_regular_price_lowered_below_a_discount_charges_the_lower_with_no_strike_through(db):
    conn, cfg, ids = seeded(db)
    tickets.create_discount(conn, cfg, "lite", "week", 6, NOW, NOW + DAY, ids["admin"], now=NOW)
    tickets.set_price(conn, cfg, "lite", "week", 5, ids["admin"], now=NOW + 10)
    assert tickets.price_now(conn, "lite", "week", NOW + 20) == {"list_usd": 5, "usd": 5, "discount_id": None, "discount_ends_at": None}
    tickets.set_price(conn, cfg, "lite", "week", 6, ids["admin"], now=NOW + 30)   # equal: no discount shown either
    assert tickets.price_now(conn, "lite", "week", NOW + 40)["discount_id"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tickets_prices.py -q`
Expected: FAIL with `AttributeError: module 'claude_proxy.tickets' has no attribute 'round_local'`.

- [ ] **Step 3: Implement**

Add to `src/claude_proxy/tickets.py` (imports first):

```python
import math
from decimal import ROUND_HALF_UP, Decimal

from . import db
```

Then, after `ACCOUNT_ID`:

```python
STALE_RATE_S = 36 * 3600
USD_ROUND_TO = 0.01


class TicketError(Exception):
    """A refused ticket operation. The message is written for the admin who asked."""


class CapacityError(TicketError):
    """The period would push what is sold over max_sold_pct."""


def _date(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def _now(now) -> int:
    return int(time.time() if now is None else now)


def _positive(v, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise TicketError(f"{what} must be a number above 0.")
    return float(v)


# ---------- money (spec section 6) ----------

def round_local(usd: float, rate: float, round_to: float) -> float:
    """round(usd × rate, round_to), ties rounded up: $20 at 0.92 with a 0.50 step is €18.50, and so is 18.25."""
    step = Decimal(str(round_to))
    steps = (Decimal(str(usd)) * Decimal(str(rate)) / step).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return float(steps * step)


def currencies(cfg: Config) -> dict[str, float]:
    """Currency code -> rounding step. USD is always there, at an implicit rate of 1."""
    return {"USD": USD_ROUND_TO, **{c: cur.round_to for c, cur in cfg.tickets.currencies.items()}}


def current_rate(conn: sqlite3.Connection, currency: str) -> dict | None:
    """The newest rate for a currency (local units per 1 USD), USD's built-in one, or None when none was ever set."""
    if currency == "USD":
        return {"currency": "USD", "rate": 1.0, "set_at": None, "set_by": None}
    row = conn.execute("SELECT currency, rate, set_at, set_by FROM fx_rates WHERE currency=? ORDER BY set_at DESC, id DESC LIMIT 1",
                       (currency,)).fetchone()
    return dict(row) if row else None


def rate_is_stale(rate: dict, now: float) -> bool:
    return rate["set_at"] is not None and now - rate["set_at"] > STALE_RATE_S


def set_rate(conn: sqlite3.Connection, cfg: Config, currency: str, rate, actor: int | None, now: float | None = None) -> dict:
    if currency not in cfg.tickets.currencies:
        raise TicketError(f"{currency!r} is not a configured currency; add [tickets.currencies.{currency}] to the config first.")
    rate = _positive(rate, "The rate (local units per 1 USD)")
    conn.execute("INSERT INTO fx_rates(currency, rate, set_at, set_by) VALUES(?,?,?,?)", (currency, rate, _now(now), actor))
    db.audit(conn, actor, "rate_set", currency, {"rate": rate})
    return current_rate(conn, currency)


# ---------- prices and discounts ----------

def _check_tier_length(cfg: Config, tier: str, length: str) -> None:
    if tier not in cfg.tickets.tiers:
        raise TicketError(f"Unknown tier {tier!r}.")
    if length not in LENGTHS:
        raise TicketError(f"Unknown length {length!r}; one of {', '.join(LENGTHS)}.")


def regular_price(conn: sqlite3.Connection, tier: str, length: str) -> float:
    row = conn.execute("SELECT usd FROM ticket_prices WHERE tier=? AND length=?", (tier, length)).fetchone()
    if row is None:
        raise TicketError(f"No price for {tier} {length}; the gateway seeds prices at start.")
    return row[0]


def set_price(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, usd, actor: int | None, now: float | None = None) -> None:
    _check_tier_length(cfg, tier, length)
    usd = _positive(usd, "The price")
    conn.execute("INSERT OR REPLACE INTO ticket_prices(tier, length, usd, updated_at, updated_by) VALUES(?,?,?,?,?)",
                 (tier, length, usd, _now(now), actor))
    db.audit(conn, actor, "price_set", f"{tier}:{length}", {"usd": usd})


def prices(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_prices ORDER BY tier, length")]


def active_discount(conn: sqlite3.Connection, tier: str, length: str, now: float) -> dict | None:
    """The discount covering `now`: not cancelled and starts_at <= now < ends_at."""
    row = conn.execute("SELECT * FROM ticket_discounts WHERE tier=? AND length=? AND cancelled_at IS NULL AND starts_at<=? AND ends_at>? "
                       "ORDER BY id LIMIT 1", (tier, length, now, now)).fetchone()
    return dict(row) if row else None


def create_discount(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, usd, starts_at, ends_at, actor: int | None,
                    now: float | None = None) -> dict:
    now = _now(now)
    _check_tier_length(cfg, tier, length)
    usd = _positive(usd, "The discounted price")
    starts_at, ends_at = int(starts_at), int(ends_at)
    if starts_at >= ends_at:
        raise TicketError("A discount must start before it ends.")
    if ends_at <= now:
        raise TicketError("A discount must end in the future.")
    regular = regular_price(conn, tier, length)
    if usd >= regular:
        raise TicketError(f"A discount must be below the regular price (${regular:g}).")
    # Interval overlap against every non-cancelled discount of the tier and length, not a check at `now`.
    clash = conn.execute("SELECT id FROM ticket_discounts WHERE tier=? AND length=? AND cancelled_at IS NULL AND starts_at<? AND ends_at>? "
                         "ORDER BY id LIMIT 1", (tier, length, ends_at, starts_at)).fetchone()
    if clash:
        raise TicketError(f"Discount #{clash[0]} already covers part of that period; cancel it first.")
    cur = conn.execute("INSERT INTO ticket_discounts(tier, length, usd, starts_at, ends_at, created_by, created_at) VALUES(?,?,?,?,?,?,?)",
                       (tier, length, usd, starts_at, ends_at, actor, now))
    db.audit(conn, actor, "discount_create", f"{tier}:{length}", {"id": cur.lastrowid, "usd": usd, "starts_at": starts_at, "ends_at": ends_at})
    return dict(conn.execute("SELECT * FROM ticket_discounts WHERE id=?", (cur.lastrowid,)).fetchone())


def cancel_discount(conn: sqlite3.Connection, discount_id: int, actor: int | None, now: float | None = None) -> None:
    row = conn.execute("SELECT tier, length FROM ticket_discounts WHERE id=? AND cancelled_at IS NULL", (discount_id,)).fetchone()
    if row is None:
        raise TicketError("No such active discount.")
    conn.execute("UPDATE ticket_discounts SET cancelled_at=? WHERE id=?", (_now(now), discount_id))
    db.audit(conn, actor, "discount_cancel", f"{row['tier']}:{row['length']}", {"id": discount_id})


def discounts(conn: sqlite3.Connection, now: float, include_ended: bool = False) -> list[dict]:
    """Non-cancelled discounts, current and future; with `include_ended`, everything, newest first."""
    if include_ended:
        return [dict(r) for r in conn.execute("SELECT * FROM ticket_discounts ORDER BY starts_at DESC, id DESC")]
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_discounts WHERE cancelled_at IS NULL AND ends_at>? ORDER BY starts_at, id", (now,))]


def price_now(conn: sqlite3.Connection, tier: str, length: str, now: float) -> dict:
    """The regular price, what is charged now, and the discount when it is the lower one. A regular price since
    lowered to or below an active discount is charged as is, and no strike-through is shown."""
    list_usd = regular_price(conn, tier, length)
    d = active_discount(conn, tier, length, now)
    if d and d["usd"] < list_usd:
        return {"list_usd": list_usd, "usd": d["usd"], "discount_id": d["id"], "discount_ends_at": d["ends_at"]}
    return {"list_usd": list_usd, "usd": list_usd, "discount_id": None, "discount_ends_at": None}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tickets_prices.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/tickets.py tests/tickets_helpers.py tests/test_tickets_prices.py
git commit -m "Tickets: exchange rates, prices, discounts and half-up local rounding

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Capacity and granting

**Files:**
- Modify: `src/claude_proxy/tickets.py`
- Test: `tests/test_tickets_capacity.py`

**Interfaces:**
- Produces, in `tickets.py`:
  - `TICKET_COLS` (SQL select list with `effective_end`), `sold_at(conn, account_id, t, exclude=None) -> float`, `check_capacity(conn, cfg, account_id, share, starts_at, ends_at, exclude=None) -> None` (raises `CapacityError`).
  - `get(conn, ticket_id) -> dict` (raises `TicketError`), `user_tickets(conn, user_id) -> list[dict]`, `last_end(conn, user_id) -> int | None`, `covering(conn, user_id, now) -> dict | None`, `next_queued(conn, user_id, now) -> dict | None`, `last_ended(conn, user_id, now) -> dict | None` (adds key `ended_at`), `is_gated(conn, user_id) -> bool`, `current_day(ticket: dict, now) -> tuple[int, int]`, `bonus_share(conn, ticket_id, now) -> float`, `bonuses(conn, ticket_id) -> list[dict]`, `active_bonuses(conn, ticket_id, now) -> list[dict]`.
  - `REMOVABLE` SQL fragment; `preview(conn, cfg, user, tier, length, currency, now) -> dict`; `grant(conn, cfg, actor, user, tier, length, currency, note="", remove_limits=(), confirm_stale_rate=False, now=None) -> dict`.
  - `capacity(conn, cfg, now) -> dict` with `sold_now_pct, peak_30d_pct, max_sold_pct`; `sold_out(conn, cfg, tier, length, now) -> bool`.
- Every ticket dict carries the table columns plus `effective_end`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tickets_capacity.py
import sqlite3
import threading

import pytest

from claude_proxy import db as dbm
from claude_proxy import tickets
from tests.tickets_helpers import DAY, NOW, seeded, user


def grant(conn, cfg, ids, who="alice", tier="lite", length="week", now=NOW, **kw):
    return tickets.grant(conn, cfg, ids["admin"], user(conn, ids[who]), tier, length, kw.pop("currency", "EUR"), now=now, **kw)


def add_user(conn, ids, name):
    ids[name], _ = dbm.create_user(conn, name)
    return ids[name]


def test_grant_records_prices_rate_and_period(db):
    conn, cfg, ids = seeded(db)
    t = grant(conn, cfg, ids)
    assert (t["tier"], t["share_pct"], t["length"], t["days"]) == ("lite", 5, "week", 7)
    assert (t["starts_at"], t["ends_at"], t["effective_end"]) == (NOW, NOW + 7 * DAY, NOW + 7 * DAY)
    assert (t["list_usd"], t["usd"], t["discount_id"], t["currency"], t["rate"], t["amount"]) == (8, 8, None, "EUR", 0.92, 7.5)
    assert (t["user_id"], t["user_name"], t["granted_by"], t["account_id"]) == (ids["alice"], "alice", ids["admin"], 1)
    row = conn.execute("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == ("ticket_grant", "alice")
    assert tickets.is_gated(conn, ids["alice"]) and not tickets.is_gated(conn, ids["admin"])


def test_grant_charges_the_discount_and_keeps_it_fixed(db):
    conn, cfg, ids = seeded(db)
    d = tickets.create_discount(conn, cfg, "lite", "week", 6, NOW, NOW + DAY, ids["admin"], now=NOW)
    t = grant(conn, cfg, ids, currency="USD")
    assert (t["list_usd"], t["usd"], t["discount_id"], t["amount"]) == (8, 6, d["id"], 6)
    tickets.set_price(conn, cfg, "lite", "week", 50, ids["admin"], now=NOW)
    tickets.set_rate(conn, cfg, "EUR", 2.0, ids["admin"], now=NOW)
    assert tickets.get(conn, t["id"])["usd"] == 6


def test_grant_needs_a_rate_and_confirms_a_stale_one(db):
    conn, cfg, ids = seeded(db)
    conn.execute("DELETE FROM fx_rates")
    with pytest.raises(tickets.TicketError, match="No exchange rate for EUR"):
        grant(conn, cfg, ids)
    tickets.set_rate(conn, cfg, "EUR", 0.9, ids["admin"], now=NOW - 40 * 3600)
    with pytest.raises(tickets.TicketError, match="40 hours old"):
        grant(conn, cfg, ids)
    assert grant(conn, cfg, ids, confirm_stale_rate=True)["rate"] == 0.9
    with pytest.raises(tickets.TicketError, match="Unknown currency"):
        grant(conn, cfg, ids, currency="GBP")


def test_grant_refused_for_revoked_disabled_or_when_disabled_feature(db):
    conn, cfg, ids = seeded(db)
    dbm.set_enabled(conn, ids["alice"], False)
    with pytest.raises(tickets.TicketError, match="disabled"):
        grant(conn, cfg, ids)
    dbm.set_enabled(conn, ids["alice"], True)
    dbm.revoke(conn, ids["alice"])
    with pytest.raises(tickets.TicketError, match="revoked"):
        grant(conn, cfg, ids)
    cfg.tickets.enabled = False
    with pytest.raises(tickets.TicketError, match="not enabled"):
        tickets.grant(conn, cfg, ids["admin"], user(conn, ids["admin"]), "lite", "day", "USD", now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_first_grant_removes_the_credit_and_ticked_rows_with_audit(db):
    conn, cfg, ids = seeded(db)
    for kind, scope, value, unit in [("cost_total", "*", "5", "usd"), ("tokens_daily", "*", "1000", "weighted"),
                                     ("requests_minute", "*", "20", "count"), ("share_7d", "*", "10", "pct"), ("allowed_models", "*", "claude-*", "list")]:
        conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (ids["alice"], kind, scope, value, unit))
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "lite", "week", "EUR", NOW)
    assert [(r["kind"], r["scope"]) for r in p["limit_rows"]] == [("allowed_models", "*"), ("requests_minute", "*"), ("tokens_daily", "*")]
    assert p["credit"] is True and p["first_ticket"] is True
    grant(conn, cfg, ids, remove_limits=[("tokens_daily", "*"), ("share_7d", "*")])   # share rows are not removable: ignored
    kinds = {r[0] for r in conn.execute("SELECT kind FROM limits WHERE user_id=?", (ids["alice"],))}
    assert kinds == {"requests_minute", "share_7d", "allowed_models"}
    actions = [tuple(r) for r in conn.execute("SELECT action, target FROM audit_log WHERE action IN ('credit_removed','limit_clear','ticket_grant') ORDER BY id")]
    assert actions == [("credit_removed", "alice"), ("limit_clear", "alice:tokens_daily:*"), ("ticket_grant", "alice")]


def test_second_ticket_queues_after_the_first_and_a_returning_user_starts_now(db):
    conn, cfg, ids = seeded(db)
    first = grant(conn, cfg, ids, length="day")
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "lite", "day", "EUR", NOW + 100)
    assert (p["starts_at"], p["queued"]) == (first["ends_at"], True)
    second = grant(conn, cfg, ids, length="day", now=NOW + 100)
    assert (second["starts_at"], second["ends_at"]) == (NOW + DAY, NOW + 2 * DAY)
    assert tickets.covering(conn, ids["alice"], NOW + 100)["id"] == first["id"]
    assert tickets.next_queued(conn, ids["alice"], NOW + 100)["id"] == second["id"]
    assert tickets.covering(conn, ids["alice"], NOW + DAY)["id"] == second["id"]   # half-open handover at the shared instant
    # Long after both ended, a new ticket starts now, not at the old end.
    third = grant(conn, cfg, ids, length="day", now=NOW + 30 * DAY)
    assert third["starts_at"] == NOW + 30 * DAY
    assert tickets.last_ended(conn, ids["alice"], NOW + 29 * DAY)["ended_at"] == NOW + 2 * DAY


def test_sold_at_counts_tickets_and_bonus_shares_over_half_open_periods(db):
    conn, cfg, ids = seeded(db)
    bob = add_user(conn, ids, "bob")
    a = grant(conn, cfg, ids, tier="standard", length="day")                       # 25%, [NOW, NOW+1d)
    grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW + DAY)  # 25%, [NOW+1d, NOW+2d)
    conn.execute("INSERT INTO ticket_bonuses(ticket_id, share_pct, extra_days, starts_at, ends_at, granted_at) VALUES(?,?,?,?,?,?)",
                 (a["id"], 10, 0, NOW + 3600, NOW + 7200, NOW))
    assert tickets.sold_at(conn, 1, NOW) == 25
    assert tickets.sold_at(conn, 1, NOW + 3600) == 35
    assert tickets.sold_at(conn, 1, NOW + 7200) == 25             # bonus over at its end
    assert tickets.sold_at(conn, 1, NOW + DAY) == 25              # a's end and bob's start touch: never 50
    assert tickets.sold_at(conn, 1, NOW + DAY, exclude=a["id"]) == 25
    assert tickets.sold_at(conn, 1, NOW - 1) == 0


def test_capacity_check_samples_every_start_inside_the_period(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 50
    bob, carol = add_user(conn, ids, "bob"), add_user(conn, ids, "carol")
    grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW + 3 * DAY)     # 25% on day 3 only
    tickets.check_capacity(conn, cfg, 1, 25, NOW, NOW + 7 * DAY)                             # day 3 would be 50: fits
    grant(conn, cfg, ids, tier="standard", length="week")                                    # alice: day 3 is now 50
    assert tickets.sold_out(conn, cfg, "standard", "week", NOW) is True                      # fine at the start, but day 3 would be 75
    assert tickets.sold_out(conn, cfg, "lite", "week", NOW) is True                          # day 3 would be 55
    assert tickets.sold_out(conn, cfg, "lite", "day", NOW) is False                          # over before day 3: 25 + 5
    with pytest.raises(tickets.CapacityError, match="50% is sold"):
        grant(conn, cfg, ids, who="carol", tier="standard", length="week")
    assert tickets.sold_out(conn, cfg, "lite", "day", NOW + 8 * DAY) is False


def test_max_sold_pct_refuses_what_100_would_allow_and_lowering_it_keeps_existing_tickets(db):
    conn, cfg, ids = seeded(db)
    for n in range(3):
        add_user(conn, ids, f"u{n}")
        grant(conn, cfg, ids, who=f"u{n}", tier="standard", length="week")                   # 3 × 25 = 75%
    with pytest.raises(tickets.CapacityError):                                               # 75 + 25 = 100 > 80
        grant(conn, cfg, ids, tier="standard", length="week")
    assert grant(conn, cfg, ids, tier="lite", length="week")["share_pct"] == 5             # 80: exactly the ceiling fits
    cfg.tickets.max_sold_pct = 50
    bob = add_user(conn, ids, "bob")
    with pytest.raises(tickets.CapacityError):
        grant(conn, cfg, ids, who="bob", tier="lite", length="day")
    assert conn.execute("SELECT COUNT(*) FROM tickets WHERE cancelled_at IS NULL").fetchone()[0] == 4   # nothing cancelled
    assert tickets.capacity(conn, cfg, NOW) == {"sold_now_pct": 80, "peak_30d_pct": 80, "max_sold_pct": 50}


def test_capacity_panel_reports_sold_now_and_the_peak_over_30_days(db):
    conn, cfg, ids = seeded(db)
    bob = add_user(conn, ids, "bob")
    grant(conn, cfg, ids, tier="lite", length="month")
    grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW + 10 * DAY)
    assert tickets.capacity(conn, cfg, NOW) == {"sold_now_pct": 5, "peak_30d_pct": 30, "max_sold_pct": 80}
    assert tickets.capacity(conn, cfg, NOW + 31 * DAY)["sold_now_pct"] == 0   # expired tickets stop counting by themselves


def test_two_concurrent_grants_cannot_oversell(db):
    path, conn = db
    _, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 30
    bob = add_user(conn, ids, "bob")
    conns = [dbm.get_conn(path) for _ in range(2)]
    start = threading.Barrier(2)
    results = []

    def go(c, who):
        start.wait()
        try:
            tickets.grant(c, cfg, ids["admin"], user(c, ids[who]), "standard", "week", "USD", now=NOW)
            results.append("ok")
        except tickets.CapacityError:
            results.append("full")

    threads = [threading.Thread(target=go, args=(c, who)) for c, who in zip(conns, ("alice", "bob"))]
    for t in threads: t.start()
    for t in threads: t.join()
    for c in conns: c.close()
    assert sorted(results) == ["full", "ok"]
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


def test_current_day_steps_from_the_ticket_start(db):
    t = {"starts_at": NOW, "ends_at": NOW + 7 * DAY, "effective_end": NOW + 7 * DAY}
    assert tickets.current_day(t, NOW) == (NOW, NOW + DAY)
    assert tickets.current_day(t, NOW + DAY - 1) == (NOW, NOW + DAY)
    assert tickets.current_day(t, NOW + DAY) == (NOW + DAY, NOW + 2 * DAY)
    assert tickets.current_day(t, NOW + 6 * DAY + 5) == (NOW + 6 * DAY, NOW + 7 * DAY)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tickets_capacity.py -q`
Expected: FAIL with `AttributeError: module 'claude_proxy.tickets' has no attribute 'grant'`.

- [ ] **Step 3: Implement capacity, queries and grant**

Append to `src/claude_proxy/tickets.py`:

```python
# ---------- capacity (spec section 5) ----------

TICKET_COLS = f"t.*, {db.TICKET_EFFECTIVE_END} AS effective_end"


def _exclude(exclude) -> tuple[str, list]:
    return (" AND t.id!=?", [exclude]) if exclude is not None else ("", [])


def sold_at(conn: sqlite3.Connection, account_id: int, t: float, exclude: int | None = None) -> float:
    """sold(t): shares of the non-cancelled tickets and bonus shares covering `t` (half-open periods).
    `exclude` leaves one ticket and its bonuses out, for re-checking that ticket at a new place."""
    ex, args = _exclude(exclude)
    a = conn.execute(f"SELECT COALESCE(SUM(t.share_pct), 0) FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL "
                     f"AND t.starts_at<=? AND {db.TICKET_EFFECTIVE_END}>?{ex}", (account_id, t, t, *args)).fetchone()[0]
    b = conn.execute(f"SELECT COALESCE(SUM(b.share_pct), 0) FROM ticket_bonuses b JOIN tickets t ON t.id=b.ticket_id "
                     f"WHERE t.account_id=? AND t.cancelled_at IS NULL AND b.cancelled_at IS NULL AND b.starts_at<=? AND b.ends_at>?{ex}",
                     (account_id, t, t, *args)).fetchone()[0]
    return a + b


def _starts_within(conn, account_id: int, starts_at: int, ends_at: int, exclude: int | None = None) -> set[int]:
    """Every ticket or bonus start strictly inside (starts_at, ends_at): the only places sold(t) can rise."""
    ex, args = _exclude(exclude)
    pts = {r[0] for r in conn.execute(f"SELECT t.starts_at FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL "
                                      f"AND t.starts_at>? AND t.starts_at<?{ex}", (account_id, starts_at, ends_at, *args))}
    pts |= {r[0] for r in conn.execute(f"SELECT b.starts_at FROM ticket_bonuses b JOIN tickets t ON t.id=b.ticket_id WHERE t.account_id=? "
                                       f"AND t.cancelled_at IS NULL AND b.cancelled_at IS NULL AND b.share_pct>0 AND b.starts_at>? AND b.starts_at<?{ex}",
                                       (account_id, starts_at, ends_at, *args))}
    return pts


def check_capacity(conn: sqlite3.Connection, cfg: Config, account_id: int, share: float, starts_at: int, ends_at: int,
                   exclude: int | None = None) -> None:
    """sold(t) + share <= max_sold_pct at the period's start and at every start inside it, else CapacityError."""
    ceiling = cfg.tickets.max_sold_pct
    for point in sorted({starts_at, *_starts_within(conn, account_id, starts_at, ends_at, exclude)}):
        sold = sold_at(conn, account_id, point, exclude)
        if sold + share > ceiling + 1e-9:
            raise CapacityError(f"Not enough capacity: {sold:g}% is sold at {_date(point)}, and {share:g}% more would pass the "
                                f"{ceiling:g}% ceiling (max_sold_pct).")


# ---------- tickets ----------

def _row(conn, sql: str, args: tuple) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def get(conn: sqlite3.Connection, ticket_id: int) -> dict:
    t = _row(conn, f"SELECT {TICKET_COLS} FROM tickets t WHERE t.id=?", (ticket_id,))
    if t is None:
        raise TicketError(f"No ticket #{ticket_id}.")
    return t


def user_tickets(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? ORDER BY t.starts_at, t.id", (user_id,))]


def last_end(conn: sqlite3.Connection, user_id: int) -> int | None:
    return conn.execute(f"SELECT MAX({db.TICKET_EFFECTIVE_END}) FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL", (user_id,)).fetchone()[0]


def covering(conn: sqlite3.Connection, user_id: int, now: float) -> dict | None:
    """The user's ticket covering now (starts_at <= now < effective end), or None."""
    return _row(conn, f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL AND t.starts_at<=? "
                      f"AND {db.TICKET_EFFECTIVE_END}>? ORDER BY t.starts_at LIMIT 1", (user_id, now, now))


def next_queued(conn: sqlite3.Connection, user_id: int, now: float) -> dict | None:
    return _row(conn, f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL AND t.starts_at>? "
                      f"ORDER BY t.starts_at LIMIT 1", (user_id, now))


def last_ended(conn: sqlite3.Connection, user_id: int, now: float) -> dict | None:
    """The user's most recently ended ticket, with `ended_at`: its effective end, or when it was cancelled if earlier."""
    best = None
    for t in user_tickets(conn, user_id):
        ended = min(t["effective_end"], t["cancelled_at"]) if t["cancelled_at"] is not None else t["effective_end"]
        if ended <= now and (best is None or ended > best["ended_at"]):
            best = {**t, "ended_at": ended}
    return best


def is_gated(conn: sqlite3.Connection, user_id: int) -> bool:
    """Ticket-gated: at least one ticket row not marked ungated_at, cancelled or not (spec section 7)."""
    return conn.execute("SELECT 1 FROM tickets WHERE user_id=? AND ungated_at IS NULL LIMIT 1", (user_id,)).fetchone() is not None


def current_day(ticket: dict, now: float) -> tuple[int, int]:
    """The ticket day containing now: whole 24-hour steps from starts_at, bonus days included."""
    n = int((now - ticket["starts_at"]) // DAY)
    start = ticket["starts_at"] + n * DAY
    return start, start + DAY


def bonuses(conn: sqlite3.Connection, ticket_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_bonuses WHERE ticket_id=? ORDER BY starts_at, id", (ticket_id,))]


def active_bonuses(conn: sqlite3.Connection, ticket_id: int, now: float) -> list[dict]:
    """Non-cancelled bonuses whose share period covers now."""
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_bonuses WHERE ticket_id=? AND cancelled_at IS NULL AND share_pct>0 "
                                          "AND starts_at<=? AND ends_at>? ORDER BY id", (ticket_id, now, now))]


def bonus_share(conn: sqlite3.Connection, ticket_id: int, now: float) -> float:
    return sum(b["share_pct"] for b in active_bonuses(conn, ticket_id, now))


# ---------- granting ----------

# Limit rows the grant form offers to remove. Share rows are replaced by the ticket while it gates the user and come
# back if the user is ever ungated; the sign-up credit goes with the first ticket anyway.
REMOVABLE = "kind NOT IN ('share_5h', 'share_7d', 'cost_total')"


def preview(conn: sqlite3.Connection, cfg: Config, user, tier: str, length: str, currency: str, now: float) -> dict:
    """Everything the grant form shows before the admin confirms. Raises TicketError for a bad tier, length or currency
    and when the currency has no rate; capacity trouble is reported in `available`, not raised."""
    now = _now(now)
    _check_tier_length(cfg, tier, length)
    steps = currencies(cfg)
    if currency not in steps:
        raise TicketError(f"Unknown currency {currency!r}.")
    rate = current_rate(conn, currency)
    if rate is None:
        raise TicketError(f"No exchange rate for {currency}; set one on the Rates panel first.")
    p = price_now(conn, tier, length, now)
    share = cfg.tickets.tiers[tier].share_pct
    starts_at = max(now, last_end(conn, user["id"]) or 0)
    ends_at = starts_at + LENGTHS[length] * DAY
    try:
        check_capacity(conn, cfg, ACCOUNT_ID, share, starts_at, ends_at)
        available, reason = True, None
    except CapacityError as e:
        available, reason = False, str(e)
    rows = [dict(r) for r in conn.execute(f"SELECT kind, scope, value, unit FROM limits WHERE user_id=? AND {REMOVABLE} ORDER BY kind, scope",
                                          (user["id"],))]
    return {"tier": tier, "label": cfg.tickets.tiers[tier].label, "length": length, "days": LENGTHS[length], "share_pct": share, **p,
            "currency": currency, "rate": rate["rate"], "rate_set_at": rate["set_at"], "stale_rate": rate_is_stale(rate, now),
            "amount": round_local(p["usd"], rate["rate"], steps[currency]),
            "starts_at": starts_at, "ends_at": ends_at, "queued": starts_at > now, "available": available, "reason": reason,
            "sold_out_now": sold_out(conn, cfg, tier, length, now) if starts_at > now else not available,
            "limit_rows": rows,
            "credit": conn.execute("SELECT 1 FROM limits WHERE user_id=? AND kind='cost_total'", (user["id"],)).fetchone() is not None,
            "first_ticket": conn.execute("SELECT 1 FROM tickets WHERE user_id=? LIMIT 1", (user["id"],)).fetchone() is None}


def grant(conn: sqlite3.Connection, cfg: Config, actor: int | None, user, tier: str, length: str, currency: str, note: str = "",
          remove_limits=(), confirm_stale_rate: bool = False, now: float | None = None) -> dict:
    """Sell a ticket. One BEGIN IMMEDIATE transaction: the price, the start (now, or after the user's last ticket), the
    capacity check, the insert, the credit removal and any ticked limit rows, each audited."""
    now = _now(now)
    if not cfg.tickets.enabled:
        raise TicketError("Tickets are not enabled: add a [tickets] section to the config.")
    if user["revoked_at"] is not None:
        raise TicketError(f"{user['name']} is revoked.")
    if not user["enabled"]:
        raise TicketError(f"{user['name']} is disabled; enable them first.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        p = preview(conn, cfg, user, tier, length, currency, now)
        if p["stale_rate"] and not confirm_stale_rate:
            raise TicketError(f"The {currency} rate is {int((now - p['rate_set_at']) // 3600)} hours old. Confirm to grant at it "
                              "anyway, or set today's rate first.")
        check_capacity(conn, cfg, ACCOUNT_ID, p["share_pct"], p["starts_at"], p["ends_at"])
        cur = conn.execute(
            "INSERT INTO tickets(user_id, user_name, account_id, tier, share_pct, length, days, starts_at, ends_at, list_usd, usd, "
            "discount_id, currency, rate, amount, granted_by, granted_at, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user["id"], user["name"], ACCOUNT_ID, tier, p["share_pct"], length, p["days"], p["starts_at"], p["ends_at"], p["list_usd"],
             p["usd"], p["discount_id"], currency, p["rate"], p["amount"], actor, now, (note or "").strip() or None))
        tid = cur.lastrowid
        if conn.execute("DELETE FROM limits WHERE user_id=? AND kind='cost_total'", (user["id"],)).rowcount:
            db.audit(conn, actor, "credit_removed", user["name"], {"ticket_id": tid})
        for kind, scope in remove_limits:
            if conn.execute(f"DELETE FROM limits WHERE user_id=? AND kind=? AND scope=? AND {REMOVABLE}", (user["id"], kind, scope)).rowcount:
                db.audit(conn, actor, "limit_clear", f"{user['name']}:{kind}:{scope}", {"ticket_id": tid})
        db.audit(conn, actor, "ticket_grant", user["name"], {"ticket_id": tid, "tier": tier, "length": length, "usd": p["usd"],
                                                            "list_usd": p["list_usd"], "currency": currency, "amount": p["amount"],
                                                            "starts_at": p["starts_at"], "ends_at": p["ends_at"]})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get(conn, tid)


# ---------- what the panels and /pricing ask ----------

def capacity(conn: sqlite3.Connection, cfg: Config, now: float) -> dict:
    """Share sold now and the highest sold(t) over the next 30 days, against max_sold_pct."""
    now = _now(now)
    points = {now, *_starts_within(conn, ACCOUNT_ID, now, now + 30 * DAY)}
    return {"sold_now_pct": sold_at(conn, ACCOUNT_ID, now), "peak_30d_pct": max(sold_at(conn, ACCOUNT_ID, p) for p in points),
            "max_sold_pct": cfg.tickets.max_sold_pct}


def sold_out(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, now: float) -> bool:
    """A tier and length is sold out when a ticket starting now would fail the capacity rule."""
    now = _now(now)
    try:
        check_capacity(conn, cfg, ACCOUNT_ID, cfg.tickets.tiers[tier].share_pct, now, now + LENGTHS[length] * DAY)
    except CapacityError:
        return True
    return False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tickets_capacity.py -q`
Expected: all PASS. If the threaded test hangs, the second connection is missing `busy_timeout`; `db.get_conn` sets it to 5000 ms, so check the test used `dbm.get_conn`.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/tickets.py tests/test_tickets_capacity.py
git commit -m "Tickets: capacity reserved in time, grants in one transaction

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Cancel, bonus, queue moves and Ungate

**Files:**
- Modify: `src/claude_proxy/tickets.py`
- Test: `tests/test_tickets_capacity.py` (append)

**Interfaces:**
- Produces: `cancel(conn, cfg, actor, ticket_id, now=None) -> dict` with keys `ticket, moved, dates_kept, reason`; `add_bonus(conn, cfg, actor, ticket_id, share_pct=0, extra_days=0, starts_at=None, ends_at=None, note="", now=None) -> dict` with keys `bonus, ticket, moved`; `ungate(conn, actor, user_id, now=None) -> int`; `user_state(conn, user_id, now) -> dict` with keys `gated, current, queued, live`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tickets_capacity.py`)

```python
def bonus(conn, cfg, ids, tid, **kw):
    return tickets.add_bonus(conn, cfg, ids["admin"], tid, now=kw.pop("now", NOW), **kw)


def test_cancel_frees_the_slice_and_ends_bonuses(db):
    conn, cfg, ids = seeded(db)
    t = grant(conn, cfg, ids, tier="standard", length="week")
    bonus(conn, cfg, ids, t["id"], share_pct=5, starts_at=NOW, ends_at=NOW + DAY, note="welcome")
    assert tickets.sold_at(conn, 1, NOW + 10) == 30
    r = tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW + 100)
    assert r["ticket"]["cancelled_at"] == NOW + 100 and r["ticket"]["cancelled_by"] == ids["admin"]
    assert tickets.sold_at(conn, 1, NOW + 200) == 0
    assert tickets.bonuses(conn, t["id"])[0]["cancelled_at"] == NOW + 100
    assert tickets.covering(conn, ids["alice"], NOW + 200) is None
    assert tickets.is_gated(conn, ids["alice"])                      # a cancelled ticket still gates
    with pytest.raises(tickets.TicketError, match="already cancelled"):
        tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW + 300)
    assert conn.execute("SELECT action FROM audit_log WHERE action='ticket_cancel'").fetchone() is not None


def test_cancel_moves_queued_tickets_forward_to_close_the_gap(db):
    conn, cfg, ids = seeded(db)
    a = grant(conn, cfg, ids, length="week")                                  # [0, 7d)
    b = grant(conn, cfg, ids, length="week", now=NOW + 10)                    # [7d, 14d)
    c = grant(conn, cfg, ids, length="day", now=NOW + 20)                     # [14d, 15d)
    bonus(conn, cfg, ids, b["id"], share_pct=1, starts_at=NOW + 8 * DAY, ends_at=NOW + 9 * DAY)
    r = tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW + DAY)       # active: the next one starts now
    assert (r["moved"], r["dates_kept"]) == (2, False)
    b2, c2 = tickets.get(conn, b["id"]), tickets.get(conn, c["id"])
    assert (b2["starts_at"], b2["ends_at"]) == (NOW + DAY, NOW + 8 * DAY)
    assert (c2["starts_at"], c2["ends_at"]) == (NOW + 8 * DAY, NOW + 9 * DAY)
    bb = tickets.bonuses(conn, b["id"])[0]
    assert (bb["starts_at"], bb["ends_at"]) == (NOW + 2 * DAY, NOW + 3 * DAY)   # bonus period moved with its ticket
    assert tickets.covering(conn, ids["alice"], NOW + DAY)["id"] == b["id"]


def test_cancelling_a_queued_ticket_moves_the_rest_to_its_start(db):
    conn, cfg, ids = seeded(db)
    grant(conn, cfg, ids, length="day")                                        # [0, 1d)
    b = grant(conn, cfg, ids, length="day", now=NOW + 10)                      # [1d, 2d)
    c = grant(conn, cfg, ids, length="day", now=NOW + 20)                      # [2d, 3d)
    tickets.cancel(conn, cfg, ids["admin"], b["id"], now=NOW + 100)
    assert tickets.get(conn, c["id"])["starts_at"] == NOW + DAY


def test_cancel_never_refused_dates_kept_when_a_move_does_not_fit(db):
    conn, cfg, ids = seeded(db)
    bob, carol = add_user(conn, ids, "bob"), add_user(conn, ids, "carol")
    a = grant(conn, cfg, ids, tier="standard", length="day")                   # alice 25% day 0
    b = grant(conn, cfg, ids, tier="standard", length="day", now=NOW + 10)     # alice 25% day 1
    grant(conn, cfg, ids, who="bob", tier="standard", length="day")            # bob 25% day 0
    grant(conn, cfg, ids, who="carol", tier="standard", length="day")          # carol 25% day 0: day 0 = 75 of 80
    cfg.tickets.max_sold_pct = 50                                              # the admin lowers the ceiling: day 0 is now over it
    r = tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW + 100)        # b cannot move onto day 0 (50 + 25 > 50)
    assert (r["moved"], r["dates_kept"]) == (0, True) and "Not enough capacity" in r["reason"]
    assert tickets.get(conn, b["id"])["starts_at"] == NOW + DAY                # b kept its date
    assert tickets.get(conn, a["id"])["cancelled_at"] == NOW + 100             # but a is cancelled all the same


def test_bonus_validation(db):
    conn, cfg, ids = seeded(db)
    t = grant(conn, cfg, ids)
    for kw in ({}, {"share_pct": 0, "extra_days": 0}, {"share_pct": -1}, {"extra_days": -1}):
        with pytest.raises(tickets.TicketError):
            bonus(conn, cfg, ids, t["id"], **kw)
    with pytest.raises(tickets.TicketError, match="within the ticket"):
        bonus(conn, cfg, ids, t["id"], share_pct=1, starts_at=NOW + 8 * DAY, ends_at=NOW + 9 * DAY)   # after the ticket
    tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW)
    with pytest.raises(tickets.TicketError, match="cancelled"):
        bonus(conn, cfg, ids, t["id"], extra_days=1)


def test_bonus_share_is_clamped_checked_and_counted(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 30
    t = grant(conn, cfg, ids, tier="standard", length="week")                  # 25%
    r = bonus(conn, cfg, ids, t["id"], share_pct=5, starts_at=NOW - DAY, ends_at=NOW + 30 * DAY, note="thanks")
    assert (r["bonus"]["starts_at"], r["bonus"]["ends_at"], r["bonus"]["note"]) == (NOW, NOW + 7 * DAY, "thanks")
    assert tickets.bonus_share(conn, t["id"], NOW + DAY) == 5 and tickets.bonus_share(conn, t["id"], NOW + 7 * DAY) == 0
    with pytest.raises(tickets.CapacityError):
        bonus(conn, cfg, ids, t["id"], share_pct=1)                           # 25 + 5 + 1 > 30
    assert conn.execute("SELECT action, target FROM audit_log WHERE action='ticket_bonus'").fetchone()[:] == ("ticket_bonus", "alice")


def test_bonus_days_extend_at_the_ticket_share_and_push_queued_tickets(db):
    conn, cfg, ids = seeded(db)
    a = grant(conn, cfg, ids, length="week")                                   # [0, 7d)
    b = grant(conn, cfg, ids, length="day", now=NOW + 10)                      # [7d, 8d)
    c = grant(conn, cfg, ids, length="day", now=NOW + 20)                      # [8d, 9d)
    r = bonus(conn, cfg, ids, a["id"], extra_days=2, note="sorry for the outage")
    assert r["moved"] == 2 and r["ticket"]["effective_end"] == NOW + 9 * DAY and r["ticket"]["ends_at"] == NOW + 7 * DAY
    assert (r["bonus"]["share_pct"], r["bonus"]["extra_days"], r["bonus"]["starts_at"], r["bonus"]["ends_at"]) == (0, 2, NOW + 7 * DAY, NOW + 9 * DAY)
    assert tickets.get(conn, b["id"])["starts_at"] == NOW + 9 * DAY
    assert tickets.get(conn, c["id"])["starts_at"] == NOW + 10 * DAY
    assert tickets.sold_at(conn, 1, NOW + 8 * DAY) == 5                        # the extension counts at the ticket share
    assert tickets.covering(conn, ids["alice"], NOW + 8 * DAY)["id"] == a["id"]
    assert tickets.current_day(tickets.get(conn, a["id"]), NOW + 8 * DAY + 5) == (NOW + 8 * DAY, NOW + 9 * DAY)   # same boundaries


def test_bonus_days_refused_when_a_moved_ticket_does_not_fit(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 50
    bob, carol = add_user(conn, ids, "bob"), add_user(conn, ids, "carol")
    a = grant(conn, cfg, ids, tier="standard", length="day")                   # alice day 0
    b = grant(conn, cfg, ids, tier="standard", length="day", now=NOW + 10)     # alice day 1
    grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW + 2 * DAY)     # bob day 2
    grant(conn, cfg, ids, who="carol", tier="standard", length="day", now=NOW + 2 * DAY)   # carol day 2: day 2 = 50
    # An extra day on a extends it over day 1, so b must move onto day 2, which is full.
    with pytest.raises(tickets.CapacityError, match="queued ticket"):
        bonus(conn, cfg, ids, a["id"], extra_days=1)
    assert tickets.get(conn, a["id"])["effective_end"] == NOW + DAY           # nothing applied
    assert tickets.get(conn, b["id"])["starts_at"] == NOW + DAY
    assert conn.execute("SELECT COUNT(*) FROM ticket_bonuses").fetchone()[0] == 0


def test_bonus_days_on_an_ended_ticket_make_it_cover_now_with_the_old_day_boundaries(db):
    conn, cfg, ids = seeded(db)
    a = grant(conn, cfg, ids, length="day")                                    # [0, 1d)
    later = NOW + DAY + 3600                                                   # an hour after it ended
    assert tickets.covering(conn, ids["alice"], later) is None
    bonus(conn, cfg, ids, a["id"], extra_days=1, now=later)
    t = tickets.covering(conn, ids["alice"], later)
    assert t["id"] == a["id"] and t["effective_end"] == NOW + 2 * DAY
    assert tickets.current_day(t, later) == (NOW + DAY, NOW + 2 * DAY)


def test_ungate_only_without_live_tickets(db):
    conn, cfg, ids = seeded(db)
    a = grant(conn, cfg, ids, length="day")
    with pytest.raises(tickets.TicketError, match="active or queued"):
        tickets.ungate(conn, ids["admin"], ids["alice"], now=NOW + 100)
    tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW + 100)
    b = grant(conn, cfg, ids, length="day", now=NOW + 200)
    conn.execute("UPDATE tickets SET starts_at=?, ends_at=? WHERE id=?", (NOW + 5 * DAY, NOW + 6 * DAY, b["id"]))   # queued
    with pytest.raises(tickets.TicketError, match="active or queued"):
        tickets.ungate(conn, ids["admin"], ids["alice"], now=NOW + 300)
    assert tickets.ungate(conn, ids["admin"], ids["alice"], now=NOW + 7 * DAY) == 2
    assert tickets.is_gated(conn, ids["alice"]) is False
    assert conn.execute("SELECT COUNT(*) FROM tickets WHERE user_id=?", (ids["alice"],)).fetchone()[0] == 2   # records stay
    assert conn.execute("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[:] == ("ungate", "alice")
    with pytest.raises(tickets.TicketError, match="no tickets"):
        tickets.ungate(conn, ids["admin"], ids["admin"], now=NOW)


def test_user_state_summarises_gating_and_live_tickets(db):
    conn, cfg, ids = seeded(db)
    assert tickets.user_state(conn, ids["alice"], NOW) == {"gated": False, "current": None, "queued": None, "live": False}
    a = grant(conn, cfg, ids, length="day")
    s = tickets.user_state(conn, ids["alice"], NOW + 10)
    assert s["gated"] and s["live"] and s["current"]["id"] == a["id"] and s["queued"] is None
    s = tickets.user_state(conn, ids["alice"], NOW + 2 * DAY)
    assert s == {"gated": True, "current": None, "queued": None, "live": False}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tickets_capacity.py -q -k "cancel or bonus or ungate or user_state"`
Expected: FAIL with `AttributeError: module 'claude_proxy.tickets' has no attribute 'add_bonus'` (and `cancel`).

- [ ] **Step 3: Implement**

Append to `src/claude_proxy/tickets.py`:

```python
# ---------- cancel, bonus, queue moves ----------

def _later_tickets(conn, user_id: int, from_t: int) -> list[dict]:
    """The user's non-cancelled tickets starting at or after `from_t`, earliest first."""
    return [dict(r) for r in conn.execute(f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL "
                                          f"AND t.starts_at>=? ORDER BY t.starts_at, t.id", (user_id, from_t))]


def _shift(conn, cfg: Config, chain: list[dict], delta: int) -> None:
    """Move a user's queued tickets by `delta` seconds, bonus periods with them, each re-checked at its new place.
    Moves toward the past go earliest-first and moves toward the future latest-first, so a moving ticket never meets
    its own neighbour. One savepoint: a move that does not fit undoes them all and raises CapacityError."""
    conn.execute("SAVEPOINT moves")
    try:
        for tk in (chain if delta < 0 else reversed(chain)):
            s, e = tk["starts_at"] + delta, tk["effective_end"] + delta
            check_capacity(conn, cfg, tk["account_id"], tk["share_pct"], s, e, exclude=tk["id"])
            for b in bonuses(conn, tk["id"]):
                if b["cancelled_at"] is None and b["share_pct"] > 0:
                    check_capacity(conn, cfg, tk["account_id"], tk["share_pct"] + b["share_pct"], b["starts_at"] + delta,
                                   b["ends_at"] + delta, exclude=tk["id"])
            conn.execute("UPDATE tickets SET starts_at=starts_at+?, ends_at=ends_at+? WHERE id=?", (delta, delta, tk["id"]))
            conn.execute("UPDATE ticket_bonuses SET starts_at=starts_at+?, ends_at=ends_at+? WHERE ticket_id=? AND cancelled_at IS NULL",
                         (delta, delta, tk["id"]))
        conn.execute("RELEASE moves")
    except BaseException:
        conn.execute("ROLLBACK TO moves")
        conn.execute("RELEASE moves")
        raise


def cancel(conn: sqlite3.Connection, cfg: Config, actor: int | None, ticket_id: int, now: float | None = None) -> dict:
    """Free the slice and end the ticket's bonuses. The user's queued tickets move forward to close the gap, each
    re-checked; if one would not fit they keep their dates and `dates_kept` says so. Never refused."""
    now = _now(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = get(conn, ticket_id)
        if t["cancelled_at"] is not None:
            raise TicketError("This ticket is already cancelled.")
        conn.execute("UPDATE tickets SET cancelled_at=?, cancelled_by=? WHERE id=?", (now, actor, ticket_id))
        conn.execute("UPDATE ticket_bonuses SET cancelled_at=?, cancelled_by=? WHERE ticket_id=? AND cancelled_at IS NULL", (now, actor, ticket_id))
        moved, kept, reason = 0, False, None
        chain = _later_tickets(conn, t["user_id"], t["effective_end"]) if t["user_id"] is not None else []
        if chain:
            shift = max(now, t["starts_at"]) - chain[0]["starts_at"]   # a queued ticket never starts in the past
            if shift < 0:
                try:
                    _shift(conn, cfg, chain, shift)
                    moved = len(chain)
                except CapacityError as e:
                    kept, reason = True, str(e)
        db.audit(conn, actor, "ticket_cancel", t["user_name"], {"ticket_id": ticket_id, "moved": moved, "dates_kept": kept})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"ticket": get(conn, ticket_id), "moved": moved, "dates_kept": kept, "reason": reason}


def add_bonus(conn: sqlite3.Connection, cfg: Config, actor: int | None, ticket_id: int, share_pct=0, extra_days=0, starts_at=None,
              ends_at=None, note: str = "", now: float | None = None) -> dict:
    """Extra share for a period inside the ticket, extra days at the ticket's share, or both. Extra days push the user's
    queued tickets forward; each move and the extension itself are checked against capacity, and one failure refuses
    the whole bonus."""
    now = _now(now)
    share_pct = float(share_pct or 0)
    extra_days = int(extra_days or 0)
    if share_pct < 0 or extra_days < 0 or (share_pct == 0 and extra_days == 0):
        raise TicketError("A bonus needs extra share above 0, extra days above 0, or both.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = get(conn, ticket_id)
        if t["cancelled_at"] is not None:
            raise TicketError("The ticket is cancelled; bonuses go on live tickets.")
        old_end, new_end, moved = t["effective_end"], t["effective_end"] + extra_days * DAY, 0
        if extra_days:
            chain = _later_tickets(conn, t["user_id"], old_end) if t["user_id"] is not None else []
            if chain and chain[0]["starts_at"] < new_end:
                try:
                    _shift(conn, cfg, chain, new_end - chain[0]["starts_at"])
                    moved = len(chain)
                except CapacityError as e:
                    raise CapacityError(f"The extra days would move {t['user_name']}'s queued ticket, which then does not fit: {e}") from e
            check_capacity(conn, cfg, t["account_id"], t["share_pct"], old_end, new_end)
        if share_pct:
            s = max(int(now if starts_at is None else starts_at), t["starts_at"])
            e = min(int(new_end if ends_at is None else ends_at), new_end)
            if s >= e:
                raise TicketError("The bonus share period must lie within the ticket's period.")
            check_capacity(conn, cfg, t["account_id"], share_pct, s, e)
        else:
            s, e = old_end, new_end
        cur = conn.execute("INSERT INTO ticket_bonuses(ticket_id, share_pct, extra_days, starts_at, ends_at, note, granted_by, granted_at) "
                           "VALUES(?,?,?,?,?,?,?,?)", (ticket_id, share_pct, extra_days, s, e, (note or "").strip() or None, actor, now))
        db.audit(conn, actor, "ticket_bonus", t["user_name"], {"ticket_id": ticket_id, "bonus_id": cur.lastrowid, "share_pct": share_pct,
                                                               "extra_days": extra_days, "starts_at": s, "ends_at": e, "moved": moved})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"bonus": dict(conn.execute("SELECT * FROM ticket_bonuses WHERE id=?", (cur.lastrowid,)).fetchone()),
            "ticket": get(conn, ticket_id), "moved": moved}


def ungate(conn: sqlite3.Connection, actor: int | None, user_id: int, now: float | None = None) -> int:
    """Stop the user's tickets from gating them (spec section 7, Exit). Only for a user with no active or queued
    ticket. The tickets remain as sales records. Returns how many rows were marked."""
    now = _now(now)
    name = db._user_name(conn, user_id)
    if covering(conn, user_id, now) or next_queued(conn, user_id, now):
        raise TicketError(f"{name} has an active or queued ticket; cancel it before ungating.")
    n = conn.execute("UPDATE tickets SET ungated_at=? WHERE user_id=? AND ungated_at IS NULL", (now, user_id)).rowcount
    if n == 0:
        raise TicketError(f"{name} has no tickets to ungate.")
    db.audit(conn, actor, "ungate", name, {"tickets": n})
    return n


def user_state(conn: sqlite3.Connection, user_id: int, now: float) -> dict:
    """For the Users page: whether the user is ticket-gated and which ticket covers now or is queued next."""
    current, queued = covering(conn, user_id, now), next_queued(conn, user_id, now)
    return {"gated": is_gated(conn, user_id), "current": current, "queued": queued, "live": current is not None or queued is not None}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tickets_capacity.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/tickets.py tests/test_tickets_capacity.py
git commit -m "Tickets: cancel closes the gap, bonuses extend or add share, Ungate

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Per-request attribution and the observed rate

**Files:**
- Modify: `src/claude_proxy/quota.py`
- Test: `tests/test_quota.py` (append)

**Interfaces:**
- Produces, in `quota.py`:
  - `request_shares(conn, pricing, bucket, since, now) -> list[tuple[int, int, str | None, float, float]]`: `(request_id, user_id, model, ended_at, points)` for every forwarded Anthropic request that ended after `since` and by `now`, each carrying the percentage points of the bucket attributed to it. Snapshot pairs are walked from the last snapshot at or before `since`, so an interval straddling `since` is split by which requests ended after it. A reset (reset time moving forward by more than `RESET_TOLERANCE_S`, or a utilization drop when reset times are unknown) restarts the high-water mark and attributes nothing for that pair.
  - `attributed_since(conn, pricing, bucket, user_id, since, now) -> float`: the user's sum of points.
  - `observed_rate(conn, pricing, bucket, now, days=7) -> float | None`: weighted tokens per utilization point, the median over the last `days` of snapshot pairs with a rise above zero and at least one forwarded Anthropic request in between; None when there is no such pair.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_quota.py`; `_snap` and `_req` exist there, `_snap` takes `resets=`)

```python
def _snap7(conn, t, util, resets=10_000_000):
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                 (t, "header", "7d", util, resets))


def test_request_shares_attribute_each_request_its_part_of_the_rise(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    b, _ = create_user(conn, "b")
    _snap7(conn, 100, 10)
    _req(conn, a, 150, 3000)       # id 1
    _req(conn, b, 160, 1000)       # id 2
    _snap7(conn, 200, 30)          # +20 split 3:1
    _req(conn, a, 250, 500)        # id 3
    _snap7(conn, 300, 32)          # +2, a alone
    rows = quota.request_shares(conn, Pricing(), "7d", since=0, now=310)
    assert [(r[0], r[1], round(r[4], 3)) for r in rows] == [(1, a, 15.0), (2, b, 5.0), (3, a, 2.0)]
    assert quota.attributed_since(conn, Pricing(), "7d", a, 0, 310) == pytest.approx(17)
    assert quota.attributed_since(conn, Pricing(), "7d", b, 0, 310) == pytest.approx(5)


def test_request_shares_split_a_straddling_interval_by_the_requests_after_since(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    _snap7(conn, 100, 0)
    _req(conn, a, 120, 1000)       # before the day boundary at 150
    _req(conn, a, 180, 3000)       # after it
    _snap7(conn, 200, 8)           # +8 over an interval that straddles 150
    assert quota.attributed_since(conn, Pricing(), "7d", a, since=150, now=210) == pytest.approx(6)   # 3000/4000 of 8
    assert quota.attributed_since(conn, Pricing(), "7d", a, since=0, now=210) == pytest.approx(8)
    assert quota.attributed_since(conn, Pricing(), "7d", a, since=190, now=210) == 0


def test_request_shares_count_both_sides_of_a_weekly_reset(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    _snap7(conn, 100, 90, resets=1000)
    _req(conn, a, 150, 10)
    _snap7(conn, 200, 95, resets=1000)      # +5 before the reset
    _snap7(conn, 1100, 1, resets=700_000)   # the week reset: a drop with the reset time moving forward
    _req(conn, a, 1150, 10)
    _snap7(conn, 1200, 4, resets=700_000)   # +3 after it
    assert quota.attributed_since(conn, Pricing(), "7d", a, since=0, now=1210) == pytest.approx(8)
    # A dip with an unchanged reset time is rounding noise, not usage and not a reset.
    _snap7(conn, 1300, 3.5, resets=700_000)
    _req(conn, a, 1350, 10)
    _snap7(conn, 1400, 5, resets=700_000)   # high-water was 4: +1
    assert quota.attributed_since(conn, Pricing(), "7d", a, since=0, now=1410) == pytest.approx(9)


def test_request_shares_with_fewer_than_two_snapshots_is_empty(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    _req(conn, a, 150, 10)
    assert quota.request_shares(conn, Pricing(), "7d", 0, 200) == []
    _snap7(conn, 100, 0)
    assert quota.attributed_since(conn, Pricing(), "7d", a, 0, 200) == 0


def test_observed_rate_is_the_median_tokens_per_point(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    now = 1_000_000
    _snap(conn, now - 5000, 0)
    _req(conn, a, now - 4500, 1000)            # 1000 weighted (sonnet input = reference)
    _snap(conn, now - 4000, 2)                 # 500 per point
    _req(conn, a, now - 3500, 3000)
    _snap(conn, now - 3000, 3)                 # 3000 per point
    _snap(conn, now - 2500, 3)                 # no rise: ignored
    _snap(conn, now - 2000, 5)                 # a rise with no request in between: ignored
    _req(conn, a, now - 1500, 4000)
    _snap(conn, now - 1000, 7)                 # 2000 per point
    assert quota.observed_rate(conn, Pricing(), "5h", now) == 2000
    assert quota.observed_rate(conn, Pricing(), "7d", now) is None
    assert quota.observed_rate(conn, Pricing(), "5h", now + 8 * 86400) is None   # older than 7 days
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_quota.py -q -k "request_shares or observed_rate"`
Expected: FAIL with `AttributeError: module 'claude_proxy.quota' has no attribute 'request_shares'`.

- [ ] **Step 3: Implement**

Add `import statistics` to `quota.py`'s imports and append after `attribution`:

```python
def _is_reset(prev, cur) -> bool:
    """The same rule _window uses between two consecutive snapshots."""
    if cur["resets_at"] and prev["resets_at"]:
        return cur["resets_at"] - prev["resets_at"] > RESET_TOLERANCE_S
    return cur["utilization_pct"] < prev["utilization_pct"]


def _pairs(conn, pricing: Pricing, bucket: str, lo: float, now: float):
    """Consecutive snapshot pairs of a bucket from `lo` to `now`, each with the forwarded Anthropic requests that ended
    in between as (id, user_id, model, ended_at, weighted). Yields (prev, cur, requests)."""
    rows = conn.execute("SELECT observed_at, utilization_pct, resets_at FROM quota_snapshots WHERE bucket=? AND observed_at>=? "
                        "AND observed_at<=? ORDER BY observed_at", (bucket, lo, now)).fetchall()
    if len(rows) < 2:
        return
    span = "provider='anthropic' AND rejected_by IS NULL AND ended_at > ? AND ended_at <= ?"
    bounds = (rows[0]["observed_at"], rows[-1]["observed_at"])
    models = [m for (m,) in conn.execute(f"SELECT DISTINCT model FROM requests WHERE {span}", bounds)]
    w, args = priced_sql(pricing, models, pricing.reference_input(), unpriced=raw_tokens_sql())
    reqs = conn.execute(f"SELECT id, user_id, model, ended_at, {w} AS w FROM requests WHERE {span} ORDER BY ended_at, id",
                        (*args, *bounds)).fetchall()
    j = 0
    for prev, cur in zip(rows, rows[1:]):
        batch = []
        while j < len(reqs) and reqs[j]["ended_at"] <= cur["observed_at"]:
            batch.append(reqs[j])
            j += 1
        yield prev, cur, batch


def request_shares(conn: sqlite3.Connection, pricing: Pricing, bucket: str, since: float, now: float) -> list[tuple]:
    """(request_id, user_id, model, ended_at, points): each forwarded Anthropic request that ended after `since`, with
    the percentage points of the bucket attributed to it: its weighted tokens' part of the rise over the snapshot pair
    it ended in. Walking starts at the last snapshot at or before `since`, so an interval straddling `since` is split by
    which requests ended after it. A reset restarts the high-water mark and attributes nothing for that pair."""
    first = conn.execute("SELECT observed_at FROM quota_snapshots WHERE bucket=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
                         (bucket, since)).fetchone()
    lo = first[0] if first else since
    out, high = [], None
    for prev, cur, batch in _pairs(conn, pricing, bucket, lo, now):
        if high is None:
            high = prev["utilization_pct"]
        if _is_reset(prev, cur):
            high = cur["utilization_pct"]
            continue
        delta = cur["utilization_pct"] - high
        high = max(high, cur["utilization_pct"])
        total_w = sum(r["w"] for r in batch)
        if delta <= 0 or total_w <= 0:
            continue
        out.extend((r["id"], r["user_id"], r["model"], r["ended_at"], delta * r["w"] / total_w) for r in batch if r["ended_at"] > since)
    return out


def attributed_since(conn: sqlite3.Connection, pricing: Pricing, bucket: str, user_id: int, since: float, now: float) -> float:
    """The bucket's share attributed to one user's requests since `since` (a ticket day's start, spec section 7)."""
    return sum(p for (_, uid, _, _, p) in request_shares(conn, pricing, bucket, since, now) if uid == user_id)


def observed_rate(conn: sqlite3.Connection, pricing: Pricing, bucket: str, now: float, days: int = 7) -> float | None:
    """Weighted tokens per utilization point: the median of weighted ÷ rise over the last `days` of snapshot pairs with
    a rise above zero and at least one forwarded Anthropic request in between. None when no such pair exists, which can
    only happen on an account that has never served a request. Used to estimate a share when snapshots are stale."""
    ratios = []
    for prev, cur, batch in _pairs(conn, pricing, bucket, now - days * 86400, now):
        delta = cur["utilization_pct"] - prev["utilization_pct"]
        total_w = sum(r["w"] for r in batch)
        if not _is_reset(prev, cur) and delta > 0 and batch and total_w > 0:
            ratios.append(total_w / delta)
    return statistics.median(ratios) if ratios else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_quota.py -q`
Expected: all PASS, the existing attribution tests included.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/quota.py tests/test_quota.py
git commit -m "Quota: per-request attribution since a moment, and the observed tokens-per-point rate

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Enforcement for ticket-gated users

**Files:**
- Modify: `src/claude_proxy/limits.py`, `src/claude_proxy/web.py` (status line label only)
- Test: `tests/test_tickets_limits.py`

**Interfaces:**
- Consumes: `tickets.is_gated`, `tickets.covering`, `tickets.next_queued`, `tickets.last_ended`, `tickets.current_day`, `tickets.bonus_share`; `quota.attribution`, `quota.attributed_since`, `quota.observed_rate`.
- Produces, in `limits.py`:
  - `LimitState` gains `no_live_data: bool = False` (estimated from weighted tokens while snapshots are stale), `resets_at: float | None = None`, `tier: str | None = None`; all three in `to_dict()`.
  - Constants `TICKET = "ticket"`, `TICKET_NO_DATA = "ticket_no_data"`, `NO_RATE = "no usage rate observed yet"`; `SHARE_LABELS["share_day"] = "today's share"`, `USER_KINDS["share_day"] = "today_limit"`.
  - `ticket_states(conn, cfg, user_id, now, ticket=None) -> list[LimitState]` (`share_5h` then `share_day`; empty when no ticket covers now).
  - `states` drops admin `share_5h`/`share_7d` rows for a gated user and appends `ticket_states`; `evaluate` applies the 403 / third-party / 503 / 429 rules.
- `web._status_line` labels `share_day` as `today`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tickets_limits.py
import pytest

from claude_proxy import limits, tickets
from claude_proxy.db import create_user
from tests.tickets_helpers import DAY, NOW, seeded, user


def set_limit(conn, uid, kind, value, unit, scope="*"):
    conn.execute("INSERT OR REPLACE INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (uid, kind, scope, str(value), unit))


def req(conn, uid, t, i=0, o=0, model="claude-sonnet-5", provider="anthropic", path="/v1/messages"):
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, input_tokens, output_tokens) VALUES(?,?,?,?,?,?,?,?,?)",
                 (uid, t - 1, t, "POST", path, provider, model, i, o))


def snap(conn, t, util, bucket, resets):
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                 (t, "header", bucket, util, resets))


RESET5, RESET7 = NOW + 3500, NOW + 3 * DAY   # fixed reset times: a moving reset time would read as a new window


def fresh(conn, t, util5=0, util7=0):
    """A pair of account snapshots at t, so share limits are measured, not skipped."""
    snap(conn, t, util5, "5h", RESET5)
    snap(conn, t, util7, "7d", RESET7)


def check(conn, cfg, uid, now=NOW, model="claude-sonnet-5", path="/v1/messages"):
    return limits.evaluate(conn, cfg, uid, model, path, now=now)


def by_kind(conn, cfg, uid, now=NOW):
    return {s.kind: s for s in limits.states(conn, cfg, uid, now=now)}


@pytest.fixture
def env(db):
    conn, cfg, ids = seeded(db)
    fresh(conn, NOW - 600)
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "EUR", now=NOW - 300)   # 5%, share_day 0.714
    return conn, cfg, ids, t


def test_non_ticket_users_are_untouched(db):
    conn, cfg, ids = seeded(db)
    set_limit(conn, ids["alice"], "share_5h", 20, "pct")
    assert check(conn, cfg, ids["alice"]) is None
    assert list(by_kind(conn, cfg, ids["alice"])) == ["share_5h"]


def test_ticket_builds_5h_and_day_states_in_place_of_admin_share_rows(env):
    conn, cfg, ids, t = env
    set_limit(conn, ids["alice"], "share_5h", 50, "pct")
    set_limit(conn, ids["alice"], "share_7d", 50, "pct")
    set_limit(conn, ids["alice"], "requests_daily", 100, "count")
    st = by_kind(conn, cfg, ids["alice"])
    assert set(st) == {"requests_daily", "share_5h", "share_day"}
    assert (st["share_5h"].limit, st["share_5h"].tier) == (5, "Lite")
    assert st["share_day"].limit == pytest.approx(5 / 7)
    assert (st["share_day"].current, st["share_day"].reset_in, st["share_day"].resets_at) == (0, DAY - 300, NOW - 300 + DAY)
    assert st["share_day"].to_dict()["no_live_data"] is False


def test_share_5h_measured_as_today_and_exceeded_is_the_usual_429(env):
    conn, cfg, ids, t = env
    req(conn, ids["alice"], NOW - 200, i=1000)
    fresh(conn, NOW - 100, util5=6)             # alice alone: +6 points of the 5-hour bucket
    d = check(conn, cfg, ids["alice"])
    assert (d.status, d.kind, d.retry_after) == (429, "share_5h", 3500)
    assert "5-hour limit" in d.body["error"]["message"]


def test_share_day_counts_the_weekly_bucket_since_the_day_began_and_resets_at_the_boundary(env):
    conn, cfg, ids, t = env
    day2 = t["starts_at"] + DAY
    req(conn, ids["alice"], day2 - 100, i=1000)
    fresh(conn, day2 - 50, util7=0.5)           # +0.5 in day 1
    req(conn, ids["alice"], day2 + 100, i=1000)
    fresh(conn, day2 + 200, util7=1.0)          # +0.5 in day 2
    assert by_kind(conn, cfg, ids["alice"], now=day2 - 10)["share_day"].current == pytest.approx(0.5)
    assert by_kind(conn, cfg, ids["alice"], now=day2 + 300)["share_day"].current == pytest.approx(0.5)   # day 1's use is gone
    assert check(conn, cfg, ids["alice"], now=day2 + 300) is None
    req(conn, ids["alice"], day2 + 400, i=1000)
    fresh(conn, day2 + 500, util7=1.3)          # 0.8 today: over 0.714
    d = check(conn, cfg, ids["alice"], now=day2 + 600)
    assert (d.status, d.kind, d.retry_after) == (429, "share_day", DAY - 600)
    assert d.body["error"]["message"] == "Today's share of your Lite ticket is used up. It resets at 07:55 UTC."   # the ticket started at 07:55


def test_a_slice_handed_over_mid_week_never_draws_more_than_its_share_from_one_week(db):
    """Alice's week ticket ends on Wednesday; Bob's starts then. In the Anthropic week that spans the handover,
    the slice's spend is capped at seven day-shares = the slice share."""
    conn, cfg, ids = seeded(db)
    cfg.tickets.tiers["lite"].share_pct = 7     # share_day = 1 point: whole numbers keep the arithmetic exact
    cfg.tickets.max_sold_pct = 7
    bob, _ = create_user(conn, "bob")
    a = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "USD", now=NOW)
    b = tickets.grant(conn, cfg, ids["admin"], user(conn, bob), "lite", "week", "USD", now=NOW + 10)   # queued after alice
    assert b["starts_at"] == a["ends_at"]
    cap = 0
    for day in range(7):                        # each ticket day, whoever holds the slice, allows share/7 at most
        t0 = NOW + 3 * DAY + day * DAY          # a 7-day span starting mid-ticket: 4 of alice's days, 3 of bob's
        holder = ids["alice"] if day < 4 else bob
        fresh(conn, t0 + 1, util7=cap)
        req(conn, holder, t0 + 10, i=1000)
        fresh(conn, t0 + 20, util7=cap + 1)
        cap += 1
        assert by_kind(conn, cfg, holder, now=t0 + 30)["share_day"].exceeded is True
    assert cap == 7                             # the slice drew exactly its share over those seven days, not 14


def test_bonus_share_raises_both_limits_and_bonus_days_continue_the_day_boundaries(env):
    conn, cfg, ids, t = env
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=2, starts_at=NOW, ends_at=NOW + DAY, now=NOW)
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], extra_days=1, now=NOW)
    st = by_kind(conn, cfg, ids["alice"], now=NOW + 10)
    assert st["share_5h"].limit == 7 and st["share_day"].limit == pytest.approx(1)
    st = by_kind(conn, cfg, ids["alice"], now=NOW + 2 * DAY)                  # bonus share over
    assert st["share_5h"].limit == 5
    late = t["ends_at"] + 3600                                                 # inside the bonus day
    fresh(conn, late - 100)
    st = by_kind(conn, cfg, ids["alice"], now=late)
    assert st["share_day"].reset_in == DAY - 3600                              # the day started at the old end


def test_no_active_ticket_is_a_403_with_the_right_message(env):
    conn, cfg, ids, t = env
    after = t["ends_at"] + 100
    fresh(conn, after - 50)
    d = check(conn, cfg, ids["alice"], now=after)
    assert (d.status, d.kind, d.body["error"]["type"]) == (403, "ticket", "permission_error")
    assert d.body["error"]["message"] == f"Your ticket ended on {limits._date(t['ends_at'])}. Send the amount by bank transfer and email the admin."
    assert check(conn, cfg, ids["alice"], now=after, model=None, path="/v1/models").status == 403   # every request
    nxt = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=after)
    conn.execute("UPDATE tickets SET starts_at=?, ends_at=? WHERE id=?", (after + DAY, after + 2 * DAY, nxt["id"]))
    d = check(conn, cfg, ids["alice"], now=after + 10)
    assert d.body["error"]["message"] == f"Your next ticket starts on {limits._date(after + DAY)}."
    assert by_kind(conn, cfg, ids["alice"], now=after + 10) == {}             # nothing to show between tickets


def test_cancelled_ticket_message_uses_the_cancel_time(env):
    conn, cfg, ids, t = env
    tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW)
    d = check(conn, cfg, ids["alice"], now=NOW + 10)
    assert d.body["error"]["message"].startswith(f"Your ticket ended on {limits._date(NOW)}.")


def test_third_party_models_refused_unless_a_cost_limit_exists(env):
    conn, cfg, ids, t = env
    d = check(conn, cfg, ids["alice"], model="muse-spark")
    assert (d.status, d.kind, d.body["error"]["message"]) == (403, "ticket", "Your ticket covers Claude models only.")
    set_limit(conn, ids["alice"], "cost_daily", 1.0, "usd")
    assert check(conn, cfg, ids["alice"], model="muse-spark") is None
    req(conn, ids["alice"], NOW - 60, i=1_000_000, model="muse-spark")        # $1.25 today
    assert check(conn, cfg, ids["alice"], model="muse-spark").kind == "cost_daily"


def test_other_limit_rows_still_apply(env):
    conn, cfg, ids, t = env
    set_limit(conn, ids["alice"], "allowed_models", "claude-sonnet-*", "list")
    assert check(conn, cfg, ids["alice"], model="claude-opus-5").status == 403
    set_limit(conn, ids["alice"], "requests_daily", 1, "count")
    req(conn, ids["alice"], NOW - 60)
    assert check(conn, cfg, ids["alice"]).kind == "requests_daily"


def test_stale_snapshot_estimates_from_weighted_tokens_at_the_observed_rate(env):
    conn, cfg, ids, t = env
    # History: 1000 weighted tokens moved both buckets by 1 point -> rate 1000 per point.
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)
    mid = NOW + 2000                                                           # snapshots are now > 30 min old
    st = by_kind(conn, cfg, ids["alice"], now=mid)
    assert st["share_5h"].no_live_data and st["share_day"].no_live_data
    assert st["share_5h"].current == pytest.approx(1.0)                        # counted from the window's first snapshot
    assert st["share_day"].current == pytest.approx(1.0)                       # and from the day's start (NOW-300)
    later = NOW + 7200                                                         # the stale snapshot's 5-hour reset (NOW+3500) has passed
    req(conn, ids["alice"], later - 60, i=600)
    st = by_kind(conn, cfg, ids["alice"], now=later)
    assert st["share_5h"].current == pytest.approx(0.6)                        # 5-hour count restarts at that reset time
    assert st["share_day"].current == pytest.approx(1.6)                       # the day keeps counting: 1.6 > 0.714
    assert check(conn, cfg, ids["alice"], now=later).kind == "share_day"


def test_ticket_user_gets_503_when_no_rate_was_ever_observed(db):
    conn, cfg, ids = seeded(db)
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW)
    d = check(conn, cfg, ids["alice"], now=NOW + 10)                           # no snapshots at all
    assert (d.status, d.kind, d.retry_after) == (503, "ticket_no_data", 60)
    assert d.body["error"] == {"type": "api_error", "message": "Usage data is unavailable. Please retry in a minute."}
    assert by_kind(conn, cfg, ids["alice"], now=NOW + 10)["share_day"].skipped == limits.NO_RATE
    assert check(conn, cfg, ids["alice"], now=NOW + 10, path="/v1/messages/count_tokens") is None   # consumes no quota


def test_ungated_users_keep_the_old_stale_behaviour(db):
    conn, cfg, ids = seeded(db)
    set_limit(conn, ids["alice"], "share_5h", 1, "pct")
    assert check(conn, cfg, ids["alice"]) is None                              # stale: skipped, as today
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW)
    tickets.cancel(conn, cfg, ids["admin"], t["id"], now=NOW + 1)
    tickets.ungate(conn, ids["admin"], ids["alice"], now=NOW + 2)
    assert check(conn, cfg, ids["alice"], now=NOW + 3) is None
    assert by_kind(conn, cfg, ids["alice"], now=NOW + 3)["share_5h"].skipped.startswith("no account snapshot")


def test_user_view_and_status_line_name_the_day_limit(env):
    from claude_proxy.web import _status_line
    conn, cfg, ids, t = env
    sts = limits.states(conn, cfg, ids["alice"], now=NOW)
    views = {v["kind"]: v for v in (limits.user_view(s) for s in sts)}
    assert set(views) == {"5h_limit", "today_limit"} and views["today_limit"]["limit"] == 100.0
    line = _status_line(user(conn, ids["alice"]), sts, None)
    assert line.startswith("alice · 5h 0% (resets in") and " · today 0% (resets in 23.9 h)" in line
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tickets_limits.py -q`
Expected: FAIL (first failure: `AttributeError: 'LimitState' object has no attribute 'tier'` or the 5h state missing).

- [ ] **Step 3: Implement in `limits.py`**

Imports: add `from . import quota, tickets`. Extend the constants:

```python
SHARE_LABELS = {"share_5h": "5-hour limit", "share_7d": "weekly limit", "share_day": "today's share"}
USER_KINDS = {"share_5h": "5h_limit", "share_7d": "weekly_limit", "share_day": "today_limit"}
TICKET = "ticket"                  # rejected_by: a ticket-gated user with no active ticket, or on a third-party model
TICKET_NO_DATA = "ticket_no_data"  # rejected_by: the 503 while no usage rate was ever observed
NO_RATE = "no usage rate observed yet"
```

(Move `SHARE_LABELS`/`USER_KINDS` above `_describe` stays fine; they already live there.) Add the three fields to `LimitState`:

```python
    estimated: bool = False
    no_live_data: bool = False    # estimated from the user's weighted tokens because account snapshots are stale
    resets_at: float | None = None
    tier: str | None = None       # the ticket's tier label, for messages
```

and in `to_dict()` add `"no_live_data": self.no_live_data, "resets_at": self.resets_at, "tier": self.tier`.

Add the helpers after `_share_state`:

```python
def _date(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def _gated(conn, cfg, user_id) -> bool:
    return cfg.tickets.enabled and tickets.is_gated(conn, user_id)


def _weighted_since(conn, cfg, user_id, since, now) -> float:
    groups = conn.execute(f"SELECT model, {SUMS} FROM requests WHERE user_id=? AND provider='anthropic' AND rejected_by IS NULL "
                          f"AND path!=? AND ended_at>? AND ended_at<=? GROUP BY model", (user_id, COUNT_TOKENS_PATH, since, now)).fetchall()
    return sum(price_totals(cfg.pricing, g["model"], Totals(g["n"], g["i"], g["o"], g["c5"], g["c1"], g["cr"])).weighted for g in groups)


def _finish(st: LimitState) -> LimitState:
    st.remaining = max(0.0, st.limit - st.current)
    st.exceeded = st.current >= st.limit
    return st


def _ticket_5h(conn, cfg, user_id, share, label, now) -> LimitState:
    st = LimitState("share_5h", "*", f"{share:g}", "pct", limit=share, estimated=True, tier=label)
    att = quota.attribution(conn, cfg.pricing, "5h", now=now, stale_after_s=cfg.quota.stale_after_s)
    if att["resets_at"] and att["resets_at"] > now:
        st.resets_at, st.reset_in = att["resets_at"], max(1, int(att["resets_at"] - now))
    if att["utilization_pct"] is not None and not att["stale"]:
        st.current = att["shares"].get(user_id, 0.0)
        return _finish(st)
    rate = quota.observed_rate(conn, cfg.pricing, "5h", now)
    if rate is None:
        st.skipped = NO_RATE
        return st
    # Counting starts at the last known window start, or at the stale snapshot's reset time once that has passed.
    start = att["window_start"] if att["window_start"] is not None else now - 5 * HOUR
    if att["resets_at"] and att["resets_at"] <= now:
        start = att["resets_at"]
    st.current, st.no_live_data = _weighted_since(conn, cfg, user_id, start, now) / rate, True
    return _finish(st)


def _ticket_day(conn, cfg, user_id, share, label, day_start, day_end, now) -> LimitState:
    limit = share / 7
    st = LimitState("share_day", "*", f"{limit:g}", "pct", limit=limit, estimated=True, tier=label,
                    resets_at=day_end, reset_in=max(1, int(day_end - now)))
    att = quota.attribution(conn, cfg.pricing, "7d", now=now, stale_after_s=cfg.quota.stale_after_s)
    if att["utilization_pct"] is not None and not att["stale"]:
        st.current = quota.attributed_since(conn, cfg.pricing, "7d", user_id, day_start, now)
        return _finish(st)
    rate = quota.observed_rate(conn, cfg.pricing, "7d", now)
    if rate is None:
        st.skipped = NO_RATE
        return st
    st.current, st.no_live_data = _weighted_since(conn, cfg, user_id, day_start, now) / rate, True
    return _finish(st)


def ticket_states(conn, cfg: Config, user_id: int, now: float, ticket: dict | None = None) -> list[LimitState]:
    """The two share limits a ticket builds (spec section 7): share_5h against Anthropic's 5-hour window, and share_day,
    one seventh of the share, against the weekly bucket's share attributed since the current ticket day began."""
    t = ticket or tickets.covering(conn, user_id, now)
    if t is None:
        return []
    share = t["share_pct"] + tickets.bonus_share(conn, t["id"], now)
    label = cfg.tickets.tiers[t["tier"]].label if t["tier"] in cfg.tickets.tiers else t["tier"]
    day_start, day_end = tickets.current_day(t, now)
    return [_ticket_5h(conn, cfg, user_id, share, label, now), _ticket_day(conn, cfg, user_id, share, label, day_start, day_end, now)]


def _no_ticket_message(conn, cfg, user_id, now) -> str:
    nxt = tickets.next_queued(conn, user_id, now)
    if nxt:
        return f"Your next ticket starts on {_date(nxt['starts_at'])}."
    last = tickets.last_ended(conn, user_id, now)
    when = _date(last["ended_at"]) if last else "an earlier date"
    return f"Your ticket ended on {when}. {cfg.tickets.how_to_buy}".strip()


def _perm(message: str) -> dict:
    return {"type": "error", "error": {"type": "permission_error", "message": message}}
```

Change `states`:

```python
def states(conn: sqlite3.Connection, cfg: Config, user_id: int, now: float | None = None) -> list[LimitState]:
    now = time.time() if now is None else now
    gated = _gated(conn, cfg, user_id)
    out = []
    for row in _rows(conn, user_id):
        if gated and row["kind"] in SHARE_BUCKETS:
            continue   # the ticket's own share limits stand in for these while it gates the user
        if row["kind"] in WINDOWS:
            out.append(_window_state(conn, cfg, user_id, row, now))
        elif row["kind"] in TOTALS:
            out.append(_total_state(conn, cfg, user_id, row))
        elif row["kind"] in SHARE_BUCKETS:
            out.append(_share_state(conn, cfg, user_id, row, now))
        else:
            out.append(LimitState(row["kind"], row["scope"], row["value"], row["unit"]))
    if gated:
        out.extend(ticket_states(conn, cfg, user_id, now))
    return out
```

Change `_describe` so a `share_day` state gets its own sentence (before the `SHARE_LABELS` branch):

```python
    if st.kind == "share_day":
        return (f"Today's share of your {st.tier} ticket is used up. It resets at "
                f"{time.strftime('%H:%M UTC', time.gmtime(st.resets_at))}.")
```

Change `evaluate`: after `third_party = ...` and the `max_inflight` check, insert:

```python
    gated = _gated(conn, cfg, user_id)
    ticket = None
    if gated:
        ticket = tickets.covering(conn, user_id, now)
        if ticket is None:   # every request, the model list included: there is no ticket to serve it on
            return Decision(403, TICKET, _perm(_no_ticket_message(conn, cfg, user_id, now)))
    rows = _rows(conn, user_id)
    if gated and third_party and not any(r["kind"].startswith("cost_") for r in rows):
        # Third-party models are real money per request, which a ticket does not cover; a cost limit the admin set governs instead.
        return Decision(403, TICKET, _perm("Your ticket covers Claude models only."))
```

(Replace the existing `rows = _rows(conn, user_id)` line.) In the row loop, `elif kind in SHARE_BUCKETS:` becomes:

```python
        elif kind in SHARE_BUCKETS:
            if third_party or gated:
                continue
            st = _share_state(conn, cfg, user_id, row, now)
```

After the loop and before the free-credit cap block:

```python
    if gated and not is_count_tokens and not third_party:
        for st in ticket_states(conn, cfg, user_id, now, ticket):
            if st.skipped == NO_RATE:
                return Decision(503, TICKET_NO_DATA, {"type": "error", "error": {"type": "api_error", "message":
                                "Usage data is unavailable. Please retry in a minute."}}, 60)
            if st.exceeded:
                return Decision(429, st.kind, {"type": "error", "error": {"type": "rate_limit_error", "message": _describe(st)}},
                                st.reset_in or 60)
```

In `web.py`, `_status_line`: replace both `label = "5h" if period == "5h" else "week"` and `f"{'5h' if period == '5h' else 'week'} share"` with a lookup `_SHARE_PERIOD = {"5h": "5h", "7d": "week", "day": "today"}` defined next to `_PERIOD`, i.e. `label = _SHARE_PERIOD.get(period, period)` and `f"{_SHARE_PERIOD.get(period, period)} share"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tickets_limits.py tests/test_limits.py tests/test_review_limits.py tests/test_web.py -q`
Expected: all PASS. The `07:55 UTC` assertion depends on `NOW = 1_800_000_000` being 08:00:00 UTC and the fixture granting at `NOW - 300`; check with `python3 -c "import time; print(time.gmtime(1_800_000_000))"` if it fails and fix the expected string, not the code.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/limits.py src/claude_proxy/web.py tests/test_tickets_limits.py
git commit -m "Limits: ticket-gated users get share_5h and share_day from their ticket, 403 without one

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Deleting users with tickets

**Files:**
- Modify: `src/claude_proxy/db.py` (`delete_user`), `src/claude_proxy/web.py` (delete action), `src/claude_proxy/cli.py` (`user delete --yes`)
- Test: `tests/test_tickets_delete.py`; update `tests/test_web.py::test_delete_only_revoked_users_and_their_history` to send the confirmation

**Interfaces:**
- `db.delete_user(conn, user_id, actor=None, now=None)` raises `ValueError("Cancel the user's active or queued ticket first.")` for a live ticket.
- `POST /api/admin/users/{uid}/delete` needs body `{"confirm": "<exact user name>"}`; 400 `Type the user's name to confirm.` otherwise.
- `claude-proxy user delete <user> [--yes]` asks `Type the user's name to confirm deletion: ` unless `--yes`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tickets_delete.py
import pytest

from claude_proxy import cli, db as dbm, tickets
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client, make_gateway
from tests.test_web import PW, admin_client
from tests.tickets_helpers import DAY, NOW, seeded, user


def test_delete_refused_with_a_live_ticket_and_keeps_records_after(db):
    conn, cfg, ids = seeded(db)
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW)
    dbm.revoke(conn, ids["alice"])
    with pytest.raises(ValueError, match="active or queued ticket"):
        dbm.delete_user(conn, ids["alice"], ids["admin"], now=NOW + 10)
    dbm.delete_user(conn, ids["alice"], ids["admin"], now=NOW + 2 * DAY)         # ended: allowed
    row = conn.execute("SELECT user_id, user_name FROM tickets WHERE id=?", (t["id"],)).fetchone()
    assert tuple(row) == (None, "alice")


async def test_web_delete_needs_the_typed_name(db, cfg):
    conn, tcfg_, ids = seeded(db)
    from argon2 import PasswordHasher
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (PasswordHasher().hash(PW), ids["admin"]))
    cfg.tickets = tcfg_.tickets
    gw = make_gateway(cfg, conn)
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW - 2 * DAY)
    dbm.revoke(conn, ids["alice"])
    async with admin_client(gw) as c:
        r = await c.post(f"/api/admin/users/{ids['alice']}/delete", json={})
        assert r.status_code == 400 and "Type the user's name" in r.json()["error"]
        r = await c.post(f"/api/admin/users/{ids['alice']}/delete", json={"confirm": "Alice"})
        assert r.status_code == 400
        r = await c.post(f"/api/admin/users/{ids['alice']}/delete", json={"confirm": "alice"})
        assert r.status_code == 200
    assert conn.execute("SELECT user_name FROM tickets WHERE id=?", (t["id"],)).fetchone()[0] == "alice"


def test_cli_delete_asks_for_the_name_unless_yes(db, monkeypatch, capsys):
    conn, cfg, ids = seeded(db)
    dbm.revoke(conn, ids["alice"])
    monkeypatch.setattr(cli, "_conn", lambda cfg: conn)
    monkeypatch.setattr("builtins.input", lambda prompt="": "nope")
    with pytest.raises(SystemExit):
        cli.main(["user", "delete", "alice"])
    assert conn.execute("SELECT 1 FROM users WHERE name='alice'").fetchone()
    monkeypatch.setattr("builtins.input", lambda prompt="": "alice")
    cli.main(["user", "delete", "alice"])
    assert conn.execute("SELECT 1 FROM users WHERE name='alice'").fetchone() is None
    bob, _ = dbm.create_user(conn, "bob")
    dbm.revoke(conn, bob)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("--yes must not prompt"))
    cli.main(["user", "delete", "bob", "--yes"])
    assert "Deleted bob" in capsys.readouterr().out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tickets_delete.py -q`
Expected: FAIL (`delete_user() got an unexpected keyword argument 'now'`, and the web delete returning 200 without confirmation).

- [ ] **Step 3: Implement**

`db.delete_user`:

```python
def delete_user(conn: sqlite3.Connection, user_id: int, actor: int | None = None, now: float | None = None) -> int:
    """Remove a revoked user and their usage history. Returns the number of requests deleted. Tickets stay as sales
    records with user_id NULL (the FK's ON DELETE SET NULL); a user with an active or queued ticket is not deleted."""
    now = time.time() if now is None else now
    u = conn.execute("SELECT name, revoked_at FROM users WHERE id=?", (user_id,)).fetchone()
    if u is None or u["revoked_at"] is None:
        raise ValueError("Only a revoked user can be deleted; revoke them first.")
    if conn.execute(f"SELECT 1 FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL AND {TICKET_EFFECTIVE_END}>? LIMIT 1",
                    (user_id, now)).fetchone():
        raise ValueError("Cancel the user's active or queued ticket first.")
    n = conn.execute("DELETE FROM requests WHERE user_id=?", (user_id,)).rowcount
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))   # limits and sessions cascade; tickets keep user_name
    audit(conn, actor, "delete_user", u["name"], {"deleted_requests": n})
    return n
```

`web.py`, in `user_action`, the `delete` branch:

```python
        if action == "delete":
            if str((await _json(request)).get("confirm", "")) != u["name"]:
                fail(400, "Type the user's name to confirm.")
            try:
                return {"ok": True, "deleted_requests": db.delete_user(conn, u["id"], actor["id"])}
            except ValueError as e:
                fail(400, str(e))
```

`cli.py`:

```python
def cmd_user_delete(args, cfg):
    conn = _conn(cfg)
    u = _user(conn, args.user)
    if not args.yes and input(f"Type the user's name to confirm deletion: ") != u["name"]:
        sys.exit("Names differ; nothing deleted.")
    try:
        n = db.delete_user(conn, u["id"])
    except ValueError as e:
        sys.exit(str(e))
    print(f"Deleted {u['name']} and {n} recorded requests.")
```

and in `main`: `s = u.add_parser("delete", help="remove a revoked user and their usage history"); s.add_argument("user"); s.add_argument("--yes", action="store_true", help="skip the typed confirmation"); s.set_defaults(func=cmd_user_delete)`.

In `tests/test_web.py::test_delete_only_revoked_users_and_their_history`, change both delete posts to `json={"confirm": "bob"}`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tickets_delete.py tests/test_web.py tests/test_device_cli.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/db.py src/claude_proxy/web.py src/claude_proxy/cli.py tests/test_tickets_delete.py tests/test_web.py
git commit -m "Users: deletion refused with a live ticket, typed confirmation, tickets kept as records

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Admin API for rates, prices and discounts

**Files:**
- Modify: `src/claude_proxy/web.py`
- Test: `tests/test_tickets_web.py`

**Interfaces:**
- Produces, all admin-only (`admin(request)`, writes with `write=True`), 404 `Tickets are not enabled on this gateway.` when `cfg.tickets.enabled` is false:
  - `GET /api/admin/rates` → `{"rates": [{currency, round_to, rate, set_at, set_by, stale}]}` (configured currencies only, USD left out).
  - `POST /api/admin/rates {currency, rate}` → `{"ok": true, "rate": {...}}`.
  - `GET /api/admin/prices` → `{"tiers": {id: {label, share_pct, compare}}, "lengths": {day: 1, ...}, "prices": [...], "discounts": [...]}` (all discounts, cancelled and ended included).
  - `POST /api/admin/prices {tier, length, usd}` → `{"ok": true}`.
  - `POST /api/admin/discounts {tier, length, usd, starts_at, ends_at}` → `{"ok": true, "discount": {...}}`; `POST /api/admin/discounts/{id}/cancel` → `{"ok": true}`.
  - `GET /api/session` gains `"tickets": {"enabled": bool}` for everyone, plus `tiers`, `currencies`, `lengths`, `max_sold_pct`, `how_to_buy` for admins.
- Helper `ticket_call(fn, *args, **kw)` maps `CapacityError` → 409 and `TicketError` → 400; `need_tickets()` raises the 404.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tickets_web.py
import time

import pytest
from argon2 import PasswordHasher

from claude_proxy import db as dbm, tickets
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client, make_gateway
from tests.test_web import PW, admin_client, bearer
from tests.tickets_helpers import DAY, seeded, user


@pytest.fixture
def env(db, cfg):
    conn, tc, ids = seeded(db)
    cfg.tickets = tc.tickets
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (PasswordHasher().hash(PW), ids["admin"]))
    alice_key = dbm.rotate_key(conn, ids["alice"])                 # a key we know, for the user's own endpoints
    gw = make_gateway(cfg, conn)
    return gw, conn, cfg, ids, {"alice": alice_key}


def last_audit(conn):
    return tuple(conn.execute("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1").fetchone())


async def test_rates_panel_and_setting_a_rate(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        r = (await c.get("/api/admin/rates")).json()["rates"]
        assert r == [{"currency": "EUR", "round_to": 0.5, "rate": 0.92, "set_at": r[0]["set_at"], "set_by": "admin", "stale": False}]
        assert (await c.post("/api/admin/rates", json={"currency": "eur", "rate": 0.95})).status_code == 200
        assert (await c.get("/api/admin/rates")).json()["rates"][0]["rate"] == 0.95
        assert (await c.post("/api/admin/rates", json={"currency": "GBP", "rate": 0.8})).status_code == 400
        assert (await c.post("/api/admin/rates", json={"currency": "EUR", "rate": -1})).status_code == 400
    assert last_audit(conn) == ("rate_set", "EUR")
    assert conn.execute("SELECT COUNT(*) FROM fx_rates WHERE currency='GBP'").fetchone()[0] == 0


async def test_stale_rate_is_flagged(env):
    gw, conn, cfg, ids, keys = env
    conn.execute("UPDATE fx_rates SET set_at=?", (int(time.time()) - 40 * 3600,))
    async with admin_client(gw) as c:
        assert (await c.get("/api/admin/rates")).json()["rates"][0]["stale"] is True


async def test_prices_grid_and_discounts(env):
    gw, conn, cfg, ids, keys = env
    now = int(time.time())
    async with admin_client(gw) as c:
        p = (await c.get("/api/admin/prices")).json()
        assert p["tiers"]["lite"] == {"label": "Lite", "share_pct": 5, "compare": "Claude Pro"} and p["lengths"] == {"day": 1, "week": 7, "month": 30}
        assert {(x["tier"], x["length"]): x["usd"] for x in p["prices"]}[("standard", "month")] == 100
        assert (await c.post("/api/admin/prices", json={"tier": "lite", "length": "week", "usd": 9})).status_code == 200
        assert last_audit(conn) == ("price_set", "lite:week")
        assert (await c.post("/api/admin/prices", json={"tier": "lite", "length": "year", "usd": 9})).status_code == 400
        r = await c.post("/api/admin/discounts", json={"tier": "lite", "length": "week", "usd": 6, "starts_at": now, "ends_at": now + DAY})
        assert r.status_code == 200 and r.json()["discount"]["usd"] == 6
        did = r.json()["discount"]["id"]
        assert last_audit(conn) == ("discount_create", "lite:week")
        r = await c.post("/api/admin/discounts", json={"tier": "lite", "length": "week", "usd": 5, "starts_at": now + 10, "ends_at": now + 20})
        assert r.status_code == 400 and "already covers" in r.json()["error"]
        assert (await c.post("/api/admin/discounts", json={"tier": "lite", "length": "week", "usd": 9, "starts_at": now, "ends_at": "soon"})).status_code == 400
        assert (await c.post(f"/api/admin/discounts/{did}/cancel")).status_code == 200
        assert last_audit(conn) == ("discount_cancel", "lite:week")
        assert (await c.post(f"/api/admin/discounts/{did}/cancel")).status_code == 400
        assert [d["id"] for d in (await c.get("/api/admin/prices")).json()["discounts"]] == [did]


async def test_ticket_endpoints_are_admin_only_and_off_without_config(env):
    gw, conn, cfg, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/admin/rates", headers=bearer(keys["alice"]))).status_code == 403
        assert (await c.post("/api/admin/prices", headers=bearer(keys["alice"]), json={})).status_code == 403
    cfg.tickets.enabled = False
    async with admin_client(gw) as c:
        assert (await c.get("/api/admin/rates")).status_code == 404
        assert (await c.get("/api/session")).json()["tickets"] == {"enabled": False}


async def test_session_carries_the_ticket_settings_for_admins_only(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        t = (await c.get("/api/session")).json()["tickets"]
    assert t["enabled"] and t["max_sold_pct"] == 80 and t["currencies"] == ["USD", "EUR"] and t["lengths"] == {"day": 1, "week": 7, "month": 30}
    assert t["tiers"]["standard"] == {"label": "Standard", "share_pct": 25, "compare": "Claude Max 5x"} and t["how_to_buy"].startswith("Send")
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/session", headers=bearer(keys["alice"]))).json()["tickets"] == {"enabled": True}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tickets_web.py -q`
Expected: FAIL with 404s from the missing routes (`assert 404 == 200`).

- [ ] **Step 3: Implement**

In `web.py`: import `from . import clerk, db, limits, quota, tickets, usage` and `from .config import LENGTHS`. In `session()`, before `return out`:

```python
        out["tickets"] = {"enabled": cfg.tickets.enabled}
        if is_admin(user) and cfg.tickets.enabled:
            out["tickets"] |= {"tiers": {k: {"label": t.label, "share_pct": t.share_pct, "compare": t.compare} for k, t in cfg.tickets.tiers.items()},
                               "currencies": list(tickets.currencies(cfg)), "lengths": LENGTHS,
                               "max_sold_pct": cfg.tickets.max_sold_pct, "how_to_buy": cfg.tickets.how_to_buy}
```

Add a new section before `# ---------- page ----------`:

```python
    # ---------- paid tickets (design 2026-10-03) ----------

    def ticket_call(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except tickets.CapacityError as e:
            fail(409, str(e))
        except tickets.TicketError as e:
            fail(400, str(e))

    def need_tickets():
        if not cfg.tickets.enabled:
            fail(404, "Tickets are not enabled on this gateway.")

    @app.get("/api/admin/rates")
    async def rates(request: Request):
        admin(request)
        need_tickets()
        now, n, out = time.time(), names(), []
        for code, step in tickets.currencies(cfg).items():
            if code == "USD":
                continue
            r = tickets.current_rate(conn, code)
            out.append({"currency": code, "round_to": step, "rate": r["rate"] if r else None, "set_at": r["set_at"] if r else None,
                        "set_by": n.get(r["set_by"]) if r and r["set_by"] else None, "stale": bool(r and tickets.rate_is_stale(r, now))})
        return {"rates": out}

    @app.post("/api/admin/rates")
    async def set_rate(request: Request):
        actor = admin(request, write=True)
        need_tickets()
        body = await _json(request)
        return {"ok": True, "rate": ticket_call(tickets.set_rate, conn, cfg, str(body.get("currency", "")).upper(), body.get("rate"), actor["id"])}

    @app.get("/api/admin/prices")
    async def prices(request: Request):
        admin(request)
        need_tickets()
        return {"tiers": {k: {"label": t.label, "share_pct": t.share_pct, "compare": t.compare} for k, t in cfg.tickets.tiers.items()},
                "lengths": LENGTHS, "prices": tickets.prices(conn), "discounts": tickets.discounts(conn, time.time(), include_ended=True)}

    @app.post("/api/admin/prices")
    async def set_price(request: Request):
        actor = admin(request, write=True)
        need_tickets()
        body = await _json(request)
        ticket_call(tickets.set_price, conn, cfg, str(body.get("tier", "")), str(body.get("length", "")), body.get("usd"), actor["id"])
        return {"ok": True}

    @app.post("/api/admin/discounts")
    async def create_discount(request: Request):
        actor = admin(request, write=True)
        need_tickets()
        body = await _json(request)
        try:
            starts_at, ends_at = int(body.get("starts_at")), int(body.get("ends_at"))
        except (TypeError, ValueError):
            fail(400, "Need starts_at and ends_at as epoch seconds.")
        d = ticket_call(tickets.create_discount, conn, cfg, str(body.get("tier", "")), str(body.get("length", "")), body.get("usd"),
                        starts_at, ends_at, actor["id"])
        return {"ok": True, "discount": d}

    @app.post("/api/admin/discounts/{did}/cancel")
    async def cancel_discount(request: Request, did: int):
        actor = admin(request, write=True)
        need_tickets()
        ticket_call(tickets.cancel_discount, conn, did, actor["id"])
        return {"ok": True}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tickets_web.py tests/test_web.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/web.py tests/test_tickets_web.py
git commit -m "Dashboard API: exchange rates, ticket prices and discounts

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Admin API for tickets, capacity and Ungate

**Files:**
- Modify: `src/claude_proxy/web.py`
- Test: `tests/test_tickets_web.py` (append)

**Interfaces:**
- `GET /api/admin/tickets?user_id=` → `{"tickets": [ticket + state ("active"|"queued"|"ended"|"cancelled"), granted_by_name, bonuses]}` newest first, at most 500.
- `POST /api/admin/tickets/preview {user, tier, length, currency}` → the `tickets.preview` dict.
- `POST /api/admin/tickets {user, tier, length, currency, note, remove_limits: [{kind, scope}], confirm_stale_rate}` → `{"ok": true, "ticket": {...}}`; 409 when the slot is full.
- `POST /api/admin/tickets/{id}/cancel` → `{"ok": true, "ticket", "moved", "dates_kept", "reason"}`.
- `POST /api/admin/tickets/{id}/bonus {share_pct, extra_days, starts_at, ends_at, note}` → `{"ok": true, "bonus", "ticket", "moved"}`.
- `GET /api/admin/capacity` → `{"sold_now_pct", "peak_30d_pct", "max_sold_pct", "utilization": {"5h": {utilization_pct, stale}, "7d": {...}}}`.
- `POST /api/admin/users/{uid}/ungate` → `{"ok": true}`.
- `GET /api/users` rows gain `"ticket": {"gated", "live", "current", "queued"}` (None when tickets are off).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tickets_web.py`)

```python
async def test_preview_grant_list_and_users_state(env):
    gw, conn, cfg, ids, keys = env
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (ids["alice"], "cost_total", "*", "5", "usd"))
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (ids["alice"], "tokens_daily", "*", "9", "weighted"))
    async with admin_client(gw) as c:
        p = (await c.post("/api/admin/tickets/preview", json={"user": "alice", "tier": "lite", "length": "week", "currency": "EUR"})).json()
        assert (p["usd"], p["amount"], p["available"], p["queued"], p["credit"], p["first_ticket"]) == (8, 7.5, True, False, True, True)
        assert [r["kind"] for r in p["limit_rows"]] == ["tokens_daily"]
        r = await c.post("/api/admin/tickets", json={"user": "alice", "tier": "lite", "length": "week", "currency": "EUR", "note": "paid 7.50",
                                                      "remove_limits": [{"kind": "tokens_daily", "scope": "*"}]})
        assert r.status_code == 200, r.text
        t = r.json()["ticket"]
        assert (t["user_name"], t["amount"], t["note"]) == ("alice", 7.5, "paid 7.50")
        assert conn.execute("SELECT COUNT(*) FROM limits WHERE user_id=?", (ids["alice"],)).fetchone()[0] == 0
        lst = (await c.get(f"/api/admin/tickets?user_id={ids['alice']}")).json()["tickets"]
        assert lst[0]["id"] == t["id"] and lst[0]["state"] == "active" and lst[0]["granted_by_name"] == "admin" and lst[0]["bonuses"] == []
        users = {u["name"]: u for u in (await c.get("/api/users")).json()["users"]}
        assert users["alice"]["ticket"]["gated"] and users["alice"]["ticket"]["live"] and users["alice"]["ticket"]["current"]["id"] == t["id"]
        assert users["admin"]["ticket"] == {"gated": False, "live": False, "current": None, "queued": None}
        # A second grant queues after the first; the preview says so.
        p = (await c.post("/api/admin/tickets/preview", json={"user": "alice", "tier": "lite", "length": "day", "currency": "USD"})).json()
        assert p["queued"] is True and p["starts_at"] == t["ends_at"]
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions[-3:] == ["credit_removed", "limit_clear", "ticket_grant"]


async def test_grant_refusals_map_to_400_and_409(env):
    gw, conn, cfg, ids, keys = env
    cfg.tickets.max_sold_pct = 5
    async with admin_client(gw) as c:
        assert (await c.post("/api/admin/tickets", json={"user": "alice", "tier": "lite", "length": "week", "currency": "EUR"})).status_code == 200
        bob = (await c.post("/api/admin/users", json={"name": "bob"})).json()["id"]
        r = await c.post("/api/admin/tickets", json={"user": bob, "tier": "lite", "length": "day", "currency": "USD"})
        assert r.status_code == 409 and "Not enough capacity" in r.json()["error"]
        assert (await c.post("/api/admin/tickets", json={"user": "nobody", "tier": "lite", "length": "day", "currency": "USD"})).status_code == 404
        assert (await c.post("/api/admin/tickets", json={"user": bob, "tier": "gold", "length": "day", "currency": "USD"})).status_code == 400
        conn.execute("UPDATE fx_rates SET set_at=?", (int(time.time()) - 40 * 3600,))
        cfg.tickets.max_sold_pct = 80
        r = await c.post("/api/admin/tickets", json={"user": bob, "tier": "lite", "length": "day", "currency": "EUR"})
        assert r.status_code == 400 and "hours old" in r.json()["error"]
        r = await c.post("/api/admin/tickets", json={"user": bob, "tier": "lite", "length": "day", "currency": "EUR", "confirm_stale_rate": True})
        assert r.status_code == 200


async def test_cancel_bonus_capacity_and_ungate(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        t = (await c.post("/api/admin/tickets", json={"user": "alice", "tier": "standard", "length": "week", "currency": "USD"})).json()["ticket"]
        r = await c.post(f"/api/admin/tickets/{t['id']}/bonus", json={"share_pct": 5, "extra_days": 1, "note": "welcome"})
        assert r.status_code == 200 and r.json()["bonus"]["note"] == "welcome" and r.json()["ticket"]["effective_end"] == t["ends_at"] + DAY
        assert (await c.post(f"/api/admin/tickets/{t['id']}/bonus", json={"share_pct": 0, "extra_days": 0})).status_code == 400
        assert (await c.post(f"/api/admin/tickets/{t['id']}/bonus", json={"share_pct": "five"})).status_code == 400
        cap = (await c.get("/api/admin/capacity")).json()
        assert (cap["sold_now_pct"], cap["peak_30d_pct"], cap["max_sold_pct"]) == (30, 30, 80) and set(cap["utilization"]) == {"5h", "7d"}
        assert (await c.post(f"/api/admin/users/{ids['alice']}/ungate")).status_code == 400          # live ticket
        r = await c.post(f"/api/admin/tickets/{t['id']}/cancel")
        assert r.status_code == 200 and r.json()["moved"] == 0 and r.json()["dates_kept"] is False
        assert (await c.get("/api/admin/capacity")).json()["sold_now_pct"] == 0
        assert (await c.post(f"/api/admin/tickets/{t['id']}/cancel")).status_code == 400
        assert (await c.post(f"/api/admin/users/{ids['alice']}/ungate")).status_code == 200
        users = {u["name"]: u for u in (await c.get("/api/users")).json()["users"]}
        assert users["alice"]["ticket"]["gated"] is False
        assert (await c.get("/api/admin/tickets")).json()["tickets"][0]["state"] == "cancelled"
    assert [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action IN ('ticket_bonus','ticket_cancel','ungate') ORDER BY id")] == ["ticket_bonus", "ticket_cancel", "ungate"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tickets_web.py -q -k "preview or refusals or cancel_bonus"`
Expected: FAIL with 404s.

- [ ] **Step 3: Implement**

In `web.py`, after the discounts endpoints of Task 9:

```python
    def ticket_state(t: dict, now: float) -> str:
        if t["cancelled_at"] is not None:
            return "cancelled"
        if t["effective_end"] <= now:
            return "ended"
        return "queued" if t["starts_at"] > now else "active"

    @app.get("/api/admin/tickets")
    async def tickets_list(request: Request, user_id: int | None = None):
        admin(request)
        need_tickets()
        now, n = time.time(), names()
        where, params = ("WHERE t.user_id=?", (user_id,)) if user_id is not None else ("", ())
        rows = [dict(r) for r in conn.execute(f"SELECT {tickets.TICKET_COLS} FROM tickets t {where} ORDER BY t.starts_at DESC, t.id DESC LIMIT 500", params)]
        for t in rows:
            t |= {"state": ticket_state(t, now), "granted_by_name": n.get(t["granted_by"]), "bonuses": tickets.bonuses(conn, t["id"])}
        return {"tickets": rows}

    def grant_args(body) -> tuple:
        return (target_user(body.get("user") or body.get("user_id")), str(body.get("tier", "")), str(body.get("length", "")),
                str(body.get("currency") or "USD").upper())

    @app.post("/api/admin/tickets/preview")
    async def ticket_preview(request: Request):
        admin(request)
        need_tickets()
        u, tier, length, currency = grant_args(await _json(request))
        return ticket_call(tickets.preview, conn, cfg, u, tier, length, currency, time.time())

    @app.post("/api/admin/tickets")
    async def ticket_grant(request: Request):
        actor = admin(request, write=True)
        need_tickets()
        body = await _json(request)
        u, tier, length, currency = grant_args(body)
        remove = [(str(r.get("kind", "")), str(r.get("scope") or "*")) for r in body.get("remove_limits") or [] if isinstance(r, dict)]
        t = ticket_call(tickets.grant, conn, cfg, actor["id"], u, tier, length, currency, note=str(body.get("note") or ""),
                        remove_limits=remove, confirm_stale_rate=bool(body.get("confirm_stale_rate")))
        return {"ok": True, "ticket": t}

    @app.post("/api/admin/tickets/{tid}/cancel")
    async def ticket_cancel(request: Request, tid: int):
        actor = admin(request, write=True)
        need_tickets()
        return {"ok": True, **ticket_call(tickets.cancel, conn, cfg, actor["id"], tid)}

    @app.post("/api/admin/tickets/{tid}/bonus")
    async def ticket_bonus(request: Request, tid: int):
        actor = admin(request, write=True)
        need_tickets()
        body = await _json(request)
        try:
            share = float(body.get("share_pct") or 0)
            days = int(body.get("extra_days") or 0)
            starts_at = None if body.get("starts_at") in (None, "") else int(body["starts_at"])
            ends_at = None if body.get("ends_at") in (None, "") else int(body["ends_at"])
        except (TypeError, ValueError):
            fail(400, "share_pct, extra_days, starts_at and ends_at must be numbers.")
        r = ticket_call(tickets.add_bonus, conn, cfg, actor["id"], tid, share_pct=share, extra_days=days, starts_at=starts_at, ends_at=ends_at,
                        note=str(body.get("note") or ""))
        return {"ok": True, **r}

    @app.get("/api/admin/capacity")
    async def capacity(request: Request):
        admin(request)
        need_tickets()
        now = time.time()
        util = {}
        for b in BUCKETS:
            att = quota.attribution(conn, cfg.pricing, b, now=now, stale_after_s=cfg.quota.stale_after_s)
            util[b] = {"utilization_pct": att["utilization_pct"], "stale": att["stale"]}
        return {**tickets.capacity(conn, cfg, now), "utilization": util}
```

In `user_action`, before `if action == "enable":`:

```python
        if action == "ungate":
            need_tickets()
            ticket_call(tickets.ungate, conn, actor["id"], u["id"])
            return {"ok": True}
```

(`need_tickets` and `ticket_call` are defined later in the same closure; Python resolves them at call time, so the order of definition inside `create_dashboard_app` does not matter.)

In `users()`, add to each row: `"ticket": tickets.user_state(conn, u["id"], now) if cfg.tickets.enabled else None`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tickets_web.py tests/test_web.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/web.py tests/test_tickets_web.py
git commit -m "Dashboard API: grant, list, cancel and bonus tickets, capacity panel, Ungate

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Usage estimates and the daily maintenance task

**Files:**
- Create: `src/claude_proxy/estimates.py`
- Modify: `src/claude_proxy/cli.py`
- Test: `tests/test_estimates.py`

**Interfaces:**
- Produces, in `estimates.py`: `FAMILIES = {"sonnet": "claude-sonnet-*", "opus": "claude-opus-*"}`, `MIN_BUSY_HOURS = 50`, `BUSY_SPREAD_S = 1200`, `BUSY_REQUESTS = 10`; `busy_hours(conn, since, now) -> set[tuple[int, int]]` of `(user_id, hour_start)`; `refresh(conn, pricing, now=None) -> dict[str, int]` (writes `usage_estimates`, returns `{"sonnet/5h": busy_hours, ...}`); `refresh_if_due(conn, cfg, now=None) -> dict | None` (runs when no row is newer than 23 hours and tickets are enabled); `hours_hint(conn, share_pct) -> dict` of `{family: {"per_5h": float | None, "per_day": float | None}}`.
- `cli._maintenance` replaces `_retention`: cleanup, then `estimates.refresh_if_due`, every 6 hours.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_estimates.py
import pytest

from claude_proxy import estimates
from claude_proxy.config import Pricing
from claude_proxy.db import create_user
from tests.tickets_helpers import DAY, NOW

H = 3600
H0 = (NOW // H) * H - 48 * H     # three clock hours well inside the 30-day window


def req(conn, uid, t, model, i=1000):
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, input_tokens) VALUES(?,?,?,?,?,?,?,?)",
                 (uid, t, t + 1, "POST", "/v1/messages", "anthropic", model, i))


def snaps(conn, t, util):
    for bucket in ("5h", "7d"):
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                     (t, "header", bucket, util, NOW + 10 * DAY))


@pytest.fixture
def filled(db):
    conn = db[1]
    a, _ = create_user(conn, "a")
    snaps(conn, H0, 0)
    req(conn, a, H0 + 60, "claude-sonnet-5")             # hour 0: two Sonnet requests 25 minutes apart -> busy
    req(conn, a, H0 + 60 + 25 * 60, "claude-sonnet-5")
    snaps(conn, H0 + H - 1, 2)                           # +2 points in hour 0
    req(conn, a, H0 + H + 60, "claude-opus-5")           # hour 1: one request -> not busy
    snaps(conn, H0 + 2 * H - 1, 3)
    for k in range(10):                                  # hour 2: ten Opus requests in five minutes, and one Sonnet -> busy, mixed
        req(conn, a, H0 + 2 * H + 30 * k, "claude-opus-5")
    req(conn, a, H0 + 2 * H + 400, "claude-sonnet-5")
    snaps(conn, H0 + 3 * H - 1, 14)                      # +11 points in hour 2
    return conn, a


def test_busy_hours_need_spread_or_count(filled):
    conn, a = filled
    assert estimates.busy_hours(conn, H0 - DAY, NOW) == {(a, H0), (a, H0 + 2 * H)}


def test_refresh_records_share_per_busy_hour_per_family(filled):
    conn, a = filled
    out = estimates.refresh(conn, Pricing(), now=NOW)
    assert out == {"sonnet/5h": 2, "opus/5h": 1, "sonnet/7d": 2, "opus/7d": 1}
    rows = {(r["family"], r["bucket"]): r for r in conn.execute("SELECT * FROM usage_estimates")}
    # Hour 2: 11 points split by weighted tokens, Opus input at $5 vs Sonnet at $2: 10 × 2500 vs 1 × 1000.
    sonnet_hour2 = 11 * 1000 / 26000
    assert rows[("opus", "5h")]["p75_share_per_hour"] == pytest.approx(11 * 25000 / 26000)          # one value: itself
    assert rows[("sonnet", "5h")]["p75_share_per_hour"] == pytest.approx(sonnet_hour2 + 0.75 * (2 - sonnet_hour2))   # 75th of [0.42, 2]
    assert rows[("sonnet", "7d")]["busy_hours"] == 2 and rows[("sonnet", "7d")]["computed_at"] == NOW


def test_refresh_with_no_data_writes_zero_rows_and_hints_stay_hidden(db):
    conn = db[1]
    assert estimates.refresh(conn, Pricing(), now=NOW) == {"sonnet/5h": 0, "opus/5h": 0, "sonnet/7d": 0, "opus/7d": 0}
    assert estimates.hours_hint(conn, 5) == {"sonnet": {"per_5h": None, "per_day": None}, "opus": {"per_5h": None, "per_day": None}}


def test_hours_hint_is_a_floor_and_hidden_below_50_busy_hours(db):
    conn = db[1]
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (NOW, "sonnet", "5h", 50, 1.3))     # 5 / 1.3 = 3.846 -> 3.8
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (NOW, "sonnet", "7d", 50, 0.3))     # (5/7) / 0.3 = 2.38 -> 2.3
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (NOW, "opus", "5h", 49, 4.0))       # too little data
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (NOW, "opus", "7d", 60, 0.0))       # no share measured
    assert estimates.hours_hint(conn, 5) == {"sonnet": {"per_5h": 3.8, "per_day": 2.3}, "opus": {"per_5h": None, "per_day": None}}


def test_refresh_if_due_runs_once_a_day_and_only_with_tickets(db):
    from tests.tickets_helpers import tcfg
    conn, cfg = db[1], tcfg()
    assert estimates.refresh_if_due(conn, cfg, now=NOW) is not None
    assert estimates.refresh_if_due(conn, cfg, now=NOW + 6 * 3600) is None
    assert estimates.refresh_if_due(conn, cfg, now=NOW + 24 * 3600) is not None
    cfg.tickets.enabled = False
    assert estimates.refresh_if_due(conn, cfg, now=NOW + 3 * DAY) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_estimates.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'claude_proxy.estimates'`.

- [ ] **Step 3: Implement**

`src/claude_proxy/estimates.py`:

```python
"""Lower-bound usage hints for /pricing (paid-tickets design, section 9).

Once a day: over the last 30 days, find each user's busy clock hours (UTC), attribute each hour's share of the 5-hour
and weekly buckets to the model family of its requests, and keep the 75th percentile of share per busy hour per
family. A tier's hint is its share divided by that: "at least N hours of steady use", a figure a heavy user will reach.
"""
from __future__ import annotations

import fnmatch
import math
import sqlite3
import statistics
import time

from . import quota
from .config import Config, Pricing
from .forwarder import COUNT_TOKENS_PATH

DAY, HOUR = 86400, 3600
FAMILIES = {"sonnet": "claude-sonnet-*", "opus": "claude-opus-*"}
BUCKETS = ("5h", "7d")
MIN_BUSY_HOURS = 50      # below this a family's hint is hidden
BUSY_SPREAD_S = 20 * 60  # a busy hour: first to last request at least this far apart ...
BUSY_REQUESTS = 10       # ... or at least this many requests
WINDOW_DAYS = 30
REFRESH_AFTER_S = 23 * HOUR


def family(model: str | None) -> str | None:
    m = (model or "").lower()
    return next((f for f, pat in FAMILIES.items() if fnmatch.fnmatchcase(m, pat)), None)


def busy_hours(conn: sqlite3.Connection, since: float, now: float) -> set[tuple[int, int]]:
    """(user_id, hour_start) for every UTC clock hour with 20 minutes between a user's first and last forwarded
    Anthropic request, or at least 10 of them. Single short bursts are left out: they would make an hour look cheap."""
    rows = conn.execute(
        f"SELECT user_id, CAST(started_at / {HOUR} AS INTEGER) * {HOUR} AS h, COUNT(*) AS n, MAX(started_at) - MIN(started_at) AS spread "
        f"FROM requests WHERE provider='anthropic' AND rejected_by IS NULL AND path!=? AND model IS NOT NULL AND user_id IS NOT NULL "
        f"AND started_at>=? AND started_at<? GROUP BY user_id, h HAVING spread>=? OR n>=?",
        (COUNT_TOKENS_PATH, since, now, BUSY_SPREAD_S, BUSY_REQUESTS)).fetchall()
    return {(r["user_id"], int(r["h"])) for r in rows}


def _p75(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=4, method="inclusive")[2]


def refresh(conn: sqlite3.Connection, pricing: Pricing, now: float | None = None) -> dict[str, int]:
    """Recompute usage_estimates from the last 30 days. Returns busy hours per family and bucket."""
    now = int(time.time() if now is None else now)
    since = now - WINDOW_DAYS * DAY
    hours = busy_hours(conn, since, now)
    meta = {r["id"]: (r["user_id"], int(r["started_at"] // HOUR) * HOUR, family(r["model"])) for r in conn.execute(
        "SELECT id, user_id, started_at, model FROM requests WHERE provider='anthropic' AND rejected_by IS NULL AND path!=? "
        "AND model IS NOT NULL AND started_at>=?", (COUNT_TOKENS_PATH, since))}
    out = {}
    for bucket in BUCKETS:
        per: dict[tuple, float] = {}   # (user_id, hour, family) -> points of this bucket
        for rid, _uid, _model, _ended, points in quota.request_shares(conn, pricing, bucket, since, now):
            m = meta.get(rid)
            if m is None or m[2] is None or (m[0], m[1]) not in hours:
                continue
            per[m] = per.get(m, 0.0) + points
        for fam in FAMILIES:
            values = [v for (_, _, f), v in per.items() if f == fam]
            conn.execute("INSERT OR REPLACE INTO usage_estimates(computed_at, family, bucket, busy_hours, p75_share_per_hour) VALUES(?,?,?,?,?)",
                         (now, fam, bucket, len(values), _p75(values)))
            out[f"{fam}/{bucket}"] = len(values)
    return out


def refresh_if_due(conn: sqlite3.Connection, cfg: Config, now: float | None = None) -> dict[str, int] | None:
    """Called by the maintenance loop every few hours; recomputes at most once a day, and only when tickets are on."""
    if not cfg.tickets.enabled:
        return None
    now = time.time() if now is None else now
    last = conn.execute("SELECT MAX(computed_at) FROM usage_estimates").fetchone()[0]
    if last is not None and now - last < REFRESH_AFTER_S:
        return None
    return refresh(conn, cfg.pricing, now)


def _hours(share: float, row) -> float | None:
    if row is None or row["busy_hours"] < MIN_BUSY_HOURS or not row["p75_share_per_hour"]:
        return None
    return math.floor(share / row["p75_share_per_hour"] * 10) / 10   # one decimal, rounded down: "at least"


def hours_hint(conn: sqlite3.Connection, share_pct: float) -> dict:
    """Per family: hours of steady use a tier share buys per 5-hour window and per ticket day (share ÷ 7 against the
    weekly bucket), or None while the family has under 50 busy hours of data."""
    rows = {(r["family"], r["bucket"]): r for r in conn.execute("SELECT * FROM usage_estimates")}
    return {fam: {"per_5h": _hours(share_pct, rows.get((fam, "5h"))), "per_day": _hours(share_pct / 7, rows.get((fam, "7d")))}
            for fam in FAMILIES}
```

In `cli.py`: import `from . import db, estimates, limits`; rename `_retention` to `_maintenance` (and its `create_task` call), and after the cleanup `if`:

```python
            done = estimates.refresh_if_due(conn, cfg)
            if done:
                logger.info("usage estimates refreshed: %s", done)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_estimates.py tests/test_device_cli.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/estimates.py src/claude_proxy/cli.py tests/test_estimates.py
git commit -m "Estimates: busy-hour usage hints recomputed daily by the maintenance loop

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Public pricing page and the user's ticket endpoint

**Files:**
- Modify: `src/claude_proxy/tickets.py` (`price_table`), `src/claude_proxy/web.py`
- Create: `src/claude_proxy/static/pricing.html`, `src/claude_proxy/static/pricing.js`
- Test: `tests/test_tickets_web.py` (append)

**Interfaces:**
- `tickets.price_table(conn, cfg, now, currency) -> dict`: `{"currency", "rate_set_at", "how_to_buy", "tiers": [{tier, label, share_pct, compare, hours: hours_hint(...), lengths: {day: {days, list_usd, usd, discount_ends_at, amount, list_amount, sold_out}, ...}}]}`. Falls back to USD when the currency has no rate.
- `GET /api/pricing` (no sign-in) → `price_table` in the first configured currency; 404 when tickets are off.
- `GET /pricing` → `static/pricing.html` with versioned asset URLs; 404 when tickets are off.
- `GET /api/me/tickets` (any signed-in user) → `{"enabled", "gated", "current", "queued", "how_to_buy", "prices"}`; `current` has `id, tier, label, share_pct, starts_at, ends_at, effective_end, bonus_days, day_end, bonus_share, bonuses: [{share_pct, note, ends_at}]`; `queued` has `id, tier, label, starts_at, effective_end`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tickets_web.py`)

```python
async def test_pricing_api_is_public_and_says_only_prices_and_sold_out(env):
    gw, conn, cfg, ids, keys = env
    now = int(time.time())
    tickets.create_discount(conn, cfg, "lite", "month", 15, now - 10, now + DAY, ids["admin"], now=now)
    cfg.tickets.max_sold_pct = 25
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "standard", "day", "USD", now=now)
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/api/pricing")
        assert r.status_code == 200
        p = r.json()
    assert p["currency"] == "EUR" and p["rate_set_at"] is not None and p["how_to_buy"].startswith("Send")
    lite = next(t for t in p["tiers"] if t["tier"] == "lite")
    assert lite["compare"] == "Claude Pro" and lite["lengths"]["month"] == {
        "days": 30, "list_usd": 20, "usd": 15, "discount_ends_at": now + DAY, "amount": 14.0, "list_amount": 18.5, "sold_out": True}   # 25 sold: nothing fits today
    assert lite["lengths"]["week"]["sold_out"] is True and lite["lengths"]["week"]["discount_ends_at"] is None
    assert lite["hours"] == {"sonnet": {"per_5h": None, "per_day": None}, "opus": {"per_5h": None, "per_day": None}}
    text = r.text
    for leaked in ("utilization", "alice", "sold_now", "shares"):
        assert leaked not in text


async def test_pricing_falls_back_to_usd_without_a_rate(env):
    gw, conn, cfg, ids, keys = env
    conn.execute("DELETE FROM fx_rates")
    async with asgi_client(create_dashboard_app(gw)) as c:
        p = (await c.get("/api/pricing")).json()
        page = await c.get("/pricing")
    assert p["currency"] == "USD" and p["rate_set_at"] is None
    assert next(t for t in p["tiers"] if t["tier"] == "lite")["lengths"]["day"]["amount"] == 3.0
    assert page.status_code == 200 and "pricing.js?v=" in page.text and "app.css?v=" in page.text
    assert page.headers["content-security-policy"].startswith("default-src 'self'")


async def test_pricing_reads_only_usage_estimates(env):
    gw, conn, cfg, ids, keys = env
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (int(time.time()), "sonnet", "5h", 80, 1.25))
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (int(time.time()), "sonnet", "7d", 80, 0.25))
    conn.execute("INSERT INTO usage_estimates VALUES(?,?,?,?,?)", (int(time.time()), "opus", "5h", 10, 5.0))
    conn.execute("DELETE FROM requests")
    conn.execute("DELETE FROM quota_snapshots")
    async with asgi_client(create_dashboard_app(gw)) as c:
        p = (await c.get("/api/pricing")).json()
    lite = next(t for t in p["tiers"] if t["tier"] == "lite")
    assert lite["hours"] == {"sonnet": {"per_5h": 4.0, "per_day": 2.8}, "opus": {"per_5h": None, "per_day": None}}   # 5/1.25; (5/7)/0.25 = 2.857


async def test_pricing_is_404_when_tickets_are_off(env):
    gw, conn, cfg, ids, keys = env
    cfg.tickets.enabled = False
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/pricing")).status_code == 404
        assert (await c.get("/pricing")).status_code == 404


async def test_me_tickets_shows_the_users_own_ticket_and_nothing_about_the_account(env):
    gw, conn, cfg, ids, keys = env
    now = int(time.time())
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "EUR", now=now - 3600)
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=2, extra_days=1, note="welcome", now=now)
    q = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "EUR", now=now)
    async with asgi_client(create_dashboard_app(gw)) as c:
        me = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()
    assert me["enabled"] and me["gated"]
    cur = me["current"]
    assert (cur["id"], cur["label"], cur["share_pct"], cur["bonus_days"], cur["bonus_share"]) == (t["id"], "Lite", 5, 1, 2)
    assert cur["effective_end"] == t["ends_at"] + DAY and cur["day_end"] == t["starts_at"] + DAY
    assert cur["bonuses"] == [{"share_pct": 2, "note": "welcome", "ends_at": t["ends_at"] + DAY}]
    assert (me["queued"]["id"], me["queued"]["starts_at"]) == (q["id"], t["ends_at"] + DAY)
    assert me["prices"]["currency"] == "EUR" and me["how_to_buy"].startswith("Send")
    for leaked in ("utilization", "sold_now", "shares", "granted_by"):
        assert leaked not in str(me)
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/me/tickets")).status_code == 401
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tickets_web.py -q -k "pricing or me_tickets"`
Expected: FAIL with 404s.

- [ ] **Step 3: Implement `price_table`** (append to `tickets.py`)

```python
def price_table(conn: sqlite3.Connection, cfg: Config, now: float, currency: str) -> dict:
    """What /pricing and a user's price list show: per tier and length the regular and charged price, the local
    amounts, a sold-out flag and the usage hints. Nothing about who holds what. Falls back to USD without a rate."""
    from . import estimates   # here, not at the top: estimates imports quota, which has nothing to do with tickets
    now = _now(now)
    rate = current_rate(conn, currency)
    if rate is None:
        currency, rate = "USD", current_rate(conn, "USD")
    step = currencies(cfg)[currency]
    tiers = []
    for tid, t in cfg.tickets.tiers.items():
        lengths = {}
        for length in LENGTHS:
            p = price_now(conn, tid, length, now)
            lengths[length] = {"days": LENGTHS[length], "list_usd": p["list_usd"], "usd": p["usd"], "discount_ends_at": p["discount_ends_at"],
                               "amount": round_local(p["usd"], rate["rate"], step), "list_amount": round_local(p["list_usd"], rate["rate"], step),
                               "sold_out": sold_out(conn, cfg, tid, length, now)}
        tiers.append({"tier": tid, "label": t.label, "share_pct": t.share_pct, "compare": t.compare,
                      "hours": estimates.hours_hint(conn, t.share_pct), "lengths": lengths})
    return {"currency": currency, "rate_set_at": rate["set_at"], "how_to_buy": cfg.tickets.how_to_buy, "tiers": tiers}
```

- [ ] **Step 4: Implement the endpoints** (in `web.py`, after `capacity`)

```python
    def display_currency() -> str:
        return next(iter(cfg.tickets.currencies), "USD")

    @app.get("/api/pricing")
    async def pricing_api():
        # Public: prices, discounts, the rate date, the usage hints and whether a purchase is possible. Nothing else.
        need_tickets()
        return tickets.price_table(conn, cfg, time.time(), display_currency())

    @app.get("/pricing")
    async def pricing_page():
        need_tickets()
        return HTMLResponse(versioned("pricing.html", ("pricing.js", "app.css")), headers=PAGE_HEADERS)

    @app.get("/api/me/tickets")
    async def me_tickets(request: Request):
        user = principal(request)
        if not cfg.tickets.enabled:
            return {"enabled": False}
        now = time.time()
        st = tickets.user_state(conn, user["id"], now)
        label = lambda t: cfg.tickets.tiers[t["tier"]].label if t["tier"] in cfg.tickets.tiers else t["tier"]  # noqa: E731
        out = {"enabled": True, "gated": st["gated"], "current": None, "queued": None, "how_to_buy": cfg.tickets.how_to_buy}
        cur = st["current"]
        if cur:
            out["current"] = {"id": cur["id"], "tier": cur["tier"], "label": label(cur), "share_pct": cur["share_pct"], "starts_at": cur["starts_at"],
                              "ends_at": cur["ends_at"], "effective_end": cur["effective_end"],
                              "bonus_days": (cur["effective_end"] - cur["ends_at"]) // tickets.DAY, "day_end": tickets.current_day(cur, now)[1],
                              "bonus_share": tickets.bonus_share(conn, cur["id"], now),
                              "bonuses": [{"share_pct": b["share_pct"], "note": b["note"], "ends_at": b["ends_at"]}
                                          for b in tickets.active_bonuses(conn, cur["id"], now)]}
        if st["queued"]:
            q = st["queued"]
            out["queued"] = {"id": q["id"], "tier": q["tier"], "label": label(q), "starts_at": q["starts_at"], "effective_end": q["effective_end"]}
        currency = cur["currency"] if cur and cur["currency"] in tickets.currencies(cfg) else display_currency()
        out["prices"] = tickets.price_table(conn, cfg, now, currency)
        return out
```

Refactor the existing `page()` so both pages share the asset versioning. Replace its body with `return HTMLResponse(versioned("index.html", ("app.js", "app.css")), headers=PAGE_HEADERS)` and add, in the closure:

```python
    def versioned(page: str, assets: tuple[str, ...]) -> str:
        """The page with each asset URL carrying its content hash, so a CDN or browser cache picks up a deploy."""
        html = (STATIC / page).read_text()
        for name in assets:
            v = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
            html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={v}"')
        return html
```

- [ ] **Step 5: Write the page**

`src/claude_proxy/static/pricing.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pricing · Claude Gateway</title>
<link rel="stylesheet" href="/static/app.css">
<script src="/static/pricing.js" defer></script>
</head>
<body>
<main class="login pricing" style="max-width:860px">
  <div class="card">
    <h2>Tickets</h2>
    <p class="lede">Buy a slice of the gateway's Claude subscription for a day, a week or a month. Prices include nothing but the slice: no payment provider, no VAT.</p>
    <div id="pricing"><p class="muted">Loading…</p></div>
    <p class="muted" id="rate-note"></p>
    <h3>How to buy</h3>
    <p id="how-to-buy"></p>
    <p><a href="/dashboard">Sign in</a> · <a href="/privacy">Privacy</a></p>
  </div>
</main>
</body>
</html>
```

`src/claude_proxy/static/pricing.js`:

```js
"use strict";
// The public price list: tiers by length, discounts with a live countdown, usage hints and sold-out badges.
// Everything comes from /api/pricing; the page computes nothing about the account itself.
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const LENGTHS = [["day", "1 day"], ["week", "1 week"], ["month", "1 month"]];
const FAMILY = { sonnet: "Sonnet", opus: "Opus" };
let data = null;

function money(amount, currency) {
  try { return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount); }
  catch { return `${amount.toFixed(2)} ${currency}`; }
}
function countdown(endsAt) {
  const s = Math.max(0, Math.floor(endsAt - Date.now() / 1000));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return `${d ? d + "d " : ""}${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m`;
}
function hints(t) {
  const parts = [];
  for (const [fam, name] of Object.entries(FAMILY)) {
    const h = t.hours[fam];
    if (h && (h.per_5h != null || h.per_day != null)) {
      parts.push(`${name}: at least ${h.per_5h != null ? `${h.per_5h} h of steady use per 5-hour window` : ""}${h.per_5h != null && h.per_day != null ? ", " : ""}${h.per_day != null ? `${h.per_day} h per day` : ""}`);
    }
  }
  return `<div class="muted">≈ ${esc(t.compare)}${parts.length ? "<br>" + parts.map(esc).join("<br>") : ""}</div>`;
}
function cell(l, currency) {
  const badge = l.sold_out ? `<span class="badge">sold out</span>` : "";
  if (l.discount_ends_at) {
    return `<td class="r"><s class="muted">${esc(money(l.list_amount, currency))}</s> <b>${esc(money(l.amount, currency))}</b><br>
      <span class="muted countdown" data-ends="${l.discount_ends_at}">Offer ends in ${esc(countdown(l.discount_ends_at))}</span> ${badge}</td>`;
  }
  return `<td class="r"><b>${esc(money(l.amount, currency))}</b> ${badge}</td>`;
}
function render() {
  const root = document.getElementById("pricing");
  root.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr><th>Tier</th>${LENGTHS.map(([, n]) => `<th class="r">${n}</th>`).join("")}</tr></thead>
    <tbody>${data.tiers.map((t) => `<tr><td><b>${esc(t.label)}</b><div class="muted">${esc(t.share_pct)}% of the subscription</div>${hints(t)}</td>
      ${LENGTHS.map(([k]) => cell(t.lengths[k], data.currency)).join("")}</tr>`).join("")}</tbody></table></div>`;
  document.getElementById("rate-note").textContent = data.rate_set_at
    ? `Prices converted at the rate of ${new Date(data.rate_set_at * 1000).toLocaleDateString()}.` : "";
  document.getElementById("how-to-buy").textContent = data.how_to_buy || "Ask the gateway admin.";
}
function tick() {
  let reload = false;
  document.querySelectorAll(".countdown").forEach((el) => {
    const ends = +el.dataset.ends;
    if (ends <= Date.now() / 1000) reload = true;
    el.textContent = `Offer ends in ${countdown(ends)}`;
  });
  if (reload) location.reload();   // the regular price returns by itself
}
fetch("/api/pricing", { credentials: "omit" }).then((r) => r.json()).then((d) => { data = d; render(); setInterval(tick, 1000); })
  .catch(() => { document.getElementById("pricing").innerHTML = `<p class="muted">Prices are not available right now.</p>`; });
```

Add to `app.css`: `.pricing s { text-decoration-thickness: 1px; } .pricing .data td { vertical-align: top; }`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tickets_web.py tests/test_web.py tests/test_privacy.py -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/claude_proxy/tickets.py src/claude_proxy/web.py src/claude_proxy/static/pricing.html src/claude_proxy/static/pricing.js src/claude_proxy/static/app.css tests/test_tickets_web.py
git commit -m "Public /pricing page and the user's own ticket endpoint

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: Dashboard, admin side

**Files:**
- Modify: `src/claude_proxy/static/app.js`, `src/claude_proxy/static/app.css`

**Interfaces:**
- Consumes: every endpoint of Tasks 9, 10 and 12 and `/api/session.tickets`.
- Produces: tabs `Tickets` (capacity panel, grant form, list with Cancel and Bonus) and `Pricing` (rates, price grid, discounts), shown only when `S.tickets.enabled`; `Ungate` and a delete-by-typed-name dialog on the Users page.

There are no JS unit tests in this repo; the backend tests of Tasks 9 to 12 pin the API, and Step 5 checks the pages in a browser.

- [ ] **Step 1: State and tabs**

In `boot()`, after `S.user = s.user; ...` add `S.tickets = s.tickets || { enabled: false };`. In `VIEWS` add, after `users`:

```js
  tickets: { label: "Tickets", render: renderTickets, admin: true, feature: "tickets" },
  pricing: { label: "Pricing", render: renderPricing, admin: true, feature: "tickets" },
```

In `renderTabs`, the filter becomes `!v.hidden && (!v.admin || isAdmin()) && (!v.feature || S.tickets?.enabled)`. In `render()`, the view guard becomes `VIEWS[S.tab] && (!VIEWS[S.tab].admin || isAdmin()) && (!VIEWS[S.tab].feature || S.tickets?.enabled)`.

Add helpers next to `fmtTime`:

```js
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
const toLocal = (t) => { const d = new Date(t * 1000); d.setSeconds(0, 0); return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };   // for <input type=datetime-local>
const fromLocal = (v) => (v ? Math.floor(new Date(v).getTime() / 1000) : null);
const money = (amount, currency) => { try { return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount); } catch { return `${amount.toFixed(2)} ${currency}`; } };
```

Add to the `TIPS` literal (anywhere inside it):

```js
  act_ungate: `This user's tickets have all ended. Stop them gating the user and set ordinary limits instead; the tickets stay as sales records.`,
  act_ticket_cancel: `Frees the slice now and ends the ticket's bonuses. Their queued tickets move forward to close the gap when they fit. Refunds happen outside the app.`,
  act_ticket_bonus: `Extra share for a period, extra days at the ticket's share, or both. Checked against capacity like a ticket.`,
  capacity_sold: `What tickets and bonuses have reserved: the sum of their shares at this moment, and the highest sum over the next 30 days. Grants are refused past <b>max_sold_pct</b>.`,
  capacity_util: `What everyone has actually used, as Anthropic reports it. The gap between max_sold_pct and 100 is what the admin, free-credit accounts and hand-limited users have.`,
  sold_out_vs_queued: `/pricing asks whether a ticket starting <b>now</b> fits. A grant to someone with a live ticket starts after it, so it can succeed while the badge says sold out.`,
```

- [ ] **Step 2: Tickets tab**

Add after `renderUsers` and its helpers:

```js
// ---------- paid tickets (design 2026-10-03) ----------

const stateBadge = (s) => `<span class="badge state-${s}">${esc(s)}</span>`;

async function renderTickets(main) {
  const [cap, { tickets }, { users }] = await Promise.all([api("/api/admin/capacity"), api(`/api/admin/tickets${S.ticketUser ? `?user_id=${S.ticketUser}` : ""}`), api("/api/users")]);
  const pct = (v, max) => meter((100 * v) / max);
  const util = (b) => (cap.utilization[b].utilization_pct == null ? "—" : `${cap.utilization[b].utilization_pct.toFixed(0)}%${cap.utilization[b].stale ? " (stale)" : ""}`);
  main.innerHTML = `<section class="view"><h2>Tickets</h2>
    <p class="lede">Each ticket reserves a share of the subscription for its days. The gateway never sells the same capacity twice.</p>
    <div class="grid cols-2">
      <div class="card"><h3>Capacity${tipI("capacity_sold")}</h3>
        <div class="limit-row"><span>Sold now</span><span class="num">${cap.sold_now_pct.toFixed(1)}% of ${cap.max_sold_pct}%</span></div>${pct(cap.sold_now_pct, cap.max_sold_pct)}
        <div class="limit-row" style="margin-top:8px"><span>Peak, next 30 days</span><span class="num">${cap.peak_30d_pct.toFixed(1)}% of ${cap.max_sold_pct}%</span></div>${pct(cap.peak_30d_pct, cap.max_sold_pct)}
        <p class="sub" style="margin-top:12px">Sold is what tickets may use. Headroom for everyone else: ${(100 - cap.max_sold_pct).toFixed(0)}%.</p></div>
      <div class="card"><h3>Account utilization (reported by Anthropic)${tipI("capacity_util")}</h3>
        <div class="limit-row"><span>5-hour bucket</span><span class="num">${util("5h")}</span></div>
        <div class="limit-row"><span>Weekly bucket</span><span class="num">${util("7d")}</span></div>
        <p class="sub" style="margin-top:12px">Utilization is what everyone has used, ticket holders and headroom users alike.</p></div>
    </div>
    <div class="controls"><button class="btn primary" id="grant">Grant a ticket</button>
      <select id="ticket-user"><option value="">All users</option>${users.filter((u) => u.ticket && u.ticket.gated).map((u) => `<option value="${u.id}" ${S.ticketUser === u.id ? "selected" : ""}>${esc(u.name)}</option>`).join("")}</select></div>
    <div class="card table-wrap"><table class="data"><thead><tr><th>User</th><th>Tier</th><th>Period</th><th class="r">Paid</th><th>State</th><th>Bonuses</th><th>Note</th><th></th></tr></thead>
      <tbody>${tickets.map(ticketRow).join("") || `<tr><td colspan="8" class="muted">No tickets yet.</td></tr>`}</tbody></table></div>
  </section>`;
  $("#grant").onclick = () => grantDialog(users);
  $("#ticket-user").onchange = (e) => { S.ticketUser = e.target.value ? +e.target.value : null; render(); };
  main.querySelectorAll("[data-tact]").forEach((b) => b.addEventListener("click", () => ticketAction(b.dataset.tact, tickets.find((t) => t.id === +b.dataset.id))));
}
function ticketRow(t) {
  const live = t.state === "active" || t.state === "queued";
  const bonus = t.bonuses.filter((b) => !b.cancelled_at).map((b) => `${b.share_pct ? `+${b.share_pct}%` : ""}${b.share_pct && b.extra_days ? " " : ""}${b.extra_days ? `+${b.extra_days} d` : ""}`).join(", ");
  return `<tr><td><b>${esc(t.user_name)}</b>${t.user_id == null ? ` <span class="badge">deleted</span>` : ""}</td>
    <td>${esc(t.tier)} <span class="muted">${t.share_pct}%</span></td>
    <td class="nowrap">${fmtDate(t.starts_at)} → ${fmtDate(t.effective_end)}${t.effective_end !== t.ends_at ? ` <span class="muted">(+${Math.round((t.effective_end - t.ends_at) / 86400)} d bonus)</span>` : ""}</td>
    <td class="r">${esc(money(t.amount, t.currency))}${t.discount_id ? `<div class="muted"><s>$${t.list_usd}</s> $${t.usd}</div>` : `<div class="muted">$${t.usd}</div>`}</td>
    <td>${stateBadge(t.state)}</td><td class="muted">${esc(bonus) || "—"}</td><td class="muted">${esc(t.note || "")}</td>
    <td><div class="row-actions">${live ? `<button class="btn small" data-tact="bonus" data-id="${t.id}" data-tip="act_ticket_bonus">Bonus</button>
      <button class="btn small danger" data-tact="cancel" data-id="${t.id}" data-tip="act_ticket_cancel">Cancel</button>` : ""}</div></td></tr>`;
}
async function ticketAction(act, t) {
  if (act === "bonus") return bonusDialog(t);
  if (!confirmInline(`Cancel ${t.user_name}'s ${t.tier} ticket? The slice is freed now and its bonuses end. Refunds happen outside the app.`)) return;
  try {
    const r = await api(`/api/admin/tickets/${t.id}/cancel`, { method: "POST", body: {} });
    if (r.dates_kept) alertInline(`Cancelled. Their queued tickets kept their dates because one of them would not fit earlier: ${r.reason}`);
    else render();
  } catch (e) { alertInline(e.message); }
}
function grantDialog(users) {
  const T = S.tickets;
  const d = openDialog(`<h3>Grant a ticket</h3>
    <form id="f-grant" class="form-grid">
      <label>User<select name="user" required>${users.filter((u) => !u.revoked && u.enabled).map((u) => `<option value="${u.id}">${esc(u.name)}</option>`).join("")}</select></label>
      <label>Tier<select name="tier">${Object.entries(T.tiers).map(([k, t]) => `<option value="${k}">${esc(t.label)} · ${t.share_pct}%</option>`).join("")}</select></label>
      <label>Length<select name="length">${Object.entries(T.lengths).map(([k, n]) => `<option value="${k}">${k} (${n} ${n === 1 ? "day" : "days"})</option>`).join("")}</select></label>
      <label>Currency<select name="currency">${T.currencies.map((c) => `<option ${c !== "USD" ? "selected" : ""}>${c}</option>`).join("")}</select></label>
      <label>Note (for you)<input type="text" name="note" maxlength="200" placeholder="e.g. transfer ref 1234"></label>
      <div id="grant-preview" class="hint" aria-live="polite">…</div>
      <div id="grant-limits"></div>
      <label id="grant-stale" class="hidden"><input type="checkbox" name="confirm_stale_rate"> The rate is stale; grant at it anyway</label>
      <button class="btn primary" type="submit" id="grant-go">Grant</button>
    </form><div class="error" id="grant-err"></div><p><button class="btn" data-close>Cancel</button></p>`);
  const f = $("#f-grant", d);
  let preview = null;
  const refresh = async () => {
    $("#grant-err", d).textContent = "";
    try {
      preview = await api("/api/admin/tickets/preview", { method: "POST", body: { user: +f.user.value, tier: f.tier.value, length: f.length.value, currency: f.currency.value } });
    } catch (e) { preview = null; $("#grant-preview", d).innerHTML = `<span class="muted">${esc(e.message)}</span>`; $("#grant-limits", d).innerHTML = ""; return; }
    const p = preview;
    const price = p.discount_id ? `<s>$${p.list_usd}</s> <b>$${p.usd}</b> (discount)` : `<b>$${p.usd}</b>`;
    const when = p.queued ? `queued: starts ${fmtDate(p.starts_at)}, after the user's current ticket` : `starts now`;
    const fit = p.available ? `<span class="good">The period is available.</span>` : `<span class="critical">${esc(p.reason)}</span>`;
    const soldOut = p.sold_out_now && p.available ? `<br><span class="muted">${tipT("/pricing shows this tier as sold out right now", "sold_out_vs_queued")}; this grant starts later and fits.</span>` : "";
    $("#grant-preview", d).innerHTML = `${price} → <b>${esc(money(p.amount, p.currency))}</b> at ${p.rate} ${p.rate_set_at ? `(rate of ${fmtDate(p.rate_set_at)}${p.stale_rate ? ", <b>stale</b>" : ""})` : ""}<br>
      ${p.days} day${p.days === 1 ? "" : "s"}, ${when}: ${fmtDate(p.starts_at)} → ${fmtDate(p.ends_at)}<br>${fit}${soldOut}
      ${p.credit ? `<br><span class="muted">Their sign-up credit is removed with the ticket.</span>` : ""}`;
    $("#grant-stale", d).classList.toggle("hidden", !p.stale_rate);
    $("#grant-limits", d).innerHTML = p.limit_rows.length ? `<p class="sub">Remove these hand-set limits with the grant, so they don't throttle a paying user:</p>` +
      p.limit_rows.map((r, i) => `<label class="check"><input type="checkbox" name="rm" value="${i}" checked> ${esc(limitLabel(r))} = ${esc(r.value)} ${esc(r.unit)}</label>`).join("") : "";
    $("#grant-go", d).disabled = !p.available;
  };
  ["user", "tier", "length", "currency"].forEach((k) => (f[k].onchange = refresh));
  refresh();
  f.onsubmit = async (e) => {
    e.preventDefault();
    if (!preview) return;
    const rm = [...f.querySelectorAll('input[name="rm"]:checked')].map((c) => ({ kind: preview.limit_rows[+c.value].kind, scope: preview.limit_rows[+c.value].scope }));
    try {
      await api("/api/admin/tickets", { method: "POST", body: { user: +f.user.value, tier: f.tier.value, length: f.length.value, currency: f.currency.value,
        note: f.note.value, remove_limits: rm, confirm_stale_rate: f.confirm_stale_rate.checked } });
      d.close(); render();
    } catch (err) { $("#grant-err", d).textContent = err.message; }
  };
}
function bonusDialog(t) {
  const d = openDialog(`<h3>Bonus on ${esc(t.user_name)}'s ${esc(t.tier)} ticket</h3>
    <p class="sub">Extra share applies between the two times (clamped to the ticket). Extra days extend the ticket at its own share and move this user's queued tickets forward by the same amount.</p>
    <form id="f-bonus" class="form-grid">
      <label>Extra share, points<input type="number" name="share_pct" min="0" step="0.1" value="0"></label>
      <label>From<input type="datetime-local" name="starts_at" value="${toLocal(Math.max(t.starts_at, Date.now() / 1000))}"></label>
      <label>Until<input type="datetime-local" name="ends_at" value="${toLocal(t.effective_end)}"></label>
      <label>Extra days<input type="number" name="extra_days" min="0" step="1" value="0"></label>
      <label>Note (shown to the user)<input type="text" name="note" maxlength="200" placeholder="e.g. Sorry for Tuesday's outage"></label>
      <button class="btn primary" type="submit">Add bonus</button>
    </form><div class="error" id="bonus-err"></div><p><button class="btn" data-close>Cancel</button></p>`);
  const f = $("#f-bonus", d);
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api(`/api/admin/tickets/${t.id}/bonus`, { method: "POST", body: { share_pct: +f.share_pct.value, extra_days: +f.extra_days.value,
        starts_at: fromLocal(f.starts_at.value), ends_at: fromLocal(f.ends_at.value), note: f.note.value } });
      d.close();
      if (r.moved) alertInline(`Bonus added. ${r.moved} queued ticket${r.moved === 1 ? "" : "s"} moved forward by ${f.extra_days.value} day(s).`); else render();
    } catch (err) { $("#bonus-err", d).textContent = err.message; }
  };
}
```

- [ ] **Step 3: Pricing tab**

```js
async function renderPricing(main) {
  const [{ rates }, p] = await Promise.all([api("/api/admin/rates"), api("/api/admin/prices")]);
  const price = (tier, length) => p.prices.find((x) => x.tier === tier && x.length === length)?.usd ?? "";
  const now = Date.now() / 1000;
  const dstate = (x) => (x.cancelled_at ? "cancelled" : x.ends_at <= now ? "ended" : x.starts_at > now ? "upcoming" : "active");
  main.innerHTML = `<section class="view"><h2>Pricing</h2>
    <p class="lede">Prices are in USD; buyers see them converted at today's rate. Shares live in the config file, since changing one changes capacity.</p>
    <div class="grid cols-2">
      <div class="card"><h3>Exchange rates</h3><p class="sub">Local units per 1 USD. A rate older than 36 hours is marked stale and the grant form asks you to confirm it.</p>
        ${rates.map((r) => `<form class="limit-row rate-row" data-cur="${esc(r.currency)}"><span><b>${esc(r.currency)}</b> <span class="muted">rounds to ${r.round_to}</span>${r.stale ? ` <span class="badge">stale</span>` : ""}
          <div class="muted" style="font-size:12px">${r.rate == null ? "no rate yet: unusable until set" : `${r.rate} · set ${fmtDate(r.set_at)} by ${esc(r.set_by || "?")}`}</div></span>
          <span><input type="number" name="rate" step="any" min="0" placeholder="today's rate" required style="width:110px"> <button class="btn small" type="submit">Save</button></span></form>`).join("") || `<p class="muted">No currencies besides USD in the config.</p>`}
      </div>
      <div class="card"><h3>Regular prices, USD</h3><p class="sub">Each change is logged. Existing tickets keep what they were sold at.</p>
        <table class="data"><thead><tr><th>Tier</th>${Object.keys(p.lengths).map((l) => `<th class="r">${l}</th>`).join("")}</tr></thead><tbody>
        ${Object.entries(p.tiers).map(([k, t]) => `<tr><td><b>${esc(t.label)}</b> <span class="muted">${t.share_pct}%</span></td>${Object.keys(p.lengths).map((l) =>
          `<td class="r"><form class="price-form" data-tier="${k}" data-length="${l}"><input type="number" name="usd" step="0.01" min="0.01" value="${price(k, l)}" required style="width:80px"> <button class="btn small" type="submit">Save</button></form></td>`).join("")}</tr>`).join("")}
        </tbody></table></div>
    </div>
    <div class="card" style="margin-top:16px"><h3>Discounts</h3><p class="sub">A lower USD price for one tier and length over a period. It must be below the regular price; while active it replaces the price everywhere and /pricing shows a countdown.</p>
      <form id="f-disc" class="form-grid">
        <label>Tier<select name="tier">${Object.entries(p.tiers).map(([k, t]) => `<option value="${k}">${esc(t.label)}</option>`).join("")}</select></label>
        <label>Length<select name="length">${Object.keys(p.lengths).map((l) => `<option>${l}</option>`).join("")}</select></label>
        <label>Price, USD<input type="number" name="usd" step="0.01" min="0.01" required></label>
        <label>From<input type="datetime-local" name="starts_at" value="${toLocal(now)}" required></label>
        <label>Until<input type="datetime-local" name="ends_at" value="${toLocal(now + 7 * 86400)}" required></label>
        <button class="btn primary" type="submit">Create discount</button></form>
      <div class="error" id="disc-err"></div>
      <table class="data" style="margin-top:12px"><thead><tr><th>Tier</th><th>Length</th><th class="r">USD</th><th>Period</th><th>State</th><th></th></tr></thead><tbody>
      ${p.discounts.map((x) => `<tr><td>${esc(p.tiers[x.tier]?.label || x.tier)}</td><td>${esc(x.length)}</td><td class="r">$${x.usd}</td><td class="nowrap">${fmtDate(x.starts_at)} → ${fmtDate(x.ends_at)}</td>
        <td>${stateBadge(dstate(x))}</td><td>${dstate(x) === "active" || dstate(x) === "upcoming" ? `<button class="btn small danger" data-dcancel="${x.id}">Cancel</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">No discounts.</td></tr>`}
      </tbody></table></div>
  </section>`;
  const post = async (path, body, errEl) => { try { await api(path, { method: "POST", body }); render(); } catch (e) { errEl ? (errEl.textContent = e.message) : alertInline(e.message); } };
  main.querySelectorAll(".rate-row").forEach((f) => (f.onsubmit = (e) => { e.preventDefault(); post("/api/admin/rates", { currency: f.dataset.cur, rate: +f.rate.value }); }));
  main.querySelectorAll(".price-form").forEach((f) => (f.onsubmit = (e) => { e.preventDefault(); post("/api/admin/prices", { tier: f.dataset.tier, length: f.dataset.length, usd: +f.usd.value }); }));
  $("#f-disc").onsubmit = (e) => { e.preventDefault(); const f = e.target; post("/api/admin/discounts", { tier: f.tier.value, length: f.length.value, usd: +f.usd.value, starts_at: fromLocal(f.starts_at.value), ends_at: fromLocal(f.ends_at.value) }, $("#disc-err")); };
  main.querySelectorAll("[data-dcancel]").forEach((b) => (b.onclick = () => post(`/api/admin/discounts/${b.dataset.dcancel}/cancel`, {})));
}
```

- [ ] **Step 4: Users page: Ungate and delete by name**

In `userActions(u)`, add before `const upgrade`:

```js
  const ungate = u.ticket && u.ticket.gated && !u.ticket.live && !u.revoked
    ? `<button class="btn small" data-act="ungate" data-id="${u.id}" data-tip="act_ungate">Ungate</button>` : "";
```

and put `${ungate}` right after `${upgrade}` in the returned template. In `userRow(u)`, after the `state` badges add `${u.ticket && u.ticket.live ? `<span class="badge">${u.ticket.current ? "ticket" : "ticket queued"}</span>` : u.ticket && u.ticket.gated ? `<span class="badge">ticket ended</span>` : ""}`.

In `userAction`, replace the `delete` confirm line and add the ungate branch:

```js
  if (act === "delete") return deleteDialog(u);
  if (act === "ungate") {
    try { await api(`/api/admin/users/${id}/ungate`, { method: "POST", body: {} }); } catch (e) { return alertInline(e.message); }
    const { users } = await api("/api/users");
    return limitsDialog(users.find((x) => x.id === id));   // the spec: Ungate opens the Limits dialog so hand limits get set
  }
```

and add:

```js
function deleteDialog(u) {
  const d = openDialog(`<h3>Delete ${esc(u.name)}</h3>
    <p>Their recorded usage is deleted too and disappears from account totals and charts. Tickets they bought stay as sales records. This cannot be undone.</p>
    <form id="f-del" class="form-grid"><label>Type <b>${esc(u.name)}</b> to confirm<input type="text" name="confirm" autocomplete="off" required></label>
    <button class="btn danger" type="submit">Delete</button></form><div class="error" id="del-err"></div><p><button class="btn" data-close>Cancel</button></p>`);
  $("#f-del", d).onsubmit = async (e) => {
    e.preventDefault();
    try { await api(`/api/admin/users/${u.id}/delete`, { method: "POST", body: { confirm: new FormData(e.target).get("confirm") } }); d.close(); render(); }
    catch (err) { $("#del-err", d).textContent = err.message; }
  };
}
```

Add to `app.css`:

```css
.badge.state-active { border-color: var(--good); color: var(--good-text); }
.badge.state-queued, .badge.state-upcoming { border-color: var(--s1); color: var(--s1); }
.badge.state-cancelled { border-color: var(--critical); color: var(--critical-text); }
.good { color: var(--good-text); } .critical { color: var(--critical-text); }
label.check { display: flex; gap: 8px; align-items: center; font-size: 13px; }
.form-grid .hint { grid-column: 1 / -1; }
```

- [ ] **Step 5: Check in a browser**

```bash
export CLAUDE_PROXY_CREDENTIAL_KEY=$(.venv/bin/claude-proxy keygen)
cat > /tmp/tickets.toml <<'EOF'
[tickets]
how_to_buy = "Send the amount by bank transfer to the admin."
[tickets.currencies.EUR]
round_to = 0.50
EOF
CLAUDE_PROXY_DB=/tmp/tickets.db CLAUDE_PROXY_ADMIN_PASSWORD=correcthorsebattery .venv/bin/claude-proxy --config /tmp/tickets.toml init
CLAUDE_PROXY_DB=/tmp/tickets.db .venv/bin/claude-proxy --config /tmp/tickets.toml user add maya
CLAUDE_PROXY_DB=/tmp/tickets.db .venv/bin/claude-proxy --config /tmp/tickets.toml serve
```

Open `http://127.0.0.1:8081/admin`, sign in as `admin`. Expected, in order: the Tickets and Pricing tabs are present; on Pricing, saving `0.92` for EUR shows the rate and no stale badge, saving a lite week price of `9` updates the grid, creating a discount lists it as `active`; on Tickets, the capacity panel reads 0% of 80%, the grant dialog previews `maya`'s lite week as `$9 → €8.50`, `starts now`, available; after Grant the row shows `active`; Bonus with 1 extra day shows `(+1 d bonus)`; Cancel frees the capacity panel back to 0%; on Users, maya shows `ticket ended` and an Ungate button that opens the Limits dialog. Stop the server (Ctrl-C) and delete `/tmp/tickets.db*`.

- [ ] **Step 6: Run the suite and commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS (the `test_dashboard_assets_are_versioned...` test still finds exactly two versioned asset URLs on `/dashboard`).

```bash
git add src/claude_proxy/static/app.js src/claude_proxy/static/app.css
git commit -m "Dashboard: Tickets and Pricing tabs, Ungate, delete by typed name

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: Dashboard, user side; status line marker; docs

**Files:**
- Modify: `src/claude_proxy/static/app.js`, `src/claude_proxy/web.py` (`_status_line`), `config.example.toml`, `README.md`
- Test: `tests/test_tickets_limits.py` (append one test)

**Interfaces:**
- Consumes: `/api/me/tickets`, `/api/me/status` (whose `limits` now carry `today_limit` and `no_live_data` for ticket users).
- Produces: the user's Overview shows a ticket card (tier, end date with `+N days bonus`, queued ticket, bonus badge with the admin's note), the 5-hour bar and a `today` bar, and a price list in their currency; values estimated without live data are marked; the status line appends ` est.` to such a share figure.

- [ ] **Step 1: Write the failing test** (append to `tests/test_tickets_limits.py`)

```python
def test_status_line_marks_shares_estimated_without_live_data(env):
    from claude_proxy.web import _status_line
    conn, cfg, ids, t = env
    req(conn, ids["alice"], NOW - 500, i=1000)
    fresh(conn, NOW - 400, util5=1, util7=1)
    sts = limits.states(conn, cfg, ids["alice"], now=NOW + 2000)   # stale: estimated from weighted tokens
    line = _status_line(user(conn, ids["alice"]), sts, None)
    assert " · 5h 20% est. (resets in" in line and " · today 140% est. (resets in" in line
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tickets_limits.py -q -k status_line_marks`
Expected: FAIL (`est.` missing from the line).

- [ ] **Step 3: Status line and user-side JS**

In `web._status_line`, the non-admin share branch becomes:

```python
        if base == "share" and account is None:
            label = _SHARE_PERIOD.get(period, period)
            est = " est." if s.no_live_data else ""
            parts.append(f"{label} n/a" if s.skipped or s.current is None else f"{label} {s.pct:.0f}%{est}{_resets(s)}")
            continue
```

In `app.js`:

1. `const USER_LIMIT_KINDS = ["5h_limit", "weekly_limit", "today_limit"];`
2. In `limitValue(l)`, the `USER_LIMIT_KINDS` branch returns `` `${l.current.toFixed(0)}% used${l.no_live_data ? " · estimated without live data" : ""}${l.reset_in ? ` · resets in ${fmtDur(l.reset_in)}` : ""}` ``; the generic branch prefixes `${l.no_live_data ? "est. (no live data) " : l.estimated ? "est. " : ""}`.
3. In `limitValueTip(l)`, before the existing `if (l.estimated)` line: `if (l.no_live_data) return "No fresh report from Anthropic right now, so this is estimated from your tokens at the account's usual rate. Your ticket's limit still applies.";`
4. In `limitLabel(l)`: `today_limit` reads `today` and `5h_limit` reads `5 h`: `const USER_LABELS = { "5h_limit": "5-hour limit", weekly_limit: "weekly limit", today_limit: "today" };` and `return `${USER_LABELS[l.kind] || (l.kind === "cost_total" ? "credit" : l.kind.replace(/_/g, " "))}${scope}`;`.
5. In `renderOverview`, fetch the ticket too: `const [ov, me, keys, tk] = await Promise.all([api("/api/overview"), isAdmin() ? null : api("/api/me/status"), api("/api/keys"), isAdmin() || !S.tickets?.enabled ? null : api("/api/me/tickets")]);`. Replace the user's `Your limits` card line (`<div class="card"><h3>${isAdmin() ? "Usage by user, last 7 days" : "Your limits"}</h3> ... </div>`) with `${isAdmin() ? usersCard() : userLimitsCard(me, tk)}` where:

```js
function usersCard() {
  return `<div class="card"><h3>Usage by user, last 7 days</h3><p class="sub">Daily, weighted tokens.</p><div class="chart short" id="ov-users"></div></div>`;
}
function userLimitsCard(me, tk) {
  const c = tk && tk.current;
  const ticket = !tk || !tk.gated ? "" : c ? `<div class="ticket">
      <div><b>${esc(c.label)} ticket</b> · ${c.share_pct}% of the subscription</div>
      <div class="muted">Ends ${fmtDate(c.effective_end)}${c.bonus_days ? ` <span class="badge">+${c.bonus_days} day${c.bonus_days === 1 ? "" : "s"} bonus</span>` : ""} · today ends in ${fmtDur(c.day_end - Date.now() / 1000)}</div>
      ${c.bonuses.map((b) => `<div class="badge bonus">Bonus: +${b.share_pct}% until ${fmtDate(b.ends_at)}${b.note ? ` · ${esc(b.note)}` : ""}</div>`).join("")}
      ${c.bonus_share ? `<p class="sub">While the bonus runs, both bars are measured against ${c.share_pct + c.bonus_share}%.</p>` : ""}
      ${tk.queued ? `<div class="muted">Next: ${esc(tk.queued.label)} ticket from ${fmtDate(tk.queued.starts_at)}</div>` : ""}</div>`
    : tk.queued ? `<div class="ticket"><b>Your next ticket</b> (${esc(tk.queued.label)}) starts ${fmtDate(tk.queued.starts_at)}.</div>`
    : `<div class="ticket"><b>Your ticket has ended.</b> ${esc(tk.how_to_buy || "Ask the gateway admin.")}</div>`;
  return `<div class="card"><h3>${c ? "Your ticket" : "Your limits"}</h3>
    <p class="sub">${c ? "The 5-hour bar follows Anthropic's window; the today bar is this ticket day's share and resets when the day ends." : `Resets a window-length after the first request; ${tipT("the request that crosses a limit is still served", "served")}.`}</p>
    ${ticket}${limitsBlock(me.limits)}</div>`;
}
function priceListCard(tk) {
  const p = tk.prices;
  const L = [["day", "1 day"], ["week", "1 week"], ["month", "1 month"]];
  const hint = (t) => Object.entries({ sonnet: "Sonnet", opus: "Opus" }).map(([f, n]) => { const h = t.hours[f]; return h && (h.per_5h != null || h.per_day != null)
    ? `<div class="muted">${n}: at least ${[h.per_5h != null ? `${h.per_5h} h per 5-hour window` : null, h.per_day != null ? `${h.per_day} h per day` : null].filter(Boolean).join(", ")}</div>` : ""; }).join("");
  const cell = (l) => `<td class="r">${l.discount_ends_at ? `<s class="muted">${esc(money(l.list_amount, p.currency))}</s> ` : ""}<b>${esc(money(l.amount, p.currency))}</b>${l.sold_out ? ` <span class="badge">sold out</span>` : ""}
    ${l.discount_ends_at ? `<div class="muted countdown" data-ends="${l.discount_ends_at}"></div>` : ""}</td>`;
  return `<div class="card"><h3>Tickets</h3><p class="sub">Buy a slice for a day, a week or a month.${p.rate_set_at ? ` Prices converted at the rate of ${fmtDate(p.rate_set_at)}.` : ""}</p>
    <div class="table-wrap"><table class="data"><thead><tr><th>Tier</th>${L.map(([, n]) => `<th class="r">${n}</th>`).join("")}</tr></thead><tbody>
    ${p.tiers.map((t) => `<tr><td><b>${esc(t.label)}</b> <span class="muted">${t.share_pct}%</span><div class="muted">≈ ${esc(t.compare)}</div>${hint(t)}</td>${L.map(([k]) => cell(t.lengths[k])).join("")}</tr>`).join("")}
    </tbody></table></div><p class="sub">${esc(tk.how_to_buy || "")}</p></div>`;
}
```

Append `${tk ? priceListCard(tk) : ""}` after `${machinesCard(keys.keys, S.install)}` in `renderOverview`'s template, and at the end of `renderOverview` add a countdown ticker that stops when the view is re-rendered:

```js
  if (tk) {
    const tick = () => main.querySelectorAll(".countdown").forEach((el) => {
      const s = Math.max(0, Math.floor(+el.dataset.ends - Date.now() / 1000));
      el.textContent = s ? `Offer ends in ${Math.floor(s / 86400)}d ${String(Math.floor((s % 86400) / 3600)).padStart(2, "0")}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m ${String(s % 60).padStart(2, "0")}s` : "Offer ended";
    });
    tick();
    clearInterval(S.countdown); S.countdown = setInterval(() => (document.contains(main.querySelector(".countdown")) ? tick() : clearInterval(S.countdown)), 1000);
  }
```

Add to `app.css`: `.ticket { padding: 10px 0 6px; border-bottom: 1px solid var(--border); margin-bottom: 8px; } .badge.bonus { display: inline-block; margin: 4px 4px 0 0; border-color: var(--s4); }`.

- [ ] **Step 4: Documentation**

Append to `config.example.toml`:

```toml
# Paid tickets (docs/superpowers/specs/2026-10-03-paid-tickets-design.md). Present = enabled: the dashboard gains
# Tickets and Pricing tabs and /pricing goes public. Shares live here because changing one changes capacity; prices
# are seeded from default_usd on first start and edited in the dashboard afterwards.
# [tickets]
# how_to_buy = "Send the amount by bank transfer to … and email the admin."
# max_sold_pct = 80            # tickets and bonuses together may reserve at most this; the rest is headroom
# [tickets.tiers.lite]
# label = "Lite"
# share_pct = 5
# compare = "Claude Pro"
# default_usd = { day = 3, week = 8, month = 20 }
# [tickets.tiers.standard]
# label = "Standard"
# share_pct = 25
# compare = "Claude Max 5x"
# default_usd = { day = 12, week = 35, month = 100 }
# [tickets.currencies.EUR]     # USD is built in; add a daily rate on the Pricing tab for each currency here
# round_to = 0.50
```

Add a README section `## Selling tickets` after the limits section (find it with `grep -n "^## " README.md`):

```markdown
## Selling tickets

With a `[tickets]` section in the config (see `config.example.toml`), the gateway sells reserved slices of the
subscription: a tier (its share of the account) for 1 day, 1 week or 1 month. Payment happens outside the app; the
admin grants the ticket on the dashboard's **Tickets** tab, where the form shows the price, the start (now, or queued
after the user's current ticket) and whether the period fits under `max_sold_pct`. **Pricing** holds the daily exchange
rate, the USD prices and discounts; `/pricing` is the public price list with sold-out badges and a lower-bound
"at least N hours of steady use" hint computed once a day from busy hours.

A ticket holder is limited to their share of Anthropic's 5-hour window and to one seventh of it per ticket day; with no
active ticket their requests get a 403 that quotes `how_to_buy`. Third-party models are not covered unless a cost
limit is set for the user. When the ticket ends and the user is not buying again, **Ungate** on the Users page returns
them to ordinary limits. Design: `docs/superpowers/specs/2026-10-03-paid-tickets-design.md`.
```

- [ ] **Step 5: Check in a browser**

Start the server as in Task 13 (keep `/tmp/tickets.db` from there, or redo the setup). Sign in at `http://127.0.0.1:8081/admin`, set the EUR rate, grant `maya` a lite week with a 1-day bonus and a `+1%` bonus with a note, then sign out. Get maya's key with `CLAUDE_PROXY_DB=/tmp/tickets.db .venv/bin/claude-proxy --config /tmp/tickets.toml user rotate maya`, sign in with it at `/dashboard`. Expected: a `Your ticket` card with `Lite ticket · 5% of the subscription`, `Ends … +1 day bonus`, the `Bonus: +1% until …` badge with the note, the `5-hour limit` and `today` rows (the latter `0% used · resets in 23.9 h` or so), and a `Tickets` card with the price list in EUR and the how-to-buy text; nothing on the page mentions utilization, other users or seats. Open `http://127.0.0.1:8081/pricing` signed out: the table, the rate date and, if a discount exists, a countdown ticking every second.

- [ ] **Step 6: Run everything and commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

```bash
git add src/claude_proxy/static/app.js src/claude_proxy/static/app.css src/claude_proxy/web.py tests/test_tickets_limits.py config.example.toml README.md
git commit -m "Dashboard: the user's ticket card, today bar and price list; docs for [tickets]

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

Then push the branch and open the PR with auto-merge, as the repo's PR memory says: `git push -u origin paid-tickets && gh pr create --fill --body "$(git log --format=%B paid-tickets-spec..HEAD | head -40)

🤖 Generated with [Claude Code](https://claude.com/claude-code)" && gh pr merge --auto --squash`. The spec branch `paid-tickets-spec` is unpushed; push it first and open its PR, or include its commits in this PR.
