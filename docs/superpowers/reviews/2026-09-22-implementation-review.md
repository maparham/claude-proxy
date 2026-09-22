# Implementation review — claude-proxy gateway

- **Reviewed:** working tree (all untracked), 2026-09-22
- **Against:** `docs/superpowers/specs/2026-09-21-claude-proxy-design.md`, `docs/superpowers/plans/2026-09-21-implementation-plan.md`
- **Reviewer:** Claude Opus 5.5

## Verdict

**Not deployable.** The proxy path is real and works — a Claude Code client pointed at it with a virtual key does reach Anthropic with the OAuth bearer swapped in, and per-request tokens land in the ledger. Everything around that path is either unauthenticated, unenforced, or absent.

Against the three things that were actually asked for:

| Asked for | State |
|---|---|
| Interactive dashboard with plots and statistics | **Mostly unmet.** One ECharts bar chart of three aggregate totals (today / 7d / 30d). No time series, no per-user plot, no model breakdown, no heatmap, no quota gauges, no error timeline, no audit view, no per-user view. Spec §10 lists nine admin panels; roughly one exists. |
| Admin sets per-user limits | **Met, with a counting bug.** `tokens_*`, `requests_daily`, `allowed_models`, `enabled` all enforce pre-request and reject with a correct Anthropic-shaped 429/403. Raw-unit totals are inflated (see P1-2). |
| Share-of-account-quota limits | **Silently inert.** `limits.py:243` hardcodes `estimated_share = 0.0`, so `share_5h` / `share_7d` can never reject. Nothing ever writes `quota_snapshots`; there is no `quota.py`; `anthropic-ratelimit-unified-*` response headers are read by no one. |

The share-limit gap is worse than a missing feature. The admin UI offers "Share 5h %", accepts the value, stores it, shows it in the leaderboard as an active limit, and it does nothing. `/api/me/status` reports `current: 0.0` for it, i.e. the full allowance remaining. Either wire it up or remove it from the UI and the CLI `--kind` choices.

Phases 0–3 of the plan are substantially done; phases 4, 7 and 8 are largely not, and phase 5 is done except attribution.

---

## P0 — fix before this is reachable by anything

**P0-1. The dashboard API is unauthenticated, on the proxy port.**
`web.py:90-111`: `/api/overview`, `/api/users` and `/api/limits` call no auth function at all. Verified live:

```
$ curl -s http://127.0.0.1:8099/api/users     # no credentials
{"users":[{"id":1,"name":"admin","role":"admin","prefix":"sk-proxy-wp5",...
```

That returns every user, their role, their key prefix, their usage and their limits. `/api/limits` and `/api/overview` are the same. `_auth_user` exists and is used on the admin POST routes and `/api/me/status`, but was never applied to the read routes.

Compounding it: `cli.py:199-206` starts **one** uvicorn on `listener.port`. `listener.dashboard_port` is parsed and never used, and `create_app` registers the web routes into the same app as the proxy. So `/dashboard` and every `/api/*` route answer on the proxy port — the port that by definition has to be reachable by every user. The moment the host binds anything other than `127.0.0.1`, every user reads every other user's data.

Fix: require `_auth_user` on all read routes, scope non-admins to their own rows, and either bind the web app to `dashboard_port` on a second uvicorn or drop `dashboard_port` from the config and document the single-port model.

**P0-2. Without an env key, the refresh token is encrypted with a public constant.**
`credentials.py:44-47`: when neither `CLAUDE_PROXY_CREDENTIAL_KEY` nor `..._KEY_FILE` is set, the Fernet key is `sha256(b"claude-proxy-dev-key")` — a literal in this repo. Anyone with the `.db` file recovers the Pro/Max refresh token. There is no warning at startup, at `login`, or in `status`; the comment "For dev/test" is the only signal, and `config.example.toml` mentions the env vars but nothing enforces them.

