<#
.SYNOPSIS
  Cold-start bootstrap for a FRESH Windows PC (nothing installed, not even Python).

.DESCRIPTION
  Installs the toolchain and configures the shell/PATH/SSL so that a brand-new
  Windows machine can clone and run this project without the trial-and-error we
  hit the first time. Safe to re-run (idempotent): tools already present are
  detected and skipped.

  What it does:
    1. Verifies winget (App Installer) is available.
    2. Installs Git, GitHub CLI, and uv from the 'winget' source.
    3. Refreshes PATH in the current session so the new commands work immediately
       (winget updates PATH but the running shell doesn't see it otherwise).
    4. Configures Git to use the Windows certificate store (schannel) so HTTPS
       clone works behind a corporate SSL-inspecting proxy.
    5. Sets UV_SYSTEM_CERTS=true so uv trusts the OS cert store.
    6. Prints the exact next steps (gh auth -> clone -> uv sync -> setup_windows.ps1).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\bootstrap_windows.ps1

.NOTES
  If you have ONLY this file (repo not cloned yet), download it from GitHub
  (raw) and run it the same way; it will install the toolchain, then tell you
  how to clone.
#>

[CmdletBinding()]
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[bootstrap] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[bootstrap] OK: $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[bootstrap] WARNING: $m" -ForegroundColor Yellow }

function Update-SessionPath {
    # winget writes to the persisted PATH, but the current process keeps its old
    # copy. Rebuild $env:Path from Machine + User so new tools resolve right away.
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (($machine, $user) -join ";")
}

function Ensure-Tool {
    param([string]$Command, [string]$WingetId, [string]$Nice)
    if (Get-Command $Command -ErrorAction SilentlyContinue) {
        Ok "$Nice already installed ($((Get-Command $Command).Source))"
        return
    }
    if ($SkipInstall) { Warn "$Nice missing and -SkipInstall set. Install '$WingetId' manually."; return }
    Info "Installing $Nice ($WingetId) via winget..."
    winget install --id $WingetId --source winget --silent `
        --accept-package-agreements --accept-source-agreements
    Update-SessionPath
    if (Get-Command $Command -ErrorAction SilentlyContinue) {
        Ok "$Nice installed."
    } else {
        Warn "$Nice installed but '$Command' not on PATH yet. Open a NEW terminal after this script."
    }
}

# --- 0. environment sanity --------------------------------------------------
Info "PowerShell $($PSVersionTable.PSVersion) on $([Environment]::OSVersion.VersionString)"
$policy = Get-ExecutionPolicy -Scope CurrentUser
Info "CurrentUser execution policy: $policy"
if ($policy -in @("Restricted", "AllSigned", "Undefined")) {
    Warn "Scripts may be blocked. This run used -ExecutionPolicy Bypass, but to run repo"
    Warn "scripts normally, consider: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
}

# --- 1. winget present? -----------------------------------------------------
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Warn "winget (App Installer) not found. Install 'App Installer' from the Microsoft Store,"
    Warn "or update Windows, then re-run this script."
    exit 1
}
Ok "winget present."

# --- 2/3. install toolchain + refresh PATH ----------------------------------
Ensure-Tool -Command "git" -WingetId "Git.Git"      -Nice "Git"
Ensure-Tool -Command "gh"  -WingetId "GitHub.cli"   -Nice "GitHub CLI"
Ensure-Tool -Command "uv"  -WingetId "astral-sh.uv" -Nice "uv"
Update-SessionPath

# --- 4. Git: trust the Windows cert store (corporate SSL) --------------------
if (Get-Command git -ErrorAction SilentlyContinue) {
    $backend = (git config --global http.sslBackend) 2>$null
    if ($backend -ne "schannel") {
        git config --global http.sslBackend schannel
        Ok "Set git http.sslBackend=schannel (uses Windows cert store for HTTPS)."
    } else {
        Ok "git already using schannel."
    }
}

# --- 5. uv: trust the OS cert store -----------------------------------------
if ([Environment]::GetEnvironmentVariable("UV_SYSTEM_CERTS", "User") -ne "true") {
    [Environment]::SetEnvironmentVariable("UV_SYSTEM_CERTS", "true", "User")
    $env:UV_SYSTEM_CERTS = "true"
    Ok "Set UV_SYSTEM_CERTS=true (user env)."
} else {
    Ok "UV_SYSTEM_CERTS already set."
}

# --- 6. next steps ----------------------------------------------------------
Info "-------------------------------------------------------------"
Info "Toolchain ready. Versions:"
foreach ($t in @("git", "gh", "uv")) {
    $c = Get-Command $t -ErrorAction SilentlyContinue
    if ($c) { Write-Host ("  {0,-4} {1}" -f $t, (& $t --version | Select-Object -First 1)) }
}
Info "-------------------------------------------------------------"
Info "NEXT STEPS:"
Info "  1) (private repo) authenticate GitHub - token method is most reliable:"
Info "       - create a classic token with scopes: repo, read:org"
Info "       - echo <TOKEN> | gh auth login --with-token ; gh auth status"
Info "  2) clone:   gh repo clone <org>/ai-newsletter-automation ; cd ai-newsletter-automation"
Info "  3) deps:    uv sync"
Info "  4) certs:   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1"
Info "  5) keys:    copy .env.example .env ; notepad .env   (set OPENAI_API_KEY)"
Info "  6) verify:  uv run ai-newsletter sample"
Info "See docs/INSTALL.md for the full walkthrough and troubleshooting."
