# Windows Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `irm https://claude-dash.rahkar.pro/install.ps1 | iex` installs claude-gateway on a stock Windows PC, authorizes it in the browser and sets up gclaude.

**Architecture:** The client is native PowerShell 5.1: `install.ps1`, `scripts/windows/claude-gateway.ps1` and `scripts/windows/statusline.ps1`. It talks to the existing dashboard device-flow and status endpoints. The dashboard gains `GET /install.ps1` and shows the Windows command. The Windows tests run only on a `windows-latest` CI runner.

**Tech Stack:** Windows PowerShell 5.1, FastAPI, pytest, and GitHub Actions `windows-latest`.

**Spec:** `docs/superpowers/specs/2026-09-27-windows-client-design.md`

## Global Constraints

- PowerShell 5.1 only. None of these may be used:
  - `-AsHashtable`, `-SkipHttpErrorCheck`, `$IsWindows`;
  - `?:`, `??`, `&&`, `||` in PowerShell code.
- The installer never calls `exit`. It runs inside a function, and errors print a message and `return`.
- JSON is written with `[IO.File]::WriteAllText($p, $s, (New-Object Text.UTF8Encoding $false))`, and every `ConvertTo-Json` gets `-Depth 20`.
- Installed scripts run only as `powershell -NoProfile -ExecutionPolicy Bypass -File "<path>"`. Inside settings.json the path uses forward slashes.
- The Windows command handles unsupported flags as follows:
  - `--global`, `--own-login`, `--key-only`, `--opencode` and `--routes-key` fail with `Not available on Windows yet; see https://github.com/maparham/claude-proxy/issues/22`.
  - `--gclaude` is accepted.
- `install.sh`, `scripts/claude-gateway` and `scripts/statusline.sh` are not changed.
- Work goes in the worktree `/Users/mahmoudparham/projects/claude_proxy-windows` on branch `windows-installer`. Commit only these paths.

## Review Focus

1. **A pending poll is a 400.** `Invoke-RestMethod` throws on it, so the poll loop must read the body from the exception and keep waiting. This is tested by a stub answering pending twice before approving.
2. **Existing gclaude settings survive.** `settings.json` in gclaude's dir may already have unrelated keys and hooks; `on` and `off` must keep them. This is tested with a pre-seeded file.
3. **No BOM, and deep nesting kept.** Hooks nest 4 levels deep. This is tested by parsing with Python `json.loads` on the raw bytes.
4. **`irm | iex` must not close the window.** No `exit` may appear in install.ps1 outside the child process. This is tested by grepping install.ps1 for `\bexit\b`.
5. **A foreign `gclaude.cmd` / `claude-gateway.cmd` is never overwritten.** This is tested for both.

---

### Task 1: The dashboard serves `/install.ps1` and shows it

**Files:**
- Modify: `src/claude_proxy/config.py` (SignupConfig)
- Modify: `src/claude_proxy/web.py` (`install_command`, the `/install` neighbourhood, and `/api/session`)
- Modify: `src/claude_proxy/static/app.js` (`machinesCard` and its call site, `S.install_windows`)
- Test: `tests/test_signup.py`

**Interfaces:**
- Produces:
  - `SignupConfig.installer_ps1_url: str`
  - `GET /install.ps1` → text/plain
  - `/api/session` → `install_windows: str | None`

- [ ] **Step 1: Write failing tests** (in `tests/test_signup.py`, beside the existing `/install` tests)
  - `/install.ps1` answers 200 `text/plain`, with `Invoke-RestMethod '<installer_ps1_url>'`, `on --url 'https://claude.example'` and `--dashboard 'https://dash.example'`, and `Cache-Control: no-cache`.
  - A URL containing `'` is doubled.
  - It answers 503 when `public_url` is unset.
  - `/api/session` has `install_windows == "irm https://dash.example/install.ps1 | iex"`, and `None` when the URLs are unset.
- [ ] **Step 2:** Run `uv run pytest tests/test_signup.py -q -k install` and check that the new tests fail.
- [ ] **Step 3: Implement.**

  ```python
  def ps_quote(s: str) -> str:
      return "'" + s.replace("'", "''") + "'"

  @app.get("/install.ps1")
  async def install_ps1():
      need_urls()
      dash = cfg.listener.dashboard_url.rstrip("/")
      script = (f"# Installs claude-gateway and connects this computer to {dash}: it opens the browser to authorize it,\n"
                "# then sets up gclaude.\n"
                "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor "
                "[Net.SecurityProtocolType]::Tls12\n"
                f"& ([scriptblock]::Create((Invoke-RestMethod {ps_quote(cfg.signup.installer_ps1_url)}))) on "
                f"--url {ps_quote(cfg.listener.public_url.rstrip('/'))} --dashboard {ps_quote(dash)}\n")
      return PlainTextResponse(script, headers={"Cache-Control": "no-cache"})
  ```

  - Add `install_windows` beside `install`.
  - In `machinesCard`, show the two labelled copy rows.
