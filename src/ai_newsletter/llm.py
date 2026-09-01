from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

from .models import RankedArticle
from .usage import usage


# Structured mode: a common 3-part spine gives readers weekly predictability,
# while the second slot is specialized per newsletter section so the body asks
# the questions that actually fit the content type. Within each slot the
# source's own structure drives the prose — slots frame, they don't flatten.
SECTION_SLOT_HEADINGS: dict[str, str] = {
    "frontier": "주요 트렌드",
    "open": "스펙과 도입 조건",
    "research": "방법과 결과",
    "tooling": "도입 가치",
}
_DEFAULT_SLOT_HEADING = "맥락 읽기"

_SLOT_GUIDES: dict[str, str] = {
    "frontier": "경쟁 구도, 가격/라이선스 정책, 파트너십·사업 구조 관점에서 이 발표가 판을 어떻게 바꾸는지",
    "open": "모델 크기·하드웨어 요구·라이선스 제약·배포 방식 등 실제 도입에 필요한 조건",
    "research": "핵심 기법을 비유 없이 짧게, 벤치마크 수치와 비교 대상, 저자가 인정한 한계",
    "tooling": "기존 도구 대비 차이, 통합 난이도, 보안·운영에 미치는 영향",
}


def structured_headings(section: str) -> list[str]:
    """Four headings mapping to the newsletter's purpose: what happened in the
    world (1+2), how others are moving (3), what it means for us (4)."""
    slot = SECTION_SLOT_HEADINGS.get(section, _DEFAULT_SLOT_HEADING)
    return ["핵심 브리핑", slot, "업계의 움직임", "시사점과 체크포인트"]

_READER_PERSONA = (
    "독자는 금융IT 기업의 임직원이고 비개발자가 절반 이상이야. "
    "이 레터는 뉴스 전달과 함께 '내부 학습'(AI 용어·개념을 자연스럽게 익히는 것)이 목표야.\n"
    "용어 원칙: 긴 정의는 본문에 넣지 말고 terms(주요 용어) 항목으로 빼. 독자는 그 블록을 따로 본다. "
    "본문에서는 꼭 필요할 때만 괄호로 15자 안쪽으로 짧게 풀어 — "
    "예: 온프레미스(사내 서버에 직접 설치해 쓰는 방식). "
    "'API(Application Programming Interface, 다른 프로그램이 모델을 호출하게 해주는 연결 규칙)용 "
    "Fast mode'처럼 주어와 서술어 사이를 긴 괄호로 끊지 마. 같은 용어를 두 번 풀지 마.\n"
    "문체 원칙 — 읽고 바로 이해되는 한국어로 써야 해:\n"
    "  1. 한 문장에 한 가지 사실만 담고, 평균 45자를 넘기지 마. 길면 끊어.\n"
    "  2. 누가 무엇을 했는지 주어를 밝혀. 피동('~되어진다', '~로 보여진다')은 능동으로 바꿔.\n"
    "  3. 번역투 금지: '~에 대하여', '~를 통해', '~됨에 따라', '~로 인해', '~에 있어서', "
    "'~을 가진다', '~하는 흐름을 보였다', '~하는 움직임이 늘었다'.\n"
    "  4. 뜻을 더하지 않는 말로 자리를 채우지 마: '함께', '또한', '한편', '더욱', '보다 나은'.\n"
    "  5. 명사를 늘어놓지 말고 서술어로 풀어. "
    "'성능·가격·출시일은 확인되지 않았다'가 아니라 '성능과 가격, 출시일은 아직 공개되지 않았다'.\n"
    "  6. 두루뭉술한 총평으로 문단을 닫지 마: '~라는 점이 크다', '~가 중요해졌다', "
    "'주목할 필요가 있다'. 무엇이 어떻게 달라지는지 구체적으로 써.\n"
    "  7. 한 문단 안에서 어미를 섞지 마.\n"
    "  8. 수치의 **값**은 원문 그대로 보존하되 **표기는 한국어로** 옮겨. "
    "'more than 5 million'→'500만 건 이상', 'roughly 200'→'약 200건', "
    "'$2 per million tokens'→'100만 토큰당 2달러'. 값을 바꾸거나 원문에 없는 수치를 "
    "만드는 것만 금지다. 한글 문장 안에 영어 구절('more than', 'per', "
    "'active and proposed')을 남기지 마 — 모델명·제품명·벤치마크명 같은 고유명사만 "
    "영문 유지.\n"
    "  9. 내보내기 전에 맞춤법과 오타를 다시 확인해. 특히 기술 용어를 잘못 적지 마"
    "('주론'이 아니라 '추론', '학습'과 '추론'을 섞어 쓰지 않기).\n"
    "  10. AI 특유의 결산·격상 공식 금지: '~로 이어진다'로 문단을 닫기, "
    "'단순한 A가 아니라 B', '중요한 것은 ~라는 점이다', '~라는 점에서 의미가 있다', "
    "'주목할 만하다', '시사하는 바가 크다'. 대신 실제 인과와 주체를 직접 서술해.\n"
    "  11. 과장 수식 금지: '혁신적', '획기적', '압도적', '게임 체인저'. "
    "형용사 대신 원문의 수치와 사실로 크기를 보여줘.\n"
    "  12. 문두 접속사('또한', '한편', '즉', '나아가', '아울러')로 문장을 여는 것은 "
    "한 기사에 한 번까지만. 문장 관계가 자명하면 접속사 없이 이어.\n"
    "  13. 같은 종결어미('~했다', '~이다' 등)를 3문장 이상 연속하지 마. "
    "문장을 합치거나 어미를 바꿔 리듬에 변화를 줘.\n"
    "  14. 무생물 주어 의인화 금지: 연구·데이터·기술이 '묻는다/말한다/질문을 던진다'고 "
    "쓰지 마. '연구에 따르면', '데이터를 보면'처럼 풀어.\n"
    "  15. 수사적 되물기('정말 ~인지부터 다시 묻는다')로 멋 부리지 마. 결론을 평서문으로 직접 써.\n"
    "  16. 반쪽 인용 금지: 수치·주장을 옮길 때 원문에서 같은 문장에 붙은 조건·기간·측정 주체·"
    "트레이드오프 단서(예: '단, 정확도 6%p 손해', '연말까지 한시 가격', '벤더 자체 측정')를 "
    "반드시 함께 옮겨. 유리한 절반만 발췌하지 마.\n"
    "  17. 따옴표 규율: 큰따옴표+발언 귀속(\"~라고 말했다\")은 원문에 실재하는 문장(또는 그 "
    "충실한 번역)에만 써. 편집자의 해석·논평을 따옴표로 싸지 마.\n"
    "  18. 벤치마크 수치엔 측정 주체(자체 측정인지 독립 평가인지), 가격 할인엔 적용 기간, "
    "논문 기반 기사엔 논문 공개 시점과 이번 주 보도 시점의 구분을 원문에 있으면 명시해.\n"
    "  19. 용어는 원문 용어를 우선해 (원문이 'AI-generated video'면 '딥페이크'가 아니라 "
    "'AI 생성 영상').\n"
    "  20. 일반 독자가 모를 회사·제품·인물이 처음 나오면 원문에 있는 소개를 한 어절이라도 "
    "함께 옮겨('수산기업 Cooke', '업무관리 SaaS monday.com'). 원문에 소개가 없으면 "
    "'고객사인 Cooke'처럼 중립 표지만 붙이고, 원문에 없는 설명을 지어내지 마.\n"
    "친절하되 가르치려 드는 말투('쉽게 말해서', '~라고 생각하면 된다'의 반복)는 피해."
)


