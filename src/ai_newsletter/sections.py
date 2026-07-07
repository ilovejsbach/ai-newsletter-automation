"""Sectioned selection: fill fixed newsletter sections by quota instead of a flat top-N.

The newsletter has a fixed editorial structure the reader can rely on every week:

  * frontier — 프론티어 & 사업 동향: 대형 연구소의 모델 공개, 정책/규제, 파트너십·사업 변동
  * open     — 오픈웨이트 & 오픈소스: 오픈웨이트 모델, GitHub/Hugging Face 생태계
  * research — 연구 & 논문: 논문, 벤치마크, 기법 연구
  * tooling  — 툴링 & 보안: 개발 도구, 인프라, 보안 이슈

Selection reuses the editorial LLM scoring + topic dedup, then fills each section's
quota by importance. A section that has no qualified candidates stays short — the
newsletter says so instead of padding it with filler — and the freed slots are
backfilled globally so the issue still reaches `limit` articles.
"""

from __future__ import annotations

import os
import re

from .models import Article, RankedArticle
from .editorial_selection import (
    _BIG_LABS,
    _heuristic_score,
    _heuristic_topic_key,
    _is_publishable,
    _llm_score,
    _prefer,
    _vendor_of,
)
from .ranking import deduplicate

SECTION_ORDER = ["frontier", "open", "research", "tooling"]

SECTION_META: dict[str, dict[str, str]] = {
    "frontier": {
        "title": "프론티어 & 사업 동향",
        "description": "대형 연구소의 모델 공개·프리뷰, 규제/정책, 파트너십 등 산업이 움직인 사건입니다.",
        "empty": "이번 주는 프론티어 랩의 주요 발표가 없었습니다.",
    },
    "open": {
        "title": "오픈웨이트 & 오픈소스",
        "description": "오픈웨이트 모델과 GitHub/Hugging Face 생태계에서 주목할 프로젝트입니다.",
        "empty": "이번 주는 오픈웨이트·오픈소스 쪽에 선정 기준을 넘는 소식이 없었습니다.",
    },
    "research": {
        "title": "연구 & 논문",
        "description": "논문·벤치마크·기법 연구 중 실무 관점에서 볼 만한 결과입니다.",
        "empty": "이번 주는 소개할 만한 연구·논문이 부족했습니다.",
    },
    "tooling": {
        "title": "툴링 & 보안",
        "description": "개발 도구, 인프라, 보안 이슈 등 바로 써먹거나 조심해야 할 소식입니다.",
        "empty": "이번 주는 툴링·보안 쪽 주요 소식이 없었습니다.",
    },
}

# Share of `limit` each section targets. Largest-remainder rounding, minimum 1
# per section when limit >= len(SECTION_ORDER).
_SECTION_WEIGHTS = {"frontier": 0.3, "open": 0.3, "research": 0.2, "tooling": 0.2}

_OPEN_WEIGHT_PATTERN = re.compile(
    r"open[-\s]?weight|open[-\s]?source|오픈소스|오픈웨이트"
    r"|\bllama\b|\bqwen\b|deepseek|\bgemma\b|\bphi[-\s]?\d|\bglm[-\s]?\d|\bolmo\b",
    re.IGNORECASE,
)
_RESEARCH_PATTERN = re.compile(r"arxiv|\bpaper\b|benchmark|논문|연구", re.IGNORECASE)
_SECURITY_PATTERN = re.compile(
    r"security|vulnerab|exploit|jailbreak|guardrail|취약점|보안", re.IGNORECASE
)


