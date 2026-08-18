"""기존 산출물의 기사를 새 표/이미지 추출 로직으로 재수집·재생성한다.

collectors.py가 벤치마크 표(_extract_tables)를 놓치거나 정보량 0인 og:image
히어로를 그대로 대표 이미지로 쓰던 시절에 만들어진 산출물을 대상으로, 원문을
다시 긁어 body/image_urls를 새 로직으로 교체하고 LLM 보강(enrich/overview/
grounding)과 렌더링을 처음부터 다시 돌린다. 원본 폴더는 건드리지 않고 사본
위에서 작업한다.

사용법:
    uv run python scripts/reenrich_details.py <원본_산출물_폴더> <새_산출물_폴더>
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

# 본문이 이 길이보다 짧으면 재수집이 실패했거나(차단/타임아웃) 원래 본문보다
# 못한 것으로 보고 기존 body를 유지한다.
MIN_BODY_LEN = 500


def _period_from_folder_name(name: str) -> tuple[datetime, datetime]:
    """폴더명(YYYY-MM-DD_weekly_ai_newsletter)에서 기간을 복원한다 (cli.py의
    _load_output_data와 동일한 규칙)."""
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
    if len(sys.argv) != 3:
        print("사용법: uv run python scripts/reenrich_details.py <원본_산출물_폴더> <새_산출물_폴더>")
        return 1
    source_dir = Path(sys.argv[1])
    target_dir = Path(sys.argv[2])
    if not source_dir.exists():
        print(f"[오류] 원본 폴더가 없습니다: {source_dir}")
        return 1
    if target_dir.exists():
        print(f"[오류] 대상 폴더가 이미 있습니다: {target_dir}")
        return 1

    print(f"[1/6] 사본 생성: {source_dir} -> {target_dir}")
    shutil.copytree(source_dir, target_dir)

    from ai_newsletter.config import load_environment

    load_environment(None)

    from ai_newsletter.collectors import Collector
    from ai_newsletter.llm import enrich_with_openai, generate_weekly_overview, grounding_flags
    from ai_newsletter.models import Article, Issue, RankedArticle
    from ai_newsletter.render import write_package
    from ai_newsletter.style_lint import lint_selection
    from ai_newsletter.usage import usage

    data_dir = target_dir / "data"
    selected_rows: list[dict] = json.loads(
        (data_dir / "selected_articles.json").read_text(encoding="utf-8")
    )
    crawled_path = data_dir / "crawled_articles.json"
    crawled_rows: list[dict] = (
        json.loads(crawled_path.read_text(encoding="utf-8")) if crawled_path.exists() else []
    )
    crawled_by_id = {row.get("id"): row for row in crawled_rows}

    print(f"[2/6] 기사 {len(selected_rows)}건 원문 재수집 (새 표/이미지 추출 로직)")
    collector = Collector()
    try:
        for row in selected_rows:
            url = row.get("url", "")
            label = (row.get("korean_title") or row.get("title") or url)[:60]
            old_len = len(row.get("body") or "")
            try:
                body, images, info_images, _published = collector.fetch_article_detail(url)
            except Exception as exc:  # 원문 재수집 실패는 건너뛰고 기존 데이터를 유지
                print(f"  [경고] 재수집 실패, 건너뜀: {label} ({exc})")
                continue
            if not body:
                print(f"  [경고] 빈 응답, 건너뜀: {label}")
                continue
            new_len = len(body)
            has_table = "[표]" in body
            body_replaced = new_len >= MIN_BODY_LEN
            if body_replaced:
                row["body"] = body
            images_replaced = bool(images)
            if images_replaced:
                row["image_urls"] = images[:3]
            row["info_image_urls"] = info_images[:3]
            crawled_row = crawled_by_id.get(row.get("id"))
            if crawled_row is not None:
                if body_replaced:
                    crawled_row["body"] = row["body"]
                if images_replaced:
                    crawled_row["image_urls"] = row["image_urls"]
                crawled_row["info_image_urls"] = row["info_image_urls"]
            print(
                f"  {label}: 본문 {old_len} -> {new_len}자 "
                f"({'교체' if body_replaced else '유지 (짧음)'}, {'표 포함' if has_table else '표 없음'})"
            )
    finally:
        collector.close()

    print("[3/6] 이미지 재선정을 위해 local_image/image_credit/local_info_images 리셋")
    for row in selected_rows:
        row["local_image"] = ""
        row["image_credit"] = ""
        row["local_info_images"] = []

    articles = [RankedArticle(**row) for row in selected_rows]

    print("[4/6] LLM 보강 (enrich_with_openai / generate_weekly_overview / grounding_flags)")
    usage.reset()
    articles = enrich_with_openai(articles, structured=True)
    overview = generate_weekly_overview(articles)
    flags = grounding_flags(articles)
    style_flags = lint_selection([a.model_dump(mode="json") for a in articles])
    if flags:
        print(f"  [경고] 근거 검증: 원문에 없는 숫자가 {len(flags)}개 섹션에서 발견")

    report_path = data_dir / "generation_report.json"
    report: dict = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    report["overview"] = overview
    report["grounding_flags"] = flags
    report["style_flags"] = style_flags
    report["reenriched_from"] = str(source_dir)
    report["usage"] = usage.summary()

    print("[5/6] selected_articles.json / crawled_articles.json 저장")
    selected_dump = [a.model_dump(mode="json") for a in articles]
    (data_dir / "selected_articles.json").write_text(
        json.dumps(selected_dump, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 매칭되는 crawled row가 없던 selected 기사는 후보 목록에 없어도 렌더링에는
    # 지장이 없지만(donor 조회용일 뿐), crawled_articles.json 자체는 원래 목록의
    # 순서를 그대로 유지한다.
    if crawled_by_id:
        crawled_dump = list(crawled_by_id.values())
    else:
        crawled_dump = selected_dump
    (data_dir / "crawled_articles.json").write_text(
        json.dumps(crawled_dump, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("[6/6] 재렌더 + PNG 캡처")
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
        overview=overview,
        capture=True,
        theme=str(report.get("theme") or "editorial"),
        thumbnails=bool(report.get("thumbnails", True)),
    )
    print(f"완료: {package.output_dir / 'newsletter.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