def _structured_prompt(payload: list[dict[str, object]]) -> str:
    slot_rules = "\n".join(
        f"  - section이 '{sec}'이면 2번 소제목은 '{heading}': {_SLOT_GUIDES[sec]}."
        for sec, heading in SECTION_SLOT_HEADINGS.items()
    )
    return (
        "다음 AI 뉴스 후보를 사내 게시판에 올릴 한국어 뉴스레터용으로 편집해줘. "
        f"{_READER_PERSONA}\n"
        "모든 기사는 4개 소제목의 골격을 따라. 1·3·4번은 공통, 2번은 기사의 section에 따라 달라져.\n"
        "분량 원칙: 상세 아티클은 길어도 좋다. 원문 발췌에 정보가 풍부하면 각 슬롯을 "
        "충분히 길게(문단 여러 개, 목록 포함) 써서 원문의 정보 밀도를 최대한 옮겨. "
        "단, 길이는 원문 정보량이 결정한다 — 원문에 없는 내용으로 분량을 늘리는 것은 금지.\n"
        "표 원칙: 원문 발췌에 [표] 블록이 있고 수치 비교가 기사의 핵심이면, 해당 섹션 객체에 "
        "\"table\": {\"columns\": [...], \"rows\": [[...]]} 필드를 추가해. 열 2~6개·행 3~10개, "
        "수치는 원문 그대로, 열 이름은 한국어로 자연스럽게. 표에 넣은 수치를 본문 불릿으로 "
        "중복 나열하지 마.\n"
        "1) '핵심 브리핑' — 사실만. 첫 문단은 반드시 '이것이 무엇이고 어떤 문제를 "
        "푸는가'를 비전문가 기준 2~3문장으로 먼저 써 — 라이선스·배포 형식·주의 문구 같은 "
        "지엽 팩트로 시작하지 마. 그다음 원문에서 확인되는 발표/변경/수치를 4-10문장으로 상세히. "
        "스펙·설정값·가격·제약처럼 나열이 읽기 쉬운 내용은 '- '로 시작하는 목록을 적극 활용해. "
        "payload에 followup_of가 있으면 이 소식은 지난 호에서 다룬 소식의 후속이야 — "
        "리드 문단에서 '지난 호에서 다룬 ~의 후속 소식'임을 자연스럽게 한 번 밝혀 "
        "(followup_of에 적힌 주차와 제목을 참조하되, 없는 내용을 지어내지 마).\n"
        "2) 섹션 특화 소제목 — 각 기사 payload의 section 값에 맞는 소제목과 관점을 써. "
        f"section이 없거나 목록에 없으면 '{_DEFAULT_SLOT_HEADING}'로 일반 맥락을 써.\n"
        f"{slot_rules}\n"
        "  - 중요: 특화 소제목이 요구하는 정보(스펙·가격·라이선스·파트너십 조건 등)가 "
        f"본문 발췌에 없으면, 특화 소제목을 쓰지 말고 '{_DEFAULT_SLOT_HEADING}'로 바꿔서 "
        "원문에 있는 맥락만 써. 소제목을 채우기 위해 원문 밖 지식으로 스펙이나 조건을 "
        "만들어 넣는 것은 금지야.\n"
        "3) '업계의 움직임' — 타사·커뮤니티·고객의 반응과 움직임을 2-6문장으로. "
        "원문에 언급된 반응·도입 사례·경쟁사 대응을 우선 쓰고, payload의 related_coverage에 "
        "다른 매체가 있으면 '여러 매체가 함께 다룬 사건'임을 자연스럽게 반영해. "
        "원문과 related_coverage 모두에 반응 정보가 없으면 한 문장으로 '아직 공개된 반응은 "
        "확인되지 않았다'고 쓰고 추측으로 채우지 마.\n"
        "4) '시사점과 체크포인트' — 금융·엔터프라이즈 관점 추론. 원문 사실과 분리해 "
        "'우리 회사라면'의 관점으로 기회와 리스크를 함께 3-8문장 또는 목록으로 구체적으로. "
        "검토할 팀/시스템, 선행 과제, 확인 질문처럼 실행 가능한 체크포인트를 포함하면 좋다. "
        "한계·주의점·미확인 정보도 이 슬롯에 포함해. 원문에 없는 성능·전망을 단정하지 마.\n"
        "골격은 소제목까지만 고정이야. 각 소제목 안의 서술 구조는 원문이 끌고 가게 해: "
        "공식 블로그면 설계 선택과 이전 방식과의 차이를, 레포면 목적과 운영 난이도를, "
        "논문이면 방법-결과-한계 순서를 살려. 슬롯을 채우려고 원문에 없는 내용을 만들지 말고, "
        "해당 소제목에 쓸 정보가 원문에 부족하면 한 문장으로 부족하다고 써.\n"
        "그 외 필드: one_liner(기사 전체를 40자 이내 한 문장으로), "
        "hook(포털 콜아웃 — 네가 쓴 detail_sections 본문에서 그대로 뽑은 가장 인상적인 문장 하나. "
        "반드시 한국어 문장이어야 해 — 영어 원문 문장을 그대로 hook으로 쓰지 마. "
        "새 문장을 만들지 말고 본문 문장을 문자 그대로 인용해. 기사가 다루는 대상이 "
        "무엇인지 드러나는 문장이어야 해 — 경고·주의·제약 단서 문장은 hook으로 쓰지 마. "
        "결론을 다 말해버리는 문장보다 뒷이야기가 궁금해지는 문장을 골라. "
        "40-90자 권장, 목록 항목 말고 서술 문장에서), "
        "korean_title(28자 안팎 — 제목만 읽어도 '누가 무엇을 했는지' 아는 직관적인 문장. "
        "회사명·행위를 명확히 하되, 이번 호 전체의 제목이 한 가지 틀로 쏠리면 안 돼: "
        "'회사, ~ 공개' 틀은 전체의 절반 이하로 쓰고, 나머지는 수치형('7배 빨라진 ~'), "
        "결과형('~가 ~를 금지했다'), 변화형('~가 ~로 바뀐다') 등 다른 틀을 섞어. "
        "끝 단어도 '공개/출시/발표'로 몰지 말고 다양하게. "
        "단, 동사를 다양화하려고 '내다'처럼 다의적인 축약 동사를 쓰면 안 돼 — "
        "예: '구글, TimesFM-3 내고'는 무엇을 했는지 모호하다. 행위가 한 번에 읽히는 "
        "구체적 동사('공개했지만', '멈췄다', '금지했다' 등)를 골라. "
        "'X: Y' 콜론 공식과 '~의 시대' 류 상투 제목, 업계 은어·영어 약어 나열·과장 금지), "
        "korean_summary(2-3문장 — 첫 문장이 야마: 이 사건의 핵심을 비전문가도 바로 잡게), "
        "why_it_matters(1-2문장), "
        "terms(3-6개 배열, 각 항목은 '용어(영문): 한 줄 풀이' 형식 — 이 기사를 이해하는 데 "
        "필요한 개념을 내부 학습용으로 고르되 본문에서 이미 충분히 풀린 것은 제외), "
        "detail_intro(브리핑처럼 맥락을 여는 도입부 3-5문장).\n"
        "본문은 2~4문장을 공백으로 이어 쓴 흐름 문단으로 작성해. 문장마다 줄바꿈하지 말고, "
        "문단과 문단 사이에만 빈 줄을 넣어. 스펙·수치 나열은 문단에 섞지 말고 '- ' 불릿 "
        "블록으로 분리해. "
        "'원문에 의하면', '기사에 따르면' 같은 출처 표지 문구는 쓰지 마. "
        "GitHub stars/forks/downloads/score 같은 정량 지표나 선별 점수는 본문에 쓰지 마. "
        "원문에 없는 사실은 만들지 마.\n"
        "반드시 {\"articles\":[{\"index\":정수, \"one_liner\":..., \"hook\":..., \"korean_title\":..., "
        "\"korean_summary\":..., \"why_it_matters\":..., \"terms\":[...], \"detail_intro\":..., "
        "\"detail_sections\":[{\"heading\":\"핵심 브리핑\",\"body\":...}, "
        "{\"heading\":\"(섹션 특화 소제목)\",\"body\":...,\"table\":{\"columns\":[...],\"rows\":[[...]]}} "
        "(table은 수치 비교가 핵심일 때만 넣는 선택 필드), {\"heading\":\"업계의 움직임\",\"body\":...}, "
        "{\"heading\":\"시사점과 체크포인트\",\"body\":...}]}]} 형태의 JSON만 반환해. "
        "모든 index를 빠짐없이 포함해.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _article_payload(idx: int, a: RankedArticle) -> dict[str, object]:
    payload: dict[str, object] = {
        "index": idx,
        "title": a.title,
        "source": a.source_name,
        "summary": a.summary,
        "body_excerpt": (a.body or "")[:9000],
        "url": a.url,
        "reason": a.reason,
        "section": a.section,
        "related_coverage": a.related_coverage,
    }
    if a.followup_of:
        payload["followup_of"] = a.followup_of
    return payload


def enrich_with_openai(
    articles: list[RankedArticle], *, structured: bool = False
) -> list[RankedArticle]:
    if not os.getenv("OPENAI_API_KEY"):
        return articles
    # Explicit timeout + limited retries: without this a large concurrent POST can
    # hang on the corporate proxy indefinitely (the build stalled for hours). A
    # legit batch finishes well under the timeout; a hung one fails fast and the
    # article drops to the per-article retry below.
    timeout = float(os.getenv("ENRICH_TIMEOUT", "150"))
    client = OpenAI(timeout=timeout, max_retries=1)
    model = os.getenv("OPENAI_MODEL", "gpt-5.4")
    # 기사 10건×본문 9천자를 한 요청에 담으면 사내 프록시가 대형 POST를 끊는
    # 경우가 있어(RemoteProtocolError) 작은 배치로 나눠 보낸다. 배치가 실패해도
    # 빌드를 죽이지 않고, 남은 기사는 아래 개별 재시도(_enrich_one)가 처리한다.
    batch_size = max(1, int(os.getenv("ENRICH_BATCH_SIZE", "4")))
    max_workers = max(1, int(os.getenv("ENRICH_WORKERS", "4")))
    indexed = list(enumerate(articles))
    batches = [indexed[start : start + batch_size] for start in range(0, len(indexed), batch_size)]

    # Batches are independent (each writes to its own articles), and httpx is
    # thread-safe, so run them concurrently — enrich is the build's bottleneck.
    def _run_batch(chunk: list[tuple[int, RankedArticle]]) -> None:
        payload = [_article_payload(idx, a) for idx, a in chunk]
        prompt = _build_enrich_prompt(payload, structured=structured)
        try:
            response = client.responses.create(
                model=model,
                input=prompt,
                text={"format": {"type": "json_object"}},
            )
            usage.record(response)
            rows = _extract_rows(json.loads(response.output_text))
        except Exception:
            return  # 이 배치 기사들은 아래 개별 재시도로 넘어간다
        for row in rows:
            if not isinstance(row, dict):
                continue
            idx = row.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(articles):
                continue
            _apply_row(articles[idx], row, structured=structured)

    if batches:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as executor:
            list(executor.map(_run_batch, batches))

    retries = [(idx, a) for idx, a in enumerate(articles) if _needs_retry(a)]
    if retries:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(retries))) as executor:
            list(
                executor.map(
                    lambda pair: _enrich_one(client, model, pair[0], pair[1], structured=structured),
                    retries,
                )
            )
    return articles


