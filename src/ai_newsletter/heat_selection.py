"""Heat selection: never miss the week's biggest story, for a structural reason.

The `sectioned` mode this replaces lost major stories every week, and the audit of
14 past builds showed why: 203 articles outranked something that shipped, and 73%
of them were killed by a cap or quota rather than by running out of room. The
mechanisms fought each other in a fixed cycle — an HN boost lifted big stories to
the top, the LLM split each one across two or three `topic_key`s, a per-"source"
cap keyed on the *feed* then rejected all but two fragments, the emptied section
quotas got backfilled with score-11 GitHub repos, and finally a paid LLM critic
bought two of the discarded stories back while breaking the section balance.

This module rebuilds the order of operations so those forces cannot recreate that
cycle:

  1. merge first  — duplicates and split topic keys are unified *before* anything
                    scores, boosts, or caps them, so one story is one candidate;
  2. canonical rep — the article representing a story is the primary account of
                    it, not whichever fragment scored highest (that is how a
                    training-sandbox writeup came to represent Kimi K3's model
                    release);
  3. two axes     — editorial importance ranks, heat guarantees. The top stories
                    by heat are admitted before any cap or quota can see them;
  4. soft caps    — caps break ties, they do not delete. An article that outranks
                    a selected one by a wide margin overrides the cap instead of
                    being silently dropped;
  5. floor        — a quota is never filled below a quality floor. A short section
                    is stated in the issue, not padded with filler.

Every drop is attributed in the report, so "why is X missing" is answerable from
the build output instead of requiring an investigation.
"""

from __future__ import annotations

import os
import re
from difflib import SequenceMatcher

from .editorial_selection import _heuristic_score, _is_publishable, _llm_score
from .heat import _hn_points, _log_norm, cluster_heat, social_matches, topic_tokens
from .identity import owner_key_of, publisher_of
from .models import Article, RankedArticle
from .ranking import _normalize_title
from .sections import SECTION_META, SECTION_ORDER, assign_section, section_quotas

# Importance given to an article the scoring LLM silently omitted from its
# response. Previously such articles ceased to exist with no log and no warning —
# a larger miss surface than every cap combined, and invisible to the safety net.
_LLM_OMISSION_DEFAULT = 45.0

# A thin article normally fails the publishable check, but a story this loud is
# thin only because its landing page resisted scraping (paywall, JS, Cloudflare).
_THIN_BUT_LOUD_POINTS = 200

# The quality floor is per section because the importance score is not comparable
# across the things sections hold. The rubric asks how newsworthy an item is, and a
# paper cannot be newsworthy the way a flagship launch is: measured over a full
# build, arXiv candidates scored a median of 32 against 60 for everything else, so
# a single 40-point floor rejected 8 of 9 research candidates on rubric mismatch
# rather than on quality. The research floor is set from that offset — low enough
# to admit real papers, still well above the GitHub filler (11-18) the floor exists
# to keep out.
_SECTION_FLOOR_OFFSET = {"research": -10.0}


