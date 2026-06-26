from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .models import RankedArticle


CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
]


def capture_article_images(articles: list[RankedArticle], output_dir: Path) -> None:
    image_dir = output_dir / "assets" / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    chrome = _find_chrome()
    if not chrome:
        chrome = ""
    for idx, article in enumerate(articles, 1):
        if _download_primary_image(article, idx, image_dir):
            continue
        if not chrome:
            continue
        target = image_dir / f"article_{idx:02d}_{article.id}.png"
        if _capture_with_chrome(chrome, article.url, target):
            article.local_image = f"assets/images/{target.name}"


def _find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if "/" in candidate and Path(candidate).exists():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _capture_with_chrome(chrome: str, url: str, target: Path) -> bool:
    cmd = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--window-size=1280,820",
        f"--screenshot={str(target)}",
        url,
    ]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, timeout=45)
    except Exception:
        return False
    return result.returncode == 0 and target.exists() and target.stat().st_size > 2048


def _download_primary_image(article: RankedArticle, idx: int, image_dir: Path) -> bool:
    for image_url in article.image_urls:
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
            return True
    return False


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
