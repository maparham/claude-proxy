# Full project review — 2026-09-27

Branch `gclaude-update` (includes uncommitted client-script changes). Six parallel reviewers, one per scope; the top server findings, the flaky Clerk test and the deploy health gate were re-verified by hand against the source.

Test run: `uv run pytest -q` → 414 passed, 26 skipped (all Windows-only), 0 failed, 102 s.

## Cross-cutting priorities

| # | Sev | Where | Defect |
|---|-----|-------|--------|
| 1 | high | `app.py:176,271` | Limits only see *recorded* requests, and a request is recorded when its stream ends. N parallel streams all pass `cost_total`, `free_credit_cap`, `requests_minute`. With public self-service sign-up this is an unbounded bypass of every limit kind. |
| 2 | high | `app.py:47,152,171` | Full-scope keys may POST any `/v1/*`. Model extraction, `allowed_models`, routing and metering only inspect top-level `model` on `/v1/messages` and `count_tokens`. `/v1/messages/batches` (model nested in `requests[].params`) skips the allow-list and is metered at 0 tokens; batch results are never metered. |
| 3 | high | `tests/test_signup.py:100` | `nbf = now+120` is computed at collection; the test runs ~100 s later with `LEEWAY_S = 10`. About 10 s of margin before it flakes on a slower CI box. Build claims inside the test or pass `now=`. |
| 4 | high | `cli.py` | 14 of 16 admin subcommands (`init`, `login`, `status`, `serve`, `user *`, `limit *`, retention) have no tests. |
| 5 | med | `deploy/lightsail/claude-gateway-deploy:74-79` | `healthy()` discards the `/health` curl result; the gate is really `claude-proxy status` which is DB-only. A new image whose listener never binds is declared healthy. |
| 6 | med | `.github/workflows/e2e.yml:13,34` | A production gateway key (`E2E_GATEWAY_KEY`) is exposed to `pull_request` runs that execute the PR's own `e2e/run.py`/`seed.py`. One `print(os.environ[...])` on a branch leaks it to a public log. |
| 7 | med | all workflows | Actions pinned to mutable major tags, no `permissions:` block, `curl … \| bash` of third-party installers with a persisted `GITHUB_TOKEN`. |
| 8 | med | `web.py:702`, `db.py:291` | "Rotate key" only replaces `users.key_hash`; machine keys and the routes key survive, so a leak "closed" by rotation stays open via any machine key the attacker minted. |
| 9 | med | `web.py:666`, `db.py:268` | A machine key can `POST /api/keys/{id}/remove` any sibling key of the same user, so a leaked computer key can revoke the owner's other computers. |
| 10 | med | `web.py:153,584` | `client_ip = request.client.host`; behind a reverse proxy every visitor shares one IP, so the login/device rate limiter locks everyone out and the "not this browser's address" check never fires. |
| 11 | med | `scripts/claude-gateway:571` + `web.py:823` | `gclaude update` pipes the dashboard's `/install` to `sh`, which runs a bare `on` (not `on --gclaude`): can block on browser auth mid-update or refresh global mode instead of gclaude. The real chain is untested (installer stubbed with `echo`). |
| 12 | med | `scripts/claude-gateway:66`, `statusline.sh:51` | Gateway key passed in `python3` argv; visible to other local users via `ps`. |
| 13 | med | `scripts/windows/claude-gateway.ps1:308` | `gclaude update`'s `\|\|` failure branch is dead: `install.ps1` never exits non-zero, so a failed update still runs `claude update` and looks successful. |
| 14 | med | `scripts/windows/statusline.ps1:58-62` | Logout catch clobbers the real HTTP status with 0 when the 200 body is empty/non-JSON, reporting "could not be reached" after a successful revoke. |
| 15 | med | `scripts/windows/claude-gateway.ps1:64` | `icacls` result discarded; on FAT/exFAT/shared volumes `client.json` and `settings.json` stay readable with no warning. |
| 16 | med | `limits.py:124-147` | `_window_start` opens a window only on rows with a model, but `_window_state` then sums all rows. Model-less traffic never gets `requests_daily`; `/v1/models` calls count against `requests_minute`. |
| 17 | med | `app.py:205`, `credentials.py:218` | Any upstream 401 forces a refresh, spending the single-use refresh token; repeated scope-rejected paths can mark `needs_login` and take the gateway down. |
| 18 | med | `meter.py:144-183` | One unparsable SSE `data:` line stops all further metering, so the final `output_tokens` is never read and the request is undercounted. |
| 19 | med | `app.py:150` | `await request.body()` has no size cap; an authenticated client can OOM the box. |
| 20 | med | `config.example.toml`, `README.md` | Docs lag the code: `cost_total` limit kind, `limit clear`, `[signup]`, `CLAUDE_PROXY_ADMIN_PASSWORD` and the other `CLAUDE_PROXY_*` env vars undocumented; example config claims "defaults" but omits several keys. |

## Per-scope detail

### Server request path (app, forwarder, limits, quota, meter, credentials)
- high: items 1, 2 above.
- med: items 16–19.
- low: `app.py:230-248` exception between `send()` and `StreamingResponse` leaks `resp` and skips the ledger row; `app.py:269` `record()` runs after `aclose()` so an aclose error loses accounting; `cost_total` is computed over `requests` which retention deletes, so credit silently regains after `retention_days`; `limits.py:91` accepts `nan`/`inf` limits that are silently inert; `forwarder.py:39` forwards `cookie` upstream; `app.py:186` 503 leaks the env var name to routes-only keys.
- Assessment: auth, credential swap, header filtering and refresh single-flight are solid and tested. Accounting scope is the exposure.