Fix: fail closed. `claude-proxy login` and `serve` should refuse to start without a key and print the command to generate one. Keep the dev fallback only behind an explicit `CLAUDE_PROXY_DEV_INSECURE_KEY=1`.

---

## P1 — correctness

**P1-1. A 401 never actually refreshes the token.**
`credentials.py:139-147`. `on_unauthorized()` → `_refresh()`, which takes the lock and then returns `True` early if the stored credential is `active` and more than 5 minutes from expiry. On a real 401 the token is almost never near expiry, so the token endpoint is never called; `app.py:264-285` then retries with the same dead token and the client gets the second 401. Reproduced with an unreachable token URL — `on_unauthorized()` returned `True` without a network call and `upstream_headers()` still handed back `Bearer dead-token`.

The double-check is meant to catch "another coroutine refreshed while I waited on the lock". To do that it must compare the access token that failed against the one now stored, not look at the expiry. Pass the failing token into `on_unauthorized(token)` and skip only if `stored != failed`.

This is also the single highest-value missing test: the plan's phase-3 "fake upstream 401 → refresh → 200" was never written, which is why six green tests hide it.

**P1-2. Cache-creation tokens are counted twice in every raw total.**
`meter.py:269-275` treats `cache_creation_5m` / `_1h` as a *breakdown* of `cache_creation_tokens` and deliberately ignores the generic field when the granular ones are present — correct. But `limits.py:60`, `web.py:19`, `web.py:45-46` and `web.py:292` all compute `... + cache_creation_tokens + cache_creation_5m + cache_creation_1h`. For a response reporting 1000 cache-creation tokens the weighted path yields 1250 and the raw path yields 2000.

Effect: `tokens_*` limits with `unit='raw'` reject early, and every dashboard token figure is inflated. Fix in one place — a single SQL expression or helper used by all four sites — and add the 5-line unit test.

**P1-3. The 401 retry can 500.**
`app.py:284-285`: the retry `client.build_request` / `client.send` is outside the try that guards the first send. A connection error on the retry propagates as an unhandled exception, and the client sees a bare 500 with no Anthropic-shaped body.

**P1-4. No rate limit on admin login.**
`web.py:113-140` verifies an argon2 password with no per-IP throttle and no lockout. Spec §12 asked for a per-IP login rate limit. With the dashboard on the proxy port (P0-1), this is a network-reachable password oracle.

**P1-5. Phase 8 is missing entirely.** No `statusline.sh` — so the one channel the spec identified for showing users their remaining allowance inside Claude Code does not exist. No retention job (`retention_days = 180` is parsed and never read). `static/` is not copied into the Docker image, so the container serves the "dashboard placeholder" string; the DB path is relative with no volume, so data dies with the container; and no credential key is set there, so it silently lands on P0-2.

**P1-6. CLI is incomplete.** `user rotate`, `revoke`, `enable`, `disable` are all in the plan and absent; `db.rotate_key` is written but unreachable. Revocation today means editing SQLite by hand. Also `init --admin-password` is a required argument, so the admin password goes into shell history and `ps`; prompt for it instead.

**P1-7. Test coverage stops at phase 1.** Six tests: auth-missing, unknown-path, bearer/beta injection, no-credential 503, hop-by-hop, PKCE round-trip. Nothing for the SSE meter (the component most likely to silently miscount), nothing for any limit kind, nothing for the API auth matrix, nothing for concurrent refresh. The plan named all of these.

Also: `pytest` fails on a clean checkout with `ModuleNotFoundError: claude_proxy` — `pyproject.toml` has no `[tool.pytest.ini_options] pythonpath = ["src"]` and there is no `conftest.py`. It passes with `PYTHONPATH=src` or after `pip install -e .`, so this is a one-line fix, but as shipped `pytest` does not run.

