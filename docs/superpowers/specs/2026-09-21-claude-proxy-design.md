# Claude Proxy: shared-subscription gateway with per-user metering, limits and dashboard

- **Date:** 2026-09-21
- **Status:** Draft, awaiting user review
- **Background research:** `reports/Shared Claude Code proxy solutions.md` and the notes under `research_notes/Shared Claude Code proxy solutions/`

## 1. Purpose

A small self-hosted gateway that lets 2-3 people use the ordinary Claude Code CLI on their own machines while sharing one Claude Pro/Max subscription. The gateway attributes every request to a user, lets an admin set per-user limits, and serves an interactive dashboard of account-wide and per-user usage.

### Goals

1. Users keep the unmodified Claude Code CLI, running locally on their own repos. Onboarding is two environment variables.
2. Every request is metered per user with input, output, cache-read and cache-write tokens, model, latency and status.
3. An admin can set, per user, absolute token limits per rolling window and share-of-account-quota limits.
4. An interactive dashboard shows account utilization, per-user usage, and statistics with plots.
5. Each user can see their own usage and remaining allowance, including from inside Claude Code via the statusline.

### Non-goals

- Pooling or rotating across multiple subscription accounts.
- Protocol translation, such as an OpenAI-compatible API or serving non-Claude-Code clients.
- Dollar-cost budgets. Costs may be displayed as API-equivalent estimates but are never enforced.
- Fingerprint spoofing, residential egress, TLS interception, or any mechanism to disguise traffic. The gateway forwards the real Claude Code client's requests and adds only what the credential requires.
- Wrapping the `claude` CLI in subprocesses.
- Cutting off a response stream mid-flight when a limit is crossed.

### Assumptions

- **Stack:** Python 3.12, FastAPI, httpx, SQLite, and a no-build-step frontend using ECharts. This is the dominant stack for this niche and the user did not choose a language.
- **Scale:** one host, one subscription, one admin, 2-10 users, low tens of requests per minute.
- **Windows are rolling**, matching Claude's own 5-hour and 7-day behaviour. Calendar-aligned windows are not supported in v1.
- **Deployment:** the gateway listens on localhost or a LAN interface. TLS is terminated by a reverse proxy such as Caddy when exposed beyond the LAN.

## 2. Policy note

Anthropic's Consumer Terms forbid making an account available to anyone else. The Claude Code legal page, updated 19 February 2026, forbids intermediating Claude.ai credentials and routing requests through Pro or Max credentials on behalf of other users, with enforcement "without prior notice". The user has chosen the subscription backend with that understanding. The design mitigates the operational side of that risk by keeping the credential backend pluggable (section 5), so the same ledger, limits and dashboard can front an Anthropic API key or per-user logins without code changes outside the backend module.

## 3. Architecture

```
Claude Code (user A laptop) ──┐
Claude Code (user B laptop) ──┼─► Gateway ──► api.anthropic.com
Claude Code (user C laptop) ──┘     │
                                    ├─ Auth: virtual key → user
                                    ├─ Limit engine (pre-request)
                                    ├─ Credential backend (OAuth grant | API key)
                                    ├─ Transparent forwarder + SSE meter
                                    ├─ Quota tracker (headers + idle polling)
                                    ├─ SQLite (raw events)
                                    └─ Dashboard + JSON API
```

One Python process runs everything. Components live in separate modules with narrow interfaces so each can be tested alone.

| Module | Responsibility | Depends on |
|---|---|---|
| `auth` | Map a virtual key to a user; admin and user sessions for the dashboard | `db` |
| `limits` | Decide allow or reject for a user before forwarding | `db`, `quota` |
| `credentials` | Provide upstream auth headers; own refresh | `db` |
| `forwarder` | Stream request and response between client and Anthropic unchanged, apart from auth headers | `credentials`, `meter` |
| `meter` | Parse usage from SSE or JSON responses with a bounded parser | none |
| `quota` | Record account utilization from response headers and idle polling; compute per-user attribution | `db`, `credentials` |
| `db` | Schema, migrations, queries, aggregates | SQLite |
| `web` | Dashboard pages, admin actions, JSON API, per-user status endpoint | all of the above |
| `cli` | Admin commands: init, login, add user, rotate key, set limit | `db`, `credentials` |

## 4. Request flow