def _build_enrich_prompt(payload: list[dict[str, object]], *, structured: bool) -> str:
    return _structured_prompt(payload) if structured else (
        "다음 AI 뉴스 후보를 사내 게시판에 올릴 한국어 뉴스레터용으로 편집해줘. "
        "제목과 요약은 자연스러운 한국어로 작성하고, 중요한 기술 용어는 한국어 기준 영문 병기 형태로 써줘. "
        "예: 에이전트(agent), 대규모 언어모델(LLM), 벤치마크(benchmark). "
        "중요 원칙: 자동화 산출물 구조는 정형화하되, 본문 해석은 원문이 끌고 가야 해. "
        "원문을 고정 템플릿에 억지로 맞추지 말고, 원문에서 정보량이 가장 높은 구조를 먼저 파악해. "
        "공식 기술 블로그는 설계 선택·이전 방식과의 차이·실험/벤치마크·한계를 살리고, "
        "GitHub 레포는 목적·기존 도구와 차이·운영 난이도·도입 리스크를 살리고, "
        "모델 릴리즈는 포지션·성능 주장·컨텍스트/비용/라이선스/배포 방식을 살려. "
        "사내 시사점은 원문 사실과 분리해 추론으로 작성하고, 원문에 없는 전망·성능·의도는 만들지 마. "
        "최종 산출물은 이미지로 읽히므로 휴머나이즈 윤문을 반드시 적용해. "
        "'원문에 의하면', '기사에 따르면', '본문은 말한다' 같은 출처 표지 문구는 반복하지 마. "
        "근거는 내용으로 드러내고, 출처 표기는 HTML의 별도 정보 영역에 맡겨. "
        "제목은 28자 안팎으로 짧고 선명하게 쓰고, 필요하면 고유명사나 모델명은 부제 성격의 요약 문장에 넣어. "
        "'공개했다', '발표했다', '제공한다'만 반복하지 말고, 첫 문장은 사람이 읽는 브리핑처럼 맥락을 열어줘. "
        "문단은 2-3문장 단위로 짧게 끊고, 한 문장은 가능하면 70자 안팎으로 유지해. "
        "섹션 제목은 보고서식 표현보다 '무엇이 달라졌나', '왜 지금 중요한가', '실무에서는 어디에 쓰일까', '아직 조심할 점'처럼 자연스럽게 써. "
        "단, 가벼운 마케팅 문구나 과한 수사는 쓰지 말고, 정확하고 차분한 한국어 웹진 톤을 유지해. "
        "반드시 {\"articles\": [...]} 형태의 JSON 객체만 반환해. "
        "각 articles 항목에는 index, korean_title, korean_summary(2-3문장), "
        "why_it_matters(1-2문장), terms(중요 용어 한국어 기준 영문 병기 배열), "
        "detail_intro(본문 도입부 3-4문장), detail_sections(heading/body를 가진 5-7개 섹션 배열)를 넣어. "
        "공통 뼈대는 '원문이 말하는 핵심', '맥락과 차별점', '실무 영향', '확인할 리스크', '출처와 한계'를 포함하되, "
        "섹션명과 순서는 원문 유형에 맞게 자연스럽게 조정해. "
        "각 섹션 body는 가능하면 3-6문장으로 작성해. 원문에 기술 스펙·설정값·명령어·아키텍처·벤치마크·제약·가격·하드웨어·API 옵션이 있으면 구체적으로 반영해. "
        "스펙, 절차, 설정값, 장단점처럼 나열이 더 읽기 쉬운 부분은 '- '로 시작하는 짧은 목록을 적극적으로 섞어 써. "
        "단, 목록 항목도 반드시 제공된 본문 발췌에서 확인되는 내용에 근거해야 해. "
        "사용자가 제공한 GLM-5.2 글처럼 원문 정보 밀도와 맥락을 살리되, 모든 기사를 같은 흐름으로 강제하지 마. "
        "비교 대상이 있는 글은 표 대신 문장으로 비교해. "
        "GitHub stars/forks/downloads/score 같은 정량 지표나 선별 점수는 본문에 쓰지 마. "
        "원문에 없는 사실은 만들지 말고, 본문 발췌에 정보가 부족한 항목은 부족하다고 쓰되 과장하지 마.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _apply_row(article: RankedArticle, row: dict[str, object], *, structured: bool) -> None:
    article.one_liner = str(row.get("one_liner") or article.one_liner)
    article.hook = str(row.get("hook") or article.hook)
    article.korean_title = row.get("korean_title") or article.korean_title
    article.korean_summary = row.get("korean_summary") or article.korean_summary
    article.why_it_matters = row.get("why_it_matters") or article.why_it_matters
    article.terms = _normalize_terms(row.get("terms")) or article.terms
    article.detail_intro = row.get("detail_intro") or article.detail_intro
    sections = _normalize_sections(row)
    if structured and sections:
        sections = _order_fixed_sections(sections, article.section)
    article.detail_sections = sections or article.detail_sections
    _clean_article_text(article)


def _order_fixed_sections(
    sections: list[dict[str, object]], section: str
) -> list[dict[str, object]]:
    """Keep the skeleton order for this article's section; unknown headings are
    appended at the end so an off-script LLM response degrades gracefully
    instead of losing text. The generic fallback heading is accepted in the
    specialized slot's position (the LLM downgrades to it when the source
    lacks the specialized info)."""
    expected = structured_headings(section)
    if _DEFAULT_SLOT_HEADING not in expected:
        expected = expected[:1] + [expected[1], _DEFAULT_SLOT_HEADING] + expected[2:]
    by_heading = {str(s["heading"]).strip(): s for s in sections}
    ordered = [by_heading.pop(h) for h in expected if h in by_heading]
    ordered.extend(by_heading.values())
    return ordered


def _extract_rows(data: object) -> list[object]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("articles", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _needs_retry(article: RankedArticle) -> bool:
    return (
        not article.detail_sections
        or not article.detail_intro
        or article.korean_summary in ("", "요약 생성 전입니다.")
        or article.korean_title == article.title
    )


def _enrich_one(
    client: OpenAI, model: str, index: int, article: RankedArticle, *, structured: bool = False
) -> None:
    payload = {
        "index": index,
        "title": article.title,
        "source": article.source_name,
        "summary": article.summary,
        "body_excerpt": (article.body or "")[:9000],
        "url": article.url,
        "section": article.section,
        "related_coverage": article.related_coverage,
    }
    prompt = _structured_prompt([payload]) if structured else (
        "다음 기사 1건을 사내 AI 웹진의 상세 아티클로 한국어 작성해줘. "
        "반드시 {\"articles\":[...]} JSON 객체만 반환하고 index를 그대로 유지해. "
        "필드는 index, korean_title, korean_summary, why_it_matters, terms, detail_intro, detail_sections를 포함해. "
        "원문을 고정 템플릿에 억지로 맞추지 말고, 원문에서 정보량이 높은 구조를 먼저 파악해. "
        "detail_sections는 5-7개로 구성하고, '원문이 말하는 핵심', '맥락과 차별점', '실무 영향', '확인할 리스크', '출처와 한계'를 포함하되 기사 유형에 맞게 섹션명을 조정해. "
        "각 섹션은 3-6문장으로, 원문 발췌의 기술 스펙·설정값·명령어·아키텍처·제약을 구체적으로 반영해. "
        "스펙, 절차, 설정값, 장단점은 '- '로 시작하는 짧은 목록을 섞어 가독성을 높여. "
        "사내 시사점은 원문 사실과 분리해 추론으로 작성하고, 원문에 없는 사실은 만들지 마. "
        "최종 산출물은 이미지로 읽히므로 휴머나이즈 윤문을 적용해. "
        "'원문에 의하면', '기사에 따르면', '본문은 말한다' 같은 출처 표지 문구는 쓰지 마. "
        "제목은 짧고 선명하게, 첫 문장은 맥락을 여는 브리핑 문장으로, 문단은 2-3문장 단위로 작성해. "
        "섹션 제목은 '무엇이 달라졌나', '왜 지금 중요한가', '실무에서는 어디에 쓰일까', '아직 조심할 점'처럼 자연스럽게 써. "
        "보고서식 문장, 같은 종결 반복, 과한 마케팅 표현은 피하고 정확하고 차분한 웹진 톤을 유지해. "
        "GitHub stars/forks/downloads/score 같은 정량 지표나 선별 점수는 쓰지 마. "
        "원문에 없는 사실은 만들지 마.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        response = client.responses.create(
            model=model,
            input=prompt,
            text={"format": {"type": "json_object"}},
        )
        usage.record(response)
        rows = _extract_rows(json.loads(response.output_text))
    except Exception:
        return
    if not rows or not isinstance(rows[0], dict):
        return
    _apply_row(article, rows[0], structured=structured)


def _normalize_terms(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    terms: list[str] = []
    for item in value:
        if isinstance(item, str):
            terms.append(item)
        elif isinstance(item, dict):
            term = item.get("term") or item.get("name") or item.get("label")
            desc = item.get("description") or item.get("desc")
            if term and desc:
                terms.append(f"{term}: {desc}")
            elif term:
                terms.append(str(term))
    return terms


def _normalize_sections(row: dict[str, object]) -> list[dict[str, object]]:
    value = row.get("detail_sections") or row.get("sections") or row.get("article_sections")
    if isinstance(value, dict):
        value = [{"heading": key, "body": body} for key, body in value.items()]
    if not isinstance(value, list):
        return []
    sections: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            continue
        heading = item.get("heading") or item.get("title") or item.get("section")
        body = item.get("body") or item.get("content") or item.get("text")
        if heading and body:
            section: dict[str, object] = {"heading": str(heading), "body": str(body)}
            table = _normalize_table(item.get("table"))
            if table:
                section["table"] = table
            sections.append(section)
    return sections


def _normalize_table(value: object) -> dict[str, object] | None:
    """LLM이 만든 표 후보를 검증한다 — columns/rows 형태를 갖추지 못했거나
    비어 있으면 표를 버리고(None) 해당 섹션은 본문(body)만으로 렌더링된다."""
    if not isinstance(value, dict):
        return None
    columns = value.get("columns")
    rows = value.get("rows")
    if not isinstance(columns, list) or not columns:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    clean_rows = [
        [str(cell) for cell in row] for row in rows if isinstance(row, list) and row
    ]
    if not clean_rows:
        return None
    return {"columns": [str(col) for col in columns], "rows": clean_rows}


def _clean_article_text(article: RankedArticle) -> None:
    article.korean_summary = _remove_source_markers(article.korean_summary)
    article.why_it_matters = _remove_source_markers(article.why_it_matters)
    article.detail_intro = _remove_source_markers(article.detail_intro)
    cleaned_sections: list[dict[str, object]] = []
    for section in article.detail_sections:
        cleaned: dict[str, object] = {
            "heading": _remove_source_markers(str(section.get("heading", ""))),
            "body": _remove_source_markers(str(section.get("body", ""))),
        }
        # table은 텍스트 필드가 아니라 그대로 보존 — 출처 표지 문구 제거는
        # 서술형 본문에만 해당한다.
        if isinstance(section.get("table"), dict):
            cleaned["table"] = section["table"]
        cleaned_sections.append(cleaned)
    article.detail_sections = cleaned_sections


def _remove_source_markers(text: str) -> str:
    replacements = (
        "원문에 의하면 ",
        "원문에 따르면 ",
        "기사에 의하면 ",
        "기사에 따르면 ",
        "본문에 따르면 ",
        "해당 글에 따르면 ",
        "이 글에 따르면 ",
    )
    cleaned = text
    for marker in replacements:
        cleaned = cleaned.replace(marker, "")
    return cleaned


def grounding_flags(articles: list[RankedArticle]) -> list[dict[str, object]]:
    """Hallucination spot-check: numbers in the generated body that do not
    appear anywhere in the source article are flagged for human review.
    This is a review aid, not a gate — publication still goes through the
    final human check, but the flags say exactly where to look.

    A number that fails the raw string match (e.g. because the source says
    'more than 5 million' and the generated text correctly translates that
    to '500만 건 이상') gets a second chance: if the numeric *value* of the
    expression it came from matches a numeric value found anywhere in the
    source (within 1% relative error), it's grounded — translating a unit
    is not the same as inventing a number. Only numbers that fail both
    checks are reported as unmatched."""
    flags: list[dict[str, object]] = []
    for idx, article in enumerate(articles, 1):
        source_text = f"{article.title} {article.summary} {article.body}"
        source = _normalized_digits(source_text)
        source_values = _numeric_values(source_text)
        for section in article.detail_sections:
            # 표로 들어간 조작 수치도 같은 검사 대상이어야 한다 — 불릿에서
            # 표로 옮겼다고 근거 검증을 피해가면 안 된다.
            text = str(section.get("body", ""))
            table = section.get("table")
            if isinstance(table, dict):
                text = f"{text} {_table_cell_text(table)}"
            expr_values = _numeric_expr_values(text)
            missing = [
                number
                for number in _significant_numbers(text)
                if number not in source
                and not any(
                    _value_matches(value, source_values)
                    for value in expr_values.get(number, ())
                )
            ]
            if missing:
                flags.append(
                    {
                        "article": idx,
                        "title": article.korean_title or article.title,
                        "heading": section.get("heading", ""),
                        "unmatched_numbers": missing[:8],
                    }
                )
    return flags


def _table_cell_text(table: dict[str, object]) -> str:
    """표의 columns/rows 셀 텍스트를 한 문자열로 모은다 (근거 검증용)."""
    parts: list[str] = []
    columns = table.get("columns")
    if isinstance(columns, list):
        parts.extend(str(col) for col in columns)
    rows = table.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                parts.extend(str(cell) for cell in row)
    return " ".join(parts)


def _normalized_digits(text: str) -> str:
    # 자릿수 구분 콤마만 제거한다. 공백까지 지우면 인접한 숫자가 붙어
    # 'Opus 5 90.4%' -> '590.4' 같은 유령 숫자가 생긴다 (표 셀에서 실제 발생).
    return re.sub(r"(?<=\d),(?=\d)", "", text)


def _significant_numbers(text: str) -> list[str]:
    """Numbers worth verifying: 2+ digits, excluding plain years (too noisy)."""
    normalized = re.sub(r"(?<=\d),(?=\d)", "", text)
    numbers = set(re.findall(r"\d{2,}(?:\.\d+)?", normalized))
    return sorted(n for n in numbers if not re.fullmatch(r"(?:19|20)\d{2}", n))


# ---------------------------------------------------------------------------
# Unit-aware value normalization for grounding_flags: a string match rejects
# '500만' against a source that says '5 million' even though they're the same
# value. These helpers parse both English and Korean magnitude words into a
# comparable float so the checker can tell "translated" from "invented".

_EN_UNIT_MULTIPLIERS = {
    "billion": 1e9,
    "million": 1e6,
    "thousand": 1e3,
    "k": 1e3,
}
_EN_UNIT_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(billion|million|thousand|k)\b", re.IGNORECASE
)
# 'B/M/K' 약어 접미사('35B 파라미터', '120M 다운로드'). 대문자만 인정해
# 'k'(위에서 처리)나 일반 단어와의 충돌을 피한다.
_EN_ABBREV_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?([BMK])\b")
_EN_ABBREV_MULTIPLIERS = {"B": 1e9, "M": 1e6, "K": 1e3}

# 긴 단위부터 매칭해야 '2천만'이 '천'+'만'으로 쪼개지지 않는다.
_KO_UNIT_MULTIPLIERS = {
    "천만": 1e7,
    "백만": 1e6,
    "십만": 1e5,
    "억": 1e8,
    "만": 1e4,
    "천": 1e3,
    "백": 1e2,
}
_KO_UNIT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)(천만|백만|십만|억|만|천|백)")

# 단위가 없는 숫자(콤마 포함) — 표기 차이(공백·자릿수 구분)를 흡수하기 위한 값 비교.
_PLAIN_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _numeric_expr_values(text: str) -> dict[str, set[float]]:
    """텍스트의 각 숫자 표현을 (그 숫자의 콤마 제거 문자열) -> {정규화된 값들}로 매핑한다.

    키는 `_significant_numbers`가 뽑아내는 숫자 문자열과 같은 형태라, 문자열
    매칭에 실패한 숫자를 이 딕셔너리로 되짚어 '어떤 값으로 해석됐는지' 알 수 있다.
    """
    result: dict[str, set[float]] = {}
    consumed: list[tuple[int, int]] = []

    def _add(digits: str, value: float) -> None:
        key = digits.replace(",", "")
        if not key:
            return
        result.setdefault(key, set()).add(value)

    for match in _EN_UNIT_RE.finditer(text):
        try:
            base = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        multiplier = _EN_UNIT_MULTIPLIERS[match.group(2).lower()]
        _add(match.group(1), base * multiplier)
        consumed.append(match.span())

    for match in _EN_ABBREV_RE.finditer(text):
        start, end = match.span()
        if any(s <= start and end <= e for s, e in consumed):
            continue
        try:
            base = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        _add(match.group(1), base * _EN_ABBREV_MULTIPLIERS[match.group(2)])
        consumed.append(match.span())

    for match in _KO_UNIT_RE.finditer(text):
        try:
            base = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        multiplier = _KO_UNIT_MULTIPLIERS[match.group(2)]
        _add(match.group(1), base * multiplier)
        consumed.append(match.span())

    for match in _PLAIN_NUMBER_RE.finditer(text):
        start, end = match.span()
        if any(s <= start and end <= e for s, e in consumed):
            continue  # 이미 단위 표현의 일부로 처리됨 (예: '5 million'의 '5')
        digits = match.group(0).replace(",", "")
        try:
            value = float(digits)
        except ValueError:
            continue
        _add(digits, value)

    return result


def _numeric_values(text: str) -> set[float]:
    """텍스트에서 발견되는 모든 수치 표현을 정규화된 값 집합으로 변환한다.
    'more than 5 million' -> {5000000.0}, '500만' -> {5000000.0},
    '250,000' -> {250000.0}. %·x·배 같은 배율 접미사는 숫자만 취한다."""
    values: set[float] = set()
    for value_set in _numeric_expr_values(text).values():
        values.update(value_set)
    return values


def _value_matches(value: float, source_values: set[float]) -> bool:
    """value가 source_values 중 하나와 상대 오차 1% 이내로 같은지."""
    for source_value in source_values:
        if source_value == 0:
            if value == 0:
                return True
            continue
        if abs(value - source_value) / abs(source_value) <= 0.01:
            return True
    return False


def generate_weekly_overview(articles: list[RankedArticle]) -> str:
    """One short editorial paragraph answering '이번 주 세상이 어떻게 돌아갔나' —
    synthesized from the selected stories, shown at the top of the newsletter."""
    if not os.getenv("OPENAI_API_KEY") or not articles:
        return ""
    client = OpenAI(timeout=float(os.getenv("OPENAI_TIMEOUT", "120")), max_retries=1)
    model = os.getenv("CRITIC_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.4-mini"))
    payload = [
        {
            "title": a.korean_title or a.title,
            "one_liner": a.one_liner,
            "section": a.section,
            "source": a.source_name,
        }
        for a in articles
    ]
    prompt = (
        "아래는 이번 주 사내 AI 뉴스레터에 선정된 기사 목록이야. "
        f"{_READER_PERSONA}\n"
        "이번 주 AI 업계가 어떻게 움직였는지 3-5문장의 한국어 브리핑으로 종합해줘. "
        "기사를 나열하지 말고 흐름을 묶어서 서술해 (예: 프론티어 경쟁, 오픈소스 움직임, "
        "규제/보안 흐름 중 이번 주 두드러진 축). 과장 없이 차분한 톤으로, "
        "마지막 문장은 독자가 이번 주 무엇을 주목해야 하는지로 맺어. "
        "반드시 {\"overview\": \"...\"} 형태의 JSON만 반환해.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        response = client.responses.create(
            model=model,
            input=prompt,
            text={"format": {"type": "json_object"}},
        )
        usage.record(response)
        data = json.loads(response.output_text)
        return str(data.get("overview") or "")
    except Exception:
        return ""


