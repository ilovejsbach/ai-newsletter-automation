from __future__ import annotations

import json
import mimetypes
import shutil
import base64
import subprocess
import csv
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from .models import Article, NewsletterPackage, RankedArticle
from .models import Issue
from .images import capture_article_images
from .sections import SECTION_META, SECTION_ORDER


def _is_sectioned(articles: list[RankedArticle]) -> bool:
    """True when the package was built by the sectioned selection mode."""
    return any(a.section in SECTION_META for a in articles)


def make_output_dir(base_dir: Path, period_end: datetime) -> Path:
    """Create a fresh output directory. Same-date reruns are kept side by side
    (_v2, _v3, ...) instead of overwriting the earlier run."""
    slug = period_end.astimezone(timezone.utc).strftime("%Y-%m-%d_weekly_ai_newsletter")
    output_dir = base_dir / slug
    version = 2
    while output_dir.exists() or output_dir.with_suffix(".zip").exists():
        output_dir = base_dir / f"{slug}_v{version}"
        version += 1
    (output_dir / "assets" / "images").mkdir(parents=True, exist_ok=True)
    (output_dir / "data").mkdir(parents=True, exist_ok=True)
    return output_dir


def write_package(
    output_dir: Path,
    period_start: datetime,
    period_end: datetime,
    candidates: list[Article],
    selected: list[RankedArticle],
    quality_report: dict[str, object],
    issues: list[Issue] | None = None,
    overview: str = "",
    capture: bool = True,
    theme: str = "editorial",
    thumbnails: bool = True,
) -> NewsletterPackage:
    package = NewsletterPackage(
        period_start=period_start,
        period_end=period_end,
        title=f"AI 주간 뉴스레터 | {period_start:%Y.%m.%d} - {period_end:%Y.%m.%d}",
        overview=overview,
        thumbnails=thumbnails,
        articles=selected,
        issues=issues or [],
        quality_report=quality_report,
        output_dir=output_dir,
    )
    article_dir = output_dir / "articles"
    article_dir.mkdir(parents=True, exist_ok=True)
    capture_article_images(selected, output_dir, donors=candidates)
    for idx, article in enumerate(selected, 1):
        (article_dir / article_filename(idx, article)).write_text(
            render_article_html(package, idx, article),
            encoding="utf-8",
        )
    (output_dir / "newsletter.html").write_text(render_html(package), encoding="utf-8")
    (output_dir / "assets" / "style.css").write_text(render_css(theme), encoding="utf-8")
    write_board_exports(package, capture=capture)
    if capture:
        write_publish_ready_package(package)
    (output_dir / "data" / "crawled_articles.json").write_text(
        json.dumps([a.model_dump(mode="json") for a in candidates], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "data" / "selected_articles.json").write_text(
        json.dumps([a.model_dump(mode="json") for a in selected], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "data" / "generation_report.json").write_text(
        json.dumps(quality_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _finalize_package(package)
    return package


def _finalize_package(package: NewsletterPackage) -> None:
    """Refresh the manifest file list and the distribution zip."""
    output_dir = package.output_dir
    manifest = {
        "title": package.title,
        "entrypoint": "newsletter.html",
        "created_at": package.generated_at.isoformat(),
        "files": sorted(str(path.relative_to(output_dir)) for path in output_dir.rglob("*") if path.is_file()),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    archive_path = shutil.make_archive(str(output_dir), "zip", output_dir)
    Path(archive_path).replace(output_dir.with_suffix(".zip"))


def capture_package(package: NewsletterPackage) -> None:
    """PNG-only stage: capture PNGs from the already-rendered HTML and build
    the publish package, then refresh manifest/zip. HTML is not regenerated."""
    write_board_image_exports(package)
    write_publish_ready_package(package)
    _finalize_package(package)


def write_publish_ready_package(package: NewsletterPackage) -> None:
    date_key = package.period_end.strftime("%Y%m%d")
    date_slug = package.period_end.strftime("%Y-%m-%d")
    # Same-date reruns (…_v2) get their own publish folder so packages never
    # overwrite each other. File names INSIDE the package stay contract-fixed
    # (ai_weekly_YYYYMMDD_*.png) for the UiPath automation.
    base_name = package.output_dir.name
    date_prefix = f"{date_slug}_weekly_ai_newsletter"
    variant = base_name[len(date_prefix):] if base_name.startswith(date_prefix) else ""
    publish_root = package.output_dir.parent / "publish_ready" / f"{date_slug}_ai_weekly{variant}"
    package_dir = publish_root / "transfer_package"
    images_dir = package_dir / "images"
    publish_dir = package_dir / "publish"
    for directory in (images_dir, publish_dir):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)

    main_image = images_dir / f"ai_weekly_{date_key}_main_00.png"
    clean_main_html = package.output_dir / "_publish_ready_main_00.html"
    clean_html = re.sub(r'<a class="detail-link"[^>]*>.*?</a>', "", render_html(package), flags=re.S)
    clean_main_html.write_text(clean_html, encoding="utf-8")
    capture_html_as_png(clean_main_html, main_image)
    clean_main_html.unlink(missing_ok=True)

    slots: list[tuple[str, str, str]] = [("MAIN_00", main_image.name, "메인 요약")]
    for idx, article in enumerate(package.articles[:10], 1):
        source = package.output_dir / "board" / "image_post" / "images" / f"article_{idx:02d}.png"
        filename = f"ai_weekly_{date_key}_article_{idx:02d}.png"
        if source.exists():
            shutil.copy2(source, images_dir / filename)
        slots.append((f"ARTICLE_{idx:02d}", filename, article.korean_title or article.title))

    (publish_dir / "board_post_template.html").write_text(
        render_publish_board_html(slots, use_placeholders=True),
        encoding="utf-8",
    )
    (publish_dir / "board_post_local_preview.html").write_text(
        render_publish_board_html(slots, use_placeholders=False),
        encoding="utf-8",
    )
    with (publish_dir / "image_url_map.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["slot", "filename", "placeholder", "uploaded_url", "title"])
        for slot, filename, title in slots:
            writer.writerow([slot, filename, _placeholder(slot), "", title])

    manifest = {
        "package_version": "1.0",
        "created_at": package.generated_at.isoformat(),
        "contract": {
            "main_count": 1,
            "article_count": 10,
            "image_count": 11,
            "html_template": "publish/board_post_template.html",
            "local_preview": "publish/board_post_local_preview.html",
            "mapping_csv": "publish/image_url_map.csv",
            "required_files": [filename for _, filename, _ in slots],
        },
        "slots": [
            {
                "slot": slot,
                "filename": filename,
                "placeholder": _placeholder(slot),
                "title": title,
            }
            for slot, filename, title in slots
        ],
    }
    (publish_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (package_dir / "README_UIPATH.md").write_text(render_publish_readme(date_key), encoding="utf-8")

    zip_path = publish_root / f"ai_weekly_{date_key}_publish_package.zip"
    zip_path.unlink(missing_ok=True)
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=publish_root, base_dir="transfer_package")


def _placeholder(slot: str) -> str:
    return "{{IMG_" + slot + "_URL}}"


def render_publish_board_html(slots: list[tuple[str, str, str]], use_placeholders: bool) -> str:
    n = len(slots)
    checked_tab_css = "\n  ".join(
        f'#slide{i}:checked ~ .tab-bar label[for="slide{i}"],' for i in range(1, n + 1)
    ).rstrip(",") + " { background:#0f766e; color:#fff; border-color:#0f766e; }"
    checked_img_css = "\n  ".join(
        f"#slide{i}:checked ~ .slides img:nth-child({i})," for i in range(1, n + 1)
    ).rstrip(",") + " { display:block; }"
    checked_page_css = "\n  ".join(
        f"#slide{i}:checked ~ .page-indicator span:nth-child({i})," for i in range(1, n + 1)
    ).rstrip(",") + " { display:inline; }"
    radio_inputs = "\n  ".join(
        f'<input type="radio" name="ai_weekly_card" id="slide{i}"{" checked" if i == 1 else ""}>'
        for i in range(1, n + 1)
    )
    labels = ["메인", *[f"{i:02d}" for i in range(1, n)]]
    tab_labels = "\n    ".join(
        f'<label for="slide{i}">{escape(label)}</label>' for i, label in enumerate(labels, 1)
    )
    slide_imgs = []
    for slot, filename, title in slots:
        src = _placeholder(slot) if use_placeholders else f"../images/{filename}"
        slide_imgs.append(f'<img src="{escape(src)}" alt="{escape(title)}">')
    page_spans = "\n    ".join(
        f"<span>{escape(label)} / {n}</span>" for label in labels
    )
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 주간 뉴스레터</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ background:#fff; }}
  .cardnews-wrap {{ width:100%; max-width:960px; margin:0 auto; padding:0; background:#fff; font-family:'NanumGothic','Malgun Gothic',Arial,sans-serif; }}
  .cardnews-title {{ text-align:center; font-size:18px; font-weight:bold; color:#0f766e; padding:8px 0; background:#fff; }}
  .cardnews-wrap input[type="radio"] {{ display:none; }}
  .tab-bar {{ position:sticky; top:0; z-index:10; display:flex; flex-wrap:wrap; justify-content:center; gap:4px; padding:6px 4px; background:#fff; border-top:1px solid #e5e7eb; border-bottom:1px solid #e5e7eb; }}
  .tab-bar label {{ display:inline-block; min-width:42px; height:32px; line-height:32px; padding:0 8px; text-align:center; font-size:13px; font-weight:bold; color:#444; background:#e8edf2; border:1px solid #d5dde5; border-radius:4px; cursor:pointer; }}
  .slides {{ width:100%; height:auto; min-height:0; margin:0; padding:0; background:#fff; line-height:0; overflow:visible; }}
  .slides img {{ display:none; width:100%; max-width:100%; height:auto; margin:0; padding:0; border:0; background:#fff; vertical-align:top; }}
  .page-indicator {{ text-align:center; padding:6px 0 10px; background:#fff; font-size:13px; line-height:1.4; color:#777; }}
  .page-indicator span {{ display:none; }}
  {checked_tab_css}
  {checked_img_css}
  {checked_page_css}
</style>
</head>
<body>
<div class="cardnews-wrap">
  <div class="cardnews-title">AI 주간 뉴스레터</div>
  {radio_inputs}
  <div class="tab-bar">
    {tab_labels}
  </div>
  <div class="slides">
    {"".join(slide_imgs)}
  </div>
  <div class="page-indicator">
    {page_spans}
  </div>
</div>
</body>
</html>
"""


def render_publish_readme(date_key: str) -> str:
    return f"""# AI 주간 뉴스레터 게시 자동화 패키지

## 고정 산출물 계약

- 메인 이미지: 1개, `images/ai_weekly_{date_key}_main_00.png`
- 상세 아티클 이미지: 10개, `images/ai_weekly_{date_key}_article_01.png` ~ `article_10.png`
- 게시용 HTML 템플릿: `publish/board_post_template.html`
- 로컬 미리보기 HTML: `publish/board_post_local_preview.html`
- 이미지 URL 매핑표: `publish/image_url_map.csv`

## 내부망 UiPath 절차

1. zip을 약속된 폴더에 압축 해제한다.
2. `images/` 폴더의 PNG 11개를 게시판에 업로드한다.
3. 게시판이 반환한 이미지 URL을 `publish/image_url_map.csv`의 `uploaded_url`에 채운다.
4. `publish/board_post_template.html`에서 placeholder를 실제 URL로 치환한다.
5. 치환된 HTML을 나모웹에디터 HTML 소스 모드에 붙여넣고 게시한다.

## 치환 예시

- `{{{{IMG_MAIN_00_URL}}}}` -> 게시판 업로드 후 받은 메인 이미지 URL
- `{{{{IMG_ARTICLE_01_URL}}}}` -> 게시판 업로드 후 받은 1번 아티클 이미지 URL

## 주의

- 파일명과 이미지 개수는 자동화 계약이므로 변경하지 않는다.
- 최종 이미지는 게시판용이므로 `상세 아티클 보기` 같은 링크 문구를 제거한 메인 이미지를 사용한다.
- HTML은 JavaScript 없이 radio input + CSS 방식으로 동작한다.
"""


def write_board_exports(package: NewsletterPackage, capture: bool = True) -> None:
    board_dir = package.output_dir / "board"
    board_images = board_dir / "images"
    board_images.mkdir(parents=True, exist_ok=True)
    for image in (package.output_dir / "assets" / "images").glob("*"):
        if image.is_file():
            shutil.copy2(image, board_images / image.name)

    css = render_board_css()
    variants = {
        "board_post_inline_css_file_images.html": render_board_post(package, css=css, image_mode="file"),
        "board_post_inline_css_base64_images.html": render_board_post(package, css=css, image_mode="base64"),
        "board_post_simple.html": render_board_post(package, css=render_simple_board_css(), image_mode="none", simple=True),
        "board_body_fragment.html": render_board_body(package, image_mode="file", simple=False),
        "board_body_fragment_base64.html": render_board_body(package, image_mode="base64", simple=False),
        "namo_inline_styles_base64.html": render_namo_inline(package, image_mode="base64"),
        "namo_inline_styles_no_images.html": render_namo_inline(package, image_mode="none"),
    }
    for filename, html in variants.items():
        (board_dir / filename).write_text(html, encoding="utf-8")

    if capture:
        write_board_image_exports(package)

    manifest = {
        "purpose": "Namo WebEditor/internal board compatibility test outputs",
        "recommended_test_order": [
            "image_post/board_image_post.html",
            "image_post/board_image_post_with_link_placeholders.html",
            "namo_inline_styles_no_images.html",
            "namo_inline_styles_base64.html",
            "board_post_inline_css_file_images.html",
            "board_post_inline_css_base64_images.html",
            "board_post_simple.html",
            "board_body_fragment.html",
            "board_body_fragment_base64.html",
        ],
        "files": sorted(str(path.relative_to(board_dir)) for path in board_dir.rglob("*") if path.is_file()),
        "notes": {
            "file_images": "이미지를 board/images 폴더 상대경로로 참조합니다. 게시판 첨부 이미지 URL 치환 테스트에 적합합니다.",
            "base64_images": "이미지를 HTML 내부 data URI로 포함합니다. 게시판이 data URI를 차단할 수 있습니다.",
            "simple": "CSS를 최소화한 1열 단순형입니다. 에디터가 CSS를 많이 제거할 때 fallback입니다.",
            "fragment": "html/head/body 없이 본문 조각만 있습니다. 에디터 HTML 소스창에 붙여넣기 테스트용입니다.",
            "namo_inline": "class/style 태그 의존 없이 각 요소에 style 속성을 직접 넣은 나모웹에디터 우선 테스트용입니다.",
            "image_post": "완성된 HTML을 PNG로 캡처해 이미지 중심으로 게시하는 방식입니다. CSS가 제거되는 게시판에서 가장 안정적인 테스트 후보입니다.",
        },
    }
    (board_dir / "upload_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (board_dir / "TEST_GUIDE.md").write_text(render_board_test_guide(), encoding="utf-8")


def write_board_image_exports(package: NewsletterPackage) -> None:
    board_dir = package.output_dir / "board"
    image_post_dir = board_dir / "image_post"
    image_dir = image_post_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    targets: list[tuple[str, Path, str]] = [
        ("newsletter_summary.png", package.output_dir / "newsletter.html", "메인 뉴스레터 요약"),
    ]
    for idx, article in enumerate(package.articles, 1):
        targets.append(
            (
                f"article_{idx:02d}.png",
                package.output_dir / "articles" / article_filename(idx, article),
                article.korean_title or article.title,
            )
        )

    capture_results = []
    for filename, html_path, label in targets:
        output_path = image_dir / filename
        ok, message = capture_html_as_png(html_path, output_path)
        capture_results.append(
            {
                "file": f"images/{filename}",
                "source_html": str(html_path.relative_to(package.output_dir)),
                "label": label,
                "created": ok,
                "message": message,
            }
        )

    created = [row for row in capture_results if row["created"]]
    image_post_html = render_board_image_post(package, created, with_link_placeholders=False)
    image_post_links_html = render_board_image_post(package, created, with_link_placeholders=True)
    image_index_html = render_board_image_indexed_post(package, created)
    image_details_html = render_board_image_details_post(package, created)
    image_tabs_html = render_board_image_tabs_post(package, created)
    image_js_tabs_html = render_board_image_js_tabs_post(package, created)
    (image_post_dir / "board_image_post.html").write_text(image_post_html, encoding="utf-8")
    (image_post_dir / "board_image_post_with_link_placeholders.html").write_text(image_post_links_html, encoding="utf-8")
    (image_post_dir / "board_image_indexed.html").write_text(image_index_html, encoding="utf-8")
    (image_post_dir / "board_image_details.html").write_text(image_details_html, encoding="utf-8")
    (image_post_dir / "board_image_css_tabs.html").write_text(image_tabs_html, encoding="utf-8")
    (image_post_dir / "board_image_js_tabs.html").write_text(image_js_tabs_html, encoding="utf-8")
    (image_post_dir / "README.md").write_text(render_board_image_test_guide(capture_results), encoding="utf-8")
    (image_post_dir / "image_manifest.json").write_text(
        json.dumps(capture_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def capture_html_as_png(html_path: Path, output_path: Path) -> tuple[bool, str]:
    if not html_path.exists():
        return False, "source HTML not found"
    # 1순위: Python playwright — `uv sync`가 설치하고, 브라우저는
    # `uv run playwright install chromium` 1회로 준비된다 (Node.js 불필요).
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError:
        return _capture_with_npx(html_path, output_path)  # 레거시 폴백 (Node 환경)
    try:
        with sync_playwright() as p:
            # 사내망에서는 playwright의 chromium 다운로드가 SSL 검사 프록시에
            # 막히므로, 이미 설치된 Chrome/Edge(channel)를 우선 사용한다.
            browser = None
            errors: list[str] = []
            for channel in ("chrome", "msedge", None):
                try:
                    browser = (
                        p.chromium.launch(channel=channel) if channel else p.chromium.launch()
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - try the next channel
                    errors.append(f"{channel or 'bundled'}: {str(exc)[:120]}")
            if browser is None:
                return False, (
                    "브라우저 없음 — Chrome 또는 Edge를 설치하거나 "
                    "`uv run playwright install chromium`을 실행하세요. " + " / ".join(errors)
                )
            page = browser.new_page(viewport={"width": 960, "height": 1400})
            # 배포물이 PNG이므로 폰트(CDN)·이미지가 다 로드된 뒤 캡처한다:
            # networkidle = 네트워크 요청이 잠잠해질 때까지 대기 (고정 대기보다 확실)
            try:
                page.goto(html_path.resolve().as_uri(), wait_until="networkidle", timeout=30000)
            except Exception:
                pass  # networkidle 타임아웃이어도 아래에서 여유를 두고 캡처
            page.wait_for_timeout(500)
            page.screenshot(path=str(output_path.resolve()), full_page=True)
            browser.close()
    except Exception as exc:
        return False, str(exc)[:300]
    return output_path.exists(), "created"


def _capture_with_npx(html_path: Path, output_path: Path) -> tuple[bool, str]:
    try:
        subprocess.run(
            [
                "npx",
                "playwright",
                "screenshot",
                "--full-page",
                "--viewport-size=960,1400",
                "--wait-for-timeout=1500",
                html_path.resolve().as_uri(),
                str(output_path.resolve()),
            ],
            check=True,
            cwd=str(html_path.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return False, str(exc)
    return output_path.exists(), "created"


def render_board_image_post(
    package: NewsletterPackage,
    image_rows: list[dict[str, object]],
    with_link_placeholders: bool,
) -> str:
    title = escape(package.title)
    lines = [
        '<div style="max-width:980px;margin:0 auto;font-family:Arial,\'Noto Sans KR\',sans-serif;color:#222;line-height:1.6;">',
        f'<h1 style="font-size:26px;line-height:1.35;margin:0 0 14px;">{title}</h1>',
        '<p style="margin:0 0 20px;color:#555;">게시판 호환성 테스트용 이미지 기반 뉴스레터입니다. HTML/CSS가 제거되는 환경에서는 이 파일을 우선 테스트하세요.</p>',
    ]
    if with_link_placeholders:
        lines.append(
            '<p style="margin:0 0 18px;color:#777;font-size:13px;">상세 게시글을 별도로 등록한 뒤, 아래 href의 INTERNAL_ARTICLE_URL 값을 내부 게시판 URL로 치환하세요.</p>'
        )
    for idx, row in enumerate(image_rows, 1):
        src = escape(str(row["file"]))
        label = escape(str(row["label"]))
        image = f'<img src="{src}" alt="{label}" style="display:block;width:100%;max-width:960px;height:auto;border:1px solid #ddd;margin:0 auto;">'
        if with_link_placeholders and idx > 1:
            image = f'<a href="INTERNAL_ARTICLE_URL_{idx - 1:02d}" style="display:block;text-decoration:none;">{image}</a>'
        lines.append(
            f"""
<div style="margin:0 0 28px;">
  <p style="margin:0 0 8px;color:#666;font-size:13px;">{idx:02d}. {label}</p>
  {image}
</div>
"""
        )
    lines.append("</div>")
    return "\n".join(lines)


def render_board_image_indexed_post(
    package: NewsletterPackage,
    image_rows: list[dict[str, object]],
) -> str:
    if not image_rows:
        return ""
    summary = image_rows[0]
    articles = image_rows[1:]
    nav = "".join(
        f'<a href="#article-{idx:02d}" style="display:inline-block;margin:0 6px 8px 0;padding:7px 10px;border:1px solid #cbd5df;border-radius:4px;color:#0f766e;text-decoration:none;font-size:13px;">{idx:02d}</a>'
        for idx, _ in enumerate(articles, 1)
    )
    sections = []
    for idx, row in enumerate(articles, 1):
        label = escape(str(row["label"]))
        src = escape(str(row["file"]))
        sections.append(
            f"""
<div id="article-{idx:02d}" style="margin:32px 0 0;padding-top:10px;border-top:1px solid #ddd;">
  <p style="margin:0 0 8px;color:#666;font-size:13px;">Article {idx:02d}. {label}</p>
  <img src="{src}" alt="{label}" style="display:block;width:100%;max-width:960px;height:auto;border:1px solid #ddd;margin:0 auto;">
  <p style="margin:10px 0 0;text-align:right;"><a href="#top" style="color:#0f766e;text-decoration:none;font-size:13px;">맨 위로</a></p>
</div>
"""
        )
    return f"""
<div id="top" style="max-width:980px;margin:0 auto;font-family:Arial,'Noto Sans KR',sans-serif;color:#222;line-height:1.6;">
  <h1 style="font-size:26px;line-height:1.35;margin:0 0 14px;">{escape(package.title)}</h1>
  <div style="margin:0 0 22px;padding:12px 14px;background:#f8fafb;border:1px solid #d8dee8;">
    <strong style="display:block;margin-bottom:8px;">상세 아티클 바로가기</strong>
    {nav}
  </div>
  <img src="{escape(str(summary["file"]))}" alt="{escape(str(summary["label"]))}" style="display:block;width:100%;max-width:960px;height:auto;border:1px solid #ddd;margin:0 auto 28px;">
  {"".join(sections)}
</div>
"""


def render_board_image_details_post(
    package: NewsletterPackage,
    image_rows: list[dict[str, object]],
) -> str:
    if not image_rows:
        return ""
    summary = image_rows[0]
    articles = image_rows[1:]
    details = []
    for idx, row in enumerate(articles, 1):
        label = escape(str(row["label"]))
        src = escape(str(row["file"]))
        details.append(
            f"""
<details style="margin:10px 0;border:1px solid #d8dee8;background:#fff;">
  <summary style="cursor:pointer;padding:12px 14px;font-weight:bold;color:#111;">{idx:02d}. {label}</summary>
  <div style="padding:0 14px 14px;">
    <img src="{src}" alt="{label}" style="display:block;width:100%;max-width:960px;height:auto;border:1px solid #ddd;margin:0 auto;">
  </div>
</details>
"""
        )
    return f"""
<div style="max-width:980px;margin:0 auto;font-family:Arial,'Noto Sans KR',sans-serif;color:#222;line-height:1.6;">
  <h1 style="font-size:26px;line-height:1.35;margin:0 0 14px;">{escape(package.title)}</h1>
  <img src="{escape(str(summary["file"]))}" alt="{escape(str(summary["label"]))}" style="display:block;width:100%;max-width:960px;height:auto;border:1px solid #ddd;margin:0 auto 24px;">
  <h2 style="font-size:20px;margin:22px 0 12px;">상세 아티클</h2>
  {"".join(details)}
</div>
"""


def render_board_image_tabs_post(
    package: NewsletterPackage,
    image_rows: list[dict[str, object]],
) -> str:
    if not image_rows:
        return ""
    tabs = []
    panels = []
    for idx, row in enumerate(image_rows):
        tab_id = "summary" if idx == 0 else f"article-{idx:02d}"
        label = "요약" if idx == 0 else f"{idx:02d}"
        title = escape(str(row["label"]))
        src = escape(str(row["file"]))
        checked = " checked" if idx == 0 else ""
        tabs.append(
            f'<input type="radio" name="ai-news-tab" id="tab-{tab_id}"{checked}><label for="tab-{tab_id}">{label}</label>'
        )
        panels.append(
            f"""
<section class="tab-panel panel-{tab_id}">
  <p>{title}</p>
  <img src="{src}" alt="{title}">
</section>
"""
        )
    panel_css = []
    for idx, _ in enumerate(image_rows):
        tab_id = "summary" if idx == 0 else f"article-{idx:02d}"
        panel_css.append(f"#tab-{tab_id}:checked ~ .panels .panel-{tab_id} {{ display:block; }}")
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>{escape(package.title)}</title>
  <style>
    .image-tabs {{ max-width:980px; margin:0 auto; font-family:Arial,'Noto Sans KR',sans-serif; color:#222; line-height:1.6; }}
    .image-tabs h1 {{ font-size:26px; line-height:1.35; margin:0 0 14px; }}
    .image-tabs input {{ position:absolute; opacity:0; pointer-events:none; }}
    .image-tabs label {{ display:inline-block; margin:0 6px 8px 0; padding:8px 11px; border:1px solid #cbd5df; border-radius:4px; cursor:pointer; font-size:13px; color:#0f766e; background:#fff; }}
    .image-tabs input:checked + label {{ background:#0f766e; color:#fff; border-color:#0f766e; }}
    .tab-panel {{ display:none; margin-top:12px; }}
    .tab-panel p {{ margin:0 0 8px; color:#666; font-size:13px; }}
    .tab-panel img {{ display:block; width:100%; max-width:960px; height:auto; border:1px solid #ddd; margin:0 auto; }}
    {" ".join(panel_css)}
  </style>
</head>
<body>
<div class="image-tabs">
  <h1>{escape(package.title)}</h1>
  <div class="tab-list">
    {"".join(tabs)}
  </div>
  <div class="panels">
    {"".join(panels)}
  </div>
</div>
</body>
</html>
"""


def render_board_image_js_tabs_post(
    package: NewsletterPackage,
    image_rows: list[dict[str, object]],
) -> str:
    if not image_rows:
        return ""
    summary = image_rows[0]
    articles = image_rows[1:]
    article_data = [
        {
            "index": idx,
            "title": str(row["label"]),
            "src": str(row["file"]),
        }
        for idx, row in enumerate(articles, 1)
    ]
    options = "".join(
        f'<option value="{escape(row["src"])}">{row["index"]:02d}. {escape(row["title"])}</option>'
        for row in article_data
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(package.title)}</title>
  <style>
    html, body {{ margin:0; padding:0; background:#fff; }}
    .js-newsletter {{ box-sizing:border-box; width:100%; max-width:1180px; min-height:100vh; margin:0 auto; padding:8px; font-family:Arial,'Noto Sans KR',sans-serif; color:#1f2933; line-height:1.5; }}
    .topbar {{ margin:0 0 8px; }}
    .topbar h1 {{ font-size:24px; line-height:1.28; margin:0 0 4px; }}
    .topbar p {{ margin:0; color:#64748b; font-size:14px; }}
    .controlbar {{ position:sticky; top:0; z-index:10; display:flex; align-items:center; gap:8px; padding:8px; margin:0 0 8px; background:#fff; border:1px solid #d8dee8; box-shadow:0 4px 12px rgba(15,23,42,.06); }}
    .home-button {{ flex:0 0 auto; height:38px; padding:0 15px; border:1px solid #0f766e; background:#0f766e; color:#fff; border-radius:5px; font-weight:800; cursor:pointer; }}
    .article-select {{ flex:1 1 auto; min-width:180px; height:38px; padding:0 10px; border:1px solid #cbd5df; border-radius:5px; background:#fff; color:#1f2933; font-size:14px; }}
    .viewer {{ background:#fff; border:0; }}
    .viewer-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; padding:6px 2px 8px; border-bottom:1px solid #e5ebf0; background:#fff; }}
    .viewer-head h2 {{ font-size:18px; line-height:1.32; margin:0; }}
    .viewer-head p {{ flex:0 0 auto; margin:3px 0 0; color:#64748b; font-size:13px; }}
    .image-wrap {{ padding:0; background:#fff; }}
    .image-wrap img {{ display:block; width:100%; max-width:none; height:auto; margin:0 auto; border:0; background:#fff; }}
    @media (max-width: 760px) {{
      .js-newsletter {{ padding:4px; }}
      .topbar h1 {{ font-size:20px; }}
      .controlbar {{ display:block; }}
      .home-button, .article-select {{ width:100%; margin:0 0 8px; }}
      .viewer-head {{ display:block; }}
      .viewer-head h2 {{ font-size:16px; }}
    }}
  </style>
</head>
<body>
<div class="js-newsletter">
  <div class="topbar">
    <div>
      <h1>{escape(package.title)}</h1>
      <p>메인 요약과 상세 아티클을 한 화면에서 선택해 크게 봅니다.</p>
    </div>
  </div>
  <div class="controlbar">
    <button type="button" class="home-button" id="home-button">메인페이지</button>
    <select class="article-select" id="article-select" aria-label="상세 아티클 선택">
      <option value="">상세 아티클 선택</option>
      {options}
    </select>
  </div>
  <div class="viewer">
    <div class="viewer-head">
      <h2 id="viewer-title">{escape(str(summary["label"]))}</h2>
      <p id="viewer-mode">Main</p>
    </div>
    <div class="image-wrap">
      <img id="viewer-image" src="{escape(str(summary["file"]))}" alt="{escape(str(summary["label"]))}">
    </div>
  </div>
</div>
<script>
const articles = {json.dumps(article_data, ensure_ascii=False)};
const summary = {json.dumps({"title": str(summary["label"]), "src": str(summary["file"])}, ensure_ascii=False)};
const titleEl = document.getElementById('viewer-title');
const modeEl = document.getElementById('viewer-mode');
const imageEl = document.getElementById('viewer-image');
const selectEl = document.getElementById('article-select');
const homeButton = document.getElementById('home-button');
function showMain() {{
  titleEl.textContent = summary.title;
  modeEl.textContent = 'Main';
  imageEl.src = summary.src;
  imageEl.alt = summary.title;
  selectEl.value = '';
  window.scrollTo({{ top: 0, behavior: 'smooth' }});
}}
function showArticle(src) {{
  if (!src) {{
    showMain();
    return;
  }}
  const selected = selectEl.options[selectEl.selectedIndex];
  const article = articles.find((item) => item.src === src);
  titleEl.textContent = selected ? selected.textContent : '상세 아티클';
  modeEl.textContent = article ? `Article ${{String(article.index).padStart(2, '0')}}` : 'Article';
  imageEl.src = src;
  imageEl.alt = selected ? selected.textContent : src;
  window.scrollTo({{ top: 0, behavior: 'smooth' }});
}}
homeButton.addEventListener('click', showMain);
selectEl.addEventListener('change', () => {{
  showArticle(selectEl.value);
}});
showMain();
</script>
</body>
</html>
"""


def render_board_image_test_guide(capture_results: list[dict[str, object]]) -> str:
    created = [row for row in capture_results if row["created"]]
    failed = [row for row in capture_results if not row["created"]]
    failed_lines = "\n".join(f"- {row['source_html']}: {row['message']}" for row in failed) or "- 없음"
    return f"""# 이미지 기반 게시판 테스트 산출물

## 파일

- `board_image_post.html`: 메인 요약 이미지와 상세 아티클 이미지를 모두 세로로 붙인 버전
- `board_image_post_with_link_placeholders.html`: 상세 아티클 이미지를 나중에 내부 게시판 URL로 감쌀 수 있는 버전
- `board_image_indexed.html`: 상단 목차에서 상세 아티클 위치로 이동하는 버전
- `board_image_details.html`: `<details>` 기반 접기/펼치기 버전
- `board_image_css_tabs.html`: JavaScript 없이 CSS radio tab으로 상세 이미지를 전환하는 버전
- `board_image_js_tabs.html`: JavaScript 버튼으로 상세 이미지 영역만 교체하는 버전
- `images/newsletter_summary.png`: 메인 뉴스레터 이미지
- `images/article_01.png` 등: 상세 아티클별 이미지

## 권장 테스트 순서

1. 게시판에 `images/` 폴더의 PNG 파일을 첨부 또는 이미지 업로드합니다.
2. `board_image_post.html`을 열어 HTML 소스 전체를 복사합니다.
3. 이미지 경로 `images/...`가 깨지면 게시판이 부여한 첨부 이미지 URL로 `src`를 치환합니다.
4. JavaScript가 허용되면 `board_image_js_tabs.html`을 우선 테스트합니다.
5. 아래 스크롤이 부담되지만 JavaScript가 제한되면 `board_image_indexed.html`, `board_image_details.html`, `board_image_css_tabs.html` 순서로 테스트합니다.
6. 별도 상세 게시글 구조가 가능하면 `board_image_post_with_link_placeholders.html`에서 `INTERNAL_ARTICLE_URL_01` 값을 실제 내부 게시판 URL로 바꿉니다.

## 생성 결과

- 생성 성공: {len(created)}개
- 생성 실패: {len(failed)}개

## 실패 항목

{failed_lines}
"""


def render_namo_inline(package: NewsletterPackage, image_mode: str) -> str:
    body = [
        f'<div style="max-width:860px;margin:0 auto;font-family:Arial,\'Noto Sans KR\',sans-serif;line-height:1.72;color:#222;">',
        '<div style="border-bottom:3px solid #0f766e;padding:22px 0 16px;margin-bottom:24px;">',
        '<p style="margin:0 0 8px;color:#0f766e;font-weight:bold;font-size:13px;letter-spacing:1px;">Weekly AI Radar</p>',
        f'<h1 style="margin:0 0 10px;font-size:30px;line-height:1.32;color:#111;">{escape(package.title)}</h1>',
        f'<p style="margin:0;color:#555;font-size:16px;">{escape(package.overview or "이번 주 AI 이슈를 주제 중심으로 정리하고, 대표 아티클을 사내 공유용으로 편집했습니다.")}</p>',
        '</div>',
    ]
    if package.issues:
        body.append('<h2 style="font-size:24px;margin:28px 0 14px;padding-bottom:8px;border-bottom:1px solid #ccc;color:#111;">이번 주 이슈 레이더</h2>')
        for issue in package.issues[:4]:
            body.append(
                f"""
<div style="border:1px solid #d8dee8;background:#f8fafb;padding:16px 18px;margin:12px 0 16px;border-radius:6px;">
  <h3 style="margin:0 0 10px;font-size:20px;line-height:1.4;color:#111;">{escape(issue.title)}</h3>
  <p style="margin:0 0 10px;color:#333;">{escape(issue.summary)}</p>
  <p style="margin:0 0 8px;color:#333;"><strong>왜 뜨나</strong> {escape(issue.why_hot)}</p>
  <p style="margin:0;color:#333;"><strong>사내 시사점</strong> {escape(issue.enterprise_relevance)}</p>
</div>
"""
            )
    _NAMO_H2 = '<h2 style="font-size:24px;margin:34px 0 14px;padding-bottom:8px;border-bottom:1px solid #ccc;color:#111;">{}</h2>'
    if _is_sectioned(package.articles):
        indexed = list(enumerate(package.articles, 1))
        top = sorted(indexed, key=lambda item: item[1].score, reverse=True)[:3]
        body.append(_NAMO_H2.format("이번 주 레이더"))
        body.append('<ul style="margin:6px 0 18px 22px;padding:0;">')
        for _, article in top:
            line = article.hook or article.one_liner or article.korean_title or article.title
            body.append(
                f'<li style="margin:6px 0;color:#333;">{escape(line)} '
                f'<span style="color:#888;font-size:13px;">— {escape(article.source_name)}</span></li>'
            )
        body.append("</ul>")
        for sec in SECTION_ORDER:
            meta = SECTION_META[sec]
            rows = [(idx, a) for idx, a in indexed if a.section == sec]
            body.append(_NAMO_H2.format(escape(meta["title"])))
            if not rows:
                body.append(f'<p style="margin:0 0 12px;color:#888;">{escape(meta["empty"])}</p>')
                continue
            body.append(f'<p style="margin:0 0 12px;color:#666;font-size:14px;">{escape(meta["description"])}</p>')
            for idx, article in rows:
                body.append(_namo_article_html(package, idx, article, image_mode))
        leftovers = [(idx, a) for idx, a in indexed if a.section not in SECTION_META]
        if leftovers:
            body.append(_NAMO_H2.format("그 밖의 소식"))
            for idx, article in leftovers:
                body.append(_namo_article_html(package, idx, article, image_mode))
    else:
        body.append(_NAMO_H2.format("상세 아티클"))
        for idx, article in enumerate(package.articles, 1):
            body.append(_namo_article_html(package, idx, article, image_mode))
    body.append("</div>")
    return "\n".join(body)


def _namo_article_html(
    package: NewsletterPackage, idx: int, article: RankedArticle, image_mode: str
) -> str:
    title = article.korean_title or article.title
    image = _board_image_src(package, article, image_mode)
    image_html = (
        f'<p style="margin:12px 0 18px;"><img src="{escape(image)}" alt="대표 이미지" style="display:block;width:100%;max-width:760px;height:auto;border:1px solid #ddd;"></p>'
        if image
        else ""
    )
    sections = article.detail_sections or _fallback_detail_sections(article)
    section_html = "".join(
        f"""
<h4 style="font-size:18px;margin:22px 0 8px;color:#111;">{escape(section.get("heading", ""))}</h4>
{namo_paragraphs(section.get("body", ""))}
"""
        for section in sections
    )
    terms = ", ".join(str(term) for term in article.terms[:8])
    return f"""
<div style="border-top:1px solid #ddd;padding:28px 0;">
  <p style="margin:0 0 6px;color:#777;font-size:13px;">{idx:02d} · {escape(article.source_name)}</p>
  <h3 style="font-size:24px;line-height:1.36;margin:0 0 14px;color:#111;">{escape(title)}</h3>
  {image_html}
  <p style="font-size:17px;line-height:1.75;color:#333;background:#f8fafb;border-left:4px solid #0f766e;padding:12px 14px;margin:0 0 18px;">{escape(article.detail_intro or article.korean_summary or article.summary)}</p>
  {section_html}
  <p style="margin:18px 0 8px;color:#666;font-size:14px;"><strong>주요 용어:</strong> {escape(terms)}</p>
  <p style="margin:0;color:#666;font-size:14px;word-break:break-all;"><strong>출처:</strong> {escape(article.url)}</p>
</div>
"""


def namo_paragraphs(text: str) -> str:
    chunks = [chunk.strip() for chunk in text.split("\n") if chunk.strip()]
    html: list[str] = []
    list_items: list[str] = []
    for chunk in chunks:
        if chunk.startswith("- "):
            list_items.append(chunk[2:].strip())
            continue
        if list_items:
            html.append(
                '<ul style="margin:6px 0 12px 22px;padding:0;">'
                + "".join(f'<li style="margin:5px 0;">{escape(item)}</li>' for item in list_items)
                + "</ul>"
            )
            list_items = []
        html.append(f'<p style="margin:0 0 12px;color:#333;">{escape(chunk)}</p>')
    if list_items:
        html.append(
            '<ul style="margin:6px 0 12px 22px;padding:0;">'
            + "".join(f'<li style="margin:5px 0;">{escape(item)}</li>' for item in list_items)
            + "</ul>"
        )
    return "\n".join(html)


def render_board_post(package: NewsletterPackage, css: str, image_mode: str, simple: bool = False) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>{escape(package.title)}</title>
  <style>
{css}
  </style>
</head>
<body>
{render_board_body(package, image_mode=image_mode, simple=simple)}
</body>
</html>
"""


def _board_article_html(
    package: NewsletterPackage, idx: int, article: RankedArticle, image_mode: str
) -> str:
    title = article.korean_title or article.title
    image = _board_image_src(package, article, image_mode)
    image_html = f'<p><img class="board-image" src="{escape(image)}" alt="대표 이미지"></p>' if image else ""
    sections = article.detail_sections or _fallback_detail_sections(article)
    section_html = "".join(
        f"<h4>{escape(section.get('heading', ''))}</h4>{paragraphs(section.get('body', ''))}"
        for section in sections
    )
    terms = ", ".join(str(term) for term in article.terms[:8])
    return f"""
<article class="board-article" id="article-{idx:02d}">
  <p class="board-source">{idx:02d} · {escape(article.source_name)}</p>
  <h3>{escape(title)}</h3>
  {image_html}
  <p class="board-lead">{escape(article.detail_intro or article.korean_summary or article.summary)}</p>
  {section_html}
  <p class="board-terms"><strong>주요 용어:</strong> {escape(terms)}</p>
  <p class="board-url"><strong>출처:</strong> {escape(article.url)}</p>
</article>
"""


def render_board_body(package: NewsletterPackage, image_mode: str, simple: bool = False) -> str:
    sectioned = _is_sectioned(package.articles)
    issue_html = ""
    if package.issues:
        issue_html = "<section class=\"board-section\"><h2>이번 주 이슈 레이더</h2>"
        for issue in package.issues[:4]:
            issue_html += f"""
<div class="board-issue">
  <h3>{escape(issue.title)}</h3>
  <p>{escape(issue.summary)}</p>
  <p><strong>왜 뜨나</strong> {escape(issue.why_hot)}</p>
  <p><strong>사내 시사점</strong> {escape(issue.enterprise_relevance)}</p>
</div>
"""
        issue_html += "</section>"
    elif sectioned:
        indexed = list(enumerate(package.articles, 1))
        top = sorted(indexed, key=lambda item: item[1].score, reverse=True)[:3]
        issue_html = "<section class=\"board-section\"><h2>이번 주 레이더</h2><ul class=\"board-radar\">"
        for idx, article in top:
            line = article.hook or article.one_liner or article.korean_title or article.title
            issue_html += (
                f'<li><a href="#article-{idx:02d}">{escape(line)}</a> '
                f"<span>— {escape(article.source_name)}</span></li>"
            )
        issue_html += "</ul></section>"

    if sectioned:
        article_html = ""
        indexed = list(enumerate(package.articles, 1))
        for sec in SECTION_ORDER:
            meta = SECTION_META[sec]
            rows = [(idx, a) for idx, a in indexed if a.section == sec]
            article_html += f"<section class=\"board-section\"><h2>{escape(meta['title'])}</h2>"
            if rows:
                article_html += f"<p class=\"board-section-desc\">{escape(meta['description'])}</p>"
                for idx, article in rows:
                    article_html += _board_article_html(package, idx, article, image_mode)
            else:
                article_html += f"<p class=\"board-section-desc\">{escape(meta['empty'])}</p>"
            article_html += "</section>"
        leftovers = [(idx, a) for idx, a in indexed if a.section not in SECTION_META]
        if leftovers:
            article_html += "<section class=\"board-section\"><h2>그 밖의 소식</h2>"
            for idx, article in leftovers:
                article_html += _board_article_html(package, idx, article, image_mode)
            article_html += "</section>"
    else:
        article_html = "<section class=\"board-section\"><h2>상세 아티클</h2>"
        for idx, article in enumerate(package.articles, 1):
            article_html += _board_article_html(package, idx, article, image_mode)
        article_html += "</section>"

    wrapper_class = "board-post simple" if simple else "board-post"
    return f"""
<div class="{wrapper_class}">
  <header class="board-header">
    <p class="board-kicker">Weekly AI Radar</p>
    <h1>{escape(package.title)}</h1>
    <p>{escape(package.overview or "이번 주 AI 이슈를 주제 중심으로 정리하고, 각 이슈의 대표 아티클을 사내 공유용으로 편집했습니다.")}</p>
  </header>
  {issue_html}
  {article_html}
</div>
"""


def _board_image_src(package: NewsletterPackage, article: RankedArticle, image_mode: str) -> str:
    if image_mode == "none":
        return ""
    src = article.local_image or _first_image([article])
    if not src:
        return ""
    if image_mode == "file":
        return src.replace("assets/images/", "images/")
    if image_mode == "base64":
        local = package.output_dir / src
        if not local.exists():
            return ""
        mime = mimetypes.guess_type(local.name)[0] or "image/png"
        encoded = base64.b64encode(local.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    return src


def render_board_css() -> str:
    return """
.board-post { max-width: 860px; margin: 0 auto; color: #1f2933; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans KR", sans-serif; line-height: 1.72; }
.board-header { border-bottom: 3px solid #0f766e; padding: 24px 0 18px; margin-bottom: 26px; }
.board-kicker { color: #0f766e; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; margin: 0 0 8px; }
.board-radar { margin: 6px 0 18px 22px; padding: 0; }
.board-radar li { margin: 6px 0; }
.board-radar span { color: #888; font-size: 13px; }
.board-section-desc { color: #666; font-size: 14px; margin: 0 0 12px; }
.board-header h1 { font-size: 30px; line-height: 1.3; margin: 0 0 10px; }
.board-section { margin: 30px 0; }
.board-section > h2 { font-size: 23px; border-bottom: 1px solid #d8dee8; padding-bottom: 8px; margin: 0 0 16px; }
.board-issue { border: 1px solid #d8dee8; background: #f8fafb; padding: 16px 18px; margin: 12px 0; border-radius: 8px; }
.board-issue h3 { margin: 0 0 8px; font-size: 20px; }
.board-article { border-top: 1px solid #d8dee8; padding: 28px 0; }
.board-source { color: #697386; font-size: 13px; margin: 0 0 6px; }
.board-article h3 { font-size: 24px; line-height: 1.35; margin: 0 0 14px; }
.board-image { width: 100%; max-width: 760px; height: auto; border: 1px solid #d8dee8; border-radius: 6px; }
.board-lead { font-size: 17px; color: #344054; background: #f8fafb; border-left: 4px solid #0f766e; padding: 12px 14px; }
.board-article h4 { font-size: 18px; margin: 22px 0 8px; }
.board-article p { margin: 0 0 12px; }
.board-article ul { margin: 6px 0 12px 20px; padding: 0; }
.board-terms, .board-url { color: #697386; font-size: 14px; overflow-wrap: anywhere; }
"""


def render_simple_board_css() -> str:
    return """
.board-post { max-width: 760px; margin: 0 auto; font-family: "Noto Sans KR", Arial, sans-serif; line-height: 1.7; color: #222; }
.board-header h1 { font-size: 26px; margin: 0 0 12px; }
.board-kicker, .board-source, .board-terms, .board-url { color: #666; font-size: 13px; }
.board-section { margin: 24px 0; }
.board-section h2 { font-size: 22px; border-bottom: 1px solid #ccc; padding-bottom: 8px; }
.board-issue, .board-article { border-bottom: 1px solid #ddd; padding: 16px 0; }
.board-article h3, .board-issue h3 { font-size: 20px; margin: 0 0 10px; }
.board-article h4 { font-size: 17px; margin: 18px 0 8px; }
"""


def render_board_test_guide() -> str:
    return """# 나모웹에디터 게시판 테스트 가이드

## 테스트 순서

1. `image_post/board_image_post.html`
   - HTML/CSS가 제거되는 게시판에서 가장 안정적인 이미지 기반 게시 테스트
   - `image_post/images/`의 PNG 파일을 첨부 또는 이미지 업로드한 뒤 `src` 경로를 확인

2. `image_post/board_image_post_with_link_placeholders.html`
   - 상세 아티클을 별도 게시글로 등록할 수 있을 때 테스트
   - `INTERNAL_ARTICLE_URL_01` 값을 실제 내부 게시판 URL로 치환

3. `board_post_inline_css_file_images.html`
   - 브라우저로 열어 모양 확인
   - 게시판 HTML 편집 모드에 전체 HTML 또는 body 내부를 붙여넣기
   - 이미지 상대경로가 깨지는지 확인

4. `namo_inline_styles_no_images.html`
   - 나모웹에디터가 `<style>`과 `class`를 제거하는 경우의 1순위 fallback
   - 이미지 없이 레이아웃/문단/섹션 구조만 확인

5. `namo_inline_styles_base64.html`
   - 각 요소에 style을 직접 넣고 이미지는 base64로 포함
   - data URI 허용 여부 확인

6. `board_post_inline_css_base64_images.html`
   - 이미지가 HTML 안에 포함된 버전
   - 게시판이 `data:image/...`를 허용하는지 확인

7. `board_body_fragment.html`
   - `<html>`, `<head>` 없는 본문 조각
   - 나모웹에디터 소스 편집창에 붙여넣기 적합

8. `board_body_fragment_base64.html`
   - 본문 조각 + base64 이미지

9. `board_post_simple.html`
   - CSS 최소화 fallback
   - 복잡한 스타일이 제거되는 게시판용

## 확인 항목

- `<style>` 태그가 유지되는가
- `class` 속성이 유지되는가
- 이미지가 표시되는가
- base64 이미지가 차단되는가
- 제목/문단/목록 간격이 유지되는가
- 본문 길이 제한에 걸리는가
- 외부 출처 URL 텍스트가 보존되는가
- 게시 후 모바일/PC에서 깨지지 않는가

## UiPath 자동화 후보 절차

1. 망연계로 `board/` 폴더 반입
2. 게시판 새 글 열기
3. 나모웹에디터 HTML 소스 모드 전환
4. 테스트에서 통과한 HTML 파일 내용 붙여넣기
5. 이미지 첨부형을 쓸 경우 `board/images/` 업로드 후 src 치환
6. 미리보기 확인
7. 게시
"""


def render_html(package: NewsletterPackage) -> str:
    hero_image = _first_image(package.articles)
    radar_title = "이번 주 이슈 레이더"
    radar_hint = ""
    if _is_sectioned(package.articles):
        radar_hint = '<p class="radar-hint">번호는 상세 아티클(첨부 이미지) 번호와 동일합니다</p>'
        # Radar = TOC-style one-liners for the top stories; sections below carry
        # the full cards, so nothing is skipped.
        radar_title = "이번 주 레이더"
        issue_cards = render_radar_cards(package.articles)
        cards = render_section_groups(package.articles, thumbnails=package.thumbnails)
    else:
        issue_cards = render_issue_cards(package)
        editor_ids = _issue_representative_ids(package)
        if not issue_cards:
            editors = package.articles[:2]
            issue_cards = "".join(
                _feature_card(idx, article) for idx, article in enumerate(editors, 1)
            )
            editor_ids = {article.id for article in editors}
        cards = render_grouped_articles(package.articles, skip_ids=editor_ids)
    hero = f'<img class="hero-image" src="{escape(hero_image)}" alt="대표 이미지">' if hero_image else '<div class="hero-panel"><span>AI</span><strong>Weekly Brief</strong></div>'
    lead = package.overview or "AI 모델 경쟁은 에이전트(agent), 보안 자동화, 실무형 평가로 이동 중입니다. 이번 주 업무 영향도가 큰 신호만 골라 웹진형으로 정리했습니다."
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(package.title)}</title>
  <link rel="stylesheet" href="assets/style.css">
</head>
<body>
  <main class="newsletter">
    <header class="masthead">
      <div>
        <p class="kicker">Weekly AI Brief</p>
        <h1>{escape(package.title)}</h1>
        <p class="lead">{escape(lead)}</p>
      </div>
      {hero}
    </header>
    <section class="editors-pick">
      <header class="section-title">
        <div>
          <p>Editor's Pick</p>
          <h2>{radar_title}</h2>
          {radar_hint}
        </div>
      </header>
      <div class="feature-grid">
        {issue_cards}
      </div>
    </section>
    <section class="articles">
      {cards}
    </section>
  </main>
</body>
</html>
"""


def render_issue_cards(package: NewsletterPackage) -> str:
    if not package.issues:
        return ""
    by_id = {article.id: article for article in package.articles}
    cards = []
    for issue_idx, issue in enumerate(package.issues[:3], 1):
        representative = by_id.get(issue.representative_article_id)
        if not representative:
            continue
        detail_href = f"articles/{escape(_article_filename_by_id(package.articles, representative.id))}"
        keywords = " ".join(f"<span>{escape(keyword)}</span>" for keyword in issue.keywords[:4])
        related = [
            by_id[article_id]
            for article_id in issue.article_ids
            if article_id in by_id and article_id != representative.id
        ][:3]
        related_html = "".join(
            f"<li>{escape(article.source_name)} · {escape(article.korean_title or article.title)}</li>"
            for article in related
        )
        related_block = f"<ul class=\"related-list\">{related_html}</ul>" if related_html else ""
        cards.append(
            f"""
<article class="feature-card issue-card">
  <p class="source"><span class="article-number">이슈 {issue_idx:02d}</span> Issue Radar</p>
  <h3><a href="{detail_href}">{escape(issue.title)}</a></h3>
  <p>{escape(issue.summary)}</p>
  <p class="why"><strong>왜 뜨나</strong> {escape(issue.why_hot)}</p>
  <p class="why"><strong>사내 시사점</strong> {escape(issue.enterprise_relevance)}</p>
  <div class="terms">{keywords}</div>
  {related_block}
  <a class="detail-link" href="{detail_href}">대표 아티클 보기</a>
</article>
"""
        )
    return "".join(cards)


def render_radar_cards(articles: list[RankedArticle]) -> str:
    """TOC-style radar: the week's top three stories as one-line summary cards."""
    indexed = list(enumerate(articles, 1))
    top = sorted(indexed, key=lambda item: item[1].score, reverse=True)[:3]
    cards = []
    for idx, article in top:
        detail_href = f"articles/{escape(article_filename(idx, article))}"
        callout = _callout_text(article, 90)
        section_title = SECTION_META.get(article.section, {}).get("title", "")
        cards.append(
            f"""
<article class="feature-card issue-card">
  <p class="source"><span class="article-number">{idx:02d}</span> {escape(section_title)}</p>
  <h3><a href="{detail_href}">{escape(article.korean_title or article.title)}</a></h3>
  <p class="card-callout">&ldquo;{escape(callout)}&rdquo;</p>
</article>
"""
        )
    return "".join(cards)


def _truncate_text(text: str, limit: int = 170) -> str:
    """Flatten list markers/newlines and cut at a sentence boundary near limit."""
    flat = " ".join(
        chunk.lstrip("-").strip() for chunk in text.split("\n") if chunk.strip()
    )
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    boundary = max(cut.rfind("다. "), cut.rfind(". "), cut.rfind("음. "), cut.rfind("함. "))
    if boundary > limit * 0.4:
        return cut[: boundary + 2].strip()
    return cut.rstrip() + "…"


def _callout_text(article: RankedArticle, limit: int = 90) -> str:
    """포털 전역 공통 콜아웃: hook(본문 인용) → one_liner → 본문/요약 절단.
    어떤 폴백이든 짧은 한 입 분량을 넘지 않는다 — 완결 요약 금지."""
    text = article.hook or article.one_liner
    if not text and article.detail_sections:
        text = article.detail_sections[0].get("body", "")
    if not text:
        text = article.korean_summary or article.summary
    return _truncate_text(text, limit)


def _story_row(index: int, article: RankedArticle, thumb: bool = False) -> str:
    """본지는 낚시(teaser): 제목 + 본문 콜아웃만 던지고, 내용 전부는
    상세 아티클(첨부 이미지)로 유도한다. thumb=True면 대표 이미지 썸네일 표시."""
    detail_href = f"articles/{escape(article_filename(index, article))}"
    hook = _callout_text(article, 110)
    # 배포물이 PNG이므로 썸네일은 캡처 시점에 확실히 존재하는 로컬 이미지만 쓴다.
    # (원격 URL 폴백은 캡처 때 빈 박스가 될 수 있어 제외 — 없으면 텍스트 행으로)
    image = article.local_image if thumb else ""
    if image:
        return f"""
<article class="story has-thumb" id="story-{index:02d}">
  <div class="story-main">
    <p class="source">{_article_meta(index, article)}</p>
    <h3><a href="{detail_href}">{escape(article.korean_title or article.title)}</a></h3>
    <p class="story-hook">&ldquo;{escape(hook)}&rdquo;</p>
  </div>
  <div class="story-thumb"><img src="{escape(image)}" alt="대표 이미지"><span>{index:02d}</span></div>
</article>
"""
    return f"""
<article class="story" id="story-{index:02d}">
  <span class="story-no">{index:02d}</span>
  <p class="source">{_article_meta(index, article)}</p>
  <h3><a href="{detail_href}">{escape(article.korean_title or article.title)}</a></h3>
  <p class="story-hook">&ldquo;{escape(hook)}&rdquo;</p>
</article>
"""


def render_section_groups(articles: list[RankedArticle], thumbnails: bool = False) -> str:
    """Fixed-section layout, 시안 A 구조: 카드 그리드가 아니라 전폭 기사 행.
    A section without articles states so instead of disappearing."""
    indexed = list(enumerate(articles, 1))
    html = []
    for sec in SECTION_ORDER:
        meta = SECTION_META[sec]
        rows = [(idx, a) for idx, a in indexed if a.section == sec]
        body = (
            f'<div class="story-list">{"".join(_story_row(idx, a, thumb=thumbnails) for idx, a in rows)}</div>'
            if rows
            else f'<p class="section-empty">{escape(meta["empty"])}</p>'
        )
        html.append(
            f"""<section class="article-group section-{sec}">
  <header class="group-header">
    <h2>{escape(meta["title"])}</h2>
    <p>{escape(meta["description"])}</p>
  </header>
  {body}
</section>"""
        )
    leftovers = [(idx, a) for idx, a in indexed if a.section not in SECTION_META]
    if leftovers:
        html.append(
            f"""<section class="article-group">
  <header class="group-header"><h2>그 밖의 소식</h2></header>
  <div class="story-list">{"".join(_story_row(idx, a, thumb=thumbnails) for idx, a in leftovers)}</div>
</section>"""
        )
    return "\n".join(html)


def _issue_representative_ids(package: NewsletterPackage) -> set[str]:
    return {issue.representative_article_id for issue in package.issues if issue.representative_article_id}


def render_quality_summary(report: dict[str, object]) -> str:
    evaluation = report.get("llm_evaluation")
    if not isinstance(evaluation, dict):
        return f"<p>{escape(str(evaluation or 'LLM 품질 점검 정보 없음'))}</p>"
    items = evaluation.get("items")
    if not isinstance(items, list):
        return "<p>LLM 품질 점검은 수행됐지만 항목별 요약 형식이 없습니다.</p>"
    scored: list[tuple[float, str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        scores = item.get("scores")
        if isinstance(scores, dict):
            total = scores.get("종합") or scores.get("overall") or scores.get("internal_implication") or 0
        else:
            total = item.get("score") or 0
        try:
            score = float(total)
        except (TypeError, ValueError):
            score = 0
        title = str(item.get("title") or item.get("id") or "항목")
        comment = str(item.get("comment") or item.get("comments") or "")
        scored.append((score, title, comment))
    scored.sort(reverse=True, key=lambda row: row[0])
    rows = "\n".join(
        f"""<li><strong>{escape(title)}</strong><span>{score:.0f}점</span><p>{escape(comment)}</p></li>"""
        for score, title, comment in scored[:3]
    )
    return f"""<p class="quality-lead">LLM 평가 기준으로 사내 공유 우선순위가 높은 항목입니다.</p>
<ol class="quality-list">
  {rows}
</ol>"""


def render_grouped_articles(articles: list[RankedArticle], skip_ids: set[str] | None = None) -> str:
    skip_ids = skip_ids or set()
    indexed_articles = [(idx, article) for idx, article in enumerate(articles, 1) if article.id not in skip_ids]
    groups = [
        (
            "주목할 만한 레포",
            "GitHub에서 최근 업데이트와 관심 지표가 확인된 오픈소스 프로젝트입니다.",
            [(idx, a) for idx, a in indexed_articles if a.source_id.startswith("github")],
        ),
        (
            "주목할 만한 허깅페이스 모델",
            "Hugging Face Hub에서 다운로드·좋아요 등 정량 지표가 확인된 모델입니다.",
            [(idx, a) for idx, a in indexed_articles if a.source_id == "huggingface-models"],
        ),
        (
            "주목할 만한 파운데이션 모델 소식",
            "주요 모델 제공사의 신규 모델, 성능 업데이트, 제품화 흐름입니다.",
            [
                (idx, a)
                for idx, a in indexed_articles
                if a.source_id in {"openai-news", "anthropic-news", "google-deepmind"}
                and _looks_foundation_model_news(a)
            ],
        ),
        (
            "주요 뉴스·기술 블로그",
            "AI 도입, 보안, 에이전트, 벤치마크 등 업무 영향도가 있는 글입니다.",
            [],
        ),
    ]
    assigned = {a.id for _, _, rows in groups[:-1] for _, a in rows}
    groups[-1] = (
        groups[-1][0],
        groups[-1][1],
        [(idx, a) for idx, a in indexed_articles if a.id not in assigned],
    )

    html = []
    for title, description, rows in groups:
        if not rows:
            continue
        html.append(
            f"""<section class="article-group">
  <header class="group-header">
    <h2>{escape(title)}</h2>
    <p>{escape(description)}</p>
  </header>
  <div class="card-grid">
    {"".join(_article_card(idx, article) for idx, article in rows)}
  </div>
</section>"""
        )
    return "\n".join(html)


def _looks_foundation_model_news(article: RankedArticle) -> bool:
    text = f"{article.title} {article.korean_title} {article.summary} {article.korean_summary}".lower()
    keywords = [
        "model",
        "gpt",
        "claude",
        "gemini",
        "opus",
        "reasoning",
        "foundation",
        "llm",
        "모델",
        "추론",
        "파운데이션",
    ]
    return any(keyword in text for keyword in keywords)


def _feature_card(index: int, article: RankedArticle) -> str:
    detail_href = f"articles/{escape(article_filename(index, article))}"
    terms = " ".join(f"<span>{escape(str(term))}</span>" for term in article.terms[:4])
    meta = _article_meta(index, article)
    return f"""
<article class="feature-card">
  <p class="source">{meta}</p>
  <h3><a href="{detail_href}">{escape(article.korean_title or article.title)}</a></h3>
  <p>{escape(article.korean_summary or article.summary)}</p>
  <div class="terms">{terms}</div>
  <a class="detail-link" href="{detail_href}">상세 아티클 보기</a>
</article>
"""


def _article_card(index: int, article: RankedArticle) -> str:
    image = _image_src(article)
    img = f'<img src="{escape(image)}" alt="기사 이미지">' if image else '<div class="thumb-placeholder">AI</div>'
    terms = " ".join(f"<span>{escape(str(term))}</span>" for term in article.terms[:3])
    detail_href = f"articles/{escape(article_filename(index, article))}"
    meta = _article_meta(index, article)
    return f"""
<article class="article-card">
  {img}
  <div class="article-body">
    <p class="source">{meta}</p>
    <h2><a href="{detail_href}">{escape(article.korean_title or article.title)}</a></h2>
    <p>{escape(article.korean_summary or article.summary)}</p>
    <div class="terms">{terms}</div>
    <p><a class="detail-link" href="{detail_href}">상세 아티클 보기</a></p>
  </div>
</article>
"""


def render_article_html(package: NewsletterPackage, index: int, article: RankedArticle) -> str:
    title = article.korean_title or article.title
    image = _image_src(article, detail=True)
    if image and article.image_credit:
        img = (
            f'<figure class="detail-image-wrap" style="margin:0">'
            f'<img class="detail-image" src="{escape(image)}" alt="기사 이미지">'
            f'<figcaption class="image-credit" style="font-size:12px;opacity:.6;margin-top:4px">'
            f'이미지 출처: {escape(article.image_credit)}</figcaption>'
            f'</figure>'
        )
    elif image:
        img = f'<img class="detail-image" src="{escape(image)}" alt="기사 이미지">'
    else:
        img = ""
    terms = " ".join(f"<span>{escape(str(term))}</span>" for term in article.terms[:8])
    intro = article.detail_intro or article.korean_summary or article.summary or "요약 없음"
    detail_sections = article.detail_sections or _fallback_detail_sections(article)
    published = _published_date(article)
    published_row = f'<p class="url">게시일: {escape(published)}</p>' if published else ""
    section_html = "\n".join(
        f"""<section>
        <h2>{escape(section.get("heading", ""))}</h2>
        {paragraphs(section.get("body", ""))}
      </section>"""
        for section in detail_sections
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="../assets/style.css">
</head>
<body>
  <main class="newsletter detail-page">
    <article class="detail-article{f' section-{escape(article.section)}' if article.section else ''}">
      <p class="source">{_article_meta(index, article)}</p>
      <h1>{escape(title)}</h1>
      {img}
      <p class="detail-lead">{escape(intro)}</p>
      {section_html}
      <section>
        <h2>주요 용어</h2>
        <div class="terms">{terms}</div>
      </section>
      <section>
        <h2>원문 정보</h2>
        <p class="url">제목: {escape(article.title)}</p>
        {published_row}
        <p class="url">출처 URL: {escape(article.url)}</p>
      </section>
    </article>
  </main>
</body>
</html>
"""


def _article_meta(index: int, article: RankedArticle) -> str:
    published = _published_date(article)
    date = f' <span class="source-date">게시일 {escape(published)}</span>' if published else ""
    return f'<span class="article-number">{index:02d}</span> {escape(article.source_name)}{date}'


def _published_date(article: RankedArticle) -> str:
    if not article.published_at:
        return ""
    return article.published_at.astimezone(timezone.utc).strftime("%Y.%m.%d")


def article_filename(index: int, article: RankedArticle) -> str:
    return f"{index:02d}_{article.id}.html"


def _article_filename_by_id(articles: list[RankedArticle], article_id: str) -> str:
    for idx, article in enumerate(articles, 1):
        if article.id == article_id:
            return article_filename(idx, article)
    return ""


def paragraphs(text: str) -> str:
    chunks = [chunk.strip() for chunk in text.split("\n") if chunk.strip()]
    if not chunks:
        return "<p>내용 없음</p>"
    html: list[str] = []
    list_items: list[str] = []
    for chunk in chunks:
        if chunk.startswith("- "):
            list_items.append(chunk[2:].strip())
            continue
        if list_items:
            html.append("<ul>" + "".join(f"<li>{escape(item)}</li>" for item in list_items) + "</ul>")
            list_items = []
        html.append(f"<p>{escape(chunk)}</p>")
    if list_items:
        html.append("<ul>" + "".join(f"<li>{escape(item)}</li>" for item in list_items) + "</ul>")
    return "\n".join(html)


def _fallback_detail_sections(article: RankedArticle) -> list[dict[str, str]]:
    if article.body:
        chunks = _body_chunks(article.body)
        headings = [
            "들어가며",
            "무엇이 달라졌나",
            "핵심 구조와 작동 방식",
            "설정·실행·적용 포인트",
            "현장 활용 시나리오",
            "주의할 점",
            "마치며",
        ]
        sections = []
        for heading, chunk in zip(headings, chunks, strict=False):
            sections.append({"heading": heading, "body": chunk})
        if len(sections) >= 4:
            return sections
    return [
        {
            "heading": "들어가며",
            "body": article.korean_summary or article.summary or "수집된 원문 정보를 바탕으로 선별된 AI 관련 항목입니다.",
        },
        {
            "heading": "핵심 변화",
            "body": article.why_it_matters or article.reason,
        },
        {
            "heading": "실무 시사점",
            "body": "AI 모델, 에이전트(agent), 기업 적용, 보안/규제, 오픈소스 확산 가능성을 기준으로 평가했습니다.",
        },
    ]


def _body_chunks(body: str) -> list[str]:
    paragraphs = [
        line.strip()
        for line in body.split("\n")
        if len(line.strip()) >= 80 and not line.strip().startswith("http")
    ]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        current.append(paragraph)
        current_len += len(paragraph)
        if current_len >= 650:
            chunks.append(" ".join(current))
            current = []
            current_len = 0
    if current:
        chunks.append(" ".join(current))
    return chunks[:7]


def _first_image(articles: list[RankedArticle]) -> str:
    for article in articles:
        for url in article.image_urls:
            if url.startswith("http"):
                return url
    return ""


def _image_src(article: RankedArticle, detail: bool = False) -> str:
    if article.local_image:
        return f"../{article.local_image}" if detail else article.local_image
    image = _first_image([article])
    return image


# 모든 테마 공통 기반 CSS. 폰트는 Pretendard로 통일 (PNG 캡처 시 CDN 로드).
_BASE_CSS = """
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css');
:root { color-scheme: light; --ink: #191f28; --muted: #6b7684; --line: #e5e8eb; --paper: #ffffff; --wash: #f4f5f7; --accent: #0f766e; --warm: #b45309;
  --frontier: #3b5bdb; --open: #0ca678; --research: #a855f7; --tooling: #e8590c; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--wash); color: var(--ink); font-family: "Pretendard", -apple-system, BlinkMacSystemFont, "Segoe UI", "Malgun Gothic", "Noto Sans KR", sans-serif; line-height: 1.65; }
.newsletter { max-width: 1080px; margin: 0 auto; background: var(--paper); min-height: 100vh; }
.masthead { display: grid; grid-template-columns: 1.25fr .75fr; gap: 34px; align-items: stretch; padding: 48px 44px 32px; border-bottom: 1px solid var(--line); background: linear-gradient(180deg, #ffffff 0%, #f7fafb 100%); }
.kicker { margin: 0 0 8px; color: var(--accent); font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
h1 { margin: 0; font-size: 34px; line-height: 1.25; letter-spacing: 0; }
.lead { margin: 14px 0 0; color: var(--muted); font-size: 17px; }
.hero-image { width: 100%; aspect-ratio: 4 / 3; object-fit: cover; border-radius: 8px; border: 1px solid var(--line); }
.hero-panel { min-height: 260px; border-radius: 8px; border: 1px solid var(--line); background: #102321; color: #f6fbf9; display: flex; flex-direction: column; justify-content: flex-end; padding: 28px; }
.hero-panel span { font-size: 54px; font-weight: 900; line-height: 1; color: #8dd7c8; }
.hero-panel strong { font-size: 22px; margin-top: 8px; }
.summary { display: grid; grid-template-columns: repeat(3, 1fr); border-bottom: 1px solid var(--line); }
.summary div { padding: 18px 44px; border-right: 1px solid var(--line); }
.summary div:last-child { border-right: 0; }
.summary strong { display: block; font-size: 24px; }
.summary span { color: var(--muted); }
.editors-pick { padding: 34px 44px 28px; border-bottom: 1px solid var(--line); }
.section-title { display: flex; align-items: end; justify-content: space-between; gap: 18px; margin-bottom: 16px; }
.section-title p { margin: 0; color: var(--accent); font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.section-title h2 { margin: 0; font-size: 24px; letter-spacing: 0; }
/* 카드 수에 맞춰 열이 늘어난다: 3장이면 1열 3개, 2장이면 2개 */
.feature-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 18px; }
.feature-card { border: 1px solid var(--line); border-radius: 8px; padding: 20px 22px; background: #fbfcfd; display: flex; flex-direction: column; }
.feature-card h3 { margin: 0 0 10px; font-size: 20px; line-height: 1.38; letter-spacing: 0; }
.feature-card h3 a { color: var(--ink); text-decoration: none; }
.feature-card p { margin: 0 0 12px; font-size: 14.5px; }
.card-callout { padding-left: 12px; border-left: 3px solid var(--sc, var(--accent)); font-weight: 500; color: #333d4b; }
.feature-card .detail-link { margin-top: auto; font-size: 13.5px; }
.issue-card .why { background: transparent; border-left: 0; padding: 0; }
.related-list { margin: 0 0 14px 18px; padding: 0; color: var(--muted); font-size: 14px; }
.related-list li { margin: 4px 0; }
.articles { padding: 0; }
.article-group { border-bottom: 1px solid var(--line); }
.group-header { padding: 30px 44px 12px; background: #fbfcfd; border-bottom: 1px solid var(--line); }
.group-header h2 { margin: 0 0 6px; font-size: 22px; letter-spacing: 0; }
.group-header p { margin: 0; color: var(--muted); }
.section-empty { margin: 0; padding: 22px 44px 30px; color: var(--muted); }
.card-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; padding: 24px 44px 32px; }
.article-card { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: #fff; display: flex; flex-direction: column; min-height: 100%; }
.article-card .article-body { padding: 18px; display: flex; flex-direction: column; flex: 1; }
.article-card h2 { margin: 0 0 10px; font-size: 19px; line-height: 1.38; letter-spacing: 0; }
.article-card h2 a { color: var(--ink); text-decoration: none; }
.article-card p { margin: 0 0 12px; }
.article-card img, .thumb-placeholder { width: 100%; aspect-ratio: 16 / 9; object-fit: cover; border-bottom: 1px solid var(--line); }
.thumb-placeholder { display: flex; align-items: center; justify-content: center; background: #e9f4f1; color: var(--accent); font-size: 34px; font-weight: 900; }
.rank { color: var(--warm); font-size: 22px; font-weight: 800; }
.source { margin: 0 0 6px; color: var(--muted); font-size: 14px; }
.source-date { display: inline-block; margin-left: 8px; color: var(--warm); font-weight: 700; }
.article-number { display: inline-block; min-width: 32px; height: 24px; line-height: 24px; margin-right: 7px; padding: 0 8px; text-align: center; border-radius: 999px; background: var(--accent); color: #fff; font-size: 12px; font-weight: 800; }
.article h2 { margin: 0 0 12px; font-size: 23px; line-height: 1.35; letter-spacing: 0; }
.article h2 a { color: var(--ink); text-decoration: none; }
.article h2 a:hover, .article-card h2 a:hover, .feature-card h3 a:hover, .detail-link:hover { color: var(--accent); text-decoration: underline; }
.article img { width: min(520px, 100%); max-height: 280px; object-fit: cover; border-radius: 8px; border: 1px solid var(--line); margin: 4px 0 12px; }
.article p { margin: 0 0 12px; }
.why { background: #f8fafb; border-left: 4px solid var(--accent); padding: 10px 12px; }
.terms { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 12px; }
.terms span { border: 1px solid var(--line); border-radius: 999px; padding: 3px 9px; color: var(--muted); font-size: 13px; }
.meta, .url { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
.detail-link { color: var(--accent); font-weight: 700; text-decoration: none; }
.quality { padding: 28px 44px 48px; }
.quality h2 { margin: 0 0 10px; font-size: 20px; }
.quality p { margin: 0; color: var(--muted); white-space: pre-wrap; overflow-wrap: anywhere; }
.quality-lead { margin-bottom: 14px !important; }
.quality-list { margin: 0; padding: 0; list-style: none; display: grid; gap: 10px; }
.quality-list li { border: 1px solid var(--line); border-radius: 8px; padding: 14px; background: #fbfcfd; }
.quality-list strong { display: block; margin-bottom: 4px; }
.quality-list span { display: inline-block; color: var(--warm); font-weight: 800; margin-bottom: 6px; }
.quality-list p { font-size: 14px; }
.detail-page { max-width: 860px; }
.detail-article { padding: 28px 44px 52px; }
.detail-article h1 { margin-bottom: 18px; }
.detail-lead { margin: 0 0 24px; color: var(--ink); font-size: 17px; line-height: 1.75; }
.detail-article section { border-top: 1px solid var(--line); padding: 22px 0; }
.detail-article h2 { margin: 0 0 10px; font-size: 19px; }
.detail-image { width: min(640px, 100%); max-height: 360px; object-fit: cover; border-radius: 8px; border: 1px solid var(--line); margin: 4px 0 22px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { border-bottom: 1px solid var(--line); padding: 9px 8px; text-align: left; vertical-align: top; }
th { width: 140px; color: var(--muted); font-weight: 700; }
.detail-article ul { margin: 8px 0 0 18px; padding: 0; color: var(--ink); }
.detail-article li { margin: 6px 0; }
/* 시안 A 구조: 전폭 기사 행 + 4단 슬롯 그리드 (sectioned 모드 본지) */
.story-list { padding: 6px 44px 34px; }
.story { position: relative; padding: 18px 0 20px; border-bottom: 1px solid var(--line); }
.story:last-child { border-bottom: 0; }
.story-no { position: absolute; top: 18px; right: 0; font-size: 34px; font-weight: 900; color: #c1c7cd; opacity: .35; line-height: 1; }
.story h3 { margin: 6px 0 4px; font-size: 22px; line-height: 1.4; }
.story h3 a { color: var(--ink); text-decoration: none; }
.story h3 a:hover { color: var(--accent); text-decoration: underline; }
.story-line { margin: 0 0 14px; color: #333d4b; font-size: 15.5px; }
/* 썸네일 옵션: 텍스트 좌 + 대표 이미지 우, 이미지 위에 번호 배지 */
.story.has-thumb { display: grid; grid-template-columns: 1fr 200px; gap: 22px; align-items: start; }
.story-thumb { position: relative; }
.story-thumb img { width: 100%; aspect-ratio: 16 / 10; object-fit: cover; border-radius: 8px; border: 1px solid var(--line); display: block; }
.story-thumb span { position: absolute; top: 8px; left: 8px; background: rgba(17,23,33,.82); color: #fff; font-size: 12px; font-weight: 800; padding: 2px 8px; border-radius: 4px; }

/* 콜아웃(pull quote): 본문에서 그대로 인용한 한 문장. --sc는 섹션 색을 상속받는다 */
.story-hook { margin: 2px 0 0; padding: 2px 0 2px 14px; border-left: 3px solid var(--sc, var(--accent)); color: #333d4b; font-size: 16.5px; font-weight: 500; line-height: 1.65; max-width: 720px; }
.story-summary { margin: 0 0 10px; color: #333d4b; }
.slots { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--line); border: 1px solid var(--line); }
.slot { background: #fff; padding: 14px 16px; }
.slot h4 { margin: 0 0 6px; font-size: 12.5px; font-weight: 700; color: var(--muted); letter-spacing: .06em; }
.slot p { margin: 0 0 8px; font-size: 14px; line-height: 1.65; color: #333d4b; }
.slot p:last-child { margin-bottom: 0; }
.slot ul { margin: 4px 0 8px 18px; padding: 0; font-size: 14px; color: #333d4b; }
.slot li { margin: 3px 0; }
.slot.impact { background: #fbfbf6; }
.story-links { margin: 12px 0 0; font-size: 14px; }

@media (max-width: 720px) {
  .story-list { padding: 6px 22px 28px; }
  .slots { grid-template-columns: 1fr; }
  .story-no { font-size: 26px; }
  .masthead { grid-template-columns: 1fr; padding: 30px 22px 22px; }
  h1 { font-size: 27px; }
  .summary { grid-template-columns: 1fr; }
  .summary div { padding: 14px 22px; border-right: 0; border-bottom: 1px solid var(--line); }
  .editors-pick { padding: 28px 22px 22px; }
  .section-title { display: block; }
  .feature-grid, .card-grid { grid-template-columns: 1fr; }
  .card-grid { padding: 20px 22px 28px; }
  .group-header { padding: 24px 22px 10px; }
  .article { grid-template-columns: 1fr; padding: 24px 22px; gap: 6px; }
  .quality { padding: 24px 22px 36px; }
  .detail-nav { padding: 20px 22px 0; }
  .detail-article { padding: 24px 22px 40px; }
  th { width: 112px; }
}
"""

# ---- 테마 오버레이 (마크업 공통, CSS만 교체) ----

# 시안 A: 미니멀 에디토리얼
_EDITORIAL_CSS = """
h1 { font-weight: 800; letter-spacing: -0.02em; }
.masthead { border-bottom: 2px solid var(--ink); }
.radar-hint { margin: 2px 0 0; color: var(--muted); font-size: 12.5px; }
.article-number { border-radius: 4px; background: var(--ink); font-weight: 900; }
/* 섹션 컬러 코딩: 헤더 밑줄·번호 칩·배지가 섹션 색을 따른다 */
.article-group.section-frontier { --sc: var(--frontier); }
.article-group.section-open { --sc: var(--open); }
.article-group.section-research { --sc: var(--research); }
.article-group.section-tooling { --sc: var(--tooling); }
.article-group[class*="section-"] .group-header { border-bottom: 3px solid var(--sc); background: #fff; }
.article-group[class*="section-"] .group-header h2 { font-weight: 800; }
.article-group[class*="section-"] .article-number { background: var(--sc); }
.article-group[class*="section-"] .detail-link { color: var(--sc); }
.article-group[class*="section-"] .thumb-placeholder { background: color-mix(in srgb, var(--sc) 10%, #fff); color: var(--sc); }
.article-group[class*="section-"] .slot h4 { color: var(--sc); }
.article-group[class*="section-"] .story-no { color: var(--sc); opacity: .28; }
/* 상세 아티클(개별 PNG)도 섹션 색 상속 */
.detail-article.section-frontier { --sc: var(--frontier); }
.detail-article.section-open { --sc: var(--open); }
.detail-article.section-research { --sc: var(--research); }
.detail-article.section-tooling { --sc: var(--tooling); }
.detail-article[class*="section-"] .article-number { background: var(--sc); }
.detail-article[class*="section-"] h1 { border-bottom: 3px solid var(--sc); padding-bottom: 14px; }
.detail-article[class*="section-"] section h2 { color: var(--sc); font-size: 15px; letter-spacing: .04em; }
.detail-article[class*="section-"] .detail-lead { background: #f8f9fa; border-left: 3px solid var(--sc); padding: 14px 18px; }
"""

# 시안 B: 카드 매거진 — 다크 헤더 + 컬러 칩 + 라운드 카드
_MAGAZINE_CSS = """
body { background: #eef1f6; }
h1 { font-weight: 900; letter-spacing: -0.02em; }
.masthead { background: linear-gradient(135deg, #1a1f2e 0%, #2b3350 100%); border-bottom: 0; }
.masthead h1 { color: #fff; }
.kicker { color: #9db2ff; }
.lead { color: rgba(255,255,255,.78); }
.radar-hint { color: rgba(255,255,255,.55); }
.feature-card, .article-card { border: 0; border-radius: 16px; box-shadow: 0 2px 12px rgba(26,31,46,.09); }
.feature-card { border-top: 4px solid var(--accent); }
.article-number { border-radius: 999px; font-weight: 800; }
.article-group.section-frontier { --sc: var(--frontier); }
.article-group.section-open { --sc: var(--open); }
.article-group.section-research { --sc: var(--research); }
.article-group.section-tooling { --sc: var(--tooling); }
.article-group[class*="section-"] .group-header { background: #fff; border-bottom: 0; }
.article-group[class*="section-"] .group-header h2 { color: var(--sc); font-weight: 900; }
.article-group[class*="section-"] .article-number { background: var(--sc); }
.article-group[class*="section-"] .detail-link { color: var(--sc); }
.article-group[class*="section-"] .slot h4 { color: var(--sc); }
.article-group[class*="section-"] .story-no { color: var(--sc); opacity: .25; }
.story { border-bottom: 0; background: #fff; border-radius: 16px; box-shadow: 0 2px 12px rgba(26,31,46,.07); padding: 22px 26px; margin-bottom: 18px; }
.story-no { right: 22px; }
.detail-article.section-frontier { --sc: var(--frontier); }
.detail-article.section-open { --sc: var(--open); }
.detail-article.section-research { --sc: var(--research); }
.detail-article.section-tooling { --sc: var(--tooling); }
.detail-article[class*="section-"] .article-number { background: var(--sc); }
.detail-article[class*="section-"] section h2 { color: var(--sc); }
.detail-article[class*="section-"] .detail-lead { background: color-mix(in srgb, var(--sc) 7%, #fff); border-left: 3px solid var(--sc); padding: 14px 18px; border-radius: 10px; }
"""

# 시안 C: 금융 리포트 — 네이비/골드, 절제된 섹션 색
_REPORT_CSS = """
:root { --frontier: #0b1f3a; --open: #1d6b4f; --research: #5b3a8c; --tooling: #8c4a1d; --accent: #0b1f3a; --warm: #b8963e; }
body { background: #e9ebef; }
.newsletter { background: #fdfcf9; }
h1 { font-weight: 800; letter-spacing: -0.01em; }
.masthead { background: #0b1f3a; border-bottom: 0; }
.masthead h1 { color: #fff; }
.kicker { color: #b8963e; letter-spacing: .14em; }
.lead { color: rgba(255,255,255,.72); }
.radar-hint { color: rgba(255,255,255,.5); }
.section-title p { color: #b8963e; }
.feature-card { background: #f4f1ea; border: 1px solid #e2dbc9; border-radius: 0; }
.article-card { border-radius: 0; }
.article-number { border-radius: 0; background: #0b1f3a; }
.article-group.section-frontier { --sc: var(--frontier); }
.article-group.section-open { --sc: var(--open); }
.article-group.section-research { --sc: var(--research); }
.article-group.section-tooling { --sc: var(--tooling); }
.article-group[class*="section-"] .group-header { background: transparent; border-bottom: 2px solid var(--sc); }
.article-group[class*="section-"] .group-header h2 { color: var(--sc); }
.article-group[class*="section-"] .article-number { background: var(--sc); }
.article-group[class*="section-"] .slot h4 { color: var(--sc); }
.story { border-bottom: 1px dotted var(--line); }
.slot.impact { background: #f6f3ec; }
.detail-article.section-frontier { --sc: var(--frontier); }
.detail-article.section-open { --sc: var(--open); }
.detail-article.section-research { --sc: var(--research); }
.detail-article.section-tooling { --sc: var(--tooling); }
.detail-article[class*="section-"] .article-number { background: var(--sc); }
.detail-article[class*="section-"] h1 { border-bottom: 3px solid var(--sc); padding-bottom: 14px; }
.detail-article[class*="section-"] section h2 { color: var(--sc); }
.detail-article[class*="section-"] .detail-lead { background: #f6f3ec; border-left: 3px solid #b8963e; padding: 14px 18px; }
"""

# 테마 이름 → 오버레이. classic은 기존 이미지(기반 CSS 그대로, 폰트만 Pretendard).
THEMES: dict[str, str] = {
    "classic": "",
    "editorial": _EDITORIAL_CSS,
    "magazine": _MAGAZINE_CSS,
    "report": _REPORT_CSS,
}


def render_css(theme: str = "editorial") -> str:
    return _BASE_CSS + THEMES.get(theme, _EDITORIAL_CSS)
