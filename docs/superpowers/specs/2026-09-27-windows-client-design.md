# Windows client: `irm … | iex` sets up gclaude

**Status:** proposed · 2026-09-27
**Scope chosen:** gclaude only, native PowerShell. Global mode, OpenCode and sharing `~/.claude` (CLAUDE.md, agents, skills, plugins, MCP servers, memory) wait in backlog issue #22.

## Problem

Sign-up tells every new user to run `curl -fsSL https://claude-dash.rahkar.pro/install | sh`. That needs a POSIX shell, curl, tar and python3. A fresh Windows PC has PowerShell 5.1 and nothing else: Claude Code runs natively on Windows and no longer needs Git Bash, and Python is not installed by default. So Windows users can't set up at all today, short of WSL.

## Goal

On a stock Windows 10/11 machine with Claude Code installed natively, one line in PowerShell:

```powershell
irm https://claude-dash.rahkar.pro/install.ps1 | iex
```

installs claude-gateway, opens the dashboard to authorize the computer (the same device flow as on macOS/Linux), and sets up `gclaude`, which runs Claude Code through the gateway with its own settings and history, the limits statusline, the 80% warning and `/usage`. Plain `claude` stays on the machine's own login. It needs no admin rights, Python, Git or execution-policy change.

## Constraints

- Windows PowerShell 5.1 is the floor (what ships with Windows). Nothing may rely on PowerShell 7 features (`-AsHashtable`, `-SkipHttpErrorCheck`, `$IsWindows`, ternaries, `??`).
- `irm | iex` runs in the user's own PowerShell session: the installer never calls `exit`, and it sets no state beyond `$env:Path` for that session.
- Installed `.ps1` files run only via `powershell -NoProfile -ExecutionPolicy Bypass -File "<path>"`, because the default execution policy blocks scripts.
- JSON is written as UTF-8 without a BOM, via `[IO.File]::WriteAllText(path, text, (New-Object Text.UTF8Encoding $false))`, and `ConvertTo-Json` always gets `-Depth 20`.
- HTTPS calls first add TLS 1.2 to `[Net.ServicePointManager]::SecurityProtocol`.
- Any non-2xx HTTP status makes `Invoke-RestMethod` throw, and the device token endpoint answers "pending" with 400. The client catches the exception and reads the JSON body from the response stream.
- The macOS/Linux client (`install.sh`, `scripts/claude-gateway`, `statusline.sh`) is not changed.

## Files

| Path | What |
|---|---|
| `install.ps1` | The installer. |
| `scripts/windows/claude-gateway.ps1` | `on` / `off` / `status` for gclaude. |
| `scripts/windows/statusline.ps1` | The statusline, the `--warn` hook and `/usage` in gclaude. |
| `src/claude_proxy/web.py` | `GET /install.ps1` and `install_windows` in `/api/session`. |
| `src/claude_proxy/config.py` | `signup.installer_ps1_url`. |
| `src/claude_proxy/static/app.js` | The Computers card shows both commands. |
| `tests/test_signup.py` | Tests for `/install.ps1` and the session field. |
| `tests/test_windows_client.py` | End-to-end tests with a stub dashboard. They are skipped off Windows. |
| `.github/workflows/windows-client.yml` | Runs those tests on `windows-latest` under `powershell.exe` 5.1. |
| `README.md`, `docs/team-setup.md` | The Windows line. |

## 1. `install.ps1`

The installer works in two ways:
- `irm https://raw.githubusercontent.com/maparham/claude-proxy/master/install.ps1 | iex` installs or updates only.
- `& ([scriptblock]::Create((irm <that URL>))) on --url … --dashboard …` installs, then runs `claude-gateway on …`.

Everything runs inside a function; an error prints a message and returns.

1. Enable TLS 1.2.
2. Get the source zip: `CLAUDE_GATEWAY_ZIP` (a local path or a URL), else `https://codeload.github.com/<CLAUDE_GATEWAY_REPO or maparham/claude-proxy>/zip/<CLAUDE_GATEWAY_REF or master>`. Unpack it with `Expand-Archive` into a temporary folder and check that it holds `scripts/windows/claude-gateway.ps1`.
3. Copy `scripts/windows/*` into `%USERPROFILE%\.local\share\claude-gateway.new\scripts\windows`, then swap that folder for `…\claude-gateway`. As in `install.sh`, a failed download never leaves half an install.
4. Write `claude-gateway.cmd` into the bin folder: `CLAUDE_GATEWAY_BIN`, else `%USERPROFILE%\.local\bin`, the folder where Claude Code's native installer already puts `claude.exe`. It runs `powershell -NoProfile -ExecutionPolicy Bypass -File "<share>\scripts\windows\claude-gateway.ps1" %*` and keeps the exit code. The file carries a marker line. If a `claude-gateway.cmd` without that marker exists, the installer stops and leaves it alone.
5. If the bin folder is not in the user's PATH, add it: `[Environment]::SetEnvironmentVariable('Path', …, 'User')` and `$env:Path` for this session. It says so. This is skipped when `CLAUDE_GATEWAY_BIN` is set.
6. With arguments, run the installed `claude-gateway.ps1` with them in a child `powershell` process, so its `exit` can't close the window. Otherwise print the next step.

