from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

from openai import OpenAI

from .models import Article, Issue, RankedArticle
from .ranking import score_article
from .usage import usage

FOUNDATION_SOURCE_IDS = {
    "openai-news",
    "openai-developers-blog",
    "anthropic-news",
    "claude-blog",
    "google-deepmind",
    "huggingface-blog",
}

AUTHORITATIVE_SOURCE_IDS = FOUNDATION_SOURCE_IDS | {
    "thenewstack",
    "pytorch-kr-blog",
    "marktechpost",
}

TOPIC_KEYWORDS = {
    "agent": ["agent", "agentic", "agents", "에이전트", "tool calling", "tools"],
    "security": ["security", "vulnerability", "patch", "sandbox", "보안", "취약점"],
    "open-model": ["open model", "glm", "gemma", "qwen", "llama", "mistral", "오픈 모델"],
    "reasoning": ["reasoning", "thinking", "추론"],
    "developer-platform": ["api", "sdk", "codex", "developer", "github actions", "개발자"],
    "enterprise-workflow": ["slack", "teams", "enterprise", "workflow", "업무", "협업"],
    "benchmark": ["benchmark", "eval", "evaluation", "벤치마크", "평가"],
}


def build_issue_radar(
    candidates: list[Article],
    limit: int = 4,
    use_llm: bool = True,
) -> tuple[list[Issue], list[RankedArticle]]:
    recent = [article for article in candidates if article.published_at is not None]
    ranked_pool = sorted((score_article(article) for article in recent), key=lambda row: row.score, reverse=True)
    issue_candidates = ranked_pool[:60]
    issues = _build_llm_issues(issue_candidates, limit) if use_llm and os.getenv("OPENAI_API_KEY") else []
    if not issues:
        issues = _build_heuristic_issues(issue_candidates, limit)
    issues = _score_and_select_representatives(issues, issue_candidates)
    selected = _select_articles_for_issues(issues, issue_candidates, max_articles=10)
    return issues, selected


