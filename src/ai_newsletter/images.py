from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .models import RankedArticle


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
        # Absolute/relative path candidates (contain a separator or an .exe suffix).
        if os.sep in candidate or "/" in candidate or candidate.lower().endswith(".exe"):
            if Path(candidate).exists():
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _capture_with_chrome(chrome: str, url: str, target: Path) -> bool:
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
        "--virtual-time-budget=10000",
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
