# Implementation Plan — Claude Proxy Gateway

- **Spec:** `docs/superpowers/specs/2026-09-21-claude-proxy-design.md:1`
- **Date:** 2026-09-21
- **Status:** Draft plan for review (spec is `Draft, awaiting user review` at `spec:4`)

## 0. Ground rules (from spec §1-2)

- Stack: `Python 3.12, FastAPI, httpx, SQLite, ECharts` (`spec:30`), single Python process (`spec:54`), WAL SQLite raw events (`spec:187`).
- Scale: 1 host / 1 sub / 1 admin / 2-10 users, low tens req/min (`spec:31`).
- Windows open with the first request and reset all at once (`spec:32`). No calendar windows in v1.
- No multi-account pooling/rotation, no OpenAI translation, no cost budgets, no fingerprint spoofing, no CLI wrapping, no mid-stream cutoff (`spec:19-26`).
- Policy-aware: Consumer Terms forbids sharing one account (`spec:35`); mitigate with pluggable `CredentialBackend` (`spec:88-111`) so same ledger fronts OAuth today, API key tomorrow.

---

## Phase 0 — Project scaffolding (prereq for all)

**Modules:** `db`, `config`, `cli` skeleton, `web` skeleton
- [ ] Repo layout: `src/claude_proxy/{auth,limits,credentials,forwarder,meter,quota,db,web,cli,config}.py`, `tests/`, `static/dashboard/`, `Dockerfile`, `Caddyfile.example`, `config.example.toml`
- [ ] `pyproject.toml` / `requirements`: `fastapi`, `uvicorn[standard]`, `httpx`, `argon2-cffi`, `cryptography` (Fernet), `tomli`/`tomli-w`
- [ ] `config` (`spec:252`): TOML + env overrides for listeners (proxy port, dashboard port), credential backend, OAuth constants (client_id `9d1c250a-e61b-44d9-88ed-5944d1962f5e`, authorize/token URLs, scopes, beta `oauth-2025-04-20`, usage URL), polling intervals (idle 10m, max every 3m, backoff to 30m), token weights, retention 180d. No hard-coded constants (`spec:106`).
- [ ] `db` schema (`spec:190-205`): `users`, `limits`, `requests`, `quota_snapshots`, `credentials`, `audit_log`, `settings` in WAL mode. Migrations, `key_hash=SHA256(32B random)`, `key_prefix` 8 chars, `argon2id` for admin password (`spec:218`), encrypted `credentials.encrypted_blob` (env/keyfile 0600, write new refresh before discarding old at `spec:104`).
- [ ] Logging: structured JSON, never prompts/responses/keys, only `key_prefix` (`spec:257`).
- [ ] Fake upstream (`spec:281`): FastAPI harness that replays recorded SSE streams, returns 401→200 after refresh, each 429 kind, drops mid-stream. Used by all integration tests.

**Accept:** `pytest` runs fake upstream, DB creates, `config` loads TOML+env.

## Phase 1 — Transparent forwarder + API-key backend + virtual-key auth

**Spec:** `§3 Architecture` (`spec:39`), `§4 Request flow` (`spec:68`), `§5.2 API key` (`spec:109`)

- [ ] `auth`: hash lookup, 401 `authentication_error` Anthropic-shaped body on unknown/disabled/revoked, fail closed (`spec:71`). Virtual key via `Authorization: Bearer` from `ANTHROPIC_AUTH_TOKEN` (`spec:70`).
- [ ] `forwarder`: remove client `Authorization`/`x-api-key`, inject backend `upstream_headers()`, forward method/path/query/body unchanged, drop hop-by-hop per RFC 9110 (`spec:73`). Only `/v1/` forwarded; else 404 never upstream (`spec:77`) — guards teamclaude #420. `httpx` with HTTP/1.1 pool (`spec:79`) to avoid H2 stall.
- [ ] `credentials.ApiKeyBackend`: `upstream_headers -> {x-api-key}`; `poll_usage -> None`; `describe()` (`spec:95-111`). Hides account gauges when `None` (`spec:111`).
- [ ] `CredentialBackend` protocol (`spec:90`): `upstream_headers`, `on_unauthorized -> bool`, `poll_usage`, `describe`
- [ ] Listeners: proxy port + separate dashboard port (`spec:214`), no build step.

**Tests (spec §14):** Integration byte-identical body forward, 404 not forwarded, hop-by-hop stripped, API-key injected, 401 on bad key, security: creds never in logs/response (`spec:283`).

**Done when:** `ANTHROPIC_BASE_URL=http://gateway:8000 ANTHROPIC_AUTH_TOKEN=<virtual>` routes through gateway to real Anthropic with API key backend.

## Phase 2 — Metering (SSE + JSON) → `requests` ledger

**Spec:** `§6 Metering` (`spec:113`), `§9 requests table` (`spec:195`)

