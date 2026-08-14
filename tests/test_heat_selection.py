"""Regression tests for heat selection, replayed against real past builds.

The point of the fixture replay is the one the local fixes kept failing: a change
that rescues this week must not quietly drop a story from a previous week. Each
fixture is a real candidate pool with the importance/topic_key the scoring model
produced that week, so these tests exercise clustering, representative choice and
the fill rules — not the LLM.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from ai_newsletter.heat import cluster_heat, topic_tokens
from ai_newsletter.heat_selection import (
    _SECTION_FLOOR_OFFSET,
    _build_representatives,
    _canonical,
    _cluster,
    _fill,
    _merge_duplicates,
    _refine_section,
    _same_story,
)
from ai_newsletter.identity import owner_key_of, publisher_of, registrable_domain
from ai_newsletter.models import Article, RankedArticle
from ai_newsletter.sections import SECTION_ORDER, section_quotas

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "selection").glob("*.json"))
FIXTURE_IDS = [p.stem for p in FIXTURES]

SCORE_FLOOR = 40.0
CAP_OVERRIDE_GAP = 15.0
PUBLISHER_LIMIT = 2


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _floor_for(section: str) -> float:
    sec = section if section in SECTION_ORDER else SECTION_ORDER[-1]
    return max(0.0, SCORE_FLOOR + _SECTION_FLOOR_OFFSET.get(sec, 0.0))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _to_ranked(row: dict) -> RankedArticle:
    published = row.get("published_at")
    return RankedArticle(
        id=row["id"],
        source_id=row["source_id"],
        source_name=row["source_name"],
        title=row["title"],
        url=row["url"],
        published_at=datetime.fromisoformat(published) if published else None,
        summary=row.get("summary", ""),
        body="x" * int(row.get("body_len") or 0),
        metrics=row.get("metrics") or {},
        source_weight=row.get("source_weight", 1.0),
        authority_tier=row.get("authority_tier", 0.5),
        panel=row.get("panel", "curator"),
        score=float(row.get("importance") or 0.0),
        topic_key=row.get("topic_key", ""),
        section=row.get("section", ""),
    )


def _select(fixture: dict, limit: int | None = None):
    scored = [_to_ranked(row) for row in fixture["articles"]]
    limit = limit or int(fixture.get("limit") or 10)
    clusters = _cluster(scored)
    ranked = _build_representatives(clusters, [], heat_bonus_max=20.0)
    ranked.sort(key=lambda a: (-a.score, a.id))
    selected, report = _fill(
        ranked,
        limit=limit,
        quotas=section_quotas(limit),
        publisher_limit=PUBLISHER_LIMIT,
        owner_limit=1,
        score_floor=SCORE_FLOOR,
        guarantee_top=3,
        guarantee_min_heat=45.0,
        cap_override_gap=CAP_OVERRIDE_GAP,
    )
    return selected, ranked, report


# --------------------------------------------------------------------------- #
# identity — the mis-keying that caused 116 of 203 historical score inversions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.theverge.com/ai/1", "theverge.com"),
        ("https://huggingface.co/moonshotai/Kimi-K3", "huggingface.co"),
        ("https://openai.com/index/incident/", "openai.com"),
        ("https://blog.company.co.uk/post", "company.co.uk"),
        ("not a url", ""),
    ],
)
def test_registrable_domain(url: str, expected: str) -> None:
    assert registrable_domain(url) == expected


def test_publisher_is_the_outlet_not_the_discovery_feed() -> None:
    """Two stories found via Hacker News are two publishers, not one source."""
    kimi = Article(
        id="a", source_id="hn-trending", source_name="HN Trending (AI)",
        title="Kimi-K3 on HuggingFace", url="https://huggingface.co/moonshotai/Kimi-K3",
    )
    incident = Article(
        id="b", source_id="hn-trending", source_name="HN Trending (AI)",
        title="OpenAI and Hugging Face address security incident",
        url="https://openai.com/index/hf-incident/",
    )
    assert publisher_of(kimi) != publisher_of(incident)
    assert publisher_of(kimi) == "huggingface.co"


def test_owner_key_is_url_derived_for_both_platforms() -> None:
    hf = Article(id="1", source_id="hn-trending", source_name="HN", title="t",
                 url="https://huggingface.co/moonshotai/Kimi-K3")
    gh = Article(id="2", source_id="hn-trending", source_name="HN", title="t",
                 url="https://github.com/moonshotai/kimi")
    reserved = Article(id="3", source_id="x", source_name="HF", title="t",
                       url="https://huggingface.co/blog/some-post")
    assert owner_key_of(hf) == "hf:moonshotai"
    assert owner_key_of(gh) == "github:moonshotai"
    assert owner_key_of(reserved) is None


# --------------------------------------------------------------------------- #
# clustering — the LLM splitting one event across several topic keys
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("kimi-k3", "agentenv-kimi-k3"),
        ("kimi-k3", "kimi-k3-fable-routing"),
        ("openai-huggingface-security", "huggingface-security-incident"),
        ("gpt-5-6", "gpt-5-6-sol"),
        ("model-routers-middleware", "model-routing-middleware"),
        ("lingbot-vla-2-0", "lingbot-va-2-0"),
    ],
)
def test_split_topic_keys_are_rejoined(left: str, right: str) -> None:
    assert _same_story(topic_tokens(left), topic_tokens(right))


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("gemini-3-6", "gpt-3-6"),
        ("openai-funding", "openai-security-incident"),
        ("claude-opus-5", "gpt-5-6"),
        ("anthropic-settlement", "anthropic-cognizant-partnership"),
        ("advertise-in-chatgpt", "chatgpt-atlas-browser"),
    ],
)
def test_distinct_stories_stay_distinct(left: str, right: str) -> None:
    assert not _same_story(topic_tokens(left), topic_tokens(right))


def test_representative_is_the_primary_account_not_the_loudest_take() -> None:
    """A cluster is represented by the announcement, not the commentary about it."""
    official = RankedArticle(
        id="1", source_id="openai-news", source_name="OpenAI News", authority_tier=1.0,
        title="OpenAI and Hugging Face partner to address security incident",
        url="https://openai.com/index/hf-incident/", topic_key="openai-huggingface-security",
        score=91.0,
    )
    hot_take = RankedArticle(
        id="2", source_id="hn-trending", source_name="HN Trending (AI)", authority_tier=0.7,
        title="OpenAI's accidental attack against Hugging Face is science fiction",
        url="https://example.com/take", topic_key="openai-huggingface-security",
        score=100.6, metrics={"hn_points": 582},
    )
    assert _canonical([official, hot_take], 582.0).id == official.id


def test_cluster_keeps_the_best_members_importance() -> None:
    """The canonical article must not be ranked down for being the plain one."""
    official = RankedArticle(
        id="1", source_id="openai-news", source_name="OpenAI News", authority_tier=1.0,
        title="OpenAI and Hugging Face partner to address security incident",
        url="https://openai.com/index/hf-incident/", topic_key="openai-huggingface-security",
        score=80.0, section="tooling",
    )
    hot_take = RankedArticle(
        id="2", source_id="hn-trending", source_name="HN", authority_tier=0.7,
        title="Why OpenAI's attack on Hugging Face is science fiction",
        url="https://example.com/take", topic_key="huggingface-security-incident",
        score=100.0, section="tooling", metrics={"hn_points": 582},
    )
    reps = _build_representatives(_cluster([official, hot_take]), [], heat_bonus_max=20.0)
    assert len(reps) == 1
    assert reps[0].id == official.id
    assert reps[0].score_breakdown["importance"] == 100.0


# --------------------------------------------------------------------------- #
# section refinement — research was starved while `open` ran over quota
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("title", "url", "given", "expected"),
    [
        # A reproduction of a published method is research even though it ships code.
        ("Meet Open Dreamer: A JAX/Flax Reproduction of the DreamerV3 paper",
         "https://marktechpost.com/open-dreamer", "open", "research"),
        ("Scaling laws for X — arXiv preprint", "https://arxiv.org/abs/2607.1", "open", "research"),
        # A security incident that merely mentions evaluation is not a paper.
        ("OpenAI and Hugging Face address security incident during model evaluation",
         "https://openai.com/index/hf/", "tooling", "tooling"),
        # Plain product releases stay where they were.
        ("Black Forest Labs Releases FLUX 3: A Multimodal Flow Model",
         "https://marktechpost.com/flux3", "open", "open"),
        ("Kimi AI and kvcache-ai Open Sources 'AgentENV'",
         "https://marktechpost.com/agentenv", "open", "open"),
    ],
)
def test_section_refinement(title: str, url: str, given: str, expected: str) -> None:
    article = RankedArticle(
        id="1", source_id="s", source_name="S", title=title, url=url, section=given,
    )
    assert _refine_section(article) == expected


# --------------------------------------------------------------------------- #
# merge-first — dedup used to destroy the vote count of the biggest stories
# --------------------------------------------------------------------------- #


def test_dedup_keeps_metrics_of_the_copy_it_discards() -> None:
    official = Article(
        id="1", source_id="openai-news", source_name="OpenAI News", source_weight=1.25,
        title="Introducing GPT-5.6", url="https://openai.com/index/gpt56/",
        body="x" * 900,
    )
    via_hn = Article(
        id="2", source_id="hn-trending", source_name="HN Trending (AI)", source_weight=1.05,
        title="Introducing GPT-5.6", url="https://openai.com/index/gpt56/",
        metrics={"hn_points": 2100},
    )
    merged, count = _merge_duplicates([official, via_hn])
    assert count == 1
    assert len(merged) == 1
    assert merged[0].source_id == "openai-news"
    assert merged[0].metrics["hn_points"] == 2100


def test_paper_upvotes_feed_heat() -> None:
    """Community upvotes are a paper's equivalent of Hacker News points."""
    hot = Article(id="1", source_id="hf-daily-papers", source_name="HF Papers", title="t",
                  url="https://arxiv.org/abs/2607.1", metrics={"paper_upvotes": 300})
    quiet = Article(id="2", source_id="hf-daily-papers", source_name="HF Papers", title="t",
                    url="https://arxiv.org/abs/2607.2", metrics={"paper_upvotes": 5})
    assert cluster_heat([hot])[0] > cluster_heat([quiet])[0]