def evaluate_with_openai(articles: list[RankedArticle], report: dict[str, object]) -> dict[str, object]:
    if not os.getenv("OPENAI_API_KEY"):
        report["llm_evaluation"] = "OPENAI_API_KEY가 없어 휴리스틱 평가만 수행했습니다."
        return report
    client = OpenAI(timeout=float(os.getenv("OPENAI_TIMEOUT", "120")), max_retries=1)
    model = os.getenv("CRITIC_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.4-mini"))
    # 평가에는 편집 결과 요약만 있으면 된다 — 원문 전문까지 보내면 요청이
    # 너무 커져 프록시가 끊을 수 있다.
    slim = [
        {
            "title": a.korean_title or a.title,
            "summary": a.korean_summary,
            "why_it_matters": a.why_it_matters,
            "section": a.section,
            "source": a.source_name,
        }
        for a in articles
    ]
    prompt = (
        "아래 주간 AI 뉴스레터 후보를 사내 게시용 관점에서 평가해줘. "
        "중복성, 중요도, 최신성, 한국어 품질, 내부 업무 시사점 기준으로 100점 만점과 짧은 개선 코멘트를 한국어 JSON으로 반환해.\n\n"
        f"{json.dumps(slim, ensure_ascii=False)}"
    )
    try:
        response = client.responses.create(
            model=model,
            input=prompt,
            text={"format": {"type": "json_object"}},
        )
        usage.record(response)
        report["llm_evaluation"] = json.loads(response.output_text)
    except Exception as exc:
        report["llm_evaluation"] = f"평가 호출 실패(빌드는 계속): {exc}"
    return report


