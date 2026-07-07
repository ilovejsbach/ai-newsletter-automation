# AI Newsletter Automation

주간 AI 동향/뉴스를 수집하고, 중요도 평가를 거쳐, 내부망 반입용 HTML 패키지를 생성하는 MVP입니다.

> 📦 **설치는 [docs/INSTALL.md](docs/INSTALL.md)를 따르세요** — Mac/Windows 공통, 사내망 SSL·인증 등 겪은 문제까지 정리돼 있습니다.
> 🪟 **아무것도 안 깔린 새 Windows PC**는 [0.5 콜드스타트](docs/INSTALL.md#05-windows-완전-초기-pc--콜드스타트-아무것도-없는-경우-여기부터) — `scripts/bootstrap_windows.ps1` 한 번으로 git·gh·uv 설치와 PATH·SSL 설정을 끝냅니다.
> 🧭 선별 모드 비교는 [docs/SELECTION_MODES.md](docs/SELECTION_MODES.md), 운영/게시는 [docs/WINDOWS_UIPATH_RUNBOOK.md](docs/WINDOWS_UIPATH_RUNBOOK.md)를 보세요.

## 목표

- 최근 7일 AI 뉴스/블로그/모델/오픈소스 후보 수집
- 중요도 루브릭 기반 상위 10개 선별
- 한국어 뉴스레터 HTML 생성
- 날짜별 폴더와 zip 패키지 생성
- LLM 품질 평가 옵션 제공

## 실행

```bash
# 1) 프로젝트 폴더로 이동 (설치 위치에 맞게 경로 조정)
cd ai-newsletter-automation
#    Windows 예: cd C:\Users\KOSCOM\workplace\ai-newsletter-automation
#    Mac 예:     cd ~/workspace/ai-newsletter-automation

# 2) 의존성 설치 (최초 1회, 또는 업데이트 후)
uv sync

# 3) 실행
uv run ai-newsletter                 # 하위 명령 없이 실행하면 대화형으로 진입 (옵션을 물어보며 생성)
uv run ai-newsletter sample
uv run ai-newsletter build --days 7 --limit 10   # 기본: sectioned 모드 + 소셜 신호 + editorial 테마 + 썸네일 + PNG
uv run ai-newsletter build --days 7 --limit 10 --env-file /Users/koscom/workspace/koscom_report/.env
```

기본 `build`는 OpenAI를 사용해 한국어 뉴스레터로 편집합니다. `.env`에 `OPENAI_API_KEY`를 설정하거나, 위 예시처럼 `koscom_report`의 `.env`를 `--env-file`로 지정합니다. LLM 없이 휴리스틱 결과만 보고 싶으면 `--no-use-llm`을 붙입니다.

## 최신 사이트 기반 게시 패키지

이슈 레이더와 별도로, 지정한 사이트의 최근 1주일 기사만 우선 수집하고 그중 10개를 선발할 수 있습니다.

```bash
uv run ai-newsletter build ^
  --days 7 ^
  --limit 10 ^
  --selection-mode latest ^
  --latest-source-ids thenewstack,claude-blog,openai-developers-blog,pytorch-kr-blog,geeknews,marktechpost
```

지정 사이트에서 유효 기사 10개가 나오지 않으면 기본값으로 다른 RSS/블로그 출처에서 보강합니다. 보강 없이 엄격하게 지정 사이트만 테스트하려면 `--no-latest-fill`을 붙입니다.

## 산출물 구조

```text
outputs/
  2026-06-24_weekly_ai_newsletter/
    newsletter.html
    manifest.json
    assets/
      style.css
      images/
    data/
      crawled_articles.json
      selected_articles.json
      generation_report.json
  2026-06-24_weekly_ai_newsletter.zip
  publish_ready/
    2026-06-26_ai_weekly/
      ai_weekly_20260626_publish_package.zip
      transfer_package/
        images/
        publish/
          board_post_template.html
          board_post_local_preview.html
          image_url_map.csv
```

내부망 전송 대상은 `outputs/publish_ready/YYYY-MM-DD_ai_weekly/ai_weekly_YYYYMMDD_publish_package.zip`입니다.

같은 날짜에 여러 번 빌드하면 이전 작업을 덮어쓰지 않고 `_v2`, `_v3` 접미사로 나란히 저장됩니다
(예: `2026-07-06_weekly_ai_newsletter_v2`, `publish_ready/2026-07-06_ai_weekly_v2`).
패키지 내부 파일명(UiPath 계약)은 접미사와 무관하게 동일합니다.

## Windows/UiPath PC 운영

망간 전송이 가능한 Windows PC에서는 저장소를 clone한 뒤 `scripts\run_weekly.ps1`을
작업 스케줄러에 주 1회 등록합니다. 스크립트가 빌드→PNG 검증→UiPath 감시 폴더 투하(+`.done` 마커)까지
자동 수행합니다. 상세 절차는 [docs/WINDOWS_UIPATH_RUNBOOK.md](docs/WINDOWS_UIPATH_RUNBOOK.md)를 기준으로 합니다.

## 현재 중요도 루브릭

- 전략적 영향도: 모델, 에이전트(agent), 기업 적용, 규제/보안 파급력
- 신선도: 최근 7일 내 발표 또는 업데이트
- 객관적 인기: GitHub stars/forks, Hugging Face downloads/likes
- 출처 신뢰도: 원문/공식 블로그/주요 개발 플랫폼 가중치
- 내부 업무 관련성: 금융, 공공, 엔터프라이즈 적용 가능성

GitHub API의 저장소 응답은 stars, forks, pushed_at 같은 필드를 제공하고, Hugging Face Hub API/클라이언트는 모델의 downloads, likes, lastModified 같은 지표를 활용할 수 있습니다. 다만 Hugging Face 모델 인기도는 실제 성능과 항상 일치하지 않을 수 있으므로, LLM 평가와 사람이 보는 최종 검토를 병행하는 전제로 설계했습니다.

## 다음 단계

1. 대상 사이트 목록과 기사 양식 확정
2. 사이트별 수집 방식 결정: RSS/API 우선, 일반 웹페이지는 보조
3. 실제 1주치 데이터로 중복 제거와 중요도 가중치 보정
4. 사내 게시판 HTML 호환성 테스트
5. UiPath가 집어갈 입력/출력 폴더 계약 확정
