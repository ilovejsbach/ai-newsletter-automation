# Windows/UiPath 운영 Runbook

이 문서는 망간 전송이 가능한 Windows PC에서 AI 주간 뉴스레터를 생성하고, UiPath가 내부망으로 넘길 파일을 준비하는 절차입니다.

## 1. 최초 설치

### 필수 프로그램

- Git for Windows
- Python 3.11 이상
- uv
- Google Chrome 또는 Edge (PNG 캡처가 설치된 브라우저를 사용 — playwright 브라우저 다운로드 불필요)

> 아무것도 없는 새 PC라면 `scripts\bootstrap_windows.ps1` → `scripts\setup_windows.ps1` 순서로
> 실행하면 설치·PATH·사내 SSL 설정까지 끝납니다. 상세는 [INSTALL.md](INSTALL.md).

### 저장소 준비

```powershell
cd C:\workspace
git clone <GIT_REMOTE_URL> ai-newsletter-automation
cd C:\workspace\ai-newsletter-automation
uv sync
```

아직 원격 저장소가 없다면, 이 폴더를 zip으로 Windows PC에 옮긴 뒤 그 PC에서 `git remote add origin <GIT_REMOTE_URL>`을 설정합니다.

## 2. 환경 변수

프로젝트 루트에 `.env` 파일을 만듭니다.

```powershell
copy .env.example .env
notepad .env
```

필수 값:

```text
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.4
CRITIC_MODEL=gpt-5.4-mini
```

`.env`는 git에 커밋하지 않습니다.

## 3. 주간 자동 실행 (권장 — run_weekly.ps1)

`scripts\run_weekly.ps1`이 전 과정을 한 번에 수행합니다:
uv sync → build(수집+LLM+렌더+PNG+게시 zip) → PNG 11장 검증 → 감시 폴더 투하 + `.done` 마커.

```powershell
# 수동 실행 (리허설)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_weekly.ps1

# 감시 폴더 변경
powershell ... -File scripts\run_weekly.ps1 -DropDir "D:\uipath_inbox"
```

작업 스케줄러 등록 (매주 월요일 07:00):

```bat
schtasks /create /tn "AI_Weekly_Newsletter" ^
  /tr "powershell -NoProfile -ExecutionPolicy Bypass -File C:\workspace\ai-newsletter-automation\scripts\run_weekly.ps1" ^
  /sc weekly /d MON /st 07:00
```

- 종료코드: 0=성공, 1=빌드 실패, 2=PNG 캡처 불완전, 3=zip 없음, 4=환경 문제
- 실행 로그: `logs\weekly_YYYYMMDD_HHMMSS.log`
- 리포트에 grounding_flags(원문에 없는 숫자 경고)가 있으면 로그에 표시됨 — 게시 전 대조 권장

### 수동 생성 (필요 시)

```powershell
uv run ai-newsletter build --days 7 --limit 10   # 기본: sectioned + 소셜 신호 + editorial 테마 + 썸네일 + PNG
uv run ai-newsletter                             # 대화형 (생성/재렌더/캡처/벤치마크)
```

단계 분리 명령:

| 명령 | 용도 |
|---|---|
| `build --no-capture` | HTML까지만 (빠른 확인) |
| `rerender <폴더> [--theme ...] [--capture]` | 기존 데이터로 렌더만 다시 (토큰 0) |
| `capture <폴더>` | 기존 HTML로 PNG·게시 zip만 |
| `benchmark <폴더>` | 외신 1주 보도와 선정 일치도 측정 (루브릭 개선 루프) |

같은 날짜에 여러 번 빌드하면 `_v2`, `_v3`로 나란히 저장됩니다 (덮어쓰지 않음).

## 4. UiPath 전달 파일

run_weekly.ps1이 감시 폴더(기본 `C:\newsletter_outbox`)에 투하하는 파일:

```text
ai_weekly_YYYYMMDD_publish_package.zip        # 게시 패키지
ai_weekly_YYYYMMDD_publish_package.zip.done   # 완료 마커
```

**UiPath 규칙: `.done` 파일이 생긴 뒤에만 zip을 집어갑니다** (복사 중 파일 오집 방지 —
스크립트가 .tmp로 복사 후 개명하고 마지막에 .done을 만듭니다). zip 집어간 뒤 두 파일 모두 삭제하세요.

원본 위치(수동 확인용):

```text
outputs\publish_ready\YYYY-MM-DD_ai_weekly\ai_weekly_YYYYMMDD_publish_package.zip
```

zip 내부 구조는 고정입니다.

```text
transfer_package\
  README_UIPATH.md
  images\
    ai_weekly_YYYYMMDD_main_00.png
    ai_weekly_YYYYMMDD_article_01.png
    ...
    ai_weekly_YYYYMMDD_article_10.png
  publish\
    board_post_template.html
    board_post_local_preview.html
    image_url_map.csv
    manifest.json
```

## 5. 내부망 게시 절차

1. 외부망 Windows PC에서 위 zip을 약속된 폴더에 생성합니다.
2. UiPath가 zip을 망간 전송 프로그램으로 내부망 약속 폴더에 전달합니다.
3. 내부망 PC에서 zip을 압축 해제합니다.
4. `images` 폴더의 PNG 11개를 게시판에 먼저 업로드합니다.
5. 게시판이 반환한 이미지 URL을 `publish\image_url_map.csv`의 `uploaded_url`에 기록합니다.
6. `publish\board_post_template.html`의 placeholder를 실제 이미지 URL로 치환합니다.
7. 치환된 HTML을 나모웹에디터 HTML 소스 모드에 붙여넣고 게시합니다.

## 6. 고정 계약

자동화를 위해 다음 규칙은 바꾸지 않습니다.

- 메인 이미지 1개: `ai_weekly_YYYYMMDD_main_00.png`
- 상세 이미지 10개: `ai_weekly_YYYYMMDD_article_01.png`부터 `article_10.png`
- 게시용 HTML 템플릿: `publish\board_post_template.html`
- 로컬 미리보기: `publish\board_post_local_preview.html`
- 이미지 URL 매핑표: `publish\image_url_map.csv`

## 7. Git 운영 원칙

커밋 대상:

- `src/`
- `tests/`
- `config/`
- `docs/`
- `scripts/`
- `design_preview/` (디자인 시안 참고용)
- `README.md`
- `pyproject.toml`
- `uv.lock`
- `.env.example`

커밋 제외 대상 (.gitignore 처리됨):

- `.env`
- `.venv/`
- `outputs/`, `out*/`
- `logs/` (주간 실행 로그)
- `benchmarks/` (벤치마크 히스토리)
- `.pycache/`, `.pytest_cache/`

산출물 zip은 운영 결과물이므로 git에 넣지 않고, UiPath 전달 폴더 또는 파일 서버에서 관리합니다.

## 8. 점검 명령

```powershell
uv run python -m compileall src tests
uv run pytest
```

Windows에서 `pytest` 플러그인 충돌이 나면 다음처럼 플러그인 자동 로드를 끄고 확인합니다.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
uv run pytest
```
