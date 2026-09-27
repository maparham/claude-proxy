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
# Environment (set in the same settings.json "env" block):
#   ANTHROPIC_AUTH_TOKEN        your gateway key
#   CLAUDE_GATEWAY_DASHBOARD    dashboard base URL
# Any error ends quietly: a statusline or a hook must never break the session.

$warn = $args -contains '--warn'
$accountMode = $args -contains '--account'
try {
  $utf8 = New-Object Text.UTF8Encoding $false
  try { [Console]::InputEncoding = $utf8 } catch { }
  try { [Console]::OutputEncoding = $utf8 } catch { }   # the diamond and the colour codes as they are
  $ProgressPreference = 'SilentlyContinue'
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

  $stdin = ''
  if (-not $accountMode) { try { $stdin = [Console]::In.ReadToEnd() } catch { } }   # session or prompt JSON; only --warn looks at it, for /usage
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

  function Emit([string]$s) { [Console]::Out.Write($s + "`n") }
  function Block([string]$reason) { Emit (ConvertTo-Json -Compress -InputObject @{ decision = 'block'; reason = $reason }); exit 0 }

  if (-not $dash) {
    if ($accountMode) { Emit 'Gateway account unavailable: CLAUDE_GATEWAY_DASHBOARD is not set (rerun claude-gateway on).'; exit 0 }
    if ($usage) { Block 'Gateway status unavailable: CLAUDE_GATEWAY_DASHBOARD is not set (rerun claude-gateway on).' }
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
    if (-not $a) { return "Gateway account unavailable; see $dash/dashboard" }
    return 'Gateway account: ' + $a + ' ' + [char]0x00B7 + " dashboard: $dash/dashboard"
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
  if (-not $warn) { [Console]::Out.Write("gateway status unavailable`n") }
  exit 0
}
