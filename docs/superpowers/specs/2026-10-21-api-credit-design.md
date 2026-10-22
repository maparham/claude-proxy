# API keys on prepaid credit (pay per token)

- **Date:** 2026-10-21
- **Builds on:** `2026-09-21-claude-proxy-design.md` (proxy, ledger, limits), `2026-10-03-paid-tickets-design.md` (the other way to pay).

## 1. What it is

A second way to pay for the gateway, next to tickets: an **API key** billed per token against a **prepaid USD credit**,
the way Anthropic's own API is billed. It serves the clients the gateway already serves: Claude Code, in the terminal
and in the VS Code extension. A user sets two variables and needs no installer:

```
ANTHROPIC_BASE_URL=<[listener] public_url>
ANTHROPIC_API_KEY=sk-proxy-a-…
```

Requests are forwarded as today (same proxy path, same subscription pool, same metering). Only the gate before the
request and the bookkeeping after it differ.

Not in scope: the OpenAI `/v1/chat/completions` format; non-Claude-Code clients; self-serve payment (admins add
credit by hand); third-party routes (`muse-spark*`) on API keys.

## 2. Decisions (agreed 2026-10-21)

| Topic | Decision |
|---|---|
| Endpoint | `/v1/messages` (+ `/v1/messages/count_tokens`, `GET /v1/models`), Anthropic format, streaming, tools: passed through unchanged |
| Billing | Per token against a per-account USD credit, separate from tickets, shares and limit rows |
| Rates | Admin-set USD per million tokens per model glob (input, output, cache write 5m/1h, cache read); seeded from the config's Claude list prices |
| Models | Only models that match a rate; anything else 404 `not_found_error` |
| Top-up | Admin grants (positive, or negative to correct), each with a note; ledger is append-only |
| Low balance | Refused when balance ≤ 0 before the request; the finished request is charged in full, so a balance may go slightly negative (bounded by `max_inflight` requests) |
| Rate limits | Per account: concurrent requests and requests per minute. Defaults in `[api]`, admin may override per account |
| Priority | First come, first served with Claude Code ticket users on the same subscriptions |
| Keys | Self-serve: create (label), list, revoke. Shown once, stored as SHA-256. Max 20 live keys per account |
| Usage | User: per key, per day tokens and cost (last 30 days), balance, grant history. Admin: per account balance, 30-day spend, limits |
| Isolation | Stateless, like the real API: nothing is kept between requests |

## 3. Behaviour

- **Key scope `api`.** `db.find_user_by_key` returns `key_scope="api"`, `role="user"` (never admin powers, like a
  machine key) and `api_key_id`. An API key may make: `POST /v1/messages`, `POST /v1/messages/count_tokens` for a
  non-routed model, and `GET /v1/models`. Everything else is 403. It cannot sign in to the dashboard.
- **Gate** (`api_credit.evaluate`, replaces `limits.evaluate` for API keys, in this order): concurrency (429
  `rate_limit_error`, `rejected_by='api_inflight'`), unpriced model (404 `not_found_error`, `api_unpriced`), credit
  ≤ 0 (400 `invalid_request_error` "Your credit balance is too low to access the API. Ask the gateway admin to add
  credit.", `api_credit`), requests in the last 60 s + in flight ≥ rpm (429, retry-after 60, `api_rpm`).
  `count_tokens` needs a priced model and rpm room (it reaches the subscription) but no credit, and is never charged.
  `GET /v1/models` (no model) passes the gate.
- **Charge.** When the request's `requests` row is written, if it was forwarded and reports tokens, one `charge`
  row goes to `api_ledger` with the cost from the rate of the requested model (the reply's model id only if the
  requested one has no rate; most specific glob: longest pattern wins). Cut-short
  streams are charged for what the meter counted. Zero-cost requests (errors before any usage) write no row.
- **`/v1/models`** for an API key: Anthropic's list, filtered to models that have a rate.
- Ticket gating, share limits, the free-credit cap and limit rows do not apply to API keys, and API requests are left
  out of the usage those count (`requests.api_key_id IS NULL`). API traffic still counts in subscription quota
  attribution, under its user, since it uses the subscriptions; so a user who holds a ticket *and* uses API keys sees
  their API traffic in their ticket's share figures. Rare enough to accept.
- Overdraft is bounded by request count (`max_inflight` per account), not dollars.
- Nothing checks that the client is Claude Code; the docs say it is the only supported client.
- Refused on API keys (400, `api_extra`), because the per-token rates can't price them (decided 2026-10-22): web search
  (a `web_search*` server tool), fast mode (`speed` other than `standard`, or a `fast-mode*` beta) and the 1M-token
  context (`context-1m*` beta). Ordinary Claude Code requests carry none of these.
- Token counts and the model list hold an in-flight slot and count toward rpm, so a key with no credit can't flood the
  subscription with them.
- Feature switch: the `[api]` config section (present = on), like `[tickets]`. Off: API keys are refused with 403
  "The API is not enabled on this gateway." and the dashboard hides the tab.

## 4. Data

New tables `api_keys`, `api_rates`, `api_ledger`, `api_account_limits`; new column `requests.api_key_id`. The ledger is
never deleted by retention cleanup (the balance is its sum). Deleting a user keeps their ledger rows with
`user_id` NULL.

## 5. Dashboard

A new **API** tab (feature `api`) for every signed-in user: balance, setup snippet, keys (create/revoke), usage per
key per day, the rate list. An admin sees, in the same tab: the rate editor (one Save per card, plus add/delete a
glob) and the accounts table (balance, 30-day spend, limits) with Add credit and Limits forms. Both languages
(English, Persian).
