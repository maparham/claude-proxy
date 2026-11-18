# Pass app keys: the API on a pass

- **Date:** 2026-11-18
- **Status:** approved 2026-11-18
- **Builds on:** `2026-10-21-api-credit-design.md` (API keys on credit), `2026-11-04-pass-capacity-in-points.md` (pass
  gating in points).

## 1. Intent

Pass holders get API keys they can use in any app that speaks the Anthropic Messages API, Claude Desktop included.
What a key uses counts against the holder's pass, exactly like Claude Code traffic, not against USD credit.

Claude Desktop reaches a gateway through Help → Troubleshooting → Enable Developer Mode, then Developer → Configure
Third-Party Inference → Gateway: base URL and API key (Bearer or x-api-key; the gateway reads both, `auth.py`).
Needs Desktop 1.2581.0 or newer.

## 2. Approaches

| Approach | Trade-off | Picked |
|---|---|---|
| Named pass app keys (new key type, pass-gated) | Per-app revoke; Claude Code key never leaves the user's machine | **Yes** |
| Show the Claude Code key and document other apps | No new code, but a leak in any app means rotating the Claude Code key | No |
| Credit API keys draw from the pass while one runs, then credit | One key, but which pool pays is hard to predict | No |

## 3. Decisions

- **Key type.** Prefix `sk-proxy-p-`, scope `pass` from `db.find_user_by_key` (with `pass_key_id`, `role="user"`).
  Stored as SHA-256, shown once, max 20 live keys per account. Self-serve create (label), list, revoke; an admin may
  create and revoke any user's. A pass key cannot create or revoke keys and cannot sign in to the dashboard.
- **Reach.** `POST /v1/messages`, `POST /v1/messages/count_tokens`, `GET /v1/models`, Claude models only (no
  third-party routes). Anything else 403 `key_scope`.
- **Gate.** The same `limits.evaluate` as the user's Claude Code key: pass share, 5-hour share, weekly points, limit
  rows. No running pass: the same refusal a Claude Code key gets. Requests count in pass usage (they are not API
  credit requests: `api_key_id` stays NULL).
- **Extras refused** (400 `invalid_request_error`, `rejected_by='api_extra'`), same rule and messages as credit
  keys: web search server tools, fast mode, 1M context beta.
- **Bookkeeping.** New column `requests.pass_key_id` (nullable), for per-key usage. No ledger.
- **Feature switch.** Available whenever tickets (passes) are on; not tied to `[api]`.
- **Dashboard.** A "Pass app keys" card in the API tab for every signed-in user (the tab shows when either `[api]` or
  passes are on): keys (create/revoke, key shown once), usage per key per day (last 30 days, tokens), setup for
  Claude Desktop and for any Anthropic-compatible app (base URL + key). Admin: a user's pass keys on the user page,
  with create/revoke. English and Persian (Persian wording rules: loanwords, کلاد in UI).

## 4. Non-goals

- Changing credit API keys, Claude Code keys or computer keys.
- The OpenAI format.
- Pricing extras (web search, fast mode, 1M) on a pass.

## 5. Left to the builder

- Table name and shape (likely mirror `api_keys`: id, user_id, label, key_hash, prefix, created_at, revoked_at,
  last_used_at).
- Exact refusal text when the user has no pass, if the Claude Code one mentions Claude Code specifically.
- Where the shared extras check lives so both key types use it.
