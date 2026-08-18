"""기존 산출물의 이미지만 새 로직(og:image 우선 대표 이미지 + 별도 정보성
이미지)으로 재수집·재캡처한다. 본문 텍스트(enrich 결과)는 절대 건드리지 않고
LLM도 호출하지 않는다 — 순수하게 collectors.fetch_article_detail을 다시 돌려
image_urls / info_image_urls만 갱신한 뒤 이미지를 재캡처하고 재렌더한다.

이전 세대 산출물은 `_rank_images`가 본문 이미지에 점수를 얹어 기자 프로필
사진 같은 것이 og:image 히어로를 밀어내는 부작용을 안고 있었다(대표적으로
MarkTechPost 기사). 그 로직이 원복되고 나서, 이미 만들어 둔 산출물에도 같은
수정을 적용하려면 원문을 다시 긁어 이미지 필드만 바꾸면 된다 — LLM 보강까지
다시 돌릴 필요는 없다.

scripts/reenrich_details.py의 패턴(truststore 주입, load_environment, 폴더명
에서 기간 복원, report에서 Issue 복원, write_package 호출)을 재사용하되, 이
스크립트는 사본을 만들지 않고 대상 폴더를 in-place로 수정한다.

사용법:
    uv run python scripts/backfill_info_images.py <산출물_폴더>
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

# truststore가 없는 최소 설치에서도 동작해야 하므로 조용히 넘어간다 — cli.py의
# 같은 패턴을 따른다 (사내 프록시의 TLS 인터셉션 대응).
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _period_from_folder_name(name: str) -> tuple[datetime, datetime]:
    """폴더명(YYYY-MM-DD_weekly_ai_newsletter)에서 기간을 복원한다 (cli.py의
    _load_output_data / reenrich_details.py와 동일한 규칙)."""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", name)
    if match:
        period_end = datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc
        )
    else:
        period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(days=7)
    return period_start, period_end


def main() -> int:
    if len(sys.argv) != 2:
        print("사용법: uv run python scripts/backfill_info_images.py <산출물_폴더>")
        return 1
    target_dir = Path(sys.argv[1])
    if not target_dir.exists():
        print(f"[오류] 폴더가 없습니다: {target_dir}")
        return 1

    from ai_newsletter.config import load_environment

    load_environment(None)

    from ai_newsletter.collectors import Collector
    from ai_newsletter.models import Article, Issue, RankedArticle
    from ai_newsletter.render import write_package

    data_dir = target_dir / "data"
    selected_rows: list[dict] = json.loads(
        (data_dir / "selected_articles.json").read_text(encoding="utf-8")
    )
    crawled_path = data_dir / "crawled_articles.json"
    crawled_rows: list[dict] = (
        json.loads(crawled_path.read_text(encoding="utf-8")) if crawled_path.exists() else []
    )
    crawled_by_id = {row.get("id"): row for row in crawled_rows}

    print(f"[1/4] 기사 {len(selected_rows)}건 이미지 필드만 재수집 (본문은 유지)")
    collector = Collector()
    try:
        for row in selected_rows:
            url = row.get("url", "")
            label = (row.get("korean_title") or row.get("title") or url)[:60]
            try:
                _body, images, info_images, _published = collector.fetch_article_detail(url)
            except Exception as exc:  # 원문 재수집 실패는 건너뛰고 기존 이미지를 유지
                print(f"  [경고] 재수집 실패, 건너뜀: {label} ({exc})")
                continue
            row["image_urls"] = images[:3]
            row["info_image_urls"] = info_images[:3]
            crawled_row = crawled_by_id.get(row.get("id"))
            if crawled_row is not None:
                crawled_row["image_urls"] = row["image_urls"]
                crawled_row["info_image_urls"] = row["info_image_urls"]
            domain = urlparse(images[0]).netloc if images else "(없음)"
            print(f"  {label}: 대표 이미지 후보 1순위 도메인={domain}, 정보 이미지 {len(info_images)}개")
    finally:
        collector.close()

    print("[2/4] local_image/image_credit/local_info_images 리셋 (재캡처를 위해)")
    for row in selected_rows:
        row["local_image"] = ""
        row["image_credit"] = ""
        row["local_info_images"] = []

    articles = [RankedArticle(**row) for row in selected_rows]

    print("[3/4] selected_articles.json / crawled_articles.json 저장")
    selected_dump = [a.model_dump(mode="json") for a in articles]
    (data_dir / "selected_articles.json").write_text(
        json.dumps(selected_dump, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if crawled_by_id:
        crawled_dump = list(crawled_by_id.values())
    else:
        crawled_dump = selected_dump
    (data_dir / "crawled_articles.json").write_text(
        json.dumps(crawled_dump, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("[4/4] 재캡처 + 재렌더 (LLM 호출 없음)")
    report_path = data_dir / "generation_report.json"
    report: dict = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    period_start, period_end = _period_from_folder_name(target_dir.name)
    issues = [Issue(**row) for row in report.get("issues", []) if isinstance(row, dict)]
    candidates = [Article(**row) for row in crawled_dump]

    package = write_package(
        target_dir,
        period_start,
        period_end,
        candidates,
        articles,
        report,
        issues=issues,
        overview=report.get("overview", ""),
        capture=True,
        theme=str(report.get("theme") or "editorial"),
        thumbnails=bool(report.get("thumbnails", True)),
    )
    print(f"완료: {package.output_dir / 'newsletter.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
