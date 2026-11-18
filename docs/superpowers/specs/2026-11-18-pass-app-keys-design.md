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

## 3a. After the builder check (2026-11-18)

Decided by the user:
- **Running pass only.** A pass key is served only while the user has a running pass (incl. a running free trial).
  It never falls back to hand-set limits, sign-up credit or an admin's ungated account, and its requests never
  auto-start a waiting free trial (`limits.evaluate` does both for Claude Code keys, limits.py:595-607). Refusal:
  403 `permission_error`, `rejected_by='pass_key_no_pass'`, "This key works while you have a running pass."
- **Own source "apps".** Pass-key traffic is a third source next to `cli` and `api` in Sessions/Requests
  (`SOURCES`, the source toggle, `web.py` filters on `pass_key_id`). Prompts and reply contents follow the credit-key
  rules (`contents.wanted` treats a pass key like an API key).
- **Never around pass limits (overrides "own pool", 2026-11-18).** Pass-key usage must never let a user get past
  their pass's limits. Concretely:
  - Pass-key requests count in the user's shared in-flight registry (`inflight`), so Claude Code and app keys
    together stay under `[limits] max_inflight`: the overshoot bound of a pass is unchanged.
  - Within that, app keys hold at most `PASS_KEY_MAX_INFLIGHT = 4` (tracked in a second registry,
    `pass_inflight`) and `PASS_KEY_RPM = 60` per account, so Desktop can't take every slot.
  - Pass-key rows are written with `api_key_id` NULL and `ticket_id` set, so every pass-use query (`_weighted_since`,
    `_week_use`, window limits, `was_used`, capacity, quota attribution) counts them exactly like Claude Code rows.
    No usage or limit query may filter on `pass_key_id`; only the dashboard source filter does.

Builder's calls (marked as such):
- Every three-way key-scope check learns `pass`: `app.scope_allows`, the `is_api` uses in `app.py`,
  `contents.wanted`, the dashboard sign-in refusal (`web.py` ~262, ~326), `_account_line`.
- Extras refusal for pass keys uses app-neutral wording (no `/fast`, no `[1m]`).
- A computer's key or session cannot create or revoke pass keys (same `not_a_computer` rule as credit keys).
- The API tab shows when `[api]` is on **or** passes are on; with `[api]` off it shows only the pass card.

## 4. Non-goals

- Changing credit API keys, Claude Code keys or computer keys.
- The OpenAI format.
- Pricing extras (web search, fast mode, 1M) on a pass.

## 5. Left to the builder

- Table name and shape (likely mirror `api_keys`: id, user_id, label, key_hash, prefix, created_at, revoked_at,
  last_used_at).
- Exact refusal text when the user has no pass, if the Claude Code one mentions Claude Code specifically.
- Where the shared extras check lives so both key types use it.