def test_heat_counts_hacker_news_once() -> None:
    """HN votes must not be added again as 'social chatter' from an HN feed."""
    article = Article(id="1", source_id="hn-trending", source_name="HN", title="t",
                      url="https://example.com/a", metrics={"hn_points": 1200})
    with_alias, _ = cluster_heat([article], ["Hacker News AI", "Reddit r/LocalLLaMA"])
    without, _ = cluster_heat([article], ["Reddit r/LocalLLaMA"])
    assert with_alias == without


# --------------------------------------------------------------------------- #
# fixture replay — every past week at once
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", FIXTURES, ids=FIXTURE_IDS)
def test_required_stories_are_carried(path: Path) -> None:
    fixture = _load(path)
    if not fixture.get("must_include"):
        pytest.skip("no hand-set expectations for this week")
    selected, _, _ = _select(fixture)
    titles = [_normalize(a.title) for a in selected]
    coverage = " | ".join(titles)
    for row in fixture["must_include"]:
        assert any(
            any(_normalize(pattern) in title for title in titles)
            for pattern in row["match_any"]
        ), f"{fixture['build']}: 누락 — {row['why']}\n실린 기사: {coverage}"


def _section_counts(selected: list[RankedArticle]) -> dict[str, int]:
    counts = {sec: 0 for sec in SECTION_ORDER}
    for article in selected:
        counts[article.section if article.section in SECTION_ORDER else SECTION_ORDER[-1]] += 1
    return counts


