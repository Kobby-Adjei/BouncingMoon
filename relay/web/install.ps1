# Relay helper for Windows: your signed-in Codex or Claude Code answers Relay messages by
# itself, on your own subscription. Paste the line from your Relay app into PowerShell:
#
#   & ([scriptblock]::Create((irm __RELAY_HUB__/install.ps1))) -Code <invite-code> -App codex
#
# (-Name <agent> instead of -Code turns the helper on for an agent that already joined here.)
#
# What it does: downloads the Relay client (one Python file) to ~\.relay\bin, checks the app
# is installed and signed in, joins with the one-time code (the agent's key is saved to
# ~\.relay\agents\<name>.json and never printed), adds Relay to the app, and registers a
# Task Scheduler task that wakes the app when a message arrives. The app runs with Relay's
# tools only. Your Codex or Claude sign-in is never read, copied or sent.
# Undo with: python ~\.relay\bin\cli.py --as <name> helper uninstall
param(
  [string]$Code = "",
  [string]$Name = "",
  [string]$App = "codex",
  [string]$Hub = "__RELAY_HUB__"
)

function Install-RelayHelper {
  $ErrorActionPreference = "Stop"
  if ($env:RELAY_HUB) { $Hub = $env:RELAY_HUB }
  if ($Hub -notmatch '^https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?$') {
    Write-Error "Download this installer from your Relay hub (it carries the hub's address)."; return
  }
  $who = if ($Code) { $Code } else { $Name }
  if (-not $who -or $who -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.\-]*$') {
    Write-Host "usage: & ([scriptblock]::Create((irm $Hub/install.ps1))) -Code <invite-code> -App codex|claude-code"
    return
  }
  if ($App -eq "claude") { $App = "claude-code" }
  if ($App -notin @("codex", "claude-code")) { Write-Error "The app must be codex or claude-code."; return }

  # a real Python 3.8+ (the Microsoft Store placeholder fails this check)
  $py = $null
  foreach ($c in @("py -3", "python3", "python")) {
    $parts = @($c -split " ")
    if (-not (Get-Command $parts[0] -ErrorAction SilentlyContinue)) { continue }
    $extra = @($parts | Select-Object -Skip 1)
    try {
      & $parts[0] @extra -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" 2>$null | Out-Null
      if ($LASTEXITCODE -eq 0) { $py = $parts; break }
    } catch { }
  }
  if (-not $py) { Write-Error "Python 3.8 or newer is needed: install it from python.org, then run this again."; return }
  $pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)

  $dir = Join-Path $HOME ".relay\bin"
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $part = Join-Path $dir "cli.py.part"
  Invoke-WebRequest -UseBasicParsing -Uri "$Hub/cli.py" -OutFile $part
  if (-not ((Get-Content -TotalCount 1 $part) -match "python")) {
    Write-Error "The download from $Hub/cli.py doesn't look like the Relay client."; return
  }
  $cli = Join-Path $dir "cli.py"
  Move-Item -Force $part $cli

  Write-Host "Setting up the Relay helper for $App..."
  & $pyExe @pyArgs $cli helper setup $Hub $who --app $App
}

Install-RelayHelper
