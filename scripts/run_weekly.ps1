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
# uv/git 등 native 명령은 진행상황을 stderr로 출력하는 경우가 많다.
# $ErrorActionPreference="Stop" 상태에서 2>&1로 stderr를 합치면
# 그 정상 출력(예: "Resolved 39 packages in 2ms")까지 터미널 오류로 처리돼
# 즉시 catch 블록으로 빠지는 PowerShell 특유의 함정이 있다.
# 이 헬퍼는 native 명령 실행 구간만 EAP를 Continue로 낮춰 그 문제를 피하고,
# 성공/실패 판정은 항상 $LASTEXITCODE로 한다.
function Invoke-Native([scriptblock]$Command) {
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command 2>&1 | ForEach-Object { Write-Host $_ }
    }
    finally {
        $ErrorActionPreference = $prevEAP
    }
}

try {
    # --- 환경 점검 ------------------------------------------------------------
    # 스케줄러 컨텍스트에는 사용자 PATH가 없을 수 있어 uv 위치를 보강한다.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    # 콘솔 출력이 파이프(2>&1 | ForEach-Object)로 리다이렉트되면 Python은 실제 콘솔이 아니라고 보고
    # 한국어 Windows 시스템 코드페이지(cp949)로 stdout을 인코딩한다. rich가 출력하는 em-dash(—) 같은
    # cp949에 없는 문자를 만나면 UnicodeEncodeError로 빌드가 죽는다. UTF-8을 강제해 이를 막는다.
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail 4 "uv를 찾을 수 없습니다 (PATH 확인)" }
    if (-not (Test-Path (Join-Path $RepoRoot ".env"))) { Fail 4 ".env가 없습니다 (OPENAI_API_KEY 필요)" }
    New-Item -ItemType Directory -Force -Path $DropDir | Out-Null

    Info "repo=$RepoRoot drop=$DropDir days=$Days limit=$Limit"

    # --- 의존성 동기화 ---------------------------------------------------------
    Invoke-Native { uv sync }
    if ($LASTEXITCODE -ne 0) { Fail 4 "uv sync 실패" }

    # --- 빌드 (기본값: sectioned + 소셜 신호 + 테마 + 썸네일 + PNG + 게시 zip) ---
    $buildStart = Get-Date
    Invoke-Native { uv run ai-newsletter build --days $Days --limit $Limit }
    if ($LASTEXITCODE -ne 0) { Fail 1 "build 실패 (로그 확인: $logFile)" }

    # --- 이번 실행이 만든 산출물 찾기 -------------------------------------------
    # @()로 배열 고정. 타임스탬프 필터가 (프로필/타이밍 등 환경 차이로) 비면
    # 가장 최근 산출물 폴더로 폴백해 mysterious null 크래시를 방어한다.
    $matching = @(Get-ChildItem (Join-Path $RepoRoot "outputs") -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "^\d{4}-\d{2}-\d{2}_weekly_ai_newsletter" })
    $outputDir = $matching |
        Where-Object { $_.LastWriteTime -ge $buildStart } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $outputDir) {
        $outputDir = $matching | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    }
    if (-not $outputDir -or [string]::IsNullOrEmpty($outputDir.FullName)) {
        Fail 3 "산출물 폴더를 찾지 못했습니다 (outputs 아래 매칭 폴더 $($matching.Count)개)"
    }
    Info "산출물: $($outputDir.Name)"

    # --- PNG 캡처 검증 ----------------------------------------------------------
    $manifestPath = Join-Path $outputDir.FullName "board\image_post\image_manifest.json"
    if ([string]::IsNullOrEmpty($manifestPath) -or -not (Test-Path -LiteralPath $manifestPath)) {
        Fail 2 "image_manifest.json 없음 — 캡처 단계 미실행 ($manifestPath)"
    }
    # Python은 manifest를 UTF-8로 쓴다. PS 5.1의 Get-Content 기본 인코딩(cp949)으로 읽으면
    # 한글 라벨이 깨지고 JSON 구조가 망가져 ConvertFrom-Json이 실패한다 → 반드시 UTF8로 읽는다.
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
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
        $report = Get-Content $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
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
    Write-Host "[weekly]   위치: 라인 $($_.InvocationInfo.ScriptLineNumber) - $($_.InvocationInfo.Line.Trim())" -ForegroundColor Red
    Write-Host "[weekly]   대상 명령: $($_.InvocationInfo.MyCommand)" -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 1
}
