# Serving rahkar.pro and the dashboard from the Iranian VPS — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** People in Iran can load rahkar.pro, sign in and use the dashboard: both names point at the Iranian VPS, which
hands them to the gateway over the existing Xray tunnel, and Clerk's sign-in goes through the gateway too.

**Architecture:** Caddy on the VPS serves `rahkar.pro` and `dash.rahkar.pro` and proxies to `127.0.0.1:18481` (Xray's
`dash` inbound → gateway box's dashboard listener). The gateway gains a Clerk Frontend API proxy at
`/__clerk/*` (Clerk "proxy" mode), so the browser never talks to Clerk's Cloudflare host. Telegram's webhook moves
to a host that stays on the Cloudflare tunnel.

**Tech Stack:** Python/FastAPI + httpx (gateway), vanilla JS (dashboard), Caddy, Xray, Cloudflare DNS/tunnel,
Clerk Backend API.

**Spec:** `docs/superpowers/specs/2026-11-14-iran-hosting-design.md`

## Global Constraints

- Work on `master`; push → tests → auto-deploy (`.github/workflows/deploy.yml`). Commit only this plan's paths.
- Proxy URL: `https://dash.rahkar.pro/__clerk` (one value, used in config, Clerk and the frontend).
- VPS address `95.38.187.63` (`ssh iran-vps.rahkar.pro`); gateway box `ec2-user@3.18.205.89`.
- `claude.rahkar.pro` (API) does not move.
- Owner decisions: Clerk bot protection may be turned off if Turnstile freezes from Iran; loss of Cloudflare
  protection accepted; Claude makes the external changes (Clerk API, Cloudflare, Google/GitHub consoles) in the
  owner's Chrome.
- Never smoke-test destructive commands against live resources.

## Review Focus

1. The proxy must not send the dashboard's own session cookie (`web.COOKIE`) or `Authorization` to Clerk — a test
   asserts the forwarded request lacks both.
2. Clerk's `Set-Cookie` and `Location` (OAuth redirects) must reach the browser unchanged — test asserts both pass.
3. Only `/__clerk/...` is forwarded, only to Clerk's Frontend API; a path like `/__clerk/../api/keys` or an absolute
   URL in the path must not reach any other host — test asserts the upstream URL host is always the FAPI host.
4. Tokens issued in proxy mode must still verify (issuer may be the FAPI host or the proxy URL) and tokens from
   other issuers still fail — test both.
5. With `clerk_proxy_url` empty everything behaves as today (scripts from the FAPI host, CSP as today) — the existing
   signup tests stay green and one test asserts the old `/api/auth-config` shape.

---

### Task 1: Clerk Frontend API proxy in the gateway

**Files:**
- Create: `src/claude_proxy/clerk_proxy.py`
- Modify: `src/claude_proxy/config.py` (`SignupConfig`: `clerk_proxy_url: str = ""`), `src/claude_proxy/web.py`
  (mount route when set), `src/claude_proxy/clerk.py` (`Verifier` accepts proxy issuer)
- Test: `tests/test_clerk_proxy.py`, `tests/test_signup.py`

**Interfaces:**
- Produces: `cfg.signup.clerk_proxy_url`; `clerk_proxy.UPSTREAM = "https://frontend-api.clerk.dev"`;
  `clerk_proxy.mount(app, cfg, http: httpx.AsyncClient, secret: str) -> None` adds
  `@app.api_route("/__clerk/{path:path}", methods=[GET, POST, PUT, PATCH, DELETE, OPTIONS])`;
  `clerk.Verifier(..., proxy_url: str = "")`.

- [ ] **Step 1: Write failing tests** in `tests/test_clerk_proxy.py` using `httpx.MockTransport` for the upstream:
  - `test_forwards_path_query_and_body`: `POST /__clerk/v1/client/sign_ins?_clerk_js_version=6` reaches
    `https://frontend-api.clerk.dev/v1/client/sign_ins?_clerk_js_version=6` with the same body.
  - `test_adds_clerk_headers`: upstream sees `Clerk-Proxy-Url: https://dash.rahkar.pro/__clerk`,
    `Clerk-Secret-Key: <secret>`, `X-Forwarded-For: <client ip>`.
  - `test_strips_own_session_and_auth`: request with `Cookie: <web.COOKIE>=abc; __client=x` and
    `Authorization: Bearer y` → upstream Cookie is `__client=x`, no Authorization.
  - `test_passes_set_cookie_and_location`: upstream 302 with `Location` and two `Set-Cookie` → both reach the client.
  - `test_upstream_host_is_fixed`: paths `..%2Fapi%2Fkeys`, `//evil.example/x`, `https:/evil.example` → upstream
    host is always `frontend-api.clerk.dev`.
  - `test_off_without_config`: `clerk_proxy_url = ""` → `GET /__clerk/v1/environment` is 404.
  - In `tests/test_signup.py`: `test_proxy_issuer_accepted` (iss `https://dash.rahkar.pro/__clerk` with
    `proxy_url` set passes) and existing "another Clerk instance" case still fails.
- [ ] **Step 2:** `pytest tests/test_clerk_proxy.py tests/test_signup.py -q` → new tests FAIL.
- [ ] **Step 3: Implement.** Stream the response (`http.send(req, stream=True)`, `StreamingResponse`), drop
  hop-by-hop headers and `content-encoding`/`content-length` only if the body is re-encoded (prefer passing bytes
  through untouched), keep multiple `Set-Cookie`. Client IP from `request.client.host` (uvicorn already applies
  X-Forwarded-For). Mount only when `cfg.signup.clerk_proxy_url` and the secret are set. `Verifier` accepts `iss`
  in `{f"https://{fapi}", proxy_url.rstrip("/")}`.
- [ ] **Step 4:** `pytest -q` → all PASS.
- [ ] **Step 5:** Commit `feat: Clerk Frontend API proxy at /__clerk (Clerk proxy mode)`.

### Task 2: Dashboard and CSP use the proxy

**Files:**
- Modify: `src/claude_proxy/web.py` (`/api/auth-config` adds `"proxy_url"`; `SecurityHeaders(clerk_host=None
  when proxy on)` but keeps `https://img.clerk.com` and `https://challenges.cloudflare.com`),
  `src/claude_proxy/static/app.js` (`setupClerk`)
- Test: `tests/test_web.py` (or `tests/test_signup.py`)

**Interfaces:**
- Consumes: `cfg.signup.clerk_proxy_url` (Task 1).
- Produces: `/api/auth-config` → `clerk: {publishable_key, frontend_api, proxy_url}` (`proxy_url` is `""` when off).

- [ ] **Step 1: Failing tests:** `test_auth_config_names_proxy` (`proxy_url == "https://dash.rahkar.pro/__clerk"`);
  `test_csp_with_proxy` (no `clerk.test.dev` in `script-src`/`connect-src`; `challenges.cloudflare.com` in
  `frame-src` and `script-src`; `img.clerk.com` in `img-src`); `test_auth_config_without_proxy` (old shape plus
  `proxy_url == ""`, CSP unchanged).
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement.** `setupClerk`: `const base = cfg.clerk.proxy_url || \`https://${cfg.clerk.frontend_api}\``;
  load `${base}/npm/@clerk/ui@1/dist/ui.browser.js` and `${base}/npm/@clerk/clerk-js@6/dist/clerk.browser.js` with
  attribute `data-clerk-proxy-url` = `proxy_url` when set.
- [ ] **Step 4:** `pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat: dashboard loads Clerk through the proxy when configured`.

### Task 3: Telegram webhook on its own host

**Files:**
- Modify: `src/claude_proxy/config.py` (`TelegramConfig.webhook_url: str = ""`), `src/claude_proxy/telegram.py:349`
  (use `webhook_url` when set, else today's `dashboard_url + /telegram/webhook`)
- Test: `tests/test_telegram.py`

- [ ] **Step 1: Failing test** `test_webhook_url_override`: with `webhook_url = "https://hooks.rahkar.pro/telegram/webhook"`
  the set-webhook call uses it; without it, today's URL.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** `pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat: [telegram] webhook_url, so the webhook can stay on Cloudflare`.

### Task 4: Ship the code with the proxy on (site still on Cloudflare)

**Files:** Modify `deploy/lightsail/config.toml` (`[signup] clerk_proxy_url = "https://dash.rahkar.pro/__clerk"`,
`[telegram] webhook_url = "https://hooks.rahkar.pro/telegram/webhook"`).

- [ ] **Step 1:** Push master; wait for the deploy workflow to pass.
- [ ] **Step 2:** `curl -s https://dash.rahkar.pro/__clerk/v1/environment | jq .auth_config` → JSON from Clerk.
- [ ] **Step 3:** Set Clerk's proxy: read `CLERK_SECRET_KEY` on the gateway box (never print it), `GET
  https://api.clerk.com/v1/domains` → production domain id, `PATCH /v1/domains/{id}` with
  `{"proxy_url": "https://dash.rahkar.pro/__clerk"}` → 200.
- [ ] **Step 4:** In the owner's Chrome: sign out of the dashboard, sign in with email, then with Google, then
  GitHub; Network shows Clerk calls only to `dash.rahkar.pro/__clerk`. If Clerk's dashboard lists a new OAuth
  redirect URI for Google/GitHub, add it in Google Cloud Console and GitHub OAuth app settings first.
- [ ] Rollback point: `PATCH` `proxy_url: null` and set `clerk_proxy_url = ""`.

### Task 5: Telegram webhook host

- [ ] **Step 1:** Cloudflare dashboard (owner's Chrome) → Tunnels → claude-gateway → add route
  `hooks.rahkar.pro` → `http://127.0.0.1:18481`.
- [ ] **Step 2:** On the box: `claude-proxy telegram set-webhook`; `getWebhookInfo` shows the new URL, no
  `last_error_message` after a test message to the bot.

### Task 6: Caddy on the VPS, DNS switch, check from Iran

- [ ] **Step 1:** Append to `/etc/caddy/Caddyfile` on the VPS:
  `rahkar.pro, dash.rahkar.pro { encode zstd gzip; reverse_proxy 127.0.0.1:18481 { flush_interval -1 } }`
  (Host is preserved by default; X-Forwarded-For handled as in the `claude-ir` block). `caddy validate` → OK;
  `systemctl reload caddy`.
- [ ] **Step 2:** At a quiet hour, in Cloudflare DNS (owner's Chrome): replace the tunnel records for `rahkar.pro`
  and `dash.rahkar.pro` with `A 95.38.187.63`, proxy off, TTL 1 min. Note the old record values for rollback.
- [ ] **Step 3:** `journalctl -u caddy` shows both certificates obtained; `curl -sI https://dash.rahkar.pro/dashboard`
  from the Mac → 200.
- [ ] **Step 4:** In the VPS browser (noVNC): rahkar.pro and the dashboard load fully; sign up with a test email
  through Clerk. If Turnstile hangs, turn off bot protection in Clerk (owner approved) and retry.
- [ ] **Step 5:** From the VPS, 8 repeated full downloads of `https://dash.rahkar.pro/static/app.js` all complete.
- [ ] Rollback: put the two DNS records back on the tunnel (routes still there).

### Task 7: Monitor, docs, key rotation

**Files:** Modify `.github/workflows/monitor.yml`, `deploy/lightsail/README.md`.

- [ ] **Step 1:** monitor.yml: new step "Site through the Iranian VPS". `/health`
  is not served on the dashboard (`/health` lives on the API listener only), so check `https://dash.rahkar.pro/api/auth-config`
  → 200 with `.clerk.proxy_url` set, and `https://rahkar.pro/` → 200, same recheck pattern,
  `if: always()`.
- [ ] **Step 2:** README: rahkar.pro/dash on the VPS (Caddy blocks, Xray 18481 already allowed on both ends),
  `hooks.rahkar.pro` route, Clerk proxy and how to turn it off, rollback steps, the `iranbrowser-*` units.
- [ ] **Step 3:** Rotate the Xray Reality key pair: `xray x25519` on the box, new private key in the box's config,
  new public key in the VPS outbound, restart both; `curl https://claude-ir.rahkar.pro/health` → ok.
- [ ] **Step 4:** Commit `docs+monitor: rahkar.pro and dashboard served via the Iranian VPS`, push.
