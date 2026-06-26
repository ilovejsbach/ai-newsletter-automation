# Windows/UiPath 운영 Runbook

이 문서는 망간 전송이 가능한 Windows PC에서 AI 주간 뉴스레터를 생성하고, UiPath가 내부망으로 넘길 파일을 준비하는 절차입니다.

## 1. 최초 설치

### 필수 프로그램

- Git for Windows
- Python 3.11 이상
- uv
- Google Chrome 또는 Chromium

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

## 3. 주간 생성 명령

기본 이슈 레이더 방식:

```powershell
uv run ai-newsletter build --days 7 --limit 10
```

지정 사이트 최신 기사 방식:

```powershell
uv run ai-newsletter build `
  --days 7 `
  --limit 10 `
  --selection-mode latest `
  --latest-source-ids thenewstack,claude-blog,openai-developers-blog,pytorch-kr-blog,geeknews,marktechpost
```

지정 사이트에서 10개가 안 나와도 게시 자동화 규칙을 지키기 위해 기본값으로 다른 공식/권위 블로그에서 보강합니다. 보강 없이 엄격히 테스트하려면 `--no-latest-fill`을 추가합니다.

## 4. UiPath 전달 파일

생성 후 UiPath가 집어갈 파일은 다음 위치입니다.

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
- `README.md`
- `pyproject.toml`
- `uv.lock`
- `.env.example`

커밋 제외 대상:

- `.env`
- `.venv/`
- `outputs/`
- `.pycache/`

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