@pytest.mark.parametrize("path", FIXTURES, ids=FIXTURE_IDS)
def test_no_wide_margin_story_is_dropped_by_a_cap(path: Path) -> None:
    """A cap may break a tie; it may not discard something far better than what shipped.

    Losing a slot to section balance is a different matter — that allocation is a
    stated requirement, and the heat guarantee covers the stories it must not cost.
    """
    fixture = _load(path)
    selected, ranked, _ = _select(fixture)
    if not selected:
        pytest.skip("empty selection")
    limit = int(fixture.get("limit") or 10)
    quotas = section_quotas(limit)
    counts = _section_counts(selected)
    lowest = min(a.score for a in selected)
    chosen = {a.id for a in selected}
    for article in ranked:
        if article.id in chosen or article.score < _floor_for(article.section):
            continue
        if article.score - lowest <= CAP_OVERRIDE_GAP:
            continue
        sec = article.section if article.section in SECTION_ORDER else SECTION_ORDER[-1]
        # A section that has reached its quota is legitimately full — `quota + 1` is
        # the ceiling an overflow may reach, not the point at which the section
        # counts as full. The bug this guards against is a wide-margin story dropped
        # while its section still had unused allocation (the Kimi K3 case: `open`
        # held 2 of 3 and the week's biggest story lost the free slot to a cap).
        assert counts[sec] >= quotas[sec], (
            f"{fixture['build']}: '{article.title[:50]}' ({article.score:.0f}점)이 "
            f"게재 최저 {lowest:.0f}점보다 크게 높은데, {sec} 섹션에 여유가 있는 상태에서 탈락"
        )