## 2. `claude-gateway.ps1`

Its commands and flags are spelled as in the Unix script:

```
claude-gateway on [--url URL] [--key KEY] [--dashboard URL] [--login]
claude-gateway off
claude-gateway status
```

- `--global`, `--own-login`, `--key-only`, `--opencode`, `--routes-key` and `--gclaude` are handled as follows:
  - `--gclaude` is accepted.
  - The others fail with "Not available on Windows yet; see https://github.com/maparham/claude-proxy/issues/22".

**Paths.** These are the same as on Unix, under `%USERPROFILE%`. Each has the same override:

| What | Default | Override |
|---|---|---|
| client file | `.config\claude-gateway\client.json` | `CLAUDE_GATEWAY_CLIENT` |
| gclaude's config dir | `.config\claude-gateway\claude` | `CLAUDE_GATEWAY_GCLAUDE_DIR` |
| launcher | `.local\bin\gclaude.cmd` | `CLAUDE_GATEWAY_BIN` |
| own Claude Code dir | `.claude` | `CLAUDE_SETTINGS` (its settings.json) |

**URL, key and dashboard.** These follow the Unix rules:
- `--url` and `--key` replace the saved ones.
- `--key` alone reuses the saved URL.
- The dashboard is `--dashboard`, else the one saved for that URL, else the URL with `://claude.` replaced by `://claude-dash.`.
- With no key given, and none saved for that URL, or with `--login`, it authorizes in the browser.

**Authorizing in the browser.** It uses the same contract as the Unix script (`POST /api/device/start` with label `$env:COMPUTERNAME`, then poll `POST /api/device/token`):
- It prints the link and the code.
- It opens the link with `Start-Process` unless `SSH_CONNECTION` is set. `CLAUDE_GATEWAY_OPEN` names another program to open it with; `none` opens nothing.
- Polling respects `interval`:
  - `slow_down` adds 2 s.
  - 5xx, 429 and network errors are retried until the code expires.
  - `access_denied`, `expired_token` and anything else end with the same messages as the Unix script.
- A 404 on start adds the `--dashboard` hint.

**Pre-flight.** `GET <url>/v1/models` with `Authorization: Bearer <key>` and `anthropic-version: 2023-06-01` must answer 200. Otherwise nothing is changed and it exits 1.

**`client.json`** holds `url`, `key` and `dashboard`, plus the `gclaude` record of what `on` added. Its ACL is reset to the current user only (`icacls <file> /inheritance:r /grant:r "<DOMAIN\user>:F"`), the Windows form of mode 600. `settings.json` in gclaude's dir gets the same.

**`on` for gclaude.** It writes, recording what it added so `off` removes exactly that:

1. **statusline copy.** `statusline.ps1` is copied next to `client.json`, so moving or updating the share folder doesn't break it.
2. **gclaude's `settings.json`.** It is created if missing. The existing JSON is read into a PSCustomObject and the fields below are set with `Add-Member -Force`; every other field stays as it was:
   - `env.ANTHROPIC_BASE_URL`, `env.ANTHROPIC_AUTH_TOKEN`, `env.CLAUDE_GATEWAY_DASHBOARD`.
   - `disableClaudeAiConnectors: true`, only if it was not set (recorded as `added_disable_connectors`).
   - `statusLine: {type: command, command: <S>, refreshInterval: 30}`, only if there is none (recorded as `added_statusline`). `<S>` is `powershell -NoProfile -ExecutionPolicy Bypass -File "C:/Users/…/statusline.ps1"`. The forward slashes and double quotes make the same text work whether Claude Code runs it through cmd, PowerShell or Git Bash.
   - A `UserPromptSubmit` group `{hooks: [{type: command, command: "<S> --warn", timeout: 10}]}`, unless ours is there already (recorded as `added_warn_hook`).
3. **`commands\usage.md`.** It has the same text and marker as on Unix, and is written only when absent or already ours.
4. **`.claude.json` in gclaude's dir.** When absent, it is seeded with `{"hasCompletedOnboarding": true}` plus `theme` from `%USERPROFILE%\.claude.json`.
5. **`gclaude.cmd`.** It has a marker line. It fails with a clear message and exit code 127 if `claude` isn't on PATH. It then runs `setlocal`, sets `CLAUDE_CONFIG_DIR` to gclaude's dir, and runs `claude %*`. An existing `gclaude.cmd` without the marker is left alone, and `on` stops.

