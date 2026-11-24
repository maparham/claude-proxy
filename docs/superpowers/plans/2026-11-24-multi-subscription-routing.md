# Multi-Subscription Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every linked Claude subscription serves traffic at once: each pass belongs to one account and its requests go only there, pass types are sized in Pro weeks so they promise the same usage on any plan, and the admin dashboard works per account.

**Architecture:** Shard inside one gateway. Every account-scoped table gains an `account` slug (`requests`, `quota_snapshots`, `account_plans`, `account_profiles`, `usage_estimates`, `tickets`, `users`), every quota/capacity function takes `account`, and `Gateway` holds one `OAuthBackend` + `Poller` per linked account, synced live. A new `routing.py` picks the request's account (serving pass → `users.account` → refused), a new `plans.py` turns `rate_limit_tier` into a plan and factor, and a new `placement.py` picks the account a sold pass lands on (most free room in Pro weeks). Users, prices, orders and payments stay global.

**Tech Stack:** Python 3 / FastAPI / SQLite (`src/claude_proxy`), vanilla JS dashboard (`static/app.js`, `static/i18n.js`), pytest (`.venv/bin/pytest`).

**Spec:** `docs/superpowers/specs/2026-11-24-multi-subscription-routing-design.md` (source of truth); builds on `2026-10-13-multi-subscription-design.md`. Capacity terms: `2026-11-04-pass-capacity-in-points.md`; pass order: `2026-11-06-pass-switch-design.md`.

## Global Constraints

- No default account. After migration nothing treats the slug `default` specially; no code path picks an account by that name. (The only `'default'` literals left: the migration backfill and the column DEFAULT that backfills pre-migration rows and legacy test fixtures; every writer passes `account` explicitly.)
- A request's account: the serving pass's `account`; with no running pass, `users.account`; neither → 403 `permission_error`, `rejected_by="no_account"`, message exactly `No subscription is assigned to you; ask the admin.` Never retried on another account. Third-party routes (`route is not None`) are untouched and record `account` NULL.
- Plans: `pro`, `max_5x`, `max_20x`; labels "Claude Pro", "Claude Max 5x", "Claude Max 20x". Factors (weekly, 5-hour) default Pro 1/1, Max 5x 5/5, Max 20x 20/20, editable. Detection is a tolerant substring map on `rate_limit_tier` (then `organization_type`); unknown → no plan → the account sells nothing until the admin picks one.
- `share_pct = size × 100 / f_7d`, `share_5h_pct = size_5h × 100 / f_5h` (`size_5h` None → `size`). Shares, `size` and `size_5h` are copied onto the ticket at grant; a later size or factor change never touches a sold pass.
- Capacity knobs stay global (`max_sold_pct`, `max_sold_5h_pct`, `sell_margin_pct`, `borrow_margin_pct`) and apply to each account alike; every capacity check runs on one account with that account's quota.
- Self-service placement: accounts with a known plan, no unacknowledged plan change, an `active` credential and a fresh weekly report, where the pass fits (`capacity.check`); most free room in Pro weeks (`ctx.room × f_7d / 100`), ties by slug. Fits nowhere → sold out, as today.
- Users never see account slugs or emails: no user-facing API (`/api/me/*`, `/api/pricing`, passcard, statusline) includes them.
- Admin per-account APIs take `?account=<slug>`; unknown slug → 404 `No subscription account '<slug>'.`
- Dashboard: Persian loanwords (سیستم, سرویس, لینک), «کلاد» in UI; tooltips only on a `tipI` "?" icon; every new string in both languages of `i18n.js`.
- Work on master; commit only your own paths; tests with `.venv/bin/pytest`. Commit messages: plain sentences, as in `git log`.

## Review Focus

- A user with running passes on two accounts whose serving pass reaches its limit: the next request goes to the other pass's account with that account's token, is recorded there, and the first account's figures do not move → test in Task 3 (`test_serving_pass_switch_moves_account`).
- An account whose credential needs login while another is healthy: its passes' requests get 503 and are never sent to the healthy account; `/health` says `credential: false`; the other account's users are served → test in Task 3 (`test_unhealthy_account_never_fails_over`).
- An account just linked (no profile yet) or with an unrecognised `rate_limit_tier`: placement skips it, the other accounts still sell, and the price list is not sold out because of it → test in Task 5 (`test_unknown_plan_is_skipped_not_sold_out`).
- The migration on a database with sold passes: every ticket keeps its `share_pct`/`share_5h_pct`, and a new sale of each tier on `default` gets exactly today's share (size → share round trip within 1e-9) → test in Task 4 (`test_migration_keeps_tier_shares_on_default`).
- Moving a running pass mid-week from Max 20x to Pro neither refills nor shrinks it: its weekly and 5-hour used % before and after the move agree within 0.1 point, and its borrow debt carries over → test in Task 6 (`test_move_keeps_used_percent`).

---

### Task 1: Schema, migration and the plan map

**Files:**
- Modify: `src/claude_proxy/db.py` (SCHEMA `requests` :~40-77, `quota_snapshots` :81, `account_plans` :93, `tickets` :230, `users`; `_migrate` :520-675)
- Create: `src/claude_proxy/plans.py`
- Test: `tests/test_plans.py`

