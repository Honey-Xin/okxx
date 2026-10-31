# Interactive OKX credential setup -> writes to USER environment variables.
# Passwords are not echoed and never enter chat history.
#
# Usage: open PowerShell, run  .\setup_okx.ps1
#
# Security:
#   1. API Key permissions: enable READ + TRADE only. NEVER enable WITHDRAW.
#   2. Add an IP whitelist if possible.
#   3. Credentials land in user env vars, readable by any local process.
#
# NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads
# .ps1 files as ANSI/GBK by default, so non-ASCII text breaks the parser.

$ErrorActionPreference = "Stop"

function Read-SecretPlain([string]$Prompt) {
    $sec = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try {
        [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

Write-Host ""
Write-Host "=== OKX credential setup ===" -ForegroundColor Cyan
Write-Host "Input is hidden and never logged."
Write-Host ""

$key = Read-Host -Prompt "API Key"
$secret = Read-SecretPlain "API Secret"
$pass = Read-SecretPlain "API Passphrase (the one you set when creating the key)"

if ([string]::IsNullOrWhiteSpace($key) -or
    [string]::IsNullOrWhiteSpace($secret) -or
    [string]::IsNullOrWhiteSpace($pass)) {
    Write-Host ""
    Write-Host "Empty field detected. Aborted, nothing was changed." -ForegroundColor Red
    exit 1
}

[Environment]::SetEnvironmentVariable("OKX_API_KEY", $key.Trim(), "User")
[Environment]::SetEnvironmentVariable("OKX_API_SECRET", $secret.Trim(), "User")
[Environment]::SetEnvironmentVariable("OKX_API_PASSWORD", $pass.Trim(), "User")
[Environment]::SetEnvironmentVariable("EXCHANGE", "okx", "User")
[Environment]::SetEnvironmentVariable("PROXY", "http://127.0.0.1:7897", "User")
[Environment]::SetEnvironmentVariable("DRY_RUN", "1", "User")

Write-Host ""
Write-Host "Saved to user environment variables." -ForegroundColor Green
Write-Host ("  API Key length    : " + $key.Trim().Length)
Write-Host ("  API Secret length : " + $secret.Trim().Length)
Write-Host ("  Passphrase length : " + $pass.Trim().Length)
Write-Host "  DRY_RUN = 1 (dry-run mode, no real orders)" -ForegroundColor Yellow
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Open a NEW PowerShell window (env vars must reload)"
Write-Host "  2. cd D:\dshwork\okx_quant"
Write-Host "  3. python verify_okx.py"