def select_heat_articles(
    candidates: list[Article],
    *,
    limit: int = 10,
    use_llm: bool = True,
    publisher_limit: int = 2,
    owner_limit: int = 1,
    social_articles: list[Article] | None = None,
    rubric: str = "standard",
    score_floor: float = 40.0,
    guarantee_top: int = 3,
    guarantee_min_heat: float = 45.0,
    cap_override_gap: float = 15.0,
    heat_bonus_max: float = 20.0,
) -> tuple[list[RankedArticle], dict[str, object]]:
    pool, merged_dupes = _merge_duplicates(candidates)
    pool = [a for a in pool if _is_publishable(a) or _is_loud(a)]

    mode = "heat-heuristic"
    scored: list[RankedArticle] = []
    omitted = 0
    if use_llm and os.getenv("OPENAI_API_KEY"):
        scored = _llm_score(pool, rubric=rubric)
        mode = f"heat-llm-{rubric}"
        scored, omitted = _repair_omissions(pool, scored)
    if not scored:
        scored = _heuristic_score(pool)
        mode = "heat-heuristic"
        omitted = 0

    clusters = _cluster(scored)
    ranked = _build_representatives(clusters, social_articles or [], heat_bonus_max)
    ranked.sort(key=lambda a: (-a.score, a.id))

    selected, fill_report = _fill(
        ranked,
        limit=limit,
        quotas=section_quotas(limit),
        publisher_limit=publisher_limit,
        owner_limit=owner_limit,
        score_floor=score_floor,
        guarantee_top=guarantee_top,
        guarantee_min_heat=guarantee_min_heat,
        cap_override_gap=cap_override_gap,
    )

    order = {sec: idx for idx, sec in enumerate(SECTION_ORDER)}
    selected.sort(key=lambda a: (order.get(a.section, len(order)), -a.score))

    report = _build_report(
        mode=mode,
        candidates=candidates,
        pool=pool,
        scored=scored,
        clusters=clusters,
        ranked=ranked,
        selected=selected,
        fill_report=fill_report,
        limit=limit,
        quotas=section_quotas(limit),
        score_floor=score_floor,
        publisher_limit=publisher_limit,
        merged_dupes=merged_dupes,
        llm_omissions=omitted,
    )
    return selected, report


# --------------------------------------------------------------------------- #
# 1. merge first
# --------------------------------------------------------------------------- #


def _is_loud(article: Article) -> bool:
    try:
        return int(article.metrics.get("hn_points") or 0) >= _THIN_BUT_LOUD_POINTS
    except (TypeError, ValueError):
        return False


def _merge_duplicates(candidates: list[Article]) -> tuple[list[Article], int]:
    """Collapse same-URL/same-title copies, keeping the union of what they knew.

    The old dedup kept whichever copy had the higher `source_weight` and threw the
    rest away — including their `metrics`. Since official feeds outweigh the HN
    channel, the HN vote count was destroyed for exactly the stories that had the
    most votes, and the boost that was supposed to surface them never fired.
    """
    best: dict[str, Article] = {}
    order: list[str] = []
    merged = 0
    for article in candidates:
        key = _normalize_title(article.title) or article.url.lower() or article.id
        existing = best.get(key)
        if existing is None:
            best[key] = article.model_copy(deep=True)
            order.append(key)
            continue
        merged += 1
        best[key] = _merge_pair(existing, article)
    return [best[key] for key in order], merged


def _merge_pair(keep: Article, other: Article) -> Article:
    """Union of two copies of one story; the more authoritative one stays primary."""
    primary, secondary = (keep, other)
    if other.source_weight > keep.source_weight:
        primary, secondary = (other, keep)
    merged = primary.model_copy(deep=True)
    for field, value in secondary.metrics.items():
        current = merged.metrics.get(field)
        if isinstance(value, (int, float)) and isinstance(current, (int, float)):
            merged.metrics[field] = max(current, value)
        elif current in (None, "", 0):
            merged.metrics[field] = value
    if len(secondary.body or "") > len(merged.body or ""):
        merged.body = secondary.body
    if len(secondary.summary or "") > len(merged.summary or ""):
        merged.summary = secondary.summary
    for url in secondary.image_urls:
        if url not in merged.image_urls:
            merged.image_urls.append(url)
    for url in secondary.info_image_urls:
        if url not in merged.info_image_urls:
            merged.info_image_urls.append(url)
    if secondary.published_at and (
        merged.published_at is None or secondary.published_at < merged.published_at
    ):
        merged.published_at = secondary.published_at
    return merged


def _repair_omissions(
    pool: list[Article], scored: list[RankedArticle]
) -> tuple[list[RankedArticle], int]:
    """Give articles the scoring LLM skipped a conservative score instead of deleting them."""
    seen = {a.id for a in scored}
    missing = [a for a in pool if a.id not in seen]
    if not missing:
        return scored, 0
    for article in _heuristic_score(missing):
        article.score = _LLM_OMISSION_DEFAULT
        article.score_breakdown = {"llm_omitted_default": _LLM_OMISSION_DEFAULT}
        article.section = assign_section(article)
        article.reason = "LLM 채점 응답에서 누락되어 보수적 기본값으로 편입"
        scored.append(article)
    return scored, len(missing)