def _build_llm_issues(articles: list[RankedArticle], limit: int) -> list[Issue]:
    client = OpenAI()
    model = os.getenv("CRITIC_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.4-mini"))
    payload = [
        {
            "id": article.id,
            "source_id": article.source_id,
            "source_name": article.source_name,
            "panel": article.panel,
            "authority_tier": article.authority_tier,
            "title": article.title,
            "summary": article.summary,
            "body_excerpt": (article.body or "")[:1200],
            "published_at": article.published_at.isoformat() if article.published_at else None,
            "url": article.url,
        }
        for article in articles
    ]
    prompt = (
        "이번 주 AI 뉴스 후보를 보고 '이슈 레이더'용 핵심 이슈를 골라줘. "
        "모든 title, summary, why_hot, enterprise_relevance는 자연스러운 한국어로 작성해. "
        "기사 자체가 아니라 여러 자료를 묶는 주제(issue)를 만들어야 해. "
        "파운데이션 모델 제공업체(OpenAI, Anthropic/Claude, Google DeepMind, Hugging Face)의 공식/기술 글은 항상 강하게 고려해. "
        "단, 단일 출처 하나만으로 이슈를 확정하지 마. authority, curator, developer, community, model-hub 같은 서로 다른 패널에서 독립 신호가 반복되는 주제를 우선해. "
        "사용자나 특정 기사 하나의 주장에 과적합하지 말고, 공식 출처와 큐레이터/개발자 커뮤니티 신호가 서로 보강되는지 확인해. "
        "각 이슈는 최신성, 출처 권위, 패널 다양성, 사내 업무 영향도, 기술 깊이를 기준으로 골라. "
        f"최대 {limit}개만 반환해. 반드시 JSON 객체 "
        "{\"issues\":[...]} 형태로 반환하고, "
        "각 항목은 id, title, summary, why_hot, enterprise_relevance, keywords, article_ids, representative_article_id를 포함해. "
        "representative_article_id는 해당 이슈를 설명하기 가장 권위 있는 article id로 골라. "
        "공식 제공사 글이 있으면 일반 매체보다 우선해. 원문에 없는 사실은 만들지 마.\n\n"
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
    except Exception:
        return []
    rows = data.get("issues") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    issues: list[Issue] = []
    for idx, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        issues.append(
            Issue(
                id=str(row.get("id") or f"issue-{idx}"),
                title=title,
                summary=str(row.get("summary") or ""),
                why_hot=str(row.get("why_hot") or ""),
                enterprise_relevance=str(row.get("enterprise_relevance") or ""),
                keywords=[str(item) for item in row.get("keywords", []) if item],
                article_ids=[str(item) for item in row.get("article_ids", []) if item],
                representative_article_id=str(row.get("representative_article_id") or ""),
            )
        )
    return issues[:limit]


def _build_heuristic_issues(articles: list[RankedArticle], limit: int) -> list[Issue]:
    buckets: dict[str, list[RankedArticle]] = defaultdict(list)
    for article in articles:
        text = f"{article.title} {article.summary} {' '.join(article.tags)}".lower()
        matched = False
        for key, keywords in TOPIC_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                buckets[key].append(article)
                matched = True
        if not matched:
            buckets["general-ai"].append(article)
    issues: list[Issue] = []
    for key, rows in buckets.items():
        if not rows:
            continue
        title = _issue_title(key)
        issues.append(
            Issue(
                id=key,
                title=title,
                summary=f"이번 주 {title} 관련 자료가 여러 출처에서 확인됐습니다.",
                why_hot="최근성, 출처 권위, 개발자/기업 적용 가능성을 기준으로 묶은 이슈입니다.",
                enterprise_relevance="사내 AI 도입, 개발 생산성, 보안·운영 리스크 판단에 참고할 수 있습니다.",
                keywords=[key],
                article_ids=[article.id for article in rows[:6]],
                representative_article_id=rows[0].id,
            )
        )
    return issues[:limit]


def _score_and_select_representatives(
    issues: list[Issue],
    articles: list[RankedArticle],
) -> list[Issue]:
    by_id = {article.id: article for article in articles}
    for issue in issues:
        linked = [by_id[item] for item in issue.article_ids if item in by_id]
        if not linked:
            linked = _match_articles_by_keywords(issue, articles)
            issue.article_ids = [article.id for article in linked[:6]]
        else:
            expanded = _match_articles_by_keywords(issue, articles)
            seen = {article.id for article in linked}
            for article in expanded:
                if article.id not in seen:
                    linked.append(article)
                    seen.add(article.id)
                if len(linked) >= 8:
                    break
            issue.article_ids = [article.id for article in linked[:8]]
        if linked:
            representative = _choose_representative(linked)
            issue.representative_article_id = representative.id
        issue.score_breakdown = _issue_score_breakdown(issue, linked)
        issue.score = round(sum(issue.score_breakdown.values()), 3)
    issues.sort(key=lambda item: item.score, reverse=True)
    return issues


def _select_articles_for_issues(
    issues: list[Issue],
    articles: list[RankedArticle],
    max_articles: int,
) -> list[RankedArticle]:
    by_id = {article.id: article for article in articles}
    selected: list[RankedArticle] = []
    seen: set[str] = set()
    for issue in issues:
        ids = [issue.representative_article_id, *issue.article_ids]
        for article_id in ids:
            article = by_id.get(article_id)
            if not article or article.id in seen:
                continue
            selected.append(article)
            seen.add(article.id)
            break
    for article in sorted(articles, key=lambda row: _article_authority_score(row), reverse=True):
        if article.id in seen:
            continue
        selected.append(article)
        seen.add(article.id)
        if len(selected) >= max_articles:
            break
    return selected[:max_articles]


def _match_articles_by_keywords(issue: Issue, articles: list[RankedArticle]) -> list[RankedArticle]:
    keywords = [keyword.lower() for keyword in issue.keywords]
    title_words = re.findall(r"[a-zA-Z0-9가-힣.\-]+", issue.title.lower())
    keywords.extend(word for word in title_words if len(word) >= 3)
    matched = []
    for article in articles:
        text = f"{article.title} {article.summary} {article.body[:1000]}".lower()
        if any(keyword in text for keyword in keywords):
            matched.append(article)
    return sorted(matched, key=lambda row: _article_authority_score(row), reverse=True)


def _choose_representative(articles: list[RankedArticle]) -> RankedArticle:
    return sorted(articles, key=lambda row: _article_authority_score(row), reverse=True)[0]


def _article_authority_score(article: RankedArticle) -> float:
    score = article.score
    score += article.authority_tier * 4
    if article.source_id in FOUNDATION_SOURCE_IDS:
        score += 8
    elif article.source_id in AUTHORITATIVE_SOURCE_IDS:
        score += 4
    if article.body and len(article.body) > 2500:
        score += 2
    if article.source_id.startswith("github"):
        score -= 1.5
    if article.source_id == "huggingface-models":
        score += 1
    if article.published_at:
        age_hours = max((datetime.now(timezone.utc) - article.published_at).total_seconds() / 3600, 0)
        score += max(0, 3 - age_hours / 48)
    return score


def _issue_score_breakdown(issue: Issue, articles: list[RankedArticle]) -> dict[str, float]:
    sources = {article.source_id for article in articles}
    panels = {article.panel for article in articles}
    foundation = 2.5 if any(article.source_id in FOUNDATION_SOURCE_IDS for article in articles) else 0.0
    source_diversity = min(2.0, len(sources) * 0.4)
    panel_diversity = min(2.5, len(panels) * 0.7)
    cross_validation = _cross_validation_score(articles)
    freshness = 0.0
    for article in articles:
        if article.published_at:
            age_hours = max((datetime.now(timezone.utc) - article.published_at).total_seconds() / 3600, 0)
            freshness = max(freshness, max(0.0, 2.0 - age_hours / 84))
    depth = min(2.0, sum(1 for article in articles if len(article.body or "") > 2500) * 0.6)
    relevance = 1.5 if issue.enterprise_relevance else 0.8
    single_source_penalty = -2.0 if len(sources) <= 1 else 0.0
    return {
        "foundation_authority": foundation,
        "source_diversity": source_diversity,
        "panel_diversity": panel_diversity,
        "cross_validation": cross_validation,
        "freshness": round(freshness, 3),
        "technical_depth": depth,
        "enterprise_relevance": relevance,
        "single_source_penalty": single_source_penalty,
    }


def _cross_validation_score(articles: list[RankedArticle]) -> float:
    if not articles:
        return 0.0
    has_authority = any(article.panel == "authority" for article in articles)
    has_curator = any(article.panel == "curator" for article in articles)
    has_dev = any(article.panel in {"developer", "model-hub", "community"} for article in articles)
    score = 0.0
    if has_authority and has_curator:
        score += 1.5
    if has_authority and has_dev:
        score += 1.2
    if has_curator and has_dev:
        score += 0.8
    if len({article.source_id for article in articles}) >= 3:
        score += 0.7
    return min(score, 3.0)


def _issue_title(key: str) -> str:
    return {
        "agent": "에이전트와 도구 사용",
        "security": "에이전트 보안과 자동 패치",
        "open-model": "오픈 모델 실행 가능성 경쟁",
        "reasoning": "추론 모델과 사고 모드",
        "developer-platform": "개발자 플랫폼과 API",
        "enterprise-workflow": "업무형 AI 인터페이스",
        "benchmark": "에이전트 평가와 벤치마크",
        "general-ai": "주요 AI 기술 흐름",
    }.get(key, key)