1. Claude Code sends a request to the gateway with `Authorization: Bearer <virtual key>`, because the user set `ANTHROPIC_AUTH_TOKEN`.
2. `auth` hashes the key and looks up the user. Unknown, disabled or revoked keys get a 401 with an Anthropic-shaped `authentication_error` body. The gateway fails closed.
3. `limits` evaluates every configured limit for the user against recorded history. If any is exceeded, the gateway returns 429 with an Anthropic-shaped body: `{"type":"error","error":{"type":"rate_limit_error","message":"<which limit, current value, reset time>"}}` and a `retry-after` header set to the seconds until the earliest limit frees up. The rejection is recorded as an event.
4. `forwarder` builds the upstream request. It removes the client's `Authorization` and `x-api-key` headers, adds the credential backend's headers, and otherwise forwards method, path, query, headers and body unchanged. Hop-by-hop headers are dropped per RFC 9110.
5. The upstream response streams back to the client as bytes arrive. `meter` observes a copy of the stream in parallel and never delays it.
6. When the response ends, `meter` produces a usage record. The gateway writes one `requests` row with user, timings, status, model and token counts. It also captures any `anthropic-ratelimit-unified-*` response headers into a `quota_snapshots` row.

Only paths under `/v1/` are forwarded. Anything else on the proxy port returns 404 and is never sent upstream with a credential. This avoids a class of bug seen in teamclaude, where a mistyped path was forwarded with the account credential.

The HTTP client uses HTTP/1.1 with a connection pool. This avoids the documented stall when many concurrent large Claude Code uploads share one HTTP/2 connection.

### Accepted behaviour

- Limits are checked before a request, from history. The request that crosses a limit is served in full, and the next one is rejected. With 2-3 users this overshoot is at most one request per concurrent session.
- Concurrent requests from one user can each pass the check before either is recorded. This is accepted for v1 and noted on the dashboard's limits page.

## 5. Credential backend

A small interface lets the rest of the system ignore where upstream auth comes from.

```python
class CredentialBackend(Protocol):
    async def upstream_headers(self) -> dict[str, str]: ...
    async def on_unauthorized(self) -> bool: ...   # refresh; True if a retry is worthwhile
    async def poll_usage(self) -> dict | None: ...  # account utilization, or None if unsupported
    def describe(self) -> BackendStatus: ...        # for the dashboard health panel
```

### 5.1 OAuth subscription backend (v1 default)

- **Own grant.** The gateway completes its own browser OAuth login with PKCE, started by an admin CLI command. It prints the authorization URL, the admin completes login in a browser, and pastes the returned code back. The gateway never reads or reuses the Claude Code CLI's stored credentials. Refresh tokens are single-use and rotate, so two holders of one grant break each other.
- **Sole refresher.** Only this backend refreshes the grant. Refresh runs behind a single-flight async lock: concurrent callers wait for the one in-flight refresh and then use its result.
- **When to refresh.** Proactively when fewer than five minutes remain before `expires_at`, and reactively once on a 401 from upstream. Access-token lifetime is taken from each token response, never assumed.
- **Storage.** Tokens are stored in the SQLite `credentials` table, encrypted with a key read from an environment variable or a key file with mode 0600. The refresh token is written before the old one is discarded so a crash cannot lose the grant.
- **Headers.** Adds `Authorization: Bearer <access token>` and appends `oauth-2025-04-20` to the client's `anthropic-beta` header if absent. The client's own `User-Agent` and other beta flags are forwarded unchanged.
- **Configuration, not constants.** Client ID, authorize URL, token URL, scopes, required beta flag and usage-endpoint URL are all in the config file with the currently known values as defaults. They are reverse-engineered and have drifted before.
- **Failure.** If refresh fails with `invalid_grant`, the backend marks itself `needs_login`. The gateway returns 503 with an Anthropic-shaped `api_error` explaining that the admin must re-login, and the dashboard shows a red banner.

### 5.2 API key backend

Adds `x-api-key: <key>`. `poll_usage` returns None, so share-of-quota limits are unavailable and the dashboard hides the account gauges. Included in v1 because it is a few lines and it is the test double for the forwarder.

## 6. Metering

### 6.1 SSE rule

For streaming responses, the meter reads Anthropic's server-sent events as they pass through.