def section_quotas(limit: int) -> dict[str, int]:
    """Distribute `limit` slots across sections by weight (largest remainder)."""
    raw = {sec: limit * w for sec, w in _SECTION_WEIGHTS.items()}
    quotas = {sec: int(v) for sec, v in raw.items()}
    if limit >= len(SECTION_ORDER):
        for sec in quotas:
            quotas[sec] = max(1, quotas[sec])
    remainder = limit - sum(quotas.values())
    if remainder > 0:
        by_frac = sorted(raw, key=lambda s: raw[s] - int(raw[s]), reverse=True)
        for sec in by_frac[:remainder]:
            quotas[sec] += 1
    elif remainder < 0:
        by_size = sorted(quotas, key=quotas.get, reverse=True)
        for sec in by_size[: -remainder]:
            quotas[sec] = max(1, quotas[sec] - 1)
    return quotas


def assign_section(article: RankedArticle) -> str:
    """Heuristic section for articles the LLM did not (or could not) classify."""
    text = f"{article.title} {article.summary}".lower()
    if (
        article.source_id.startswith("github")
        or article.source_id == "huggingface-models"
        or _OPEN_WEIGHT_PATTERN.search(text)
    ):
        return "open"
    if article.category == "research" or _RESEARCH_PATTERN.search(text):
        return "research"
    if article.category in ("security", "tooling") or _SECURITY_PATTERN.search(text):
        return "tooling"
    if article.category in ("model", "industry", "policy"):
        return "frontier"
    return "frontier" if _vendor_of(article) in _BIG_LABS else "tooling"


def select_sectioned_articles(
    candidates: list[Article],
    *,
    limit: int = 10,
    use_llm: bool = True,
    per_source_limit: int = 2,
    social_articles: list[Article] | None = None,
    rubric: str = "standard",
) -> tuple[list[RankedArticle], dict[str, object]]:
    pool = [a for a in deduplicate(candidates) if _is_publishable(a)]
    mode = "sectioned-heuristic"
    scored: list[RankedArticle] = []
    if use_llm and os.getenv("OPENAI_API_KEY"):
        scored = _llm_score(pool, rubric=rubric)
        mode = f"sectioned-llm-{rubric}"
    if not scored:
        scored = _heuristic_score(pool)
        mode = "sectioned-heuristic"

    for article in scored:
        if article.section not in SECTION_ORDER:
            article.section = assign_section(article)

    # Collapse near-duplicates: best article per topic_key. Instead of discarding
    # the duplicates outright, keep who else covered the story on the winner —
    # that cross-coverage is the "how are others reacting" signal for the body.
    groups: dict[str, list[RankedArticle]] = {}
    for article in scored:
        groups.setdefault(article.topic_key or article.id, []).append(article)
    reps: list[RankedArticle] = []
    for group in groups.values():
        rep = group[0]
        for article in group[1:]:
            if _prefer(article, rep):
                rep = article
        rep.related_coverage = [
            f"{a.source_name} — {a.title}" for a in group if a.id != rep.id
        ][:5]
        reps.append(rep)
    ranked = sorted(reps, key=lambda a: a.score, reverse=True)

    # Social boost: token-free. Social posts (YouTube/HN/Reddit/blogs) never get
    # published — they only add weight to main articles covering the same topic
    # and feed the "업계의 움직임" slot via related_coverage.
    social_matched = 0
    social_hits: dict[str, list[str]] = {}
    for post in social_articles or []:
        key = _heuristic_topic_key(post)
        if key == post.id:
            continue  # no recognizable entity in the post title/summary
        social_hits.setdefault(key, []).append(f"{post.source_name} — {post.title}")
    if social_hits:
        for rep in ranked:
            hits = social_hits.get(rep.topic_key) or social_hits.get(_heuristic_topic_key(rep))
            if not hits:
                continue
            distinct_sources = len({hit.split(" — ")[0] for hit in hits})
            boost = 2.0 * min(3, distinct_sources)
            rep.score = round(rep.score + boost, 3)
            rep.score_breakdown = {**rep.score_breakdown, "social_boost": boost}
            rep.related_coverage = (rep.related_coverage + hits)[:8]
            social_matched += 1
        ranked.sort(key=lambda a: a.score, reverse=True)

    quotas = section_quotas(limit)
    selected, shortfalls = _fill_quotas(
        ranked, limit=limit, quotas=quotas, per_source_limit=per_source_limit
    )

    # Present articles in fixed section order (importance within a section).
    order = {sec: idx for idx, sec in enumerate(SECTION_ORDER)}
    selected.sort(key=lambda a: (order.get(a.section, len(order)), -a.score))

    section_counts = {sec: 0 for sec in SECTION_ORDER}
    for article in selected:
        section_counts[article.section] = section_counts.get(article.section, 0) + 1
    report: dict[str, object] = {
        "mode": mode,
        "candidate_count": len(pool),
        "scored_count": len(scored),
        "topics_after_dedup": len(ranked),
        "selected_count": len(selected),
        "section_quotas": quotas,
        "section_counts": section_counts,
        "section_shortfalls": {
            sec: SECTION_META[sec]["empty"] for sec in shortfalls
        },
        "social_signal": {
            "posts": len(social_articles or []),
            "topics_with_signal": len(social_hits),
            "boosted_articles": social_matched,
        },
        # 낙선 이유 추적용: 상위 25위까지 전체 순위 (selected=False가 탈락자)
        "ranking": [
            {
                "rank": rank,
                "selected": article.id in {a.id for a in selected},
                "importance": article.score,
                "section": article.section,
                "source": article.source_name,
                "topic_key": article.topic_key,
                "title": article.title[:80],
            }
            for rank, article in enumerate(ranked[:25], 1)
        ],
        "selected": [
            {
                "title": a.title,
                "source": a.source_name,
                "section": a.section,
                "importance": a.score,
                "topic_key": a.topic_key,
                "reason": a.reason,
            }
            for a in selected
        ],
        "selection_contract": {
            "rule": "고정 섹션 최소 보장(quota-1, 최소 1) + 남는 슬롯은 섹션 무관 전체 중요도순",
            "quota": ", ".join(
                f"{SECTION_META[s]['title']} 최소 {max(1, quotas[s] - 1)}" for s in SECTION_ORDER
            ),
            "dedup": "같은 사건(topic_key)은 1건만, 함께 다룬 매체는 related_coverage로 보존",
            "shortfall": "후보가 부족한 섹션은 채우지 않고 명시, 남는 슬롯은 전체 중요도순 보충",
            "diversity": f"출처당 최대 {per_source_limit}건",
        },
    }
    return selected, report


