# Client side: Claude Code wrapper and OpenCode on third-party routes

- **Date:** 2026-09-25
- **Status:** Implemented (branch opencode-routes)
- **Builds on:** `2026-09-21-claude-proxy-design.md` (sections 4, 9, 11 and 17). This spec lifts one of its non-goals, "serving non-Claude-Code clients", for third-party routes only. `claude` is still never run as a subprocess.

## 1. Purpose

The question was whether to build our own client CLI to replace Claude Code. The reasons given were:

1. the things Claude Code loses when it runs against the gateway (`/usage`, plan bars, claude.ai connectors);
2. first-class use of Muse and other non-Claude models;
3. limits and quota visible, and acted on, before a request is refused with a 429.

Owning the agent loop was not a goal. All three are served without writing an agent: Claude Code stays the client for Claude, `scripts/claude-gateway` does more of the setup around it, and the open-source OpenCode client is supported for third-party routes.

### Goals

1. A person can use OpenCode through the gateway for every configured third-party route (today `muse-spark*` on Meta), with the same per-person limits, metering and dashboard as Claude Code.
2. OpenCode requests can never reach the subscription credential, whatever model or path they ask for.
3. OpenCode knows each route model's real context window, instead of the 200k guess Claude Code makes for Muse.
4. Claude Code warns the person when a limit is close, before the gateway starts refusing.
5. Onboarding for both clients stays one `claude-gateway on` command.

### Non-goals

