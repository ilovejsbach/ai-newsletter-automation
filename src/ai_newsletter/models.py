from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class SourceConfig(BaseModel):
    id: str
    name: str
    kind: Literal["rss", "webpage", "github", "huggingface"]
    url: str | None = None
    query: str | None = None
    language: str = "en"
    weight: float = 1.0
    panel: str = "curator"
    authority_tier: float = 0.5
    enabled: bool = True


class CollectionOptions(BaseModel):
    require_dates: bool = True
    strict_week: bool = True
    per_source_limit: int = 50


class Article(BaseModel):
    id: str
    source_id: str
    source_name: str
    title: str
    url: str
    published_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    summary: str = ""
    body: str = ""
    image_urls: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metrics: dict[str, int | float | str | None] = Field(default_factory=dict)
    source_weight: float = 1.0
    panel: str = "curator"
    authority_tier: float = 0.5


class RankedArticle(Article):
    score: float = 0.0
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    reason: str = ""
    korean_title: str = ""
    korean_summary: str = ""
    why_it_matters: str = ""
    terms: list[str] = Field(default_factory=list)
    detail_intro: str = ""
    detail_sections: list[dict[str, str]] = Field(default_factory=list)
    local_image: str = ""


class Issue(BaseModel):
    id: str
    title: str
    summary: str
    why_hot: str
    enterprise_relevance: str = ""
    keywords: list[str] = Field(default_factory=list)
    article_ids: list[str] = Field(default_factory=list)
    representative_article_id: str = ""
    score: float = 0.0
    score_breakdown: dict[str, float] = Field(default_factory=dict)


class NewsletterPackage(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    period_start: datetime
    period_end: datetime
    title: str
    articles: list[RankedArticle]
    issues: list[Issue] = Field(default_factory=list)
    quality_report: dict[str, Any] = Field(default_factory=dict)
    output_dir: Path


class SourceList(BaseModel):
    sources: list[SourceConfig]
