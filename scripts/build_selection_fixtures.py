"""Freeze past builds into regression fixtures for the selection stage.

Every selection change so far fixed one week and quietly broke another, because
the only way to tell was to look at the newest newsletter. This turns the last
few months of real builds into a test: each fixture pairs a week's actual
candidate pool (with the importance/topic_key the scoring model gave it that week)
against the stories that week's issue should have carried.

Run after a build to add the new week:

    .venv/Scripts/python.exe scripts/build_selection_fixtures.py

Fixtures land in tests/fixtures/selection/ and are committed, so the tests do not
depend on outputs/ being present.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"
FIXTURES = ROOT / "tests" / "fixtures" / "selection"


def _normalize(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _legacy_hn_boost(metrics: dict) -> float:
    """The boost sectioned mode had already folded into the recorded score.

    `ranking[].importance` is post-boost, so replaying it as a base score would
    count Hacker News twice — once in the frozen number and again in heat.
    """
    try:
        points = int(metrics.get("hn_points") or 0)
    except (TypeError, ValueError):
        return 0.0
    if points < 100:
        return 0.0
    return round(min(25.0, 9.0 * math.log10(points / 50.0)), 3)


def build_fixture(build_dir: Path) -> dict | None:
    data = build_dir / "data"
    report_path = data / "generation_report.json"
    crawled_path = data / "crawled_articles.json"
    if not report_path.exists() or not crawled_path.exists():
        return None
    report = json.loads(report_path.read_text(encoding="utf-8"))
    ranking = report.get("ranking")
    if not ranking:
        return None
    crawled = json.loads(crawled_path.read_text(encoding="utf-8"))
    # heat mode never folded the boost into its recorded importance; only the
    # sectioned builds need it taken back out.
    mode = str(report.get("mode", ""))
    legacy_boost = _legacy_hn_boost if mode.startswith("sectioned") else (lambda _m: 0.0)

    # `ranking` truncates titles to 80 chars; match back to the full article so the
    # fixture keeps the URL and metrics the selector actually needs.
    by_prefix: dict[str, dict] = {}
    for article in crawled:
        by_prefix.setdefault(_normalize(article.get("title", ""))[:70], article)

    articles = []
    for row in ranking:
        full = by_prefix.get(_normalize(row.get("title", ""))[:70])
        if not full:
            continue
        articles.append(
            {
                "id": full.get("id", ""),
                "title": full.get("title", ""),
                "url": full.get("url", ""),
                "source_id": full.get("source_id", ""),
                "source_name": full.get("source_name", ""),
                "summary": (full.get("summary") or "")[:600],
                "body_len": len(full.get("body") or ""),
                "published_at": full.get("published_at"),
                "metrics": full.get("metrics") or {},
                "source_weight": full.get("source_weight", 1.0),
                "authority_tier": full.get("authority_tier", 0.5),
                "panel": full.get("panel", "curator"),
                # what the scoring model said that week, with the legacy HN boost
                # removed so heat is not counted on top of itself
                "importance": round(
                    float(row.get("importance", 0)) - legacy_boost(full.get("metrics") or {}), 3
                ),
                "importance_recorded": row.get("importance", 0),
                "topic_key": row.get("topic_key", ""),
                "section": row.get("section", ""),
                "was_selected": bool(row.get("selected")),
            }
        )
    if len(articles) < 10:
        return None
    return {
        "build": build_dir.name,
        "mode": report.get("mode", ""),
        "limit": report.get("selected_count", 10),
        "section_quotas": report.get("section_quotas", {}),
        "articles": articles,
        # Filled in by hand in the committed fixture: the stories this week's issue
        # had to carry. Left empty here so regenerating never erases them.
        "must_include": [],
    }


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    for build_dir in sorted(OUTPUTS.iterdir()):
        if not build_dir.is_dir():
            continue
        fixture = build_fixture(build_dir)
        if fixture is None:
            skipped += 1
            continue
        target = FIXTURES / f"{build_dir.name}.json"
        # Never clobber hand-written expectations.
        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            fixture["must_include"] = existing.get("must_include", [])
        target.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        written += 1
        print(f"  {build_dir.name}: {len(fixture['articles'])} articles")
    print(f"\n{written} fixtures written, {skipped} builds skipped (no ranking data)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