- [ ] `meter`: bounded parser, ≤1 event buffered, 1 MiB line cap, flag `meter_error` and continue forwarding on cap (`spec:129` — teamclaude #341).
- [ ] SSE rule (`spec:115`): `message_start.message.usage` → `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `cache_creation.ephemeral_5m/1h`; `model`, `id`; every `message_delta.usage` overwrites `output_tokens` (cumulative, never sum), overwrites input/cache if present (`spec:120`); missing `message_stop` → `complete=false` (`spec:121`).
- [ ] Non-streaming (`spec:123`): `usage` object direct; `/v1/messages/count_tokens` → zero tokens + path recorded.
- [ ] Write `requests` row on completion: timings, status, model, token columns (`spec:195`), `stream`, `complete`, `upstream_request_id`, `session_id` from session header (`spec:192`), `client_version`, `error_type`, `meter_error`, `rejected_by NULL` for now.
- [ ] Weighted tokens (`spec:131`): config weights (output 5× input, cache_write 1.25× 5m / 2× 1h, cache_read 0.1×, model mults relative Sonnet); computed on read not stored (`spec:133`).

**Tests:** Recorded streams — normal, cached, thinking, tool_use, truncated, oversized line, delta with input fields (`spec:277`). Assert weighted recomputes after weight change.

## Phase 3 — OAuth backend (own grant, single-flight)

**Spec:** `§5.1` (`spec:98`)

- [ ] PKCE flow `cli login`: print authorize URL, admin pastes code, exchange `grant_type=authorization_code` with `code_verifier`/`state`, never reuse CLI credentials (`spec:100`).
- [ ] Storage: encrypted `credentials` row, refresh rotation: write new before discarding old (`spec:104`), `0600` keyfile.
- [ ] Single-flight lock: `asyncio.Lock` or `singleflight` — concurrent `on_unauthorized` wait on one refresh (`spec:101`, test `spec:282`).
- [ ] Proactive refresh `<5m to expires_at`, reactive once on upstream 401 (`spec:102`), `expires_in` from token response not assumed.
- [ ] Retry only before first byte: gate `on_unauthorized` retry on `bytes_forwarded==0` (`spec:103`). 401 arrives in status line so safe.
- [ ] Headers: `Authorization: Bearer` + append `oauth-2025-04-20` to `anthropic-beta` if absent; forward client `User-Agent`/betas unchanged (`spec:105`).
- [ ] Config-driven OAuth constants (`spec:106`), `invalid_grant` → `state=needs_login`, 503 `api_error` + dashboard red banner (`spec:107`).

**Tests:** Fake upstream 401→refresh→200, concurrent 401s → 1 refresh, `invalid_grant` → 503, encrypted blob round-trip, crash-safety (new refresh persisted before old discarded).

## Phase 4 — Quota tracking (headers + idle poll)

**Spec:** `§7` (`spec:135`)

- [ ] On every upstream response, capture `anthropic-ratelimit-unified-*` into `quota_snapshots` (`spec:146`): `bucket` (5h/7d/per-model 7d), `utilization_pct`, `resets_at`, `status`, `raw_json` (`spec:200`), `source='header'|'poll'`, `observed_at`.
- [ ] Normalisation (`spec:149`): accept `utilization`/`used_percentage`, `resets_at`/`reset_at`/`reset` (ISO8601 or epoch sec), `raw_json` for unknowns. Fraction-vs-percent by source, not `>1.0` heuristic (prior bug `1%→100%`).
- [ ] Poll `poll_usage` only if no snapshot for 10m, at most every 3m, exponential backoff to 30m on 429 (`spec:148`). Never compete with per-token bucket that Claude Code uses.
- [ ] Labels strict: account utilization "Account (reported by Anthropic)" vs observed vs allocation (`spec:138`).
- [ ] Buckets: 5h, 7d, per-model 7d when present (`spec:150`).

**Tests:** Normalisation matrix (0-1 vs 0-100, `1%`, field variants, ISO vs epoch) (`spec:278`).

## Phase 5 — Limits engine + attribution

**Spec:** `§8` (`spec:165`), `§7.2` (`spec:152`)

- [ ] Attribution (`spec:152`): per bucket window, snapshot-pair deltas split proportional to `weighted_tokens` in interval; single active user gets whole delta; no activity → unattributed; sum since last reset; reset when `resets_at` moves forward or utilization drops (`spec:161`). Dashboard labels "estimated share" + remainder (`spec:163`).
- [ ] Limits (`spec:165` table): `tokens_5h` / `tokens_daily` / `tokens_weekly` (raw vs weighted per-user, default weighted `spec:180`), `requests_daily`, `share_5h`/`share_7d` (pct points of account bucket), `allowed_models` (glob patterns), `enabled`.
- [ ] Eval pre-request vs history (`spec:72`): 429 `rate_limit_error` + `retry-after` seconds to earliest reset (`spec:72`); record rejection with `rejected_by` (`spec:73`); rejected never counts (`spec:182`); `count_tokens` never rejected by token/share limits (`spec:184`); share limits skipped if no snapshot <30m or backend `poll_usage is None`, token limits still enforce + warning (`spec:181`); `enabled=false` blocks; model check → 403 `permission_error` (`spec:265`).
- [ ] Accepted concurrency overshoot: checked from history, crossing request served, next rejected (`spec:83`); concurrent same-user races accepted v1, noted on dashboard (`spec:84`).

**Tests:** Each limit kind, boundaries, raw vs weighted, share with stale snapshot skipped, `count_tokens` bypass, model glob, `rejected_by` not counted (`spec:280`). Attribution: single user, two unequal weights, no activity, reset, whole-percent steps (`spec:279`).

## Phase 6 — JSON API + Admin CLI

**Spec:** `§10.4` (`spec:239`), `§12` (`spec:251`)

- [ ] JSON API under `/api/` (thin client): KPI aggregates, usage series, leaderboards, quota timeline. `GET /api/me/status` bearer virtual-key → limits, current, remaining, reset times + account utilization (`spec:241`).
- [ ] `cli` (`spec:254`): `init` (DB+admin argon2id), `login` (OAuth PKCE), `user add|list|rotate|revoke|enable|disable`, `limit set|clear`, `status` (credential + quota health). `audit_log` rows for admin actions (`spec:203`).
- [ ] Auth: admin session cookie `HttpOnly, SameSite=Strict, Secure if TLS` + CSRF on state changes (`spec:218`), user login via virtual key sees own data only (`spec:219`), no loopback/IP exemption (`spec:220`), per-IP login rate limit (`spec:221`).

**Tests:** API auth matrix, CSRF reject, key auth `me/status` returns correct remaining, audit trail.

## Phase 7 — Dashboard

**Spec:** `§10` (`spec:212`)

- [ ] One process serves `/dashboard` (separate port), vanilla JS + ECharts, hover/zoom/range/toggle (`spec:214`).
- [ ] Admin views (`spec:222-232`): (1) Overview KPIs today/7/30d, 5h/7d gauges + countdowns, credential health, burn rate (weighted tok/min) + exhaustion projection; (2) Usage over time stacked area by user/model, granularity h/d/w, token-type toggles; (3) Users leaderboard + limits % + last-seen + actions; (4) Account quota timeline + estimated share + unattributed; (5) Models & cache (hit ratio formula `cache_read/(input+cache_creation+cache_read)` shown); (6) Activity heatmap DOW×hour; (7) Sessions table; (8) Errors timeline by type (gateway limit, upstream quota/per-minute/request-scoped, 4xx/5xx); (9) Audit log.
- [ ] User view (`spec:235`): own KPIs, usage over time, limits remaining + reset, estimated share.
- [ ] Share staleness warning when >30m (`spec:181`).

## Phase 8 — Statusline, packaging, docs

**Spec:** `§11` (`spec:243`), `§12-13`

- [ ] `statusline.sh`: `GET /api/me/status`, tmpfile cache 30s, `refreshInterval 30s`, prints `you 42% of daily · acct 5h 61% · 7d 38%` (`spec:247`), install via Claude Code settings `refreshInterval` (`spec:248`). Experimental header rewrite off by default (`spec:249`).
- [ ] Packaging: `uvicorn` package, `Dockerfile`, `Caddyfile.example` for TLS termination (`spec:256`), TOML example.
- [ ] Error table (`spec:259`): 401 auth, 429 limit, 403 model, 503 needs_login, 401→refresh once pre-byte, pass-through 429/5xx with headers, 502 upstream unreachable, `complete=false` on client disconnect, `meter_error` unaffected.
- [ ] Retention job: `requests`/`quota_snapshots` 180d default configurable (`spec:210`).

## Testing summary (spec §14) — wiring

- Unit: SSE, normalisation, attribution, limits — run without network.
- Integration vs fake upstream: byte-identical forward, ledger rows, refresh, 429 classes, drop mid-stream (`spec:281`).
- Concurrency: many 401s → 1 refresh (`spec:282`).
- Security: unknown path not forwarded, creds not in logs/downstream, dashboard CSRF/auth (`spec:283`).
- Manual smoke: 2 real Claude Code clients, interactive, subagents, streaming, `count_tokens`, limit 429 rendering, statusline (`spec:284`). Covers open items §15 (`spec:286`): 429 rendering, body `metadata.user_id`/`account_uuid` rewrite need (`spec:291`), base-URL mode still needs local login? (`spec:292` + teamclaude #395), usage endpoint shape (`spec:293`), session header name (`spec:294`).

## Dependencies / order

```
0 scaffolding → 1 forwarder/API-key → 2 metering → 3 OAuth → 4 quota → 5 limits/attribution → 6 API/CLI → 7 dashboard → 8 statusline/packaging
```
Each phase shippable; 1 alone proves `ANTHROPIC_BASE_URL` proxying. 2 needed before 5. 3 needed before 4 poll. 4 needed before share limits in 5.

## Open decisions to lock before Phase 3

- Confirm weighted defaults (5× output etc `spec:133`) vs API pricing actually used.
- Confirm `account_uuid`/`metadata.user_id` handling after smoke capture — keep forwarder "unchanged" (`spec:73`) or add rewrite.
- Decide if `poll_usage` URL/fields should be live-captured (dario wire-shape) vs static config.

---

**Next action:** Approve plan → branch `feat/slice-1-forwarder` and implement Phase 0+1 with fake upstream + pytest. Mark spec `Status: Draft` → `Approved` when you’re ready.
