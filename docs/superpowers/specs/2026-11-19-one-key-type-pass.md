# One key type: API keys draw from the pass

- **Date:** 2026-11-19
- **Status:** decided by the user 2026-11-19 ("remove tariffs; we charge API tokens the same as pass tokens, no
  separate charging"; picked "One key type: the pass")
- **Replaces:** the credit part of `2026-10-21-api-credit-design.md`. **Builds on:** `2026-11-18-pass-app-keys-design.md`.

## Intent

There is only one way to pay: the pass. Requests made with an API key count against the pass, the way Claude Code's do. USD credit, per-model API rates and the credit gate go away.

## Decisions

- **Existing `sk-proxy-a-` keys keep working.** A one-time migration copies every live `api_keys` row into
  `pass_keys`, keeping the same hash, prefix, label, created_at and last_used_at, then marks the `api_keys` rows revoked. From then on they are pass app keys. They need a running pass, they count against it, and they follow the same rules as app keys. New keys are only `sk-proxy-p-`.
- **No credit scope.** `db.find_user_by_key` no longer returns `key_scope="api"`. The credit gate, the per-request charge and the credit-only model list leave the proxy.
- **Dashboard.** The user "API & apps" tab shows only the pass app keys card. Balance, credit setup, credit keys, credit usage and the rates table are gone. The admin "API" tab (rates, accounts, credit, limits, everyone's credit keys) is removed. Admins still manage a user's app keys on the user page. The `/api/me/api*` and `/api/admin/api*` endpoints are removed.
- **History stays.** The `api_keys`, `api_ledger`, `api_rates` and `api_account_limits` tables and the old rows' `requests.api_key_id` stay in the database, but nothing reads them in the UI. The "API" source in Sessions and Requests is folded into "Apps": `pass_key_id IS NOT NULL OR api_key_id IS NOT NULL`.
- **Kept:** `api_credit.refuse_extras` (web search, fast mode and 1M stay refused on app keys). The `api_rates` table and its seeding go too (nothing read them; the app-key model list uses `[pricing]`).
- **Balances left on accounts are dropped.** They are not refunded or converted. That is the user's call when they pick this option.
