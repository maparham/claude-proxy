# ChatGPT design summary (user-supplied, unverified)

Source: output from ChatGPT given an equivalent research prompt, pasted by the user on 2026-09-20.
Status: third-party model output. Contains NO citations. Treat every claim as an inference to be
cross-checked against the cited notes in this folder, not as a verified fact.

## Reference projects it names
- claude-code-limiter: "closest match for per-user quotas + admin dashboard"
- TeamClaude: "best reference for transparent Claude Code proxying, OAuth refresh, account quota monitoring"
- OCP: "multi-user virtual keys and centralized credential handling"
- Claude Monitor: "token/session analytics"
- Bifrost / LiteLLM / Kong AI Gateway: "patterns for users, teams, keys, budgets and enforcement"

## Recommended architecture
Claude Code clients -> HTTPS gateway -> per-user auth + policy engine -> transparent Anthropic proxy
-> shared Claude OAuth credential -> Anthropic. Transparent reverse proxy via ANTHROPIC_BASE_URL;
avoid CLI subprocess wrapping.

## Design points worth noting (all uncited)
- Store only a hash of each virtual key; never expose OAuth tokens downstream.
- Serialized / single-flight OAuth token refresh to avoid refresh-token races.
- Treat OAuth endpoints/headers as reverse-engineered and unstable.
- Metering fields: user, session ID, timestamp, model, input/output/cache-read/cache-write tokens,
  latency, upstream status. Parse SSE usage while forwarding; never buffer full responses.
- Poll the OAuth usage endpoint and/or inspect upstream rate-limit headers. Track account 5h and 7d
  utilization separately from locally observed per-user consumption.
- Explicitly label per-user share of account quota as an ESTIMATE ("estimated contribution to
  account usage"), never as an Anthropic-reported fact.
- Limits: daily tokens, weekly tokens, request/turn count, per-model, enable/disable/revoke,
  optional allocation of shared quota between users.
- Dashboard: FastAPI + SQLite, simple JS frontend, ECharts/Chart.js/Plotly. Show account quota and
  resets, tokens by user/day/model, requests and sessions, burn rate, estimated exhaustion, limits
  and remaining allowance, active sessions, 429s, leaderboard.
- Tables: users, requests, sessions, quota_snapshots, limits. Store raw events, derive aggregates.
- Preserve Claude Code upstream behaviour: headers, SSE, model names, body, beta/client headers,
  tool calls, long-lived connections.
- Optional statusline script hitting a gateway /user/status endpoint.
- Security: HTTPS, hashed keys, admin auth, rate limiting, audit log, no prompt/response logging by
  default, fail closed on invalid/revoked keys.
- Policy: consumer terms prohibit sharing; Anthropic has restricted third-party OAuth use.
- Principle: never conflate (1) Anthropic account quota, (2) observed per-user usage, (3) local
  per-user allocation.
- Suggested build order: transparent proxy slice -> usage extraction -> SQLite -> limits ->
  quota polling -> dashboard -> statusline.
