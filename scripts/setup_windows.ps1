<#
.SYNOPSIS
  Windows / corporate-network setup helper for ai-newsletter-automation.

.DESCRIPTION
  FALLBACK helper. The app now trusts the OS certificate store at runtime via
  the `truststore` dependency, and `UV_SYSTEM_CERTS` is set by the bootstrap
  script, so you usually do NOT need to run this. Run it only if TLS/SSL errors
  persist behind an SSL-inspecting corporate proxy (e.g. a DLP root CA): it
  patches the venv certifi bundle as a belt-and-suspenders fallback.
  Safe to run repeatedly (idempotent) and backs up the certifi bundle.

  Run AFTER `uv sync` has created the .venv:
      powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1

.NOTES
  Steps performed:
    1. Set UV_SYSTEM_CERTS=true (user env) so uv trusts the OS cert store.
    2. Append Windows root CAs (incl. any corporate DLP CA) to the venv's
       certifi bundle so httpx / the OpenAI SDK trust them.
    3. Verify OpenAI TLS connectivity and report browser availability.
#>

[CmdletBinding()]
param(
    [string]$RepoRoot
)

$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[setup] $m" }
function Warn($m) { Write-Host "[setup] WARNING: $m" -ForegroundColor Yellow }

# Resolve the repo root robustly (param default can't rely on $PSScriptRoot).
if (-not $RepoRoot) {
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot }
                 elseif ($PSCommandPath) { Split-Path -Parent $PSCommandPath }
                 else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
    $RepoRoot = Split-Path -Parent $scriptDir
}

Info "Repo root: $RepoRoot"

# --- locate the venv python -------------------------------------------------
$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Warn "'.venv' not found. Run 'uv sync' first, then re-run this script."
    exit 1
}

# --- 1. UV_SYSTEM_CERTS ------------------------------------------------------
if ([Environment]::GetEnvironmentVariable("UV_SYSTEM_CERTS", "User") -ne "true") {
    [Environment]::SetEnvironmentVariable("UV_SYSTEM_CERTS", "true", "User")
    Info "Set UV_SYSTEM_CERTS=true (user). New terminals pick this up automatically."
} else {
    Info "UV_SYSTEM_CERTS already set."
}
$env:UV_SYSTEM_CERTS = "true"

# --- 2. append Windows root CAs to certifi ----------------------------------
$certifi = (& $venvPython -c "import certifi; print(certifi.where())").Trim()
if (-not (Test-Path $certifi)) {
    Warn "certifi bundle not found at '$certifi'."
    exit 1
}
$backup = "$certifi.orig"
if (-not (Test-Path $backup)) {
    Copy-Item $certifi $backup
    Info "Backed up original certifi bundle -> $backup"
}

$marker = "# ai-newsletter: windows-root-cas"
if (Select-String -Path $certifi -SimpleMatch $marker -Quiet) {
    Info "certifi bundle already patched with Windows root CAs. Skipping."
} else {
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine($marker)
    $seen = @{}
    $count = 0
    foreach ($c in (Get-ChildItem Cert:\LocalMachine\Root) + (Get-ChildItem Cert:\CurrentUser\Root)) {
        if ($seen.ContainsKey($c.Thumbprint)) { continue }
        $seen[$c.Thumbprint] = $true
        $count++
        $b64 = [Convert]::ToBase64String($c.RawData, [Base64FormattingOptions]::InsertLineBreaks)
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine("# $($c.Subject)")
        [void]$sb.AppendLine("-----BEGIN CERTIFICATE-----")
        [void]$sb.AppendLine($b64)
        [void]$sb.AppendLine("-----END CERTIFICATE-----")
    }
    Add-Content -Path $certifi -Value $sb.ToString() -Encoding Ascii
    Info "Appended $count Windows root CAs to certifi bundle."
    $dlp = $seen.Keys | ForEach-Object { $_ }  # already deduped; just note count
}

# --- 3. verify connectivity + browser ---------------------------------------
Info "Verifying OpenAI TLS connectivity (HTTP 401 = cert OK, just no auth header)..."
try {
    $status = (& $venvPython -c "import httpx; print(httpx.get('https://api.openai.com/v1/models', timeout=15).status_code)").Trim()
    if ($status -eq "401" -or $status -eq "200") {
        Info "OpenAI TLS OK (HTTP $status)."
    } else {
        Warn "Unexpected HTTP $status from OpenAI — check network/proxy."
    }
} catch {
    Warn "OpenAI connectivity check failed: $($_.Exception.Message)"
}

$browsers = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ }
if ($browsers) {
    Info "Browser for image capture found: $($browsers[0])"
} else {
    Warn "No Chrome/Edge found. Screenshot image capture will be skipped (og:image still works)."
}

# --- 4. playwright chromium (PNG capture, OPTIONAL) ---------------------------
# PNG 캡처는 설치된 Chrome/Edge를 우선 사용하므로 이 다운로드는 선택 사항입니다.
# 사내 SSL 프록시에서는 다운로드가 막힐 수 있어, 패치된 certifi 번들을 Node가
# 신뢰하도록 NODE_EXTRA_CA_CERTS를 지정해 시도합니다. 실패해도 무해합니다.
if ($browsers) {
    Info "Chrome/Edge found — skipping playwright chromium download (system browser is used for capture)."
} else {
    Info "No Chrome/Edge — attempting playwright chromium download (~150MB)..."
    try {
        $env:NODE_EXTRA_CA_CERTS = $certifi
        & $venvPython -m playwright install chromium
        Info "playwright chromium ready."
    } catch {
        Warn "playwright chromium install failed: $($_.Exception.Message)"
        Warn "Install Chrome or Edge instead — capture uses the system browser."
    }
}

Info "Done. Next: copy .env.example to .env, fill OPENAI_API_KEY, then 'uv run ai-newsletter build'."
