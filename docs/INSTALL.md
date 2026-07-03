# 설치 매뉴얼 (Mac / Windows 공통)

이 프로젝트는 **Mac에서 최초 개발**되었고 **Windows(사내 PC)에서 추가 개발·운영**됩니다.
이 문서는 두 환경 모두에서, **처음 접하는 사람도 그대로 따라 하면 설치가 끝나도록** 정리한 것입니다.
운영(뉴스레터 생성·게시) 절차는 [WINDOWS_UIPATH_RUNBOOK.md](WINDOWS_UIPATH_RUNBOOK.md)를 보세요.

> 이 프로젝트의 유일한 필수 도구는 **`uv`** 하나입니다. `uv`가 Python 3.11까지 알아서 받아옵니다.
> 이미지 캡처를 쓰려면 **Chrome 또는 Edge**가 추가로 필요합니다.

---

## 0. 한눈에 보기

| 단계 | Mac | Windows |
|---|---|---|
| 코드 받기 | `gh` 또는 `git clone` | `gh` 또는 `git clone` |
| uv 설치 | `brew install uv` 또는 공식 스크립트 | `winget install --id astral-sh.uv --source winget` |
| 의존성 | `uv sync` | `uv sync`(사내망이면 아래 SSL 절차 먼저) |
| 사내망 SSL | 보통 불필요 | **필요** — `UV_SYSTEM_CERTS` + certifi 보정 |
| 이미지용 브라우저 | Chrome(기본 경로 자동 인식) | Chrome/Edge(코드가 경로 자동 탐색) |
| API 키 | `.env` | `.env` |

