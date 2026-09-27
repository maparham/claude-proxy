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

[docs/team-setup.md](docs/team-setup.md) is the short version to send to the team.

On their machine, in the shell profile or in `~/.claude/settings.json` under `"env"`:

```sh
export ANTHROPIC_BASE_URL=http://gateway.lan:8080
export ANTHROPIC_AUTH_TOKEN=sk-proxy-...        # their own gateway key
```

Or use `claude-gateway`. It needs no checkout of this repository; this installs it in `~/.local` (run it
again to update) and sets it up:

```sh
curl -fsSL https://raw.githubusercontent.com/maparham/claude-proxy/master/install.sh | sh -s -- on --url https://gateway.example.com --key sk-proxy-...
```

Without `--key`, `claude-gateway on --url …` authorizes in the browser instead: it opens the dashboard at a code,
and once you click **Authorize** there, the dashboard gives this computer a key of its own. With sign-up set up
(below), a teammate's whole setup is `curl -fsSL https://<dashboard>/install | sh`.

On Windows, `irm https://<dashboard>/install.ps1 | iex` in PowerShell does the same with no Python, Git or admin
rights (`install.ps1`, `scripts/windows`). It sets up gclaude only (`gclaude.cmd`, with the limits statusline, the
80% warning and `/usage`); global mode, OpenCode and sharing `~/.claude` with gclaude are
[not on Windows yet](https://github.com/maparham/claude-proxy/issues/22).

By default `claude-gateway on` sets up **gclaude** (below): a second command that runs Claude Code through the
gateway while `claude` keeps this machine's own login. For a machine where plain `claude` itself should use the
gateway, global mode edits `~/.claude/settings.json` and switches back cleanly. From a checkout,
`scripts/claude-gateway` is the same command:

```sh
claude-gateway on --global --url https://gateway.example.com --key sk-proxy-...   # first time
claude-gateway off                # back to this machine's own login
claude-gateway on                 # later: reuses the saved URL and key (and stays in global mode)
claude-gateway status             # on/off, reachability, current usage
```

That's all. They don't need a local `/login`. Plan bars and `/usage` aren't available in this
mode, so `claude-gateway on` also installs a statusline with their limits (unless they already have
one; then it says how to add them to it). Setting it up by hand instead:

```json
"env": { "CLAUDE_GATEWAY_DASHBOARD": "http://gateway.lan:8081" },
"statusLine": { "type": "command", "command": "/path/to/scripts/statusline.sh", "refreshInterval": 30 }
```

It prints each of their limits, e.g. `maya · daily $61/$100 61% (resets in 3.2 h) · 5h 30% (resets in 2.1 h)`: a
used/limit figure gets its percentage, rounded down, and each limit says when it resets (a usage limit when its window
ends, a share limit with the account's bucket). It is
cyan with a leading `◆`, and a figure turns yellow at 80% and red at 100%. They can also sign in to the dashboard with their key and
see only their own data.

`claude-gateway on` also adds a prompt hook (`statusline.sh --warn`): when any of those figures reaches 80%,
Claude Code shows the status line as a warning before the prompt is sent, again every 15 minutes, and at once
at 100%. It never blocks a prompt; the gateway is still the only place limits are enforced.

### Sign-up and browser authorization

People can create their own account and connect their computers without the admin sending keys
(`docs/superpowers/specs/2026-09-26-signup-and-browser-authorization-design.md`):

- **Sign-up**: the dashboard's sign-in is [Clerk](https://clerk.com) (Google, GitHub or an emailed code). A first
  sign-in links an existing user whose name or email is that address; otherwise, with `signup.enabled`, it creates
  one with a one-time credit (`cost_total`, `signup.credit_usd`). Clerk only proves who someone is; the dashboard
  then uses its own session, and requests never touch Clerk.
- **Computers**: `curl -fsSL https://<dashboard>/install | sh` (Windows: `irm https://<dashboard>/install.ps1 | iex`) installs claude-gateway and runs
  `claude-gateway on`, which shows a code and opens `…/dashboard#authorize/<code>`. **Authorize** there gives that
  computer a key of its own (the `keys` table), listed and removable under **Your computers**.
- **Upgrade**: limits apply together, so the admin's **Upgrade** swaps the credit for a daily allowance.
- **Shared cap**: `signup.free_daily_cap_usd` limits what all accounts still on their credit spend together in any
  24 hours, so many sign-ups can't use up the subscription even if each stays within its own credit.

```toml
[listener]
public_url = "https://claude.example.com"            # where people reach the gateway
dashboard_url = "https://claude-dash.example.com"    # and the dashboard

[signup]
enabled = true
credit_usd = 5.0
free_daily_cap_usd = 20.0                            # all credit accounts together, any 24 h; 0: no cap
clerk_publishable_key = "pk_live_..."                # CLERK_SECRET_KEY goes in the environment (gateway.env)
```

In Clerk: email code, Google and GitHub sign-in, bot protection, and blocking of email sub-addresses and
disposable domains. A production instance also needs its DNS records (DNS only, not proxied, in Cloudflare) and
its own Google and GitHub OAuth apps.

### gclaude: the gateway beside your own `claude`

To keep `claude` on this machine's own login and reach the gateway with a second command, use gclaude, which
is what `claude-gateway on` sets up unless the machine is already in global mode:

```sh
claude-gateway on --url https://gateway.example.com --key sk-proxy-...   # first time (or on --gclaude)
gclaude                            # Claude Code through the gateway; any claude flag works (gclaude -p "...")
claude-gateway off                 # off --gclaude when global mode is set up too
```

This leaves `~/.claude` alone and installs `~/.local/bin/gclaude`, which runs Claude Code with
`CLAUDE_CONFIG_DIR=~/.config/claude-gateway/claude`. That folder gets the key-only setup above (key, statusline,
limit warning; the statusline shows your limits and then your own statusline from `~/.claude`, if you have one,
through `statusline.sh --then`), and shares the rest of your own setup in `~/.claude`, refreshed each time gclaude starts
(`scripts/gclaude-sync.py`):

- `CLAUDE.md`, `agents`, `skills` and each of your `commands`;
- plugins: installed once for both, so installing, updating or removing one in gclaude does it for plain
  `claude` too (and `off --gclaude` doesn't undo that). gclaude follows the changes you make to plain `claude`'s
  list of enabled plugins; a plugin you turn on or off in gclaude stays that way until you change the same one
  in plain `claude`;
- MCP servers: the user-scope servers in `~/.claude.json` (`claude mcp add --scope user`), followed the same way
  as the enabled plugins: a server added, changed or removed in plain `claude` shows up in gclaude at its next
  start, and one you add or change in gclaude stays gclaude's. Project-scope servers (`.mcp.json`) work in both
  already;
- memory: each project's `memory` folder, once plain `claude` has been used in that project, so a memory saved in
  either shows up in both. A gclaude memory folder that already holds memories of its own is left alone;
- sessions: gclaude's `/resume` (and `gclaude --resume <id>`) lists your plain `claude` sessions too. Each is a
  hard link to the same file (the picker skips symbolic links), so a session you resume in gclaude goes on in
  plain `claude`'s history as well, with its new requests going through the gateway. Its rewind checkpoints are
  shared too, so `/rewind` in gclaude can restore the files it changed. Sessions started in gclaude stay
  gclaude's, and a session plain `claude` cleans up stays in gclaude until gclaude's own cleanup.

Settings, login and prompt history stay separate, so both commands can run at once in the same terminal.
The gateway's `ANTHROPIC_BASE_URL` and key live only in the `"env"` block of gclaude's `settings.json`, which
Claude Code applies to its own process, and `CLAUDE_CONFIG_DIR` is set only for the `claude` that gclaude starts.
Nothing is exported to your shell, so plain `claude` still reads `~/.claude` and uses this machine's login.
`off --gclaude` removes the command, the links, `/usage`, `/account`, `/logout` and the gateway settings, and keeps gclaude's own history.

Claude Code's own `/usage` can't see the gateway: with a gateway key it shows only the session's cost and tokens.
In gclaude, `/usage` is a command of ours instead. The limit-warning hook stops that prompt before it reaches the
model and shows a fresh gateway line, e.g. `Gateway: maya · daily $61/$100 61% · details: https://…/dashboard`,
so it costs nothing. Plain `claude` keeps the real `/usage`.
`/account` shows who your key belongs to, which key it is and a link to the dashboard, e.g.
`Account: maya · maya@example.com · user · key sk-proxy-ab1… for MacBook, authorized 2026-09-20 · dashboard: https://…/dashboard`.
A small Haiku request repeats that line, since a hook's reply reads as an error in Claude Code. When a limit is
reached or the gateway is down, so that request would fail, the hook answers `/account` itself.
Claude Code's own `/logout` only clears its claude.ai login, which gclaude doesn't use. gclaude's `/logout` signs
this computer out of the gateway instead, answered by the hook: it revokes the key when it is this computer's own
(one authorized in the browser; your first key may be in use elsewhere, so it stays valid) and removes it from
gclaude's `settings.json` and `client.json`. gclaude then won't start until `claude-gateway on --login`.

**Users never see the subscription.** To anyone but an admin, their own limits are all there is: the
dashboard, the statusline and the proxy's responses carry no account quota, no credential state and
no Anthropic rate-limit headers. A share limit (`share_5h 20`) shows to them as their own "5h limit",
0-100% used; an upstream quota 429 reads "Usage limit reached"; a missing or expired login is a plain
503 "the gateway can't serve Claude requests right now". Admins see all of it, including
`plan 5h 61% (yours 20%) · week 38%` on their statusline.

### Muse as a subagent

Copy `examples/muse-worker.md` to `~/.claude/agents/` (or a project's `.claude/agents/`). Its
`model: muse-spark` is sent to the gateway, which routes it to Meta with `META_API_KEY` and rewrites
it to `muse-spark-1.3`. The Claude credential is never sent on that route. Claude Code prints a
notice that it doesn't know `muse-spark`'s context window and assumes 200k tokens. Set
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` if you want it to use more.

### OpenCode for Muse and other third-party models

Anthropic accepts a subscription login only from Claude.ai and Claude Code, so OpenCode can't use Claude
through the gateway. It can use the gateway's other routes. The admin issues a second key that works only for
them:

```sh
claude-proxy user routes-key maya          # or Users & limits → OpenCode key; --remove deletes it
```

On maya's machine:

```sh
scripts/claude-gateway on --opencode --url https://gateway.example.com --routes-key sk-proxy-r-...
scripts/claude-gateway on --opencode       # later: picks up new routes
scripts/claude-gateway off --opencode
```

This adds a `gateway` provider to `~/.config/opencode/opencode.json`, with each route model and the context
window set in the route's `model_info`, and a `muse` subagent in `~/.config/opencode/agents/`. The key stays in
`~/.config/claude-gateway/routes.key`. The gateway refuses it for Claude models and for the dashboard (it can
read `/api/me/status`), with a 403 that says to use the Claude Code key. Limits, metering and the dashboard
count it as maya's. Share limits don't apply to third-party models, so Muse still works after maya's share of
the subscription is used up. OpenCode sends a session header, so its requests are grouped into sessions in the
Sessions tab, just without titles, and its own title-generation request is billed to the person like any other.

## Limits

Set in the dashboard (Users & limits → Limits) or the CLI:

```sh
claude-proxy limit set maya tokens_daily 2000000              # weighted tokens, per 24 h window
claude-proxy limit set omid requests_minute 20
claude-proxy limit set sara share_5h 30                        # ≤ 30 points of the account's 5-hour bucket (estimated)
claude-proxy limit set sara requests_daily 100 --scope 'claude-opus-*'
claude-proxy limit set omid cost_monthly 50                    # USD, API-equivalent estimate
claude-proxy limit set guest allowed_models 'claude-sonnet-*,muse-spark'
claude-proxy limit list
```

| Kind | Unit | Window |
|---|---|---|
| `requests_minute`, `requests_daily`, `requests_monthly` | count | 60 s, 24 h, 30 d |
| `tokens_minute`, `tokens_5h`, `tokens_daily`, `tokens_weekly`, `tokens_monthly` | `weighted` (default) or `raw` | 60 s, 5 h, 24 h, 7 d, 30 d |
| `cost_daily`, `cost_monthly` | USD | 24 h, 30 d |
| `share_5h`, `share_7d` | percentage points of the account bucket | Anthropic's current window |
| `allowed_models` | comma-separated globs | — |

A usage limit's window opens with the user's first request, like Claude's own 5-hour limit. When it ends, the count goes
back to zero, and the next request opens a new window. A user over a limit waits until their window resets.

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
- If the grant is revoked or expires, Claude requests get a 503 (an admin's says to run
  `claude-proxy login`; a user's says only that the gateway can't serve Claude right now), and the
  admin's dashboard shows a red banner. A running server picks up
  the new login without a restart.
- Prompts and responses are never stored, only metadata and token counts. The one exception is the
  short title Claude Code generates for each session, kept so the Sessions tab can show sessions by
  name; the admin and the session's owner can see it. Requests, quota reports and session titles
  older than `retention_days` (default 180) are deleted.
- To remove someone, **Revoke** them: their key stops working at once and their usage stays in the
  history. **Delete** (on a revoked user, in the dashboard or `claude-proxy user delete <name>`) also
  removes their recorded usage from totals and charts. Revoking also stops the person's OpenCode key.
- Admin actions from the dashboard and the CLI go to the audit log.
- `python scripts/demo_data.py demo.db` fills a database with synthetic traffic, for trying the dashboard.

## Development

```sh
pip install -e . pytest pytest-asyncio
pytest
```

CI runs every test on Linux before each deploy. The `client scripts` workflow adds the client side where people run
it: Windows (`tests/test_windows_client.py` under Windows PowerShell 5.1) and macOS (the installer and
`claude-gateway` under the Mac's own bash 3.2), and installs from GitHub itself on all three, the way the
one-liners do. The one-liners the dashboard serves are tested end to end against a stand-in dashboard.

`e2e/run.py` goes further, as the `e2e` workflow on Linux (the Docker image the deploy ships), macOS and Windows: a
gateway starts in the job, a clean user account runs the dashboard's one-liner, a real browser (Playwright) signs
in and clicks **Authorize**, and real Claude Code runs `gclaude -p` and must answer "pong". With the
`E2E_GATEWAY_KEY` secret, a key of the production user `ci-e2e` (limit $1 a day), the job's gateway sends that
request on to claude.rahkar.pro, so Claude itself answers; without it, `e2e/fake_anthropic.py` does. Locally:

```sh
uv run --with playwright python -m playwright install chromium   # once
uv run --with playwright python e2e/run.py [--docker]             # needs claude on PATH; touches only a temp folder
```

Only Clerk's own sign-in (Google, GitHub, an emailed code) is left to a person.
