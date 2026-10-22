# API Keys on Prepaid Credit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Claude Code (terminal and VS Code) use the gateway with an `sk-proxy-a-…` API key billed per token against a prepaid USD credit, the way Anthropic's API is billed.

**Architecture:** A new key scope `api` flows through the existing proxy handler (`app.handle`). For that scope a new gate (`api_credit.evaluate`) replaces `limits.evaluate` before forwarding, and the `record()` that writes each `requests` row also writes a `charge` row to an append-only `api_ledger`. Rates, keys, ledger and per-account limits live in four new tables owned by one new module, `api_credit.py`. The dashboard gets one new tab, **API**, with user and admin sections.

**Tech Stack:** Python 3.12, FastAPI, SQLite (stdlib `sqlite3`), httpx, pytest + pytest-asyncio (`asyncio_mode = "auto"`), vanilla JS dashboard (`static/app.js`, `static/i18n.js`).

**Spec:** `docs/superpowers/specs/2026-10-21-api-credit-design.md`

## Global Constraints

- Work on `master` directly; no branches or worktrees (subagents included).
- The repo is shared with concurrent sessions and has **other people's uncommitted edits** in `db.py`, `web.py`, `config.py`, `app.js`, `i18n.js` (see `git status` at start). Commit only your own hunks: before each commit run `git diff <file>`; if a file has hunks that aren't yours, stage yours with `git diff <file> > /tmp/mine.patch`, edit the patch down to your hunks, `git apply --cached /tmp/mine.patch`. Never `git stash`, `git checkout -- <file>` or `git reset` others' work.
- Run tests with `.venv/bin/pytest`. The whole suite must pass at the end of each task (`.venv/bin/pytest -q`).
- API key prefix: exactly `sk-proxy-a-`. Displayed key prefix: first 14 characters (`raw[:len(prefix) + 3]`, as `generate_virtual_key` does).
- Money is USD as `REAL`, rates are USD per million tokens. Cache defaults when not given: write 5m = input × 1.25, write 1h = input × 2.0, read = input × 0.1 (same as `config.Price`).
- Error bodies on the proxy are Anthropic-shaped: `{"type":"error","error":{"type":…,"message":…}}` via `app.api_error` / `limits.Decision`.
- Low-credit message, verbatim: `Your credit balance is too low to access the API. Ask the gateway admin to add credit.`
- API disabled message, verbatim: `The API is not enabled on this gateway.`
- Dashboard refusal for API keys, verbatim: `API keys work only with Claude Code (/v1/messages); sign in to the dashboard instead.`
- Every new dashboard string exists in both `en` and `fa` of `static/i18n.js` with the same `{placeholders}`. Persian: use loanwords (سیستم, سرویس, لینک), «کلاد» not "Claude Code", never «تیم»; Persian letters only (no ي/ك). `app.js` holds no Persian.
- Comment density and style: match the surrounding code (short "why" comments, no docstring essays).

## Review Focus