# --------------------------------------------------------------------------- #
# 2. cluster + canonical representative
# --------------------------------------------------------------------------- #


def _cluster(scored: list[RankedArticle]) -> list[list[RankedArticle]]:
    """Group articles by story, repairing the LLM's habit of splitting one event.

    The scoring model gave the same Kimi K3 release three different topic keys
    (`kimi-k3`, `kimi-k3-fable-routing`, `agentenv-kimi-k3`) and the OpenAI/Hugging
    Face incident two (`openai-huggingface-security`, `huggingface-security-incident`).
    Each fragment then competed against its own siblings for the same cap slots.
    Keys are merged when one's meaningful tokens contain the other's, or when they
    overlap by half.
    """
    groups: dict[str, list[RankedArticle]] = {}
    for article in scored:
        groups.setdefault(article.topic_key or article.id, []).append(article)

    keys = list(groups)
    tokens = {key: topic_tokens(key) for key in keys}
    parent = {key: key for key in keys}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, left in enumerate(keys):
        for right in keys[i + 1 :]:
            if _same_story(tokens[left], tokens[right]):
                union(left, right)

    clustered: dict[str, list[RankedArticle]] = {}
    for key in keys:
        clustered.setdefault(find(key), []).extend(groups[key])
    return list(clustered.values())


def _distinctive(tokens: set[str]) -> bool:
    """A single version-bearing name ('gpt56', 'llama4') identifies a story on its own."""
    return len(tokens) == 1 and all(
        len(t) >= 4 and any(ch.isdigit() for ch in t) for t in tokens
    )


