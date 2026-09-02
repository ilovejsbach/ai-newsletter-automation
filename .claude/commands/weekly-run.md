---
description: 화요일 주간 뉴스레터 자동 실행 런북 (빌드 → 판단 → 배포 → 릴리스)
---

주간 뉴스레터를 처음부터 끝까지 수행한다. 각 단계의 결과를 확인한 뒤 다음으로
넘어가고, 복구 불가능한 문제는 알림을 남기고 중단한다. 모든 단계에서 파일 경로는
저장소 루트(`~/workspace/ai-newsletter-automation`) 기준이다.

## 1. 빌드와 기계 검증

`scripts/run_weekly_mac.sh build` 를 실행한다. 실패하면 로그(`/tmp/ai-newsletter-build-<날짜>.log`)를
읽고, 수집 출처 일부의 403/429 경고는 무시하되 그 외 원인은 알림 후 중단한다.
성공 시 stdout 마지막 줄이 산출물 폴더 경로다 — 이후 단계에서 사용한다.

## 2. 판단 게이트 (LLM 산출물 품질)

산출물 폴더의 `data/generation_report.json`을 읽고 다음을 처리한다.

- **근거 검증 플래그(grounding_flags)**: 각 플래그의 숫자를 `data/selected_articles.json`의
  원문(body/summary)과 대조한다. 단위 변환(예: "November"→11월, "$1.25 billion"→12억 5천만,
  "per million"→100만 개당)은 오탐이므로 무시한다. 원문에 근거가 없는 실제 오류만
  해당 문장을 원문에 맞게 직접 수정한다.
- **문체 린트(style_flags)**: 강·중 지적이 있으면 `uv run ai-newsletter humanize <산출폴더>`를
  1회 실행한다. 남는 지적은 해당 문장의 어미만 직접 고쳐 반복을 끊는다
  (예: "~했다" 4연속이면 세 번째 문장을 "~다는 내용이다"류로).
- **이력 판정(history 항목, 있는 경우)**: hits를 훑어 명백히 무관한 기사가 재탕/후속으로
  묶였는지 본다. 오탐이 있어도 게재 지면에 영향이 없으면(선정 기사에 followup_of가
  잘못 붙지 않았으면) 수정 없이 요약에만 기록한다.

수정을 했다면 `uv run ai-newsletter rerender <산출폴더>`로 재렌더링한다
(이미지는 이미 존재하면 다시 받지 않으므로 안전하다). 이미지 보존 확인과 원문
대조에는 보조 스크립트를 쓴다: 수정 전에
`uv run python scripts/gate_inspect.py <산출폴더> snapshot`, 재렌더링 후
`uv run python scripts/gate_inspect.py <산출폴더> diff` (변경 0건이어야 한다),
원문 확인은 `uv run python scripts/gate_inspect.py <산출폴더> article <N>`.

## 3. 배포와 릴리스

`scripts/run_weekly_mac.sh publish <산출폴더>` 를 실행한다. 이 스크립트가
사이트 배포, 외부 리소스 0건 확인, push, 델타 zip 생성·검증, GitHub 릴리스
업로드, 다운로드 확인까지 수행한다. 실패 지점의 메시지를 읽고, 스스로 고칠 수
있는 문제(예: zip 재생성)면 고쳐서 해당 단계부터 다시 실행한다.

트랙은 스크립트의 `TRACK` 환경 변수를 따른다 (기본 history = 비교판 테스트).
정식 전환은 사용자가 지시했을 때만 한다.

## 4. 마무리 보고

`scripts/run_weekly_mac.sh notify "주간 실행 완료: <핵심 요약>"` 으로 알림을 남기고,
다음 내용을 정리해 마지막 메시지로 남긴다: 선정 10건 제목, 판단 게이트에서
수정한 내용, 이력 판정 요약, 릴리스 다운로드 URL, 사람이 할 남은 일
(화요일 인터넷망 PC에서 다운로드 → 망간 전송).