# ---------------------------------------------------------------------------
# Humanize pass: 린터에 걸린 필드만 의미 보존 조건으로 국소 재작성한다.
# 전체 재생성이 아니라 플래그된 텍스트만 다듬으므로 토큰 비용이 작고,
# 이미 저장된 산출물(data/selected_articles.json)에 사후 적용할 수 있다.

_HUMANIZE_RULES = (
    "규칙 — 반드시 지켜:\n"
    "1) 사실·수치·고유명사·날짜·인용을 바꾸거나 새로 만들지 마.\n"
    "2) 문장 수는 ±1, 전체 길이는 ±20% 안에서 유지해.\n"
    "3) problems에 지적된 표현과 어미 리듬만 고치고 나머지 문장은 그대로 둬.\n"
    "4) 새 감탄·반문·비유를 만들지 말고, 뜻이 같은 자연스러운 한국어로만 바꿔.\n"
    "5) 같은 종결어미가 3문장 이상 이어지지 않게 문장을 합치거나 어미를 바꿔."
)

_DETAIL_WHERE_RE = re.compile(r"detail_sections\[(\d+)\]")


def _get_field_text(row: dict[str, object], where: str) -> str:
    match = _DETAIL_WHERE_RE.match(where)
    if match:
        sections = row.get("detail_sections") or []
        idx = int(match.group(1))
        if isinstance(sections, list) and idx < len(sections):
            return str(sections[idx].get("body") or "")
        return ""
    return str(row.get(where) or "")


