from datetime import datetime, timezone

from ai_newsletter.benchmark import compare_with_reference
from ai_newsletter.models import Article


def _ref(idx: int, source_id: str, title: str) -> Article:
    return Article(
        id=f"r{idx:02d}",
        source_id=source_id,
        source_name=source_id,
        title=title,
        url=f"https://ref.example/{idx}",
        published_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        summary=title,
    )


def test_alignment_and_missed_topics():
    selected = [
        {"title": "Claude Sonnet 5, 에이전트 전환의 승부수", "topic_key": "claude-sonnet-5"},
        {"title": "완전히 내부적인 자체 기획 기사", "topic_key": "internal-only"},
    ]
    reference = [
        _ref(1, "ref-techcrunch-ai", "Anthropic launches Claude Sonnet 5 with agent focus"),
        _ref(2, "ref-theverge-ai", "Claude Sonnet 5 hands-on: agents everywhere"),
        # GPT-6 프리뷰를 두 매체가 다뤘지만 우리는 안 뽑음 → missed
        _ref(3, "ref-techcrunch-ai", "OpenAI previews GPT-6 with new pricing"),
        _ref(4, "ref-venturebeat-ai", "GPT-6 preview: what enterprises should know"),
    ]
    result = compare_with_reference(selected, reference)
    assert result["alignment_rate"] == 0.5
    aligned = result["aligned"][0]
    assert aligned["topic_key"] == "claude-sonnet-5"
    assert len(aligned["reference_outlets"]) >= 2
    assert [m["topic_key"] for m in result["missed_hot_topics"]] == ["gpt-6"]
    assert result["ours_only"][0]["topic_key"] == "internal-only"


def test_empty_reference_is_safe():
    result = compare_with_reference([{"title": "t", "topic_key": "k"}], [])
    assert result["alignment_rate"] == 0.0
    assert result["missed_hot_topics"] == []
