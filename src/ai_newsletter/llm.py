from __future__ import annotations

import json
import os

from openai import OpenAI

from .models import RankedArticle
from .usage import usage


def enrich_with_openai(articles: list[RankedArticle]) -> list[RankedArticle]:
    if not os.getenv("OPENAI_API_KEY"):
        return articles
    client = OpenAI()
    model = os.getenv("OPENAI_MODEL", "gpt-5.4")
    payload = [
        {
            "index": idx,
            "title": a.title,
            "source": a.source_name,
            "summary": a.summary,
            "body_excerpt": (a.body or "")[:9000],
            "url": a.url,
            "reason": a.reason,
        }
        for idx, a in enumerate(articles)
    ]
    prompt = (
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
    response = client.responses.create(
        model=model,
        input=prompt,
        text={"format": {"type": "json_object"}},
    )
    usage.record(response)
    try:
        data = json.loads(response.output_text)
        rows = _extract_rows(data)
        for row in rows:
            if not isinstance(row, dict):
                continue
            idx = row.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(articles):
                continue
            article = articles[idx]
            article.korean_title = row.get("korean_title") or article.korean_title
            article.korean_summary = row.get("korean_summary") or article.korean_summary
            article.why_it_matters = row.get("why_it_matters") or article.why_it_matters
            article.terms = _normalize_terms(row.get("terms")) or article.terms
            article.detail_intro = row.get("detail_intro") or article.detail_intro
            article.detail_sections = _normalize_sections(row) or article.detail_sections
            _clean_article_text(article)
    except Exception:
        return articles
    for idx, article in enumerate(articles):
        if _needs_retry(article):
            _enrich_one(client, model, idx, article)
    return articles


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


def _enrich_one(client: OpenAI, model: str, index: int, article: RankedArticle) -> None:
    payload = {
        "index": index,
        "title": article.title,
        "source": article.source_name,
        "summary": article.summary,
        "body_excerpt": (article.body or "")[:9000],
        "url": article.url,
    }
    prompt = (
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
    row = rows[0]
    article.korean_title = row.get("korean_title") or article.korean_title
    article.korean_summary = row.get("korean_summary") or article.korean_summary
    article.why_it_matters = row.get("why_it_matters") or article.why_it_matters
    article.terms = _normalize_terms(row.get("terms")) or article.terms
    article.detail_intro = row.get("detail_intro") or article.detail_intro
    article.detail_sections = _normalize_sections(row) or article.detail_sections
    _clean_article_text(article)


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


def evaluate_with_openai(articles: list[RankedArticle], report: dict[str, object]) -> dict[str, object]:
    if not os.getenv("OPENAI_API_KEY"):
        report["llm_evaluation"] = "OPENAI_API_KEY가 없어 휴리스틱 평가만 수행했습니다."
        return report
    client = OpenAI()
    model = os.getenv("CRITIC_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.4-mini"))
    prompt = (
        "아래 주간 AI 뉴스레터 후보 10개를 사내 게시용 관점에서 평가해줘. "
        "중복성, 중요도, 최신성, 한국어 품질, 내부 업무 시사점 기준으로 100점 만점과 짧은 개선 코멘트를 한국어 JSON으로 반환해.\n\n"
        f"{json.dumps([a.model_dump(mode='json') for a in articles], ensure_ascii=False)}"
    )
    response = client.responses.create(
        model=model,
        input=prompt,
        text={"format": {"type": "json_object"}},
    )
    usage.record(response)
    try:
        report["llm_evaluation"] = json.loads(response.output_text)
    except Exception:
        report["llm_evaluation"] = response.output_text
    return report