- From `message_start.message.usage`: `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, and `cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens` when present. Also `message.model` and `message.id`.
- From every `message_delta.usage`: overwrite `output_tokens` with the latest value. The counts are cumulative, so values are never summed. If a `message_delta` carries input or cache fields, they overwrite the `message_start` values.
- A stream that ends without `message_stop` is recorded with the counts seen so far and `complete = false`.

### 6.2 Non-streaming responses

The `usage` object of the JSON body is read directly. Responses from endpoints that carry no usage, such as `/v1/messages/count_tokens`, are recorded with zero tokens and their endpoint path.

### 6.3 Bounded parsing

The meter buffers at most one event at a time and caps its line buffer at 1 MiB. If the cap is hit, metering for that request stops, the request is flagged `meter_error`, and forwarding continues unaffected. This prevents the out-of-memory failure documented in teamclaude issue 341.

### 6.4 Weighted tokens

Share-of-quota attribution and some charts need a single number per request that approximates quota cost. The gateway computes `weighted_tokens` from the token counts using per-model and per-token-type weights held in config. Defaults follow Anthropic's public API price ratios: output 5 times input, cache write 1.25 times input for the 5-minute TTL and 2 times for the 1-hour TTL, cache read 0.1 times input, and model multipliers relative to Sonnet. Weights are applied when read, not stored, so changing them re-weights history.

## 7. Account quota tracking

Three quantities are kept strictly separate everywhere: in tables, in API fields and on the dashboard.

| Quantity | Source | Label on dashboard |
|---|---|---|
| **Account utilization** | Anthropic, via response headers or the usage endpoint | "Account (reported by Anthropic)" |
| **Observed usage** | The gateway's own meter | "Observed tokens" |
| **Allocation** | Admin-set limits | "Limit" |

### 7.1 Sources

- **Response headers, primary.** Every upstream response's `anthropic-ratelimit-unified-*` headers are captured into `quota_snapshots`. This costs no extra requests.
- **Usage endpoint, secondary.** `poll_usage` calls the account usage endpoint only when no snapshot has arrived for 10 minutes, and at most every 3 minutes. On 429 it backs off exponentially up to 30 minutes. This avoids competing with the per-token rate bucket Claude Code itself draws from.
- **Normalisation.** The parser accepts utilization as 0-100 or 0-1. It treats a value as a fraction only when the field is known to be fractional from its source, never by testing `> 1.0`, since that heuristic misreads a true 1% as 100%. It accepts `utilization` or `used_percentage`, and `resets_at`, `reset_at` or `reset`, as ISO-8601 or epoch seconds. Unknown fields are stored raw in a JSON column.
- Buckets recorded: 5-hour, 7-day, and per-model 7-day buckets when present.

### 7.2 Attribution

Each user's estimated contribution to an account bucket is computed, not measured.

1. Within a bucket's current window, take consecutive snapshot pairs. Each pair has a utilization delta and a time interval.
2. Sum each user's `weighted_tokens` for requests that completed in that interval.
3. Split the delta across users in proportion to those sums. If only one user was active, they get the whole delta. If no requests completed, the delta is left unattributed.
4. A user's estimated share is the sum of their attributed deltas since the bucket's last reset.

Reset detection: when a snapshot's `resets_at` moves forward, or utilization drops, a new window starts and shares restart at zero.

The dashboard labels these numbers "estimated share" everywhere, shows the unattributed remainder, and notes that utilization arrives in whole percents.

## 8. Limits

Each user may have any subset of these limits. All are optional.

| Limit | Unit | Window | Evaluated against |
|---|---|---|---|
| `tokens_5h` | weighted or raw tokens, admin's choice | rolling 5 hours | Observed usage |
| `tokens_daily` | same | rolling 24 hours | Observed usage |
| `tokens_weekly` | same | rolling 7 days | Observed usage |
| `requests_daily` | request count | rolling 24 hours | Observed usage |
| `share_5h` | percentage points of the account 5-hour bucket | Anthropic's current 5-hour window | Estimated share |
| `share_7d` | percentage points of the account 7-day bucket | Anthropic's current 7-day window | Estimated share |
| `allowed_models` | list of model name patterns | none | The request's `model` field |
| `enabled` | boolean | none | none |

- Token limits have a per-user setting choosing raw total tokens or weighted tokens. The default is weighted, since raw totals are dominated by cache reads.
- If share limits are set but the backend cannot supply utilization, or no snapshot is newer than 30 minutes, share limits are skipped, token limits still apply, and the dashboard shows a warning.
- Limit changes take effect on the next request.
- `/v1/messages/count_tokens` requests are never rejected by token or share limits, since they consume no quota.

## 9. Data model

SQLite in WAL mode. Raw events are the source of truth. Aggregates are computed by queries, optionally cached in memory for the dashboard, and never stored as counters.

```
users(id, name, role['admin'|'user'], key_hash, key_prefix, enabled,
      created_at, revoked_at, password_hash NULL)
