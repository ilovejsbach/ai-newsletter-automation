from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup
from dateutil.parser import parse as parse_date

from .models import Article, CollectionOptions, SourceConfig

USER_AGENT = "ai-newsletter-automation/0.1"


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def strip_html(value: str) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except Exception:
        try:
            dt = parse_date(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None


def parse_date_from_text(text: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    patterns = [
        r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b",
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:,\s*(20\d{2}))?\b",
    ]
    match = re.search(patterns[0], text, re.I)
    if match:
        year, month, day = map(int, match.groups())
        return datetime(year, month, day, tzinfo=timezone.utc)
    match = re.search(patterns[1], text, re.I)
    if match:
        month_name, day, year = match.groups()
        parsed = parse_datetime(f"{month_name} {day}, {year or now.year}")
        if parsed and parsed > now + timedelta(days=7):
            parsed = parsed.replace(year=parsed.year - 1)
        return parsed
    return None


class Collector:
    def __init__(self, timeout: float = 20.0, options: CollectionOptions | None = None) -> None:
        self.options = options or CollectionOptions()
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )

    def close(self) -> None:
        self.client.close()

    def collect(self, source: SourceConfig, days: int) -> list[Article]:
        if source.kind == "rss":
            return self.collect_rss(source, days)
        if source.kind == "webpage":
            return self.collect_webpage(source, days)
        if source.kind == "github":
            return self.collect_github(source, days)
        if source.kind == "huggingface":
            return self.collect_huggingface(source, days)
        return []

    def collect_rss(self, source: SourceConfig, days: int) -> list[Article]:
        if not source.url:
            return []
        resp = self.client.get(source.url)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        articles: list[Article] = []
        for entry in entries:
            if len(articles) >= self.options.per_source_limit:
                break
            title = _xml_text(entry, "title")
            link = _xml_text(entry, "link")
            if not link:
                link_node = entry.find("{http://www.w3.org/2005/Atom}link")
                link = link_node.attrib.get("href", "") if link_node is not None else ""
            published = (
                parse_datetime(_xml_text(entry, "pubDate"))
                or parse_datetime(_xml_text(entry, "published"))
                or parse_datetime(_xml_text(entry, "updated"))
            )
            if self.options.require_dates and published is None:
                continue
            if self.options.strict_week and published and published < cutoff:
                continue
            summary = strip_html(
                _xml_text(entry, "description")
                or _xml_text(entry, "summary")
                or _xml_text(entry, "content")
            )
            image_urls = _extract_images_from_xml(entry)
            body, detail_images = self.fetch_article_detail(link) if title and link else ("", [])
            if title and link:
                articles.append(
                    Article(
                        id=stable_id(link),
                        source_id=source.id,
                        source_name=source.name,
                        title=unescape(title).strip(),
                        url=link.strip(),
                        published_at=published,
                        summary=summary[:1200],
                        body=body,
                        image_urls=list(dict.fromkeys(image_urls + detail_images))[:3],
                        source_weight=source.weight,
                        panel=source.panel,
                        authority_tier=source.authority_tier,
                    )
                )
        return articles

    def collect_webpage(self, source: SourceConfig, days: int) -> list[Article]:
        if not source.url:
            return []
        resp = self.client.get(source.url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        articles: list[Article] = []
        for link in soup.select("a[href]"):
            if len(articles) >= self.options.per_source_limit:
                break
            title = " ".join(link.get_text(" ", strip=True).split())
            title = _clean_title(title)
            href = urljoin(source.url, link["href"])
            if len(title) < 12 or not href.startswith("http"):
                continue
            if not _looks_ai_related(title):
                continue
            published = parse_date_from_text(title)
            if published is None:
                nearby = link.find_parent(["article", "li", "div"])
                if nearby is not None:
                    published = parse_date_from_text(nearby.get_text(" ", strip=True))
            body, image_urls = self.fetch_article_detail(href)
            if published is None and body:
                published = parse_date_from_text(body[:3000])
            if self.options.require_dates and published is None:
                continue
            if self.options.strict_week and published and published < cutoff:
                continue
            articles.append(
                Article(
                    id=stable_id(href),
                    source_id=source.id,
                    source_name=source.name,
                    title=title,
                    url=href,
                    published_at=published,
                    body=body,
                    image_urls=image_urls[:3],
                    source_weight=source.weight,
                    panel=source.panel,
                    authority_tier=source.authority_tier,
                )
            )
        return list({a.id: a for a in articles}.values())[: self.options.per_source_limit]

    def collect_github(self, source: SourceConfig, days: int) -> list[Article]:
        query = source.query or "topic:llm"
        since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        url = (
            "https://api.github.com/search/repositories"
            f"?q={quote_plus(f'({query}) pushed:>={since}')}&sort=stars&order=desc&per_page=30"
        )
        headers = {}
        if token := os.getenv("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        resp = self.client.get(url, headers=headers)
        resp.raise_for_status()
        items = resp.json().get("items", [])[: self.options.per_source_limit]
        articles: list[Article] = []
        for item in items:
            repo_url = item.get("html_url", "")
            metrics = {
                "stars": item.get("stargazers_count", 0),
                "forks": item.get("forks_count", 0),
                "open_issues": item.get("open_issues_count", 0),
                "pushed_at": item.get("pushed_at"),
            }
            articles.append(
                Article(
                    id=stable_id(repo_url),
                    source_id=source.id,
                    source_name=source.name,
                    title=item.get("full_name") or item.get("name") or repo_url,
                    url=repo_url,
                    published_at=parse_datetime(item.get("pushed_at")) or parse_datetime(item.get("updated_at")),
                    summary=item.get("description") or "",
                    body=_repo_body(item),
                    tags=item.get("topics") or [],
                    metrics=metrics,
                    source_weight=source.weight,
                    panel=source.panel,
                    authority_tier=source.authority_tier,
                )
            )
        return articles

    def collect_huggingface(self, source: SourceConfig, days: int) -> list[Article]:
        query = quote_plus(source.query or "")
        url = f"https://huggingface.co/api/models?search={query}&sort=trendingScore&direction=-1&limit=80&full=true"
        headers = {}
        if token := os.getenv("HF_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        resp = self.client.get(url, headers=headers)
        resp.raise_for_status()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        articles: list[Article] = []
        for item in resp.json()[: self.options.per_source_limit]:
            model_id = item.get("modelId") or item.get("id")
            if not model_id:
                continue
            last_modified = parse_datetime(item.get("lastModified"))
            tags = item.get("tags") or []
            downloads = int(item.get("downloads") or 0)
            likes = int(item.get("likes") or 0)
            if not _is_notable_hf_model(
                model_id,
                tags,
                downloads,
                likes,
                last_modified,
                cutoff,
                require_recent=self.options.strict_week,
            ):
                continue
            metrics = {
                "downloads": downloads,
                "likes": likes,
                "last_modified": item.get("lastModified"),
                "pipeline_tag": item.get("pipeline_tag"),
                "library_name": item.get("library_name"),
            }
            articles.append(
                Article(
                    id=stable_id(f"https://huggingface.co/{model_id}"),
                    source_id=source.id,
                    source_name=source.name,
                    title=model_id,
                    url=f"https://huggingface.co/{model_id}",
                    published_at=last_modified,
                    summary=", ".join(tags[:8]),
                    body=", ".join(tags[:30]),
                    tags=tags,
                    metrics=metrics,
                    source_weight=source.weight,
                    panel=source.panel,
                    authority_tier=source.authority_tier,
                )
            )
        return articles

    def fetch_article_detail(self, url: str) -> tuple[str, list[str]]:
        try:
            resp = self.client.get(url)
            resp.raise_for_status()
        except Exception:
            return "", []
        soup = BeautifulSoup(resp.text, "html.parser")
        for node in soup.select("script, style, nav, footer, header, aside, form"):
            node.decompose()
        title = _meta_content(soup, "og:title") or ""
        description = _meta_content(soup, "og:description") or _meta_content(soup, "description") or ""
        image = _meta_content(soup, "og:image")
        main = soup.select_one("article") or soup.select_one("main") or soup.body
        paragraphs: list[str] = []
        if main:
            for node in main.select("h1, h2, h3, p, li, pre, code"):
                text = " ".join(node.get_text(" ", strip=True).split())
                if len(text) < 24:
                    continue
                if _looks_boilerplate(text):
                    continue
                paragraphs.append(text)
        body = "\n".join(dict.fromkeys([title, description, *paragraphs]))
        images = [image] if image else []
        if main:
            for img in main.select("img[src]"):
                src = urljoin(url, img.get("src", ""))
                if src.startswith("http"):
                    images.append(src)
        return body[:12000], list(dict.fromkeys(images))


def _xml_text(entry: ET.Element, tag: str) -> str:
    found = entry.find(tag)
    if found is None:
        found = entry.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
    if found is None:
        for child in entry.iter():
            if child.tag.rsplit("}", 1)[-1] == tag:
                found = child
                break
    return found.text if found is not None and found.text else ""


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"^(Product|Research|Company|Policy)\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4}\s+", "", title)
    if len(title) > 180:
        return f"{title[:177].rstrip()}..."
    return title


def _extract_images_from_xml(entry: ET.Element) -> list[str]:
    values = []
    for node in entry.iter():
        url = node.attrib.get("url") or node.attrib.get("href")
        if url and re.search(r"\.(png|jpe?g|webp)(\?|$)", url, re.I):
            values.append(url)
    text = ET.tostring(entry, encoding="unicode")
    values.extend(re.findall(r"https?://[^\"' <>\)]+?\.(?:png|jpe?g|webp)(?:\?[^\"' <>\)]*)?", text, re.I))
    return list(dict.fromkeys(values))


def _looks_ai_related(text: str) -> bool:
    return bool(
        re.search(
            r"\b(ai|artificial intelligence|llm|agent|gpt|claude|gemini|model|openai|anthropic|deepmind|hugging face)\b",
            text,
            re.I,
        )
    )


def _meta_content(soup: BeautifulSoup, key: str) -> str | None:
    if key == "description":
        node = soup.find("meta", attrs={"name": key})
    else:
        node = soup.find("meta", attrs={"property": key})
    if not node:
        return None
    value = node.get("content")
    return str(value).strip() if value else None


def _looks_boilerplate(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "subscribe",
            "cookie",
            "privacy policy",
            "terms of service",
            "sign up",
            "newsletter",
            "all rights reserved",
            "share this",
            "advertisement",
        )
    )


def _repo_body(item: dict[str, object]) -> str:
    parts = [
        str(item.get("full_name") or ""),
        str(item.get("description") or ""),
        f"language: {item.get('language')}" if item.get("language") else "",
        f"topics: {', '.join(item.get('topics') or [])}" if item.get("topics") else "",
        f"last pushed: {item.get('pushed_at')}" if item.get("pushed_at") else "",
    ]
    return "\n".join(part for part in parts if part)


def _is_notable_hf_model(
    model_id: str,
    tags: list[str],
    downloads: int,
    likes: int,
    last_modified: datetime | None,
    cutoff: datetime,
    require_recent: bool = True,
) -> bool:
    text = f"{model_id} {' '.join(tags)}".lower()
    if any(bad in text for bad in ("gpt2-small", "bert-base", "distilbert", "tiny-random")):
        return False
    has_model_signal = any(
        signal in text
        for signal in (
            "llm",
            "chat",
            "instruct",
            "reasoning",
            "agent",
            "tool",
            "multimodal",
            "text-generation",
            "image-text-to-text",
            "gguf",
            "safetensors",
            "transformers",
        )
    )
    has_scale_or_brand_signal = any(
        signal in text
        for signal in (
            "qwen",
            "llama",
            "mistral",
            "deepseek",
            "glm",
            "gemma",
            "phi",
            "kimi",
            "openai",
            "anthropic",
            "zai",
            "z-ai",
        )
    )
    is_recent = last_modified is not None and last_modified >= cutoff
    is_popular = downloads >= 50_000 or likes >= 100
    is_recent_and_noticed = is_recent and (downloads >= 3_000 or likes >= 25)
    if not require_recent:
        is_recent_and_noticed = downloads >= 3_000 or likes >= 25
    return has_model_signal and (has_scale_or_brand_signal or is_popular or is_recent_and_noticed)