### Web UI, DB, identity (web, db, credentials, clerk, static)
- med: items 8–10.
- low/med: `web.py:183` `/api/login/key` accepts an admin's first key and issues a full admin session, making the laptop token password-equivalent.
- low: legacy sessions with `csrf_token=''` pass the CSRF check for ≤7 days after upgrade; `Secure` cookie flag defaults off regardless of https dashboard; Clerk session token not bound one-time so it can be exchanged repeatedly within its ~60 s validity; `clerk.py:109` unguarded `r.json()` → 500 instead of 503; check-then-insert without transaction in admin `create_user` and Clerk account link; `app.js:414` `data-v` unescaped (only upstream header names, no practical XSS); `credentials.py:238` operator-precedence bug `(A and B) or C` can mark `needs_login` on a 429 mentioning `invalid_grant`.
- Assessment: coherent auth model, parameterised SQL, argon2, RS256-only JWT with full claim checks, HttpOnly+Strict cookies, thorough `esc()`. Gaps are lifecycle (rotate, sibling revoke) and reverse-proxy IP assumptions.

### Unix client (scripts/claude-gateway, statusline.sh, gclaude-sync.py, install.sh)
- med: items 11, 12; `off --global` leaves the key in `settings.json.bak-claude-gateway`; `/logout` revokes the shared key without regard to global mode, breaking plain `claude` on dual-mode machines.
- low/med: `save_client` persists a new `--key` before `preflight` validates it.
- low: launcher signed-out check is presence-only; `install.sh` `trap EXIT` never fires after `exec`, leaking the tarball dir; predictable `/tmp` cache files written with `>` (symlink follow); `curl` of `/install` has no `--max-time` and runs over whatever scheme the dashboard uses; revoke-before-remove with narrow exception handling; sync stderr suppressed; `install.sh` has no main-function wrapper so a truncated download runs a prefix (after `rm -rf "$share"`).
- Assessment: quoting, BSD/GNU portability and curl handling sound; the diffed logout/launcher logic works and its happy/local-failure paths are tested. The `gclaude update` chain and credential hygiene are the gaps.

### Windows client (claude-gateway.ps1, statusline.ps1, install.ps1)
- med: items 13–15.
- low/med: signed-out marker written before token removal, so a JSON failure locks the user out while reporting "nothing removed".
- low: non-atomic in-place rewrites of settings/client JSON; `Usage` `Select-Object -First 17` not bumped after 3 added header lines (help truncates mid-sentence); IDN dashboard URL in gclaude.cmd yields a misleading error; marker-file vs token-presence divergence from unix; no ACL on the config dir; tests stub the installer and cover only `revoked:true`.
- Divergence from unix: marker-file signed-out detection; `/install.ps1` never fails so `||` is dead; missing global mode, OpenCode, sync, `--then`, dir ACL (documented, issue #22); different `status` output; no health check.
- Assessment: diff is PS 5.1-safe and functionally sound. Silent-failure paths are the problem.

### Deploy, CI, e2e
- med: items 5–7; `uv.lock` ignored, base image undigested, image built with `pip` while CI tests with `uv` so tested and shipped resolutions differ.
- low: backup pruning keeps 5 lexicographically-last `*.db`, and documented `before-restore-*` names sort after every date stamp so after 5 restores fresh backups are deleted immediately; `update.sh` has no backup/rollback and ignores the health loop result; compose has `restart` but no `healthcheck`; deploy gates only on `test`, not e2e/client-scripts; `install-backup-key.sh` writes the repo secret before the key is shown or installed; `build:` + `image:` in compose lets `--build` overwrite the rollback image.
- Assessment: careful design (forced SSH commands, tag verification, SQLite backup API, age-encrypted off-box copies). Gaps: blind health gate, floating supply chain, prod key reachable from PR runs.

### Tests and docs
- high: items 3, 4.
- med: `POST /api/logout`, `POST /api/admin/limits/delete`, `db.cleanup` untested; item 20; 25 of 26 Windows tests skip off-Windows.
- low: six near-identical `create_user+seed_oauth+make_gateway` fixtures and three stub HTTP servers duplicated across test modules; `test_user_detail.py` imports a fixture from another test module; ~95 of 102 s are subprocess-driven client tests with no marker for a fast inner loop; `team-setup.md:49` says "installer below" but it is above; README never mentions `--config`/`CLAUDE_PROXY_CONFIG`.
- Untested: `cli.py` (14 subcommands), `web.py` logout and limits/delete, `db.cleanup`, `credentials.check_key/token_record`; indirect only: `forwarder.strip_account_headers`, `meter.parse_non_streaming`, `usage.grouped`, `limits.user_view/human/free_credit_spend`.
- Assessment: honest suite (real fakes, real subprocesses, no mocks, fixed clocks). Admin CLI and retention are essentially untested; server-side docs lag the code.

## Suggested order of work
1. In-flight accounting (item 1): reserve a row or in-memory counter at request start, release/finalize on completion.
2. Restrict full-scope keys to paths the gateway can meter, or extract model from batch bodies and meter batch results (item 2).
3. Fix the flaky `nbf` test and add CLI tests (items 3, 4).
4. Deploy health gate + workflow hardening (`permissions:`, SHA pins, keep the prod key out of `pull_request`) (items 5–7).
5. Key lifecycle: rotate revokes machine keys; machine keys cannot remove siblings; honor `X-Forwarded-For` behind the proxy (items 8–10).
6. Client fixes: `gclaude update` chain on both platforms, key out of argv, Windows logout status clobber and `icacls` check (items 11–15).
7. Limits window/model-less rows, refresh-on-401 guard, meter resilience, body size cap (items 16–19).
8. Docs sync (item 20).