def _fill_quotas(
    ranked: list[RankedArticle],
    *,
    limit: int,
    quotas: dict[str, int],
    per_source_limit: int,
) -> tuple[list[RankedArticle], list[str]]:
    selected: list[RankedArticle] = []
    selected_ids: set[str] = set()
    source_counts: dict[str, int] = {}

    def _admit(article: RankedArticle) -> None:
        selected.append(article)
        selected_ids.add(article.id)
        source_counts[article.source_id] = source_counts.get(article.source_id, 0) + 1

    # Phase 1: guarantee each section a MINIMUM (quota-1, at least 1) so the
    # structure survives, but don't let quotas consume the whole issue — that
    # was dropping a top-5 frontier story in favor of a 40-point filler.
    minimums = {sec: max(1, quota - 1) for sec, quota in quotas.items()}
    shortfalls: list[str] = []
    for sec in SECTION_ORDER:
        count = 0
        for article in ranked:
            if count >= minimums.get(sec, 0):
                break
            if article.section != sec or article.id in selected_ids:
                continue
            if source_counts.get(article.source_id, 0) >= per_source_limit:
                continue
            _admit(article)
            count += 1
        if count < minimums.get(sec, 0):
            shortfalls.append(sec)

    # Phase 2: remaining slots go to the best stories globally, section-blind
    # (source cap still applies). A strong week for one section can now earn
    # it an extra slot instead of forcing weak picks elsewhere.
    for article in ranked:
        if len(selected) >= limit:
            break
        if article.id in selected_ids:
            continue
        if source_counts.get(article.source_id, 0) >= per_source_limit:
            continue
        _admit(article)

    return selected[:limit], shortfalls
