# Claude Code statusline for gclaude on Windows: your gateway limits. The Windows form of scripts/statusline.sh.
#
# gclaude's settings.json (written by claude-gateway on):
#   "statusLine": {"type": "command", "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"C:/.../statusline.ps1\"", "refreshInterval": 30}
# With --warn it is a UserPromptSubmit hook instead. When a figure on the line is at 80% or more it prints
# {"systemMessage": "Gateway: <line>"}, which Claude Code shows; again after 15 minutes, or at once when a
# figure reaches 100%. Nothing else ever goes to stdout in that mode: a hook's plain output joins the prompt.
# The same hook answers gclaude's /usage command (commands\usage.md there; Claude Code's own /usage can't see the
# gateway): it blocks that prompt, so no model call is made, and gives a fresh line with the dashboard link as the reason.
# With --account it prints who the key belongs to, which key it is and the dashboard link, for gclaude's /account
# (commands\account.md there runs it, and the model repeats the line: a hook's reply would read as an error).
# Only when a limit is reached or the gateway is down, so that model call would fail, --warn answers /account instead.
# It also answers gclaude's /logout_gclaude (commands\logout_gclaude.md there): it revokes the key on
# the gateway when it is this computer's own (the first key may be in use elsewhere, so it stays valid), and removes
# it from gclaude's settings.json and from client.json (beside this script, or CLAUDE_GATEWAY_CLIENT). It leaves a
# signed-out file in CLAUDE_CONFIG_DIR, so the next gclaude.cmd signs in again (`claude-gateway on --login`, which removes it) before it starts.
# Then, like Claude Code's own /logout, the session ends: about a second later a hidden helper types Ctrl-C twice into the
# console of the claude that ran the hook, so it exits the usual way, saving the session, after showing the reason.
# Typed, not sent as a console Ctrl-C event: that event would reach every process on the console, and gclaude.cmd's
# cmd would then ask 'Terminate batch job (Y/N)?'.
# Environment (set in the same settings.json "env" block):
#   ANTHROPIC_AUTH_TOKEN        your gateway key
#   CLAUDE_GATEWAY_DASHBOARD    dashboard base URL
# Any error ends quietly: a statusline or a hook must never break the session.

