from __future__ import annotations

import hashlib
import json
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


# A date past the end of the issue's own window is always wrong — a scheduled post,
# a double-applied timezone, or a misread D/M. Publishing one undermines the letter,
# so the window is closed at both ends. The tolerance absorbs clock and timezone
# skew without letting a genuinely wrong date through.
_FUTURE_TOLERANCE = timedelta(days=1)


def _outside_window(published: datetime | None, cutoff: datetime) -> bool:
    if published is None:
        return False
    if published < cutoff:
        return True
    return published > datetime.now(timezone.utc) + _FUTURE_TOLERANCE


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
        if source.kind == "hnsearch":
            return self.collect_hnsearch(source, days)
        if source.kind == "hfpapers":
            return self.collect_hfpapers(source, days)
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
            if self.options.strict_week and _outside_window(published, cutoff):
                continue
            summary = strip_html(
                _xml_text(entry, "description")
                or _xml_text(entry, "summary")
                or _xml_text(entry, "content")
            )
            image_urls = _extract_images_from_xml(entry)
            body, detail_images, info_images, _ = (
                self.fetch_article_detail(link) if title and link else ("", [], [], None)
            )
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
                        info_image_urls=info_images[:3],
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
            raw_text = " ".join(link.get_text(" ", strip=True).split())
            title = _clean_title(raw_text)
            href = urljoin(source.url, link["href"])
            if len(title) < 12 or not href.startswith("http"):
                continue
            if not _looks_ai_related(title):
                continue
            body, image_urls, info_images, meta_published = self.fetch_article_detail(href)
            # What the article says about itself beats anything guessed from the
            # listing page around it.
            # Parse from the raw anchor text: _clean_title strips trailing dates.
            published = meta_published or parse_date_from_text(raw_text)
            if published is None:
                nearby = link.find_parent(["article", "li", "div"])
                if nearby is not None:
                    published = parse_date_from_text(nearby.get_text(" ", strip=True))
            if published is None and body:
                published = parse_date_from_text(body[:3000])
            if self.options.require_dates and published is None:
                continue
            if self.options.strict_week and _outside_window(published, cutoff):
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
                    info_image_urls=info_images[:3],
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

    def _arxiv_v1_published_at(self, arxiv_id: str) -> datetime | None:
        """arXiv 원문 API에서 v1 제출일을 가져온다.

        HF daily_papers의 publishedAt은 'HF 데일리 페이퍼에 오른 시점'이라 실제
        논문이 처음 공개된 날짜와 며칠씩 어긋날 수 있다 (외부 팩트체크에서 지적된
        메타데이터 오류 중 하나). arXiv API의 <published>는 v1 제출일을 가리키므로
        더 신뢰할 수 있는 소스다. 주간 후보가 2~3건 수준이라 논문별 호출을 허용한다.
        """
        base_id = re.sub(r"v\d+$", "", arxiv_id)
        try:
            resp = self.client.get(
                "https://export.arxiv.org/api/query", params={"id_list": base_id}
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entry = root.find("atom:entry", ns)
            if entry is None:
                return None
            published_el = entry.find("atom:published", ns)
            if published_el is None or not (published_el.text or "").strip():
                return None
            return parse_datetime(published_el.text)
        except Exception:
            return None

    def collect_hfpapers(self, source: SourceConfig, days: int) -> list[Article]:
        """Papers the ML community actually upvoted, linked to the original arXiv entry.

        The newsletter had no research source at all, so the research section spent
        months either empty or padded with whatever GitHub repo scored least badly.
        Ingesting arXiv wholesale is the opposite failure — roughly 600 cs.AI
        submissions a week, none of them ranked. Hugging Face's daily papers feed
        carries the community's upvote count, which is the same kind of signal HN
        points give news: a way to tell which of the week's papers people actually
        read. The upvote count rides along in `metrics` and feeds the heat index.
        """
        min_upvotes = 5
        api = "https://huggingface.co/api/daily_papers?limit=100"
        resp = self.client.get(api)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rows: list[tuple[int, Article]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            paper = item.get("paper")
            if not isinstance(paper, dict):
                continue
            arxiv_id = str(paper.get("id") or "").strip()
            title = unescape(str(paper.get("title") or "")).strip()
            abstract = unescape(str(paper.get("summary") or "")).strip()
            if not arxiv_id or not title:
                continue
            upvotes = int(paper.get("upvotes") or 0)
            if upvotes < min_upvotes:
                continue
            published = parse_datetime(item.get("publishedAt") or paper.get("publishedAt"))
            arxiv_published = self._arxiv_v1_published_at(arxiv_id)
            if arxiv_published:
                published = arxiv_published
            if self.options.strict_week and _outside_window(published, cutoff):
                continue
            if self.options.require_dates and not published:
                continue
            # Point at the paper itself — the abstract is the article, so there is
            # no page to scrape and nothing to lose to a paywall or JS challenge.
            url = f"https://arxiv.org/abs/{arxiv_id}"
            rows.append(
                (
                    upvotes,
                    Article(
                        id=stable_id(url),
                        source_id=source.id,
                        source_name=source.name,
                        title=title,
                        url=url,
                        published_at=published,
                        summary=abstract[:600],
                        body=abstract,
                        metrics={
                            "paper_upvotes": upvotes,
                            "paper_comments": int(item.get("numComments") or 0),
                            "stars": int(paper.get("githubStars") or 0),
                        },
                        source_weight=source.weight,
                        panel=source.panel,
                        authority_tier=source.authority_tier,
                    ),
                )
            )
        rows.sort(key=lambda row: row[0], reverse=True)
        return [article for _, article in rows[: self.options.per_source_limit]]

    def collect_hnsearch(self, source: SourceConfig, days: int) -> list[Article]:
        """Promote stories dominating Hacker News into publishable candidates.

        HN/Reddit are otherwise boost-only, so a story that overwhelms the feed
        (e.g. Kimi K3 at 2000+ points) had no publishable article to represent it
        and vanished. Query the HN Algolia API for the window's top stories by
        points, keep the AI-related ones, and pull each linked article in as a
        real candidate. HN points ride along in metrics as a strength signal.
        """
        min_points = 200
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        api = (
            "https://hn.algolia.com/api/v1/search"
            f"?tags=story&numericFilters=created_at_i>{since},points>{min_points}"
            f"&hitsPerPage={max(self.options.per_source_limit, 40)}"
        )
        resp = self.client.get(api)
        resp.raise_for_status()
        data = resp.json()
        articles: list[Article] = []
        for hit in data.get("hits", []):
            if len(articles) >= self.options.per_source_limit:
                break
            link = hit.get("url")
            title = unescape(hit.get("title") or "").strip()
            # Ask HN / text posts have no external URL; skip non-AI top stories.
            if not link or not title or not _hn_looks_ai(title):
                continue
            created = hit.get("created_at_i")
            published = datetime.fromtimestamp(created, timezone.utc) if created else None
            if self.options.strict_week and published and published < (
                datetime.now(timezone.utc) - timedelta(days=days)
            ):
                continue
            body, images, info_images, _ = self.fetch_article_detail(link)
            articles.append(
                Article(
                    id=stable_id(link),
                    source_id=source.id,
                    source_name=source.name,
                    title=title,
                    url=link.strip(),
                    published_at=published,
                    summary=(strip_html(body)[:600] if body else title),
                    body=body,
                    image_urls=images[:3],
                    info_image_urls=info_images[:3],
                    metrics={
                        "hn_points": int(hit.get("points") or 0),
                        "hn_comments": int(hit.get("num_comments") or 0),
                    },
                    source_weight=source.weight,
                    panel=source.panel,
                    authority_tier=source.authority_tier,
                )
            )
        return articles

    def fetch_article_detail(self, url: str) -> tuple[str, list[str], list[str], datetime | None]:
        try:
            resp = self.client.get(url)
            resp.raise_for_status()
        except Exception:
            return "", [], [], None
        soup = BeautifulSoup(resp.text, "html.parser")
        # Read the date before the <script> tags are stripped — JSON-LD lives there,
        # and it is the only place most blogs state the date unambiguously. Scanning
        # page text with a regex instead made a Claude blog post dated Aug 07 arrive
        # as Aug 14: the first date-shaped string on a listing page belongs to
        # whatever the layout happened to put first, not to this article.
        published = _published_from_metadata(soup)
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
        # Benchmark tables carry the numbers a "vs. previous model" article is
        # actually about (x.ai's Grok launch posts, model system cards, ...), but
        # they usually sit well past the 12k-char body cap. Append them separately
        # so they survive the cap instead of being lost to whatever came first.
        body = body[:12000]
        tables = _extract_tables(soup)
        if tables:
            body += "\n\n[표]\n" + "\n\n[표]\n".join(tables)
        body_imgs: list = []
        if main:
            for img in main.select("img[src]"):
                resolved = urljoin(url, img.get("src", ""))
                if resolved.startswith("http"):
                    # Mutated in place so _find_info_images can read a resolved URL
                    # back off the tag alongside its alt/width — passing the tag
                    # itself (not a bare string) is what lets it match on alt text.
                    img["src"] = resolved
                    body_imgs.append(img)
        # 대표 이미지는 og:image 우선(소셜 카드용으로 고른 브랜드 히어로가 보기
        # 좋다), 그다음 본문 이미지를 등장 순서 그대로 — 점수로 재정렬하면 기자
        # 프로필 사진 같은 본문 첫 이미지가 히어로를 밀어내는 부작용이 생긴다
        # (MarkTechPost 기사에서 실제로 발생). 정보성 이미지(차트/표)는 대표
        # 이미지 자리를 놓고 경쟁시키지 않고 info_image_urls로 별도 수집한다.
        ordered_images = ([image] if image else []) + [
            img.get("src", "") for img in body_imgs if img.get("src")
        ]
        images = list(dict.fromkeys(ordered_images))
        info_images = _find_info_images(body_imgs)
        return body, images, info_images, published


# Words that mark an image as data-bearing rather than decorative — a chart,
# table screenshot, or benchmark comparison, as opposed to a hero photo or logo.
_BENCH_IMAGE_RE = re.compile(
    r"bench|chart|table|graph|compar|score|eval|result|metric", re.I
)


def _extract_tables(soup: BeautifulSoup) -> list[str]:
    """Serialize benchmark tables so their numbers survive the body-length cap.

    Real <table> markup is tried first; the Tailwind `tabular-nums` div-grid
    fallback only runs when no <table> was found, since a page that has both
    would otherwise duplicate the same numbers twice.
    """
    tables = _extract_html_tables(soup)
    if tables:
        return tables
    return _extract_grid_tables(soup)


def _extract_html_tables(soup: BeautifulSoup) -> list[str]:
    serialized: list[str] = []
    for table in soup.find_all("table"):
        if len(serialized) >= 3:
            break
        rows: list[str] = []
        caption = table.find("caption")
        if caption:
            caption_text = " ".join(caption.get_text(" ", strip=True).split())
            if caption_text:
                rows.append(caption_text)
        for tr in table.find_all("tr"):
            cells = [
                " ".join(cell.get_text(" ", strip=True).split())
                for cell in tr.find_all(["th", "td"])
            ]
            cells = [cell for cell in cells if cell]
            if cells:
                rows.append(" | ".join(cells))
        text = "\n".join(rows).strip()
        if text:
            serialized.append(text[:1200])
    return serialized


# Below this density (chars of visible text per tabular-nums cell), a container
# is a genuine data grid — mostly short labels and numbers. Above it, the
# container is something wide like the whole article body that merely happens
# to contain 8+ numeric cells somewhere inside it.
_GRID_DENSITY_THRESHOLD = 80


def _extract_grid_tables(soup: BeautifulSoup) -> list[str]:
    """Fallback for benchmark tables built as Tailwind `tabular-nums` div grids
    instead of <table> markup (verified against x.ai's Grok launch posts).

    Walk up from every `tabular-nums` cell to the innermost ancestor that holds
    8+ such cells (a plausible table), then keep only ancestors whose text
    density says they are actually a compact grid and not, say, the whole
    article wrapped around a couple of numbers.
    """
    cells = soup.select('[class*="tabular-nums"]')
    if len(cells) < 8:
        return []
    candidates: dict[int, object] = {}
    for cell in cells:
        node = cell
        best = None
        while node is not None and getattr(node, "name", None):
            if len(node.select('[class*="tabular-nums"]')) >= 8:
                best = node
                break
            node = node.parent
        if best is not None:
            candidates[id(best)] = best
    accepted = []
    for node in candidates.values():
        cell_count = len(node.select('[class*="tabular-nums"]'))
        density = len(node.get_text(" | ", strip=True)) / cell_count
        if density < _GRID_DENSITY_THRESHOLD:
            accepted.append(node)
    # Drop any accepted ancestor that is itself nested inside another accepted
    # ancestor — otherwise the same grid is emitted once per nesting level.
    filtered = []
    for node in accepted:
        ancestor_ids = {id(parent) for parent in node.parents}
        if any(id(other) in ancestor_ids for other in accepted if other is not node):
            continue
        filtered.append(node)
    serialized: list[str] = []
    for node in filtered:
        if len(serialized) >= 2:
            break
        text = node.get_text(" | ", strip=True)
        if text:
            serialized.append(text[:1500])
    return serialized


# Filenames/alt text that mark an image as decorative rather than data-bearing —
# a byline photo, a site logo, a status badge — even when it also happens to
# match _BENCH_IMAGE_RE (e.g. "compare" in a favicon's marketing alt text).
_DECORATIVE_IMAGE_RE = re.compile(
    r"avatar|author|profile|headshot|logo|icon|badge|favicon|banner", re.I
)


def _has_small_dimension(img) -> bool:
    """True if a declared width/height/data-origin-width says this is an icon
    (< 300px). Missing attributes are not evidence either way, so they pass."""
    for attr in ("width", "height", "data-origin-width"):
        value = img.get(attr)
        if not value:
            continue
        digits = re.sub(r"[^\d]", "", str(value))
        if digits and int(digits) < 300:
            return True
    return False


def _find_info_images(body_img_tags: list) -> list[str]:
    """Collect information-bearing body images (charts, tables, benchmark
    comparisons) separately from the representative (og:image) hero.

    These used to compete with og:image for the single "representative image"
    slot, which let a reporter's profile photo (matched as a body <img>) beat
    a brand hero (see collectors.py history / MarkTechPost regression). Instead
    they are collected here and rendered inline in the article body, so a good
    hero and a useful chart can both survive.
    """
    results: list[str] = []
    for img in body_img_tags:
        src = img.get("src", "")
        if not src:
            continue
        alt = img.get("alt") or ""
        haystack = f"{src} {alt}"
        if not _BENCH_IMAGE_RE.search(haystack):
            continue
        if _DECORATIVE_IMAGE_RE.search(haystack):
            continue
        if _has_small_dimension(img):
            continue
        results.append(src)
    return list(dict.fromkeys(results))[:3]


def _published_from_metadata(soup: BeautifulSoup) -> datetime | None:
    """The date the page states about itself, in decreasing order of reliability."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for block in data if isinstance(data, list) else [data]:
            if not isinstance(block, dict):
                continue
            for key in ("datePublished", "dateCreated", "uploadDate"):
                parsed = parse_datetime(block.get(key)) or parse_date_from_text(
                    str(block.get(key) or "")
                )
                if parsed:
                    return parsed
    for attrs in (
        {"property": "article:published_time"},
        {"name": "article:published_time"},
        {"itemprop": "datePublished"},
        {"name": "date"},
        {"name": "pubdate"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            parsed = parse_datetime(tag["content"]) or parse_date_from_text(tag["content"])
            if parsed:
                return parsed
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        parsed = parse_datetime(time_tag["datetime"])
        if parsed:
            return parsed
    return None


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


# Listing-page anchor text often glues the article title to its metadata line,
# e.g. "… to put it to work Aug 11th 2026 9:00am, by Frederic Lardinois".
_TRAILING_BYLINE_RE = re.compile(r",\s*by\s+[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*){0,3}$")
_TRAILING_DATE_RE = re.compile(
    r"\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
    r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"(?:\s+\d{1,2}:\d{2}\s*[AaPp][Mm])?$"
)


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"^(Product|Research|Company|Policy)\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4}\s+", "", title)
    title = _TRAILING_BYLINE_RE.sub("", title)
    title = _TRAILING_DATE_RE.sub("", title).rstrip()
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


# HN top-story titles often omit generic AI words (e.g. "Kimi K3", "Qwen 3.8",
# "Grok Build"), so also match known model/lab names to keep dominant stories.
_HN_AI_RE = re.compile(
    r"\b(ai|artificial intelligence|llm|agent|gpt|chatgpt|claude|gemini|gemma|model|models|"
    r"openai|anthropic|deepmind|hugging ?face|kimi|moonshot|qwen|grok|llama|mistral|deepseek|"
    r"glm|nemotron|phi|open[- ]?weight|multimodal|inference)\b",
    re.I,
)


def _hn_looks_ai(title: str) -> bool:
    return bool(_HN_AI_RE.search(title))


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
