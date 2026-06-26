from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ai_newsletter.latest_selection import (
    default_latest_source_ids,
    latest_quality_report,
    select_latest_articles,
)
from ai_newsletter.models import Article, SourceConfig


def _source(source_id: str, kind: str = "rss") -> SourceConfig:
    return SourceConfig(
        id=source_id,
        name=source_id,
        kind=kind,  # type: ignore[arg-type]
        url="https://example.com",
        weight=1.0,
        authority_tier=0.8,
    )


def _article(source_id: str, title: str, age_days: int) -> Article:
    return Article(
        id=f"{source_id}-{title}",
        source_id=source_id,
        source_name=source_id,
        title=title,
        url=f"https://example.com/{source_id}/{title}",
        published_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        summary="AI agent model security for enterprise workflow automation.",
        body="Detailed article body about AI agents and enterprise security. " * 20,
        authority_tier=0.8,
    )


def test_default_latest_sources_exclude_platform_sources() -> None:
    sources = [_source("blog", "rss"), _source("site", "webpage"), _source("github", "github")]

    assert default_latest_source_ids(sources) == {"blog", "site"}


def test_select_latest_articles_filters_week_and_source_ids() -> None:
    sources = [_source("blog"), _source("other")]
    articles = [
        _article("blog", "fresh-agent", 1),
        _article("blog", "old-agent", 9),
        _article("other", "fresh-other", 1),
    ]

    selected = select_latest_articles(
        articles,
        sources,
        days=7,
        limit=10,
        source_ids={"blog"},
    )

    assert [article.title for article in selected] == ["fresh-agent"]


def test_select_latest_articles_excludes_listing_pages() -> None:
    sources = [_source("marktechpost", "webpage")]
    listing = _article("marktechpost", "Artificial Intelligence", 1)
    listing.url = "https://www.marktechpost.com/category/technology/artificial-intelligence/"
    article = _article("marktechpost", "New AI agent model released", 1)
    article.url = "https://www.marktechpost.com/2026/06/25/new-ai-agent-model-released/"

    selected = select_latest_articles(
        [listing, article],
        sources,
        days=7,
        source_ids={"marktechpost"},
    )

    assert [row.title for row in selected] == ["New AI agent model released"]


def test_select_latest_articles_can_fill_from_fallback_sources() -> None:
    sources = [_source("primary"), _source("fallback")]
    articles = [
        _article("primary", "primary-agent", 1),
        _article("fallback", "fallback-agent", 1),
    ]

    selected = select_latest_articles(
        articles,
        sources,
        days=7,
        limit=2,
        source_ids={"primary"},
        fallback_source_ids={"fallback"},
    )

    assert [row.title for row in selected] == ["primary-agent", "fallback-agent"]


def test_latest_quality_report_counts_selected_fallback_sources() -> None:
    primary = _article("primary", "primary-agent", 1)
    fallback = _article("fallback", "fallback-agent", 1)

    report = latest_quality_report(
        [primary, fallback],  # type: ignore[list-item]
        [primary, fallback],
        source_ids={"primary"},
        fallback_source_ids={"fallback"},
        days=7,
    )

    assert report["selected_source_counts"] == {"primary": 1, "fallback": 1}
    assert report["candidate_source_counts"] == {"primary": 1}