**사내망(KOSCOM 등 SSL 검사 프록시 환경) Windows 사용자는 [4. 사내망 SSL](#4-사내망-ssl-회사-네트워크-필수)을 반드시 먼저 읽으세요.** 이걸 건너뛰면 `uv sync`와 OpenAI 호출이 인증서 오류로 실패합니다.

---

## 0.5 Windows 완전 초기 PC — 콜드스타트 (아무것도 없는 경우 여기부터)

> **파이썬도, git도, 아무것도 안 깔린 새 Windows PC**를 기준으로 한 절차입니다. 순서를 그대로 지키면 시행착오가 없습니다.
> 아래 1~9번 상세 절차의 준비 단계를 **하나의 스크립트로 자동화**한 것이 `scripts/bootstrap_windows.ps1`입니다.

### 반드시 지킬 3가지 (여기서 대부분 애를 먹습니다)

1. **`cmd`가 아니라 `PowerShell`을 쓰세요.**
   - 시작 메뉴 → "PowerShell" → 실행. (이 매뉴얼의 명령은 PowerShell 기준입니다.)
   - `cmd`에서 `& "...\gh.exe"` 같은 명령은 `&은(는) 예상되지 않았습니다` 오류가 납니다. `&`는 PowerShell 전용입니다.

2. **실행 정책(ExecutionPolicy)** — 스크립트 실행이 기본 차단돼 있습니다. 둘 중 하나:
   - 매번: `powershell -ExecutionPolicy Bypass -File <스크립트>` (권장, 시스템 설정 안 바꿈)
   - 영구: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (한 번만)

3. **winget으로 설치한 직후엔 그 창에서 명령이 안 잡힙니다 (PATH 미갱신).**
   - 가장 확실한 해결: **새 PowerShell 창을 연다.**
   - 창을 유지해야 하면 아래로 현재 세션 PATH를 갱신:
     ```powershell
     $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
     ```
   - `bootstrap_windows.ps1`은 이 PATH 갱신을 자동으로 처리합니다.

### 원스텝: 부트스트랩 스크립트

이 스크립트가 **git · GitHub CLI · uv 설치 + PATH 갱신 + git/uv의 사내 SSL 설정**까지 한 번에 합니다. 멱등(여러 번 실행해도 안전)입니다.

**저장소를 이미 받았다면** (또는 스크립트 파일이 손에 있다면):
```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_windows.ps1
```

**아직 아무것도 없다면** (git이 없어 clone도 못 하는 상태) — 스크립트 파일 하나만 먼저 확보:
1. 브라우저로 레포의 `scripts/bootstrap_windows.ps1` 열기 → **Raw** → 다른 이름으로 저장 (예: `C:\bootstrap_windows.ps1`)
2. 실행:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\bootstrap_windows.ps1
   ```
3. 끝나면 스크립트가 출력한 **NEXT STEPS**(gh 인증 → clone → uv sync → setup_windows.ps1)를 따릅니다.

부트스트랩이 하는 일:
- winget 존재 확인
- `git` / `gh` / `uv` 설치 (이미 있으면 건너뜀)
- 현재 세션 PATH 갱신 (설치 직후 바로 사용 가능)
- **git이 Windows 인증서 저장소를 쓰도록** `http.sslBackend=schannel` 설정 → 사내망에서 HTTPS clone 성공
- **`UV_SYSTEM_CERTS=true`** 설정 → uv 다운로드 SSL 통과
- 다음 단계 안내 출력

부트스트랩 이후에는 아래 **2번(gh 인증·clone) → 5번(uv sync) → 6번(.env) → 8번(검증)** 순서로 진행하면 됩니다. 앱의 파이썬 TLS(수집·OpenAI)는 `truststore`로 자동 처리되므로 **4번의 certifi 수동 패치(`setup_windows.ps1`)는 대개 필요 없습니다** — SSL 오류가 계속될 때만 폴백으로 실행하세요.

---

## 1. 사전 준비물

### 공통
- **Git**
- **OpenAI API 키** (LLM 편집·편집자 선별 모드에 필요. 없으면 `--no-use-llm`으로 제한 실행 가능)
- **Chrome 또는 Edge** (기사 대표 이미지가 없을 때 스크린샷으로 보충 — 없으면 이미지 일부 누락)

### Mac
- Homebrew 권장

### Windows
- **winget** (Windows 10/11 기본 포함)
- 저장소가 비공개면 **GitHub CLI(`gh`)** — 아래 2번에서 설치

---

## 2. 코드 받기

> **사내망에서 `git clone`이 SSL 오류(`SSL certificate problem`)로 실패하면**, git이 Windows 인증서 저장소를 쓰도록 한 번 설정하세요 (부트스트랩 스크립트는 자동 적용):
> ```powershell
> git config --global http.sslBackend schannel
> ```

### 공개 저장소
```bash
git clone https://github.com/<org>/ai-newsletter-automation.git
cd ai-newsletter-automation
```

### 비공개 저장소 (GitHub 인증 필요)

`gh`가 없으면 설치합니다.

- **Mac**: `brew install gh`
- **Windows**: `winget install --id GitHub.cli --source winget`
  > `--source winget`을 꼭 붙이세요. 생략하면 msstore 소스의 인증서 오류(`0x8a15005e`)가 납니다.

로그인 — **웹 브라우저 방식이 사내망에서 자주 막히므로 토큰 방식을 권장**합니다:

1. https://github.com/settings/tokens → **Generate new token (classic)**
2. scope **두 개**를 체크: **`repo`**, **`read:org`**  ← `read:org`이 없으면 `missing required scope 'read:org'` 오류
3. 생성된 토큰으로 로그인:
   ```bash
   echo <YOUR_TOKEN> | gh auth login --with-token
   gh auth status          # "Logged in ..." 확인
   ```
4. 클론:
   ```bash
   gh repo clone <org>/ai-newsletter-automation
   cd ai-newsletter-automation
   ```

> ⚠️ 토큰은 비밀번호와 같습니다. 채팅·이슈·커밋 등에 노출하지 말고, 노출됐다면 즉시 [토큰 페이지](https://github.com/settings/tokens)에서 Revoke 하세요.

---

## 3. uv 설치

### Mac
```bash
brew install uv
# 또는
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Windows
```powershell
winget install --id astral-sh.uv --source winget --accept-package-agreements --accept-source-agreements
```
설치 후 **새 터미널**을 열어야 `uv`가 PATH에 잡힙니다.

확인:
```bash
uv --version
```

---

## 4. 사내망 SSL (회사 네트워크 — Windows 필수)

회사 네트워크가 SSL을 검사(DLP/프록시)하면, 파이썬·uv가 회사 루트 인증서를 몰라서
`invalid peer certificate: UnknownIssuer` / `APIConnectionError` / `CERTIFICATE_VERIFY_FAILED (self-signed certificate in certificate chain)` 로 실패합니다. **가정용/일반 인터넷에서는 이 절이 필요 없습니다.**

> **앱의 파이썬 TLS(수집·OpenAI)는 이제 자동 처리됩니다.** 의존성에 `truststore`가 포함되어 있어, CLI가 시작할 때 Windows 인증서 저장소를 그대로 사용합니다. 따라서 `uv sync`가 certifi를 새로 설치해도(파이썬 버전 교체 등) OpenAI/수집 호출은 계속 회사 CA를 신뢰합니다 — **아래 4-2 certifi 수동 패치는 이제 폴백일 뿐**입니다. 남는 필수 설정은 **4-1(uv 다운로드용 `UV_SYSTEM_CERTS`)** 과, clone용 **git `schannel`**([2번](#2-코드-받기)) 입니다.

### 폴백 스크립트 (필요 시)
보통은 `truststore`가 앱 TLS를 처리하고, `UV_SYSTEM_CERTS`는 부트스트랩에서 설정되므로 **이 스크립트를 실행할 필요가 없습니다.** 그래도 SSL 오류가 남으면, 저장소의 스크립트가 4-1·4-2를 한 번에 처리합니다. **`uv sync`로 `.venv`를 만든 뒤** 실행하세요:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
```
> 이 스크립트는 되돌릴 수 있게 원본 인증서 번들을 `cacert.pem.orig`로 백업합니다.

### 수동 (스크립트를 못 쓸 때)

**4-1. uv가 OS 인증서를 쓰도록** (다운로드 SSL 오류 해결):
```powershell
setx UV_SYSTEM_CERTS true
# 현재 창에도 즉시 적용
$env:UV_SYSTEM_CERTS = "true"
```

**4-2. 파이썬 라이브러리(httpx/OpenAI)가 회사 CA를 신뢰하도록** — `.venv`의 certifi 번들에 Windows 루트 CA를 추가:
```powershell
$certifi = (uv run python -c "import certifi; print(certifi.where())")
Copy-Item $certifi "$certifi.orig" -ErrorAction SilentlyContinue   # 백업
$sb = New-Object System.Text.StringBuilder
$seen = @{}
foreach ($c in (Get-ChildItem Cert:\LocalMachine\Root) + (Get-ChildItem Cert:\CurrentUser\Root)) {
  if ($seen.ContainsKey($c.Thumbprint)) { continue }; $seen[$c.Thumbprint] = $true
  $b64 = [Convert]::ToBase64String($c.RawData, 'InsertLineBreaks')
  [void]$sb.AppendLine("`n# $($c.Subject)`n-----BEGIN CERTIFICATE-----`n$b64`n-----END CERTIFICATE-----")
}
Add-Content -Path $certifi -Value $sb.ToString() -Encoding Ascii
```

**연결 확인** (HTTP 401 이 나오면 인증서 검증은 통과한 것 — 정상):
```powershell
uv run python -c "import httpx; print(httpx.get('https://api.openai.com/v1/models', timeout=15).status_code)"
```

> 참고: `uv sync`가 certifi를 재설치하면 4-2가 지워집니다. 그때 OpenAI 연결이 다시 막히면 스크립트를 다시 실행하세요.

---

## 5. 의존성 설치 (`uv sync`)

```bash
uv sync
```

### Windows에서 "Missing expected target directory for Python minor version link" 오류가 나면
uv가 관리 Python의 버전 링크(심볼릭 링크)를 만들려다 **Windows 권한**에 막힌 것입니다. 둘 중 하나로 해결:

- **방법 A (간단):** 이미 받아진 python.exe를 직접 지정
  ```powershell
  $py = "$env:APPDATA\uv\python\cpython-3.11*\python.exe"
  $py = (Resolve-Path $py | Select-Object -First 1).Path
  $env:UV_PYTHON_DOWNLOADS = "never"
  uv venv --python $py
  uv sync --python $py
  ```
- **방법 B (근본):** Windows **개발자 모드**를 켠 뒤(설정 → 개인정보 및 보안 → 개발자용) `uv sync` 재시도. 개발자 모드는 비관리자 심볼릭 링크 생성을 허용합니다.

---

## 6. API 키 (`.env`)

```bash
cp .env.example .env      # Windows: copy .env.example .env
```
`.env`를 열어 채웁니다:
```text
OPENAI_API_KEY=sk-...        # 실제 키
OPENAI_MODEL=gpt-5.4
CRITIC_MODEL=gpt-5.4-mini
# 선택(레이트리밋·메타데이터 개선): GITHUB_TOKEN=, HF_TOKEN=
```
`.env`는 절대 커밋하지 않습니다(`.gitignore`에 포함).

---

## 7. 이미지 캡처용 브라우저

기사에 대표 이미지(og:image)가 없으면 **헤드리스 브라우저 스크린샷**으로 보충합니다.
설치돼 있으면 코드가 아래 순서로 **자동 탐색**하므로 별도 설정이 필요 없습니다.

- **Mac**: `/Applications/Google Chrome.app` (또는 Chromium/Edge)
- **Windows**: Chrome(`C:\Program Files (x86)\Google\Chrome\...`) 또는 **Edge**(사내 기본 설치)

브라우저가 전혀 없으면 og:image가 있는 기사만 이미지가 붙습니다(오류는 아님).

> Windows에서 과거 스크린샷이 전혀 안 되던 문제(경로 미탐색·상대경로·프로필 잠금)는 코드에서 해결되었습니다. Chrome이 열려 있어도 캡처됩니다.

---

## 8. 설치 검증

```bash
uv run pytest                                   # 테스트 통과 확인
uv run ai-newsletter sample                     # 키 없이 샘플 산출물 생성
uv run ai-newsletter                            # 대화형: 옵션을 물어보며 생성(하위 명령 없이 실행)
uv run ai-newsletter build --days 7 --limit 10 --no-use-llm   # LLM 없이 실제 수집
uv run ai-newsletter build --days 7 --limit 10  # OpenAI로 한국어 편집(키 필요)
```
`outputs/<날짜>_weekly_ai_newsletter/newsletter.html`이 생성되면 성공입니다.

> **하위 명령 없이 `uv run ai-newsletter`만 실행하면 대화형 모드**로 들어갑니다. 선별 모드·기간·기사 수·LLM 사용 여부를 차례로 물어보고, 요약을 확인한 뒤 생성합니다. 플래그로 직접 지정하거나 자동화(UiPath)에는 `build`를 씁니다.

선별 모드는 4가지 중 선택할 수 있습니다(자세한 비교는 [SELECTION_MODES.md](SELECTION_MODES.md)):
```bash
--selection-mode issue              # 이슈 레이더(기본)
--selection-mode latest             # 지정 사이트 최신 기사
--selection-mode editorial          # LLM 뉴스가치 + 주제 중복제거
--selection-mode editorial-diverse  # editorial + 카테고리/벤더 다양성
```

---

## 9. 문제 해결 요약 (겪었던 이슈 전부)

| 증상 | 원인 | 해결 |
|---|---|---|
| `gh` 설치 시 `0x8a15005e` 인증서 오류 | winget이 msstore 소스를 시도 | `winget install ... --source winget` |
| `gh auth login` 브라우저가 안 열림/멈춤 | 사내망에서 device flow 폴링 실패 | 토큰 방식 `gh auth login --with-token` |
| `missing required scope 'read:org'` | 토큰 권한 부족 | 토큰에 `repo` + `read:org` 둘 다 체크 |
| `uv sync` → `invalid peer certificate: UnknownIssuer` | 사내 SSL 검사(회사 루트 CA 미신뢰) | `UV_SYSTEM_CERTS=true` ([4-1](#4-사내망-ssl-회사-네트워크-필수)) |
| `Missing expected target directory for Python minor version link` | Windows 심볼릭 링크 권한 | python.exe 직접 지정 또는 개발자 모드 ([5](#5-의존성-설치-uv-sync)) |
| OpenAI 호출 시 `APIConnectionError` / `CERTIFICATE_VERIFY_FAILED` | Python이 회사 CA 미신뢰 | `truststore`가 자동 처리(기본 포함). 그래도 나면 `scripts/setup_windows.ps1` 폴백([4-2](#4-사내망-ssl-회사-네트워크-필수)) |
| 뉴스레터 이미지가 일부/전부 없음 | 브라우저 미설치 또는 og:image 없음 | Chrome/Edge 설치([7](#7-이미지-캡처용-브라우저)) |
| `pytest` 플러그인 충돌(Windows) | 외부 pytest 플러그인 자동 로드 | `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 후 재실행 |

---

## 10. Mac ↔ Windows 차이 정리

| 항목 | Mac | Windows |
|---|---|---|
| uv 설치 | brew / 공식 스크립트 | winget(`--source winget`) |
| 사내 SSL 보정 | 보통 불필요 | 필요할 수 있음([4](#4-사내망-ssl-회사-네트워크-필수)) |
| Python 링크 오류 | 없음 | 발생 가능([5](#5-의존성-설치-uv-sync)) |
| 브라우저 경로 | `.app` 자동 | Program Files/Edge 자동 탐색 |
| 줄바꿈/경로 | `/` | `\` (스크립트는 자동 처리) |

개발 환경이 달라도 **코드/설정은 동일**합니다. 플랫폼 차이는 위 표의 설치·환경 단계에서만 발생하며, 애플리케이션 동작은 같습니다.
