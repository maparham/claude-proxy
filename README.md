# claude-proxy

A small self-hosted gateway that lets a few people use the ordinary Claude Code CLI on their own
machines while sharing one Claude Pro/Max subscription. Every request is attributed to a person;
an admin sets per-person limits; a dashboard shows account-wide and per-person usage. Selected
models (Muse on Meta's Model API) can be routed to another provider with its own key.

Design: [`docs/superpowers/specs/2026-09-21-claude-proxy-design.md`](docs/superpowers/specs/2026-09-21-claude-proxy-design.md).

> **Terms of service.** Anthropic's Consumer Terms forbid making an account available to anyone
> else, and the Claude Code legal page forbids routing requests through Pro or Max credentials on
> behalf of other users, with enforcement "without prior notice". Running this with a subscription
> risks the account. The credential backend is pluggable so the same ledger, limits and dashboard
> can front an API key instead.

```
Claude Code (person A) ──┐                       ┌─► api.anthropic.com   (subscription OAuth grant)
Claude Code (person B) ──┼─► proxy :8080 ────────┤
Claude Code (person C) ──┘   key → person        └─► api.meta.ai          (META_API_KEY, muse-spark*)
                             limits, meter
                         dashboard :8081 (admin password, or each person's own key)
```

## Set up the gateway host

```sh
pip install .                                   # Python 3.12+
mkdir -m 700 ~/.claude-gateway
(umask 077; claude-proxy keygen > ~/.claude-gateway/gateway.key)   # encrypts the stored grant; required
export META_API_KEY=...                         # optional: enables the muse-spark route

claude-proxy init                               # database + admin dashboard password
claude-proxy login                              # link the subscription (see below)
claude-proxy user add maya                      # prints maya's gateway key once
claude-proxy serve                              # proxy on :8080, dashboard on :8081
```

When `~/.claude-gateway` exists, the gateway keeps its database there and reads `gateway.key` from
it, so no variables are needed. Elsewhere, set `CLAUDE_PROXY_DB` and `CLAUDE_PROXY_CREDENTIAL_KEY_FILE`
(or `CLAUDE_PROXY_CREDENTIAL_KEY`). `claude-proxy user passwd admin` changes the dashboard password.

`claude-proxy login` prints a claude.ai authorization URL. Open it in a browser signed in to the
account that owns the subscription, approve, and paste back the code the page shows (or the URL
you land on). To split this across two commands, run `claude-proxy login --print-url`, then
`claude-proxy login --code '<code>'` within 15 minutes. The gateway then holds **its own** grant and is its only refresher. It never reads
Claude Code's stored login. Refresh tokens are single-use, so don't reuse this grant anywhere
else.

Listeners bind to `127.0.0.1` by default. To serve a LAN, set `host`/`dashboard_host` in the
config; beyond a LAN, put Caddy in front (`Caddyfile.example`) and set `secure_cookies = true`.
Docker: `docker run -v gw:/data -e CLAUDE_PROXY_CREDENTIAL_KEY=... -p 8080:8080 -p 8081:8081 <image>`,
then run `init`/`login`/`user add` with `docker exec -it`.

## Onboard a person

On their machine, in the shell profile or in `~/.claude/settings.json` under `"env"`:

```sh
export ANTHROPIC_BASE_URL=http://gateway.lan:8080
export ANTHROPIC_AUTH_TOKEN=sk-proxy-...        # their own gateway key
```

Or use `scripts/claude-gateway`, which edits `~/.claude/settings.json` for them and switches back cleanly:

```sh
scripts/claude-gateway on --url https://gateway.example.com --key sk-proxy-...   # first time
scripts/claude-gateway off        # back to this machine's own login
scripts/claude-gateway on         # later: reuses the saved URL and key
scripts/claude-gateway status     # on/off, reachability, current usage
```

That's all. They don't need a local `/login`. Plan bars and `/usage` aren't available in this
mode, so for their allowance they can install the statusline:

```json
"env": { "CLAUDE_GATEWAY_DASHBOARD": "http://gateway.lan:8081" },
"statusLine": { "type": "command", "command": "/path/to/scripts/statusline.sh", "refreshInterval": 30 }
```

It prints e.g. `maya · tokens daily 42% · acct 5h 61% (you ~20) · 7d 38%`. They can also sign in
to the dashboard with their key and see only their own data.

### Muse as a subagent

Copy `examples/muse-worker.md` to `~/.claude/agents/` (or a project's `.claude/agents/`). Its
`model: muse-spark` is sent to the gateway, which routes it to Meta with `META_API_KEY` and rewrites
it to `muse-spark-1.3`. The Claude credential is never sent on that route. Claude Code prints a
notice that it doesn't know `muse-spark`'s context window and assumes 200k tokens. Set
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` if you want it to use more.

## Limits

Set in the dashboard (Users & limits → Limits) or the CLI:

```sh
claude-proxy limit set maya tokens_daily 2000000              # weighted tokens, rolling 24 h
claude-proxy limit set omid requests_minute 20
claude-proxy limit set sara share_5h 30                        # ≤ 30 points of the account's 5-hour bucket (estimated)
claude-proxy limit set sara requests_daily 100 --scope 'claude-opus-*'
claude-proxy limit set omid cost_monthly 50                    # USD, API-equivalent estimate
claude-proxy limit set guest allowed_models 'claude-sonnet-*,muse-spark'
claude-proxy limit list
```

| Kind | Unit | Window |
|---|---|---|
| `requests_minute`, `requests_daily`, `requests_monthly` | count | 60 s, 24 h, 30 d rolling |
| `tokens_minute`, `tokens_5h`, `tokens_daily`, `tokens_weekly`, `tokens_monthly` | `weighted` (default) or `raw` | rolling |
| `cost_monthly` | USD | 30 d rolling |
| `share_5h`, `share_7d` | percentage points of the account bucket | Anthropic's current window |
| `allowed_models` | comma-separated globs | — |

- **Weighted tokens** are API-equivalent cost in units of a Claude Sonnet 5 input token. They count
  output, cache writes and cache reads at their list-price ratios and pricier models higher, so
  they track quota consumption much better than raw totals. Raw totals are dominated by cache reads.
- **Share limits** use each person's *estimated* share: every rise in the account's reported
  utilization is split across the people whose requests finished in that interval, by weighted
  tokens. If Anthropic hasn't reported for 30 minutes, share limits are skipped (token limits
  still apply) and the dashboard says so.
- Limits are checked from recorded history before each request. The request that crosses a limit
  is served, and the next one gets a 429 that Claude Code shows verbatim, for example `API Error:
  Request rejected (429) · Gateway limit requests_daily reached: 1 of 1 requests; retry in 24.0 h.`

## Operations

- `claude-proxy status` shows credential health, the latest account figures and the route keys.
- If the grant is revoked or expires, Claude requests get a 503 telling the person the admin must
  run `claude-proxy login`, and the dashboard shows a red banner. A running server picks up
  the new login without a restart.
- Prompts and responses are never stored, only metadata and token counts. Requests and quota
  reports older than `retention_days` (default 180) are deleted.
- To remove someone, **Revoke** them: their key stops working at once and their usage stays in the
  history. **Delete** (on a revoked user, in the dashboard or `claude-proxy user delete <name>`) also
  removes their recorded usage from totals and charts.
- Admin actions from the dashboard and the CLI go to the audit log.
- `python scripts/demo_data.py demo.db` fills a database with synthetic traffic, for trying the dashboard.

## Development

```sh
pip install -e . pytest pytest-asyncio
pytest
```