def _same_story(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return False
    if left <= right or right <= left:
        smaller = left if len(left) <= len(right) else right
        return len(smaller) >= 2 or _distinctive(smaller)
    shared = left & right
    if len(left) >= 2 and len(right) >= 2 and len(shared) >= 2:
        if len(shared) / len(left | right) >= 0.5:
            return True
    # Near-identical keys that differ by a typo or a dropped letter
    # ('lingbot-vla-2-0' vs 'lingbot-va-2-0') share too few whole tokens to pass
    # the overlap test but are plainly the same story.
    if shared:
        left_s = "".join(sorted(left))
        right_s = "".join(sorted(right))
        if SequenceMatcher(None, left_s, right_s).ratio() >= 0.85:
            return True
    return False


def _build_representatives(
    clusters: list[list[RankedArticle]],
    social_articles: list[Article],
    heat_bonus_max: float,
) -> list[RankedArticle]:
    reps: list[RankedArticle] = []
    for members in clusters:
        keys = " ".join(m.topic_key for m in members)
        titles = " ".join(m.title for m in members[:4])
        tokens = topic_tokens(keys) or topic_tokens(titles)
        matched = [p.source_name for p in social_articles if social_matches(tokens, p)]

        heat, heat_breakdown = cluster_heat(members, matched)
        rep = _canonical(members, heat_breakdown.get("hn_points", 0.0))

        # The cluster is as important as its best account of the story — the
        # canonical article must not inherit a lower rank for being the plain,
        # official one rather than the most quotable writeup.
        importance = max(m.score for m in members)
        bonus = round(heat_bonus_max * (heat / 100.0), 3)
        rep.score = round(importance + bonus, 3)
        rep.score_breakdown = {
            "importance": round(importance, 3),
            "heat": heat,
            "heat_bonus": bonus,
        }
        rep.heat = heat
        rep.heat_breakdown = heat_breakdown
        rep.cluster_size = len(members)
        rep.publisher = publisher_of(rep)
        rep.discovery_channel = rep.source_id
        rep.related_coverage = [
            f"{m.source_name} — {m.title}" for m in members if m.id != rep.id
        ][:5] + [f"{name} (소셜)" for name in dict.fromkeys(matched)][:3]
        if rep.section not in SECTION_ORDER:
            rep.section = _cluster_section(members) or assign_section(rep)
        rep.section = _refine_section(rep)
        reps.append(rep)
    return reps


# Work that reports a result rather than shipping a thing. Deliberately narrow:
# "benchmark" and "evaluation" appear in security and product copy too (an incident
# "during model evaluation" is not a paper), so they are not triggers on their own.
_RESEARCH_TITLE = re.compile(
    r"arxiv|\bpreprint\b|\bpapers?\b|reproduc(?:tion|ing|es|ed)|replicat(?:ion|ing|es)"
    r"|\bablation\b|논문|재현",
    re.IGNORECASE,
)
_SECURITY_TITLE = re.compile(
    r"security|vulnerab|exploit|breach|incident|attack|jailbreak|취약점|보안|사고",
    re.IGNORECASE,
)


def _refine_section(article: RankedArticle) -> str:
    """Route paper-shaped work to research when the scorer filed it elsewhere.

    The scoring model reads a reproduction of a published method as an open-source
    release because it ships code, which leaves the research section starved while
    `open` runs over quota.
    """
    if article.section == "research":
        return "research"
    if "arxiv.org" in article.url.lower():
        return "research"
    haystack = f"{article.title} {article.summary[:200]}"
    if _SECURITY_TITLE.search(article.title):
        return article.section
    if _RESEARCH_TITLE.search(haystack):
        return "research"
    return article.section


def _cluster_section(members: list[RankedArticle]) -> str:
    counts: dict[str, int] = {}
    for member in members:
        if member.section in SECTION_ORDER:
            counts[member.section] = counts.get(member.section, 0) + 1
    if not counts:
        return ""
    return max(counts, key=lambda sec: (counts[sec], -SECTION_ORDER.index(sec)))


def _canonical(members: list[RankedArticle], peak_points: float) -> RankedArticle:
    """The primary account of the story, not the highest-scoring fragment.

    Ranking a cluster by score picked whichever article the scorer found most
    striking, which is systematically the commentary ("OpenAI's accidental attack
    is science fiction") over the announcement, and a peripheral tool release over
    the model release it was released alongside.
    """
    return max(members, key=lambda m: (_canonicity(m, peak_points), m.score, m.id))


def _canonicity(article: RankedArticle, peak_points: float) -> float:
    title = article.title
    url = article.url.lower()
    domain = publisher_of(article)
    score = 2.0 * float(article.authority_tier)

    if _PRIMARY_TITLE.search(title):
        score += 2.5
    if _COMMENTARY_TITLE.search(title):
        score -= 3.0
    if any(part in url for part in ("/blog/", "/news/", "/research/", "/index/")):
        score += 1.5
    if "huggingface.co/" in url and not any(
        part in url for part in ("/blog/", "/papers/", "/posts/")
    ):
        score += 1.5
    # The vendor's own domain is the primary source for its own story.
    if any(token in domain.replace(".", "") for token in topic_tokens(article.topic_key)):
        score += 2.0
    # Within a cluster, the article the industry actually read.
    score += 3.0 * _log_norm(float(_hn_points(article)), 50.0, max(peak_points, 51.0))
    return round(score, 3)


_PRIMARY_TITLE = re.compile(
    r"\b(introducing|announc\w*|releas\w*|open[-\s]?sourc\w*|now available|available now"
    r"|launch\w*|unveil\w*|general availability|we[''`]re |partners? with)\b",
    re.IGNORECASE,
)
_COMMENTARY_TITLE = re.compile(
    r"\b(why|how|what|opinion|analysis|skeptic\w*|thoughts|hands[-\s]?on|we tried|review"
    r"|explained|vs\.?|versus|compared|hype|is dead|science fiction|what it means"
    r"|takeaways|roundup|recap|first look|deep dive)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# 3-5. fill: guarantee, floor, soft caps
# --------------------------------------------------------------------------- #


def _fill(
    ranked: list[RankedArticle],
    *,
    limit: int,
    quotas: dict[str, int],
    publisher_limit: int,
    owner_limit: int,
    score_floor: float,
    guarantee_top: int,
    guarantee_min_heat: float,
    cap_override_gap: float,
) -> tuple[list[RankedArticle], dict[str, object]]:
    selected: list[RankedArticle] = []
    chosen: set[str] = set()
    guaranteed: set[str] = set()
    publisher_counts: dict[str, int] = {}
    owner_counts: dict[str, int] = {}
    section_counts: dict[str, int] = {sec: 0 for sec in SECTION_ORDER}
    deferred: list[RankedArticle] = []
    overflow: set[str] = set()
    notes: list[str] = []

    def sec_of(article: RankedArticle) -> str:
        return article.section if article.section in SECTION_ORDER else SECTION_ORDER[-1]

    def floor_for(article: RankedArticle) -> float:
        return max(0.0, score_floor + _SECTION_FLOOR_OFFSET.get(sec_of(article), 0.0))

    def blocked(article: RankedArticle) -> str:
        if publisher_counts.get(publisher_of(article), 0) >= publisher_limit:
            return "publisher"
        owner = owner_key_of(article)
        if owner and owner_counts.get(owner, 0) >= owner_limit:
            return "owner"
        return ""

    def admit(article: RankedArticle) -> None:
        selected.append(article)
        chosen.add(article.id)
        publisher = publisher_of(article)
        publisher_counts[publisher] = publisher_counts.get(publisher, 0) + 1
        owner = owner_key_of(article)
        if owner:
            owner_counts[owner] = owner_counts.get(owner, 0) + 1
        section_counts[sec_of(article)] += 1

    # Stage 0 — heat guarantee. The week's loudest stories are admitted before any
    # cap or quota exists. This is the structural answer to "the biggest story must
    # never be missed": it no longer depends on a cap, a quota, or an LLM critic.
    hottest = sorted(ranked, key=lambda a: (-a.heat, -a.score, a.id))
    for article in hottest:
        if len(guaranteed) >= guarantee_top or len(selected) >= limit:
            break
        if article.heat < guarantee_min_heat or article.id in chosen:
            continue
        admit(article)
        guaranteed.add(article.id)
        notes.append(
            f"열기 보장 편입 '{article.title[:46]}' (heat {article.heat:.0f}, "
            f"HN {int(article.heat_breakdown.get('hn_points', 0))}점)"
        )

    # Stage 1 — sections up to quota, best first, never below the quality floor.
    for sec in SECTION_ORDER:
        for article in ranked:
            if section_counts[sec] >= quotas.get(sec, 0) or len(selected) >= limit:
                break
            if sec_of(article) != sec or article.id in chosen:
                continue
            if article.score < floor_for(article) or blocked(article):
                continue
            admit(article)

    # Stage 2 — remaining slots globally, section may exceed its quota by one.
    for article in ranked:
        if len(selected) >= limit:
            break
        if article.id in chosen or article.score < floor_for(article):
            continue
        if section_counts[sec_of(article)] >= quotas.get(sec_of(article), 0) + 1:
            continue
        if blocked(article):
            continue
        admit(article)

    # Stage 3 — caps break ties, they do not delete. Shipping a third article from
    # one publisher beats shipping a short issue, and a story that outranks a
    # selected one by a wide margin overrides the cap rather than vanishing.
    #
    # Section balance is the one thing a cap override does not buy: allocation
    # proportional to the week is a stated requirement, and the heat guarantee
    # above already covers the case it would otherwise protect against.
    section_min = {sec: max(1, quotas.get(sec, 0) - 1) for sec in SECTION_ORDER}

    def _room(article: RankedArticle) -> bool:
        return section_counts[sec_of(article)] < quotas.get(sec_of(article), 0) + 1

    # Everything still eligible, best first. Computing this from `ranked` rather
    # than from what earlier stages happened to reach matters: once the issue fills
    # up, the loops above stop, and anything past that point would never be
    # reconsidered — which is how a story worth 79 could sit out an issue whose
    # weakest entry scored 51.
    def pending() -> list[RankedArticle]:
        return [
            a
            for a in ranked
            if a.id not in chosen and a.score >= floor_for(a)
        ]

    # Stage 3a — spare slots, cap relaxed but section balance kept.
    for article in pending():
        if len(selected) >= limit:
            break
        if not _room(article):
            continue
        if blocked(article):
            notes.append(f"캡 해제 편입 '{article.title[:44]}' (빈 슬롯, {article.score:.0f}점)")
        admit(article)

    # Stage 3b — only once nothing else can fill the issue does section balance
    # yield: a short issue is worse than one section running long.
    for article in pending():
        if len(selected) >= limit:
            break
        admit(article)
        overflow.add(article.id)
        notes.append(
            f"섹션 초과 편입 '{article.title[:42]}' "
            f"({sec_of(article)} 쿼터 초과, 대체 후보 없음)"
        )

    # Stage 3c — a story that outranks a published one by a wide margin overrides
    # the cap that blocked it, instead of vanishing.
    for article in pending():
        def _replaceable(current: RankedArticle) -> bool:
            if current.id in guaranteed:
                return False
            if article.score - current.score <= cap_override_gap:
                return False
            # A cap is a rule about publishers, so overriding one must never worsen
            # the allocation. Swapping inside a section is neutral; across sections
            # it is allowed only when it moves a slot from an over-served section to
            # an under-served one. Without that restriction a 92-point frontier
            # story evicted a research paper, leaving research with one entry while
            # four qualified papers sat unused.
            if sec_of(current) == sec_of(article):
                return True
            return (
                section_counts[sec_of(current)] > quotas.get(sec_of(current), 0)
                and section_counts[sec_of(article)] < quotas.get(sec_of(article), 0)
            )

        swappable = [a for a in selected if _replaceable(a)]
        if not swappable:
            continue
        weakest = min(swappable, key=lambda a: a.score)
        selected.remove(weakest)
        chosen.discard(weakest.id)
        publisher_counts[publisher_of(weakest)] -= 1
        owner = owner_key_of(weakest)
        if owner:
            owner_counts[owner] -= 1
        section_counts[sec_of(weakest)] -= 1
        admit(article)
        notes.append(
            f"캡 역전 교체 '{article.title[:40]}'({article.score:.0f}점) "
            f"← '{weakest.title[:34]}'({weakest.score:.0f}점)"
        )

    shortfalls = [
        sec for sec in SECTION_ORDER if section_counts[sec] < max(1, quotas.get(sec, 0) - 1)
    ]
    return selected[:limit], {
        "notes": notes,
        "shortfalls": shortfalls,
        "section_counts": section_counts,
        "guaranteed_ids": sorted(guaranteed),
        # Admitted outside their section's quota, and why it was allowed.
        "quota_exempt_ids": sorted(guaranteed | overflow),
        "publisher_counts": publisher_counts,
    }


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def _drop_reason(
    article: RankedArticle,
    *,
    selected: list[RankedArticle],
    score_floor: float,
    publisher_limit: int,
    quotas: dict[str, int],
    section_counts: dict[str, int],
    limit: int,
) -> str:
    sec_floor = max(
        0.0,
        score_floor
        + _SECTION_FLOOR_OFFSET.get(
            article.section if article.section in SECTION_ORDER else SECTION_ORDER[-1], 0.0
        ),
    )
    if article.score < sec_floor:
        return f"점수 하한 미달({article.score:.0f} < {sec_floor:.0f})"
    publisher = publisher_of(article)
    if sum(1 for a in selected if publisher_of(a) == publisher) >= publisher_limit:
        return f"발행사 상한({publisher} {publisher_limit}건)"
    owner = owner_key_of(article)
    if owner and any(owner_key_of(a) == owner for a in selected):
        return f"소유자 상한({owner})"
    sec = article.section if article.section in SECTION_ORDER else SECTION_ORDER[-1]
    if section_counts.get(sec, 0) >= quotas.get(sec, 0) + 1:
        return f"섹션 쿼터({sec})"
    if len(selected) >= limit:
        return "슬롯 소진"
    return "미상"


def _build_report(
    *,
    mode: str,
    candidates: list[Article],
    pool: list[Article],
    scored: list[RankedArticle],
    clusters: list[list[RankedArticle]],
    ranked: list[RankedArticle],
    selected: list[RankedArticle],
    fill_report: dict[str, object],
    limit: int,
    quotas: dict[str, int],
    score_floor: float,
    publisher_limit: int,
    merged_dupes: int,
    llm_omissions: int,
) -> dict[str, object]:
    section_counts = fill_report["section_counts"]  # type: ignore[index]
    selected_ids = {a.id for a in selected}
    lowest = min((a.score for a in selected), default=0.0)

    # Deterministic replacement for the LLM completeness critic: anything that
    # outranked a published article and did not ship must state why, in the build
    # output. No investigation, no second paid model call.
    inversions = [
        {
            "title": a.title[:80],
            "score": a.score,
            "heat": a.heat,
            "section": a.section,
            "publisher": publisher_of(a),
            "channel": a.discovery_channel or a.source_id,
            "reason": _drop_reason(
                a,
                selected=selected,
                score_floor=score_floor,
                publisher_limit=publisher_limit,
                quotas=quotas,
                section_counts=section_counts,  # type: ignore[arg-type]
                limit=limit,
            ),
        }
        for a in ranked
        if a.id not in selected_ids and a.score > lowest
    ]

    return {
        "mode": mode,
        "candidate_count": len(candidates),
        "merged_duplicates": merged_dupes,
        "pool_count": len(pool),
        "scored_count": len(scored),
        "llm_omissions_recovered": llm_omissions,
        "clusters": len(clusters),
        "clusters_merged": sum(1 for c in clusters if len(c) > 1),
        "selected_count": len(selected),
        "section_quotas": quotas,
        "section_counts": section_counts,
        "section_shortfalls": {sec: SECTION_META[sec]["empty"] for sec in fill_report["shortfalls"]},  # type: ignore[index]
        "score_floor": score_floor,
        "heat_guarantees": fill_report["guaranteed_ids"],
        "fill_notes": fill_report["notes"],
        "score_inversions": inversions,
        "ranking": [
            {
                "rank": rank,
                "selected": a.id in selected_ids,
                "importance": a.score_breakdown.get("importance", a.score),
                "heat": a.heat,
                "score": a.score,
                "section": a.section,
                "publisher": publisher_of(a),
                "channel": a.discovery_channel or a.source_id,
                "cluster_size": a.cluster_size,
                "topic_key": a.topic_key,
                "title": a.title[:80],
            }
            for rank, a in enumerate(ranked[:30], 1)
        ],
        "selected": [
            {
                "title": a.title,
                "publisher": publisher_of(a),
                "section": a.section,
                "score": a.score,
                "heat": a.heat,
                "cluster_size": a.cluster_size,
                "topic_key": a.topic_key,
                "reason": a.reason,
            }
            for a in selected
        ],
        "selection_contract": {
            "rule": "열기 상위 보장 → 섹션 쿼터(하한 적용) → 전체 중요도순 → 캡 역전 교체",
            "identity": "캡 기준은 기사 URL의 발행 도메인(피드 id 아님)",
            "guarantee": "heat 상위 건은 캡·쿼터와 무관하게 확정 편입",
            "floor": (
                f"{score_floor:.0f}점(연구 {score_floor + _SECTION_FLOOR_OFFSET['research']:.0f}점) "
                "미만으로는 쿼터를 채우지 않고 부족을 명시"
            ),
            "cap": f"발행사당 {publisher_limit}건이 원칙이나, 점수 격차가 크면 교체로 역전 허용",
            "dedup": "같은 사건은 1건만, 대표는 최고점이 아니라 정본(공식 발표) 기준",
        },
    }
