# Multiple Claude subscriptions: sharded multi-account design

- **Date:** 2026-10-13
- **Status:** Accepted; implements step 1 (storage + login). Routing, quota and dashboard steps deferred.
- **Supersedes:** the non-goal in `2026-09-21-claude-proxy-design.md` §1 ("Pooling or rotating across multiple subscription accounts").

## 1. Locked decisions

Decision 8 is implemented in this step; 1–7 are locked now but land with the deferred steps (section 4).

1. **Sharding, not pooling.** Every Claude request has exactly one eligible account and never crosses to another: a ticket user's request goes to their ticket's account; a non-ticket user's request goes to their assigned account, defaulting to one admin-set global default. No cross-account failover.
2. **Ticket ↔ account:** each ticket is bound to exactly one account at grant. The grant form requires an explicit account pick (no silent default). The admin may move a ticket to another account; the move rechecks capacity on the new account and enforcement switches immediately.
3. **Ticket traffic:** strict pin. A 429 or a 401-that-survives-refresh on the ticket's account passes through to the client (429 / 503); it never fails over.
4. **Non-ticket traffic:** fixed account (per-user assignment, falling back to the global default). Only the existing 401 → refresh → retry-once happens, on that one account (`app.py:288` unchanged in shape).
5. **Hand-set `share_5h`/`share_7d` limits:** enforced against the serving account's utilization snapshots.
6. **Health:** all-healthy — `/health` `credential` is true only if every linked account is healthy. The dashboard lists each account separately.
7. **History:** requests and quota snapshots recorded before multi-account have no account label and count as the `default` account, so existing tickets and billing keep working. Existing tickets carry `account_id = 1`, which means `default`.
8. **Identity:** an Anthropic account is identified by `account.uuid` in the OAuth token response (`token_record` also accepts `id`/`sub`/`user_id`, first one present wins), stored as `anthropic_sub`; `account.email_address` is display only. Linking the same subscription twice is refused. Grants linked before this step have a NULL `anthropic_sub` and are not checked for duplicates until they log in again.

## 2. Storage

`credentials` gains an account dimension; one row per linked subscription:

```sql
CREATE TABLE credentials (
    backend TEXT,                 -- always 'oauth' in this step
    account TEXT DEFAULT 'default',
    encrypted_blob TEXT,
    expires_at INTEGER,
    updated_at INTEGER,
    state TEXT,                   -- active | needs_login | decrypt_failed
    label TEXT,                   -- display name, defaults to the account slug
    anthropic_sub TEXT,           -- stable dedupe key, plaintext (may be NULL on old grants)
    email TEXT,                   -- display only; the blob stays the source of truth
    PRIMARY KEY (backend, account)
)
```

- Account slugs: `^[a-z0-9][a-z0-9_-]{0,31}$` (`credentials.ACCOUNT_RE`).
- Migration (`db._migrate`): old single-row tables are rebuilt with that row as `account='default'`; the rebuild runs in one transaction so a crash cannot strand the grant. The three new columns are `ADD COLUMN`ed when absent, and a partial unique index on `(backend, anthropic_sub) WHERE anthropic_sub IS NOT NULL` enforces one slug per subscription. No data loss; old code paths that address the default account keep working.
- `tickets.account_id` already exists (integer, `tickets.ACCOUNT_ID = 1` today). Credential accounts are text slugs, so the two are not yet the same key. Fixed now so step 2 needs no remap: `account_id = 1` ≡ `'default'`. The routing step either changes `tickets.account_id` to the slug or gives `credentials` a stable integer id, and migrates `1 → 'default'`. This step does not change ticket semantics.
- Pending-login keys move from `pending_login` to `pending_login:{account}`. A login still in progress during the deploy is lost (the migration deletes the old row), so run `login` again (the window is 15 minutes).

## 3. Login (per account)

- `login.start(conn, cfg, account)` stores the PKCE verifier under `pending_login:{account}` (15-minute TTL, as before); concurrent logins for different accounts no longer clobber each other.
- `login.finish(conn, cfg, http, pasted, account)` exchanges the code, builds the record via `token_record` (which now also extracts `anthropic_sub`), refuses the exchange when another account row already carries the same non-null `sub` (`LoginError: already linked`; the check can only run after the exchange, so the user restarts from `--print-url`; a concurrent race is caught by the unique index), and stores via `OAuthBackend(..., account).store(record)`.
- Refresh answers may omit `account`. A refresh keeps the grant's `anthropic_sub` and email, both in the encrypted blob and in the plaintext columns (the `store` upsert uses `COALESCE`), so the duplicate check and the displayed email survive refreshes.
- CLI: `claude-proxy login --account <slug>` (default `default`); `claude-proxy status` lists every account and flags `default` as NOT OK when it is unlinked; `claude-proxy account list` / `claude-proxy account remove <slug>` (refuses to remove the last remaining row, and refuses `default` while the gateway still serves all traffic from it; to replace that subscription, re-link it with `login --account default`).
- Dashboard: an admin-only **Subscriptions** tab (`#accounts`) lists every account (name, Claude email, state, token expiry, last update), links a new one or re-links an existing one with the same paste-the-code flow (`POST /api/admin/accounts/{slug}/login/start` → authorize URL; `.../login/finish` with `{code}`), and removes non-default accounts (`.../remove`). The remove rules live in `login.remove_account`, shared with the CLI. A banner warns when `default` is unlinked. Re-linking `default` reaches the running gateway at once, because `_load` reloads whenever the stored row changes.
- `OAuthBackend(cfg, conn, http, account)` scopes `_load` / `store` / `_mark_needs_login` to its `(backend, account)` row. Refresh locks, backoff and `needs_login` marking stay per account: a 401 on A never spends B's single-use refresh token. `Gateway` keeps constructing the default account's backend, so runtime behavior on one subscription is unchanged.

## 4. Explicitly deferred

- Routing: ticket→account binding at grant, per-user non-ticket assignment, global `default_account` setting.
- Quota: `account` column on `quota_snapshots` and `requests`; per-account attribution, `observed_rate` caches, `Poller`, `USAGE_FRESH_SQL`, `estimates`.
- Per-account quota in `/api/overview` + `/api/session`; `/health` all-healthy aggregation.
- Backfill beyond "old rows read as `default`".

## 5. Tests

- Per-account pending isolation; second login under another slug coexists.
- Duplicate `sub` refused across slugs.
- Old-DB migration: single legacy row becomes `account='default'` with tokens intact.
- Per-account refresh isolation: 401 on A refreshes only A's row.
- CLI `account remove` refuses the last row and refuses `default`; `status` flags a missing `default`.
- The unique index refuses a second row with the same `anthropic_sub`; migration drops the stale `pending_login` row.
- Dashboard (`tests/test_accounts_web.py`): list, link, re-link of `default` reaching the live backend, duplicate refused, remove rules, admin only, CSRF.
- A refresh whose answer has no `account` keeps `anthropic_sub`/email, and the duplicate check still fires afterwards.
