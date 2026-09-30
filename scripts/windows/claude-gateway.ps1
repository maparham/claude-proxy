# claude-gateway (Windows): run Claude Code through a claude-proxy gateway as `gclaude`, beside plain `claude`.
#
#   claude-gateway on --url https://claude.example.com   # first time: authorize this computer in the browser
#   claude-gateway on --url https://claude.example.com --key sk-proxy-...   # or with a key from the admin
#   claude-gateway on                # later: refresh what it set up
#   claude-gateway on --login        # authorize this computer again, e.g. after it was removed in the dashboard
#   claude-gateway off               # remove gclaude (its history stays)
#   claude-gateway status
# gclaude users need none of these: `gclaude update`, `gclaude status` and `gclaude uninstall` run them.
#
# gclaude runs Claude Code with CLAUDE_CONFIG_DIR=%USERPROFILE%\.config\claude-gateway\claude, whose settings.json
# sends requests to the gateway with this computer's key, shows your gateway limits on the status line, warns at
# 80% of a limit, answers /usage with the gateway's figures and /account with your account and a dashboard link. Plain `claude` keeps this machine's own login.
# `gclaude update` updates claude-gateway from the dashboard (refreshing gclaude), then Claude Code itself.
# gclaude's /logout_gclaude signs this computer out: it revokes the key when it is this computer's own and removes it here;
# the next gclaude then signs this computer in again in the browser (as `on --login` does) before it starts, as it
# does when the dashboard answers 401 to the saved key (the computer was removed there).
# Without --key (and none saved for that URL), `on` opens the dashboard (--dashboard, else the URL with claude.
# replaced by claude-dash.) at a code; once you click Authorize there, the dashboard hands this computer a key of
# its own. Over SSH it only prints the link and the code. The URL and key are kept in
# %USERPROFILE%\.config\claude-gateway\client.json, readable by you alone. Start a new gclaude session after `on`.
# Global mode and OpenCode are not on Windows yet: https://github.com/maparham/claude-proxy/issues/22
# Installed by install.ps1; runs as powershell -ExecutionPolicy Bypass -File (see claude-gateway.cmd).

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
# Windows PowerShell 5.1 gives arrays an extra Count property that ConvertTo-Json writes out as {"value": [...], "Count": n}.
Remove-TypeData System.Array -ErrorAction SilentlyContinue

$HomeDir = $env:USERPROFILE
$Client = if ($env:CLAUDE_GATEWAY_CLIENT) { $env:CLAUDE_GATEWAY_CLIENT } else { Join-Path $HomeDir '.config\claude-gateway\client.json' }
$ClientDir = Split-Path -Parent $Client
$GDir = if ($env:CLAUDE_GATEWAY_GCLAUDE_DIR) { $env:CLAUDE_GATEWAY_GCLAUDE_DIR } else { Join-Path $ClientDir 'claude' }   # gclaude's CLAUDE_CONFIG_DIR
$Bin = if ($env:CLAUDE_GATEWAY_BIN) { $env:CLAUDE_GATEWAY_BIN } else { Join-Path $HomeDir '.local\bin' }
$Launcher = Join-Path $Bin 'gclaude.cmd'
$LauncherMark = 'rem Installed by claude-gateway on --gclaude.'
$UsageMark = '# Installed by claude-gateway on --gclaude.'   # the same as on macOS/Linux
$Settings = Join-Path $GDir 'settings.json'
$Statusline = Join-Path $ClientDir 'statusline.ps1'   # a copy, so the installed folder can be replaced
# How Claude Code runs it: forward slashes and double quotes read the same in cmd, PowerShell and Git Bash.
$LineCmd = 'powershell -NoProfile -ExecutionPolicy Bypass -File "' + ($Statusline -replace '\\', '/') + '"'
$WarnCmd = "$LineCmd --warn"
# Without a claude.ai login Claude Code gives fable, opus and sonnet a 200K context window; only their [1m] forms get 1M.
# So gclaude's aliases (and its /model picker) name those. The newest of each; update when one ships.
$OneMModels = [ordered]@{ ANTHROPIC_DEFAULT_FABLE_MODEL = 'claude-fable-5-1[1m]'; ANTHROPIC_DEFAULT_OPUS_MODEL = 'claude-opus-5-5[1m]'
                          ANTHROPIC_DEFAULT_SONNET_MODEL = 'claude-sonnet-5[1m]' }
