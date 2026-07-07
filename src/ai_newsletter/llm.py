from __future__ import annotations

import json
import os
import re

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
    "용어 원칙: 전문 용어는 처음 등장할 때 괄호로 한 줄 풀어 써 — "
    "예: 온프레미스(외부 클라우드 없이 사내 서버에 설치해 쓰는 방식), "
    "오픈웨이트(open-weight, 모델 가중치를 공개해 직접 내려받아 쓸 수 있는 것), "
    "추론(inference, 학습된 모델로 답을 생성하는 단계). "
    "풀이는 짧게 괄호로 처리해 본문 흐름을 끊지 말고, 같은 용어를 두 번 풀지 마.\n"
    "문체 원칙: 영어 음차 남발 금지, 번역투('~에 대하여', '~를 통해', '~됨에 따라') 지양, "
    "자연스러운 한국어 문장으로. 친절하되 독자를 가르치려 드는 말투('쉽게 말해서', "
    "'~라고 생각하면 된다'의 반복)는 피해."
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
        "1) '핵심 브리핑' — 사실만. 원문에서 확인되는 발표/변경/수치를 4-10문장으로 상세히. "
        "스펙·설정값·가격·제약처럼 나열이 읽기 쉬운 내용은 '- '로 시작하는 목록을 적극 활용해.\n"
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
        "새 문장을 만들지 말고 본문 문장을 문자 그대로 인용해. 결론을 다 말해버리는 문장보다 "
        "뒷이야기가 궁금해지는 문장을 골라. 40-90자 권장, 목록 항목 말고 서술 문장에서), "
        "korean_title(28자 안팎 — 제목만 읽어도 '누가 무엇을 했는지' 아는 직관적인 문장. "
        "회사명·행위를 명확히: 'Sonnet 5 공개'보다 '앤스로픽, 에이전트 특화 모델 Sonnet 5 공개'. "
        "업계 은어·영어 약어 나열·과장 금지), "
        "korean_summary(2-3문장 — 첫 문장이 야마: 이 사건의 핵심을 비전문가도 바로 잡게), "
        "why_it_matters(1-2문장), "
        "terms(3-6개 배열, 각 항목은 '용어(영문): 한 줄 풀이' 형식 — 이 기사를 이해하는 데 "
        "필요한 개념을 내부 학습용으로 고르되 본문에서 이미 충분히 풀린 것은 제외), "
        "detail_intro(브리핑처럼 맥락을 여는 도입부 3-5문장).\n"
        "문장은 70자 안팎, 문단은 2-3문장 단위로 짧게. "
        "'원문에 의하면', '기사에 따르면' 같은 출처 표지 문구는 쓰지 마. "
        "GitHub stars/forks/downloads/score 같은 정량 지표나 선별 점수는 본문에 쓰지 마. "
        "원문에 없는 사실은 만들지 마.\n"
        "반드시 {\"articles\":[{\"index\":정수, \"one_liner\":..., \"hook\":..., \"korean_title\":..., "
        "\"korean_summary\":..., \"why_it_matters\":..., \"terms\":[...], \"detail_intro\":..., "
        "\"detail_sections\":[{\"heading\":\"핵심 브리핑\",\"body\":...}, "
        "{\"heading\":\"(섹션 특화 소제목)\",\"body\":...}, {\"heading\":\"업계의 움직임\",\"body\":...}, "
        "{\"heading\":\"시사점과 체크포인트\",\"body\":...}]}]} 형태의 JSON만 반환해. "
        "모든 index를 빠짐없이 포함해.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _article_payload(idx: int, a: RankedArticle) -> dict[str, object]:
    return {
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


def enrich_with_openai(
    articles: list[RankedArticle], *, structured: bool = False
) -> list[RankedArticle]:
    if not os.getenv("OPENAI_API_KEY"):
        return articles
    client = OpenAI()
    model = os.getenv("OPENAI_MODEL", "gpt-5.4")
    # 기사 10건×본문 9천자를 한 요청에 담으면 사내 프록시가 대형 POST를 끊는
    # 경우가 있어(RemoteProtocolError) 작은 배치로 나눠 보낸다. 배치가 실패해도
    # 빌드를 죽이지 않고, 남은 기사는 아래 개별 재시도(_enrich_one)가 처리한다.
    batch_size = max(1, int(os.getenv("ENRICH_BATCH_SIZE", "4")))
    for start in range(0, len(articles), batch_size):
        chunk = list(enumerate(articles))[start : start + batch_size]
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
            continue  # 이 배치 기사들은 아래 개별 재시도로 넘어간다
        for row in rows:
            if not isinstance(row, dict):
                continue
            idx = row.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(articles):
                continue
            _apply_row(articles[idx], row, structured=structured)
    for idx, article in enumerate(articles):
        if _needs_retry(article):
            _enrich_one(client, model, idx, article, structured=structured)
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
    sections: list[dict[str, str]], section: str
) -> list[dict[str, str]]:
    """Keep the skeleton order for this article's section; unknown headings are
    appended at the end so an off-script LLM response degrades gracefully
    instead of losing text. The generic fallback heading is accepted in the
    specialized slot's position (the LLM downgrades to it when the source
    lacks the specialized info)."""
    expected = structured_headings(section)
    if _DEFAULT_SLOT_HEADING not in expected:
        expected = expected[:1] + [expected[1], _DEFAULT_SLOT_HEADING] + expected[2:]
    by_heading = {s["heading"].strip(): s for s in sections}
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


def _normalize_sections(row: dict[str, object]) -> list[dict[str, str]]:
    value = row.get("detail_sections") or row.get("sections") or row.get("article_sections")
    if isinstance(value, dict):
        value = [{"heading": key, "body": body} for key, body in value.items()]
    if not isinstance(value, list):
        return []
    sections: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            continue
        heading = item.get("heading") or item.get("title") or item.get("section")
        body = item.get("body") or item.get("content") or item.get("text")
        if heading and body:
            sections.append({"heading": str(heading), "body": str(body)})
    return sections


def _clean_article_text(article: RankedArticle) -> None:
    article.korean_summary = _remove_source_markers(article.korean_summary)
    article.why_it_matters = _remove_source_markers(article.why_it_matters)
    article.detail_intro = _remove_source_markers(article.detail_intro)
    article.detail_sections = [
        {
            "heading": _remove_source_markers(section.get("heading", "")),
            "body": _remove_source_markers(section.get("body", "")),
        }
        for section in article.detail_sections
    ]


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
    final human check, but the flags say exactly where to look."""
    flags: list[dict[str, object]] = []
    for idx, article in enumerate(articles, 1):
        source = _normalized_digits(f"{article.title} {article.summary} {article.body}")
        for section in article.detail_sections:
            missing = [
                number
                for number in _significant_numbers(section.get("body", ""))
                if number not in source
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


def _normalized_digits(text: str) -> str:
    return re.sub(r"[,\s]", "", text)


def _significant_numbers(text: str) -> list[str]:
    """Numbers worth verifying: 2+ digits, excluding plain years (too noisy)."""
    normalized = re.sub(r"[,\s]", "", text)
    numbers = set(re.findall(r"\d{2,}(?:\.\d+)?", normalized))
    return sorted(n for n in numbers if not re.fullmatch(r"(?:19|20)\d{2}", n))


def generate_weekly_overview(articles: list[RankedArticle]) -> str:
    """One short editorial paragraph answering '이번 주 세상이 어떻게 돌아갔나' —
    synthesized from the selected stories, shown at the top of the newsletter."""
    if not os.getenv("OPENAI_API_KEY") or not articles:
        return ""
    client = OpenAI()
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
    client = OpenAI()
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
