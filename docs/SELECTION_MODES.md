# 기사 선별 모드 (`--selection-mode`)

뉴스레터에 실릴 상위 기사를 고르는 방식은 4가지입니다. 수집(collection)은 모든 모드가 동일하게
`config/sources.yaml`의 출처를 긁고, **선별(selection)** 단계만 다릅니다.

| 모드 | 한 줄 요약 | 강점 | 약점 |
|---|---|---|---|
| `issue` (기본) | LLM이 후보를 **주제(이슈)로 묶어** 대표 기사 선정 | 교차검증된 이슈 큐레이션 | 빈 인덱스 페이지 혼입 가능 |
| `latest` | 지정 사이트의 **최근 1주 기사**를 신선도·권위로 정렬 | 최신성·출처 통제 | 중요하지만 오래된 뉴스는 탈락 |
| `editorial` | LLM **뉴스가치 채점 + 주제 중복제거** | 대형 뉴스가 안 묻힘, 중복 제거 | 파운데이션 모델에 쏠릴 수 있음 |
| `editorial-diverse` | `editorial` + **카테고리/벤더 다양성 캡** | 오픈소스·툴링·보안까지 폭넓게 | 다양성 위해 일부 벤더 뉴스 후순위 |

## `editorial` — 왜 만들었나

기존 `issue`/`latest`는 내부적으로 **키워드 밀도**에 크게 의존해서, 다음 문제가 있었습니다.

- `GPT-5.6 공개` 같은 대형 발표가 키워드 도배된 GitHub 레포·비교글에 밀려 탈락
- `수출통제 후 모델 재배포` 같은 진짜 업계 뉴스는 기술 버즈워드가 없어 저평가
- 같은 사건(예: Claude Sonnet 5 출시)을 다룬 두 기사가 제목이 달라 둘 다 선정

`editorial`은 **키워드 개수 대신 "편집자적 뉴스가치"를 LLM이 0~100점으로 채점**하고,
같은 사건은 `topic_key`로 묶어 1건만 남깁니다. 내용 없는 인덱스/카테고리 페이지는 채점 전에 제거합니다.

## `editorial-diverse` — 다양성

`editorial`이 대형 연구소(파운데이션 모델) 뉴스로 쏠리는 경향을 보완합니다.

- **벤더 캡**: 한 회사 최대 2건
- **카테고리 캡**: `model` 카테고리 최대 3건
- **빅랩 총량 캡**: 대형 연구소(OpenAI/Anthropic/Google/Meta/NVIDIA/Mistral) 합계 최대 ~60%
- **상위 중요도 보호**: 중요도 상위 ~40%는 위 캡의 **예외**로 항상 포함
  (예: 대형 재배포 뉴스가 다양성 때문에 탈락하지 않도록)

## 사용 예

```bash
# 기본
uv run ai-newsletter build --days 7 --limit 10

# 편집자 모드 (권장: 대형 뉴스 보존 + 중복 제거)
uv run ai-newsletter build --days 7 --limit 10 --selection-mode editorial

# 다양성 강화
uv run ai-newsletter build --days 7 --limit 10 --selection-mode editorial-diverse

# 지정 사이트 최신
uv run ai-newsletter build --days 7 --limit 10 --selection-mode latest \
  --latest-source-ids thenewstack,claude-blog,openai-developers-blog,marktechpost,alphasignal
```

`editorial` 계열은 `OPENAI_API_KEY`가 있어야 LLM 채점이 동작합니다. 키가 없으면 자동으로
휴리스틱(권위+교차검증+최신성) 폴백으로 동작합니다.