**Interfaces:**
- Produces columns (fresh schema and `_migrate`, each `ADD COLUMN` only when absent, backfill in the same step):
  - `requests.account TEXT DEFAULT 'default'` + index `idx_requests_account_ended ON requests(account, ended_at)`.
  - `quota_snapshots.account TEXT NOT NULL DEFAULT 'default'`; index `idx_quota_account_bucket_time ON quota_snapshots(account, bucket, observed_at)` (keep the old index).
  - `account_plans.account TEXT NOT NULL DEFAULT 'default'`.
  - `tickets.account TEXT NOT NULL DEFAULT 'default'` (backfill: `account_id=1` → `'default'`; `account_id` stays, unread from Task 2 on) + index `idx_tickets_acct ON tickets(account, starts_at)`; `tickets.size REAL`, `tickets.size_5h REAL` (Pro weeks; backfilled as `share × f(default) / 100` when default's plan is detectable, else left NULL).
  - `users.account TEXT` (backfill `'default'` for every existing user, only in the step that adds the column).
  - Table `ticket_moves(id INTEGER PK, ticket_id INTEGER NOT NULL REFERENCES tickets(id), moved_at INTEGER NOT NULL, from_account TEXT NOT NULL, to_account TEXT NOT NULL, from_share_pct REAL NOT NULL, from_share_5h_pct REAL, carry_7d_pts REAL NOT NULL DEFAULT 0, carry_5h_pts REAL NOT NULL DEFAULT 0, moved_by INTEGER)` + index on `(ticket_id, moved_at)`.
- Produces `plans.py` (imports only `db`-free stdlib; `db._migrate` imports `detect` and `DEFAULT_FACTORS`):
  - `PLANS = ("pro", "max_5x", "max_20x")`, `PLAN_LABELS`, `DEFAULT_FACTORS = {"pro": {"7d": 1.0, "5h": 1.0}, "max_5x": {"7d": 5.0, "5h": 5.0}, "max_20x": {"7d": 20.0, "5h": 20.0}}`.
  - `detect(organization_type: str | None, rate_limit_tier: str | None) -> str | None`: lower-cased `rate_limit_tier` containing `20x` → `max_20x`, else `5x` → `max_5x`, else `pro` → `pro`; then `organization_type` the same way (`claude_pro` → `pro`); a bare `max` with no multiplier, empty or anything else → None.
  - Settings keys: `plan_factors` (JSON), `account_plan:<slug>` (override), `account_plan_ack:<slug>` (the acknowledged tier string: `rate_limit_tier or organization_type`, as `quota.plan_changes` names a plan).
  - `factors(conn) -> dict[str, dict[str, float]]` (stored over defaults); `set_factor(conn, plan: str, f_7d, f_5h, actor: int | None) -> dict` (`ValueError` unless plan in PLANS and both finite, > 0, ≤ 1000); audit `plan_factor_set`.
  - `detected(conn, account: str) -> dict | None` latest `account_plans` row of the account `{observed_at, organization_type, rate_limit_tier, plan}`.
  - `override(conn, account) -> str | None`; `set_override(conn, account, plan: str | None, actor) -> str | None` (None clears; audit `plan_override`).
  - `plan(conn, account) -> str | None` = override, else `detect(...)` of the latest row.
  - `factor(conn, account, bucket: str = "7d") -> float | None`.
  - `shares(conn, account, size: float, size_5h: float | None) -> tuple[float, float] | None` (None without a plan).
  - `pro_weeks(conn, account, points: float, bucket: str = "7d") -> float | None` = `points × f / 100`.
  - `acknowledged(conn, account) -> str | None`; `acknowledge(conn, account, actor) -> None` (stores the latest detected tier string; audit `plan_ack`).
  - `pending_change(conn, account) -> dict | None`: `{"from": ack, "to": latest, "downgrade": bool}` when the latest detected tier string differs from the acknowledged one (downgrade: the new detected plan's `f_7d` is below the acknowledged one's, or the new plan is unknown); None when equal or nothing detected.
  - `sells(conn, account) -> str | None`: None when the account may sell, else the reason: `"plan_unknown"` or `"plan_changed"`.
- Migration also stores `account_plan_ack:default` = default's latest tier string when one exists and no ack is stored.

- [ ] **Step 1: Write failing tests** in `tests/test_plans.py`:
  - `test_detect_map`: `detect("claude_max", "default_claude_max_20x") == "max_20x"`; `detect("claude_max", "default_claude_max_5x") == "max_5x"`; `detect("claude_pro", "default_claude_ai") == "pro"`; `detect(None, "claude_pro_tier") == "pro"`; `detect("claude_max", "default_claude_max") is None`; `detect(None, None) is None`.
  - `test_factors_default_and_set`: `factors(conn)["max_20x"] == {"7d": 20.0, "5h": 20.0}`; after `set_factor(conn, "max_5x", 6, 4, None)`, `factor(conn, acct, "5h") == 4.0` for an account detected as Max 5x; `set_factor(conn, "max_5x", 0, 1, None)` and `set_factor(conn, "team", 1, 1, None)` raise `ValueError`.
  - `test_override_wins`: account `b` recorded `("claude_max","default_claude_max_5x")`, `set_override(conn, "b", "pro", None)` → `plan(conn, "b") == "pro"`; cleared → `"max_5x"`.
  - `test_shares`: on Max 20x `shares(conn, "b", 0.5, None) == (2.5, 2.5)`; on Pro `(50.0, 50.0)`; plan unknown → None.
  - `test_pending_change_and_sells`: ack `default_claude_max_20x`, latest `default_claude_max_5x` → `pending_change` is `{"from": "default_claude_max_20x", "to": "default_claude_max_5x", "downgrade": True}` and `sells == "plan_changed"`; after `acknowledge` → `pending_change is None`, `sells is None`; account with only `default_claude_max` → `sells == "plan_unknown"`.
  - `test_migration_backfills_default`: build an old-shape DB (create the pre-change tables by executing the old `CREATE TABLE` strings for `requests`, `quota_snapshots`, `account_plans`, `tickets`, `users` without the new columns, insert one row each plus an `account_plans` row `("claude_max","default_claude_max_20x")` and a ticket with `share_pct=5, share_5h_pct=10`), run `db._migrate`; assert `requests.account == quota_snapshots.account == tickets.account == users.account == "default"`, ticket `size == 1.0`, `size_5h == 2.0`, `settings['account_plan_ack:default'] == "default_claude_max_20x"`; `_migrate` run twice changes nothing.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_plans.py -v` → FAIL (no module `plans`).
- [ ] **Step 3:** Implement the columns, indexes, `ticket_moves`, the backfills and `plans.py` as above.
- [ ] **Step 4:** `.venv/bin/pytest tests/test_plans.py -v` → PASS; `.venv/bin/pytest -q` → all pass (nothing reads the new columns yet).
- [ ] **Step 5:** Commit `db.py`, `plans.py`, `tests/test_plans.py`.

### Task 2: Quota, capacity and limits per account

**Files:**
- Modify: `src/claude_proxy/quota.py` (every function from `record` :107 to `Poller` :442), `src/claude_proxy/capacity.py` (all `account_id: int` params, `_att` :268, `context` :272, `check_5h_room` :307, `check_sub_end` :329, `Guard` :362), `src/claude_proxy/tickets.py` (`ACCOUNT_ID` :22, `week_bounds` :177, `week_split` :195, `order_split` :225, `subscription_end*` :601-633, `sold_at`/`_starts_within`/`check_capacity`/`verify_capacity` :645-690, inserts :953, `capacity` :986, `_misfit` :1000, `_shift` :1021, `price_table` :1407), `src/claude_proxy/limits.py` (`_share_state` :187, `_ticket_5h` :221, `_free_room` :246, `_ticket_7d` :256, `pass_weeks` :383), `src/claude_proxy/estimates.py`, `src/claude_proxy/trials.py:288-298`, `src/claude_proxy/orders.py:116,267`, `src/claude_proxy/payments.py:153`, `src/claude_proxy/passcard.py:38,110`, `src/claude_proxy/db.py` (`account_profile` → `account_profiles`, `usage_estimates` rebuild, `cleanup` :949), `src/claude_proxy/app.py:183,219`, `src/claude_proxy/web.py` (every `quota.*`, `capacity_mod.*`, `tickets.week_bounds`/`subscription_*` call; `_exhaustion`, `_account_blocked` :2262), `src/claude_proxy/cli.py:113`
- Test: `tests/test_quota_accounts.py`; mechanical updates across `tests/` (add `account="default"` to the ~150 calls listed by `grep -rnE "quota\.(attribution|observed_rate|record_plan|...)\(|capacity\.(context|check|...)\(|tickets\.(sold_at|week_bounds|...)\(" tests`; `1` → `"default"` where tests pass an account id); `tests/conftest.py` `db` fixture key `subscription_ends_at` → `subscription_ends_at:default`.

**Interfaces:**
- Consumes: Task 1 columns.
- Produces (keyword-only, required, so a forgotten call site fails loudly):
  - `quota.record(conn, snaps, now=None, *, account)`, `attribution(conn, pricing, bucket, now=None, stale_after_s=1800, *, account)` (snapshots and requests both filtered `account=?`), `attribution_cached(..., *, account)` (cache key `(id(conn), account, bucket)`), `request_shares(..., *, account)`, `attributed_since(..., *, account)`, `observed_rate(..., *, account)` (cache key with account), `rate_history(..., *, account)`, `plan_changes(conn, *, account)`, `record_plan(conn, organization_type, rate_limit_tier, now, *, account) -> bool`, `record_profile(conn, profile, now, *, account)`, `latest_profile(conn, *, account)`, `buckets(conn, since, *, account)`. `_pairs`/`_ratios`/`_median_ratio`/`_att_inputs` filter likewise. `clear_rate_cache()` unchanged.
  - Table `account_profiles(account TEXT PRIMARY KEY, observed_at REAL NOT NULL, raw_json TEXT NOT NULL)`, migrated from `account_profile` id 1 as `'default'`, old table dropped. `usage_estimates` rebuilt with `account TEXT NOT NULL` and `PRIMARY KEY (account, family, bucket)`; `estimates.refresh` writes one set per account seen in `quota_snapshots` of the last 30 days; `hours_hint(conn, share_pct, *, account)`.
  - `quota.Poller(conn, backend, qc, db=None)` reads `self.account = backend.account`; its snapshot freshness query, `record`, `record_plan`, `record_profile` are per that account. On the first `account_plans` row for an account (record_plan returned False and no ack stored) it calls `plans.acknowledge(conn, account, None)`; on a change it logs the warning with the account and clears the rate cache.
  - `app.USAGE_FRESH_SQL` takes `account` twice as parameters; `app._usage_fresh(conn, account) -> float | None`; `app._record_quota(conn, headers, account)`.
  - `capacity.*`: `account_id: int` → `account: str` in the same position (`reserved`, `week_use`, `room`, `context`, `reserved_5h`, `check_5h_room`, `check`, `Guard`, `_returned`, `summary`, `_sold_rows`, `sold_by_week`); queries use `t.account=?`; `_att(conn, cfg, now, account)`; `check_sub_end(conn, week, ends_at, now, account)`.
  - `tickets.week_bounds(conn, cfg, now, *, account)`, `week_split(conn, cfg, share_pct, starts_at, ends_at, now=None, *, account)`, `order_split(conn, cfg, starts_at, now=None, *, account)`, `terms_text/terms_html/mail_html(..., account=None)` (None → undated text, as with `starts_at=None`), `sold_at(conn, account, t, exclude=None)`, `_starts_within(conn, account, ...)`, `check_capacity(conn, cfg, account, ...)`, `verify_capacity(conn, cfg, account, ...)`, `capacity(conn, cfg, now, account)`.
  - Subscription end per account: `subscription_end_set(conn, account)`, `subscription_end(conn, week, now, account)`, `set_subscription_end(conn, ends_at, actor, account, now=None)`; key `subscription_ends_at:<slug>`; `_migrate` renames a stored `subscription_ends_at` to `subscription_ends_at:default`.
  - Transitional: `tickets.ACCOUNT_ID` becomes `tickets.SELL_ACCOUNT = "default"`, used only where a pass is sold today (grant, preview, `_misfit`, `price_table`, `capacity`, trials, orders, payments) and written to `tickets.account`. Task 5 deletes it.
  - `limits`: every per-pass function uses `t["account"]`; `_shift`'s `Guard` is one per distinct account in the chain (today only `chain[0]`'s, tickets.py:1028). Hand-set `share_5h`/`share_7d` rows (`_share_state`) are measured on `users.account`; NULL → the state is `skipped="no subscription assigned"`.
  - Web point views take `account: str | None = None` query param resolved by a helper in `web.py`: `view_account(account: str | None) -> str | None` = the given slug if linked (404 `No subscription account '<slug>'.` otherwise), else the first linked account (`list_accounts` order), else None (point blocks then empty/None). Used by `/api/session` pill, `/api/overview` (`quota`, `exhaustion`), `/api/quota/timeline`, `/api/admin/capacity`, `/api/admin/subscription*`, `/api/admin/tickets` (`week`), `/api/users` (`share`, this task still one account), `/api/me/status` admin block. `_account_blocked(conn, now, account)`, `_exhaustion(conn, now, account)`.
  - `cli status` quota lines per account (`account <slug> <bucket>: …`).

- [ ] **Step 1: Write failing tests** in `tests/test_quota_accounts.py` (two accounts `a`, `b` seeded with snapshots and requests tagged by `account`):
  - `test_attribution_is_per_account`: `a` rises 0→10 with alice's 10k tokens on `a`, `b` rises 0→50 with bob's on `b`; `attribution(..., account="a")["shares"] == {alice: 10.0}`, `account="b"` → `{bob: 50.0}`.
  - `test_observed_rate_cache_per_account`: `observed_rate(..., account="a")` and `account="b"` return their own medians (1000 and 200 tokens/point) on the same connection and `now`.
  - `test_record_tags_account`: `quota.record(conn, parse_headers(h), account="b")` writes only `account='b'` rows; dedupe compares within the account (identical snapshot on `a` within 60 s is still written).
  - `test_profiles_per_account`: `record_profile(..., account="a")` and `"b"`; `latest_profile(conn, account="b")["profile"]` is b's; migration copies the old id-1 row to `default`.
  - `test_capacity_context_per_account`: a 20% pass on `a` reserves on `a` only: `capacity.context(conn, cfg, "b", NOW).reserved == 0.0`, `"a"` > 0.
  - `test_subscription_end_per_account`: `set_subscription_end(conn, NOW + DAY, None, "b")` → `capacity.check(..., "b", ...)` for a week pass raises `SubscriptionEnds`, `"a"` does not; `_migrate` turns `subscription_ends_at` into `subscription_ends_at:default`.
  - `test_poller_records_its_account`: a `Poller` over a fake backend with `account="b"` whose `poll_usage` returns a body → snapshots with `account='b'`; `check_plan` stores `account_plans.account='b'` and, being the first row, `plans.acknowledged(conn, "b")` equals that tier string.
  - `test_hand_set_share_on_assigned_account`: an ungated user with `share_5h=10` and `users.account='b'` is measured against `b`'s 5-hour attribution; with `users.account=NULL` the state is `skipped`.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_quota_accounts.py -v` → FAIL (unexpected keyword `account`).
- [ ] **Step 3:** Implement as specified, then fix the existing tests mechanically (`account="default"`, `"default"` for account ids, the conftest settings key).
- [ ] **Step 4:** `.venv/bin/pytest -q` → all pass.
- [ ] **Step 5:** Commit the modules and tests above.

### Task 3: One backend and poller per account, and request routing

**Files:**
- Create: `src/claude_proxy/routing.py`
- Modify: `src/claude_proxy/gateway.py` (whole), `src/claude_proxy/app.py` (`Upstream` :149, `/health` :241, `handle` :288-560), `src/claude_proxy/limits.py:584` (`evaluate`), `src/claude_proxy/passkeys.py` (passes `served` through, no change of rules), `src/claude_proxy/db.py:724` (`create_user`), `src/claude_proxy/login.py:86-102` (`remove_account`), `src/claude_proxy/cli.py:95-123` (`status`), `:333` (background tasks), `src/claude_proxy/web.py` (`gw.backend`/`gw.poller` uses: :427, :507-513, :823, :1327, :1347 finish, :1352 remove, :1628)
- Test: `tests/test_routing.py`; update `gw.backend` → `gw.backends["default"]`, `gw.poller` → `gw.pollers["default"]` in `tests/test_review_credentials_meter.py`, `test_hardening.py`, `test_units.py`, `test_routes_proxy.py`, `test_db_off_loop.py`, `test_subscription_end.py`, `test_privacy.py`, `test_review_fixes.py`, `test_proxy.py`, `test_accounts_web.py`, `test_multi_account.py`; `tests/conftest.py` `seed_oauth` also sets `users.account` to its account for users whose account is NULL (test helper only).

**Interfaces:**
- Consumes: Task 2.
- Produces `Gateway`:
  - `backends: dict[str, OAuthBackend]`, `pollers: dict[str, Poller]`, built by `sync_accounts()` in `__init__`; `Gateway(cfg, conn, http=None)` (the `backend=` parameter goes).
  - `sync_accounts() -> tuple[list[str], list[str]]` (added, removed): one backend + poller per `list_accounts` row; existing ones kept (their locks and refresh state survive).
  - `healthy() -> bool`: at least one account and every backend `describe().healthy`.
  - `async run_pollers(interval_s: float = 60.0) -> None`: every interval `sync_accounts()` (so a `claude-proxy login` from another process is picked up), then `tick()` every poller (`asyncio.gather(..., return_exceptions=True)`, exceptions logged). `cli serve` starts it in place of `gw.poller.run()`.
  - `view(conn, http)`: `backends` mapped to `_BackendView` each; `pollers` shared.
- Produces `routing.py`:
  - `NO_ACCOUNT = "no_account"`, `NO_ACCOUNT_MSG = "No subscription is assigned to you; ask the admin."`
  - `assigned(conn, user_id: int) -> str | None` (`users.account`).
  - `account_for(conn, user_id: int, now: float, served: dict | None = None) -> str | None`: `served["account"]` when `limits.evaluate` set it; else the account of the first pass in `limits.pass_order(conn, user_id, tickets.running(...))` (count_tokens and `/v1/models`, which `evaluate` serves without picking a pass); else `assigned`.
- `limits.evaluate`: where it sets `served["ticket_id"]` it also sets `served["account"] = t["account"]`.
- `app.handle`: `base["account"] = None` before auth; after the limits decision and only when `route is None`: `account = routing.account_for(...)` (on the db thread); None → record 403 `no_account` and return `api_error(403, "permission_error", NO_ACCOUNT_MSG)`; an account with no backend in `gw.backends` → 503 as `gateway_needs_login` (admins see "Subscription account '<slug>' is not linked."). `base["account"] = account`; `Upstream(gw, route, account)` uses `gw.backends[account]` for headers and the 401 retry; `_record_quota(conn, resp.headers, account)`.
- `/health`: `credential = gw.healthy()`; `usage_fresh` true only when every linked account's `_usage_fresh` is within `usage_fresh_s`.
- `db.AUTO_ACCOUNT` sentinel; `create_user(conn, name, role="user", password_hash=None, account=AUTO_ACCOUNT)`: AUTO → the only `active` linked account when exactly one is linked, else NULL (Task 5 adds the most-free-room pick at the call sites).
- `login.remove_account`: the `DEFAULT_ACCOUNT` refusal goes; refused with `LoginError` while the account has running or queued passes (`Refusing to remove '<slug>': it has N running or queued passes; move them first.`) or assigned users (`… N users are assigned to it; assign them another subscription first.`); the last-account refusal stays. Web finish/remove call `gw.sync_accounts()`; finish then awaits `gw.pollers[account].check_plan(time.time())` (plan detection right after linking).
- `credentials.DEFAULT_ACCOUNT` remains only as `login --account`'s CLI default slug; `describe()` names every backend `oauth:<slug>`.
- `cli status`: one `credential [<slug>]: OK|NOT OK — …` line per account, then `credential: OK` only when all are healthy (the deploy script greps `^credential: OK`, `deploy/lightsail/claude-gateway-deploy:82`); "No subscription accounts linked" → `credential: NOT OK`.

- [ ] **Step 1: Write failing tests** in `tests/test_routing.py` (two accounts via `seed_oauth(conn, access="tok-a", account="a")`, `("tok-b","b")`, `FakeUpstream` recording the `authorization` header; passes inserted with `tickets.account`; fresh snapshots on both accounts via `tickets_helpers.report` adapted with `account`):
  - `test_pass_account_serves`: alice's running pass on `b`, `users.account='a'` → upstream saw `Bearer tok-b`; row `account == "b"`, `ticket_id` set; the response's rate-limit headers produced `quota_snapshots` rows with `account='b'` only.
  - `test_no_pass_uses_assigned`: ungated user assigned `a` → `Bearer tok-a`, row `account == "a"`.
  - `test_unassigned_refused`: user with `account` NULL and no pass → 403, body message `No subscription is assigned to you; ask the admin.`, row `rejected_by == "no_account"`, no upstream call.
  - `test_serving_pass_switch_moves_account`: passes on `a` (oldest, at its 5-hour limit) and `b` → request goes to `b`; `attribution(..., account="a")` unchanged.
  - `test_count_tokens_and_models_follow_first_pass`: same user, `count_tokens` and `GET /v1/models` → `Bearer tok-<first pass's account>`.
  - `test_unhealthy_account_never_fails_over`: `a` marked `needs_login`, pass on `a` → 503, no request to the upstream with `tok-b`; a user on `b` → 200; `GET /health` → `credential: false`.
  - `test_third_party_route_untouched`: routed model → row `account is None`, no Anthropic token used.
  - `test_auth_refused_has_no_account`: bad key → row `account is None`.
  - `test_sync_accounts_live`: `gw = make_gateway(...)` with `a`; `seed_oauth(..., account="c")`; `gw.sync_accounts() == (["c"], [])`; deleting `c`'s row → `([], ["c"])`; `gw.backends["a"]` is the same object before and after.
  - `test_remove_refused_with_passes_or_users`: `login.remove_account(conn, "b")` raises `LoginError` naming passes while a queued pass is on `b`, and naming users while a user is assigned `b`; after both are cleared it succeeds; removing `default` with nothing on it succeeds when another account is linked.
  - `test_status_lists_every_account`: `cmd_status` output has `credential [a]: OK`, `credential [b]: NOT OK` and no `credential: OK` line; all healthy → `credential: OK`.
  - `test_create_user_single_account`: one linked account → `create_user` sets it; two → NULL.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_routing.py -v` → FAIL.
- [ ] **Step 3:** Implement; update the listed tests' `gw.backend`/`gw.poller` references.
- [ ] **Step 4:** `.venv/bin/pytest -q` → all pass.
- [ ] **Step 5:** Commit.

### Task 4: Pass sizes in Pro weeks

**Files:**
- Modify: `src/claude_proxy/config.py` (`Tier` :120-142, `_default_tiers` :153, validation :421-430), `src/claude_proxy/tickets.py` (`seed_shares` :465 → `seed_sizes`, `tier_shares`, `effective_share*` :481-496, `compare_plan` :498, `effective_tiers` :505, `set_share*`/`set_compare` :516-577, `timeline` :1377, `price_table` compare :1443), `src/claude_proxy/db.py` (`ticket_tier_shares` :199 + `_migrate`), `src/claude_proxy/trials.py` (`Settings` :31, `default_tier` :54, `settings` :62, `_validate`), `src/claude_proxy/referrals.py:116-121` (`pass_cost`), `src/claude_proxy/gateway.py` (seed call), `src/claude_proxy/web.py:1406-1420` (`/api/admin/shares`), `config.example.toml:78-90`, `deploy/lightsail/config.toml:64-74`
- Test: `tests/test_pass_sizes.py`; update `tests/test_tickets_config.py` and tests that build `Tier(...)` or call `set_share*`/`set_compare`; `tests/tickets_helpers.py` gains `link(conn, account="default", plan="pro")` (credential row via `conftest.seed_oauth` + `plans.set_override`) and `seeded()` calls it.

**Interfaces:**
- Consumes: Task 1 `plans.*`, Task 2.
- Produces:
  - `Tier(label: str, size: float, default_usd: dict, size_5h: float | None = None)`; `share_pct`/`share_5h_pct` in a TOML tier → `ConfigError` "`[tickets.tiers.<id>].share_pct` is now `size`, in Claude Pro weeks (share × plan factor ÷ 100)". `size` in (0, 1000]. Validation rejects a tier only when it fits no plan under `DEFAULT_FACTORS`: `size × 100 / 20 > max_sold_pct` (likewise 5-hour vs `max_sold_5h_pct`). Defaults: Lite `size=0.05`, Standard `size=0.25` (today's shares on a Pro account).
  - `ticket_tier_shares` gains `size REAL`, `size_5h REAL`; `_migrate` (when `size` is absent) sets `size = share_pct × f7(default) / 100`, `size_5h = share_5h_pct × f5(default) / 100` (NULL stays NULL), where `f = DEFAULT_FACTORS[plans.plan(conn, "default")]`; with default's plan unknown it leaves `size` NULL and logs a warning (those tiers sell nothing until set on the Pricing tab). `share_pct`, `compare`, `compare_ratio` are no longer read.
  - `tickets.seed_sizes(conn, cfg, now=None) -> int` (INSERT OR IGNORE config sizes); `tier_size(conn, cfg, tier) -> float | None`; `tier_size_5h(conn, cfg, tier) -> float | None` (DB, else config, else `tier_size`); `set_size(conn, cfg, tier, size, actor, now=None) -> float` and `set_size_5h(...)` (refuse ≤ 0, > 1000, or fitting no plan under the current `plans.factors`; audit `size_set`/`size_5h_set`).
  - `tickets.effective_share(conn, cfg, tier, account) -> float | None` and `effective_share_5h(conn, cfg, tier, account) -> float | None` via `plans.shares`; None when the size is unset or the account has no plan.
  - `tickets.effective_tiers(conn, cfg) -> {tier: {label, size, size_5h}}`; the "equals N× Claude Pro" comparison on pages is `size` (price_table: `compare: "Claude Pro"`, `compare_pct: round(size × 100)`; timeline `plan: "Claude Pro"`, `plan_x: Σ size`). `set_compare`/`compare_plan` go.
  - `trials.Settings.size`/`size_5h` replace `share_pct`/`share_5h_pct` (None → the tier's sizes); `default_tier` = smallest `tier_size`; `_migrate` converts stored trial settings' shares with default's factors, like the tiers.
  - `referrals.pass_cost(conn, cfg, tier, days) = account_cost_usd × tier_size × days / 30`; `account_cost_usd` now means one Claude Pro's worth of usage per 30 days; `_migrate` divides the stored value by `f7(default)` (unchanged when unknown).
  - `POST /api/admin/shares` → `POST /api/admin/sizes` `{tier, size}` or `{tier, size_5h}`; `/api/admin/prices` returns `tiers` from `effective_tiers`.
  - Selling still uses `SELL_ACCOUNT` (Task 5), now with `effective_share(..., SELL_ACCOUNT)`; a None share there raises `CapacityError("This subscription's plan is unknown; pick it on the Subscriptions page.")`.
  - Deploy config: tiers rewritten to `size`/`size_5h` with a comment that the database's sizes (migrated from the live shares) govern and these only seed a new tier; Lite `size = 0.05 × f`, Standard `size = 0.25 × f` for the factor of the live account's plan, which the person deploying fills in (the live plan is not read here).

- [ ] **Step 1: Write failing tests** in `tests/test_pass_sizes.py`:
  - `test_share_from_size_per_plan`: Standard `size=0.5` → `effective_share(..., "p")` on Pro 50.0, on Max 5x 10.0, on Max 20x 2.5; with `set_factor("max_20x", 20, 10)` the 5-hour share on Max 20x is 5.0.
  - `test_migration_keeps_tier_shares_on_default`: old DB with `ticket_tier_shares` `(lite, 5, share_5h 7)` and `(standard, 25, NULL)`, default detected Max 20x, a sold ticket `share_pct=25`; after `_migrate`: sizes 1.0 / 1.4 / 5.0 / NULL; `effective_share(conn, cfg, "standard", "default") == 25.0` and `effective_share_5h(..., "lite", "default") == 7.0` within 1e-9; the ticket's `share_pct` still 25.
  - `test_migration_unknown_plan_leaves_size_null`: default with `default_claude_max` only → `tier_size` falls back to the config's; DB `size` is NULL.
  - `test_config_rejects_share_pct`: a TOML tier with `share_pct = 5` → `ConfigError` mentioning `size`; `size = 100` with `max_sold_pct = 80` → `ConfigError` (fits no plan: 100×100/20 = 500 > 80); `size = 10` accepted.
  - `test_set_size_validation`: `set_size(conn, cfg, "lite", 0, ...)` raises; `set_size(..., 0.2, ...)` then `effective_share(..., "lite", "default")` on Pro == 20.0.
  - `test_price_table_compare_is_size`: `compare_pct` of Standard (0.25) is 25 and `compare == "Claude Pro"`.
  - `test_trial_sizes`: trial settings `size=0.01` → trial ticket `share_pct` 1.0 on Pro.
  - `test_pass_cost_in_pro_weeks`: `account_cost_usd=20`, Standard `size=0.25`, 30 days → 5.0.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_pass_sizes.py -v` → FAIL.
- [ ] **Step 3:** Implement; update the existing tests and the two TOML files.
- [ ] **Step 4:** `.venv/bin/pytest -q` → all pass.
- [ ] **Step 5:** Commit.

### Task 5: Placing passes on accounts

**Files:**
- Create: `src/claude_proxy/placement.py`
- Modify: `src/claude_proxy/tickets.py` (`SELL_ACCOUNT` removed; `preview` :818, `grant` :890, `capacity` :986, `sold_out`/`_misfit` :992-1012, `upgrade_quote` :1237, `price_table` :1407, `_terms` :100), `src/claude_proxy/trials.py:270-300`, `src/claude_proxy/orders.py:110-130,260-270,444-499,580-624`, `src/claude_proxy/payments.py:148-160,389,535-554`, `src/claude_proxy/passcard.py:110`, `src/claude_proxy/web.py` (`ticket_preview`/`ticket_grant` :1555-1588, user creation :396 and :1144), `src/claude_proxy/orders.py:667`, `src/claude_proxy/cli.py:152`
- Test: `tests/test_placement.py`

**Interfaces:**
- Consumes: Tasks 2-4.
- Produces `placement.py`:
  - `candidates(conn, cfg, now) -> list[str]`: linked accounts with credential `state='active'`, `plans.sells(conn, a) is None` and `capacity.context(conn, cfg, a, now).fresh`, sorted by slug.
  - `free_pro_weeks(conn, cfg, account, now, ctx=None) -> float` = `max(0, ctx.room) × plans.factor(conn, account) / 100`.
  - `pick(conn, cfg, size, size_5h, starts_at, ends_at, now, exclude=()) -> tuple[str | None, list[dict]]`: every linked account as `{account, share_pct, share_5h_pct, fits, reason, free_pro_weeks}` (reason: `capacity.check`'s message, or "This subscription's plan is unknown; …" / "This subscription's plan changed; acknowledge it on the Subscriptions page." / "Credential needs login." / `NO_REPORT`), and the fitting candidate with the most `free_pro_weeks`, ties by slug. A `FiveHourFull` misfit keeps its `resets_at` in the dict as `available_at`.
  - `pick_tier(conn, cfg, tier, length, starts_at, now, exclude=()) -> tuple[str | None, list[dict]]` (sizes from `tier_size*`).
  - `best_account(conn, cfg, now) -> str | None`: the candidate with the most free Pro weeks, else the first `active` linked account by slug, else None.
  - `any_fresh(conn, cfg, now) -> bool` (`candidates` non-empty).
  - `assign(conn, cfg, user_id: int, now) -> str | None`: `users.account = best_account(...)`; audit `user_account_set` with actor None.
- `tickets.preview(..., account: str | None = None)`: returns `account` (the given one, else `pick_tier`'s) and `accounts` (pick's list); `share_pct`/`share_5h_pct`/`week_split`/`available` are those of `account`; with no account fitting, `available False`, `account None`, `reason` the first account's reason. `tickets.grant(..., account: str | None = None)`: None → the pick (self-service and the default of the admin form); a given account is checked as is (`plans.sells` and capacity) and refused with its reason; inserts `account`, `share_pct`, `share_5h_pct`, `size`, `size_5h`.
- `sold_out(conn, cfg, tier, length, now, starts_at=None, exclude=None, ctx=None)` = fits on no account; `_misfit` returns the misfit with the earliest `resets_at` among `FiveHourFull`/`SubscriptionEnds`, else the first; `price_table` judges per account (one `capacity.context` each), `unavailable = not any_fresh`, `week_split` from `order_split(..., account=best_account)`.
- Upgrades: `upgrade_quote` uses `effective_share(..., ticket["account"])`; bonuses stay on the pass's account (they join on `ticket_id`).
- Trials: `_start_locked` places the trial with `pick(conn, cfg, s.size, s.size_5h, now, end, now, exclude)`; none fits → `NO_ROOM`.
- Orders/payments: `capacity.context(...SELL_ACCOUNT...).fresh` → `placement.any_fresh`; the paid callback's `tickets.grant` passes no account; mails use the granted ticket's `account` for dated terms, undated terms take `best_account`.
- User creation in `web.clerk_account` (:396), `web` admin add (:1144), `orders.link` (:667) and `cli user add` (:152) call `placement.assign` after `create_user` (only when a ticket config exists; else the AUTO rule stands).

- [ ] **Step 1: Write failing tests** in `tests/test_placement.py` (accounts `p` Pro, `m` Max 20x, both linked and fresh, `tickets_helpers.link`):
  - `test_most_free_pro_weeks_wins`: `p` 60% free, `m` 10% free → Pro weeks 0.6 vs 2.0 → a Standard (0.25) week pass goes to `m` with `share_pct == 1.25`; equal room → slug order.
  - `test_fits_only_where_it_fits`: a size whose share is over `max_sold_pct` on Pro (size 0.9, max_sold 80) lands on `m`; `m` full → sold out (`sold_out` True, `grant` raises `CapacityError`).
  - `test_unknown_plan_is_skipped_not_sold_out`: third account `u` with tier `default_claude_max`, most room → not picked; `price_table` not sold out; `pick` lists `u` with the plan-unknown reason.
  - `test_plan_changed_stops_selling`: `m` with a pending change → skipped; after `plans.acknowledge` → picked again.
  - `test_admin_override_account`: `grant(..., account="p")` lands on `p` with Pro's share; `account="p"` when it doesn't fit → `CapacityError` with `p`'s reason, nothing inserted.
  - `test_preview_lists_accounts`: `preview(...)["account"] == "m"` and `accounts` has both with `fits` and `free_pro_weeks`.
  - `test_queued_pass_placed_independently`: user's running pass on `m`, `m` then full → a queued pass goes to `p`.
  - `test_paid_order_autoplaces` (payments callback path as `test_payments.py` drives it): ticket `account == "m"`.
  - `test_trial_placed`: trial claim started → ticket on the best account with that plan's share.
  - `test_signup_assigns_best_account`: Clerk sign-up of a new user → `users.account == "m"`.
  - `test_no_sell_account_constant`: `not hasattr(tickets, "SELL_ACCOUNT")` and neither `'default'` nor `"default"` occurs in `placement.py`, `routing.py`, `tickets.py`, `limits.py`, `capacity.py`, `app.py` (read the files).
- [ ] **Step 2:** `.venv/bin/pytest tests/test_placement.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `.venv/bin/pytest -q` → all pass.
- [ ] **Step 5:** Commit.

### Task 6: Moving a pass

**Files:**
- Modify: `src/claude_proxy/tickets.py` (new `move`), `src/claude_proxy/limits.py` (`_ticket_5h` :221, `_ticket_7d` :256), `src/claude_proxy/capacity.py` (`_week_use` :72, `reserved` :161, `week_use` :202, `reserved_5h` :293)
- Test: `tests/test_pass_move.py`

**Interfaces:**
- Consumes: Task 1 `ticket_moves`, Tasks 2-5.
- Produces:
  - `tickets.move(conn, cfg, actor, ticket_id: int, account: str, now=None, dry_run: bool = False) -> dict` → `{ticket, from, to, share_pct, share_5h_pct, carry_7d_pts, carry_5h_pts}`. One `BEGIN IMMEDIATE`. Refused (`TicketError`) for a cancelled or ended pass, the same account, an unknown/unlinked account, or `plans.sells(target)` not None. Size kept: `size = ticket.size or share_pct × f7_old / 100` (likewise 5-hour); new shares by `plans.shares(target, ...)`; active bonuses' `share_pct`/`share_5h_pct` scaled by `f_old / f_new`. Capacity on the target: `capacity.check(conn, cfg, account, new_share, new_share_5h, max(now, starts_at), effective_end, now)`. Carry (running pass only): `carry_7d_pts = limits._ticket_7d(old).current × f7_old / f7_new`, `carry_5h_pts = limits._ticket_5h(old).current × f5_old / f5_new`; `pass_borrows` rows rescaled `own_pts × f7_old / f7_new`, `rate × f7_new / f7_old` (so `_debt` converts by the same ratio). Writes `tickets.account/share_pct/share_5h_pct`, a `ticket_moves` row, audit `ticket_move`. `dry_run` rolls back and returns the same dict (the admin's preview).
  - `limits._carry(conn, t, since: float) -> tuple[float, float, float]`: `(carry_7d_pts, carry_5h_pts, moved_at)` of the pass's latest move after `since`, else `(0.0, 0.0, since)`. `_ticket_7d`: `used = tokens since max(piece_start, moved_at) / rate + carry_7d`; `_ticket_5h`: live branch `ticket_shares[t] + carry_5h` when `moved_at > window_start`, stale branch counts tokens from `max(start, moved_at)` plus `carry_5h`.
  - `capacity._week_use(conn, cfg, ticket_ids, since, now, rate: float | None = None)`: for a moved pass counts tokens after its latest move inside the span and adds `carry_7d_pts × rate`; `reserved_5h` adds `carry_5h` likewise, so the target's reserved figures match the pass's own limits.

- [ ] **Step 1: Write failing tests** in `tests/test_pass_move.py` (`m` Max 20x, `p` Pro):
  - `test_move_keeps_used_percent`: Standard week pass on `m` (`share 1.25`), used 40% of its weekly piece and 30% of its 5-hour share, with a recorded borrow; `move(..., "p")` → `share_pct == 25.0`; `limits._ticket_7d` and `_ticket_5h` `current / limit` before and after agree within 0.001; `_debt` after equals before × 20; the next request is routed to `p`.
  - `test_move_rechecks_capacity`: `p` full → `CapacityError`, ticket still on `m`, no `ticket_moves` row.
  - `test_move_refusals`: ended pass, same account, unlinked account, target with pending plan change → `TicketError`.
  - `test_move_queued_no_carry`: queued pass moves with `carry_7d_pts == 0` and its shares recomputed.
  - `test_move_frees_source_and_reserves_target`: `capacity.context(...,"m").reserved` drops and `"p"`'s rises by the pass's remaining piece (in each account's points).
  - `test_dry_run_changes_nothing`.
  - `test_bonus_scaled`: an active 2% share bonus on `m` becomes 40% on `p`.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_pass_move.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `.venv/bin/pytest -q` → all pass.
- [ ] **Step 5:** Commit.

### Task 7: Admin APIs per account

**Files:**
- Modify: `src/claude_proxy/web.py` (`scope_where` :454, `/api/overview` :489, `/api/series` :517, `/api/models` :535, `/api/heatmap` :551, `/api/sessions` :564, `/api/errors` :613, `/api/requests` :662, `/api/users` :722, `/api/admin/accounts` :1321, `/api/admin/tickets*` :1528-1612, `/api/me/status` :779, `/api/session` :419)
- Test: `tests/test_accounts_web.py` (extend), `tests/test_routing_web.py`

**Interfaces:**
- Consumes: Tasks 2-6 (`view_account` from Task 2).
- Produces:
  - Token views (`overview`, `series`, `models`, `heatmap`, `sessions`, `errors`, `requests`) take `account`; for an admin with an account resolved by `view_account`, `scope_where` adds `(account=? OR account IS NULL)` (refused-before-routing rows show under every account). Non-admins: `account` ignored. `/api/requests` rows include `account` (admin only).
  - `/api/admin/tickets?account=` filters `t.account=?` and adds `account` to each row; `week` from that account's `week_use`.
  - `GET /api/admin/accounts` → each row adds `plan` (`plans.plan`), `plan_detected` (`detect(...)` of the latest row), `rate_limit_tier`, `plan_override`, `pending_change`, `sells` (reason or None), `factor`, `running_passes`, `queued_passes`, `assigned_users`; top-level `factors` (`plans.factors`), `plans` (PLAN_LABELS); the `default` key goes.
  - `PUT /api/admin/accounts/{account}/plan` `{plan: "pro"|"max_5x"|"max_20x"|null}`; `POST /api/admin/accounts/{account}/plan/ack`; `PUT /api/admin/plan_factors` `{plan, f_7d, f_5h}`; `POST /api/admin/accounts/{account}/refresh` (the old `/api/admin/subscription/refresh`, per account). Write endpoints `admin(request, write=True)`; bad values 400.
  - `PUT /api/admin/users/{uid}/account` `{account}` (PUT, so the catch-all `POST /api/admin/users/{uid}/{action}` :1215 never takes it): linked slug required; audit `user_account_set`.
  - `/api/users`: each user adds `account`; `share` becomes Pro weeks summed over accounts: `Σ_a plans.pro_weeks(conn, a, shares_a[uid], bucket)` (None when every account is stale); `/api/me/*` never include `account`.
  - `POST /api/admin/tickets/preview` and `/api/admin/tickets` take `account` (absent → the pick); preview returns `account` and `accounts`.
  - `POST /api/admin/tickets/{tid}/move` `{account, dry_run}` → `tickets.move`.
  - `/api/me/status` admin block and `_account_blocked` use `routing.account_for(conn, uid, now)`; for an admin the `account` key is the slug-free utilization block as today plus `credential_healthy` of that account's backend.
  - `/api/session` (admin): `accounts: [{account, label, plan, healthy, sells}]` for the picker; `account` pill block for `view_account(?account=)`; `credential` = `gw.healthy()` plus per-account detail.
  - `/health` and `/api/admin/accounts` call nothing that names `default`.

- [ ] **Step 1: Write failing tests:**
  - `test_views_filter_by_account`: rows on `a`, on `b` and one auth-refused (NULL) → `/api/requests?account=a` returns a's and the NULL one, not b's; `/api/overview?account=b` totals only b's; no `account` → first linked account; `?account=zz` → 404 `No subscription account 'zz'.`
  - `test_quota_timeline_and_capacity_per_account`: `/api/quota/timeline?account=b` snapshots are b's; `/api/admin/capacity?account=b` sums b's passes only.
  - `test_accounts_list_has_plans`: Max 20x detected + Pro override on `b` → `plan == "pro"`, `plan_detected == "max_20x"`, `sells is None`; unknown tier → `sells == "plan_unknown"`.
  - `test_plan_override_ack_factors_api`: PUT plan, POST ack, PUT factors change `plans.*`; non-admin 403; missing CSRF 403; `{plan: "team"}` 400.
  - `test_assign_user_account`: PUT `/api/admin/users/{uid}/account` `{account: "b"}` → `users.account == "b"`; unlinked slug 400; `POST /api/admin/users/{uid}/disable` still works.
  - `test_users_share_in_pro_weeks`: alice 10 points on Max 20x `m` and 30 on Pro `p` → `share["7d"] == 2.3`.
  - `test_move_api`: dry run leaves the ticket; real move returns the new account; audit row `ticket_move`.
  - `test_user_views_never_name_accounts`: as a user, `/api/me/status`, `/api/me/tickets`, `/api/pricing` JSON contains neither slug nor credential email (search the serialized body).
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `.venv/bin/pytest -q` → all pass.
- [ ] **Step 5:** Commit.

### Task 8: Dashboard UI

**Files:**
- Modify: `src/claude_proxy/static/index.html:48-58` (header), `src/claude_proxy/static/app.js` (`api` :142 and `BY_SOURCE` :133, `utilPill` :604, `boot` :3345, `renderOverview` :661, `renderUsers` :1522, `renderTickets` :1612, `grantDialog` :1747, `renderPricing` :2030-2160, `renderAccounts` :2909, `renderQuota` :2967), `src/claude_proxy/static/i18n.js`, `src/claude_proxy/static/app.css` (picker only)
- Test: `tests/test_i18n.py` (key parity, no Persian in app.js); manual check per memory "Local dashboard check" (seeded dev gateway on :18081, Playwright) with two linked accounts in the seed.

**Interfaces:**
- Consumes: Task 7 endpoints.
- Produces:
  - Header `<select id="acct-pick">` (admin only, hidden with one account): each option `label · plan` with a dot for health; choice in `localStorage["cp-account"]`, falling back to the first account when the stored slug is gone. `S.acct` holds it; a change re-renders the current view.
  - `PER_ACCOUNT = /^\/api\/(overview|series|models|heatmap|sessions|errors|requests|quota\/timeline|admin\/capacity|admin\/tickets(\?|$)|admin\/subscription|session)/`; `api()` appends `account=<S.acct>` to matching paths for admins (alongside `withSource`).
  - Subscriptions tab: columns Plan (detected label, or a select Pro / Max 5x / Max 20x / "Detect" when unknown or overridden, `tipI` explaining detection and override), Selling (yes / "plan unknown" / "plan changed: acknowledge" button), passes and assigned users counts; a factors card (weekly and 5-hour per plan, `tipI`); a critical banner per account with a downgrade pending ("<label>'s plan went down; it may be oversold. Move passes off it on the Passes tab."). The `acct.no_default` banner goes.
  - Users table: "Subscription" column with a select per user (PUT on change); the share column reads in Pro weeks (`tipI` saying so).
  - Pricing tab: columns Size (Pro weeks) and 5-hour size, replacing share, share 5h, compare and ratio; inputs post to `/api/admin/sizes`; a per-plan hint under each size ("= 25% of Pro · 5% of Max 5x · 1.25% of Max 20x").
  - Grant dialog: a Subscription select preselected with the preview's `account`, each option showing free Pro weeks or the misfit reason; changing it re-previews with `account`.
  - Passes tab (tickets): rows of the picked account; a Move action on running/queued passes → dialog with target select, dry-run preview (new share, carried use) then confirm.
  - Persian strings for all of the above (loanwords, «کلاد»), tooltips only through `tipI`.
- [ ] **Step 1:** Add the i18n keys (English + Persian); `.venv/bin/pytest tests/test_i18n.py -q` → PASS.
- [ ] **Step 2:** Implement the UI.
- [ ] **Step 3:** Local dashboard check with accounts `a` (Pro) and `b` (Max 20x): switch the picker and see quota, passes and requests change; reload keeps the choice; set a plan override and acknowledge a change; assign a user; edit a size and see the per-plan hint; grant on the preselected account and on the other; move a pass. Screenshots in English and Persian.
- [ ] **Step 4:** `.venv/bin/pytest -q` → PASS.
- [ ] **Step 5:** Commit.

---

## Self-review notes

- Spec coverage: §3 routing → Task 3; assignment (builder's call) → Tasks 3 (AUTO rule), 5 (`assign`), 7 (API), 8 (column). §4 plan detection → Tasks 1, 2 (poller ack), 3 (check after linking); factors → Tasks 1, 7, 8; sizes → Task 4; capacity per account → Task 2; placement, admin grant, queued passes, bonuses → Task 5; move with carried use and borrows → Task 6; plan-change stop and downgrade warning → Tasks 1, 5, 7, 8; units across accounts → Task 7 (Users share in Pro weeks). §5 data and migration → Tasks 1, 2, 4. §6 machinery, `/health`, CLI, removal rules → Task 3. §7 dashboard → Tasks 7, 8.
- Builder's choices made here: `tickets.account` text slug beside the dead `account_id`; per-account subscription end date; `view_account` falls back to the first linked account; refused-before-routing rows shown under every account; count_tokens and the model list follow the first pass in pass order; `account_cost_usd` reinterpreted per Pro.
