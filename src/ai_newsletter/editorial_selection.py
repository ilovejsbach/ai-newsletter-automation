"""Editorial selection: rank candidates by LLM-judged newsworthiness and collapse
near-duplicate stories by topic, instead of by keyword density.

This addresses three failure modes of the keyword-weighted heuristic:
  * major launches written in plain language (e.g. "Previewing GPT-5.6") got buried
    under keyword-stuffed GitHub repos and comparison posts;
  * genuinely important industry news (e.g. a flagship model being export-banned and
    then redeployed) scored low because it lacks technical buzzwords;
  * the same event covered by two outlets survived because dedup keyed on exact title.
"""

from __future__ import annotations

import json
import os
import re

from openai import OpenAI

from .models import Article, RankedArticle
from .ranking import deduplicate, score_article
from .usage import usage

# Heuristic entity patterns used for topic keys when the LLM is unavailable.
_ENTITY_PATTERNS = [
    r"gpt[-\s]?\d[\d.]*",
    r"claude\s+(?:opus|sonnet|haiku)\s*\d[\d.]*",
    r"fable\s*\d+",
    r"mithos",
    r"gemini\s*\d[\d.]*",
    r"nano\s+banana\s*\d*",
    r"llama\s*\d[\d.]*",
    r"qwen\s*\d[\d.]*",
    r"deepseek[-\s]?\w*",
    r"mistral\s*\w*",
    r"glm[-\s]?\d[\d.]*",
    r"gemma\s*\d[\d.]*",
    r"phi[-\s]?\d[\d.]*",
]

# Vendor families used for a diversity cap so one company cannot dominate the issue.
_VENDOR_PATTERNS = {
    "openai": r"\bopenai\b|\bgpt[-\s]?\d|chatgpt",
    "anthropic": r"\banthropic\b|\bclaude\b|\bfable\b",
    "google": r"\bgoogle\b|deepmind|gemini|gemma|nano\s+banana",
    "meta": r"\bmeta\b|\bllama\b",
    "nvidia": r"\bnvidia\b|nemotron",
    "mistral": r"\bmistral\b",
}

# Big frontier labs — grouped so "editorial-diverse" can guarantee breadth beyond
# foundation-model announcements (open source, tooling, enterprise, smaller players).
_BIG_LABS = {"openai", "anthropic", "google", "meta", "nvidia", "mistral"}


# 채점 루브릭 (옵션 비교용 — --rubric 플래그로 선택)
_RUBRIC_STANDARD = (
    "높게 평가: 주요 연구소(OpenAI, Anthropic, Google, Meta, Mistral, DeepSeek 등)의 "
    "플래그십 모델 공개·프리뷰; 규제/수출통제/정책/대형 파트너십·사업 변동 같은 산업 이동 사건; "
    "보안 취약점·사고; 서로 다른 독립 출처가 함께 다룬(교차검증된) 사건.\n"
)
_RUBRIC_SOTA = (
    "채점 위계:\n"
    "최상위(90-100): 프론티어 3사(OpenAI, Anthropic, Google)의 SOTA/플래그십 모델 공개·프리뷰. "
    "그리고 이미 출시된 SOTA 모델에 대한 배포 중단·킬스위치·수출통제·재배포·리콜 같은 "
    "라이프사이클 개입 사건 — 출시보다 드물고 파급이 크므로, '후속 보도'라는 이유로 "
    "감점하지 말고 오히려 출시급 이상으로 평가해.\n"
    "높게(70-89): 그 외 주요 연구소(Meta, Mistral, DeepSeek 등)의 모델 공개; "
    "규제/정책/대형 파트너십·사업 변동 같은 산업 이동 사건; 보안 취약점·사고; "
    "서로 다른 독립 출처가 함께 다룬(교차검증된) 사건.\n"
)
_RUBRICS = {"standard": _RUBRIC_STANDARD, "sota": _RUBRIC_SOTA}


