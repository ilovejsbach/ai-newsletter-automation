"""주차 간 이력 참조(history.py) 테스트 — 네트워크 없이 픽스처 폴더로 검증한다."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from ai_newsletter.history import (
    REPEAT_PENALTY,
    SIMILAR_PENALTY,
    apply_history,
    load_history,
)
from ai_newsletter.models import RankedArticle


def _past_row(url: str, title: str, korean_title: str = "", publisher: str = "") -> dict:
    return {"url": url, "title": title, "korean_title": korean_title, "publisher": publisher}


def _write_week(outputs: Path, week: str, rows: list[dict], version: str = "") -> None:
    folder = outputs / f"{week}_weekly_ai_newsletter{version}"
    (folder / "data").mkdir(parents=True)
    (folder / "data" / "selected_articles.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8"
    )


def _candidate(
    url: str,
    title: str,
    published: str | None = None,
    publisher: str = "",
    score: float = 60.0,
) -> RankedArticle:
    return RankedArticle(
        id=url[-12:],
        source_id="feed",
        source_name="Feed",
        title=title,
        url=url,
        publisher=publisher,
        published_at=datetime.fromisoformat(published).replace(tzinfo=timezone.utc)
        if published
        else None,
        score=score,
    )


def test_load_picks_latest_version_and_recent_weeks_only(tmp_path: Path) -> None:
    _write_week(tmp_path, "2026-08-18", [_past_row("https://a.com/1", "Old story")])
    _write_week(tmp_path, "2026-08-25", [_past_row("https://a.com/v1", "V1 story")])
    _write_week(tmp_path, "2026-08-25", [_past_row("https://a.com/v2", "V2 story")], "_v2")
    # 당일(빌드 중) 폴더와 미래 폴더는 이력에서 제외된다.
    _write_week(tmp_path, "2026-09-01", [_past_row("https://a.com/today", "Today")])

    index = load_history(tmp_path, weeks=1, before=date(2026, 9, 1))
    assert index.weeks == ["2026-08-25"]
    assert [e.url for e in index.entries] == ["https://a.com/v2"]

    index2 = load_history(tmp_path, weeks=4, before=date(2026, 9, 1))
    assert index2.weeks == ["2026-08-25", "2026-08-18"]


def test_load_survives_missing_and_broken_files(tmp_path: Path) -> None:
    assert not load_history(tmp_path / "없는폴더", weeks=4)
    folder = tmp_path / "2026-08-25_weekly_ai_newsletter" / "data"
    folder.mkdir(parents=True)
    (folder / "selected_articles.json").write_text("깨진 JSON {", encoding="utf-8")
    assert not load_history(tmp_path, weeks=4, before=date(2026, 9, 1))


def test_exact_url_repeat_is_penalized(tmp_path: Path) -> None:
    _write_week(
        tmp_path, "2026-08-25", [_past_row("https://a.com/story", "Big launch", "빅 런치")]
    )
    index = load_history(tmp_path, weeks=4, before=date(2026, 9, 1))
    article = _candidate("https://a.com/story/", "Big launch again", score=60.0)

    hits = apply_history([article], index)
    assert article.score == 60.0 - REPEAT_PENALTY
    assert article.score_breakdown["history_repeat"] == -REPEAT_PENALTY
    assert hits[0]["kind"] == "repeat"
    assert hits[0]["past_week"] == "2026-08-25"


def test_same_publisher_similar_title_is_penalized(tmp_path: Path) -> None:
    _write_week(
        tmp_path,
        "2026-08-25",
        [_past_row("https://blog.a.com/launch", "Acme releases SuperModel 3 weights", publisher="a.com")],
    )
    index = load_history(tmp_path, weeks=4, before=date(2026, 9, 1))
    # 같은 발행사가 같은 소식을 다른 URL로 — 발행일이 지난 게재 이전이므로 유사 재탕.
    article = _candidate(
        "https://blog.a.com/launch-mirror",
        "Acme releases SuperModel 3 weights today",
        published="2026-08-24T00:00:00",
        publisher="a.com",
    )

    hits = apply_history([article], index)
    assert article.score == 60.0 - SIMILAR_PENALTY
    assert hits[0]["kind"] == "similar"


def test_newer_coverage_is_marked_followup_without_penalty(tmp_path: Path) -> None:
    _write_week(
        tmp_path,
        "2026-08-25",
        [_past_row("https://a.com/launch", "Acme releases SuperModel 3 Turbo weights", "Acme, SuperModel 3 공개")],
    )
    index = load_history(tmp_path, weeks=4, before=date(2026, 9, 1))
    # 발행사가 다르므로 고유 토큰 3개(acme, supermodel3, turbo) 이상이어야 후속 판정.
    article = _candidate(
        "https://b.com/followup",
        "Acme SuperModel 3 Turbo adoption grows",
        published="2026-08-28T00:00:00",
        publisher="b.com",
    )

    hits = apply_history([article], index)
    assert article.score == 60.0
    assert article.followup_of == "2026-08-25호 'Acme, SuperModel 3 공개'"
    assert hits[0]["kind"] == "followup"


def test_weak_cross_publisher_overlap_is_ignored(tmp_path: Path) -> None:
    _write_week(tmp_path, "2026-08-25", [_past_row("https://a.com/launch", "Acme releases SuperModel 3 weights")])
    index = load_history(tmp_path, weeks=4, before=date(2026, 9, 1))
    # 겹침이 토큰 2개뿐이고 발행사도 다르면 후속으로 단정하지 않는다.
    article = _candidate(
        "https://b.com/other",
        "Acme SuperModel 3 rival benchmark drama",
        published="2026-08-28T00:00:00",
        publisher="b.com",
    )
    assert apply_history([article], index) == []
    assert article.followup_of == ""
    assert article.score == 60.0


def test_unrelated_article_is_untouched(tmp_path: Path) -> None:
    _write_week(tmp_path, "2026-08-25", [_past_row("https://a.com/1", "Acme releases SuperModel 3")])
    index = load_history(tmp_path, weeks=4, before=date(2026, 9, 1))
    article = _candidate("https://c.com/other", "Totally different quantum paper", score=55.0)

    hits = apply_history([article], index)
    assert hits == []
    assert article.score == 55.0
    assert article.followup_of == ""