limits(user_id, kind, value, unit['raw'|'weighted'|'pct'|'count'|'list'],
       updated_at, updated_by)
requests(id, user_id, started_at, ended_at, method, path, model, status,
         stream, complete, input_tokens, output_tokens,
         cache_creation_tokens, cache_creation_5m, cache_creation_1h,
         cache_read_tokens, upstream_request_id, session_id,
         client_version, error_type, meter_error, rejected_by NULL)
quota_snapshots(id, observed_at, source['header'|'poll'], bucket,
                utilization_pct, resets_at, status, raw_json)
credentials(backend, encrypted_blob, expires_at, updated_at, state)
audit_log(id, at, actor_user_id, action, target, detail_json)
settings(key, value)
```

- `key_hash` uses SHA-256 of a 32-byte random key. Keys are shown once at creation. `key_prefix` holds the first 8 characters for identification in the UI.
- `session_id` is taken from Claude Code's session header when present, for per-session statistics.
- Prompts and responses are never stored. Only metadata and counts.
- Retention: `requests` and `quota_snapshots` are kept for 180 days by default, configurable.

## 10. Dashboard

Served by the same process at `/dashboard`, on a separate listener port from the proxy by default. Vanilla JavaScript with ECharts, no build step. Charts support hover values, zoom, range selection, and toggling series.

### 10.1 Access

- **Admin:** username and password, hashed with argon2id. Session cookie that is HttpOnly, SameSite=Strict, and Secure when served over TLS. CSRF token on every state-changing form.
- **User:** logs in with their virtual key and sees only their own data.
- No loopback or IP-based auth exemptions of any kind, since they fail open behind a reverse proxy.
- Login attempts are rate-limited per IP.

### 10.2 Admin views

1. **Overview.** KPI tiles for requests, raw tokens, weighted tokens and active users for today, 7 days and 30 days. Account gauges for 5-hour and 7-day utilization with reset countdowns. Credential health. Current burn rate in weighted tokens per minute and a projected time to exhaustion of the account's 5-hour bucket.
2. **Usage over time.** Stacked area chart of tokens by user or by model, switchable, with hour, day or week granularity and token-type toggles.
3. **Users.** Leaderboard table with today, 7-day and 30-day tokens, estimated share of each account bucket, each limit with percent consumed, and last-seen time. Row actions: edit limits, enable or disable, rotate key, revoke.
4. **Account quota.** Timeline of reported utilization per bucket with resets marked, overlaid with each user's estimated share and the unattributed remainder.
5. **Models and cache.** Tokens by model, cache hit ratio over time, with the formula shown on the panel: `cache_read / (input + cache_creation + cache_read)`.
6. **Activity.** Day-of-week by hour heatmap, per user or combined.
7. **Sessions.** Table of sessions with user, duration, request count and tokens.
8. **Errors.** Timeline of upstream errors and gateway rejections, split by type: gateway limit, upstream quota 429, upstream per-minute 429, other 4xx and 5xx.
9. **Audit log.** Admin actions with actor and timestamp.

### 10.3 User view

Their own KPI tiles, usage over time, each limit with remaining allowance and reset time, and their estimated account share.

### 10.4 JSON API

Every chart reads from a JSON endpoint under `/api/`, so the dashboard is a thin client and the data can be scripted. `GET /api/me/status` authenticates with the virtual key as a bearer token and returns the user's limits, current values, remaining allowance and reset times, plus the account's reported utilization.

## 11. What each user sees inside Claude Code

In bearer-token mode Claude Code does not show the subscription's plan bars, and `/usage-credits` is unavailable. The statusline is the supported channel for per-user information.

- The repo ships a statusline script. It calls `/api/me/status`, caches the result in a temp file for 30 seconds, and prints a line such as `you 42% of daily · acct 5h 61% · 7d 38%`.
- Setup is one entry in the user's Claude Code settings with a `refreshInterval` of 30 seconds.
- **Experimental, off by default:** the gateway could rewrite the `anthropic-ratelimit-unified-*` headers on responses to reflect the user's own allowance. Whether Claude Code renders these in bearer mode is undocumented. This is left for a later spike and is not part of v1.

## 12. Configuration and operations

- **Config:** a TOML file for listeners, credential backend, OAuth constants, polling intervals, token weights and retention, with environment-variable overrides.
- **Admin CLI:** `init` creates the database and admin account. `login` runs the OAuth flow. `user add|list|rotate|revoke|enable|disable`. `limit set|clear`. `status` prints credential and quota health.
- **Client onboarding:** each user sets `ANTHROPIC_BASE_URL` to the gateway URL and `ANTHROPIC_AUTH_TOKEN` to their virtual key, and optionally installs the statusline script.
- **Packaging:** a Python package run with `uvicorn`, plus a Dockerfile and an example Caddyfile for TLS.
- **Logging:** structured JSON logs of request metadata. Never prompts, responses, or credential values. Virtual keys appear only as their prefix.

## 13. Error handling summary

| Situation | Gateway response to client | Recorded as |
|---|---|---|
| Unknown or revoked virtual key | 401 `authentication_error` | rejection |
| User over a limit | 429 `rate_limit_error` with `retry-after` | rejection with `rejected_by` |
| Model not allowed | 403 `permission_error` | rejection |
| Credential needs re-login | 503 `api_error` | gateway error |
| Upstream 401 | one refresh and one retry, then pass through | upstream error |
| Upstream 429 or 5xx | passed through unchanged, headers included | upstream error, classified quota, per-minute or request-scoped |
| Upstream unreachable | 502 `api_error` | gateway error |
| Client disconnects mid-stream | upstream request cancelled | request with `complete = false` |
| Meter parse failure | stream unaffected | `meter_error = true` |

Upstream 429s are classified for the dashboard: quota exhaustion when a unified status header says rejected, per-minute throttle when rate-limit headers are present without that, and request-scoped when no rate-limit headers are present at all.

## 14. Testing

- **Unit tests** for the SSE meter using recorded Anthropic streams: normal, cached, with thinking, tool use, truncated without `message_stop`, oversized line, and a `message_delta` that carries input fields.
- **Unit tests** for utilization normalisation covering 0-1 and 0-100 inputs, a true 1%, field-name variants, and ISO versus epoch reset times.
- **Unit tests** for attribution: single active user, two users with unequal weights, no activity, window reset, whole-percent steps.
- **Unit tests** for the limit engine at every limit kind, boundary values, and share limits with stale snapshots.
- **Integration tests** run the gateway against a fake upstream built with FastAPI that replays recorded streams, returns 401 then succeeds after refresh, returns each kind of 429, and drops connections mid-stream. They assert byte-identical forwarding of bodies and correct ledger rows.
- **Concurrency test:** many simultaneous 401s trigger exactly one refresh.
- **Security tests:** unknown paths are not forwarded, credential headers never appear downstream or in logs, dashboard endpoints reject missing or wrong sessions and CSRF tokens.
- **Manual smoke test** with two real Claude Code clients on separate machines: interactive session, subagents, streaming, `count_tokens`, a limit rejection as rendered in the CLI, and the statusline line.

## 15. Open items to verify during implementation

These are unknowns the research could not settle. None changes the design, but each needs a check in the smoke test.

1. How Claude Code renders a proxy-originated 429 with an Anthropic-shaped body, and whether it retries automatically.
2. Whether any request body field, such as the account identifier inside `metadata.user_id`, must match the injected credential for upstream to accept it.
3. Whether a Claude Code client with `ANTHROPIC_AUTH_TOKEN` set still depends on a valid local login for any feature, per teamclaude issue 395.
4. The current shape of the usage endpoint's response, which has drifted before.
5. Which session identifier header Claude Code currently sends.

## 16. Delivery order

1. Transparent forwarder with the API key backend and virtual-key auth, proven against the fake upstream.
2. SSE and JSON metering with the `requests` ledger.
3. OAuth backend with its own grant, single-flight refresh, and encrypted storage.
4. Quota tracking from headers and idle polling, with normalisation.
5. Limit engine for token and request limits, then attribution and share limits.
6. JSON API and admin CLI.
7. Dashboard views, admin first, then the user view.
8. Statusline script, Dockerfile, Caddy example, README.