**P1-8. The usage-endpoint User-Agent is probably wrong.**
`credentials.py:234` sends `claude-cli/1.0.60 (external, cli)`. The research notes record the required form as `claude-code/<version>` and state that without the Claude Code User-Agent the usage endpoint falls into "an aggressively rate-limited bucket" (persistent 429). Since `poll_usage` swallows every non-200 into `None`, a wrong UA here fails completely silently. Worth checking against a real call before phase 4 is built on it.

---

## Unverified, and the highest-risk path

`cmd_login` (`cli.py:60-117`) has never touched the real endpoint. If it does not work, nothing else matters. What the research notes do confirm matches the code: form-encoded body, `grant_type=authorization_code` with `code, redirect_uri, client_id, code_verifier, state`, the client ID, both URLs, the redirect URI, PKCE S256, `expires_in` 28800.

Two divergences and one unknown:

- The notes list the scopes as `user:inference user:profile user:sessions:claude_code user:mcp_servers`. The code adds `user:file_upload` (`config.py:30`). Unexplained; an unrecognised scope may be rejected outright.
- The notes don't record what the callback page actually displays. If it shows the code with a `#state` suffix, `cli.py:76-81` will send that whole string as the code and the exchange will fail with an opaque 400. Check this on the first real login.
- State is sent to the token endpoint but never compared against the generated one.

Smoke test to run before anything else is touched: `claude-proxy init`, `claude-proxy login`, `claude-proxy status`, then one real `curl` through the proxy, then one interactive Claude Code session with `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` set. That also answers spec §15's open items (429 rendering in the CLI, whether the body carries an account identifier, the session header name).

---

## P2 — one line each

- `app.py:174-176` sets both `retry-after` and `Retry-After`, so rejections carry a duplicate header.
- `app.py:35-38`: after the known session-header names miss, it returns the value of *any* header whose name contains "session". Drop the fallback; record `NULL`.
- `app.py:43`: `anthropic-version` is tried before the user agent, so `client_version` usually records the API version, not the client.
- `requests` has no index; every limit check full-scans it. Add `(user_id, started_at)`.
- Session cookie is `SameSite=Lax`, never `Secure`, and there is no CSRF token. Spec §12 asked for Strict + CSRF.
- `OAuthBackend._load_row` opens a fresh SQLite connection on every proxied request (and `describe()` opens two). Cache the decrypted token in memory.
- `app.py:26` hardcodes `timeout=300.0` and ignores `cfg.upstream.timeout_s`; the client is never closed on shutdown.
- `config.py:87` swallows every TOML error with a bare `except: pass` — a typo in the config file is invisible and silently yields defaults.
- `limits.py:107-112` computes `user_id` three times in a row.
- Uvicorn's factory detection does handle `app:create_app` without `--factory`, so the Dockerfile boots; it only warns. Worth adding the flag anyway.

---

## Phase coverage

| Phase | State |
|---|---|
| 0 — scaffolding | Done, minus the fake-upstream harness (a partial one lives in the test file) |
| 1 — forwarder + virtual keys | Done. API-key backend deliberately dropped per your constraint |
| 2 — metering | Done, minus the double-count and any tests |
| 3 — OAuth backend | Partial — flow written but unverified; refresh is a no-op on 401; encryption fails open |
| 4 — quota tracking | **Missing.** No `quota.py`, no header capture, no poller, `quota_snapshots` never written |
| 5 — limits + attribution | Limits done; attribution missing, so share limits are inert |
| 6 — JSON API + CLI | Partial — read routes unauthenticated, CLI missing four commands |
| 7 — dashboard | ~15%. One bar chart of aggregates; no user view |
| 8 — statusline / packaging | Statusline and retention missing; Docker incomplete |

## Suggested order to resume

1. P0-1 and P0-2 (auth on read routes, fail closed on the credential key).
2. The login smoke test — it gates everything downstream.
3. P1-1 with its test, then P1-2 with its test.
4. Phase 4 (`quota.py`: header capture + idle poll), then attribution, so share limits stop lying.
5. Phase 7 — the dashboard needs a time-series endpoint before it needs more charts.
6. Phase 8.