- [ ] **Step 4:** Run `uv run pytest -q` and check that everything passes.
- [ ] **Step 5:** Commit: "Dashboard: /install.ps1 and the Windows command".

### Task 2: `install.ps1`, the Windows test harness, and CI

**Files:**
- Create: `install.ps1`
- Create: `scripts/windows/claude-gateway.ps1`. At this stage it is a stub that prints `claude-gateway (Windows)` for `help`. Task 3 fills it in.
- Create: `tests/test_windows_client.py`
- Create: `.github/workflows/windows-client.yml`

**Interfaces:**
- Produces, for the tests:
  - a fixture `win(tmp_path)` that gives `run_ps(script, *args, env=..., stdin=...)`;
  - a `USERPROFILE`/`TEMP` sandbox;
  - `source_zip` (a zip of the checkout under a top-level `claude-proxy-test/` folder);
  - `installed` (a fixture that has run install.ps1 into the sandbox).
- The stub `Dashboard` from `tests/test_device_cli.py`, subclassed to answer `GET /api/me/status?format=text` with a settable line.

- [ ] **Step 1: Write the tests.** They are skipped unless `sys.platform == "win32"`, except for the grep test, which runs everywhere.
  - Install puts these in place:
    - `share\scripts\windows\claude-gateway.ps1`;
    - `bin\claude-gateway.cmd`, with the marker, and running `claude-gateway.cmd help` prints `claude-gateway (Windows)`.
  - A foreign `claude-gateway.cmd` is left unchanged, and the output says so.
  - Everywhere: `install.ps1` has no `exit` statement (regex `(?m)^\s*exit\b|[;{]\s*exit\b`).
- [ ] **Step 2: Write `install.ps1`** as in spec §1. `CLAUDE_GATEWAY_ZIP` may be a local path, in which case it is copied rather than downloaded.
- [ ] **Step 3: Add the workflow.**

  ```yaml
  name: windows-client
  on:
    pull_request:
      paths: [install.ps1, "scripts/windows/**", tests/test_windows_client.py, tests/test_device_cli.py, src/claude_proxy/web.py, .github/workflows/windows-client.yml]
    push:
      branches: [master]
      paths: [install.ps1, "scripts/windows/**", tests/test_windows_client.py, .github/workflows/windows-client.yml]
    workflow_dispatch:
  jobs:
    windows:
      runs-on: windows-latest
      steps:
        - uses: actions/checkout@v4
        - uses: astral-sh/setup-uv@v6
        - run: uv run --python 3.12 pytest -q tests/test_windows_client.py tests/test_signup.py
  ```

- [ ] **Step 4:** Push the branch, open a draft PR, and check that the Windows job runs the install tests and they pass.
- [ ] **Step 5:** Commit: "Windows: install.ps1 and its tests on a Windows runner".

### Task 3: `claude-gateway.ps1` on / off / status

**Files:**
- Modify: `scripts/windows/claude-gateway.ps1`
- Test: `tests/test_windows_client.py`

**Interfaces:**
- Consumes:
  - the device endpoints: `/api/device/start` → `{device_code, user_code, verification_uri_complete, interval, expires_in}`, and `/api/device/token` → 200 `{key, user}` or 400 `{error}`;
  - `/v1/models`.
- Produces the files named in spec §2, with the command strings that Task 4's statusline must match:
  - `<S> = powershell -NoProfile -ExecutionPolicy Bypass -File "<client dir, forward slashes>/statusline.ps1"`;
  - the hook is `<S> --warn`.

