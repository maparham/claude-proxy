# Install or update claude-gateway, the client side of claude-proxy, for this Windows user (PowerShell 5.1 or later):
#
#   irm https://raw.githubusercontent.com/maparham/claude-proxy/master/install.ps1 | iex
#   irm https://claude-dash.example.com/install.ps1 | iex    # a gateway's own: this, then `on` authorizing in the browser
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/maparham/claude-proxy/master/install.ps1))) on --url https://claude.example.com --key sk-proxy-...
#
# It puts scripts\windows from the repository in %USERPROFILE%\.local\share\claude-gateway, replacing an earlier copy,
# and writes %USERPROFILE%\.local\bin\claude-gateway.cmd, the folder where Claude Code's own installer puts claude.exe,
# adding that folder to the user's PATH if it isn't there. Arguments then run as `claude-gateway ...`.
# Needs no admin rights, Python or Git. CLAUDE_GATEWAY_REPO (owner/name) and CLAUDE_GATEWAY_REF (branch or tag) pick
# another source; CLAUDE_GATEWAY_ZIP gives the archive directly (a URL or a local file); CLAUDE_GATEWAY_BIN another
# folder for claude-gateway.cmd (then PATH is left to you).
# It runs inside the caller's own PowerShell (irm | iex), so it never calls exit: that would close their window. A
# failure is a throw instead: an interactive session just shows it, `powershell -File install.ps1` exits 1 with it, and
# gclaude update wraps its `irm ... | iex` in try/catch to exit 1 (Windows PowerShell's -Command exits 0 when the
# throw happens inside iex).

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
  if ($enc.GetString($bytes) -ne $Text) { throw "$Path would need letters this console's code page lacks; set CLAUDE_GATEWAY_BIN to a plainer folder" }
  [IO.File]::WriteAllBytes($Path, $bytes)
}

function Add-UserPath([string]$Dir) {
  # Straight from the registry and back as REG_EXPAND_SZ: [Environment]'s Path comes expanded, and writing that back
  # would freeze entries like %USERPROFILE%\AppData\Local\Microsoft\WindowsApps to today's values.
  $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
  try {
    $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $parts = @($raw -split ';' | Where-Object { $_ })
    $known = @($parts | ForEach-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') })
    if ($known -contains $Dir.TrimEnd('\')) { return $false }
    $key.SetValue('Path', (@($parts) + $Dir) -join ';', [Microsoft.Win32.RegistryValueKind]::ExpandString)
  } finally { $key.Close() }
  [Environment]::SetEnvironmentVariable('ClaudeGatewayPathChanged', $null, 'User')   # tells Explorer, so new terminals see it
  return $true
}

function Install-ClaudeGateway {
  param([string[]]$Rest)
  $ErrorActionPreference = 'Stop'
  $ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest's progress bar makes 5.1 downloads crawl
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

  $home_ = $env:USERPROFILE
  if (-not $home_) { throw 'install.ps1: USERPROFILE is not set' }
  $repo = if ($env:CLAUDE_GATEWAY_REPO) { $env:CLAUDE_GATEWAY_REPO } else { 'maparham/claude-proxy' }
  $ref = if ($env:CLAUDE_GATEWAY_REF) { $env:CLAUDE_GATEWAY_REF } else { 'master' }
  $zip = if ($env:CLAUDE_GATEWAY_ZIP) { $env:CLAUDE_GATEWAY_ZIP } else { "https://codeload.github.com/$repo/zip/$ref" }
  $share = Join-Path $home_ '.local\share\claude-gateway'
  $bin = if ($env:CLAUDE_GATEWAY_BIN) { $env:CLAUDE_GATEWAY_BIN } else { Join-Path $home_ '.local\bin' }
  $launcher = Join-Path $bin 'claude-gateway.cmd'
  $mark = 'rem Installed by claude-gateway install.ps1.'

  if ((Test-Path -LiteralPath $launcher) -and -not (Select-String -LiteralPath $launcher -SimpleMatch $mark -Quiet)) {
    throw "install.ps1: $launcher already exists and install.ps1 did not write it; left as it is."
  }

  $tmp = Join-Path ([IO.Path]::GetTempPath()) ('claude-gateway-' + [Guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $tmp | Out-Null
  try {
    $archive = Join-Path $tmp 'source.zip'
    try {
      if (Test-Path -LiteralPath $zip) { Copy-Item -LiteralPath $zip -Destination $archive }
      else { Invoke-WebRequest -UseBasicParsing -Uri $zip -OutFile $archive }
      Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $tmp 'src')
    } catch {
      throw "install.ps1: could not get $zip ($($_.Exception.Message))"
    }
    $top = Get-ChildItem -LiteralPath (Join-Path $tmp 'src') -Directory | Select-Object -First 1
    $windows = if ($top) { Join-Path $top.FullName 'scripts\windows' } else { '' }
    if (-not $top -or -not (Test-Path -LiteralPath (Join-Path $windows 'claude-gateway.ps1'))) {
      throw "install.ps1: $zip has no scripts\windows\claude-gateway.ps1"
    }

    # Build the new copy beside the old one, then swap, so a failed download never leaves half an install.
    $new = "$share.new"
    if (Test-Path -LiteralPath $new) { Remove-Item -LiteralPath $new -Recurse -Force }
    New-Item -ItemType Directory -Path (Join-Path $new 'scripts') | Out-Null
    Copy-Item -LiteralPath $windows -Destination (Join-Path $new 'scripts\windows') -Recurse
    if (Test-Path -LiteralPath $share) { Remove-Item -LiteralPath $share -Recurse -Force }
    Move-Item -LiteralPath $new -Destination $share
  } finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
  }

  $script = Join-Path $share 'scripts\windows\claude-gateway.ps1'
  New-Item -ItemType Directory -Path $bin -Force | Out-Null
  $cmd = "@echo off`r`n$mark`r`nrem Runs claude-gateway.ps1, which the execution policy would otherwise block.`r`n" +
         "powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Cmd-Path $script)`" %*`r`nexit /b %ERRORLEVEL%`r`n"
  try { Write-Cmd $launcher $cmd } catch { throw "install.ps1: $($_.Exception.Message)" }
  Write-Host "claude-gateway is installed in $share ($launcher)."

  if (-not $env:CLAUDE_GATEWAY_BIN) {
    if (Add-UserPath $bin) { Write-Host "Added $bin to your PATH; other terminals see it once reopened." }
    if (($env:Path -split ';') -notcontains $bin) { $env:Path = "$env:Path;$bin" }
  }

  if ($Rest -and $Rest.Count -gt 0) {
    # A child process, so that its exit can't close this window. Its own message is printed already; the arguments
    # aren't repeated, as they may hold a key.
    & powershell -NoProfile -ExecutionPolicy Bypass -File $script @Rest
    if ($LASTEXITCODE -ne 0) { throw "install.ps1: claude-gateway $($Rest[0]) failed (exit code $LASTEXITCODE); see above." }
  } else {
    Write-Host "Next: claude-gateway on --url https://claude.example.com   (your gateway's address)"
    Write-Host "After an update, run 'claude-gateway on' again to refresh what it set up."
  }
}

Install-ClaudeGateway -Rest $args
