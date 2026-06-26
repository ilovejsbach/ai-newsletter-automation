from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .models import Article, RankedArticle

KEYWORD_WEIGHTS = {
    "agent": 1.5,
    "agents": 1.5,
    "agentic": 1.6,
    "llm": 1.3,
    "model": 1.1,
    "open model": 1.3,
    "benchmark": 1.0,
    "reasoning": 1.2,
    "multimodal": 1.2,
    "enterprise": 1.1,
    "security": 1.2,
    "regulation": 1.1,
    "governance": 1.1,
    "openai": 1.4,
    "anthropic": 1.3,
    "google": 1.2,
    "deepmind": 1.2,
    "hugging face": 1.1,
}

TERM_GLOSSARY = {
    "agent": "에이전트(agent)",
    "agentic": "에이전틱(agentic)",
    "llm": "대규모 언어모델(LLM)",
    "benchmark": "벤치마크(benchmark)",
    "reasoning": "추론(reasoning)",
    "multimodal": "멀티모달(multimodal)",
    "open model": "오픈 모델(open model)",
    "fine-tuning": "파인튜닝(fine-tuning)",
    "inference": "추론 실행(inference)",
}


def rank_articles(articles: list[Article], limit: int = 10, per_source_limit: int = 2) -> list[RankedArticle]:
    deduped = deduplicate(articles)
    ranked = [score_article(article) for article in deduped]
    ranked.sort(key=lambda a: a.score, reverse=True)
    selected: list[RankedArticle] = []
    source_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    for article in ranked:
        if source_counts.get(article.source_id, 0) >= per_source_limit:
            continue
        family = _source_family(article)
        if family == "github" and family_counts.get(family, 0) >= 2:
            continue
        if family == "huggingface-model" and family_counts.get(family, 0) >= 2:
            continue
        selected.append(article)
        source_counts[article.source_id] = source_counts.get(article.source_id, 0) + 1
        family_counts[family] = family_counts.get(family, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def deduplicate(articles: list[Article]) -> list[Article]:
    seen: dict[str, Article] = {}
    for article in articles:
        key = _normalize_title(article.title)
        current = seen.get(key)
        if not current or article.source_weight > current.source_weight:
            seen[key] = article
    return list(seen.values())


def score_article(article: Article) -> RankedArticle:
    text = f"{article.title} {article.summary} {' '.join(article.tags)}".lower()
    keyword_score = sum(weight for keyword, weight in KEYWORD_WEIGHTS.items() if keyword in text)
    recency_score = _recency_score(article)
    popularity_score = _popularity_score(article)
    source_score = max(article.source_weight, 0.1)
    platform_penalty = _platform_penalty(article)
    score = (keyword_score * 1.6 + recency_score * 1.2 + popularity_score * 2.4) * source_score * platform_penalty
    terms = [label for key, label in TERM_GLOSSARY.items() if key in text]
    reason_bits = []
    if keyword_score:
        reason_bits.append("핵심 AI 키워드 밀도가 높음")
    if popularity_score >= 1.5:
        reason_bits.append("객관적 인기 지표가 높음")
    if recency_score >= 1.5:
        reason_bits.append("최근 1주 이내 신호")
    reason = ", ".join(reason_bits) or "기본 중요도 기준 통과"
    return RankedArticle(
        **article.model_dump(),
        score=round(score, 3),
        score_breakdown={
            "keyword": round(keyword_score, 3),
            "recency": round(recency_score, 3),
            "popularity": round(popularity_score, 3),
            "source_weight": round(source_score, 3),
            "platform_penalty": round(platform_penalty, 3),
        },
        reason=reason,
        korean_title=article.title,
        korean_summary=article.summary[:360] if article.summary else "요약 생성 전입니다.",
        why_it_matters="AI 모델, 에이전트, 기업 적용, 정책/보안 관점의 파급 가능성을 기준으로 선별했습니다.",
        terms=terms[:6],
    )


def build_quality_report(selected: list[RankedArticle], candidates: list[Article]) -> dict[str, object]:
    source_counts: dict[str, int] = {}
    for article in candidates:
        source_counts[article.source_name] = source_counts.get(article.source_name, 0) + 1
    avg_score = sum(a.score for a in selected) / len(selected) if selected else 0
    return {
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "average_score": round(avg_score, 2),
        "source_counts": source_counts,
        "rubric": {
            "strategic_impact": "AI 모델/에이전트 생태계, 기업 적용, 규제/보안 파급력",
            "novelty": "최근 7일 내 새 발표, 빠르게 확산되는 구현체 또는 모델",
            "objective_popularity": "GitHub stars/forks, Hugging Face downloads/likes 등",
            "source_authority": "원문/공식 블로그/주요 개발 플랫폼 가중치",
            "internal_relevance": "금융/공공/엔터프라이즈 업무 적용 가능성",
        },
    }


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]+", " ", title.lower()).strip()


def _recency_score(article: Article) -> float:
    if not article.published_at:
        return 0.8
    age_hours = max((datetime.now(timezone.utc) - article.published_at).total_seconds() / 3600, 0)
    return max(0.2, 2.0 - age_hours / 84)


def _popularity_score(article: Article) -> float:
    metrics = article.metrics
    stars = _num(metrics.get("stars"))
    forks = _num(metrics.get("forks"))
    downloads = _num(metrics.get("downloads"))
    likes = _num(metrics.get("likes"))
    raw = stars * 1.0 + forks * 1.5 + downloads * 0.01 + likes * 2.0
    return min(3.0, math.log10(raw + 1))


def _platform_penalty(article: Article) -> float:
    if article.source_id.startswith("github"):
        stars = _num(article.metrics.get("stars"))
        if stars < 5:
            return 0.3
        if stars < 25:
            return 0.55
        if stars < 100:
            return 0.8
    if article.source_id.startswith("huggingface"):
        downloads = _num(article.metrics.get("downloads"))
        likes = _num(article.metrics.get("likes"))
        if downloads < 1000 and likes < 20:
            return 0.65
    return 1.0


def _source_family(article: RankedArticle) -> str:
    if article.source_id.startswith("github"):
        return "github"
    if article.source_id == "huggingface-models":
        return "huggingface-model"
    return article.source_id


def _num(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
