# Sign-up and browser authorization

- **Date:** 2026-09-26
- **Builds on:** `2026-09-21-claude-proxy-design.md` (sections 8, 10 and 17.2) and `2026-09-25-client-side-and-opencode-routes-design.md` (the `claude-gateway` script).
- **Goal:** a teammate sets up with one short command and a click in the browser. No key is copied or pasted, and no admin needs to act first.

## 1. What the person does

1. Opens the dashboard and signs up with Google, GitHub or an emailed code. The account starts with a one-time credit (default $5).
2. Copies the one command the dashboard shows, `curl -fsSL https://<dashboard>/install | sh`, and runs it.
3. The browser opens on the dashboard with a code. The person checks it matches the terminal and clicks **Authorize**. The terminal finishes setting up gclaude.

When the credit is used up, requests are refused with a message saying so. The admin then gives the account a real limit (see section 5).

On a machine without a browser (over SSH), the command prints the link and the code, and the person opens them on any device.

## 2. Identity: Clerk

[Clerk](https://clerk.com) handles sign-up and sign-in with Google, GitHub and email codes, using bot protection and blocking of throwaway addresses. Everything it needs is on the free plan. Clerk only proves who the person is:

- **Browser.** The dashboard loads `clerk-js` from the instance's Frontend API host (derived from the publishable key) and mounts Clerk's sign-in component.
- **Exchange.** Once signed in, the browser posts the Clerk session token to `POST /api/login/clerk`.
- **Token check.** The gateway verifies the token itself: RS256 against the instance's JWKS (`https://<frontend api>/.well-known/jwks.json`, cached), `exp`/`nbf` with 10 s leeway, `iss` equal to the Frontend API origin, and `azp`, when present, equal to `listener.dashboard_url`.
- **Email lookup.** It then reads the user's verified primary email from Clerk's Backend API (`GET /v1/users/{sub}`, with `CLERK_SECRET_KEY`). This happens only at sign-in.
- **Session.** Finally it starts the gateway's own cookie session (unchanged, section 10.1).

Every dashboard page and every proxied request uses the gateway's sessions and keys, never Clerk. If Clerk is down, new sign-ins wait and nothing else is affected. Admin password sign-in and key sign-in stay as they are.

**Mapping a Clerk user to a gateway user:**

1. **Linked user.** A user whose `clerk_id` matches is the one.
2. **Verified email.** Otherwise, a user whose `email` or `name` equals the verified email, compared case-insensitively, is linked: `clerk_id` and `email` are set. This is how existing accounts named by email join.
3. **Removed user.** If that user is revoked, sign-in is refused. A removed account never gets a new credit.
4. **New account, open sign-ups.** With no match and `signup.enabled`, a user is created: named after the email, role `user`, and a `cost_total` limit of `signup.credit_usd`. It is audited as `signup`.
5. **New account, closed sign-ups.** With no match and sign-ups off, the answer is 403 "Sign-ups are closed; ask the gateway admin for an account."

Clerk's own settings do the rest:
- Sign-in methods: email code, Google and GitHub.
- Bot protection on.
- Email sub-addresses and disposable domains blocked.
- Production instance on the dashboard's domain, with the DNS records set to DNS only in Cloudflare.
- Its own Google and GitHub OAuth credentials.

## 3. Keys per machine

A user has had one full key (`users.key_hash`), stored only as a hash. Each browser authorization therefore creates a new key in a new table:

```
keys(id, user_id → users ON DELETE CASCADE, key_hash UNIQUE, key_prefix, label, created_at, last_used_at, revoked_at)
```

- `find_user_by_key` also matches a key in `keys` that is not revoked, as a full key.
- `last_used_at` is written at most once an hour per key, so the hot path stays read-only.
- The user's original key is unchanged, and so are admin rotate, revoke and delete: revoking or disabling the user stops all their keys.
- The dashboard shows "Your machines" (label, prefix, created, last used), and a person can remove one. An admin sees and removes them on the user's page.

## 4. Browser authorization (device flow)

Everything is served by the dashboard listener, shaped after RFC 8628.

| Call | Who | Does |
|---|---|---|
| `POST /api/device/start {label}` | anyone; at most 10 per client address per 5 min | Creates a request with an 8-letter user code (`BCDF-GHJK`, no vowels or look-alikes) and a random device code, which is stored only as a hash. Expires in 10 min. Returns `device_code, user_code, verification_uri, verification_uri_complete (…/dashboard#authorize/<code>), interval (3 s), expires_in`. |
| `GET /api/device/<user_code>` | signed-in session | The pending request's label and time left, for the Authorize page. 404 if unknown or expired. |
| `POST /api/device/<user_code>/approve` / `deny` | signed-in session, with CSRF | Records the decision against the signed-in user. |
| `POST /api/device/token {device_code}` | the CLI | `authorization_pending` (400) until decided; `slow_down` when polled faster than the interval; `access_denied` or `expired_token` (400, and the request is removed); once approved, **creates the key now**, removes the request and returns `{key, user, url, dashboard}`. The key is never stored in plain text, not even briefly. |

- **The Authorize page** (`#authorize/<code>`) shows the machine's label and the code in large type: "Check that your terminal shows this code. Only authorize if you just ran `claude-gateway on` yourself."
- **Returning after sign-in.** If the person isn't signed in, the code is kept in `sessionStorage` across the sign-in, including Google or GitHub redirects, and the page comes back after it.
- **Cleanup.** Expired requests are deleted by the daily `cleanup` and at each start.

**`GET /install`** returns a short sh script for this gateway:

```
curl -fsSL <signup.installer_url> | sh -s -- on --url <public_url> --dashboard <dashboard_url>
```

## 5. Credit: the `cost_total` limit

- **What it is.** A new limit kind, `cost_total` (usd): the user's estimated cost over all forwarded requests kept in the database, optionally scoped to models. Retention (180 days) bounds it. It has no window, so `reset_in` is always empty.
- **Where it counts.** It is enforced like the others and shows as `credit $1.20/$5.00 24%` in the status line and in `/usage`. The dashboard calls it "credit".
- **When it runs out.** The rejection reads: "Your gateway credit is used up ($5.00 of $5.00). Ask the gateway admin for more."
- **Upgrading.** Limits apply together, so a real limit only helps once the credit is gone. The Users page gets an **Upgrade** action, which removes `cost_total` and sets `cost_daily` to an amount the admin picks (default $100). Removing or raising the limit in the Limits dialog works too.

## 6. `claude-gateway on` without a key

- **Authorizing.** When no key is given and none is saved for this gateway, `on` authorizes in the browser:
  - It needs `--url`, or a saved URL. The installer passes both URLs.
  - It posts to `start` with the label `<short hostname>`.
  - It prints the link and the code.
  - It opens the link with `open` or `xdg-open`, unless over SSH or neither command exists.
  - It polls `token` until the key arrives, 10 minutes pass, or the request is denied.
  - It saves the key the way `--key` does and continues as before (gclaude by default).
- **Other options.** `--login` authorizes again even with a saved key, for example after a machine was removed. `--key` still works.
- **Re-running the installer** on a machine that already has a key for that URL only updates. It doesn't ask again.

## 7. Dashboard

- **Login card.** When Clerk is configured, Clerk's sign-in (which includes sign-up) comes first; the admin and key forms sit behind "Other ways to sign in". `GET /api/auth-config` (no sign-in needed) tells the page whether Clerk is on and its publishable key.
- **Signed-in user's Overview:** a "Set up a computer" card with the install command, plus "Your machines".
- **Content-Security-Policy** gains, only when Clerk is configured:
  - the Frontend API host (script, connect, img),
  - `https://img.clerk.com`,
  - `https://challenges.cloudflare.com` (script, frame, for bot protection),
  - `worker-src 'self' blob:`.
- **Sign out** also signs out of Clerk.

## 8. Configuration

```toml
[listener]
public_url = "https://claude.rahkar.pro"
dashboard_url = "https://claude-dash.rahkar.pro"

[signup]
enabled = true
credit_usd = 5.0
clerk_publishable_key = "pk_live_…"
```

`CLERK_SECRET_KEY` goes in `gateway.env`. Without a publishable key, Clerk sign-in is off. Without `public_url` and `dashboard_url`, `/install` and the device flow answer 503.

## 9. Database changes

All are additive, so rollback keeps working:
- `users.email` and `users.clerk_id`, with a unique index on `clerk_id`.
- A `keys` table.
- A `device_requests` table: `id, device_hash UNIQUE, user_code UNIQUE, label, created_at, expires_at, last_poll_at, user_id, decision`.

## 10. Testing

- **Clerk verification**, with a locally generated RSA key and JWKS: a good token, and each check failing (expiry, issuer, `azp`, signature, unknown `kid`).
- **Account mapping:** link by email, refuse a revoked user, create a user with credit, refuse when sign-ups are closed.
- **Device flow:** pending, `slow_down`, deny, expiry, approve then a single pickup, and the new key working against the proxy.
- **`cost_total` enforcement** and its status-line text.
- **CLI device login** against a stub dashboard, including SSH mode (no opener) and re-running with a saved key.
- **The `/install` script** contents.