- Our own agent CLI, or one built on the Claude Agent SDK.
- Claude models in OpenCode. Press reports from 2026 say Anthropic began blocking third-party clients that used subscription OAuth in January, changed its terms in February, and from 4 April 2026 accepts subscription OAuth only from Claude.ai and Claude Code ([VentureBeat](https://venturebeat.com/technology/anthropic-cuts-off-the-ability-to-use-claude-subscriptions-with-openclaw-and), [timeline](https://decodethefuture.org/en/anthropic-blocks-third-party-tools/)). We have not tested the block ourselves; the terms alone rule the route out. Serving Claude to OpenCode on an Anthropic API key needs a second Claude backend chosen per key; that is deferred to its own design.
- Making any client appear to be Claude Code.
- Restoring `/usage` and plan bars in key-only mode (section 7).
- claude.ai connectors and Claude in Chrome for gateway users. They need the person's own claude.ai login; the existing own-login mode covers a machine that has one.
- Choosing a model automatically from remaining budget.
- Session names in the Sessions tab for OpenCode traffic (section 8).

## 2. Routes-only keys

Each person may hold a second gateway key whose scope is `routes`. Their existing key has scope `full` and is unchanged.

### 2.1 Data model

Two nullable columns on `users`, and a unique index:

| Change | Meaning |
|---|---|
| `routes_key_hash TEXT` | SHA-256 of the routes-only key, like `key_hash` |
| `routes_key_prefix TEXT` | first 14 characters, for display |
| `CREATE UNIQUE INDEX IF NOT EXISTS idx_users_routes_key ON users(routes_key_hash)` | one user per key; SQLite allows many NULLs in a unique index |

SQLite cannot add a `UNIQUE` column with `ALTER TABLE`, so the columns go in the `users` definition in `SCHEMA` for new databases, `_migrate` adds them to older ones with plain `ADD COLUMN`, and the index is created in both cases.

Routes-only keys start `sk-proxy-r-` so people can tell the two apart. The prefix is for humans only; scope comes from which column the hash matched, never from the key text.

`find_user_by_key` matches either column and returns a `dict` copy of the row with an added `key_scope` of `"full"` or `"routes"`. `authenticate` returns that dict. Every caller already reads the user by column name (`user["id"]`, `user["role"]`, `user.keys()`), so no caller changes shape. Every other rule in `authenticate` (revoked, disabled, `sk-ant-` tokens) applies to both keys. `find_session` (dashboard cookie) returns the same kind of dict with `key_scope="full"`.

### 2.2 Lifecycle

- `claude-proxy user routes-key <name>` issues or replaces the routes-only key and prints it once. `--remove` deletes it.
- The dashboard's Users & limits page gets the same two actions per user.
- Revoke and Delete act on the user, so both keys stop working together. Rotating the full key leaves the routes-only key alone, and the reverse.
- Issue, replace and remove are written to the audit log.

### 2.3 Enforcement on the proxy listener

The check sits in one function, `scope_allows(key_scope, method, path, route) -> bool`, so the rule is testable on its own. For a routes-only key it allows exactly two things and refuses everything else:

- `POST /v1/messages` or `POST /v1/messages/count_tokens` whose model matches a route;
- `GET /v1/models`, which the gateway answers itself from the route models (section 3.2). Anthropic is not called.

In `handle` (`src/claude_proxy/app.py`) it runs **immediately after `route` is resolved**: before the 400 for an unreadable `model`, before `limits.evaluate`, and so before `Upstream.headers()` ever loads the subscription credential. Running it before limits matters: otherwise a routes-only key asking for a Claude model could be refused by a Claude-scoped limit, get a 429 instead of a 403, and be recorded as a limit hit.

A refusal is `403 permission_error`, recorded with `rejected_by="key_scope"`, with the message:
`This key is for third-party models only (muse-spark*). Claude models need your Claude Code key.` The pattern list comes from the configured routes.

Limits and metering treat an allowed routes-only request like any other routed request from that person. Two existing behaviours already keep route traffic apart from the subscription, and stay as they are:

- quota attribution counts only `provider='anthropic'` requests (`quota.py`), so Muse traffic never takes a share of a rise in subscription utilization;
- `limits.evaluate` skips `share_5h`/`share_7d` for routed models, so a person past their subscription share can still fall back to Muse. Token, request and cost limits, and `allowed_models`, still apply.

Consequence of the last point: if the admin sets `allowed_models = claude-*` for a person, their routes-only key can reach nothing. That is consistent (the admin chose which models they may use), and the 403 from `allowed_models` says so.

### 2.4 Dashboard

- `principal` (every cookie- or key-authenticated dashboard endpoint) refuses a `key_scope="routes"` user with 403, except on `GET /api/me/status`, so OpenCode users can read their limits.
- `/api/login/key` calls `authenticate` directly rather than `principal`, so it gets its own check: a routes-only key returns 403 `This key only works for third-party models; sign in with your Claude Code key.` It is not counted as a failed login attempt, since the key is valid.
- An admin who holds a routes-only key sees the account quota block in `/api/me/status`, as they do with their full key. That is the admin's own data; no new exposure to non-admins.
- **Errors panel:** `ERROR_KIND` in `web.py` maps any `rejected_by` other than `auth` and `request` to `gateway_limit`. It gains `WHEN rejected_by = 'key_scope' THEN 'gateway_key_scope'`, so refusals are not counted as limit hits. The dashboard's error-kind labels and tooltips get an entry for `gateway_key_scope` ("a routes-only key asked for a model or path it can't use"). `limits.USER_KINDS` needs no entry: it only renames share limits, and `web.py` falls back to the raw name.

## 3. Route model metadata

### 3.1 Configuration

`[[routes]]` gains an optional `model_info` table, keyed by the model name clients send:

```toml
[[routes]]
name = "meta"
# ...
model_info = { "muse-spark" = { context = 1000000, output = 64000, display_name = "Muse Spark" } }
```

`context` and `output` are token counts, supplied by the admin; the gateway does not discover them. Give both or neither (OpenCode's `limit` needs both); `display_name` is optional. The shipped default for `muse-spark` is `context = 1048576`, the 1M window listed for `muse-spark-1.3` in Promptfoo's Meta provider docs and on OpenRouter, and `output = 32000`, a conservative cap: Meta has not published an output limit for 1.3.

### 3.2 `GET /v1/models`

Routes match by glob (`muse-spark*`), which can't be enumerated. The **listed** route models are the union of each route's `model_map` keys and `model_info` keys. A model that matches a route's glob but appears in neither is still routed and usable, just not listed; the admin lists it by adding a `model_info` entry.

For a full key, the existing behaviour stays: Anthropic's list with the listed route models appended (`_models_with_routes`). Each appended route model now also carries `max_input_tokens` and `max_tokens` when `model_info` gives them.

For a routes-only key, the response is built locally from the listed route models only, in the same shape.

## 4. OpenCode set-up in `claude-gateway`

```sh
claude-gateway on --opencode --routes-key sk-proxy-r-...   # first time
claude-gateway on --opencode                              # later
claude-gateway off --opencode
```

`--opencode` makes `on` and `off` act on OpenCode only; the Claude Code set-up is a separate `claude-gateway on`. A person using both runs both. `on --opencode`:

1. Preflights `GET <url>/v1/models` with the routes-only key in `x-api-key` (as OpenCode sends it) and fails without changing anything unless it returns 200.
2. Reads `~/.config/opencode/opencode.json` and fails without changing anything if it is not plain JSON. It never edits `opencode.jsonc`: when only that file exists, it creates `opencode.json` beside it, and OpenCode merges the two (verified in plan task 1).
3. Saves the routes-only key in `~/.config/claude-gateway/routes.key` (mode 600, no trailing newline) and records `opencode` in `client.json`.
4. Adds a `gateway` provider to `opencode.json`, taking models and limits from the preflight response:

   ```json
   "provider": {
     "gateway": {
       "npm": "@ai-sdk/anthropic",
       "name": "Claude gateway",
       "options": { "baseURL": "<url>/v1", "apiKey": "{file:/Users/maya/.config/claude-gateway/routes.key}" },
       "models": { "muse-spark": { "name": "Muse Spark", "limit": { "context": 1000000, "output": 64000 } } }
     }
   }
   ```

   The key is referenced by absolute path, not copied, so `opencode.json` holds no secret and no `~` expansion is needed. A route model without `model_info` gets no `limit` entry (OpenCode then uses its own default) rather than zeros. `@ai-sdk/anthropic` is the package OpenCode's built-in Anthropic provider uses; OpenCode's custom-provider docs only show OpenAI-format packages, so plan task 1 verifies it (section 8).
5. Installs `examples/opencode/muse.md` as `~/.config/opencode/agents/muse.md`, unless a file of that name exists. This is a new file written in OpenCode's agent format, not a copy of `examples/muse-worker.md`: frontmatter with `description`, `mode: subagent`, `model: gateway/muse-spark` and a `tools:` map of booleans, and no `name:` field (OpenCode takes the name from the file name). The body is the same instructions as the Claude Code agent.
6. Records in `client.json` exactly what it added. If `opencode.json` already has a `gateway` provider that it did not add, it stops without changing anything.

`off --opencode` removes the `gateway` provider and the agent file only if `on` added them (and the agent file is unchanged since), deletes `routes.key`, and removes the `opencode` record from `client.json`. It leaves the rest of `opencode.json` alone, the same way `off` already treats `settings.json`, and deletes `opencode.json` only if `on` created it and nothing else was added since. The file is rewritten as 2-space JSON, so a file already in that format comes back byte-identical and any other file comes back with the same content.

Re-running `on --opencode` refreshes the model list, so a new route appears after one command.

`claude-gateway status` also reports whether the OpenCode provider is installed.

## 5. Warning before a limit

`claude-gateway on` (Claude Code set-up) also installs a `UserPromptSubmit` hook in `~/.claude/settings.json`, under the same rules as the statusline: added only if absent, recorded in `client.json`, removed exactly by `off`.

The hook is a new mode of `scripts/statusline.sh` (`statusline.sh --warn`), so there is one script and one cache:

- It uses the statusline's cached text line (the plain `format=text` output, 30-second cache). When the cache is stale it refreshes it with the same 3-second timeout, so a prompt can be delayed by up to 3 seconds about once every 30 seconds of use. The gateway sees no extra traffic.
- It applies the statusline's existing threshold rule to that line: every percentage and every used/limit pair is a figure. The awk that colours the line is moved into a function both modes call, which also reports the highest figure.
- It warns when the highest figure is at 80% or more and either it has entered a higher band than at the last warning (80% → 100%), or 15 minutes have passed since the last warning. The band and time of the last warning are kept in a small file next to the cache.
- On a warning it prints exactly one line of JSON, `{"systemMessage": "Gateway: <status line>"}`, which Claude Code shows to the person. The prompt is not blocked; the gateway remains the only place a limit is enforced.
- **Stdout stays clean.** On `UserPromptSubmit`, anything else on stdout would be added to the model's context. In `--warn` mode the script sends every diagnostic to stderr, replaces the `CLAUDE_GATEWAY_DASHBOARD` `:?` guard with a silent exit, keeps `curl` output out of stdout, and exits 0 on every path that does not warn (below 80%, gateway down, variable unset, key missing).

## 6. Error handling

| Situation | Result |
|---|---|
| Routes-only key asks for a Claude model, an unrouted path, or sends no readable model | 403 as in 2.3, recorded as `key_scope`, nothing sent upstream, limits not consulted |
| Routes-only key on a dashboard endpoint other than `/api/me/status` | 403 |
| Routes-only key on `/api/login/key` | 403 with the message in 2.4, not counted as a failed attempt |
| Route's provider key unset on the gateway | existing 503 `gateway_route_unconfigured` |
| `claude-gateway on --opencode` preflight fails | exits 1, prints the HTTP status and body, changes nothing |
| `opencode.json` is not valid JSON | exits 1 without writing |
| Warning hook cannot reach the gateway, or is misconfigured | nothing on stdout, exit 0 |

## 7. Probe result: `/usage` and plan bars

In the Claude Code 2.1.282 binary, the `/api/oauth/usage` fetch sends the client's OAuth credentials and returns early behind a check that appears to require a claude.ai login. If that holds, Claude Code in key-only mode never asks for usage, and there is nothing the gateway could answer. The statusline and the warning hook are the substitute. Not confirmed on 2026-09-25 with Claude Code 2.1.282: a `--debug`, `CLAUDE_CONFIG_DIR`-isolated session driven through a pty never reached `/usage` in three attempts. All three stopped at the first-run "Do you trust the files in this folder?" prompt, whose default-highlighted option is "No, exit". The first two attempts sent a blind Enter there, selecting "No, exit". The third attempt sent an explicit down-arrow before Enter to select "Yes, I trust this folder", but the down-arrow arrived while the preceding "Use Claude Code's terminal setup?" screen was still on screen (it moved that screen's own selection instead of the trust prompt's), so the trust prompt was still showing its "No, exit" default when Enter confirmed it. Claude Code exited immediately each time (debug log: `Skipping SessionEnd:other hook execution - workspace trust not accepted`); the chat interface and `/usage` were never reached, and the recording upstream logged no requests in any attempt. The claim above stands only on the binary read, not on a live run. A future attempt would need to wait for the trust prompt to actually be the frontmost screen (e.g. by polling the tty output for its text) before sending the down-arrow, or pre-accept trust in the isolated `.claude.json` instead of navigating the prompt.

## 8. Known gaps

- OpenCode sends its own `x-session-id` header, which the gateway's forwarder already recognises as a session id (`SESSION_HEADERS` in `forwarder.py`), so its requests are grouped into sessions in the Sessions tab. They show up without titles, though, because the gateway only recognises Claude Code's own title request (`titles.py`), not OpenCode's differently-shaped one. OpenCode makes that title-generation call itself, as a separate `/v1/messages` request, and it is billed to the person like any other request.
- Confirmed on 2026-09-25 with OpenCode 1.18.30 against a recording stand-in for the gateway: an `opencode.json` is loaded alongside an existing `opencode.jsonc` in the same `XDG_CONFIG_HOME` directory rather than being ignored (`opencode models gateway` listed `gateway/muse-spark`; the `.jsonc` held only `$schema`, so this shows the `.json` is picked up, not the full shape of the merge); a custom provider with `npm: "@ai-sdk/anthropic"` is accepted and used (`opencode run -m gateway/muse-spark` completed and posted to `/v1/messages`); `{file:<absolute path>}` in `options.apiKey` resolved to the exact key content, sent as `x-api-key: sk-proxy-r-probe` (no `{file:` text, no trailing newline, no `authorization` header) — `~` expansion in `{file:}` was not exercised, though `claude-gateway` is only observed to write absolute paths there. The same run also showed `x-session-affinity` sent alongside `x-session-id` on every request, and the title-generation call recorded in the first bullet above.

## 9. Testing

Unit and app tests (pytest, existing fixtures):

1. `scope_allows`: every combination of scope, method, `ROUTED_PATHS` / `/v1/models` / other path, and route present or absent.
2. Routes-only key, `POST /v1/messages` with `claude-sonnet-5`: 403, `rejected_by="key_scope"`, and the fake upstream and credential backend are never called.
3. Routes-only key, `claude-sonnet-5`, while the person is over a Claude-scoped limit: still 403 `key_scope`, not 429.
4. Routes-only key, `POST /v1/messages/count_tokens` with a Claude model: 403, no upstream call.
5. Routes-only key, `GET /v1/models`: only listed route models (`model_map` ∪ `model_info` keys), with `max_input_tokens`/`max_tokens`; Anthropic not called.
6. Routes-only key, `GET /v1/foo`: 403.
7. Routes-only key, `muse-spark`: forwarded to the route with the route's key, metered, counted toward that person's limits.
8. Full key behaviour unchanged (existing suite passes).
9. Revoking the user stops both keys; rotating one key leaves the other working.
10. Routes-only key: `/api/me/status` works; `/api/login/key` returns 403 without counting a failure; an admin endpoint returns 403.
11. Migration adds the two columns and the unique index to a database created by the previous schema; two users cannot share a routes-only key hash.
12. A person past their `share_5h` limit gets a 429 for Claude on the full key but can still use `muse-spark` on the routes-only key.
13. The Errors panel query reports a `key_scope` rejection as `gateway_key_scope`, not `gateway_limit`.

Script tests (bash, against a temporary `HOME` and a stub gateway):

14. `on --opencode` then `off --opencode` leaves a pre-existing 2-space `opencode.json` byte-identical, keeps edits made in between, and removes `routes.key`.
15. A route model without `model_info` produces a model entry with no `limit`.
16. `statusline.sh --warn`: a `systemMessage` at 80%; nothing at 79%; nothing within 15 minutes of a warning at the same band; a new warning on crossing to 100%; nothing on stdout and exit 0 when the gateway is down or `CLAUDE_GATEWAY_DASHBOARD` is unset.

Live check before merging: OpenCode against the local gateway with a routes-only key completes a Muse request, and a Claude model request is refused with the 403 message.

## 10. Delivery order

1. Verify OpenCode's key header, `@ai-sdk/anthropic` in a custom provider, and `{file:}` substitution against a recording gateway; confirm the section 7 finding.
2. Routes-only keys: schema and index, `authenticate`, `scope_allows`, `principal` and `/api/login/key` checks, `ERROR_KIND`, CLI, audit, tests 1-13.
3. `model_info` and the `/v1/models` changes.
4. Dashboard actions for issuing and removing routes-only keys, and the `gateway_key_scope` label.
5. `claude-gateway --opencode`, the OpenCode Muse agent file, script tests 14-15.
6. Warning hook, script test 16.
7. README: onboarding an OpenCode user.