**`off`** does the following:
- It removes the env keys, and each recorded item that is still ours.
- It removes our `usage.md` (and `commands\` if that leaves it empty) and our `gclaude.cmd`.
- It removes the `gclaude` record.
- It keeps gclaude's history and sessions, and says where they are, the same wording as on Unix.
- The URL and key stay in `client.json`, as on Unix.

**`status`** prints:
- The URL, the dashboard, and whether gclaude is installed.
- The gateway's line from `/api/me/status?format=text`, or why that failed.

## 3. `statusline.ps1`

It ports `statusline.sh` without `--then` (gclaude's settings start empty, so there's no user's statusline to chain):

- **Start-up.** It sets `[Console]::OutputEncoding` and `InputEncoding` to UTF-8, so the `◆` and ANSI colours survive, then reads stdin fully.
- **Status line.** It fetches `<CLAUDE_GATEWAY_DASHBOARD>/api/me/status?format=text` with `Authorization: Bearer $env:ANTHROPIC_AUTH_TOKEN` and a 3 s timeout.
  - The line is cached for 30 s in `%TEMP%\claude-gateway-status.txt`.
  - Figures are rendered as in `statusline.sh`: each used/limit pair gets its floored percentage, cyan with a leading `◆`, yellow from 80% and red from 100%.
  - It prints `gateway status unavailable` when the fetch fails.
- **`--warn`.** This is the `UserPromptSubmit` hook.
  - For a `/usage` prompt, it prints `{"decision":"block","reason":"Gateway: … · details: <dash>/dashboard"}` with a fresh fetch. This happens only where gclaude's `usage.md` is ours.
  - Otherwise, when the highest figure is ≥ 80, it prints `{"systemMessage":"Gateway: …"}`. The band (80 or 100) and the 15-minute repeat are kept in `%TEMP%\claude-gateway-status.txt.warned`.
  - Nothing else ever goes to stdout.
- **Errors.** Any error is swallowed: a statusline or hook must never break the session.

## 4. Dashboard

- `GET /install.ps1` answers `text/plain; charset=utf-8` with `Cache-Control: no-cache`, and 503 as `/install` does when the URLs aren't set. Its body is:
  ```powershell
  # Installs claude-gateway and connects this computer to <dashboard>: it opens the browser to authorize it, then sets up gclaude.
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
  & ([scriptblock]::Create((Invoke-RestMethod '<installer_ps1_url>'))) on --url '<public_url>' --dashboard '<dashboard_url>'
  ```
  Values are single-quoted with `'` doubled.
- `SignupConfig.installer_ps1_url` defaults to `https://raw.githubusercontent.com/maparham/claude-proxy/master/install.ps1`.
- `/api/session` gains `install_windows: "irm <dashboard>/install.ps1 | iex"`. `install` is unchanged.
- **Computers card.** It shows two copy rows, "macOS / Linux" and "Windows (PowerShell)".

## 5. Testing

- **On any OS** (`tests/test_signup.py`):
  - the `/install.ps1` body (URLs, quoting, the 503 case);
  - `install_windows` in the session.
- **On Windows** (`tests/test_windows_client.py`), using the stub dashboard from `tests/test_device_cli.py`, extended with `/api/me/status`. Every test runs `powershell.exe` 5.1 with a temporary `USERPROFILE` and `TEMP`, and the source zip built from the checkout. The tests cover:
  - Install from the local zip:
    - the files and `claude-gateway.cmd` are in place;
    - a foreign `claude-gateway.cmd` is left alone.
  - `on` authorizing in the browser, with `CLAUDE_GATEWAY_OPEN=none`:
    - `client.json` has url, key and dashboard;
    - `settings.json` has no BOM and holds the env, `statusLine` and the hook;
    - `usage.md`, `.claude.json` and `gclaude.cmd` are written.
  - `on --key` with pre-existing unrelated settings, which stay as they were.
  - Denied and expired codes change nothing.
  - A fake `claude.cmd` on PATH echoes `CLAUDE_CONFIG_DIR`, and running `gclaude` shows gclaude's dir.
  - The statusline renders the figures. `--warn` warns at 85% once, then stays quiet, and blocks `/usage`.
  - `off` undoes everything `on` added and keeps foreign settings.
  - Unsupported flags fail with the issue link.
- **CI.** `.github/workflows/windows-client.yml` runs them on pull requests and on pushes to master that touch the Windows files, the installer, the stub or the workflow.
- **Live check after merge.** A last run of the Windows job installs from the real `https://claude-dash.rahkar.pro/install.ps1` text against the stub. The real authorize on a real Windows PC is the user's to try.

## Known limits

- Pressing Ctrl+C to leave `gclaude` may end with cmd's "Terminate batch job (Y/N)?", a property of `.cmd` launchers (npm's have it too).
- Starting PowerShell costs about 0.3–0.5 s per statusline refresh. The 30 s cache keeps the network out of that path.
- Global mode, OpenCode and sharing `~/.claude` items with gclaude are not on Windows yet (#22).
