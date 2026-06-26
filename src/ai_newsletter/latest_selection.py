from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Article, RankedArticle, SourceConfig
from .ranking import deduplicate, score_article

LATEST_KEYWORDS = {
    "agent": 1.6,
    "agentic": 1.7,
    "ai agent": 1.7,
    "llm": 1.4,
    "model": 1.1,
    "reasoning": 1.3,
    "benchmark": 1.0,
    "security": 1.4,
    "vulnerability": 1.3,
    "inference": 1.2,
    "open model": 1.2,
    "enterprise": 1.1,
    "governance": 1.1,
    "오픈AI": 1.3,
    "에이전트": 1.6,
    "보안": 1.4,
    "모델": 1.1,
}


def default_latest_source_ids(sources: list[SourceConfig]) -> set[str]:
    return {source.id for source in sources if source.enabled and source.kind in {"rss", "webpage"}}


def select_latest_articles(
    candidates: list[Article],
    sources: list[SourceConfig],
    *,
    days: int,
    limit: int = 10,
    source_ids: set[str] | None = None,
    fallback_source_ids: set[str] | None = None,
    per_source_limit: int = 3,
) -> list[RankedArticle]:
    allowed = source_ids or default_latest_source_ids(sources)
    source_by_id = {source.id: source for source in sources}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    primary = [
        article
        for article in deduplicate(candidates)
        if article.source_id in allowed
        and article.published_at is not None
        and article.published_at >= cutoff
        and _looks_like_article(article, source_by_id.get(article.source_id))
    ]
    ranked = _rank_latest(primary)

    selected = _pick_with_source_cap(ranked, limit=limit, per_source_limit=per_source_limit)

    if len(selected) < limit:
        seen = {article.id for article in selected}
        for article in ranked:
            if article.id in seen:
                continue
            selected.append(article)
            seen.add(article.id)
            if len(selected) >= limit:
                break
    if len(selected) < limit and fallback_source_ids:
        seen = {article.id for article in selected}
        fallback = [
            article
            for article in deduplicate(candidates)
            if article.source_id in fallback_source_ids
            and article.source_id not in allowed
            and article.published_at is not None
            and article.published_at >= cutoff
            and _looks_like_article(article, source_by_id.get(article.source_id))
            and article.id not in seen
        ]
        fallback_ranked = _rank_latest(fallback)
        selected.extend(fallback_ranked[: max(0, limit - len(selected))])
    return selected[:limit]


def score_latest_article(article: Article) -> RankedArticle:
    base = score_article(article)
    text = f"{article.title} {article.summary} {article.body[:2000]} {' '.join(article.tags)}".lower()
    keyword_score = sum(weight for keyword, weight in LATEST_KEYWORDS.items() if keyword.lower() in text)
    recency_score = _freshness_score(article)
    authority_score = article.authority_tier * 2.0 + article.source_weight
    body_score = min(1.5, len(article.body or "") / 4000)
    score = recency_score * 3.0 + authority_score * 2.0 + keyword_score * 1.2 + body_score
    base.score = round(score, 3)
    base.score_breakdown.update(
        {
            "latest_recency": round(recency_score, 3),
            "latest_authority": round(authority_score, 3),
            "latest_keywords": round(keyword_score, 3),
            "body_depth": round(body_score, 3),
        }
    )
    base.reason = "최근 1주 내 게시된 지정 사이트 최신 아티클 기준으로 선별"
    return base


def latest_quality_report(
    selected: list[RankedArticle],
    candidates: list[Article],
    *,
    source_ids: set[str],
    fallback_source_ids: set[str] | None = None,
    days: int,
) -> dict[str, object]:
    candidate_source_counts: dict[str, int] = {}
    for article in candidates:
        if article.source_id in source_ids:
            candidate_source_counts[article.source_name] = candidate_source_counts.get(article.source_name, 0) + 1
    selected_source_counts: dict[str, int] = {}
    for article in selected:
        selected_source_counts[article.source_name] = selected_source_counts.get(article.source_name, 0) + 1
    return {
        "mode": "latest-sites",
        "window_days": days,
        "candidate_count": len(candidates),
        "eligible_source_ids": sorted(source_ids),
        "fallback_source_ids": sorted(fallback_source_ids or []),
        "selected_count": len(selected),
        "selected_source_counts": selected_source_counts,
        "candidate_source_counts": candidate_source_counts,
        "selection_contract": {
            "rule": "지정 사이트의 최근 1주일 내 날짜 확인 기사 중 10개 선발",
            "date_required": True,
            "strict_week": True,
            "platform_sources": "github/huggingface API 소스는 기본 제외",
            "ranking": "최신성, 출처 권위, AI 관련성, 원문 본문 정보량",
            "fallback": "지정 사이트에서 10개 미만이면 fallback_source_ids의 최근 기사로 보충",
        },
    }


def _rank_latest(articles: list[Article]) -> list[RankedArticle]:
    ranked = [score_latest_article(article) for article in articles]
    ranked.sort(key=lambda row: _latest_sort_key(row), reverse=True)
    return ranked


def _pick_with_source_cap(
    ranked: list[RankedArticle],
    *,
    limit: int,
    per_source_limit: int,
) -> list[RankedArticle]:
    selected: list[RankedArticle] = []
    source_counts: dict[str, int] = {}
    for article in ranked:
        if source_counts.get(article.source_id, 0) >= per_source_limit:
            continue
        selected.append(article)
        source_counts[article.source_id] = source_counts.get(article.source_id, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def _freshness_score(article: Article) -> float:
    if not article.published_at:
        return 0.0
    age_hours = max((datetime.now(timezone.utc) - article.published_at).total_seconds() / 3600, 0)
    return max(0.0, 1.0 - age_hours / (24 * 7))


def _latest_sort_key(article: RankedArticle) -> tuple[float, float, float]:
    timestamp = article.published_at.timestamp() if article.published_at else 0
    return (article.score, timestamp, article.authority_tier)


def _looks_like_article(article: Article, source: SourceConfig | None) -> bool:
    title = article.title.strip().lower()
    url = article.url.rstrip("/")
    if source and source.url and url == source.url.rstrip("/"):
        return False
    if any(part in url.lower() for part in ("/category/", "/tag/", "/author/", "/page/")):
        return False
    if title in {
        "news",
        "blog",
        "anthropic news",
        "artificial intelligence",
        "ai paper summary",
        "technology",
        "research",
    }:
        return False
    if len(article.body or "") < 500 and len(article.summary or "") < 160:
        return False
    return True