$Issue = 'https://github.com/maparham/claude-proxy/issues/22'

function Say([string]$m) { [Console]::Error.WriteLine($m) }
function Fail([string]$m) { Say $m; exit 1 }

function Usage {   # the comment block at the top of this file, up to its first blank line
  Write-Output 'claude-gateway (Windows)'
  foreach ($line in @(Get-Content -LiteralPath $PSCommandPath | Select-Object -Skip 1)) {
    if ($line -notmatch '^#') { break }
    Write-Output ($line -replace '^# ?', '')
  }
}

# ---------- JSON files ----------

function Read-Json([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return $null }
  $text = [IO.File]::ReadAllText($Path)
  if (-not $text.Trim()) { return $null }
  return ($text | ConvertFrom-Json)
}

function Protect([string]$Path, [string]$Shown = $Path) {   # this user alone may read it: the Windows form of chmod 600
  # icacls can't on a FAT/exFAT drive or some shared folders; the file is already written, so warn rather than stop.
  $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  $ok = $false
  try { & icacls.exe $Path /inheritance:r /grant:r "${me}:F" | Out-Null; $ok = ($LASTEXITCODE -eq 0) } catch { }
  if (-not $ok) { Write-Warning "$Shown holds your gateway key but could not be made readable by you alone (icacls failed; is this a FAT/exFAT or shared drive?). Keep it where only you can read it." }
}

