<#
.SYNOPSIS
  주간 AI 뉴스레터 자동 빌드 + 내부망 반입 zip 투하 스크립트.

.DESCRIPTION
  Windows 작업 스케줄러(또는 UiPath)에 등록해 주 1회 실행합니다.
  흐름: uv sync → build(수집+LLM+렌더+PNG+게시 zip) → PNG 성공 검증
       → UiPath 감시 폴더($DropDir)에 zip을 원자적으로 복사(.tmp → 개명)
       → zip과 같은 이름의 .done 마커 생성 (UiPath는 .done을 보고 집어감)

  종료코드: 0=성공, 1=빌드 실패, 2=PNG 캡처 불완전, 3=게시 zip 없음, 4=환경 문제

.EXAMPLE
  작업 스케줄러 등록 (매주 월요일 07:00):
    schtasks /create /tn "AI_Weekly_Newsletter" ^
      /tr "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\KOSCOM\workplace\ai-newsletter-automation\scripts\run_weekly.ps1" ^
      /sc weekly /d MON /st 07:00
#>

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    # UiPath가 감시하는 투하 폴더 (망간 전송 대상)
    [string]$DropDir = "C:\newsletter_outbox",
    [int]$Days = 7,
    [int]$Limit = 10,
    # 11장(메인1+기사10) 중 이 개수 미만이면 캡처 불완전으로 실패 처리
    [int]$MinPngCount = 11
)

$ErrorActionPreference = "Stop"

# --- 경로/로그 준비 ----------------------------------------------------------
if (-not $RepoRoot) {
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
    $RepoRoot = Split-Path -Parent $scriptDir
}
Set-Location $RepoRoot

$logDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = Join-Path $logDir "weekly_$stamp.log"
Start-Transcript -Path $logFile | Out-Null

function Info($m) { Write-Host "[weekly] $m" }
function Fail($code, $m) {
    Write-Host "[weekly] FAIL($code): $m" -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit $code
}

try {
    # --- 환경 점검 ------------------------------------------------------------
    # 스케줄러 컨텍스트에는 사용자 PATH가 없을 수 있어 uv 위치를 보강한다.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail 4 "uv를 찾을 수 없습니다 (PATH 확인)" }
    if (-not (Test-Path (Join-Path $RepoRoot ".env"))) { Fail 4 ".env가 없습니다 (OPENAI_API_KEY 필요)" }
    New-Item -ItemType Directory -Force -Path $DropDir | Out-Null

    Info "repo=$RepoRoot drop=$DropDir days=$Days limit=$Limit"

    # --- 의존성 동기화 ---------------------------------------------------------
    uv sync 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { Fail 4 "uv sync 실패" }

    # --- 빌드 (기본값: sectioned + 소셜 신호 + 테마 + 썸네일 + PNG + 게시 zip) ---
    $buildStart = Get-Date
    uv run ai-newsletter build --days $Days --limit $Limit 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { Fail 1 "build 실패 (로그 확인: $logFile)" }

    # --- 이번 실행이 만든 산출물 찾기 -------------------------------------------
    $outputDir = Get-ChildItem (Join-Path $RepoRoot "outputs") -Directory |
        Where-Object { $_.Name -match "^\d{4}-\d{2}-\d{2}_weekly_ai_newsletter" -and $_.LastWriteTime -ge $buildStart } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $outputDir) { Fail 3 "이번 실행의 산출물 폴더를 찾지 못했습니다" }
    Info "산출물: $($outputDir.Name)"

    # --- PNG 캡처 검증 ----------------------------------------------------------
    $manifestPath = Join-Path $outputDir.FullName "board\image_post\image_manifest.json"
    if (-not (Test-Path $manifestPath)) { Fail 2 "image_manifest.json 없음 — 캡처 단계 미실행" }
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    $created = @($manifest | Where-Object { $_.created }).Count
    Info "PNG 캡처: $created/$($manifest.Count)"
    if ($created -lt $MinPngCount) {
        $firstFail = ($manifest | Where-Object { -not $_.created } | Select-Object -First 1).message
        Fail 2 "PNG 캡처 불완전 ($created/$MinPngCount) — $firstFail"
    }

    # --- 게시 zip 찾기 ----------------------------------------------------------
    $zip = Get-ChildItem (Join-Path $RepoRoot "outputs\publish_ready") -Recurse -Filter "ai_weekly_*_publish_package.zip" |
        Where-Object { $_.LastWriteTime -ge $buildStart } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $zip) { Fail 3 "게시 zip을 찾지 못했습니다 (outputs\publish_ready)" }
    Info "게시 zip: $($zip.FullName) ($([math]::Round($zip.Length/1MB,1)) MB)"

    # --- 원자적 투하: .tmp로 복사 후 개명 → 마지막에 .done 마커 -------------------
    # UiPath는 '<zip이름>.done' 파일이 생긴 뒤에만 zip을 집어가면 안전하다.
    $finalPath = Join-Path $DropDir $zip.Name
    $tmpPath = "$finalPath.tmp"
    if (Test-Path $tmpPath) { Remove-Item $tmpPath -Force }
    Copy-Item $zip.FullName $tmpPath
    if (Test-Path $finalPath) { Remove-Item $finalPath -Force }   # 같은 주 재실행 시 교체
    Rename-Item $tmpPath $zip.Name
    Set-Content -Path "$finalPath.done" -Value (Get-Date -Format "o") -Encoding Ascii
    Info "투하 완료: $finalPath (+ .done 마커)"

    # grounding 경고가 있으면 로그에 남겨 사람 검토를 유도
    $reportPath = Join-Path $outputDir.FullName "data\generation_report.json"
    if (Test-Path $reportPath) {
        $report = Get-Content $reportPath -Raw | ConvertFrom-Json
        $flagCount = @($report.grounding_flags).Count
        if ($flagCount -gt 0) {
            Info "주의: grounding_flags $flagCount 건 — 게시 전 원문 대조 권장 ($reportPath)"
        }
    }

    Info "성공"
    Stop-Transcript | Out-Null
    exit 0
}
catch {
    Write-Host "[weekly] 예기치 못한 오류: $($_.Exception.Message)" -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 1
}