def _set_field_text(row: dict[str, object], where: str, text: str) -> None:
    match = _DETAIL_WHERE_RE.match(where)
    if match:
        row["detail_sections"][int(match.group(1))]["body"] = text
    else:
        row[where] = text


def humanize_articles_data(rows: list[dict[str, object]]) -> dict[str, object]:
    """선정 기사 JSON 행을 린트하고, 플래그된 필드와 제목 틀 쏠림을 국소 재작성한다.

    rows를 제자리에서 수정하고 변경 내역을 반환한다 (전후 비교·감사용).
    """
    from .style_lint import lint_article_fields, lint_titles

    result: dict[str, object] = {"changes": [], "title_changes": [], "calls": 0}
    if not os.getenv("OPENAI_API_KEY"):
        return result
    client = OpenAI(timeout=float(os.getenv("ENRICH_TIMEOUT", "150")), max_retries=1)
    model = os.getenv("OPENAI_MODEL", "gpt-5.4")

    def _rewrite_row(row: dict[str, object]) -> list[dict[str, str]]:
        flags = lint_article_fields(row)
        if not flags:
            return []
        by_where: dict[str, list] = {}
        for flag in flags:
            by_where.setdefault(flag.where, []).append(flag)
        payload = {
            where: {
                "text": _get_field_text(row, where),
                "problems": [f"{f.label} — 처방: {f.fix_hint}" for f in fs],
            }
            for where, fs in by_where.items()
        }
        prompt = (
            "다음은 사내 AI 뉴스레터에 실릴 한국어 텍스트다. 각 항목의 problems에 "
            "적힌 AI 문체 신호만 국소적으로 고쳐라.\n"
            f"{_HUMANIZE_RULES}\n"
            "입력과 같은 키로 {\"필드명\": \"수정된 전체 텍스트\"} JSON만 반환해.\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            response = client.responses.create(
                model=model, input=prompt, text={"format": {"type": "json_object"}}
            )
            usage.record(response)
            edited = json.loads(response.output_text)
        except Exception:
            return []
        result["calls"] = int(result["calls"]) + 1
        changed = []
        for where in by_where:
            new_text = edited.get(where)
            if isinstance(new_text, dict):  # 모델이 {"text": ...} 형태로 감싼 경우
                new_text = new_text.get("text")
            old_text = _get_field_text(row, where)
            if isinstance(new_text, str) and new_text.strip() and new_text.strip() != old_text:
                _set_field_text(row, where, new_text.strip())
                changed.append({
                    "article": str(row.get("korean_title") or row.get("title") or ""),
                    "where": where,
                    "before": old_text,
                    "after": new_text.strip(),
                    "problems": [f.label for f in by_where[where]],
                })
        return changed

    with ThreadPoolExecutor(max_workers=4) as executor:
        for changed in executor.map(_rewrite_row, rows):
            result["changes"].extend(changed)  # type: ignore[union-attr]

    # 제목은 개별 기사가 아니라 목록의 틀 쏠림이 신호라 한 번에 다룬다.
    titles = [str(r.get("korean_title") or "") for r in rows]
    title_flags = lint_titles(titles)
    if title_flags:
        problems = "; ".join(f"{f.label} — 처방: {f.fix_hint}" for f in title_flags)
        prompt = (
            "다음은 이번 호 사내 AI 뉴스레터의 기사 제목 목록이다. "
            f"목록 전체의 문제: {problems}\n"
            "사실과 고유명사를 바꾸지 말고 각 제목을 28자 안팎으로 유지하면서, "
            "제목 틀이 다양해지도록 다시 써라.\n"
            "핵심 요건: '회사명, ~' 콤마 틀은 전체의 절반 이하여야 한다. 끝 단어만 "
            "바꾸는 것은 다양화가 아니다 — 초과분은 문장 구조 자체를 바꿔라.\n"
            "대체 틀 예시 (사실은 각 기사 것을 유지):\n"
            "  - 문장형: '구글, Gemini 공개' → 'Gemini가 3.7로 빨라졌다'\n"
            "  - 수치형: '회사, 신모드 공개' → '7배 빨라진 추론 모드'\n"
            "  - 결과형: '오라클, 정책 발표' → '오라클이 AI 코드를 막았다'\n"
            "  - 주제형: '연구진, 기법 제안' → 'KV 캐시 병목을 줄이는 새 기법'\n"
            "끝 단어도 '공개/출시/발표'로 몰지 마. 다만 다양화하려고 '내다'처럼 "
            "다의적인 축약 동사로 행위를 흐리지 마 — 제목만 읽어도 무엇을 했는지 "
            "바로 잡히는 구체적 동사를 유지해라. 바꿀 필요 없는 제목은 그대로 둬.\n"
            "{\"titles\": [...]} JSON만 반환해 (순서 유지, 개수 동일).\n\n"
            f"{json.dumps(titles, ensure_ascii=False)}"
        )
        try:
            response = client.responses.create(
                model=model, input=prompt, text={"format": {"type": "json_object"}}
            )
            usage.record(response)
            new_titles = json.loads(response.output_text).get("titles")
            result["calls"] = int(result["calls"]) + 1
            if isinstance(new_titles, list) and len(new_titles) == len(rows):
                for row, old, new in zip(rows, titles, new_titles):
                    if isinstance(new, str) and new.strip() and new.strip() != old:
                        row["korean_title"] = new.strip()
                        result["title_changes"].append(  # type: ignore[union-attr]
                            {"before": old, "after": new.strip()}
                        )
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# 독자 감수 패스: 정규식 린터는 알려진 패턴만 잡는다. 여기서는 반대로
# '뜻을 두 번 읽어야 하거나 AI 티가 나는' 문장을 사람 독자 관점에서 LLM이
# 직접 고르게 한다. 과교정을 막기 위해 호 전체에서 최대 5개로 제한하고,
# 자연스러운 문장을 억지로 지적하지 말라고 명시한다.