function Write-Json([string]$Path, $Object, [switch]$Private) {
  $dir = Split-Path -Parent $Path
  if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  $tmp = "$Path.tmp-claude-gateway"
  [IO.File]::WriteAllText($tmp, (ConvertTo-Json -InputObject $Object -Depth 20) + "`n", (New-Object Text.UTF8Encoding $false))
  if ($Private) { Protect $tmp $Path }
  Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Has($o, [string]$n) { return ($null -ne $o) -and ($o -is [psobject]) -and ($null -ne $o.PSObject.Properties[$n]) }
function Set-Field($o, [string]$n, $v) { $o | Add-Member -NotePropertyName $n -NotePropertyValue $v -Force }
function Remove-Field($o, [string]$n) { if (Has $o $n) { $o.PSObject.Properties.Remove($n) } }
function Field($o, [string]$n) { if (Has $o $n) { return $o.$n } return $null }
function Is-Empty($o) { return @($o.PSObject.Properties).Count -eq 0 }

function Cmd-Path([string]$Path) {
  # How a .cmd file should spell a path: the profile folder as %USERPROFILE%, since its name may hold letters the
  # console's code page lacks, or & % ^, and the rest with % doubled.
  $h = ([string]$env:USERPROFILE).TrimEnd('\')
  if ($h -and $Path.StartsWith($h + '\', [StringComparison]::OrdinalIgnoreCase)) { return '%USERPROFILE%' + $Path.Substring($h.Length).Replace('%', '%%') }
  return $Path.Replace('%', '%%')
}

function Write-Cmd([string]$Path, [string]$Text) {   # cmd.exe reads a .cmd file in the console's code page, not UTF-8
  $enc = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
  $bytes = $enc.GetBytes($Text)
  if ($enc.GetString($bytes) -ne $Text) { throw "$Path would need letters this console's code page lacks; set CLAUDE_GATEWAY_BIN or CLAUDE_GATEWAY_GCLAUDE_DIR to a plainer folder" }
  [IO.File]::WriteAllBytes($Path, $bytes)
}

# ---------- HTTP ----------

function Invoke-Json([string]$Method, [string]$Uri, $Body, [hashtable]$Headers = @{}) {
  # -> @{code; data; text}. An HTTP error status is an answer, not an exception; no answer at all still throws.
  $p = @{ Method = $Method; Uri = $Uri; Headers = $Headers; UseBasicParsing = $true; TimeoutSec = 15 }
  if ($null -ne $Body) { $p.Body = (ConvertTo-Json -InputObject $Body -Compress); $p.ContentType = 'application/json; charset=utf-8' }
  try {
    $r = Invoke-WebRequest @p
    $code = [int]$r.StatusCode
    $text = [string]$r.Content
  } catch {
    $resp = $_.Exception.Response
    if ($null -eq $resp) { throw }
    $code = [int]$resp.StatusCode
    $text = ''
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $text = $_.ErrorDetails.Message }
    elseif ($resp -is [Net.HttpWebResponse]) {
      try { $text = (New-Object IO.StreamReader($resp.GetResponseStream())).ReadToEnd() } catch { $text = '' }
    }
  }
  $data = $null
  try { $data = $text | ConvertFrom-Json } catch { $data = $null }
  return @{ code = $code; data = $data; text = $text }
}

function Authorize([string]$Dash) {   # the key the dashboard hands this computer once someone authorizes it there
  try { $s = Invoke-Json POST "$Dash/api/device/start" @{ label = $env:COMPUTERNAME } }
  catch { Fail "Can't reach the dashboard at ${Dash}: $($_.Exception.Message)" }
  if ($s.code -ne 200 -or -not (Field $s.data 'device_code')) {
    $hint = if ($s.code -eq 404) { " (is $Dash this gateway's dashboard? Give it with --dashboard)" } else { '' }
    $err = Field $s.data 'error'; if (-not $err) { $err = "HTTP $($s.code)" }
    Fail "The dashboard at $Dash can't authorize this computer: $err$hint"
  }
  $link = $s.data.verification_uri_complete
  Say "To connect this computer, open $link"
  Say "and check that it shows the code $($s.data.user_code)."
  $qr = @(Field $s.data 'qr')
  $glyph = ' ', [char]0x2584, [char]0x2580, [char]0x2588
  $enc = [Console]::Error.Encoding   # a code page without half blocks would print the code as question marks
  if ($qr.Count -gt 1 -and $enc.GetString($enc.GetBytes(-join $glyph)) -eq (-join $glyph)) {
    # Two rows per line in half blocks, the light modules drawn: right on a dark console.
    if ($qr.Count % 2) { $qr += '0' * ([string]$qr[0]).Length }
    Say 'Or scan this with your phone:'
    for ($i = 0; $i -lt $qr.Count; $i += 2) {
      $top = [string]$qr[$i]; $bottom = [string]$qr[$i + 1]
      $line = New-Object Text.StringBuilder
      for ($j = 0; $j -lt $top.Length; $j++) { [void]$line.Append($glyph[2 * ($top[$j] -eq '0') + ($bottom[$j] -eq '0')]) }
      Say $line.ToString()
    }
  }
  $opener = $env:CLAUDE_GATEWAY_OPEN
  if (-not $env:SSH_CONNECTION -and $opener -ne 'none') {
    try {
      if ($opener) { Start-Process -FilePath $opener -ArgumentList $link } else { Start-Process $link }
      Say 'It is open in your browser.'
    } catch { }
  }
  Say 'Waiting for you to authorize it there...'
  $interval = 3.0; if (Field $s.data 'interval') { $interval = [double]$s.data.interval }
  $expires = 600.0; if (Field $s.data 'expires_in') { $expires = [double]$s.data.expires_in }
  $deadline = (Get-Date).AddSeconds($expires)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds ([int]($interval * 1000))
    try { $t = Invoke-Json POST "$Dash/api/device/token" @{ device_code = $s.data.device_code } } catch { continue }   # a blip
    if ($t.code -ge 500 -or $t.code -eq 429) { continue }   # e.g. the gateway restarting; the approval is still there
    if ($t.code -eq 200 -and (Field $t.data 'key')) {
      $who = Field $t.data 'user'; if (-not $who) { $who = '?' }
      Say "Authorized as $who."
      return [string]$t.data.key
    }
    $err = Field $t.data 'error'
    if ($err -eq 'slow_down') { $interval += 2; continue }
    if ($err -eq 'authorization_pending') { continue }
    if ($err -eq 'access_denied') { Fail 'Cancelled in the browser; nothing was changed.' }
    if ($err -eq 'expired_token') { Fail 'The code expired. Run the command again for a new one.' }
    if (-not $err) { $err = $t.code }
    Fail "Authorization failed: $err"
  }
  Fail 'The code expired. Run the command again for a new one.'
}

function Preflight([string]$Url, [string]$Key) {   # the gateway answers and accepts the key
  try { $r = Invoke-Json GET "$Url/v1/models" $null @{ Authorization = "Bearer $Key"; 'anthropic-version' = '2023-06-01' } }
  catch { $r = @{ code = '000'; text = $_.Exception.Message } }
  if ($r.code -ne 200) {
    $detail = [string]$r.text; if ($detail.Length -gt 400) { $detail = $detail.Substring(0, 400) }
    Fail "The gateway at $Url did not accept the key (HTTP $($r.code)); leaving Claude Code as it is.`n$detail"
  }
}

# ---------- gclaude ----------

function Launcher-Ours { return (Test-Path -LiteralPath $Launcher) -and (Select-String -LiteralPath $Launcher -SimpleMatch $LauncherMark -Quiet) }

function Ours-Hook($group) {
  foreach ($h in @(Field $group 'hooks')) { if ((Field $h 'command') -eq $WarnCmd) { return $true } }
  return $false
}

function Remove-Ours($s, $rec) {   # what an earlier `on` added to gclaude's settings.json, as far as it is still ours
  $envBlock = Field $s 'env'
  if ($envBlock -is [psobject]) {
    foreach ($k in 'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_GATEWAY_DASHBOARD') { Remove-Field $envBlock $k }
    $added = Field $rec 'added_models'
    if ($added -is [psobject]) {   # only while still ours: the user has not picked another model for that alias since
      foreach ($p in $added.PSObject.Properties) { if ((Field $envBlock $p.Name) -eq $p.Value) { Remove-Field $envBlock $p.Name } }
    }
    if (Is-Empty $envBlock) { Remove-Field $s 'env' }
  }
  if (Field $rec 'added_disable_connectors') { Remove-Field $s 'disableClaudeAiConnectors' }
  if ((Field $rec 'added_statusline') -and ((Field (Field $s 'statusLine') 'command') -eq $LineCmd)) { Remove-Field $s 'statusLine' }
  $hooks = Field $s 'hooks'
  if ((Field $rec 'added_warn_hook') -and ($hooks -is [psobject]) -and (Has $hooks 'UserPromptSubmit')) {
    $groups = @(@($hooks.UserPromptSubmit) | Where-Object { -not (Ours-Hook $_) })
    if ($groups.Count) { Set-Field $hooks 'UserPromptSubmit' $groups } else { Remove-Field $hooks 'UserPromptSubmit' }
    if (Is-Empty $hooks) { Remove-Field $s 'hooks' }
  }
  foreach ($k in 'added_disable_connectors', 'added_statusline', 'added_warn_hook', 'added_models') { Remove-Field $rec $k }
}

function Command-Text([string]$name) {
  if ($name -eq 'logout_gclaude') {   # answered by the --warn hook, as the key it removes is the one a model call would need
    return @"
---
description: Sign this computer out of the gateway
disable-model-invocation: true
---
<!-- $UsageMark -->
The claude-gateway hook that signs gclaude out did not run, so nothing was changed. Tell the user, in one sentence,
that running ``gclaude update`` in a terminal reinstalls the hook and ``gclaude uninstall`` removes gclaude with its key. Use no tools.
"@
  }
  if ($name -eq 'account') {   # statusline.ps1 --account, repeated by a small model call; see statusline.ps1
    return @"
---
description: Your gateway account and a link to the dashboard
allowed-tools: Bash($LineCmd --account)
model: haiku
disable-model-invocation: true
---
<!-- $UsageMark -->
!``$LineCmd --account``

Repeat the line above to the user exactly as it is, and nothing else. Use no tools.
"@
  }
  return @"
---
description: Your gateway limits and usage
disable-model-invocation: true
---
<!-- $UsageMark -->
The claude-gateway hook that answers /usage did not run. Tell the user, in one sentence, that their gateway limits
are on the status line and on the dashboard, and that running ``gclaude update`` in a terminal reinstalls the hook. Use no tools.
"@
}

function Gclaude-On($c) {
  $rec = Field $c 'gclaude'
  if (-not ($rec -is [psobject])) { $rec = New-Object psobject; Set-Field $c 'gclaude' $rec }

  foreach ($d in $ClientDir, $GDir) { if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null } }
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'statusline.ps1') -Destination $Statusline -Force

  $s = Read-Json $Settings
  if (-not ($s -is [psobject])) { $s = New-Object psobject }
  Remove-Ours $s $rec
  $envBlock = Field $s 'env'
  if (-not ($envBlock -is [psobject])) { $envBlock = New-Object psobject; Set-Field $s 'env' $envBlock }
  Set-Field $envBlock 'ANTHROPIC_BASE_URL' $c.url
  Set-Field $envBlock 'ANTHROPIC_AUTH_TOKEN' $c.key
  Set-Field $envBlock 'CLAUDE_GATEWAY_DASHBOARD' $c.dashboard
  $added = New-Object psobject
  foreach ($k in $OneMModels.Keys) {
    if (-not (Has $envBlock $k)) { Set-Field $envBlock $k $OneMModels[$k]; Set-Field $added $k $OneMModels[$k] }
  }
  if (-not (Is-Empty $added)) { Set-Field $rec 'added_models' $added }
  if (-not (Has $s 'disableClaudeAiConnectors')) {   # no claude.ai login in gclaude: silence its connectors warning
    Set-Field $s 'disableClaudeAiConnectors' $true
    Set-Field $rec 'added_disable_connectors' $true
  }
  if (-not (Has $s 'statusLine')) {
    Set-Field $s 'statusLine' ([pscustomobject]@{ type = 'command'; command = $LineCmd; refreshInterval = 30 })
    Set-Field $rec 'added_statusline' $true
  } elseif ((Field $s.statusLine 'command') -ne $LineCmd) {
    Write-Output "gclaude already has a statusline, so it was left as it is. To show your gateway limits in it too,"
    Write-Output "add the output of this command to it: $LineCmd"
  }
  $hooks = Field $s 'hooks'
  if (-not ($hooks -is [psobject])) { $hooks = New-Object psobject; Set-Field $s 'hooks' $hooks }
  $groups = @(); if (Has $hooks 'UserPromptSubmit') { $groups = @($hooks.UserPromptSubmit) }
  if (-not @($groups | Where-Object { Ours-Hook $_ }).Count) {
    $hook = [pscustomobject]@{ type = 'command'; command = $WarnCmd; timeout = 10 }
    $groups += [pscustomobject]@{ hooks = @($hook) }
    Set-Field $hooks 'UserPromptSubmit' $groups
    Set-Field $rec 'added_warn_hook' $true
  }
  Write-Json $Settings $s -Private   # it holds the key
  Remove-Item -LiteralPath (Join-Path $GDir 'signed-out') -Force -ErrorAction SilentlyContinue   # left by /logout_gclaude
  Write-Json $Client $c -Private

  # Claude Code's own /usage can't see the gateway, nor its /logout sign out of it; the --warn hook answers /usage
  # and /logout_gclaude instead (not /logout: a built-in can't be hidden, so the menu would list both). /account: Command-Text.
  $old = Join-Path $GDir 'commands\logout.md'   # what an older gclaude named /logout_gclaude
  if ((Test-Path -LiteralPath $old) -and (Select-String -LiteralPath $old -SimpleMatch $UsageMark -Quiet)) { Remove-Item -LiteralPath $old -Force }
  foreach ($name in 'usage', 'account', 'logout_gclaude') {
    $file = Join-Path $GDir "commands\$name.md"
    if ((Test-Path -LiteralPath $file) -and -not (Select-String -LiteralPath $file -SimpleMatch $UsageMark -Quiet)) {
      Write-Output "$file is your own, so it was left as it is; /$name in gclaude runs it instead of the gateway's."
    } else {
      New-Item -ItemType Directory -Path (Split-Path -Parent $file) -Force | Out-Null
      [IO.File]::WriteAllText($file, ((Command-Text $name) -replace "`r`n", "`n") + "`n", (New-Object Text.UTF8Encoding $false))
    }
  }

  $state = Join-Path $GDir '.claude.json'   # Claude Code's own state; seeded once so it skips its welcome screens
  if (-not (Test-Path -LiteralPath $state)) {
    $seed = [pscustomobject]@{ hasCompletedOnboarding = $true }
    try { $theme = Field (Read-Json (Join-Path $HomeDir '.claude.json')) 'theme'; if ($theme) { Set-Field $seed 'theme' $theme } } catch { }
    Write-Json $state $seed
  }

  if (-not (Test-Path -LiteralPath $Bin)) { New-Item -ItemType Directory -Path $Bin -Force | Out-Null }
  $cmd = @(
    '@echo off',
    'rem gclaude: Claude Code through the claude-proxy gateway, with its own settings and history in the',
    'rem CLAUDE_CONFIG_DIR below. Plain claude keeps this machine''s own login.',
    $LauncherMark,
    'rem Remove it with: gclaude uninstall',
    'setlocal',
    'rem Users only need gclaude: its update, status and uninstall run claude-gateway for them.',
    'if /i "%~1"=="status" goto status',
    'if /i "%~1"=="uninstall" goto uninstall',
    'where claude >nul 2>nul || (echo gclaude: Claude Code ^(claude^) is not installed or not on PATH 1>&2 & exit /b 127)',
    'rem gclaude update: the latest claude-gateway from the dashboard, whose installer also refreshes gclaude, then',
    'rem Claude Code''s own update, and a last line saying it was gclaude''s. One block, so cmd has read all of it before the installer rewrites this file.',
    'rem install.ps1 throws when it fails. Windows PowerShell exits 0 all the same when the throw happens inside iex,',
    'rem so the try/catch turns it into exit 1. The failure then goes to :updatefailed (a label is found by name, so',
    'rem the rewritten file serves; keep the name), whose top-level exit /b 1 reaches cmd /c where one in a nested',
    'rem block would not, and Claude Code is left alone.',
    ('if /i "%~1"=="update" (' + "`r`n" +
     "  powershell -NoProfile -ExecutionPolicy Bypass -Command `"try { irm '$(($c.dashboard + '/install.ps1').Replace("'", "''").Replace('%', '%%'))' | iex } catch { [Console]::Error.WriteLine(`$_); exit 1 }`" || goto :updatefailed" + "`r`n" +
     '  echo Now Claude Code itself:' + "`r`n" +
     '  claude %* && echo gclaude is up to date.' + "`r`n" +
     '  exit /b' + "`r`n" +
     ')'),
    "set `"CLAUDE_CONFIG_DIR=$(Cmd-Path $GDir)`"",
    'rem Sign this computer in again first, as plain claude''s login would, when /logout_gclaude left its signed-out file',
    'rem (`if exist` reads any folder name, where findstr can''t) or the dashboard no longer accepts the key (the computer',
    'rem was removed there). Only a 401 counts: offline, without curl.exe, or with the dashboard slow or down, gclaude',
    'rem starts as usual. The key reaches curl on stdin, through GW_KEY, which the inner cmd expands: never in a command line.',
    'set "GW_WHY=" & set "GW_KEY=" & set "GW_CODE="',
    'if exist "%CLAUDE_CONFIG_DIR%\signed-out" set "GW_WHY=signed out by /logout_gclaude"',
    'if not defined GW_WHY for /f "tokens=2 delims=:, " %%k in (''type "%CLAUDE_CONFIG_DIR%\settings.json" 2^>nul ^| findstr ANTHROPIC_AUTH_TOKEN'') do set "GW_KEY=%%~k"',
    ("if defined GW_KEY for /f %%c in ('echo Authorization: Bearer %%GW_KEY%%^| curl.exe -s -o nul --max-time 3 -H @- -w `"%%{http_code}`" `"" +
     ($c.dashboard + '/api/me/status').Replace('%', '%%') + "`" 2^>nul') do set `"GW_CODE=%%c`""),
    'if "%GW_CODE%"=="401" set "GW_WHY=this computer''s key no longer works, removed in the dashboard?"',
    'set "GW_KEY="',
    'rem One block, read before `on` rewrites this file; GW_WHY holds no parentheses, which would end it.',
    ('if defined GW_WHY (' + "`r`n" +
     '  echo gclaude: %GW_WHY%; signing this computer in again. 1>&2' + "`r`n" +
     '  set CLAUDE_GATEWAY_FROM_GCLAUDE=1' + "`r`n" +
     '  call claude-gateway on --gclaude --login || goto signinfailed' + "`r`n" +
     '  call claude %*' + "`r`n" +
     '  if exist "%CLAUDE_CONFIG_DIR%\signed-out" call :closed' + "`r`n" +
     '  exit /b' + "`r`n" +
     ')'),
    'rem call: an npm install''s claude is claude.cmd, and a batch run without call never comes back here.',
    'call claude %*',
    'if exist "%CLAUDE_CONFIG_DIR%\signed-out" call :closed',
    'exit /b',
    'rem /logout_gclaude signed out and stopped claude (statusline.ps1): the terminal back as Claude Code would leave it',
    'rem (colours, cursor, bracketed paste, focus reports, keyboard mode), with the lines Claude Code drew below its cursor',
    'rem cleared from the start of its line, then the reason it left in the signed-out file.',
    ':closed',
    ('<nul set /p "=' + ((@('[1G', '[0J', '[0m', '[?25h', '[?2004l', '[?1004l', '[<u') | ForEach-Object { [char]27 + $_ }) -join '') + '"'),
    'type "%CLAUDE_CONFIG_DIR%\signed-out"',
    'exit /b 0',
    ':updatefailed',
    'echo gclaude: the gateway update failed, so Claude Code was not updated 1>&2',
    'exit /b 1',
    ':signinfailed',   # a top-level label, like :updatefailed, so cmd /c really returns 1
    'echo gclaude: not signed in, so Claude Code was not started 1>&2',
    'exit /b 1',
    ':status',
    'call claude-gateway status & exit /b',
    'rem `off` deletes this file, and cmd would go on reading it ("The batch file cannot be found.", exit 1): (goto)',
    'rem first ends the batch, and the rest of the line runs on its own, its exit code going to cmd /c.',
    ':uninstall',
    '(goto) 2>nul & call claude-gateway off'
  ) -join "`r`n"
  try { Write-Cmd $Launcher ($cmd + "`r`n") } catch { Fail $_.Exception.Message }

  Write-Output "gclaude now runs Claude Code through the gateway at $($c.url); plain 'claude' is unchanged."
  if (($env:Path -split ';') -notcontains $Bin -and -not $env:CLAUDE_GATEWAY_FROM_GCLAUDE) { Write-Output "Open a new terminal (or add $Bin to your PATH) to run gclaude." }
}

function Gclaude-Off {
  if (-not (Launcher-Ours) -and -not (Test-Path -LiteralPath $Settings)) { Write-Output 'gclaude was not set up; nothing to undo.'; return }
  $c = Read-Json $Client
  if (-not ($c -is [psobject])) { $c = New-Object psobject }
  $rec = Field $c 'gclaude'
  if (-not ($rec -is [psobject])) { $rec = New-Object psobject }
  $s = Read-Json $Settings
  if ($s -is [psobject]) { Remove-Ours $s $rec; Write-Json $Settings $s -Private }
  Remove-Item -LiteralPath (Join-Path $GDir 'signed-out') -Force -ErrorAction SilentlyContinue
  $cmds = Join-Path $GDir 'commands'
  foreach ($name in 'usage', 'account', 'logout_gclaude', 'logout') {   # logout: an older gclaude's
    $file = Join-Path $cmds "$name.md"
    if ((Test-Path -LiteralPath $file) -and (Select-String -LiteralPath $file -SimpleMatch $UsageMark -Quiet)) { Remove-Item -LiteralPath $file -Force }
  }
  if ((Test-Path -LiteralPath $cmds) -and -not (Get-ChildItem -LiteralPath $cmds -Force)) { Remove-Item -LiteralPath $cmds -Force }
  Remove-Field $c 'gclaude'
  if (Test-Path -LiteralPath $Client) { Write-Json $Client $c -Private }
  if (Launcher-Ours) {
    Remove-Item -LiteralPath $Launcher -Force
    Write-Output "gclaude is removed. Its history and sessions are still in $GDir; delete that folder to drop them."
  } else {
    Write-Output "gclaude's gateway settings are removed. $Launcher was not installed by claude-gateway, so it was left as it is."
  }
}

function Show-Status {
  $c = Read-Json $Client
  $url = Field $c 'url'
  if (-not $url) { Write-Output 'Not set up yet. Run: claude-gateway on --url https://claude.example.com'; return }
  Write-Output "gateway: $url"
  Write-Output "dashboard: $($c.dashboard)"
  if (Launcher-Ours) { Write-Output "gclaude: installed ($Launcher)" } else { Write-Output 'gclaude: not set up' }
  try {
    $r = Invoke-Json GET "$($c.dashboard)/api/me/status?format=text" $null @{ Authorization = "Bearer $($c.key)" }
    if ($r.code -eq 200) { Write-Output "status: $(([string]$r.text).Trim())" }
    elseif (-not $c.key) { Write-Output 'status: signed out; run gclaude to sign in' }
    elseif ($r.code -eq 401) { Write-Output "status: this computer's key no longer works; run gclaude to sign in again" }
    else { Write-Output "status: unavailable (HTTP $($r.code))" }
  } catch { Write-Output "status: unavailable ($($_.Exception.Message))" }
}

# ---------- commands ----------

$cmd = 'status'; $rest = @()
if ($args.Count -gt 0) { $cmd = [string]$args[0] }
if ($args.Count -gt 1) { $rest = @($args[1..($args.Count - 1)]) }
$url = ''; $key = ''; $dash = ''; $login = $false
for ($i = 0; $i -lt $rest.Count; $i++) {
  $a = [string]$rest[$i]
  $next = { if ($i + 1 -ge $rest.Count) { Fail "$a needs a value" }; $script:i++; [string]$rest[$script:i] }
  switch -Exact ($a) {
    '--url' { $url = & $next }
    '--key' { $key = & $next }
    '--dashboard' { $dash = & $next }
    '--login' { $login = $true }
    '--gclaude' { }
    { $_ -in '--global', '--own-login', '--key-only', '--opencode', '--routes-key' } { Fail "$a is not available on Windows yet; see $Issue" }
    { $_ -in '-h', '--help' } { Usage; exit 0 }
    default { Fail "Unknown option: $a (see claude-gateway help)" }
  }
}

switch -Exact ($cmd) {
  'on' {
    if ((Test-Path -LiteralPath $Launcher) -and -not (Launcher-Ours)) {
      Fail "$Launcher already exists and claude-gateway did not install it; left as it is."
    }
    $c = Read-Json $Client
    if (-not ($c -is [psobject])) { $c = New-Object psobject }
    $savedUrl = [string](Field $c 'url'); $savedKey = [string](Field $c 'key'); $savedDash = [string](Field $c 'dashboard')
    if (-not $url) { $url = $savedUrl }
    if (-not $url) { Fail 'No gateway yet: claude-gateway on --url https://claude.example.com' }
    $url = $url.TrimEnd('/')
    if (-not $dash) {
      if ($savedUrl -eq $url -and $savedDash) { $dash = $savedDash } else { $dash = ([regex]'://claude\.').Replace($url, '://claude-dash.', 1) }
    }
    $dash = $dash.TrimEnd('/')
    if (-not $key -and -not $login -and -not $savedKey -and $savedUrl -eq $url -and (Launcher-Ours)) {
      # Signed out by /logout_gclaude, and here from the installer (gclaude update): no browser mid-update. The next
      # gclaude signs in (`on --login`), which also refreshes its setup.
      Write-Output 'This computer is signed out of the gateway; gclaude signs it in again when you next start it.'
      exit 0
    }
    if (-not $key) {
      if ($login -or $savedUrl -ne $url -or -not $savedKey) { $key = Authorize $dash } else { $key = $savedKey }
    }
    Preflight $url $key
    Set-Field $c 'url' $url; Set-Field $c 'key' $key; Set-Field $c 'dashboard' $dash
    Gclaude-On $c
    if (-not $env:CLAUDE_GATEWAY_FROM_GCLAUDE) { Write-Output 'Start a new gclaude session to use it.' }   # else gclaude starts now
  }
  'off' { Gclaude-Off }
  'status' { Show-Status }
  { $_ -in 'help', '-h', '--help' } { Usage }
  default { Fail "Unknown command: $cmd (on, off, status or help)" }
}
