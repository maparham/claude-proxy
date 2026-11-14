# Serving rahkar.pro and the dashboard from the Iranian VPS

## Why

From Iran, connections through Cloudflare freeze partway. Tested 2026-11-14 from the Iranian VPS (ArvanCloud,
`95.38.187.63`): downloads from some Cloudflare addresses (172.67.x, 104.18.x) always stopped at 86–103 KB; the
188.114.x addresses mostly finished but still froze now and then. A browser on the VPS could not load rahkar.pro or
the dashboard: `i18n.js` (227 KB) and `app.js` (267 KB) never arrived. Clerk's sign-in API (`clerk.dash.rahkar.pro`,
Clerk's own Cloudflare, 104.18.34.146) froze in every one of 8 tries. So people in Iran — the customers — often
cannot buy, sign in or see their dashboard.

The same files through the existing Xray tunnel from the VPS to the gateway box (`127.0.0.1:18481` on the VPS)
arrived whole every time, in about 1 s.

## Decision

`rahkar.pro` and `dash.rahkar.pro` point at the Iranian VPS **for everyone** (DNS only in Cloudflare, no orange
cloud). Caddy on the VPS terminates TLS (Let's Encrypt, as for `claude-ir.rahkar.pro`) and hands both to the
existing Xray tunnel, which delivers them to the gateway's dashboard listener (`127.0.0.1:18481`). Same host names,
same single gateway and database; nothing is copied.

Accepted trade-offs (chosen by the owner): visitors outside Iran go through Iran too (slower), and if Iran's
international link goes down, the site is down for everyone.

## Approaches considered

1. **Auto-redirect by country** — keep Cloudflare, send visitors Cloudflare marks as Iranian to Iran-only host
   names. Nobody else affected, but needs a second set of host names throughout the app (Clerk origin, cookies,
   redirects, links).
2. **Separate Iran links only** — same new host names, no redirect; visitors who type rahkar.pro still freeze.
3. **Move rahkar.pro and dash.rahkar.pro to the VPS** — chosen: no second set of names, every visitor gets the
   working path; costs speed abroad and ties the site's availability to Iran's link.

## Parts

### VPS (Caddy)

- Site blocks for `rahkar.pro` and `dash.rahkar.pro` → `reverse_proxy 127.0.0.1:18481` (Xray's existing `dash`
  inbound), preserving the Host header so the gateway's home/dashboard split (`HomeHost`) keeps working.
- Caddy compresses responses (`encode zstd gzip`); the tunnel carries them uncompressed today.
- X-Forwarded-For set to the real client IP, as the API relay already does; the gateway already trusts it from
  127.0.0.1.

### Clerk through the gateway

Clerk's Frontend API cannot be reached from Iran, so it goes through Clerk's **proxy** mode: Clerk's production
instance gets a proxy URL on our own host (e.g. `https://dash.rahkar.pro/__clerk`), and the gateway forwards
`/__clerk/*` to Clerk's Frontend API with the headers Clerk requires (`Clerk-Proxy-Url`, `Clerk-Secret-Key`,
`X-Forwarded-For`). The gateway box is in the US, so its side of that is not throttled.

- The dashboard loads `clerk-js` and `@clerk/ui` through the proxy too (they are served from the Frontend API's
  `/npm/...`), and starts Clerk with the proxy URL.
- CSP: `connect-src`/`script-src` no longer need `clerk.dash.rahkar.pro` once everything goes through `'self'`.
- Token verification (`clerk.Verifier`): JWKS fetch and the `iss` check must match what Clerk issues in proxy mode
  — the builder verifies which issuer Clerk puts in tokens then.
- Google and GitHub OAuth apps: their authorized redirect URI becomes the proxy's callback if Clerk requires it
  (Clerk's dashboard shows the value once the proxy is set).

### Things that must still reach the gateway from outside Iran

- **Telegram webhook** (`/telegram/webhook` on the dashboard listener): Telegram's servers are blocked in Iran in
  both directions, so the webhook must not go through the VPS. It moves to a host that stays on the Cloudflare
  tunnel to the gateway box (a new `hooks.rahkar.pro` → `127.0.0.1:18481`, or the existing route made to reach the
  webhook), and `telegram set-webhook` points there.
- **ZarinPal callback** (`rahkar.pro/pay/callback`) is a browser redirect from an Iranian payment page — fine
  through the VPS.
- **Hourly monitor** (`monitor.yml`) runs from GitHub (outside Iran); it should check the dashboard through the
  VPS as well, so an outage of the new path raises an alarm.

### Cutover and rollback

- Before switching DNS: Caddy on the VPS gets certificates (Let's Encrypt HTTP-01 needs the names to point at the
  VPS, so either switch DNS and let Caddy obtain them in the first minute, or use the DNS-01 challenge with a
  Cloudflare token).
- Switch the two DNS records from the Cloudflare tunnel to `A 95.38.187.63`, DNS only, short TTL.
- Rollback: put the two records back on the Cloudflare tunnel (the tunnel's routes stay in place), and turn the
  Clerk proxy off.
- `claude.rahkar.pro` (the API) stays on Cloudflare; gclaude already falls back to `claude-ir.rahkar.pro`.

### Bot check (Turnstile)

Clerk's bot protection is on (Turnstile, "smart" widget, checked 2026-11-14). Turnstile loads from
`challenges.cloudflare.com` in the browser and cannot go through the Clerk proxy. From the VPS its `api.js`
(87 KB) arrived 4 of 4 times in under 0.5 s, but the challenge frame loads more; the builder tests a full sign-up
in the VPS browser before cutover. If it freezes: see the owner's decision below.

### Builder's calls (marked as such)

- Order: turn the Clerk proxy on first, while the site is still on Cloudflare (the proxy works there too), test
  sign-in; then switch DNS. Rollback switches DNS back and leaves the proxy on.
- Certificates: HTTP-01 at a quiet hour (a minute or two of TLS errors right after the switch), unless a
  Cloudflare API token for DNS-01 is at hand.
- Monitor: the hourly check of `https://dash.rahkar.pro/health`-style URLs now covers the VPS path by itself,
  since every visitor uses it; failing it is an outage alarm like the gateway check.
- echarts (jsDelivr) and Google Fonts loaded fine from the VPS (1 MB echarts in 0.7 s); they stay as they are.
- Xray already allows and forwards `18481` on both ends (checked 2026-11-14); the README is updated to say so.

## Non-goals

- No second gateway, no database copy, no sync.
- The API host (`claude.rahkar.pro`) does not move.
- Telegram and YouTube links on the site stay as they are (blocked in Iran; WhatsApp works).

## Also to do

- Rotate the Xray Reality key pair on both ends: the gateway box's private key was printed into a Claude session
  log on 2026-11-14.
- The test browser on the VPS (`iranbrowser-*` systemd units, Chromium + noVNC on localhost, started with
  `--disable-quic` during testing) stays for checking the result from Iran; documented in
  `deploy/lightsail/README.md`.

## Left to the builder

- Exact Caddyfile, Clerk proxy route and header handling, CSP changes, how `setupClerk` gets the proxy URL
  (`/api/auth-config`).
- Whether the Telegram webhook gets a new host or an existing one.
- Monitor changes.