1. **A stream cut short by the client or upstream** — must still be charged for the tokens the meter counted (not zero, not the full `max_tokens`). Test in Task 3.
2. **An upstream error (429/529/500) or 401 with no usage** — must charge nothing and write no ledger row, yet still count toward rpm. Test in Task 3.
3. **An API key belonging to an admin user** — must never get admin treatment (account headers stripped, Anthropic's 429/401 bodies rewritten). Test in Task 3.
4. **Concurrent requests near zero balance** — at most `max_inflight` requests pass the ≤ 0 check, so overdraft is bounded; the next one after the balance goes negative is refused. Test in Task 3.
5. **A revoked key, or a key of a disabled/revoked user** — 401/403 on the proxy just like other keys; revoking one key leaves the user's others working. Tests in Task 1 (lookup) and Task 3 (proxy).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/claude_proxy/api_credit.py` (new) | Rates, ledger (grant/charge/balance), keys (create/list/revoke), per-account limits, the pre-request gate, usage queries |
| `src/claude_proxy/db.py` | Schema for the four tables + `requests.api_key_id`; `find_user_by_key` learns the `api` scope; `API_KEY_PREFIX` |
| `src/claude_proxy/config.py` | `ApiConfig` and the `[api]` section loader |
| `src/claude_proxy/gateway.py` | Seed rates at start when `[api]` is on |
| `src/claude_proxy/app.py` | Scope `api` in `scope_allows`; gate, inflight, charge, `/v1/models` filter in `handle` |
| `src/claude_proxy/web.py` | `principal` refuses API keys; `/api/session` exposes `api.enabled`; user and admin endpoints |
| `src/claude_proxy/static/app.js` | `api` view; feature check per view |
| `src/claude_proxy/static/i18n.js` | `en` + `fa` strings |
| `config.example.toml`, `README.md` | `[api]` section and a short "Use an API key" section |
| `tests/test_api_credit.py` (new) | Unit tests of `api_credit` and the db/config parts |
| `tests/test_api_proxy.py` (new) | Proxy behaviour with API keys |
| `tests/test_api_web.py` (new) | Dashboard endpoints |

---

### Task 1: Schema, config, key lookup

**Files:**
- Modify: `src/claude_proxy/db.py` (SCHEMA list, `_migrate`, constants near `ROUTES_KEY_PREFIX`, `find_user_by_key`, `delete_user`)
- Modify: `src/claude_proxy/config.py` (new `ApiConfig`, `Config.api`, loader)
- Modify: `config.example.toml`
- Test: `tests/test_api_credit.py`

**Interfaces:**
- Produces: tables `api_keys`, `api_rates`, `api_ledger`, `api_account_limits`; column `requests.api_key_id`; `db.API_KEY_PREFIX = "sk-proxy-a-"`; `find_user_by_key(...)` returns `{..., "key_scope": "api", "role": "user", "api_key_id": int}` for an API key; `Config.api: ApiConfig(enabled: bool=False, max_inflight: int=4, rpm: int=60)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_credit.py`:

```python
"""API keys on prepaid credit (2026-10-21 spec): storage, rates, ledger, keys, gate."""
import time

import pytest

from claude_proxy.config import Config, ConfigError
from claude_proxy.db import API_KEY_PREFIX, create_user, find_user_by_key, hash_key, init_db, revoke


def add_api_key(conn, uid, label="laptop"):
    raw = API_KEY_PREFIX + "x" * 20 + str(uid) + label
    cur = conn.execute("INSERT INTO api_keys(user_id, key_hash, key_prefix, label, created_at) VALUES(?,?,?,?,?)",
                       (uid, hash_key(raw), raw[:14], label, int(time.time())))
    return cur.lastrowid, raw


def test_schema_has_the_api_tables_and_the_request_column(db):
    conn = db[1]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"api_keys", "api_rates", "api_ledger", "api_account_limits"} <= tables
    assert "api_key_id" in {r[1] for r in conn.execute("PRAGMA table_info(requests)")}


def test_old_database_gains_the_request_column(tmp_path):
    path = tmp_path / "old.db"
    conn = init_db(path)
    conn.execute("DROP INDEX idx_requests_api_key")   # SQLite won't drop an indexed column
    conn.execute("ALTER TABLE requests DROP COLUMN api_key_id")
    conn.close()
    conn = init_db(path)
    assert "api_key_id" in {r[1] for r in conn.execute("PRAGMA table_info(requests)")}


def test_an_api_key_maps_to_its_user_with_the_api_scope_and_never_admin(db):
    conn = db[1]
    uid, _ = create_user(conn, "boss", role="admin")
    kid, raw = add_api_key(conn, uid)
    u = find_user_by_key(conn, raw)
    assert (u["id"], u["key_scope"], u["role"], u["api_key_id"]) == (uid, "api", "user", kid)


def test_a_revoked_api_key_is_unknown(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    kid, raw = add_api_key(conn, uid)
    conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (int(time.time()), kid))
    assert find_user_by_key(conn, raw) is None


def test_api_key_of_a_revoked_user_is_still_found_so_auth_can_refuse_it(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    _, raw = add_api_key(conn, uid)
    revoke(conn, uid)
    assert find_user_by_key(conn, raw)["revoked_at"] is not None   # authenticate() turns this into a 401


def test_api_section_switches_the_api_on(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[api]\nmax_inflight = 2\nrpm = 30\n")
    c = Config.load(str(p))
    assert (c.api.enabled, c.api.max_inflight, c.api.rpm) == (True, 2, 30)
    assert Config().api.enabled is False


@pytest.mark.parametrize("body,msg", [
    ("[api]\nenabled = true\n", "remove the [api] section"),
    ("[api]\nfoo = 1\n", "unknown key [api].foo"),
    ("[api]\nrpm = 0\n", "[api].rpm must be a whole number of at least 1"),
    ("[api]\nmax_inflight = 1.5\n", "[api].max_inflight must be a whole number of at least 1"),
])
def test_bad_api_section_is_refused(tmp_path, body, msg):
    p = tmp_path / "c.toml"
    p.write_text(body)
    with pytest.raises(ConfigError, match=msg.replace("[", r"\[").replace("]", r"\]").replace(".", r"\.")):
        Config.load(str(p))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api_credit.py -q`
Expected: FAIL / ERROR (`ImportError: cannot import name 'API_KEY_PREFIX'`).

- [ ] **Step 3: Add the schema**

In `db.py`, append to `SCHEMA` (after the payments index):

```python
    # ---- API keys on prepaid credit (design 2026-10-21) ----
    """CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL,
        label TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_used_at INTEGER,
        revoked_at INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)",
    # USD per million tokens per model glob; the longest matching pattern wins.
    """CREATE TABLE IF NOT EXISTS api_rates (
        pattern TEXT PRIMARY KEY,
        input REAL NOT NULL,
        output REAL NOT NULL,
        cache_write_5m REAL NOT NULL,
        cache_write_1h REAL NOT NULL,
        cache_read REAL NOT NULL,
        updated_at INTEGER NOT NULL,
        updated_by INTEGER
    )""",
    # Append-only: a balance is the sum of its rows. Never touched by retention cleanup.
    """CREATE TABLE IF NOT EXISTS api_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at REAL NOT NULL,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        kind TEXT NOT NULL CHECK(kind IN ('grant','charge')),
        usd REAL NOT NULL,                  -- grants as given (+/-), charges negative
        api_key_id INTEGER,
        request_id INTEGER,
        model TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        actor_user_id INTEGER,
        note TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_api_ledger_user ON api_ledger(user_id, at)",
    "CREATE INDEX IF NOT EXISTS idx_api_ledger_key ON api_ledger(api_key_id, at)",
    """CREATE TABLE IF NOT EXISTS api_account_limits (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        max_inflight INTEGER,               -- NULL: [api].max_inflight
        rpm INTEGER,                        -- NULL: [api].rpm
        updated_at INTEGER NOT NULL,
        updated_by INTEGER
    )""",
```

Add `api_key_id INTEGER` to the `requests` CREATE TABLE (after `rejected_by TEXT`, so new databases have it), and at the end of `_migrate`:

```python
    if "api_key_id" not in _columns(conn, "requests"):
        conn.execute("ALTER TABLE requests ADD COLUMN api_key_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_api_key ON requests(api_key_id, started_at)")
```

(The index goes in `_migrate`, not `SCHEMA`, because `SCHEMA` runs before the column exists on an old database.)

- [ ] **Step 4: Teach `find_user_by_key` the API scope**

Next to `ROUTES_KEY_PREFIX`:

```python
API_KEY_PREFIX = "sk-proxy-a-"      # an API key (2026-10-21): Claude Code billed per token against prepaid credit
```

In `find_user_by_key`, before the `keys` (machine key) lookup:

```python
    a = conn.execute("SELECT id, user_id, last_used_at FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (h,)).fetchone()
    if a is not None:
        row = conn.execute("SELECT * FROM users WHERE id=?", (a["user_id"],)).fetchone()
        now = int(time.time())
        if (a["last_used_at"] or 0) < now - 3600:
            conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now, a["id"]))
        # Never admin powers, like a machine key: an API key is handed to tools and pasted into config files.
        return {**dict(row), "key_scope": "api", "role": "user", "api_key_id": a["id"]}
```

Update the docstring's scope list to mention `"api"`.

- [ ] **Step 5: Add `ApiConfig` and the loader**

In `config.py`, after `LimitsConfig`:

```python
@dataclass
class ApiConfig:
    """API keys on prepaid credit (2026-10-21 spec). On while the [api] section exists, like [tickets]."""
    enabled: bool = False
    max_inflight: int = 4    # per account, unless the admin set its own on the API tab
    rpm: int = 60            # requests per minute per account, likewise
```

Add `api: ApiConfig = field(default_factory=ApiConfig)` to `Config` (after `limits`). In `Config.load`, after the `tickets` block:

```python
        if "api" in data:
            a = dict(data["api"])
            if "enabled" in a:
                raise ConfigError(f"{toml_path}: [api].enabled is not a setting: the API is on while the [api] section "
                                  "exists; remove the [api] section to switch it off")
            cfg.api = ApiConfig(enabled=True)
            for k, v in a.items():
                if k not in ("max_inflight", "rpm"):
                    raise ConfigError(f"{toml_path}: unknown key [api].{k}; [api] takes max_inflight, rpm")
                if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                    raise ConfigError(f"{toml_path}: [api].{k} must be a whole number of at least 1")
                setattr(cfg.api, k, v)
```

In `config.example.toml`, after `[limits]`:

```toml
# API keys billed per token against prepaid credit (Claude Code with ANTHROPIC_API_KEY). Present = on.
# [api]
# max_inflight = 4          # concurrent requests per account (the admin can override per account)
# rpm = 60                  # requests per minute per account
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api_credit.py -q && .venv/bin/pytest -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/test_api_credit.py config.example.toml   # plus your hunks of db.py / config.py (see Global Constraints)
git commit -m "API credit, step 1: schema, [api] config, api key scope"
```

---

### Task 2: `api_credit` core — rates, ledger, keys, limits, gate

**Files:**
- Create: `src/claude_proxy/api_credit.py`
- Modify: `src/claude_proxy/gateway.py` (seed at start)
- Test: `tests/test_api_credit.py` (append)

**Interfaces:**
- Consumes: Task 1 tables, `db.generate_virtual_key`, `db.API_KEY_PREFIX`, `db.audit`, `limits.Decision`, `forwarder.COUNT_TOKENS_PATH`, `config.Price`.
- Produces (exact signatures):
  - `class CreditError(ValueError)`
  - `seed_rates(conn, cfg, now=None) -> int`
  - `rates(conn) -> list[dict]` (keys: pattern, input, output, cache_write_5m, cache_write_1h, cache_read, updated_at)
  - `rate_for(conn, model: str | None) -> dict | None`
  - `set_rate(conn, pattern: str, input, output, cache_write_5m=None, cache_write_1h=None, cache_read=None, actor: int | None = None, now=None) -> dict`
  - `delete_rate(conn, pattern: str, actor: int | None) -> bool`
  - `cost_usd(rate: dict, input_tokens: int, output_tokens: int, cache_write_5m: int, cache_write_1h: int, cache_read: int) -> float`
  - `balance(conn, user_id: int) -> float`
  - `grant(conn, user_id: int, usd: float, note: str, actor: int | None, now=None) -> float` (new balance)
  - `charge(conn, user_id: int, api_key_id: int, request_id: int, model: str | None, row: dict, now=None) -> float` (USD charged, 0.0 = no row)
  - `create_key(conn, user_id: int, label: str, actor: int | None = None) -> tuple[int, str]`
  - `keys(conn, user_id: int) -> list[dict]`
  - `revoke_key(conn, user_id: int, key_id: int, actor: int | None) -> bool`
  - `account_limits(conn, cfg, user_id: int) -> dict` (`{"max_inflight": int, "rpm": int, "custom": bool}`)
  - `set_account_limits(conn, user_id: int, max_inflight: int | None, rpm: int | None, actor: int | None, now=None) -> None`
  - `evaluate(conn, cfg, user_id: int, model: str | None, path: str, inflight: int = 0, now=None) -> limits.Decision | None`
  - `usage(conn, user_id: int, since: float, tz_offset_s: int = 0) -> list[dict]` (keys: day, api_key_id, requests, input, output, cache_write, cache_read, usd)
  - `grants(conn, user_id: int, limit: int = 50) -> list[dict]`
  - `accounts(conn, cfg, now=None) -> list[dict]` (keys: user_id, name, balance, spent_30d, max_inflight, rpm, custom, keys)
  - Constants: `MAX_KEYS = 20`, `REJECT_INFLIGHT = "api_inflight"`, `REJECT_UNPRICED = "api_unpriced"`, `REJECT_CREDIT = "api_credit"`, `REJECT_RPM = "api_rpm"`, `LOW_CREDIT` (the verbatim message).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_credit.py`:

```python
from claude_proxy import api_credit
from claude_proxy.forwarder import COUNT_TOKENS_PATH


@pytest.fixture
def api_cfg(cfg):
    cfg.api.enabled = True
    cfg.api.max_inflight, cfg.api.rpm = 2, 3
    return cfg


def test_seed_copies_claude_list_prices_once(db, cfg):
    conn = db[1]
    n = api_credit.seed_rates(conn, cfg)
    pats = [r["pattern"] for r in api_credit.rates(conn)]
    assert n == len(pats) and all(p.startswith("claude-") for p in pats) and "muse-spark*" not in pats
    api_credit.delete_rate(conn, "claude-opus-5*", None)
    assert api_credit.seed_rates(conn, cfg) == 0    # never refills an edited table


def test_longest_matching_pattern_wins_and_defaults_fill_cache_prices(db):
    conn = db[1]
    api_credit.set_rate(conn, "claude-opus-5*", 5, 25)
    api_credit.set_rate(conn, "claude-opus-5-5*", 4, 20, cache_read=0.2)
    assert api_credit.rate_for(conn, "claude-opus-5-5-20261001")["input"] == 4
    assert api_credit.rate_for(conn, "claude-opus-5-1")["input"] == 5
    assert api_credit.rate_for(conn, "claude-haiku-4") is None
    r = api_credit.rate_for(conn, "claude-opus-5-1")
    assert (r["cache_write_5m"], r["cache_write_1h"], r["cache_read"]) == (6.25, 10.0, 0.5)


@pytest.mark.parametrize("args", [("", 1, 1), ("claude-*", -1, 1), ("claude-*", 1, float("nan")), ("x" * 129, 1, 1)])
def test_bad_rates_are_refused(db, args):
    with pytest.raises(api_credit.CreditError):
        api_credit.set_rate(db[1], *args)


def test_cost_counts_every_token_kind():
    r = {"input": 3, "output": 15, "cache_write_5m": 3.75, "cache_write_1h": 6, "cache_read": 0.3}
    assert api_credit.cost_usd(r, 1_000_000, 100_000, 0, 0, 0) == pytest.approx(4.5)
    assert api_credit.cost_usd(r, 0, 0, 1_000_000, 1_000_000, 1_000_000) == pytest.approx(10.05)


def test_grants_and_charges_make_the_balance(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    api_credit.set_rate(conn, "claude-*", 3, 15)
    assert api_credit.grant(conn, uid, 10, "bank transfer", None) == 10
    kid, _ = api_credit.create_key(conn, uid, "laptop")
    usd = api_credit.charge(conn, uid, kid, 1, "claude-sonnet-5",
                            {"input_tokens": 1_000_000, "output_tokens": 0, "cache_creation_tokens": 0,
                             "cache_creation_5m": 0, "cache_creation_1h": 0, "cache_read_tokens": 0})
    assert usd == pytest.approx(3) and api_credit.balance(conn, uid) == pytest.approx(7)
    assert api_credit.grant(conn, uid, -2, "correction", None) == pytest.approx(5)
    assert [g["note"] for g in api_credit.grants(conn, uid)] == ["correction", "bank transfer"]


def test_charge_uses_the_cache_breakdown_like_the_ledger_does(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    api_credit.set_rate(conn, "claude-*", 1, 1, cache_write_5m=2, cache_write_1h=4, cache_read=0)
    row = {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 3_000_000,
           "cache_creation_5m": 1_000_000, "cache_creation_1h": 2_000_000, "cache_read_tokens": 0}
    assert api_credit.charge(conn, uid, 1, 1, "claude-x", row) == pytest.approx(10)
    row = {**row, "cache_creation_5m": 0, "cache_creation_1h": 0}   # no breakdown: all priced as 5-minute writes
    assert api_credit.charge(conn, uid, 1, 2, "claude-x", row) == pytest.approx(6)


def test_a_request_with_no_tokens_or_no_rate_writes_no_charge(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    api_credit.set_rate(conn, "claude-*", 3, 15)
    zero = {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_creation_5m": 0,
            "cache_creation_1h": 0, "cache_read_tokens": 0}
    assert api_credit.charge(conn, uid, 1, 1, "claude-x", zero) == 0.0
    assert api_credit.charge(conn, uid, 1, 2, "gpt-4", {**zero, "input_tokens": 5}) == 0.0
    assert conn.execute("SELECT COUNT(*) FROM api_ledger").fetchone()[0] == 0


@pytest.mark.parametrize("usd,note", [(0, "x"), (float("inf"), "x"), (1e7, "x"), (5, ""), (5, "x" * 201)])
def test_bad_grants_are_refused(db, usd, note):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    with pytest.raises(api_credit.CreditError):
        api_credit.grant(conn, uid, usd, note, None)


def test_keys_create_list_revoke(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    kid, raw = api_credit.create_key(conn, uid, "  laptop ")
    other, raw2 = api_credit.create_key(conn, uid, "ci")
    assert raw.startswith("sk-proxy-a-") and find_user_by_key(conn, raw)["api_key_id"] == kid
    assert [k["label"] for k in api_credit.keys(conn, uid)] == ["ci", "laptop"]
    assert "key_hash" not in api_credit.keys(conn, uid)[0]
    assert api_credit.revoke_key(conn, uid, kid, uid) is True
    assert api_credit.revoke_key(conn, uid, kid, uid) is False
    assert find_user_by_key(conn, raw) is None and find_user_by_key(conn, raw2)["api_key_id"] == other
    bob, _ = create_user(conn, "bob")
    assert api_credit.revoke_key(conn, bob, other, bob) is False   # not bob's


def test_key_count_and_label_are_bounded(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    with pytest.raises(api_credit.CreditError):
        api_credit.create_key(conn, uid, " ")
    for i in range(api_credit.MAX_KEYS):
        api_credit.create_key(conn, uid, f"k{i}")
    with pytest.raises(api_credit.CreditError, match="20"):
        api_credit.create_key(conn, uid, "one more")


def test_account_limits_default_to_config_and_can_be_overridden(db, api_cfg):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    assert api_credit.account_limits(conn, api_cfg, uid) == {"max_inflight": 2, "rpm": 3, "custom": False}
    api_credit.set_account_limits(conn, uid, 10, None, None)
    assert api_credit.account_limits(conn, api_cfg, uid) == {"max_inflight": 10, "rpm": 3, "custom": True}
    api_credit.set_account_limits(conn, uid, None, None, None)
    assert api_credit.account_limits(conn, api_cfg, uid)["custom"] is False


def _forwarded(conn, uid, kid, ago=1, path="/v1/messages"):
    t = time.time() - ago
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, model, status, api_key_id) "
                 "VALUES(?,?,?,?,?,?,?,?)", (uid, t, t, "POST", path, "claude-x", 200, kid))


def test_gate_order_and_messages(db, api_cfg):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    kid, _ = api_credit.create_key(conn, uid, "k")
    api_credit.set_rate(conn, "claude-*", 3, 15)
    ev = lambda model="claude-x", path="/v1/messages", inflight=0: api_credit.evaluate(conn, api_cfg, uid, model, path, inflight)  # noqa: E731
    assert ev(inflight=2).kind == api_credit.REJECT_INFLIGHT and ev(inflight=2).status == 429
    assert ev(model="gpt-4").status == 404 and ev(model="gpt-4").body["error"]["type"] == "not_found_error"
    d = ev()
    assert (d.status, d.kind, d.body["error"]["message"]) == (400, api_credit.REJECT_CREDIT, api_credit.LOW_CREDIT)
    api_credit.grant(conn, uid, 1, "x", None)
    assert ev() is None
    for _ in range(3):
        _forwarded(conn, uid, kid)
    d = ev()
    assert (d.status, d.kind, d.retry_after) == (429, api_credit.REJECT_RPM, 60)


def test_rpm_counts_in_flight_requests_and_ignores_old_and_rejected_ones(db, api_cfg):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    kid, _ = api_credit.create_key(conn, uid, "k")
    api_credit.set_rate(conn, "claude-*", 3, 15)
    api_credit.grant(conn, uid, 1, "x", None)
    _forwarded(conn, uid, kid, ago=61)
    _forwarded(conn, uid, kid)
    conn.execute("UPDATE requests SET rejected_by='api_credit' WHERE id=(SELECT MAX(id) FROM requests)")
    _forwarded(conn, uid, kid)
    assert api_credit.evaluate(conn, api_cfg, uid, "claude-x", "/v1/messages", inflight=1) is None   # 1 + 1 < 3
    assert api_credit.evaluate(conn, api_cfg, uid, "claude-x", "/v1/messages", inflight=2).kind == api_credit.REJECT_INFLIGHT


def test_count_tokens_needs_a_priced_model_and_rpm_room_but_no_credit(db, api_cfg):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    kid, _ = api_credit.create_key(conn, uid, "k")
    api_credit.set_rate(conn, "claude-*", 3, 15)
    assert api_credit.evaluate(conn, api_cfg, uid, "claude-x", COUNT_TOKENS_PATH, inflight=0) is None   # no credit needed
    assert api_credit.evaluate(conn, api_cfg, uid, "gpt-4", COUNT_TOKENS_PATH).status == 404
    for _ in range(3):
        _forwarded(conn, uid, kid, path=COUNT_TOKENS_PATH)
    assert api_credit.evaluate(conn, api_cfg, uid, "claude-x", COUNT_TOKENS_PATH).kind == api_credit.REJECT_RPM


def test_no_model_means_nothing_to_gate(db, api_cfg):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    assert api_credit.evaluate(conn, api_cfg, uid, None, "/v1/models", inflight=99) is None


def test_usage_by_key_and_day_and_admin_accounts(db, api_cfg):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    kid, _ = api_credit.create_key(conn, uid, "k")
    api_credit.set_rate(conn, "claude-*", 1, 1)
    api_credit.grant(conn, uid, 5, "x", None)
    row = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_creation_tokens": 0, "cache_creation_5m": 0,
           "cache_creation_1h": 0, "cache_read_tokens": 0}
    api_credit.charge(conn, uid, kid, 1, "claude-x", row)
    api_credit.charge(conn, uid, kid, 2, "claude-x", row)
    [u] = api_credit.usage(conn, uid, time.time() - 86400)
    assert (u["api_key_id"], u["requests"], u["input"], u["usd"]) == (kid, 2, 2_000_000, pytest.approx(2))
    [a] = api_credit.accounts(conn, api_cfg)
    assert (a["name"], a["balance"], a["spent_30d"], a["keys"]) == ("alice", pytest.approx(3), pytest.approx(2), 1)


def test_gateway_seeds_rates_only_with_the_api_on(db, cfg):
    from tests.conftest import make_gateway
    conn = db[1]
    make_gateway(cfg, conn)
    assert api_credit.rates(conn) == []
    cfg.api.enabled = True
    make_gateway(cfg, conn)
    assert api_credit.rates(conn) != []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api_credit.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'claude_proxy.api_credit'`).

- [ ] **Step 3: Write `api_credit.py`**

```python
"""API keys on prepaid credit (2026-10-21 spec): Claude Code with ANTHROPIC_API_KEY, billed per token.

Rates are USD per million tokens per model glob (the longest matching glob wins). The ledger is append-only: grants
as the admin gave them, charges negative, one per forwarded request that reported tokens. A balance is its sum.
"""
from __future__ import annotations

import math
import sqlite3
import time

from . import db
from .config import Config, Price
from .forwarder import COUNT_TOKENS_PATH
from .limits import Decision

MAX_KEYS = 20
MAX_GRANT_USD = 1_000_000
REJECT_INFLIGHT, REJECT_UNPRICED, REJECT_CREDIT, REJECT_RPM = "api_inflight", "api_unpriced", "api_credit", "api_rpm"
LOW_CREDIT = "Your credit balance is too low to access the API. Ask the gateway admin to add credit."
RATE_FIELDS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")


class CreditError(ValueError):
    pass


def _err(kind: str, message: str) -> dict:
    return {"type": "error", "error": {"type": kind, "message": message}}


def _price(v, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 10_000:
        raise CreditError(f"{what} must be a number from 0 to 10000 (USD per million tokens).")
    return float(v)


# ---------- rates ----------

def rates(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT pattern, input, output, cache_write_5m, cache_write_1h, cache_read, updated_at FROM api_rates")
    return sorted((dict(r) for r in rows), key=lambda r: (-len(r["pattern"]), r["pattern"]))


def rate_for(conn: sqlite3.Connection, model: str | None) -> dict | None:
    if not model:
        return None
    # SQLite's GLOB is case-sensitive like fnmatchcase; lower() matches config.Pricing.price_for.
    row = conn.execute("SELECT * FROM api_rates WHERE lower(?) GLOB lower(pattern) ORDER BY length(pattern) DESC, pattern "
                       "LIMIT 1", (model,)).fetchone()
    return dict(row) if row else None


def set_rate(conn, pattern: str, input, output, cache_write_5m=None, cache_write_1h=None, cache_read=None,
             actor: int | None = None, now=None) -> dict:
    pattern = (pattern or "").strip()
    if not pattern or len(pattern) > 128 or any(c.isspace() for c in pattern):
        raise CreditError("A model pattern needs 1-128 characters and no spaces, such as claude-sonnet-5*.")
    inp, out = _price(input, "The input price"), _price(output, "The output price")
    p = Price(inp, out, *(None if v is None else _price(v, "A cache price") for v in (cache_write_5m, cache_write_1h, cache_read)))
    row = {"pattern": pattern, "input": p.input, "output": p.output, "cache_write_5m": p.cache_write_5m,
           "cache_write_1h": p.cache_write_1h, "cache_read": p.cache_read}
    conn.execute("INSERT INTO api_rates(pattern, input, output, cache_write_5m, cache_write_1h, cache_read, updated_at, updated_by) "
                 "VALUES(:pattern,:input,:output,:cache_write_5m,:cache_write_1h,:cache_read,:at,:by) ON CONFLICT(pattern) DO UPDATE SET "
                 "input=excluded.input, output=excluded.output, cache_write_5m=excluded.cache_write_5m, "
                 "cache_write_1h=excluded.cache_write_1h, cache_read=excluded.cache_read, updated_at=excluded.updated_at, "
                 "updated_by=excluded.updated_by", {**row, "at": int(time.time() if now is None else now), "by": actor})
    db.audit(conn, actor, "api_rate_set", pattern, {k: row[k] for k in RATE_FIELDS})
    return row


def delete_rate(conn, pattern: str, actor: int | None) -> bool:
    n = conn.execute("DELETE FROM api_rates WHERE pattern=?", (pattern,)).rowcount
    if n:
        db.audit(conn, actor, "api_rate_delete", pattern)
    return bool(n)


def seed_rates(conn, cfg: Config, now=None) -> int:
    """Fill an empty rate table from the config's Claude list prices. Never refills: an admin's deletions stay."""
    if conn.execute("SELECT 1 FROM api_rates LIMIT 1").fetchone():
        return 0
    at = int(time.time() if now is None else now)
    n = 0
    for pattern, p in cfg.pricing.prices.items():
        if pattern.startswith("claude-"):
            conn.execute("INSERT INTO api_rates(pattern, input, output, cache_write_5m, cache_write_1h, cache_read, updated_at) "
                         "VALUES(?,?,?,?,?,?,?)", (pattern, p.input, p.output, p.cache_write_5m, p.cache_write_1h, p.cache_read, at))
            n += 1
    return n


def cost_usd(rate: dict, input_tokens: int, output_tokens: int, cache_write_5m: int, cache_write_1h: int, cache_read: int) -> float:
    return (input_tokens * rate["input"] + output_tokens * rate["output"] + cache_write_5m * rate["cache_write_5m"]
            + cache_write_1h * rate["cache_write_1h"] + cache_read * rate["cache_read"]) / 1e6


# ---------- ledger ----------

def balance(conn, user_id: int) -> float:
    return conn.execute("SELECT COALESCE(SUM(usd), 0) FROM api_ledger WHERE user_id=?", (user_id,)).fetchone()[0]


def grant(conn, user_id: int, usd, note: str, actor: int | None, now=None) -> float:
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not math.isfinite(usd) or usd == 0 or abs(usd) > MAX_GRANT_USD:
        raise CreditError(f"The amount must be a non-zero number of USD up to {MAX_GRANT_USD:,}.")
    note = (note or "").strip()
    if not note or len(note) > 200:
        raise CreditError("Give a note of 1-200 characters, such as how it was paid.")
    conn.execute("INSERT INTO api_ledger(at, user_id, kind, usd, actor_user_id, note) VALUES(?,?,'grant',?,?,?)",
                 (time.time() if now is None else now, user_id, float(usd), actor, note))
    db.audit(conn, actor, "api_credit_grant", db._user_name(conn, user_id), {"usd": usd, "note": note})
    return balance(conn, user_id)


def charge(conn, user_id: int, api_key_id: int, request_id: int, model: str | None, row: dict, now=None) -> float:
    """Charge one forwarded request from its `requests` row fields. Split as usage.CW5/CW1 split cache writes."""
    rate = rate_for(conn, model)
    if rate is None:
        return 0.0
    g = lambda k: int(row.get(k) or 0)  # noqa: E731
    split = g("cache_creation_5m") + g("cache_creation_1h") > 0
    cw5 = g("cache_creation_5m") if split else g("cache_creation_tokens")
    cw1 = g("cache_creation_1h") if split else 0
    usd = cost_usd(rate, g("input_tokens"), g("output_tokens"), cw5, cw1, g("cache_read_tokens"))
    if usd <= 0:
        return 0.0
    conn.execute("INSERT INTO api_ledger(at, user_id, kind, usd, api_key_id, request_id, model, input_tokens, output_tokens, "
                 "cache_write_tokens, cache_read_tokens) VALUES(?,?,'charge',?,?,?,?,?,?,?,?)",
                 (time.time() if now is None else now, user_id, -usd, api_key_id, request_id, model,
                  g("input_tokens"), g("output_tokens"), cw5 + cw1, g("cache_read_tokens")))
    return usd


def grants(conn, user_id: int, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT at, usd, note FROM api_ledger WHERE user_id=? AND kind='grant' ORDER BY at DESC, id DESC LIMIT ?",
                        (user_id, limit))
    return [dict(r) for r in rows]


def usage(conn, user_id: int, since: float, tz_offset_s: int = 0) -> list[dict]:
    off = int(tz_offset_s)
    day = f"(CAST((at + {off}) / 86400 AS INTEGER) * 86400 - {off})"
    rows = conn.execute(f"SELECT {day} AS day, api_key_id, COUNT(*) AS requests, SUM(input_tokens) AS input, "
                        "SUM(output_tokens) AS output, SUM(cache_write_tokens) AS cache_write, SUM(cache_read_tokens) AS cache_read, "
                        "-SUM(usd) AS usd FROM api_ledger WHERE user_id=? AND kind='charge' AND at>=? "
                        "GROUP BY day, api_key_id ORDER BY day DESC, api_key_id", (user_id, since))
    return [dict(r) for r in rows]


# ---------- keys ----------

def create_key(conn, user_id: int, label: str, actor: int | None = None) -> tuple[int, str]:
    label = (label or "").strip()
    if not label or len(label) > 64:
        raise CreditError("Name the key in 1-64 characters, such as the computer it is for.")
    n = conn.execute("SELECT COUNT(*) FROM api_keys WHERE user_id=? AND revoked_at IS NULL", (user_id,)).fetchone()[0]
    if n >= MAX_KEYS:
        raise CreditError(f"You have {MAX_KEYS} API keys already; revoke one first.")
    raw, h, prefix = db.generate_virtual_key(db.API_KEY_PREFIX)
    kid = conn.execute("INSERT INTO api_keys(user_id, key_hash, key_prefix, label, created_at) VALUES(?,?,?,?,?)",
                       (user_id, h, prefix, label, int(time.time()))).lastrowid
    db.audit(conn, actor if actor is not None else user_id, "api_key_add", db._user_name(conn, user_id), {"label": label, "prefix": prefix})
    return kid, raw


def keys(conn, user_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, key_prefix, label, created_at, last_used_at FROM api_keys WHERE user_id=? AND revoked_at IS NULL "
                        "ORDER BY created_at DESC, id DESC", (user_id,))
    return [dict(r) for r in rows]


def revoke_key(conn, user_id: int, key_id: int, actor: int | None) -> bool:
    n = conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                     (int(time.time()), key_id, user_id)).rowcount
    if n:
        db.audit(conn, actor, "api_key_revoke", db._user_name(conn, user_id), {"key_id": key_id})
    return bool(n)


# ---------- per-account limits ----------

def account_limits(conn, cfg: Config, user_id: int) -> dict:
    r = conn.execute("SELECT max_inflight, rpm FROM api_account_limits WHERE user_id=?", (user_id,)).fetchone()
    mi = r["max_inflight"] if r and r["max_inflight"] is not None else None
    rpm = r["rpm"] if r and r["rpm"] is not None else None
    return {"max_inflight": mi or cfg.api.max_inflight, "rpm": rpm or cfg.api.rpm, "custom": mi is not None or rpm is not None}


def set_account_limits(conn, user_id: int, max_inflight: int | None, rpm: int | None, actor: int | None, now=None) -> None:
    """None for either: back to the [api] default. Both None removes the row."""
    for v, what in ((max_inflight, "Concurrent requests"), (rpm, "Requests per minute")):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 10_000):
            raise CreditError(f"{what} must be a whole number from 1 to 10000, or empty for the default.")
    if max_inflight is None and rpm is None:
        conn.execute("DELETE FROM api_account_limits WHERE user_id=?", (user_id,))
    else:
        conn.execute("INSERT INTO api_account_limits(user_id, max_inflight, rpm, updated_at, updated_by) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(user_id) DO UPDATE SET max_inflight=excluded.max_inflight, rpm=excluded.rpm, "
                     "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                     (user_id, max_inflight, rpm, int(time.time() if now is None else now), actor))
    db.audit(conn, actor, "api_limits_set", db._user_name(conn, user_id), {"max_inflight": max_inflight, "rpm": rpm})


# ---------- the gate ----------

def evaluate(conn, cfg: Config, user_id: int, model: str | None, path: str, inflight: int = 0, now=None) -> Decision | None:
    """None to forward, or the refusal. Replaces limits.evaluate for API keys: tickets, shares and limit rows don't apply."""
    if model is None:   # GET /v1/models: nothing to price, nothing to charge
        return None
    now = time.time() if now is None else now
    is_count = path == COUNT_TOKENS_PATH
    lim = account_limits(conn, cfg, user_id)
    if not is_count and inflight >= lim["max_inflight"]:
        return Decision(429, REJECT_INFLIGHT, _err("rate_limit_error", f"{inflight} of {lim['max_inflight']} requests are "
                        "already in flight for this account; wait for one to finish."), 1)
    if rate_for(conn, model) is None:
        return Decision(404, REJECT_UNPRICED, _err("not_found_error", f"model: {model} is not available on this API."))
    if not is_count and balance(conn, user_id) <= 0:
        return Decision(400, REJECT_CREDIT, _err("invalid_request_error", LOW_CREDIT))
    # Token counts are free but still reach the subscription, so they count here: a key with no credit can't flood it.
    recent = conn.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND api_key_id IS NOT NULL AND rejected_by IS NULL "
                          "AND started_at>=?", (user_id, now - 60)).fetchone()[0]
    if recent + inflight >= lim["rpm"]:
        return Decision(429, REJECT_RPM, _err("rate_limit_error", f"This account may make {lim['rpm']} requests a minute; "
                        "try again shortly."), 60)
    return None


# ---------- admin view ----------

def accounts(conn, cfg: Config, now=None) -> list[dict]:
    """Every user with a ledger row or a live key."""
    since = (time.time() if now is None else now) - 30 * 86400
    rows = conn.execute(
        "SELECT u.id AS user_id, u.name, COALESCE((SELECT SUM(usd) FROM api_ledger l WHERE l.user_id=u.id), 0) AS balance, "
        "COALESCE((SELECT -SUM(usd) FROM api_ledger l WHERE l.user_id=u.id AND kind='charge' AND at>=?), 0) AS spent_30d, "
        "(SELECT COUNT(*) FROM api_keys k WHERE k.user_id=u.id AND k.revoked_at IS NULL) AS keys FROM users u "
        "WHERE EXISTS(SELECT 1 FROM api_ledger l WHERE l.user_id=u.id) OR EXISTS(SELECT 1 FROM api_keys k WHERE k.user_id=u.id "
        "AND k.revoked_at IS NULL) ORDER BY u.name", (since,))
    return [{**dict(r), **account_limits(conn, cfg, r["user_id"])} for r in rows]
```

Check for an import cycle: `limits` imports `quota, tickets, config, forwarder, usage` — none import `api_credit`, so `from .limits import Decision` is safe. `db._user_name` is module-private but already shared inside the package; use it rather than duplicating.

- [ ] **Step 4: Seed at start**

In `gateway.py`, add `from . import api_credit, tickets` and at the end of `Gateway.__init__`:

```python
        if cfg.api.enabled:
            api_credit.seed_rates(conn, cfg)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api_credit.py -q && .venv/bin/pytest -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/claude_proxy/api_credit.py src/claude_proxy/gateway.py tests/test_api_credit.py
git commit -m "API credit, step 2: rates, ledger, keys, per-account limits, gate"
```

---

### Task 3: Proxy — scope, gate, inflight, charge, model list

**Files:**
- Modify: `src/claude_proxy/app.py` (`scope_allows`, `inflight_of`, `handle`, new `_models_for_api`)
- Test: `tests/test_api_proxy.py` (new)

**Interfaces:**
- Consumes: `api_credit.evaluate`, `api_credit.charge`, `api_credit.rate_for`, `api_credit.REJECT_*`, `find_user_by_key` with `key_scope="api"` and `api_key_id`.
- Produces: `scope_allows(key_scope, method, path, route)` accepting `"api"`; `inflight_of(gw, attr: str = "inflight") -> Inflight`; `requests.api_key_id` set on every row an API key makes.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_proxy.py`:

```python
"""API keys on the proxy (2026-10-21 spec, section 3)."""
import asyncio
import json

import pytest
from fastapi.responses import JSONResponse, StreamingResponse

from claude_proxy import api_credit
from claude_proxy.app import create_app, scope_allows
from claude_proxy.db import create_user, set_enabled
from tests.conftest import asgi_client, make_gateway, message_json, seed_oauth, sse_response

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "stream": True,
       "tools": [{"name": "Read", "input_schema": {"type": "object"}}], "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def env(cfg, db, anthropic):
    conn = db[1]
    cfg.api.enabled = True
    cfg.api.max_inflight, cfg.api.rpm = 2, 100
    uid, full = create_user(conn, "alice")
    seed_oauth(conn, access="oauth-secret-token")
    gw = make_gateway(cfg, conn, anthropic)   # seeds the rates
    api_credit.set_rate(conn, "claude-sonnet-5*", 3, 15)
    kid, raw = api_credit.create_key(conn, uid, "laptop")
    return gw, conn, uid, kid, {"x-api-key": raw}


def last(conn):
    return conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()


def ledger(conn):
    return [dict(r) for r in conn.execute("SELECT kind, usd, api_key_id, request_id, model FROM api_ledger WHERE kind='charge'")]


@pytest.mark.parametrize("method,path,routed,allowed", [
    ("POST", "/v1/messages", False, True),
    ("POST", "/v1/messages/count_tokens", False, True),
    ("GET", "/v1/models", False, True),
    ("POST", "/v1/messages", True, False),
    ("GET", "/v1/messages", False, False),
    ("POST", "/v1/files", False, False),
    ("GET", "/v1/organizations/x", False, False),
])
def test_api_scope(method, path, routed, allowed):
    assert scope_allows("api", method, path, object() if routed else None) is allowed


async def test_streamed_request_passes_through_with_tools_and_is_charged(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    anthropic.queue.append(lambda req: sse_response(model="claude-sonnet-5", input_tokens=1000, output_tokens=200))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
        assert r.status_code == 200 and b"message_stop" in r.content
    sent = anthropic.calls[-1]
    assert sent["json"]["tools"] == MSG["tools"]                       # unchanged
    assert sent["headers"]["authorization"] == "Bearer oauth-secret-token"
    assert "x-api-key" not in sent["headers"]                          # the client's key never goes upstream
    row = last(conn)
    assert (row["api_key_id"], row["input_tokens"], row["output_tokens"]) == (kid, 1000, 200)
    [ch] = ledger(conn)
    assert (ch["api_key_id"], ch["request_id"], ch["model"]) == (kid, row["id"], "claude-sonnet-5")
    assert ch["usd"] == pytest.approx(-(1000 * 3 + 200 * 15) / 1e6)


async def test_low_credit_is_refused_before_forwarding(env, anthropic):
    gw, conn, uid, kid, h = env
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 400 and r.json()["error"]["message"] == api_credit.LOW_CREDIT
    assert anthropic.calls == [] and last(conn)["rejected_by"] == "api_credit" and ledger(conn) == []


async def test_unpriced_model_is_404_and_routed_model_is_403(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    api_credit.delete_rate(conn, "claude-opus-5*", None)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, "model": "claude-opus-5"}, headers=h)
        assert r.status_code == 404 and r.json()["error"]["type"] == "not_found_error"
        r = await c.post("/v1/messages", json={**MSG, "model": "muse-spark"}, headers=h)
        assert r.status_code == 403 and last(conn)["rejected_by"] == "key_scope"
    assert anthropic.calls == []


async def test_upstream_error_without_usage_is_not_charged(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    anthropic.queue.append(lambda req: JSONResponse({"type": "error", "error": {"type": "overloaded_error", "message": "o"}}, status_code=529))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 529 and ledger(conn) == [] and api_credit.balance(conn, uid) == 10
    assert last(conn)["api_key_id"] == kid and last(conn)["rejected_by"] is None   # counts toward rpm


async def test_a_stream_cut_short_is_charged_for_what_was_metered(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    # No message_stop: the upstream ended the stream early (as tests/test_proxy.py simulates it).
    anthropic.queue.append(lambda req: sse_response(model="claude-sonnet-5", input_tokens=1000, output_tokens=200, stop=False))
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages", json=MSG, headers=h)
    [ch] = ledger(conn)
    assert ch["usd"] < 0 and last(conn)["complete"] == 0


async def test_count_tokens_is_free_and_not_counted_for_rpm(env, anthropic):
    gw, conn, uid, kid, h = env   # no credit at all
    anthropic.queue.append(lambda req: JSONResponse({"input_tokens": 12}))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages/count_tokens", json={"model": "claude-sonnet-5", "messages": []}, headers=h)
    assert r.status_code == 200 and ledger(conn) == []


async def test_an_admins_api_key_is_treated_as_a_user(cfg, db, anthropic):
    conn = db[1]
    cfg.api.enabled = True
    uid, _ = create_user(conn, "boss", role="admin")
    seed_oauth(conn)
    gw = make_gateway(cfg, conn, anthropic)
    api_credit.grant(conn, uid, 10, "x", None)
    _, raw = api_credit.create_key(conn, uid, "k")
    anthropic.queue.append(lambda req: JSONResponse(message_json("claude-sonnet-5"), headers={"anthropic-ratelimit-unified-5h-utilization": "0.5"}))
    anthropic.queue.append(lambda req: JSONResponse({"type": "error", "error": {"type": "rate_limit_error", "message": "account x"}}, status_code=429))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, "stream": False}, headers={"x-api-key": raw})
        assert not any(k.startswith("anthropic-ratelimit-") for k in r.headers)
        r = await c.post("/v1/messages", json={**MSG, "stream": False}, headers={"x-api-key": raw})
        assert r.status_code == 429 and "account" not in r.json()["error"]["message"]


async def test_concurrent_requests_are_capped_so_overdraft_is_bounded(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 0.000001, "x", None)
    gate = asyncio.Event()

    async def held_body():   # the upstream answers only once the test opens the gate
        await gate.wait()
        yield json.dumps(message_json("claude-sonnet-5", input_tokens=1000, output_tokens=100)).encode()

    anthropic.default = lambda req: StreamingResponse(held_body(), media_type="application/json")
    async with asgi_client(create_app(gw)) as c:
        first = [asyncio.create_task(c.post("/v1/messages", json={**MSG, "stream": False}, headers=h)) for _ in range(2)]
        await asyncio.sleep(0.05)
        third = await c.post("/v1/messages", json={**MSG, "stream": False}, headers=h)
        assert third.status_code == 429 and last(conn)["rejected_by"] == "api_inflight"
        gate.set()
        assert [t.status_code for t in await asyncio.gather(*first)] == [200, 200]
        r = await c.post("/v1/messages", json={**MSG, "stream": False}, headers=h)
    assert api_credit.balance(conn, uid) < 0                       # overdraft by at most the two in flight
    assert r.status_code == 400 and r.json()["error"]["message"] == api_credit.LOW_CREDIT


async def test_model_list_shows_only_priced_models(env, anthropic):
    gw, conn, uid, kid, h = env
    anthropic.queue.append(lambda req: JSONResponse({"data": [{"id": "claude-sonnet-5"}, {"id": "claude-x-1"}], "has_more": False}))
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/v1/models", headers=h)
    assert [m["id"] for m in r.json()["data"]] == ["claude-sonnet-5"]


async def test_api_keys_are_refused_while_the_api_is_off(env, anthropic):
    gw, conn, uid, kid, h = env
    gw.cfg.api.enabled = False
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 403 and r.json()["error"]["message"] == "The API is not enabled on this gateway."


async def test_disabled_user_and_revoked_key_are_refused_and_other_keys_still_work(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    _, other = api_credit.create_key(conn, uid, "ci")
    api_credit.revoke_key(conn, uid, kid, uid)
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages", json={**MSG, "stream": False}, headers=h)).status_code == 401
        assert (await c.post("/v1/messages", json={**MSG, "stream": False}, headers={"x-api-key": other})).status_code == 200
        set_enabled(conn, uid, False)
        assert (await c.post("/v1/messages", json={**MSG, "stream": False}, headers={"x-api-key": other})).status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api_proxy.py -q`
Expected: FAIL (`scope_allows("api", …)` returns False; API requests go through `limits.evaluate`).

- [ ] **Step 3: Implement in `app.py`**

`scope_allows` — add before the routes-only branch:

```python
    if key_scope == "api":
        # Claude Code billed per token: messages and token counts on the subscription, and the model list. No routes.
        return (method == "POST" and path in ROUTED_PATHS and route is None) or (method == "GET" and path == "/v1/models")
```

Update its docstring to mention the API scope. Note the existing final `return` must stay for `"routes"` only; any other unknown scope still falls to it (keeps `test_an_unknown_key_scope…` meaning).

`inflight_of` — one registry per kind, so a user's Claude Code requests and API requests don't share a cap:

```python
def inflight_of(gw: Gateway, attr: str = "inflight") -> Inflight:
    """The gateway's registry, made on first use so every Gateway (and every test's) has its own. API keys count in
    their own registry ("api_inflight"): their cap is the account's API limit, not [limits] max_inflight."""
    reg = getattr(gw, attr, None)
    if reg is None:
        reg = Inflight()
        setattr(gw, attr, reg)
    return reg
```

Add `from . import api_credit, limits, quota, titles`.

In `handle`, after `base["user_id"] = user["id"]`:

```python
    is_api = user["key_scope"] == "api"
    base["api_key_id"] = user.get("api_key_id")
    inflight = inflight_of(gw, "api_inflight" if is_api else "inflight")
```

Keep the existing `inflight = inflight_of(gw)` at the top of `handle` (the auth-failure `record` needs the name to exist) and reassign it after auth as above; `record` reads it at call time.

Change `record` to charge:

```python
    def record(**kw) -> None:
        nonlocal held
        if held:
            inflight.release(base["user_id"])
            held = False
        try:
            rid = insert_request(conn, **{**base, "ended_at": time.time(), **kw})
            if base.get("api_key_id") and "rejected_by" not in kw:
                # Priced by the model the gate approved; the reply's model id only if that one has no rate.
                model_ = base.get("requested_model") if api_credit.rate_for(conn, base.get("requested_model")) else kw.get("model")
                api_credit.charge(conn, base["user_id"], base["api_key_id"], rid, model_, kw)
        except Exception:
            logger.exception("could not record request")
```

(`base` has no `api_key_id` before auth, so the auth-failure record passes `.get` → None.)

After the `scope_allows` check, add the off-switch (before it, actually, so a disabled API never reaches the scope message):

```python
    if is_api and not cfg.api.enabled:
        record(status=403, stream=0, complete=1, error_type="permission_error", rejected_by="key_scope")
        return api_error(403, "permission_error", "The API is not enabled on this gateway.")
```

Change the scope refusal message to depend on the scope:

```python
        msg = ("API keys reach Claude models only, through /v1/messages." if is_api else
               f"This key is for third-party models only ({_route_patterns(cfg)}). Claude models need your Claude Code key.")
        return api_error(403, "permission_error", msg)
```

Replace the `limits.evaluate` call:

```python
    metered = model is not None and path != COUNT_TOKENS_PATH
    held_now = inflight.get(user["id"]) if metered else 0
    decision = (api_credit.evaluate(conn, cfg, user["id"], model, path, inflight=held_now) if is_api
                else limits.evaluate(conn, cfg, user["id"], model, path, inflight=held_now))
```

Model list: replace the `/v1/models` line with:

```python
        if request.method == "GET" and path == "/v1/models" and status == 200 and route is None:
            if is_api:
                return await _models_for_api(gw, resp, resp_headers, record)
            return await _models_with_routes(gw, resp, resp_headers, record)
```

and add next to `_models_with_routes`:

```python
async def _models_for_api(gw: Gateway, resp: httpx.Response, headers: dict, record) -> Response:
    """Anthropic's list cut to the models an API key may use: those with an API rate."""
    raw = await resp.aread()
    await resp.aclose()
    try:
        data = json.loads(raw)
        data["data"] = [m for m in data.get("data", []) if api_credit.rate_for(gw.conn, m.get("id"))]
        ids = [m["id"] for m in data["data"]]
        data["first_id"], data["last_id"] = (ids[0], ids[-1]) if ids else (None, None)
        raw = json.dumps(data).encode()
    except (ValueError, AttributeError, TypeError, KeyError):
        data = {"data": [], "has_more": False, "first_id": None, "last_id": None}   # unreadable: list nothing rather than everything
        raw = json.dumps(data).encode()
    record(status=200, stream=0, complete=1)
    headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
    return Response(raw, status_code=200, headers=headers, media_type="application/json")
```

`is_admin` is already `user["role"] == "admin"`; `find_user_by_key` returns `role="user"` for API keys, so admin treatment is impossible. Leave a one-line comment there: `# An API key is never an admin's (db.find_user_by_key), so its replies are always scrubbed.`

The routes-only key's own model list is answered before forwarding by `if user["key_scope"] != "full" and path == "/v1/models":` (`app.py:235`). Change it to `== "routes"`, or API keys would get the third-party list and never reach `_models_for_api`.

**Record before closing.** In `relay()`'s `finally`, the existing order is `await resp.aclose()` then `record(...)`. If the server cancels the generator on a client disconnect (Esc in Claude Code), the `await` can be cancelled and `record` never runs: no row, no charge, and the inflight slot is never released. Reorder so `meter.finalize()`/`parse_non_streaming` and `record(...)` (and the title write) run first, then `await resp.aclose()`. Nothing in `record` needs the open response. This fixes the same loss for ticket users.

**API traffic stays out of ticket and limit-row usage.** A user may hold a ticket and API keys; their paid API traffic must not use up the ticket's limit rows or the sign-up credit. Add `AND api_key_id IS NULL` to the user-usage `WHERE` clauses in `limits.py` — `_window_start` (L130), `_window_state` (L157), `_total_state` (L171), `_weighted_since` (L216-218), `free_credit_spend` (L397, L404). Read each `where`/`span` string where it is built and append the condition there once. Leave `quota.py` alone: API requests use the subscriptions and must stay in account attribution (spec §3 notes the one consequence). Add this test to `tests/test_api_proxy.py`:

```python
async def test_api_traffic_does_not_count_against_limit_rows(env, anthropic):
    gw, conn, uid, kid, h = env
    from claude_proxy import limits
    api_credit.grant(conn, uid, 10, "x", None)
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,?)",
                 (uid, "requests_daily", "*", "1", "count", 0))
    async with asgi_client(create_app(gw)) as c:
        for _ in range(2):
            assert (await c.post("/v1/messages", json={**MSG, "stream": False}, headers=h)).status_code == 200
    assert limits.evaluate(conn, gw.cfg, uid, "claude-sonnet-5", "/v1/messages") is None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api_proxy.py -q && .venv/bin/pytest -q`
Expected: all PASS. If `test_a_stream_cut_short…` raises a different exception class from the test client, keep `pytest.raises(Exception)`; what matters is the ledger row.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/app.py tests/test_api_proxy.py
git commit -m "API credit, step 3: proxy gate, per-account inflight, per-request charge"
```

---

### Task 4: Dashboard endpoints

**Files:**
- Modify: `src/claude_proxy/web.py` (`principal`, `/api/session`, new endpoints after the `/api/keys` block, `delete_user` behaviour unchanged)
- Test: `tests/test_api_web.py` (new)

**Interfaces:**
- Consumes: everything `api_credit` produces.
- Produces (JSON):
  - `GET /api/session` → adds `"api": {"enabled": bool}`
  - `GET /api/me/api` → `{"balance": float, "keys": [...], "usage": [...], "grants": [...], "rates": [...], "limits": {...}, "base_url": str}`
  - `POST /api/me/api/keys` `{label}` → `{"ok": true, "id": int, "key": str}`
  - `POST /api/me/api/keys/{kid}/revoke` → `{"ok": true}`; 404 when not yours
  - `GET /api/admin/api` → `{"rates": [...], "accounts": [...]}`
  - `POST /api/admin/api/rates` `{pattern, input, output, cache_write_5m?, cache_write_1h?, cache_read?}` → `{"ok": true, "rate": {...}}`
  - `POST /api/admin/api/rates/delete` `{pattern}` → `{"ok": true}`; 404 when unknown
  - `POST /api/admin/api/credit` `{user, usd, note}` → `{"ok": true, "balance": float}`
  - `POST /api/admin/api/limits` `{user, max_inflight, rpm}` (each a whole number or null) → `{"ok": true, "limits": {...}}`
  - Every `/api/*/api*` route answers 404 `"The API is not enabled on this gateway."` while `[api]` is off.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_web.py`:

```python
"""API keys on the dashboard (2026-10-21 spec, section 5)."""
from claude_proxy import api_credit
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_web import admin_client, bearer, env  # noqa: F401  (fixture)

REFUSAL = "API keys work only with Claude Code (/v1/messages); sign in to the dashboard instead."


def on(gw):
    gw.cfg.api.enabled = True
    api_credit.seed_rates(gw.conn, gw.cfg)


async def test_everything_is_404_while_the_api_is_off(env):  # noqa: F811
    gw, conn, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/me/api", headers=bearer(keys["alice"]))).status_code == 404
        assert (await c.get("/api/session", headers=bearer(keys["alice"]))).json()["api"] == {"enabled": False}


async def test_user_creates_lists_and_revokes_keys_and_sees_balance(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    gw.cfg.listener.public_url = "https://gw.example"
    api_credit.grant(conn, ids["alice"], 7.5, "paid", None)
    async with asgi_client(create_dashboard_app(gw)) as c:
        h = bearer(keys["alice"])
        r = await c.post("/api/me/api/keys", json={"label": "laptop"}, headers=h)
        assert r.status_code == 200 and r.json()["key"].startswith("sk-proxy-a-")
        kid = r.json()["id"]
        me = (await c.get("/api/me/api", headers=h)).json()
        assert me["balance"] == 7.5 and [k["label"] for k in me["keys"]] == ["laptop"] and me["base_url"] == "https://gw.example"
        assert me["grants"][0]["note"] == "paid" and me["rates"] and me["limits"]["rpm"] == gw.cfg.api.rpm
        assert "key" not in me["keys"][0] and "key_hash" not in me["keys"][0]
        assert (await c.post(f"/api/me/api/keys/{kid}/revoke", headers=bearer(keys["bob"]))).status_code == 404
        assert (await c.post(f"/api/me/api/keys/{kid}/revoke", headers=h)).status_code == 200
        assert (await c.get("/api/me/api", headers=h)).json()["keys"] == []


async def test_an_api_key_cannot_use_the_dashboard(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    _, raw = api_credit.create_key(conn, ids["alice"], "k")
    async with asgi_client(create_dashboard_app(gw)) as c:
        for path in ("/api/me/api", "/api/overview", "/api/me/status"):
            r = await c.get(path, headers={"x-api-key": raw})
            assert r.status_code == 403 and r.json()["error"] == REFUSAL
        r = await c.post("/api/login/key", json={"key": raw})
        assert r.status_code == 403


async def test_admin_edits_rates_grants_credit_and_sets_limits(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    async with admin_client(gw) as c:
        r = await c.post("/api/admin/api/rates", json={"pattern": "claude-test-*", "input": 1, "output": 2})
        assert r.status_code == 200 and r.json()["rate"]["cache_read"] == 0.1
        assert (await c.post("/api/admin/api/rates", json={"pattern": "x", "input": -1, "output": 2})).status_code == 400
        assert (await c.post("/api/admin/api/rates/delete", json={"pattern": "claude-test-*"})).status_code == 200
        assert (await c.post("/api/admin/api/rates/delete", json={"pattern": "claude-test-*"})).status_code == 404
        r = await c.post("/api/admin/api/credit", json={"user": "alice", "usd": 20, "note": "card"})
        assert r.json() == {"ok": True, "balance": 20}
        assert (await c.post("/api/admin/api/credit", json={"user": "alice", "usd": 0, "note": "x"})).status_code == 400
        assert (await c.post("/api/admin/api/credit", json={"user": "nobody", "usd": 1, "note": "x"})).status_code == 404
        r = await c.post("/api/admin/api/limits", json={"user": "alice", "max_inflight": 9, "rpm": None})
        assert r.json()["limits"] == {"max_inflight": 9, "rpm": gw.cfg.api.rpm, "custom": True}
        [a] = (await c.get("/api/admin/api")).json()["accounts"]
        assert (a["name"], a["balance"], a["max_inflight"]) == ("alice", 20, 9)
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.post("/api/admin/api/credit", json={"user": "alice", "usd": 5, "note": "x"},
                             headers=bearer(keys["alice"]))).status_code == 403


async def test_writes_need_the_csrf_token(env):  # noqa: F811
    gw, conn, ids, keys = env
    on(gw)
    async with admin_client(gw) as c:
        del c.headers["x-csrf-token"]
        assert (await c.post("/api/me/api/keys", json={"label": "x"})).status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api_web.py -q`
Expected: FAIL (404s from the catch-all / `KeyError: 'api'`).

- [ ] **Step 3: Implement in `web.py`**

Import `api_credit`. In `principal`, before the routes-only check:

```python
            if user["key_scope"] == "api":
                fail(403, "API keys work only with Claude Code (/v1/messages); sign in to the dashboard instead.")
```

In `/api/login/key` (it calls `authenticate` directly), refuse `key_scope == "api"` with the same message and 403, without counting a failed attempt — mirror the existing routes-only check there.

In `/api/session`, after `out["tickets"] = …`: `out["api"] = {"enabled": cfg.api.enabled}`.

After the `/api/keys/{key_id}/remove` route add:

```python
    # ---------- API keys on prepaid credit (2026-10-21 spec) ----------

    def need_api():
        if not cfg.api.enabled:
            fail(404, "The API is not enabled on this gateway.")

    def credit_call(fn, *args):
        try:
            return fn(*args)
        except api_credit.CreditError as e:
            fail(400, str(e))

    @app.get("/api/me/api")
    async def me_api(request: Request, tz_offset_s: int = 0):
        user = principal(request)
        need_api()
        uid = user["id"]
        return {"balance": api_credit.balance(conn, uid), "keys": api_credit.keys(conn, uid),
                "usage": api_credit.usage(conn, uid, time.time() - 30 * 86400, tz_offset_s),
                "grants": api_credit.grants(conn, uid), "rates": api_credit.rates(conn),
                "limits": api_credit.account_limits(conn, cfg, uid), "base_url": cfg.listener.public_url}

    @app.post("/api/me/api/keys")
    async def me_api_key_add(request: Request):
        user = principal(request, write=True)
        need_api()
        body = await _json(request)
        kid, raw = credit_call(api_credit.create_key, conn, user["id"], str(body.get("label", "")))
        return {"ok": True, "id": kid, "key": raw}

    @app.post("/api/me/api/keys/{kid}/revoke")
    async def me_api_key_revoke(request: Request, kid: int):
        user = principal(request, write=True)
        need_api()
        if not api_credit.revoke_key(conn, user["id"], kid, user["id"]):
            fail(404, "No such API key.")
        return {"ok": True}

    @app.get("/api/admin/api")
    async def admin_api(request: Request):
        admin(request)
        need_api()
        return {"rates": api_credit.rates(conn), "accounts": api_credit.accounts(conn, cfg)}

    @app.post("/api/admin/api/rates")
    async def admin_api_rate(request: Request):
        actor = admin(request, write=True)
        need_api()
        body = await _json(request)
        opt = lambda k: None if body.get(k) in (None, "") else _number(body[k], "A cache price")  # noqa: E731
        rate = credit_call(api_credit.set_rate, conn, str(body.get("pattern", "")), _number(body.get("input"), "The input price"),
                           _number(body.get("output"), "The output price"), opt("cache_write_5m"), opt("cache_write_1h"),
                           opt("cache_read"), actor["id"])
        return {"ok": True, "rate": rate}

    @app.post("/api/admin/api/rates/delete")
    async def admin_api_rate_delete(request: Request):
        actor = admin(request, write=True)
        need_api()
        body = await _json(request)
        if not api_credit.delete_rate(conn, str(body.get("pattern", "")), actor["id"]):
            fail(404, "No such rate.")
        return {"ok": True}

    @app.post("/api/admin/api/credit")
    async def admin_api_credit(request: Request):
        actor = admin(request, write=True)
        need_api()
        body = await _json(request)
        u = target_user(body.get("user"))
        bal = credit_call(api_credit.grant, conn, u["id"], _number(body.get("usd"), "The amount"), str(body.get("note", "")), actor["id"])
        return {"ok": True, "balance": bal}

    @app.post("/api/admin/api/limits")
    async def admin_api_limits(request: Request):
        actor = admin(request, write=True)
        need_api()
        body = await _json(request)
        u = target_user(body.get("user"))
        val = lambda k: None if body.get(k) in (None, "") else _number(body[k], "A limit", whole=True)  # noqa: E731
        credit_call(api_credit.set_account_limits, conn, u["id"], val("max_inflight"), val("rpm"), actor["id"])
        return {"ok": True, "limits": api_credit.account_limits(conn, cfg, u["id"])}
```

Clamp `tz_offset_s` in `me_api` to ±14 h (`max(-50400, min(50400, tz_offset_s))`).

**Persian for every new server message, in this task** (`tests/test_i18n.py` requires every literal `fail(...)` text to be in `ERR_FA`, so the suite fails otherwise). Add exact `ERR_FA` entries to `static/i18n.js`, following its shape, for: `The API is not enabled on this gateway.`, `No such API key.`, `No such rate.`, `API keys work only with Claude Code (/v1/messages); sign in to the dashboard instead.`, and every `CreditError` text in `api_credit.py` as written (`A model pattern needs 1-128 characters and no spaces, such as claude-sonnet-5*.`, the three `… must be a number from 0 to 10000 (USD per million tokens).` messages, `The amount must be a non-zero number of USD up to 1,000,000.`, `Give a note of 1-200 characters, such as how it was paid.`, `Name the key in 1-64 characters, such as the computer it is for.`, `You have 20 API keys already; revoke one first.`, `Concurrent requests must be a whole number from 1 to 10000, or empty for the default.`, `Requests per minute must be a whole number from 1 to 10000, or empty for the default.`). These are fixed strings: exact entries, not `ERR_PATTERNS`. Run `.venv/bin/pytest tests/test_i18n.py -q` before committing.

Notes: `target_user` is defined later in the file (`# ---------- admin actions ----------`); place this block **after** `target_user` so the closure name exists at call time (closures resolve at call time anyway, but keep reading order sane). `_number` returns a float for `usd`; `grant` accepts floats. `/api/me/api` returns `float` balances — the test compares `== 7.5` / `== 20`, fine.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api_web.py -q && .venv/bin/pytest -q`
Expected: all PASS. If `test_routes_web.py::test_an_unknown_key_scope…` changes behaviour, you placed the API check wrong: it must match only `key_scope == "api"`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_api_web.py   # plus your hunks of web.py
git commit -m "API credit, step 4: dashboard endpoints for keys, credit, rates and limits"
```

---

### Task 5: Dashboard tab (en + fa)

**Files:**
- Modify: `src/claude_proxy/static/app.js` (`VIEWS`, feature checks in `renderTabs` and `render`, new `renderApi`)
- Modify: `src/claude_proxy/static/i18n.js` (`en` and `fa`)
- Test: `tests/test_i18n.py` (existing; it already checks every `t("…")` key exists in both languages with the same placeholders)

**Interfaces:**
- Consumes: Task 4 endpoints; existing helpers `api`, `esc`, `bdi`, `t`, `fmtUsd`, `fmtNum`, `fmtDate`, `fmtAgo`, `nfFull`, `num`, `keyDialog`, `alertInline`, `isAdmin`, `render`, `$`.
- Produces: view key `api`, feature `"api"`.

- [ ] **Step 1: Feature check per view**

In `app.js` add next to `isAdmin`:

```js
// A view's feature is "tickets" or "api": on when that config section is.
const featureOn = (v) => !v.feature || (v.feature === "api" ? S.api?.enabled : S.tickets?.enabled);
```

Replace `(!v.feature || S.tickets?.enabled)` in `renderTabs` with `featureOn(v)`, and `(!VIEWS[S.tab].feature || S.tickets?.enabled)` in `render` with `featureOn(VIEWS[S.tab])`. Where `/api/session` is read into `S` (search `S.tickets =`), also set `S.api = data.api;` (use the same variable name that line uses).

Add to `VIEWS` after `pricing`: `api: { render: renderApi, feature: "api" },`

- [ ] **Step 2: `renderApi`**

Add after `renderPricing`:

```js
// ---------- API keys on prepaid credit (design 2026-10-21) ----------

async function renderApi(main) {
  const tz = -new Date().getTimezoneOffset() * 60;
  const [me, adm] = await Promise.all([api(`/api/me/api?tz_offset_s=${tz}`), isAdmin() ? api("/api/admin/api") : null]);
  const keyName = Object.fromEntries(me.keys.map((k) => [k.id, k.label]));
  const rateRow = (r, edit) => `<tr><td dir="ltr"><code>${esc(r.pattern)}</code></td>${["input", "output", "cache_write_5m", "cache_write_1h", "cache_read"].map((f) =>
    edit ? `<td class="r"><input type="text" inputmode="decimal" dir="ltr" name="${f}" data-pattern="${esc(r.pattern)}" data-init="${r[f]}" value="${r[f]}" required style="width:70px"></td>`
      : `<td class="r num">${esc(String(r[f]))}</td>`).join("")}${edit ? `<td><button class="btn small danger" type="button" data-rdel="${esc(r.pattern)}">${t("api.delete")}</button></td>` : ""}</tr>`;
  const rateHead = (edit) => `<thead><tr><th>${t("api.model")}</th><th class="r">${t("api.in")}</th><th class="r">${t("api.out")}</th><th class="r">${t("api.cw5")}</th><th class="r">${t("api.cw1")}</th><th class="r">${t("api.cr")}</th>${edit ? "<th></th>" : ""}</tr></thead>`;
  const env = me.base_url ? `ANTHROPIC_BASE_URL=${me.base_url}\nANTHROPIC_API_KEY=sk-proxy-a-…` : t("api.no_base_url");
  main.innerHTML = `<section class="view"><h2>${t("tab.api")}</h2>
    <p class="lede">${t("api.lede")}</p>
    <div class="grid cols-2">
      <div class="card"><h3>${t("api.balance")}</h3><p class="big num ${me.balance <= 0 ? "bad" : ""}">${fmtUsd(me.balance)}</p>
        <p class="sub">${me.balance <= 0 ? t("api.empty") : t("api.limits", { n: nfFull.format(me.limits.max_inflight), rpm: nfFull.format(me.limits.rpm) })}</p>
        ${me.grants.length ? `<details><summary>${t("api.grants")}</summary><table class="data"><tbody>${me.grants.map((g) =>
          `<tr><td>${fmtDate(g.at)}</td><td class="r num">${fmtUsd(g.usd)}</td><td>${bdi(g.note)}</td></tr>`).join("")}</tbody></table></details>` : ""}</div>
      <div class="card"><h3>${t("api.setup")}</h3><p class="sub">${t("api.setup_sub")}</p><pre dir="ltr" class="key">${esc(env)}</pre></div>
    </div>
    <div class="card" style="margin-top:16px"><h3>${t("api.keys")}</h3>
      <form id="f-akey" class="form-grid"><label>${t("api.key_label")}<input name="label" maxlength="64" required placeholder="${esc(t("api.key_label_eg"))}"></label>
        <button class="btn primary" type="submit">${t("api.create")}</button></form><div class="error" id="akey-err"></div>
      <div class="table-wrap"><table class="data"><thead><tr><th>${t("api.key_label")}</th><th>${t("pc.key")}</th><th>${t("pc.authorized")}</th><th>${t("pc.last_used")}</th><th></th></tr></thead><tbody>
      ${me.keys.map((k) => `<tr><td>${bdi(k.label)}</td><td dir="ltr"><code>${esc(k.key_prefix)}…</code></td><td>${fmtDate(k.created_at)}</td><td>${fmtAgo(k.last_used_at)}</td>
        <td><button class="btn small danger" data-krevoke="${k.id}">${t("api.revoke")}</button></td></tr>`).join("") || `<tr><td colspan="5" class="muted">${t("api.no_keys")}</td></tr>`}
      </tbody></table></div></div>
    <div class="card" style="margin-top:16px"><h3>${t("api.usage")}</h3>
      <div class="table-wrap"><table class="data"><thead><tr><th>${t("api.day")}</th><th>${t("api.key_label")}</th><th class="r">${t("metric.requests")}</th><th class="r">${t("api.in")}</th><th class="r">${t("api.out")}</th><th class="r">${t("api.cost")}</th></tr></thead><tbody>
      ${me.usage.map((u) => `<tr><td>${new Date(u.day * 1000).toLocaleDateString(LOC)}</td><td>${keyName[u.api_key_id] != null ? bdi(keyName[u.api_key_id]) : `<span class="muted">${t("api.revoked_key")}</span>`}</td>
        <td class="r num">${nfFull.format(u.requests)}</td><td class="r num">${fmtNum(u.input + u.cache_write + u.cache_read)}</td><td class="r num">${fmtNum(u.output)}</td><td class="r num">${fmtUsd(u.usd)}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">${t("api.no_usage")}</td></tr>`}
      </tbody></table></div></div>
    ${adm ? `
    <div class="card" style="margin-top:16px"><h3>${t("api.rates")}</h3><p class="sub">${t("api.rates_sub")}</p>
      <form id="f-arates"><div class="table-wrap"><table class="data">${rateHead(true)}<tbody>${adm.rates.map((r) => rateRow(r, true)).join("")}</tbody></table></div>
        <div style="margin-top:8px"><button class="btn small" type="submit">${t("app.save")}</button></div><div class="error" id="arates-err"></div></form>
      <form id="f-arate-add" class="form-grid" style="margin-top:12px"><label>${t("api.model")}<input name="pattern" dir="ltr" required placeholder="claude-sonnet-5*"></label>
        <label>${t("api.in")}<input name="input" inputmode="decimal" dir="ltr" required></label><label>${t("api.out")}<input name="output" inputmode="decimal" dir="ltr" required></label>
        <button class="btn" type="submit">${t("api.add_rate")}</button></form><div class="error" id="arate-add-err"></div></div>
    <div class="card" style="margin-top:16px"><h3>${t("api.accounts")}</h3>
      <form id="f-acredit" class="form-grid"><label>${t("app.user")}<input name="user" required></label><label>${t("api.amount")}<input name="usd" inputmode="decimal" dir="ltr" required></label>
        <label>${t("tk.note")}<input name="note" maxlength="200" required></label><button class="btn primary" type="submit">${t("api.add_credit")}</button></form><div class="error" id="acredit-err"></div>
      <div class="table-wrap"><table class="data"><thead><tr><th>${t("app.user")}</th><th class="r">${t("api.balance")}</th><th class="r">${t("api.spent_30d")}</th><th class="r">${t("api.keys")}</th><th class="r">${t("api.max_inflight")}</th><th class="r">${t("api.rpm")}</th><th></th></tr></thead><tbody>
      ${adm.accounts.map((a) => `<tr><td>${bdi(a.name)}</td><td class="r num">${fmtUsd(a.balance)}</td><td class="r num">${fmtUsd(a.spent_30d)}</td><td class="r num">${nfFull.format(a.keys)}</td>
        <td class="r"><input type="text" inputmode="numeric" dir="ltr" data-lim="max_inflight" data-user="${esc(a.name)}" value="${a.custom ? a.max_inflight : ""}" placeholder="${a.max_inflight}" style="width:60px"></td>
        <td class="r"><input type="text" inputmode="numeric" dir="ltr" data-lim="rpm" data-user="${esc(a.name)}" value="${a.custom ? a.rpm : ""}" placeholder="${a.rpm}" style="width:60px"></td>
        <td><button class="btn small" data-lsave="${esc(a.name)}">${t("app.save")}</button></td></tr>`).join("") || `<tr><td colspan="7" class="muted">${t("api.no_accounts")}</td></tr>`}
      </tbody></table></div></div>` : ""}
  </section>`;

  const post = async (path, body, errEl) => { try { const r = await api(path, { method: "POST", body }); return r; } catch (e) { errEl ? (errEl.textContent = e.message) : alertInline(e.message); return null; } };
  $("#f-akey").onsubmit = async (e) => {
    e.preventDefault();
    const r = await post("/api/me/api/keys", { label: e.target.label.value }, $("#akey-err"));
    if (r) keyDialog(t("api.new_key"), r.key, t("api.key_how"));
  };
  main.querySelectorAll("[data-krevoke]").forEach((b) => (b.onclick = async () => {
    if (!confirm(t("api.revoke_confirm"))) return;
    if (await post(`/api/me/api/keys/${b.dataset.krevoke}/revoke`, {})) render();
  }));
  if (!adm) return;
  $("#f-arates").onsubmit = async (e) => {
    e.preventDefault();
    const changed = {};
    e.target.querySelectorAll("input[data-pattern]").forEach((i) => { if (num(i.value) !== Number(i.dataset.init)) (changed[i.dataset.pattern] ||= {})[i.name] = num(i.value); });
    for (const [pattern, edits] of Object.entries(changed)) {
      const r = adm.rates.find((x) => x.pattern === pattern);
      if (!(await post("/api/admin/api/rates", { ...r, ...edits, pattern }, $("#arates-err")))) return;
    }
    render();
  };
  main.querySelectorAll("[data-rdel]").forEach((b) => (b.onclick = async () => {
    if (confirm(t("api.delete_confirm", { pattern: b.dataset.rdel })) && (await post("/api/admin/api/rates/delete", { pattern: b.dataset.rdel }))) render();
  }));
  $("#f-arate-add").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    if (await post("/api/admin/api/rates", { pattern: f.pattern.value.trim(), input: num(f.input.value), output: num(f.output.value) }, $("#arate-add-err"))) render();
  };
  $("#f-acredit").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    if (await post("/api/admin/api/credit", { user: f.user.value.trim(), usd: num(f.usd.value), note: f.note.value }, $("#acredit-err"))) render();
  };
  main.querySelectorAll("[data-lsave]").forEach((b) => (b.onclick = async () => {
    const v = (k) => { const i = main.querySelector(`input[data-lim="${k}"][data-user="${CSS.escape(b.dataset.lsave)}"]`); return i.value.trim() === "" ? null : num(i.value); };
    if (await post("/api/admin/api/limits", { user: b.dataset.lsave, max_inflight: v("max_inflight"), rpm: v("rpm") })) render();
  }));
}
```

Check before writing: that the classes `big`, `bad`, `form-grid`, `key` exist in `app.css` (`grep -n "\.big\b\|\.bad\b\|\.form-grid\|\.key\b" src/claude_proxy/static/app.css`). If `big`/`bad` don't, reuse whatever the Overview card uses for its large number (search `renderOverview` for the class on its headline figure) instead of inventing CSS.

- [ ] **Step 3: Strings**

Add to `I18N.en` (next to `tab.pricing`) and the same keys to `I18N.fa`:

| key | en | fa |
|---|---|---|
| `tab.api` | API | API |
| `api.lede` | Use Claude Code (terminal or VS Code) with an API key. Each request is paid from your credit at the rates below. | با یک کلید API از کلاد (ترمینال یا VS Code) استفاده کنید. هزینهٔ هر درخواست با تعرفه‌های زیر از اعتبار شما کم می‌شود. |
| `api.balance` | Credit | اعتبار |
| `api.empty` | No credit left: requests are refused. Ask the admin to add credit. | اعتباری نمانده و درخواست‌ها رد می‌شوند. از ادمین بخواهید اعتبار اضافه کند. |
| `api.limits` | Up to {n} requests at once and {rpm} a minute. | حداکثر {n} درخواست هم‌زمان و {rpm} درخواست در دقیقه. |
| `api.grants` | Credit added | اعتبارهای اضافه‌شده |
| `api.setup` | Set up | راه‌اندازی |
| `api.setup_sub` | Set these two variables where Claude Code runs (shell profile, or VS Code's Claude Code settings). | این دو متغیر را جایی که کلاد اجرا می‌شود تنظیم کنید (پروفایل شل یا تنظیمات کلاد در VS Code). |
| `api.keys` | API keys | کلیدهای API |
| `api.key_label` | Name | نام |
| `api.key_label_eg` | e.g. work laptop | مثلاً لپ‌تاپ کار |
| `api.create` | Create key | ساخت کلید |
| `api.new_key` | New API key | کلید API جدید |
| `api.key_how` | Put it in ANTHROPIC_API_KEY. | آن را در ANTHROPIC_API_KEY بگذارید. |
| `api.revoke` | Revoke | لغو |
| `api.revoke_confirm` | Revoke this key? Anything using it stops working at once. | این کلید لغو شود؟ هر چیزی که از آن استفاده می‌کند فوراً از کار می‌افتد. |
| `api.no_keys` | No API keys yet. | هنوز کلید API ندارید. |
| `api.usage` | Usage, last 30 days | مصرف ۳۰ روز اخیر |
| `api.day` | Day | روز |
| `api.in` | Input | ورودی |
| `api.out` | Output | خروجی |
| `api.cw5` | Cache write 5m | نوشتن کش ۵ دقیقه |
| `api.cw1` | Cache write 1h | نوشتن کش ۱ ساعت |
| `api.cr` | Cache read | خواندن کش |
| `api.cost` | Cost | هزینه |
| `api.revoked_key` | revoked key | کلید لغوشده |
| `api.no_usage` | No usage yet. | هنوز مصرفی نیست. |
| `api.model` | Model | مدل |
| `api.rates` | API rates (USD per million tokens) | تعرفه‌های API (دلار برای هر میلیون توکن) |
| `api.rates_sub` | The most specific matching pattern prices each request. A model with no rate is refused. | هر درخواست با دقیق‌ترین الگوی منطبق قیمت‌گذاری می‌شود. مدلی که تعرفه ندارد رد می‌شود. |
| `api.add_rate` | Add rate | افزودن تعرفه |
| `api.delete` | Delete | حذف |
| `api.delete_confirm` | Delete the rate for {pattern}? Its models will be refused. | تعرفهٔ {pattern} حذف شود؟ مدل‌های آن رد خواهند شد. |
| `api.accounts` | Credit accounts | حساب‌های اعتبار |
| `api.amount` | Amount (USD, negative to correct) | مبلغ (دلار، منفی برای اصلاح) |
| `api.add_credit` | Add credit | افزودن اعتبار |
| `api.spent_30d` | Spent, 30 days | مصرف ۳۰ روز |
| `api.max_inflight` | At once | هم‌زمان |
| `api.rpm` | Per minute | در دقیقه |
| `api.no_accounts` | No one has API credit or keys yet. | هنوز کسی اعتبار یا کلید API ندارد. |

(The server messages' `ERR_FA` entries were added in Task 4.)

**Frontend fixes the review found:**
- `fmtUsd` prints any negative as `<$0.01`. Add `const fmtSigned = (v) => (v < 0 ? `−${fmtUsd(-v)}` : fmtUsd(v));` next to `renderApi` and use it for the balance, the accounts table's balance and the grants list.
- `.big` and `.bad` don't exist in `app.css`, and only `code.key` is styled. Use the class the Overview card uses for its headline figure (search `renderOverview`), and for the low balance reuse an existing warning class (search `app.css` for the one the quota bars use when full). Render the setup snippet as `<code class="key" style="white-space:pre">` instead of `<pre class="key">`.
- `location.origin` is the dashboard, not the proxy. When `me.base_url` is empty, show `t("api.no_base_url")` instead of a URL. Add `api.no_base_url` — en: `The admin has not set the gateway's address yet ([listener] public_url).`, fa: `ادمین هنوز نشانی گیت‌وی را تنظیم نکرده است ([listener] public_url).`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_i18n.py -q && .venv/bin/pytest -q`
Expected: all PASS (keys in both languages, same placeholders, no Persian in `app.js`).

- [ ] **Step 5: See it work**

Start a local gateway with an `[api]` section (use the `run` skill, or `CLAUDE_PROXY_CONFIG=<tmp toml with [api]> .venv/bin/claude-proxy serve` — check `cli.py` for the exact command). Sign in as admin, open `#api`: add credit to a user, edit a rate, create a key as that user. Then point Claude Code at it — `ANTHROPIC_BASE_URL=http://127.0.0.1:8080 ANTHROPIC_API_KEY=<key> claude -p "say hi"` — and confirm the reply, a new usage row, and a lower balance. Check both languages (the language switch) and a phone-width window. Delete the temp database afterwards; never point this at the live gateway.

- [ ] **Step 6: Commit**

```bash
git add src/claude_proxy/static/app.js src/claude_proxy/static/i18n.js   # your hunks only (see Global Constraints)
git commit -m "API credit, step 5: dashboard API tab (en, fa)"
```

---

### Task 6: Docs

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a section**

Under the existing usage/config sections, add "API keys (pay per token)": what `[api]` does, its two settings, how an admin adds credit and edits rates (API tab), and the user's two variables:

```
export ANTHROPIC_BASE_URL=https://<your gateway>
export ANTHROPIC_API_KEY=sk-proxy-a-…
```

plus the VS Code note: set the same two variables in the Claude Code extension's environment settings. State plainly that only Claude Code is supported. Warn: on a machine set up with gclaude/`claude-gateway on`, the gateway key in `x-gateway-key` or `ANTHROPIC_AUTH_TOKEN` wins over `ANTHROPIC_API_KEY` (`auth.credential_from_headers`), so that traffic is billed to the ticket, not the credit; unset those to use the API key. Also note: rates use the longest matching glob, unlike `[pricing.models]` (first match).

- [ ] **Step 2: Full suite and commit**

Run: `.venv/bin/pytest -q`
Expected: all PASS.

```bash
git add README.md
git commit -m "API credit: README section"
```

---

## Self-review notes

- Spec coverage: endpoint/scope (T3), billing + ledger (T2, T3), admin rates seeded from list prices (T2), models only with a rate (T2 gate, T3 model list), top-up by admin (T2, T4), low balance + bounded overdraft (T2, T3), per-account limits with config defaults (T1, T2, T4), FCFS (no priority code: nothing to build), self-serve keys shown once (T2, T4, T5), usage per key per day + admin totals (T2, T4, T5), stateless (pass-through, nothing kept), feature switch (T1, T3, T4, T5), retention keeps the ledger (`db.cleanup` is untouched — it deletes `requests` only), deleted users keep ledger rows (`ON DELETE SET NULL`).
- Overdraft: bounded by count (`max_inflight` requests per account), not by dollars; with large prompts on an expensive model one account can go a few dollars negative. Accepted per the spec; revisit with a max-cost reservation if it bites.
- Models with a zero rate write no ledger row, so their usage doesn't show on the API tab. Acceptable: a zero rate is the admin's choice.
- `delete_user` deletes `requests` rows; ledger rows keep `request_id` pointing at nothing. That's acceptable: usage reads the ledger, never joins `requests`.
