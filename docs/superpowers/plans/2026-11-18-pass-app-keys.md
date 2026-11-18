# Pass App Keys Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pass holders create named API keys for any Anthropic-format app (Claude Desktop included); those keys are gated and counted by their running pass.

**Architecture:** A fourth key scope `pass` (`sk-proxy-p-…`, table `pass_keys`, column `requests.pass_key_id`). A new module `passkeys.py` holds key CRUD and a gate that requires a running pass, applies an app sub-cap and rpm, then delegates to `limits.evaluate` (shared in-flight count, trial auto-start off), so pass limits apply to app traffic exactly as to Claude Code. `app.py` routes the scope through that gate; `web.py` and `static/app.js` add the "apps" source and the Pass app keys card.

**Tech Stack:** Python 3 / FastAPI / SQLite (`src/claude_proxy`), vanilla JS dashboard (`static/app.js`, `static/i18n.js`), pytest (`uv run pytest`).

**Spec:** `docs/superpowers/specs/2026-11-18-pass-app-keys-design.md` (read §3 and §3a).

## Global Constraints

- Key prefix `sk-proxy-p-`; stored as SHA-256 via `db.generate_virtual_key`; shown once; `MAX_KEYS = 20` live per account; label 1–64 chars.
- Scope `pass`: `role="user"` always, never admin; reaches only `POST /v1/messages`, `POST /v1/messages/count_tokens` (Claude models, `route is None`), `GET /v1/models`.
- Served only while `cfg.tickets.enabled and tickets.is_gated(uid) and tickets.running(uid, now)`; else 403 `permission_error`, `rejected_by='pass_key_no_pass'`, message "This key works while you have a running pass." A pass-key request never calls `trials.autostart`.
- **Never around pass limits.** Pass-key requests hold a slot in the shared `inflight` registry (so Claude Code + apps stay under `[limits] max_inflight`) and also one in `pass_inflight` (sub-cap `PASS_KEY_MAX_INFLIGHT = 4`). `PASS_KEY_RPM = 60` counts `requests` rows with `pass_key_id IS NOT NULL AND rejected_by IS NULL` in the last 60 s plus pass in flight. Pass-key rows have `api_key_id` NULL and `ticket_id` set; no usage or limit query may filter on `pass_key_id` (only the dashboard `SOURCES`).
- Extras refused with `rejected_by='api_extra'`, app-neutral wording (no `/fast`, no `[1m]`).
- Pass-key rows: `api_key_id` NULL, `pass_key_id` set, `ticket_id` from `served` (so they count in the pass's use).
- A computer's key or session (`machine_key_id` / `session_key_id`) cannot create or revoke pass keys.
- Dashboard: API tab visible when `S.api.enabled || S.tickets.enabled`; with `[api]` off it shows only the Pass app keys card. Source toggle values `cli`, `api`, `apps`. Persian: loanwords (سیستم, سرویس, لینک), «کلاد» in UI; tooltips only via `tipI` "?" icon.
- Commit straight to master, only your own paths.

## Review Focus

- A user whose only running pass ends mid-session: the next pass-key request is refused 403 `pass_key_no_pass`, not served by hand-set limits or sign-up credit → test in Task 2.
- A user with a waiting (claimed, not started) free trial pings with a pass key: refused, and the trial stays waiting → test in Task 2.
- Pass-key traffic never shows under `source=cli` and never under `source=api` → test in Task 4.
- A revoked pass key gets 401 at once, while the user's Claude Code key keeps working → test in Task 1.
- Pass-key traffic is counted in pass use exactly like Claude Code traffic, in both directions (apps use up a limit → Claude Code is refused, and vice versa), and the shared in-flight cap holds across both → tests in Tasks 2 and 3.
- Pass keys used against the dashboard (`/api/me`, login form) get 403 with a pass-key message, not "third-party models only" → test in Task 4.

---

### Task 1: Storage and key lookup

**Files:**
- Modify: `src/claude_proxy/db.py` (schema list near `api_keys` ~line 337; migrations near line 591; `PASS_KEY_PREFIX` near line 682; `find_user_by_key` ~707)
- Create: `src/claude_proxy/passkeys.py`
- Test: `tests/test_pass_keys.py`

**Interfaces:**
- Produces: table `pass_keys(id, user_id → users ON DELETE CASCADE, key_hash UNIQUE, key_prefix, label, created_at, last_used_at, revoked_at)` + index on `user_id`; column `requests.pass_key_id INTEGER` + index `(pass_key_id, started_at)`; `db.PASS_KEY_PREFIX = "sk-proxy-p-"`.
- Produces: `find_user_by_key` returns `{..., "key_scope": "pass", "role": "user", "pass_key_id": id}` for a live pass key (same hourly `last_used_at` write as api keys).
- Produces in `passkeys.py`: `class PassKeyError(ValueError)`; `MAX_KEYS = 20`; `create_key(conn, user_id, label, actor=None) -> tuple[int, str]`; `keys(conn, user_id) -> list[dict]` (id, key_prefix, label, created_at, last_used_at); `key_owner(conn, key_id) -> int | None`; `revoke_key(conn, user_id, key_id, actor) -> bool`; `usage(conn, user_id, since, tz_offset_s=0, tz=None) -> list[dict]` per key per day `{day, pass_key_id, requests, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens}`, mirroring `api_credit.usage`'s day bucketing. Audit actions `pass_key_add`, `pass_key_revoke`.

- [ ] **Step 1: Write failing tests** in `tests/test_pass_keys.py`:
  - `test_create_and_find`: `kid, raw = passkeys.create_key(conn, uid, "Desktop")`; `raw.startswith("sk-proxy-p-")`; `db.find_user_by_key(conn, raw)` has `key_scope == "pass"`, `role == "user"` (also for an admin user), `pass_key_id == kid`.
  - `test_label_and_cap`: empty label and 65-char label raise `PassKeyError`; the 21st live key raises `PassKeyError`.
  - `test_revoke`: after `revoke_key`, `find_user_by_key(raw)` is None; the user's own Claude Code key still resolves with scope `full`.
  - `test_migration_adds_column`: `"pass_key_id" in db._columns(conn, "requests")`.
- [ ] **Step 2:** `uv run pytest tests/test_pass_keys.py -v` → FAIL (no module `passkeys`).
- [ ] **Step 3:** Implement the schema, migration, prefix, lookup branch (after the `api_keys` lookup, before `keys`) and the `passkeys.py` functions above, copying `api_credit`'s key functions' shape.
- [ ] **Step 4:** `uv run pytest tests/test_pass_keys.py -v` → PASS; `uv run pytest -q` → no new failures.
- [ ] **Step 5:** Commit `db.py`, `passkeys.py`, `tests/test_pass_keys.py`.

### Task 2: The gate

**Files:**
- Modify: `src/claude_proxy/passkeys.py`, `src/claude_proxy/limits.py:575` (`evaluate`), `src/claude_proxy/api_credit.py:294` (`refuse_extras`)
- Test: `tests/test_pass_keys.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `limits.evaluate(..., autostart: bool = True)`: when False, the `trials.autostart` block is skipped. Nothing else changes for existing callers.
- Produces: `passkeys.NO_PASS = "pass_key_no_pass"`, `REJECT_INFLIGHT = "pass_key_inflight"`, `REJECT_RPM = "pass_key_rpm"`, `PASS_KEY_MAX_INFLIGHT = 4`, `PASS_KEY_RPM = 60`; `passkeys.evaluate(conn, cfg, user_id, model, path, inflight=0, pass_inflight=0, now=None, served=None) -> Decision | None`, where `inflight` is the user's shared in-flight count (Claude Code + apps) and `pass_inflight` the apps' share of it. Order: running pass (403 NO_PASS, every path incl. `/v1/models`); `pass_inflight` ≥ 4 for non-count_tokens with a model (429 `rate_limit_error`, retry 1); rpm on `pass_inflight` (429, retry 60); then `limits.evaluate(..., inflight=inflight, served=served, autostart=False)`, which applies the shared `max_inflight` and every pass and window limit.
- Produces: `api_credit.refuse_extras(data, beta_header, app: bool = False)`: with `app=True`, messages are "Web search is not available with pass app keys; turn web search off in the app.", "Fast mode is not available with pass app keys.", "The 1M-token context is not available with pass app keys; pick a standard model."

- [ ] **Step 1: Write failing tests** (use `tests/tickets_helpers.py` `tcfg`/`seeded`/`live` for a running pass):
  - `test_no_pass_refused`: user with no ticket → `Decision.status == 403`, `kind == "pass_key_no_pass"`.
  - `test_hand_set_limits_not_used`: ungated user with a sign-up credit limit row and no pass → 403 `pass_key_no_pass`.
  - `test_waiting_trial_not_started`: user with a waiting claim (`trial_claims` row, `ticket_id IS NULL AND started_at IS NULL`) → 403 `pass_key_no_pass`; afterwards the claim is still `ticket_id IS NULL AND started_at IS NULL` and the user has no `tickets` row.
  - `test_limits_evaluate_autostart_false`: same waiting-claim user, `limits.evaluate(conn, cfg, uid, "claude-sonnet-5", "/v1/messages", autostart=False)` leaves the claim unstarted; with the default `autostart=True` it starts (proves the flag, not the precheck).
  - `test_running_pass_allowed_and_served`: running pass → `None`, `served["ticket_id"] == t["id"]`.
  - `test_pass_limits_apply`: running pass whose 5-hour share is used up (as `test_tickets_limits.py` sets it up) → 429 with the pass's `share_5h` kind.
  - `test_inflight_and_rpm`: `pass_inflight=4` → 429 `pass_key_inflight`; `inflight=cfg.limits.max_inflight, pass_inflight=0` → 429 `max_inflight`; 60 recent pass-key rows → 429 `pass_key_rpm`.
  - `test_app_use_counts_as_pass_use`: one Claude Code row and one pass-key row with equal tokens, both `ticket_id=t`; `capacity._weighted_since(..., ticket_id=t)` counts both equally, and `tickets.was_used(t)` is True with only the pass-key row.
  - `test_limit_reached_by_apps_blocks_claude_code_and_back`: pass-key rows that use up the 5-hour share → `limits.evaluate` (Claude Code) 429 and `passkeys.evaluate` 429; same with Claude Code rows using it up.
  - `test_refuse_extras_app_wording`: `refuse_extras({"tools":[{"type":"web_search_20250305"}]}, None, app=True)` message contains "pass app keys" and not "/fast"; default `app=False` messages unchanged.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement as specified.
- [ ] **Step 4:** `uv run pytest tests/test_pass_keys.py tests/test_limits.py tests/test_trials.py tests/test_api_credit.py -q` → PASS.
- [ ] **Step 5:** Commit.

### Task 3: Proxy wiring

**Files:**
- Modify: `src/claude_proxy/app.py` (`scope_allows` :41, handler :325–500), `src/claude_proxy/contents.py:12`
- Test: `tests/test_pass_keys_proxy.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces: `scope_allows("pass", ...)` identical to `"api"`. In the handler: `is_pass = key_scope == "pass"`; `base["pass_key_id"] = user.get("pass_key_id")`; a pass-key request holds a slot in the shared `inflight` registry **and** in `"pass_inflight"`, takes both before the gate and releases both on every exit path; scope-refusal message "Pass app keys reach Claude models only, through /v1/messages."; `refuse_extras(..., app=True)` for pass keys on `/v1/messages`; slots: a pass-key request takes a `pass_inflight` slot whenever it is counted for rpm (`metered`, `count_tokens` and `/v1/models`), and a shared `inflight` slot only when `metered` (same as Claude Code, so Desktop's model list isn't refused by the shared cap); read `held_now = inflight.get(uid)` and `pass_held_now = pass_reg.get(uid)`, then acquire both synchronously **before** the gate's `await` (app.py:386-390); gate `passkeys.evaluate(..., inflight=held_now, pass_inflight=pass_held_now, served=served)`; a `pass_held` flag mirrors `held`: `pass_inflight` is released in the same `record()` done callback (after the row is written, never before, app.py:302-316) and at every other release point (app.py:397-400, 404-407, 543-547); `/v1/models` 200 for a pass key returns Anthropic's list as-is (no route entries). No `[api]` check for pass keys. `contents.wanted(keep: bool, session_id, method, path)` — callers pass `is_api or is_pass`. The `record` writer stores `pass_key_id` (follow how `api_key_id` reaches the `requests` insert).

- [ ] **Step 1: Write failing tests** modelled on `tests/test_api_proxy.py`. Build the `env` fixture the way `tests/test_pass_accounting.py:53` does (`env` and `grant` from `tests/test_tickets_web.py`, then a `report()` call so the 7-day snapshot is fresh; without it a gated request gets 503 `TICKET_NO_DATA`, limits.py:657). Use `{"authorization": f"Bearer {raw}"}` to cover Desktop's Bearer scheme):
  - `test_pass_scope` parametrized like `test_api_scope`, with `"pass"`.
  - `test_message_forwarded_and_counted`: 200; last `requests` row has `pass_key_id == kid`, `api_key_id is None`, `ticket_id == t["id"]`; the upstream body's system starts with `CLAUDE_CODE_IDENTITY`; no `api_ledger` row.
  - `test_no_pass_403`: pass cancelled → 403, row `rejected_by == "pass_key_no_pass"`, no upstream call.
  - `test_web_search_refused`: 400, `rejected_by == "api_extra"`.
  - `test_works_with_api_disabled`: `cfg.api.enabled = False` → still 200.
  - `test_models_list_has_no_routes`: `GET /v1/models` → no routed model ids.
  - `test_contents_kept_without_session`: no session header → request contents stored (as `test_request_contents.py` checks for API keys).
  - `test_inflight_shared`: with the user's Claude Code requests at `cfg.limits.max_inflight` in flight, a pass-key request gets 429 `max_inflight`; with 4 pass-key requests in flight, a fifth gets 429 `pass_key_inflight` while a Claude Code request still passes; after requests finish, both registries are back to 0.
  - `test_usage_queries_never_filter_pass_key`: the string `pass_key_id` does not occur at all in `limits.py`, `capacity.py`, `tickets.py`, `quota.py`, `trials.py`, `usage.py`, `estimates.py` (read the files in the test); it may occur only in `passkeys.py`, `db.py`, `app.py`, `web.py`.
  - `test_slots_released_on_failure`: a stream cut short mid-reply and an upstream `httpx` error each leave `inflight` and `pass_inflight` for the user at 0 afterwards.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** Commit.

### Task 4: Dashboard API (server)

**Files:**
- Modify: `src/claude_proxy/web.py` (`SOURCES` :86, `principal` :255, key login :326, requests list :660–700, API endpoints after :1157, `_account_line` :2340)
- Test: `tests/test_pass_keys_web.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `SOURCES = {"all": "1=1", "cli": "api_key_id IS NULL AND pass_key_id IS NULL", "api": "api_key_id IS NOT NULL", "apps": "pass_key_id IS NOT NULL"}`.
  - Pass keys refused at `principal` and key login with 403 "Pass app keys work only in apps (/v1/messages); sign in to the dashboard instead."
  - Requests list: `pass_key_id` selected; `key` name resolved from `pass_keys` too; search matches pass key label/prefix.
  - `_account_line`: `pass app key {prefix}… for {label}`.
  - `GET /api/me/passkeys` → `{"enabled": cfg.tickets.enabled, "keys": [...], "usage": passkeys.usage(... last 30 days, tz), "base_url": cfg.listener.public_url}`; `POST /api/me/passkeys` `{label}` → `{"ok", "id", "key"}`; `POST /api/me/passkeys/{kid}/revoke`. Write endpoints use `principal(request, write=True)` and `not_a_computer` (message generalised to "manage API or app keys").
  - Admin: `GET /api/admin/users/{uid}/passkeys`, `POST /api/admin/passkeys` `{user, label}`, `POST /api/admin/passkeys/{kid}/revoke` (via `key_owner`).
  - All of these 404 "Passes are not enabled on this gateway." when `cfg.tickets.enabled` is false.
- [ ] **Step 1: Write failing tests:** create/list/revoke as the user; computer session → 403 on create and revoke; pass key on `/api/me` → 403 with "Pass app keys"; `source=cli`, `source=api`, `source=apps` lists each contain only their own rows (seed one row of each); admin create for another user returns the raw key once; tickets off → 404.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** Commit.

### Task 5: Dashboard UI and docs

**Files:**
- Modify: `src/claude_proxy/static/app.js` (`featureOn` :515, `renderApi`, `sourceSwitch` :553, `apiView`/`withSource` :132, `renderUser`), `src/claude_proxy/static/i18n.js` (also make the `key_scope` error tooltip key-neutral, i18n.js:162, :194 and their Persian lines: it says "An OpenCode key" today), `README.md`
- Test: `tests/test_i18n.py` (key parity already enforced); manual check per memory "Local dashboard check" (seeded dev gateway on :18081, Playwright).

**Interfaces:**
- Consumes: Task 4 endpoints.
- Produces:
  - `api` view visible when `S.api?.enabled || S.tickets?.enabled`; `renderApi` renders credit sections only when `S.api?.enabled`, and the "Pass app keys" card when `S.tickets?.enabled`.
  - Card: key list (label, prefix, created, last used, Revoke), "New key" form (label) showing the raw key once with Copy; usage table per key per day (requests, tokens); setup block with two parts: **Claude Desktop** (Help → Troubleshooting → Enable Developer Mode; Developer → Configure Third-Party Inference → Gateway; base URL = `base_url`; API key = the key; needs Desktop 1.2581.0+) and **Other apps** (base URL + key, Anthropic format). A note: works while you have a running pass; web search, fast mode and 1M context are off.
  - Source switch shows `apps` when tickets are on (labels `source.apps` "Apps" / «اپ‌ها», tip `source:apps`); `withSource`/`apiView` apply when either feature is on.
  - Admin user page: a "Pass app keys" fold with the user's keys, Revoke and New key.
  - README: a short "Using a pass in other apps (Claude Desktop)" section with the same steps.
- [ ] **Step 1:** Add i18n keys (English + Persian) for every new string; `uv run pytest tests/test_i18n.py -q` → PASS.
- [ ] **Step 2:** Implement the UI.
- [ ] **Step 3:** Run the local dashboard check: create a key, see it once, revoke it, switch the source toggle to Apps, open a user's page as admin; screenshot in English and Persian.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** Commit.