$warn = $args -contains '--warn'
$accountMode = $args -contains '--account'
try {
  $utf8 = New-Object Text.UTF8Encoding $false
  # UTF-8 through our own stdin and stdout, not [Console]::InputEncoding/OutputEncoding: those set the code page of
  # the console Claude Code shares with us, and the old console (conhost) then draws its logo and bullets as boxes.
  $stdout = New-Object IO.StreamWriter ([Console]::OpenStandardOutput()), $utf8
  $stdout.AutoFlush = $true
  $ProgressPreference = 'SilentlyContinue'
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
  # Windows PowerShell 5.1 would write arrays as {"value": [...], "Count": n}; see claude-gateway.ps1.
  Remove-TypeData System.Array -ErrorAction SilentlyContinue

  $stdin = ''
  if (-not $accountMode) { try { $stdin = (New-Object IO.StreamReader ([Console]::OpenStandardInput()), $utf8).ReadToEnd() } catch { } }   # session or prompt JSON; only --warn looks at it, for /usage
  $dash = ([string]$env:CLAUDE_GATEWAY_DASHBOARD).TrimEnd('/')

  function Ours([string]$name) {   # the prompt is /name and gclaude's commands\name.md is claude-gateway's
    if (-not ($warn -and $env:CLAUDE_CONFIG_DIR)) { return $false }
    $cmdFile = Join-Path $env:CLAUDE_CONFIG_DIR "commands\$name.md"
    if (-not ((Test-Path -LiteralPath $cmdFile) -and (Select-String -LiteralPath $cmdFile -SimpleMatch '# Installed by claude-gateway on --gclaude.' -Quiet))) { return $false }
    $prompt = ''
    try { $prompt = [string]($stdin | ConvertFrom-Json).prompt } catch { }
    return $prompt -match "^/$name(\s|$)"
  }
  $usage = Ours 'usage'
  $account = Ours 'account'

  function Emit([string]$s) { $stdout.Write($s + "`n") }
  function Block([string]$reason) { Emit (ConvertTo-Json -Compress -InputObject @{ decision = 'block'; reason = $reason }); exit 0 }
  function Stop-Prompt([string]$reason) { Emit (ConvertTo-Json -Compress -InputObject @{ continue = $false; stopReason = $reason }); exit 0 }
  function Quit-Claude {   # the claude that runs this hook (up to three processes up, through a shell), only when one does
    if (-not $env:CLAUDE_PROJECT_DIR) { return $false }   # set by Claude Code for its hooks
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction Stop; $found = $null
    for ($i = 0; $i -lt 3 -and -not $found; $i++) {
      $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)" -ErrorAction Stop
      if (-not $proc) { return $false }
      if ($proc.Name -eq 'claude.exe' -or ($proc.Name -eq 'node.exe' -and [string]$proc.CommandLine -match 'claude')) { $found = $proc.ProcessId }   # node: an npm install
    }
    if (-not $found) { return $false }
    $helper = @"
Add-Type -Namespace CG -Name Con -MemberDefinition @'
[StructLayout(LayoutKind.Explicit, CharSet = CharSet.Unicode)] public struct Key {
  [FieldOffset(0)] public ushort EventType; [FieldOffset(4)] public int KeyDown; [FieldOffset(8)] public ushort Repeat;
  [FieldOffset(10)] public ushort VKey; [FieldOffset(12)] public ushort ScanCode; [FieldOffset(14)] public char Char;
  [FieldOffset(16)] public uint ControlKeys; }
[DllImport("kernel32.dll")] public static extern bool FreeConsole();
[DllImport("kernel32.dll")] public static extern bool AttachConsole(uint pid);
[DllImport("kernel32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr CreateFile(string name, uint access, uint share, IntPtr sec, uint disp, uint flags, IntPtr tmpl);
[DllImport("kernel32.dll", CharSet = CharSet.Unicode)] public static extern bool WriteConsoleInput(IntPtr h, Key[] keys, uint n, out uint written);
'@
Start-Sleep -Milliseconds 500   # with PowerShell's start and Add-Type, about a second after the hook
[void][CG.Con]::FreeConsole()
if (-not [CG.Con]::AttachConsole($found)) { exit }
`$in = [CG.Con]::CreateFile('CONIN`$', 3221225472, 3, [IntPtr]::Zero, 3, 0, [IntPtr]::Zero)
foreach (`$n in 1, 2) {   # Ctrl-C, down and up: the key 0x43 (C) with the left Ctrl held, as the character 3
  `$keys = foreach (`$down in 1, 0) { `$k = New-Object CG.Con+Key; `$k.EventType = 1; `$k.KeyDown = `$down; `$k.Repeat = 1; `$k.VKey = 0x43; `$k.ScanCode = 0x2E; `$k.Char = [char]3; `$k.ControlKeys = 8; `$k }
  `$w = [uint32]0; [void][CG.Con]::WriteConsoleInput(`$in, [CG.Con+Key[]]`$keys, 2, [ref]`$w)
  Start-Sleep -Milliseconds 300
}
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($helper))
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded
    return $true
  }

  if (Ours 'logout_gclaude') {   # gclaude's /logout_gclaude: revoke this computer's key on the gateway, then drop every copy of it here
    $key = [string]$env:ANTHROPIC_AUTH_TOKEN
    $code = 0; $revoked = $false; $odd = $false   # code 0: no answer at all; odd: a 2xx whose body isn't the JSON expected
    if ($key -and $dash) {
      $r = $null
      try {   # the call alone: what fails here is the transport or a non-2xx status
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Method Post -Uri "$dash/api/me/logout" -Headers @{ Authorization = "Bearer $key" }
        $code = [int]$r.StatusCode
      } catch {
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
      }
      if ($code -ge 200 -and $code -lt 300) {   # the body apart, so an empty or non-JSON reply can't hide the 2xx
        try {
          $body = $r.Content
          if ($body -is [byte[]]) { $body = $utf8.GetString($body) }
          $j = [string]$body | ConvertFrom-Json   # {"ok": true, "revoked": bool}
          if (($j -is [psobject]) -and $j.PSObject.Properties['revoked']) { $revoked = [bool]$j.revoked } else { $odd = $true }
        } catch { $odd = $true }
      }
    }
    $settings = Join-Path $env:CLAUDE_CONFIG_DIR 'settings.json'
    $client = if ($env:CLAUDE_GATEWAY_CLIENT) { $env:CLAUDE_GATEWAY_CLIENT } else { Join-Path $PSScriptRoot 'client.json' }
    try {   # rewritten in place, so each file keeps its owner-only permissions
      [IO.File]::WriteAllText((Join-Path $env:CLAUDE_CONFIG_DIR 'signed-out'), "/logout_gclaude`n", $utf8)
      $s = [IO.File]::ReadAllText($settings) | ConvertFrom-Json
      if (($s.env -is [psobject]) -and ($s.env.PSObject.Properties.Name -contains 'ANTHROPIC_AUTH_TOKEN')) {
        $s.env.PSObject.Properties.Remove('ANTHROPIC_AUTH_TOKEN')
        [IO.File]::WriteAllText($settings, (ConvertTo-Json -InputObject $s -Depth 20) + "`n", $utf8)
      }
      if ($key -and (Test-Path -LiteralPath $client)) {
        $c = [IO.File]::ReadAllText($client) | ConvertFrom-Json
        if ([string]$c.key -eq $key) {
          $c.PSObject.Properties.Remove('key')
          [IO.File]::WriteAllText($client, (ConvertTo-Json -InputObject $c -Depth 20) + "`n", $utf8)
        }
      }
    } catch { Stop-Prompt "Sign-out failed: the key could not be removed from $settings. gclaude uninstall removes it." }
    $closing = $false
    try { $closing = Quit-Claude } catch { }
    $again = if ($closing) { 'gclaude is closing; run gclaude again to sign in.' } else { 'Exit gclaude now (/exit); run gclaude again to sign in.' }
    $where = if ($dash) { " ($dash/dashboard)" } else { '' }
    $accepted = $code -ge 200 -and $code -lt 300
    if ($accepted -and $odd) { Stop-Prompt "Signed out: the key is removed from gclaude, and the gateway accepted the sign-out (HTTP $code) but gave an unexpected reply; check in the dashboard that this computer is gone$where. $again" }
    if ($accepted -and $revoked) { Stop-Prompt "Signed out: this computer's key is revoked on the gateway and removed from gclaude. $again" }
    if ($accepted) { Stop-Prompt "Signed out: the key is removed from gclaude. It is your first key, so it still works wherever else it is set up. $again" }
    if ($code -eq 401 -or $code -eq 403) { Stop-Prompt "Signed out: the key is removed from gclaude (the gateway no longer accepted it). $again" }
    $why = if ($code) { "refused to revoke it (HTTP $code)" } else { 'could not be reached to revoke it' }
    Stop-Prompt "Signed out here: the key is removed from gclaude, but the gateway $why; remove this computer in the dashboard$where. $again"
  }

  if (-not $dash) {
    if ($accountMode) { Emit 'Account details unavailable: CLAUDE_GATEWAY_DASHBOARD is not set (run gclaude update).'; exit 0 }
    if ($usage) { Block 'Gateway status unavailable: CLAUDE_GATEWAY_DASHBOARD is not set (run gclaude update).' }
    if (-not $warn) { [Console]::Error.WriteLine('statusline.ps1: set CLAUDE_GATEWAY_DASHBOARD') }
    exit 0
  }

  # A figure is a percentage or a used/limit pair ($61/$100, 4.2M/5.0M); Pct gives it as a percentage.
  $RE = [regex]'\$?[0-9][0-9,.]*[KM]?/\$?[0-9][0-9,.]*[KM]?%?|[0-9]+(\.[0-9]+)?%'
  function Num([string]$t) {
    $t = $t -replace '[$,%]', ''
    $mult = 1.0
    if ($t.EndsWith('M')) { $mult = 1e6; $t = $t.TrimEnd('M') } elseif ($t.EndsWith('K')) { $mult = 1e3; $t = $t.TrimEnd('K') }
    $v = 0.0
    if ([double]::TryParse($t, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$v)) { return $v * $mult }
    return 0.0
  }
  function Pct([string]$tok) {
    $ab = $tok.Split('/')
    if ($ab.Count -eq 2) { $b = Num $ab[1]; if ($b) { return 100.0 * (Num $ab[0]) / $b } return 0.0 }
    return Num $tok
  }
  # Figures: each used/limit pair (with its " req"/" tok" unit) followed by its percentage, rounded down so 100%
  # means reached. With colour: cyan with a leading diamond, a figure yellow at 80% and red at 100%.
  function Figures([string]$line, [bool]$colour) {
    $e = [char]27
    $out = ''; $rest = $line
    while ($true) {
      $m = $RE.Match($rest)
      if (-not $m.Success) { break }
      $pre = $rest.Substring(0, $m.Index); $tok = $m.Value; $v = Pct $tok
      $rest = $rest.Substring($m.Index + $m.Length)
      $ab = $tok.Split('/')
      if ($ab.Count -eq 2) {
        if ($rest -match '^ (req|tok)') { $tok += $rest.Substring(0, 4); $rest = $rest.Substring(4) }
        if (Num $ab[1]) { $tok += ' ' + [int][math]::Floor($v + 1e-9) + '%' }
      }
      $c = ''
      if ($colour) { if ($v -ge 100) { $c = "$e[31m" } elseif ($v -ge 80) { $c = "$e[33m" } }
      if ($c) { $out += $pre + $c + $tok + "$e[36m" } else { $out += $pre + $tok }
    }
    if ($colour) { return "$e[36m" + [char]0x25C6 + ' ' + $out + $rest + "$e[0m" }
    return $out + $rest
  }
  function Peak([string]$line) {
    $max = 0.0
    foreach ($m in $RE.Matches($line)) { $v = Pct $m.Value; if ($v -gt $max) { $max = $v } }
    return [int][math]::Floor($max)
  }
  function Age([string]$path) { return ((Get-Date) - (Get-Item -LiteralPath $path).LastWriteTime).TotalSeconds }

  function Status([string]$format) {   # /api/me/status as text, with this key; '' when it fails
    try {
      $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "$dash/api/me/status?format=$format" -Headers @{ Authorization = "Bearer $env:ANTHROPIC_AUTH_TOKEN" }
      $body = $r.Content
      if ($body -is [byte[]]) { $body = $utf8.GetString($body) }
      return ([string]$body).Trim()
    } catch { return '' }
  }

  function Account-Line {
    $a = Status 'account'
    if (-not $a) { return "Account details unavailable; see $dash/dashboard" }
    return 'Account: ' + $a + ' ' + [char]0x00B7 + " dashboard: $dash/dashboard"
  }
  if ($accountMode) { Emit (Account-Line); exit 0 }

  $cache = Join-Path ([IO.Path]::GetTempPath()) 'claude-gateway-status.txt'
  $line = ''
  if (-not ($usage -or $account) -and (Test-Path -LiteralPath $cache) -and (Age $cache) -lt 30) {
    $line = ([IO.File]::ReadAllText($cache)).Trim()
  } else {
    $line = Status 'text'
    if ($line) { [IO.File]::WriteAllText($cache, $line + "`n", $utf8) }
  }

  if (-not $warn) {
    if (-not $line) { $line = 'gateway status unavailable' }
    Emit (Figures $line $true)
    exit 0
  }

  if ($usage) {
    if (-not $line) { Block "Gateway status unavailable; see $dash/dashboard" }
    Block ('Gateway: ' + (Figures $line $false) + ' ' + [char]0x00B7 + " details: $dash/dashboard")
  }
  if ($account) {   # the model answers /account, unless it can't be reached
    if (-not $line) { Block (Account-Line) }
    if ((Peak $line) -ge 100) { Block ((Account-Line) + ' ' + [char]0x00B7 + ' limit reached: ' + (Figures $line $false)) }
  }

  # --warn: band 1 from 80%, band 2 from 100%. The state file holds the band last warned about; its age is the time since.
  if (-not $line) { exit 0 }
  $p = Peak $line
  $band = 0
  if ($p -ge 80) { $band = 1 }
  if ($p -ge 100) { $band = 2 }
  if ($band -eq 0) { exit 0 }
  $state = "$cache.warned"
  $last = 0
  if (Test-Path -LiteralPath $state) {
    $t = ([IO.File]::ReadAllText($state)).Trim()
    if ($t -eq '1' -or $t -eq '2') { $last = [int]$t }
    if ($band -le $last -and (Age $state) -lt 900) { exit 0 }
  }
  [IO.File]::WriteAllText($state, "$band`n", $utf8)
  Emit (ConvertTo-Json -Compress -InputObject @{ systemMessage = 'Gateway: ' + (Figures $line $false) })
  exit 0
} catch {
  if (-not $warn -and $stdout) { $stdout.Write("gateway status unavailable`n") }   # $stdout: unless making it failed
  exit 0
}
