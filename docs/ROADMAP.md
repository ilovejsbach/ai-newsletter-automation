# 개발 로드맵

뉴스레터의 목적 3문항 기준으로 향후 개발 주제를 관리합니다.
① 세상이 어떻게 돌아가고 있나 ② 우리에게 미치는 영향 ③ 타인들은 어떻게 행동하나

## 진행 중 / 다음 작업

| # | 주제 | 목적 연결 | 상태 | 비고 |
|---|---|---|---|---|
| 1 | 실빌드 검증 (사용자 PC) | 전체 | **다음 작업** | `uv sync` → `build --selection-mode sectioned --include-social` → 섹션 배분·업계의 움직임 슬롯·grounding_flags·테마·PNG(시스템 Chrome) 실물 확인 |
| 2 | 미커밋 변경 커밋 정리 | - | **다음 작업** | ① 병렬 수집 ② sectioned+social ③ 테마/렌더 분리/버전 분리 — 덩어리별 분리 커밋 |
| 3 | social 소스 큐레이션 | ③ | 대기 | sources.social.yaml의 YouTube 채널 ID 채우기, 인플루언서 목록 확정 |
| 4 | magazine/report 테마 다듬기 | - | 대기 | 오버레이는 시안 근사치 — 실물 PNG 확인 후 세부 조정 |
| 4b | 사내 게시판 실게시 테스트 | - | 대기 | 새 디자인 PNG로 board/TEST_GUIDE.md 절차 재확인 |

## 백로그

| # | 주제 | 목적 연결 | 비고 |
|---|---|---|---|
| 5 | Threads Keyword Search API (social 2단계) | ③ | Meta 개발자 앱 등록 + `threads_keyword_search` 권한 심사(기업 인증, 수 주 소요). 통과 시 `kind: threads-search` 수집기 추가만 하면 되는 구조. Facebook은 공식 경로 없음 — 크로스포스팅으로 커버 |
| 6 | LLM 교차검증 (grounding 강화) | ② | 현재 숫자 대조는 코드 레벨. 비판 모델이 본문-원문 대조하는 단계 추가 검토 (서술형 할루시네이션 방어) |
| 6b | 루브릭 재귀 개선 루프 운영 | ① | 매주 `benchmark <산출물>` 실행 → missed_hot_topics 검토 → 루브릭(standard/sota) 수정 → benchmarks/history.jsonl로 일치도 추이 관찰. 레퍼런스 패널은 config/sources.reference.yaml |
| 8 | 대상 사이트 목록·기사 양식 확정 | ① | README 기존 항목. sources.yaml 본편 큐레이션 |
| 9 | 중요도 가중치 보정 | ① | 실제 수 주치 데이터로 섹션 quota·부스팅 가중치 튜닝 |
| 10 | 사내 게시판 HTML 호환성 테스트 | - | board/ 산출물 변형들 실게시 테스트 (TEST_GUIDE.md 참고) |
| 11 | UiPath 입출력 폴더 계약 확정 | - | README 기존 항목 |

## 원칙

- 시의성 신호(social)는 **선별 부스팅과 반응 재료로만** 사용 — LLM 채점 대상에 넣지 않아 토큰 증가 0
- 본문은 소제목까지만 고정, 서술은 원문 주도 — 채우기 압박에 의한 할루시네이션 금지
- 자동화는 게이트가 아니라 리뷰 보조 — 최종 판단은 사람 검토
