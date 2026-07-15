from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import httpx
from openai import OpenAI

from .editorial_selection import _heuristic_topic_key
from .models import Article, RankedArticle

# A screenshot smaller than this is treated as blank / a bot-challenge page
# (e.g. Cloudflare's "just a moment" wall renders almost all-white and compresses
# to a tiny PNG). Real content screenshots are far larger.
_MIN_MEANINGFUL_SHOT_BYTES = 25000


def _windows_browser_candidates() -> list[str]:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    roots = [program_files, program_files_x86]
    candidates = []
    for root in roots:
        candidates.append(str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"))
        candidates.append(str(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    if local_app_data:
        candidates.append(str(Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    return candidates


CHROME_CANDIDATES = [
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    # Windows (Chrome / Edge)
    *_windows_browser_candidates(),
    # Linux / PATH lookups
    "google-chrome",
    "chromium",
    "chromium-browser",
    "chrome",
    "msedge",
]


def capture_article_images(
    articles: list[RankedArticle],
    output_dir: Path,
    donors: list[Article] | None = None,
    max_workers: int = 4,
    generate_images: bool = True,
    max_generated: int = 6,
) -> None:
    image_dir = output_dir / "assets" / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    chrome = _find_chrome() or ""
    donor_pool = donors or []
    if not articles:
        return

    used_urls: set[str] = set()
    lock = threading.Lock()

    def _remember(url: str) -> None:
        with lock:
            used_urls.add(url)

    # Phase 1 (parallel): each article's OWN image. og:image download, else a
    # screenshot of its own page (rejected if blank / bot-challenge). Independent
    # per article, so it is safe to run concurrently.
    def _own(item: tuple[int, RankedArticle]) -> None:
        idx, article = item
        url = _download_image_urls(article.image_urls, idx, article, image_dir)
        if url:
            _remember(url)
            return
        if chrome:
            _screenshot_for(chrome, article.url, idx, article, image_dir)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_own, enumerate(articles, 1)))

    # Phase 2 (sequential): fill the rest. Generation is preferred over borrowing
    # because a borrowed image is often a generic newsletter card that is off-topic
    # or repeated across articles; a generated illustration is on-topic, on-brand
    # (the company's logo), and unique. Borrowing stays as a fallback when
    # generation is disabled/capped/fails, and never reuses an image already taken.
    #   3) generate an on-topic branded illustration
    #   4) borrow a fresh (unused) image from the most-related coverage
    #   5) branded placeholder
    generated = 0
    can_generate = generate_images and bool(os.getenv("OPENAI_API_KEY"))
    for idx, article in enumerate(articles, 1):
        if article.local_image:
            continue
        if can_generate and generated < max_generated and _generate_image(article, idx, image_dir):
            generated += 1
            continue
        donor = _best_donor(article, donor_pool)
        if donor is not None:
            fresh = [u for u in donor.image_urls if u not in used_urls]
            url = _download_image_urls(fresh, idx, article, image_dir)
            if url:
                used_urls.add(url)
                article.image_credit = donor.source_name
                continue
        if chrome:
            _render_placeholder(chrome, article, idx, image_dir)


def _find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        # Absolute/relative path candidates (contain a separator or an .exe suffix).
        if os.sep in candidate or "/" in candidate or candidate.lower().endswith(".exe"):
            if Path(candidate).exists():
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _capture_with_chrome(chrome: str, url: str, target: Path, virtual_time_ms: int = 10000) -> bool:
    # Chrome's --screenshot flag only reliably writes to an ABSOLUTE path; a
    # relative path is resolved against Chrome's own working directory and fails
    # with "path not found" (this silently broke all captures on Windows).
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    # A unique user-data-dir keeps the capture working even when a normal
    # Chrome/Edge window is already open (otherwise the profile can be locked).
    profile_dir = tempfile.mkdtemp(prefix="ai_news_shot_")
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        f"--user-data-dir={profile_dir}",
        "--window-size=1280,820",
        # Give JS-rendered (SPA) pages time to paint before the screenshot is
        # taken, otherwise sites like openai.com capture as a blank white page.
        # Static local HTML (placeholders) can use a much smaller budget.
        f"--virtual-time-budget={virtual_time_ms}",
        f"--screenshot={str(target)}",
        url,
    ]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, timeout=60)
    except Exception:
        return False
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    return result.returncode == 0 and target.exists() and target.stat().st_size > 2048


def _download_image_urls(
    image_urls: list[str], idx: int, article: RankedArticle, image_dir: Path
) -> str:
    """Download the first usable image; returns the URL used (falsy "" on failure)."""
    for image_url in image_urls:
        if not _looks_like_content_image(image_url):
            continue
        suffix = _image_suffix(image_url)
        target = image_dir / f"article_{idx:02d}_{article.id}{suffix}"
        try:
            with httpx.Client(follow_redirects=True, timeout=20.0) as client:
                response = client.get(image_url, headers={"User-Agent": "ai-newsletter-automation/0.1"})
                response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "image" not in content_type and suffix == ".png":
                continue
            target.write_bytes(response.content)
        except Exception:
            continue
        if target.exists() and target.stat().st_size > 4096:
            article.local_image = f"assets/images/{target.name}"
            return image_url
    return ""


def _screenshot_for(
    chrome: str, url: str, idx: int, article: RankedArticle, image_dir: Path
) -> bool:
    target = (image_dir / f"article_{idx:02d}_{article.id}.png").resolve()
    if not _capture_with_chrome(chrome, url, target):
        return False
    if target.exists() and target.stat().st_size >= _MIN_MEANINGFUL_SHOT_BYTES:
        article.local_image = f"assets/images/{target.name}"
        return True
    # Drop blank/challenge captures so they are not referenced as an "image".
    try:
        target.unlink()
    except OSError:
        pass
    return False


# Company -> logo, for an on-brand generated illustration (internal newsletter,
# no trademark concern). Matched against title/summary/source.
_COMPANY_PATTERNS: list[tuple[str, str]] = [
    ("OpenAI", r"\bopenai\b|\bgpt[-\s]?\d|chatgpt|\bcodex\b|\bsora\b"),
    ("Anthropic", r"\banthropic\b|\bclaude\b|\bfable\b"),
    ("Google", r"\bgoogle\b|deepmind|gemini|gemma|nano\s*banana"),
    ("Meta", r"\bmeta\b|\bllama\b"),
    ("NVIDIA", r"\bnvidia\b|nemotron"),
    ("Mistral AI", r"\bmistral\b"),
    ("Microsoft", r"\bmicrosoft\b|copilot|\bazure\b"),
    ("Hugging Face", r"hugging\s*face|huggingface"),
]


def _company_of(article: RankedArticle) -> str | None:
    text = f"{article.title} {article.summary} {article.source_id}".lower()
    for name, pattern in _COMPANY_PATTERNS:
        if re.search(pattern, text):
            return name
    return None


def _generate_image(article: RankedArticle, idx: int, image_dir: Path) -> bool:
    """Generate an on-topic editorial illustration when no real image is available.

    Uses the article's title/summary/category and, when a company is recognized,
    asks for that company's logo (internal newsletter, so this is intentional).
    In-image headline/caption text is forbidden because generated text comes out
    garbled — the headline is shown separately in HTML. Credited as AI 생성.
    """
    title = article.korean_title or article.title
    summary = (article.korean_summary or article.summary or "")[:300]
    category = article.category or ""
    company = _company_of(article)
    logo_line = (
        f"{company}의 공식 로고 심볼을 화면에 또렷하고 눈에 띄게 배치하고, " if company else ""
    )
    prompt = (
        "AI 주간 뉴스레터용 에디토리얼 일러스트레이션. "
        f"주제: {title}. 맥락: {summary}. 카테고리: {category}. "
        f"{logo_line}"
        "칩·성장 그래프 등 주제와 관련된 기술 상징을 곁들여. "
        "깔끔하고 프로페셔널한 현대적 톤, 차분한 색조, 내부 뉴스레터용. "
        "중요: 제목·헤드라인·문장·설명·캡션 같은 텍스트나 숫자는 이미지 안에 절대 그리지 말 것. "
        "로고 심볼만 허용."
    )
    try:
        client = OpenAI()
        response = client.images.generate(
            model="gpt-image-1", prompt=prompt, size="1536x1024", quality="low", n=1
        )
        data = base64.b64decode(response.data[0].b64_json)
    except Exception:
        return False
    target = image_dir / f"article_{idx:02d}_{article.id}.png"
    try:
        target.write_bytes(data)
    except OSError:
        return False
    if target.exists() and target.stat().st_size > 4096:
        article.local_image = f"assets/images/{target.name}"
        article.image_credit = "AI 생성"
        return True
    return False


def _render_placeholder(chrome: str, article: RankedArticle, idx: int, image_dir: Path) -> bool:
    """Rasterize a branded card so donor-less articles still yield a board PNG."""
    source = escape(article.source_name or "AI Weekly")
    title = escape((article.korean_title or article.title or "")[:80])
    html = (
        "<!doctype html><html><head><meta charset='utf-8'></head>"
        "<body style='margin:0'>"
        "<div style='width:1280px;height:720px;box-sizing:border-box;display:flex;"
        "flex-direction:column;justify-content:center;padding:88px;color:#e6edf3;"
        "font-family:\"Malgun Gothic\",\"Segoe UI\",sans-serif;"
        "background:linear-gradient(135deg,#0f766e,#0b3b57);'>"
        f"<div style='font-size:30px;font-weight:800;letter-spacing:.04em;opacity:.85'>{source}</div>"
        f"<div style='font-size:52px;font-weight:800;line-height:1.25;margin-top:26px'>{title}</div>"
        "<div style='margin-top:auto;font-size:22px;opacity:.7'>AI Weekly Brief</div>"
        "</div></body></html>"
    )
    fd, path = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(html)
        target = (image_dir / f"article_{idx:02d}_{article.id}.png").resolve()
        if _capture_with_chrome(chrome, Path(path).resolve().as_uri(), target, virtual_time_ms=500):
            if target.exists() and target.stat().st_size > 4096:
                article.local_image = f"assets/images/{target.name}"
                return True
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return False


def _best_donor(article: RankedArticle, donors: list[Article]) -> Article | None:
    """The related-coverage article (same model/product) that has a real image."""
    key = _heuristic_topic_key(article)
    if key == article.id:  # no recognizable entity -> can't match related coverage safely
        return None
    best: Article | None = None
    for donor in donors:
        if donor.id == article.id or not donor.image_urls:
            continue
        if _heuristic_topic_key(donor) != key:
            continue
        if best is None or (donor.source_weight, len(donor.image_urls)) > (
            best.source_weight,
            len(best.image_urls),
        ):
            best = donor
    return best


def _looks_like_content_image(url: str) -> bool:
    lowered = url.lower()
    if not lowered.startswith("http"):
        return False
    if any(skip in lowered for skip in ("avatar", "logo", "icon", "tracking", "pixel", "spinner")):
        return False
    return True


def _image_suffix(url: str) -> str:
    path = urlparse(url).path.lower()
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        if path.endswith(suffix):
            return suffix
    return ".png"