_GENERIC_TITLES = {
    "news",
    "blog",
    "anthropic news",
    "openai news",
    "artificial intelligence",
    "ai paper summary",
    "ai infrastructure",
    "technology",
    "research",
    "announcements",
    "product",
}


def _is_publishable(article: Article) -> bool:
    """Drop low-content index/category/landing pages so they cannot represent a topic."""
    title = article.title.strip().lower()
    url = article.url.rstrip("/").lower()
    if title in _GENERIC_TITLES:
        return False
    if any(part in url for part in ("/category/", "/tag/", "/author/", "/page/")):
        return False
    # An article needs at least some substance in body or summary.
    if len(article.body or "") < 400 and len(article.summary or "") < 120:
        return False
    return True


def select_editorial_articles(
    candidates: list[Article],
    *,
    limit: int = 10,
    use_llm: bool = True,
    per_source_limit: int = 2,
    per_vendor_limit: int = 3,
    diversify: bool = False,
) -> tuple[list[RankedArticle], dict[str, object]]:
    # In diversify mode, tighten vendor concentration and cap any single category
    # (typically "model") so the issue spans tooling / enterprise / security / OSS
    # instead of being an all-foundation-model list.
    category_caps: dict[str, int] | None = None
    big_lab_cap: int | None = None
    protect_top = 0
    if diversify:
        per_vendor_limit = min(per_vendor_limit, 2)
        category_caps = {"model": 3}
        big_lab_cap = max(1, (limit * 6) // 10)  # ~60% of the issue, rest = breadth
        protect_top = max(1, (limit * 4) // 10)  # top ~40% by importance bypass caps
    pool = [a for a in deduplicate(candidates) if _is_publishable(a)]
    scored: list[RankedArticle]
    mode: str
    if use_llm and os.getenv("OPENAI_API_KEY"):
        scored = _llm_score(pool)
        mode = "editorial-llm"
    else:
        scored = []
        mode = "editorial-heuristic"
    if not scored:
        scored = _heuristic_score(pool)
        mode = "editorial-heuristic"

    # Collapse near-duplicate stories: keep the best article per topic_key.
    by_topic: dict[str, RankedArticle] = {}
    for article in scored:
        key = article.topic_key or article.id
        current = by_topic.get(key)
        if current is None or _prefer(article, current):
            by_topic[key] = article
    deduped = sorted(by_topic.values(), key=lambda a: a.score, reverse=True)

    selected = _pick_with_diversity(
        deduped,
        limit=limit,
        per_source_limit=per_source_limit,
        per_vendor_limit=per_vendor_limit,
        category_caps=category_caps,
        big_lab_cap=big_lab_cap,
        protect_top=protect_top,
    )
    if diversify:
        mode = mode + "-diverse"
    report = _editorial_report(selected, pool, scored, deduped, mode=mode)
    return selected, report


def select_consensus_articles(
    candidates: list[Article],
    *,
    limit: int = 10,
    use_llm: bool = True,
    per_source_limit: int = 2,
    source_sets: dict[str, str] | None = None,
) -> tuple[list[RankedArticle], dict[str, object]]:
    """Rank by CORROBORATION: stories that the most distinct sources covered win.

    The premise is that when many independent sites report the same event in the
    same week, that overlap is itself the importance signal. Articles are grouped
    by topic_key, each topic is scored by its distinct-source count, and the
    highest-corroborated topics rise to the top (ties broken by importance, then
    recency). One representative article per topic is emitted.
    """
    pool = [a for a in deduplicate(candidates) if _is_publishable(a)]
    scored: list[RankedArticle] = []
    mode = "consensus-heuristic"
    if use_llm and os.getenv("OPENAI_API_KEY"):
        scored = _llm_score(pool)
        mode = "consensus-llm"
    if not scored:
        scored = _heuristic_score(pool)
        mode = "consensus-heuristic"

    source_sets = source_sets or {}

    groups: dict[str, list[RankedArticle]] = {}
    for article in scored:
        groups.setdefault(article.topic_key or article.id, []).append(article)

    topics: list[tuple[float, RankedArticle]] = []
    candidate_hits: dict[str, int] = {}  # candidate source -> # cross-set topics it corroborated
    for arts in groups.values():
        sources = {a.source_id for a in arts}
        corroboration = len(sources)
        sets_covered = {source_sets.get(sid, "main") for sid in sources}
        cross_set = len(sets_covered) >= 2
        # Cross-set overlap (a candidate corroborating a main source, or vice versa)
        # is a stronger signal than repetition within one set.
        consensus_score = float(corroboration) + (2.0 if cross_set else 0.0)
        rep = max(arts, key=lambda a: (a.authority_tier, a.score))
        rep.score_breakdown = {
            **rep.score_breakdown,
            "corroboration": float(corroboration),
            "cross_set": 1.0 if cross_set else 0.0,
            "consensus_score": round(consensus_score, 2),
        }
        if cross_set:
            rep.reason = f"{corroboration}개 출처(main+candidate 교차)가 다룬 화제 — 강한 중복 신호"
            for a in arts:
                if source_sets.get(a.source_id) == "candidate":
                    candidate_hits[a.source_name] = candidate_hits.get(a.source_name, 0) + 1
        elif corroboration > 1:
            rep.reason = f"{corroboration}개 출처가 함께 다룬 화제 (중복도 상위)"
        else:
            rep.reason = "단일 출처 (중복 없음)"
        topics.append((consensus_score, rep))

    # Primary key: consensus score (corroboration + cross-set bonus). Ties: importance, recency.
    topics.sort(key=lambda t: (t[0], t[1].score, _recency_ts(t[1])), reverse=True)

    selected: list[RankedArticle] = []
    source_counts: dict[str, int] = {}
    for _, rep in topics:
        if source_counts.get(rep.source_id, 0) >= per_source_limit:
            continue
        selected.append(rep)
        source_counts[rep.source_id] = source_counts.get(rep.source_id, 0) + 1
        if len(selected) >= limit:
            break

    multi = sum(1 for _, r in topics if r.score_breakdown.get("corroboration", 1) > 1)
    cross = sum(1 for _, r in topics if r.score_breakdown.get("cross_set", 0) == 1.0)
    report = {
        "mode": mode,
        "candidate_count": len(pool),
        "distinct_topics": len(topics),
        "multi_source_topics": multi,
        "cross_set_topics": cross,
        "candidate_contribution": dict(sorted(candidate_hits.items(), key=lambda kv: kv[1], reverse=True)),
        "selected_count": len(selected),
        "selected": [
            {
                "title": r.title,
                "source": r.source_name,
                "corroboration": int(r.score_breakdown.get("corroboration", 1)),
                "cross_set": bool(r.score_breakdown.get("cross_set", 0)),
                "importance": r.score,
                "topic_key": r.topic_key,
                "reason": r.reason,
            }
            for r in selected
        ],
        "selection_contract": {
            "rule": "여러 출처가 함께 다룬 화제(중복도)를 1순위로 랭킹, main+candidate 교차 시 가산점",
            "ranking": "consensus_score(corroboration + cross-set bonus) → importance → recency",
            "dedup": "topic_key로 묶어 대표 1건",
            "diversity": "출처당 최대 2건",
        },
    }
    return selected, report


def _recency_ts(article: RankedArticle) -> float:
    return article.published_at.timestamp() if article.published_at else 0.0


def _completeness_check(
    selected: list[RankedArticle],
    ranked_pool: list[RankedArticle],
    *,
    limit: int,
    use_llm: bool = True,
    max_promote: int = 2,
) -> tuple[list[RankedArticle], list[str]]:
    """Safety net: ask the LLM whether any MAJOR industry story that was a
    candidate got dropped by scoring/quotas, and swap it in for the weakest pick.

    This is the systemic guard against 'missing the week's biggest story' — it
    catches the case where the story WAS collected (e.g. Kimi K3) but selection
    logic left it out. It cannot recover a story that was never a candidate.
    """
    if not use_llm or not os.getenv("OPENAI_API_KEY"):
        return selected, []
    selected_ids = {a.id for a in selected}
    unselected = sorted(
        (a for a in ranked_pool if a.id not in selected_ids),
        key=lambda a: a.score,
        reverse=True,
    )[:40]
    if not unselected:
        return selected, []
    sel_payload = [{"title": a.title, "source": a.source_name} for a in selected]
    un_payload = [
        {
            "index": i,
            "title": a.title,
            "source": a.source_name,
            "hn_points": int(a.metrics.get("hn_points") or 0),
        }
        for i, a in enumerate(unselected)
    ]
    prompt = (
        "너는 주간 AI 뉴스레터 편집장이야. 아래 '선정된 기사'와 '탈락 후보'를 비교해, "
        "탈락 후보 중 이번 주 업계의 MAJOR 사건인데 빠지면 안 되는 것을 고른다. "
        "MAJOR = 플래그십 모델 출시/프리뷰, 대형 자금·인수합병, 규제·정책 변화, "
        "커뮤니티를 뒤덮은 바이럴 기술 돌파구. 선정 기사들보다 명백히 더 큰 뉴스일 때만 고르고 "
        "애매하면 고르지 마. 지어내지 말고 반드시 주어진 index만 사용.\n"
        f"최대 {max_promote}개. 반드시 JSON만 반환: "
        '{"missing":[{"index":정수, "reason":"한 문장 한국어"}]} (없으면 빈 배열).\n\n'
        f"선정된 기사: {json.dumps(sel_payload, ensure_ascii=False)}\n\n"
        f"탈락 후보: {json.dumps(un_payload, ensure_ascii=False)}"
    )
    try:
        client = OpenAI()
        model = os.getenv("CRITIC_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.4-mini"))
        response = client.responses.create(
            model=model, input=prompt, text={"format": {"type": "json_object"}}
        )
        usage.record(response)
        data = json.loads(response.output_text)
    except Exception:
        return selected, []
    rows = data.get("missing") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        return selected, []
    result = list(selected)
    notes: list[str] = []
    for row in rows[:max_promote]:
        idx = row.get("index") if isinstance(row, dict) else None
        if not isinstance(idx, int) or idx < 0 or idx >= len(unselected):
            continue
        promote = unselected[idx]
        if promote.id in {a.id for a in result}:
            continue
        weakest = min(result, key=lambda a: a.score)
        result = [a for a in result if a.id != weakest.id]
        promote.reason = f"완전성 크리틱 편입: {row.get('reason') or '이번 주 major 사건'}"
        result.append(promote)
        notes.append(
            f"편입 '{promote.title[:50]}' (사유: {row.get('reason', '')}) / 대체 '{weakest.title[:40]}'"
        )
    return result, notes


def _prefer(candidate: RankedArticle, current: RankedArticle) -> bool:
    """Prefer the higher-scored article for a topic; break ties toward official sources."""
    if candidate.score != current.score:
        return candidate.score > current.score
    return candidate.authority_tier > current.authority_tier


def _pick_with_diversity(
    ranked: list[RankedArticle],
    *,
    limit: int,
    per_source_limit: int,
    per_vendor_limit: int,
    category_caps: dict[str, int] | None = None,
    big_lab_cap: int | None = None,
    protect_top: int = 0,
) -> list[RankedArticle]:
    category_caps = category_caps or {}
    selected: list[RankedArticle] = []
    source_counts: dict[str, int] = {}
    vendor_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    big_lab_count = 0
    protected_ids: set[str] = set()

    def _admit(article: RankedArticle) -> None:
        nonlocal big_lab_count
        selected.append(article)
        source_counts[article.source_id] = source_counts.get(article.source_id, 0) + 1
        category = article.category or "other"
        category_counts[category] = category_counts.get(category, 0) + 1
        vendor = _vendor_of(article)
        if vendor:
            vendor_counts[vendor] = vendor_counts.get(vendor, 0) + 1
        if vendor in _BIG_LABS:
            big_lab_count += 1

    # First: the most important stories bypass vendor/category/big-lab caps so a
    # genuinely major item (e.g. a flagship redeploy) is never dropped for diversity.
    # `ranked` is already sorted by importance desc. per_source_limit still applies.
    for article in ranked[:protect_top]:
        if source_counts.get(article.source_id, 0) >= per_source_limit:
            continue
        protected_ids.add(article.id)
        _admit(article)
        if len(selected) >= limit:
            return selected

    deferred: list[RankedArticle] = []
    for article in ranked:
        if article.id in protected_ids:
            continue
        vendor = _vendor_of(article)
        category = article.category or "other"
        if source_counts.get(article.source_id, 0) >= per_source_limit:
            continue
        if vendor and vendor_counts.get(vendor, 0) >= per_vendor_limit:
            deferred.append(article)
            continue
        if category in category_caps and category_counts.get(category, 0) >= category_caps[category]:
            deferred.append(article)
            continue
        if big_lab_cap is not None and vendor in _BIG_LABS and big_lab_count >= big_lab_cap:
            deferred.append(article)
            continue
        _admit(article)
        if len(selected) >= limit:
            return selected
    # If diversity caps left us short, backfill from deferred/high-score remainder
    # (source cap still respected; vendor/category caps relaxed to reach `limit`).
    for article in deferred + ranked:
        if len(selected) >= limit:
            break
        if article in selected:
            continue
        if source_counts.get(article.source_id, 0) >= per_source_limit:
            continue
        selected.append(article)
        source_counts[article.source_id] = source_counts.get(article.source_id, 0) + 1
    return selected[:limit]


def _llm_score(pool: list[Article], rubric: str = "standard") -> list[RankedArticle]:
    client = OpenAI()
    model = os.getenv("CRITIC_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.4-mini"))
    payload = [
        {
            "index": idx,
            "title": a.title,
            "source": a.source_name,
            "panel": a.panel,
            "authority_tier": a.authority_tier,
            "published_at": a.published_at.isoformat() if a.published_at else None,
            "summary": (a.summary or a.body or "")[:400],
        }
        for idx, a in enumerate(pool)
    ]
    prompt = (
        "너는 금융/공공/엔터프라이즈 독자를 위한 주간 AI 뉴스레터의 편집장이야. "
        "아래 후보 기사들을 '편집자적 뉴스가치(newsworthiness)' 기준으로 0~100점으로 채점해. "
        "키워드가 몇 개 들어갔는지가 아니라, 업계에 실제로 얼마나 중요한 사건인지로 판단해.\n"
        f"{_RUBRICS.get(rubric, _RUBRIC_STANDARD)}"
        "낮게 평가: 키워드만 많은 GitHub 레포, 일반 튜토리얼, 사소한 점진적 업데이트, 홍보성 글, "
        "정보량 없는 목록/카테고리 페이지.\n"
        "각 기사에 topic_key를 부여해. topic_key는 '그 기사가 다루는 실제 사건/제품'을 나타내는 짧은 "
        "영문 슬러그이고, 같은 사건을 다룬 서로 다른 기사는 반드시 같은 topic_key를 가져야 해 "
        "(예: 'claude-sonnet-5', 'gpt-5.6-sol', 'fable-5-redeploy'). 코드명과 정식명은 같은 키로 묶어.\n"
        "각 기사에 뉴스레터 섹션도 배정해. section은 다음 중 하나: "
        "frontier(대형 연구소의 모델 공개·정책·규제·파트너십·사업 변동), "
        "open(오픈웨이트 모델·GitHub/Hugging Face 오픈소스 생태계), "
        "research(논문·벤치마크·기법 연구), tooling(개발 도구·인프라·보안).\n"
        "반드시 {\"items\":[{\"index\":정수, \"importance\":0-100 정수, \"topic_key\":\"슬러그\", "
        "\"category\":\"model|research|policy|security|industry|tooling|other\", "
        "\"section\":\"frontier|open|research|tooling\", "
        "\"reason\":\"한 문장 한국어\"}]} 형태의 JSON만 반환해. 모든 index를 빠짐없이 포함해.\n\n"
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
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    by_index: dict[int, dict] = {}
    for row in items:
        if isinstance(row, dict) and isinstance(row.get("index"), int):
            by_index[row["index"]] = row
    scored: list[RankedArticle] = []
    for idx, article in enumerate(pool):
        row = by_index.get(idx)
        if not row:
            continue
        importance = _clamp_int(row.get("importance"))
        ranked = score_article(article)
        ranked.score = float(importance)
        ranked.score_breakdown = {"llm_importance": float(importance)}
        ranked.topic_key = str(row.get("topic_key") or _heuristic_topic_key(article))
        ranked.category = str(row.get("category") or "other")
        ranked.section = str(row.get("section") or "")
        ranked.reason = str(row.get("reason") or "편집자 뉴스가치 기준으로 선별")
        scored.append(ranked)
    return scored


def _heuristic_score(pool: list[Article]) -> list[RankedArticle]:
    """Fallback when no LLM: rebalanced heuristic that de-emphasises keyword density,
    rewards authority + corroboration, and penalises platform (GitHub/HF) noise."""
    topic_sources: dict[str, set[str]] = {}
    for article in pool:
        topic_sources.setdefault(_heuristic_topic_key(article), set()).add(article.source_id)
    scored: list[RankedArticle] = []
    for article in pool:
        base = score_article(article)
        key = _heuristic_topic_key(article)
        corroboration = min(2, len(topic_sources.get(key, set())) - 1)
        authority = article.authority_tier * 3.0 + article.source_weight
        platform_penalty = base.score_breakdown.get("platform_penalty", 1.0)
        recency = base.score_breakdown.get("recency", 0.8)
        importance = (authority * 3.0 + corroboration * 3.0 + recency * 1.5) * platform_penalty
        base.score = round(importance, 3)
        base.score_breakdown = {
            "authority": round(authority, 3),
            "corroboration": float(corroboration),
            "recency": round(recency, 3),
            "platform_penalty": round(platform_penalty, 3),
        }
        base.topic_key = key
        base.category = "other"
        base.reason = "휴리스틱(권위+교차검증+최신성) 기준으로 선별"
        scored.append(base)
    return scored


def _heuristic_topic_key(article: Article) -> str:
    text = f"{article.title} {article.summary}".lower()
    for pattern in _ENTITY_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return re.sub(r"\s+", "-", match.group(0).strip())
    return article.id


def _vendor_of(article: RankedArticle) -> str | None:
    text = f"{article.title} {article.summary} {article.source_id}".lower()
    for vendor, pattern in _VENDOR_PATTERNS.items():
        if re.search(pattern, text):
            return vendor
    return None


def _clamp_int(value: object) -> int:
    try:
        return max(0, min(100, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _editorial_report(
    selected: list[RankedArticle],
    pool: list[Article],
    scored: list[RankedArticle],
    deduped: list[RankedArticle],
    *,
    mode: str,
) -> dict[str, object]:
    collapsed = len(scored) - len(deduped)
    return {
        "mode": mode,
        "candidate_count": len(pool),
        "scored_count": len(scored),
        "topics_after_dedup": len(deduped),
        "duplicates_collapsed": max(0, collapsed),
        "selected_count": len(selected),
        "selected": [
            {
                "title": a.title,
                "source": a.source_name,
                "importance": a.score,
                "topic_key": a.topic_key,
                "category": a.category,
                "reason": a.reason,
            }
            for a in selected
        ],
        "selection_contract": {
            "rule": "LLM 편집자 뉴스가치 채점 + topic_key 중복 제거 + 출처/벤더 다양성 캡",
            "ranking": "importance(뉴스가치) 우선, 키워드 밀도 비의존",
            "dedup": "같은 사건(topic_key)은 1건만",
            "diversity": "출처당 최대 2건, 벤더당 최대 3건",
        },
    }