- [ ] **Step 1: Write the tests.**
  - `on` via the browser, with tokens `[pending, pending, 200 {key: "sk-proxy-new", user: "ana"}]`:
    - the exit code is 0;
    - `client.json` holds `{url, key: "sk-proxy-new", dashboard}`;
    - the start body is `{"label": COMPUTERNAME}`;
    - `settings.json` bytes don't start with a BOM and parse;
    - its env holds the three keys;
    - `statusLine.command` ends with `statusline.ps1"`;
    - `hooks.UserPromptSubmit[-1].hooks[0].command` ends with `--warn`;
    - `disableClaudeAiConnectors` is true;
    - `commands\usage.md` holds the marker;
    - `.claude.json` has `hasCompletedOnboarding`;
    - `gclaude.cmd` holds the marker.
  - `on --url U --key K`, with `settings.json` pre-seeded with `{"model": "opus", "hooks": {"Stop": [...]}, "statusLine": {"type": "command", "command": "mine"}}`:
    - `model`, `Stop` and the user's statusLine are kept, and the output mentions that the statusline was left alone;
    - `off` then gives back exactly the pre-seeded object.
  - `access_denied`: the exit code is non-zero, the output says "Cancelled in the browser", and neither `client.json` nor `settings.json` is created.
  - A 401 from `/v1/models` gives a non-zero exit, and settings.json is unchanged.
  - With a fake `claude.cmd` (`@echo CONFIG=%CLAUDE_CONFIG_DIR%`) first on PATH, `cmd /c gclaude` prints gclaude's dir.
  - `off` removes `gclaude.cmd`, `usage.md` and the env keys, and keeps `.claude.json`.
  - A foreign `gclaude.cmd` makes `on` fail and stay unchanged.
  - `--global` fails with the issue #22 link.
  - `status` prints the url and "gclaude: installed".
- [ ] **Step 2: Implement** as in spec §2. The key helper is:

  ```powershell
  function Invoke-Json($Method, $Uri, $Body, $Headers) {   # -> @{code=int; data=object}; never throws on an HTTP status
    try {
      $r = Invoke-WebRequest -UseBasicParsing -Method $Method -Uri $Uri -Headers $Headers -TimeoutSec 15 `
           -ContentType 'application/json' -Body $(if ($Body) { $Body | ConvertTo-Json -Depth 20 -Compress })
      $code = [int]$r.StatusCode; $text = $r.Content
    } catch [System.Net.WebException] {
      if (-not $_.Exception.Response) { throw }   # no answer at all: the caller treats it as a network error
      $code = [int]$_.Exception.Response.StatusCode
      $text = (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())).ReadToEnd()
    }
    $data = $null; try { $data = $text | ConvertFrom-Json } catch {}
    @{ code = $code; data = $data }
  }
  ```

- [ ] **Step 3:** Push, check that the Windows job passes, and fix until it is green.
- [ ] **Step 4:** Commit: "Windows: claude-gateway on/off/status for gclaude".

### Task 4: `statusline.ps1`

**Files:**
- Create: `scripts/windows/statusline.ps1`
- Test: `tests/test_windows_client.py`

- [ ] **Step 1: Write the tests.** The stub status line is `daily $85/$100 · 5h 10%`, with env `CLAUDE_GATEWAY_DASHBOARD`, `ANTHROPIC_AUTH_TOKEN` and `CLAUDE_CONFIG_DIR` (gclaude's dir, after `on`).
  - With no args, stdin `{}`: the output contains `◆`, `$85/$100 85%` and the yellow escape `\x1b[33m`, and the stub saw `Authorization: Bearer <key>`.
  - With the dashboard down: the output contains `gateway status unavailable`, and the exit code is 0.
  - `--warn` with stdin `{"prompt":"hi"}`:
    - the first run prints JSON with `systemMessage` starting `Gateway: `;
    - an immediate second run prints nothing.
  - `--warn` with stdin `{"prompt":"/usage"}` prints JSON `decision == "block"`, and `reason` contains `/dashboard`.
  - At 50%, `--warn` prints nothing.
- [ ] **Step 2: Implement** as in spec §3. It ports `figures()` and `peak()` from `scripts/statusline.sh` using `[regex]::Matches` with the same pattern: ``'\$?[0-9][0-9,.]*[KM]?/\$?[0-9][0-9,.]*[KM]?%?|[0-9]+(\.[0-9]+)?%'``.
- [ ] **Step 3:** Push, and check that the Windows job is green.
- [ ] **Step 4:** Commit: "Windows: statusline, limit warning and /usage for gclaude".

### Task 5: Docs, review, merge, live check

- [ ] Add a Windows line to the README ("Sign-up and browser authorization" section and client section) and to `docs/team-setup.md`.
- [ ] Run a whole-branch review on the most capable model, and fix what it finds.
- [ ] Mark the PR ready, merge it after CI is green (the Ubuntu tests and deploy, and the Windows job), and check that the deploy is healthy.
- [ ] **Live check:**
  - `curl https://claude-dash.rahkar.pro/install.ps1` shows the expected text.
  - Run the Windows workflow by `workflow_dispatch` once more on master.
