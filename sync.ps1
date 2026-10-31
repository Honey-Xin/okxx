# sync.ps1 - copy source dirs into this publish repo, then optionally push.
#
# This repo is a PUBLISH SNAPSHOT. The working tree lives at D:\dshwork
# (okx_quant / quant-panel / okx_trader) plus the DSH skill dir. Run this
# after changing those, so the published copy does not drift.
#
# Usage:
#   .\sync.ps1                       # copy + local commit
#   .\sync.ps1 -Push                 # copy + commit + push
#   .\sync.ps1 -Push -Message "fix"  # custom commit message
#
# Never copies: state/ data/ models/ __pycache__ node_modules .git
# Those hold account balances, positions, audit logs and downloaded bars.

param(
    [switch]$Push,
    [string]$Message = "chore: sync from working tree"
)

$ErrorActionPreference = "Stop"
$dst = $PSScriptRoot
$src = "D:\dshwork"
$skill = "C:\Users\Honey\.dsh\skills\crypto-quant"

$excludeDirs = @("state", "data", "models", "__pycache__", "node_modules", ".git", ".pytest_cache")
# Keep one-off debugging scripts out of the repo: _*.ps1 / dbg*.py / probe*.py
$excludeFiles = @("shot*.png", "preview.html", "_*.ps1", "_*.py", "dbg*.py", "probe*.py")

$pairs = @(
    @{ from = Join-Path $src "okx_quant";   to = Join-Path $dst "okx_quant" },
    @{ from = Join-Path $src "quant-panel"; to = Join-Path $dst "quant-panel" },
    @{ from = Join-Path $src "okx_trader";  to = Join-Path $dst "okx_trader" }
)

foreach ($p in $pairs) {
    if (-not (Test-Path $p.from)) { Write-Host "skip (missing): $($p.from)"; continue }
    robocopy $p.from $p.to /E /XD $excludeDirs /XF $excludeFiles /NFL /NDL /NJH /NJS /NP | Out-Null
    Write-Host "synced $($p.from)"
}

if (Test-Path $skill) {
    robocopy $skill (Join-Path $dst "skills\crypto-quant") /E /XD state __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
    Write-Host "synced $skill"
}

git -C $dst add -A
$diff = git -C $dst status --porcelain
if (-not $diff) {
    Write-Host "no changes"
    exit 0
}

$stat = ($diff | Measure-Object -Line).Lines
git -C $dst commit -q -m $Message
Write-Host "committed $stat changed file(s): $Message"

if ($Push) {
    git -C $dst push
    Write-Host "pushed to origin"
}
