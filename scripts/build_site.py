"""뉴스레터 산출물을 정적 사이트(GitHub/GitLab Pages) 구조로 변환한다.

사용법: uv run python scripts/build_site.py <산출물_폴더> <사이트_저장소_루트>

내부망 깃랩은 외부 CDN이 안 되므로 모든 리소스를 저장소 안에서 서빙해야 한다:
- Pretendard 폰트를 사이트 루트 /fonts 에 한 번 내려받아 공유하고,
  각 주차 style.css의 CDN @import를 상대경로 @font-face로 치환한다.
- 원문 등 외부 하이퍼링크에는 target="_blank"를 붙인다 (iframe 안에서
  외부 사이트로 끌려가는 것을 방지).
- 마지막에 외부 리소스 참조가 0건인지 검증해 결과를 출력한다.

생성 구조:
  <site_root>/index.html          아카이브 목록 (주차 네비게이션)
  <site_root>/latest/index.html   최신 주차로 meta refresh (JS 불필요)
  <site_root>/fonts/              Pretendard (전 주차 공유)
  <site_root>/<YYYY-MM-DD>/       해당 주차 (index.html + articles/ + assets/)
"""

from __future__ import annotations

import re
import shutil
import sys
import urllib.request
from pathlib import Path

FONT_URL = (
    "https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9"
    "/packages/pretendard/dist/web/variable/woff2/PretendardVariable.woff2"
)
FONT_FACE = """@font-face {
  font-family: 'Pretendard';
  src: url('../../fonts/PretendardVariable.woff2') format('woff2-variations');
  font-weight: 45 920;
  font-display: swap;
}
"""
CDN_IMPORT_RE = re.compile(r"@import\s+url\(['\"]?https://cdn\.jsdelivr[^)]*\)\s*;?")
EXTERNAL_LINK_RE = re.compile(r'<a\s+(?![^>]*target=)([^>]*href="https?://[^"]*"[^>]*)>')
# 렌더링에 필요한 외부 리소스(하이퍼링크 제외): src/href의 스타일·이미지·스크립트, css url()
EXTERNAL_RESOURCE_RE = re.compile(
    r'(?:<(?:img|link|script|source)\b[^>]*(?:src|href)="https?://[^"]*")|(?:url\(\s*[\'"]?https?://)'
)


def build_week(output_dir: Path, site_root: Path) -> Path:
    date_match = re.match(r"\d{4}-\d{2}-\d{2}", output_dir.name)
    if not date_match:
        sys.exit(f"산출물 폴더명에서 날짜를 찾을 수 없음: {output_dir.name}")
    week = date_match.group(0)
    dest = site_root / week
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    shutil.copy(output_dir / "newsletter.html", dest / "index.html")
    for sub in ("articles", "assets"):
        if (output_dir / sub).exists():  # 구버전 산출물은 articles/가 없을 수 있다
            shutil.copytree(output_dir / sub, dest / sub)

    # 폰트: 사이트 루트에 1회 다운로드, 주차 간 공유.
    fonts = site_root / "fonts"
    fonts.mkdir(exist_ok=True)
    font_file = fonts / "PretendardVariable.woff2"
    if not font_file.exists():
        print("Pretendard 다운로드 중 (최초 1회)...")
        urllib.request.urlretrieve(FONT_URL, font_file)

    css_path = dest / "assets" / "style.css"
    css = css_path.read_text(encoding="utf-8")
    css, n = CDN_IMPORT_RE.subn(FONT_FACE, css)
    if n == 0 and "PretendardVariable" not in css:
        css = FONT_FACE + css
    css_path.write_text(css, encoding="utf-8")

    article_pages = sorted((dest / "articles").glob("*.html")) if (dest / "articles").exists() else []
    for html_path in [dest / "index.html", *article_pages]:
        html = html_path.read_text(encoding="utf-8")
        html = EXTERNAL_LINK_RE.sub(r'<a target="_blank" rel="noopener" \1>', html)
        html = _localize_remote_images(html, html_path, dest)
        html_path.write_text(html, encoding="utf-8")
    return dest


REMOTE_IMG_RE = re.compile(r'(<img\b[^>]*\bsrc=")(https?://[^"]+)(")')


def _localize_remote_images(html: str, html_path: Path, week_dir: Path) -> str:
    """원격 <img>를 assets/remote/로 내려받아 상대경로로 치환한다 (내부망 서빙용)."""
    import hashlib

    remote_dir = week_dir / "assets" / "remote"
    prefix = "assets/remote/" if html_path.parent == week_dir else "../assets/remote/"

    def _replace(match: re.Match[str]) -> str:
        url = match.group(2)
        ext = ".png" if ".png" in url.lower() else ".jpg"
        name = hashlib.sha1(url.encode()).hexdigest()[:16] + ext
        target = remote_dir / name
        if not target.exists():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=20).read()
                remote_dir.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except Exception as exc:  # noqa: BLE001 - 실패 시 원본 URL 유지 + 경고
                print(f"  [경고] 원격 이미지 다운로드 실패, URL 유지: {url[:70]} ({exc})")
                return match.group(0)
        return f"{match.group(1)}{prefix}{name}{match.group(3)}"

    return REMOTE_IMG_RE.sub(_replace, html)


def rebuild_indexes(site_root: Path) -> None:
    weeks = sorted(
        (d.name for d in site_root.iterdir() if d.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}$", d.name)),
        reverse=True,
    )
    items = "\n".join(
        f'      <li><a href="{w}/">{w} 주간 AI 뉴스레터</a></li>' for w in weeks
    )
    (site_root / "index.html").write_text(f"""<!doctype html>
<html lang="ko">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 주간 뉴스레터 아카이브</title>
<style>
  body {{ font-family:'Malgun Gothic',sans-serif; max-width:720px; margin:40px auto; padding:0 20px; color:#22252A; }}
  h1 {{ font-size:22px; border-bottom:2px solid #22252A; padding-bottom:10px; }}
  li {{ margin:10px 0; font-size:16px; }}
  a {{ color:#31518F; }}
</style></head>
<body>
  <h1>AI 주간 뉴스레터 아카이브</h1>
  <ul>
{items}
  </ul>
</body>
</html>
""", encoding="utf-8")
    if weeks:
        latest = site_root / "latest"
        latest.mkdir(exist_ok=True)
        (latest / "index.html").write_text(
            f'<!doctype html><meta charset="utf-8">'
            f'<meta http-equiv="refresh" content="0; url=../{weeks[0]}/">'
            f'<a href="../{weeks[0]}/">최신 호로 이동</a>\n',
            encoding="utf-8",
        )


def validate(site_root: Path) -> int:
    """렌더링용 외부 리소스 참조를 찾는다 (0이어야 내부망 서빙 가능)."""
    bad = 0
    for path in site_root.rglob("*"):
        if path.suffix not in (".html", ".css"):
            continue
        for match in EXTERNAL_RESOURCE_RE.finditer(path.read_text(encoding="utf-8")):
            print(f"  [외부 리소스] {path.relative_to(site_root)}: {match.group(0)[:80]}")
            bad += 1
    return bad


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    output_dir, site_root = Path(sys.argv[1]), Path(sys.argv[2])
    site_root.mkdir(parents=True, exist_ok=True)
    dest = build_week(output_dir, site_root)
    rebuild_indexes(site_root)
    bad = validate(site_root)
    print(f"생성: {dest}")
    print(f"외부 리소스 참조: {bad}건 {'— 내부망 서빙 가능' if bad == 0 else '— 치환 필요!'}")


if __name__ == "__main__":
    main()
