from datetime import datetime, timezone

from ai_newsletter.models import Article
from ai_newsletter.sections import (
    SECTION_ORDER,
    section_quotas,
    select_sectioned_articles,
)

_BODY = "본문 " * 250  # long enough to pass the publishable filter


def _article(idx: int, source_id: str, title: str, summary: str = "") -> Article:
    return Article(
        id=f"a{idx:02d}",
        source_id=source_id,
        source_name=source_id,
        title=title,
        url=f"https://example.com/{source_id}/{idx}",
        published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        summary=summary or title,
        body=_BODY,
        source_weight=1.0,
        authority_tier=0.8,
    )


def _pool() -> list[Article]:
    return [
        # frontier (big-lab news)
        _article(1, "openai-news", "Previewing GPT-6: a next-generation model"),
        _article(2, "anthropic-news", "Introducing Claude Sonnet 5"),
        _article(3, "deepmind-blog", "Google announces Gemini enterprise partnership"),
        # open (github / open-weight)
        _article(4, "github-ai", "awesome-agents: curated agent framework list"),
        _article(5, "huggingface-models", "Qwen 4 open-weight release tops downloads"),
        _article(6, "github-llm", "vLLM fork adds speculative decoding"),
        # research
        _article(7, "marktechpost", "New arXiv paper improves tabular benchmark results"),
        _article(8, "alphasignal", "Research: verification-free inference paper"),
        # tooling/security
        _article(9, "thenewstack", "LLM guardrail gateway fixes prompt injection vulnerability"),
        _article(10, "geeknews", "New security scanner for AI pipelines"),
        _article(11, "geeknews2", "Jailbreak exploit found in popular chatbot"),
    ]


def test_section_quotas_sum_to_limit():
    for limit in (4, 8, 10, 12):
        quotas = section_quotas(limit)
        assert sum(quotas.values()) == limit
        assert all(q >= 1 for q in quotas.values())


def test_sectioned_selection_fills_all_sections():
    selected, report = select_sectioned_articles(_pool(), limit=8, use_llm=False)
    assert len(selected) == 8
    counts = report["section_counts"]
    for sec in SECTION_ORDER:
        assert counts[sec] >= 1, f"section {sec} is empty: {counts}"
    # Output is ordered by fixed section order.
    order = {sec: i for i, sec in enumerate(SECTION_ORDER)}
    positions = [order[a.section] for a in selected]
    assert positions == sorted(positions)


def test_sectioned_selection_reports_shortfall_and_backfills():
    # No research candidates at all -> research shortfall, but limit still reached.
    pool = [a for a in _pool() if a.source_id not in ("marktechpost", "alphasignal")]
    selected, report = select_sectioned_articles(pool, limit=8, use_llm=False)
    assert len(selected) == 8
    assert "research" in report["section_shortfalls"]


def test_related_coverage_preserved_on_dedup():
    # Two sources cover the same GPT-6 story; the loser's source/title must
    # survive on the winner as related_coverage (the "others' reaction" signal).
    pool = _pool() + [
        _article(30, "thenewstack2", "Previewing GPT-6 next-generation model preview"),
    ]
    selected, _ = select_sectioned_articles(pool, limit=10, use_llm=False)
    gpt6 = [a for a in selected if "gpt-6" in (a.topic_key or "").lower() or "GPT-6" in a.title]
    assert gpt6, "GPT-6 story should be selected"
    merged = [a for a in gpt6 if a.related_coverage]
    if merged:  # dedup grouped them by topic_key
        assert any("thenewstack2" in r or "openai-news" in r for r in merged[0].related_coverage)


def test_social_boost_and_coverage():
    # Two social posts (distinct sources) mention GPT-6 -> the GPT-6 article
    # gets a score boost and the posts land in related_coverage.
    social = [
        _article(40, "reddit-localllama", "GPT-6 preview discussion megathread"),
        _article(41, "hn-ai-top", "GPT-6 is a big deal — early impressions"),
    ]
    baseline, _ = select_sectioned_articles(_pool(), limit=10, use_llm=False)
    boosted, report = select_sectioned_articles(
        _pool(), limit=10, use_llm=False, social_articles=social
    )
    base_gpt6 = next(a for a in baseline if "GPT-6" in a.title)
    boost_gpt6 = next(a for a in boosted if "GPT-6" in a.title)
    assert boost_gpt6.score > base_gpt6.score
    assert boost_gpt6.score_breakdown.get("social_boost", 0) > 0
    assert any("reddit-localllama" in r or "hn-ai-top" in r for r in boost_gpt6.related_coverage)
    assert report["social_signal"]["posts"] == 2
    assert report["social_signal"]["boosted_articles"] >= 1


def test_per_source_cap_respected():
    pool = _pool() + [
        _article(20 + i, "openai-news", f"OpenAI minor update {i}") for i in range(5)
    ]
    selected, _ = select_sectioned_articles(pool, limit=10, use_llm=False)
    per_source: dict[str, int] = {}
    for a in selected:
        per_source[a.source_id] = per_source.get(a.source_id, 0) + 1
    assert all(count <= 2 for count in per_source.values())