_REVIEW_WHERE_RE = re.compile(r"^(\d+):(.+)$")


def reader_review_pass(rows: list[dict[str, object]]) -> dict[str, object]:
    """린트 규칙이 못 잡는 '어렵고 겉멋 든 문장'을 LLM 감수로 찾아 국소 치환한다.

    humanize_articles_data(규칙 위반 → 재작성)와 달리 진단 기준이 독자 체감이라
    모델이 스스로 최대 5개만 고르게 하고, 치환은 humanize와 동일하게 원문 문장이
    필드 텍스트에 그대로 있을 때만(문자열 치환) 적용한다.
    """
    from .style_lint import TEXT_FIELDS

    result: dict[str, object] = {"flags": [], "applied": 0, "skipped": 0, "calls": 0}
    if not os.getenv("OPENAI_API_KEY") or not rows:
        return result
    client = OpenAI(timeout=float(os.getenv("ENRICH_TIMEOUT", "150")), max_retries=1)
    model = os.getenv("CRITIC_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.4-mini"))

    # 위치 키는 "행 인덱스:필드명" — 감수 대상이 여러 기사에 걸쳐 있어
    # humanize의 필드명 단독 where로는 어느 기사인지 구분할 수 없다.
    payload: dict[str, str] = {}
    for i, row in enumerate(rows):
        for field in TEXT_FIELDS:
            value = row.get(field)
            if isinstance(value, str) and value.strip():
                payload[f"{i}:{field}"] = value
        sections = row.get("detail_sections")
        if isinstance(sections, list):
            for j, sec in enumerate(sections):
                if isinstance(sec, dict) and sec.get("body"):
                    payload[f"{i}:detail_sections[{j}]"] = str(sec["body"])
    if not payload:
        return result

    prompt = (
        "너는 금융IT 기업의 비개발 직원이다. 이 뉴스레터를 소리 내어 읽는다고 상상하고, "
        "(a) 뜻을 두 번 읽어야 이해되는 문장 (b) 겉멋 들거나 AI가 쓴 티가 나는 문장을 "
        "전체에서 최대 5개만 골라라. 각 항목: 필드 위치(where — 아래 입력 JSON의 키를 "
        "그대로), 원문 문장(original — 입력 텍스트에서 그대로 발췌, 새로 쓰지 마), "
        "왜 어색한지 한 줄(why), 같은 뜻의 쉬운 대안 한 문장(alt)을 담아라. "
        "이 호에서 가장 자연스럽게 읽히는 문단 하나를 기준 결로 삼아, alt도 그 결에 맞춰라. "
        "자연스러운 문장을 억지로 지적하지 마라 — 지적할 문장이 없으면 flags를 빈 "
        "배열로 반환해.\n"
        "{\"flags\": [{\"where\": \"...\", \"original\": \"...\", \"why\": \"...\", "
        "\"alt\": \"...\"}]} 형태의 JSON만 반환해.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        response = client.responses.create(
            model=model, input=prompt, text={"format": {"type": "json_object"}}
        )
        usage.record(response)
        data = json.loads(response.output_text)
    except Exception:
        return result
    result["calls"] = 1
    flags = data.get("flags")
    if not isinstance(flags, list):
        return result

    for item in flags[:5]:
        if not isinstance(item, dict):
            continue
        where = str(item.get("where") or "")
        original = str(item.get("original") or "")
        alt = str(item.get("alt") or "")
        why = str(item.get("why") or "")
        entry = {"where": where, "original": original, "why": why, "alt": alt, "applied": False}
        match = _REVIEW_WHERE_RE.match(where)
        if not match or not original or not alt:
            result["skipped"] = int(result["skipped"]) + 1
            result["flags"].append(entry)  # type: ignore[union-attr]
            continue
        idx, field = int(match.group(1)), match.group(2)
        if idx < 0 or idx >= len(rows):
            result["skipped"] = int(result["skipped"]) + 1
            result["flags"].append(entry)  # type: ignore[union-attr]
            continue
        row = rows[idx]
        text = _get_field_text(row, field)
        if original in text:
            _set_field_text(row, field, text.replace(original, alt, 1))
            entry["applied"] = True
            result["applied"] = int(result["applied"]) + 1
        else:
            result["skipped"] = int(result["skipped"]) + 1
        result["flags"].append(entry)  # type: ignore[union-attr]
    return result