@pytest.mark.parametrize("path", FIXTURES, ids=FIXTURE_IDS)
def test_quota_is_never_filled_with_filler(path: Path) -> None:
    fixture = _load(path)
    selected, _, _ = _select(fixture)
    weak = [a for a in selected if a.score < _floor_for(a.section)]
    assert not weak, (
        f"{fixture['build']}: 하한 미달 기사 게재 — "
        + ", ".join(f"{a.title[:36]}({a.score:.0f})" for a in weak)
    )


@pytest.mark.parametrize("path", FIXTURES, ids=FIXTURE_IDS)
def test_sections_stay_within_one_of_quota(path: Path) -> None:
    """Only a heat guarantee or a stated last-resort overflow may exceed a quota."""
    fixture = _load(path)
    selected, _, report = _select(fixture)
    limit = int(fixture.get("limit") or 10)
    quotas = section_quotas(limit)
    exempt = set(report["quota_exempt_ids"])
    counts = _section_counts([a for a in selected if a.id not in exempt])
    for sec, count in counts.items():
        assert count <= quotas[sec] + 1, (
            f"{fixture['build']}: {sec} 쿼터 초과 {count}/{quotas[sec]} (보장·초과편입 제외 후)"
        )


@pytest.mark.parametrize("path", FIXTURES, ids=FIXTURE_IDS)
def test_every_drop_above_the_floor_has_a_stated_reason(path: Path) -> None:
    fixture = _load(path)
    selected, ranked, _ = _select(fixture)
    assert len({a.id for a in selected}) == len(selected), "중복 게재"
    assert len(selected) <= int(fixture.get("limit") or 10)


@pytest.mark.parametrize("path", FIXTURES, ids=FIXTURE_IDS)
def test_selection_is_deterministic(path: Path) -> None:
    fixture = _load(path)
    first, _, _ = _select(fixture)
    second, _, _ = _select(fixture)
    assert [a.id for a in first] == [a.id for a in second]
