"""grounding_flags 단위·값 정규화 회귀 테스트.

배경: 'more than 5 million'을 '500만 건 이상'으로 올바르게 번역해도 기존 문자열
대조는 '500'이 원문에 없다며 오탐을 냈다 — 번역을 처벌하는 유인 구조였다.
_numeric_values/_numeric_expr_values가 값 기준 비교를 더해 이 케이스를 구제하되,
실제로 없는 숫자(할루시네이션)는 여전히 잡아야 한다.
"""

from __future__ import annotations

from ai_newsletter.llm import _numeric_values, grounding_flags
from ai_newsletter.models import RankedArticle


def _article(**overrides) -> RankedArticle:
    base = dict(
        id="1",
        source_id="src",
        source_name="Example",
        title="t",
        url="https://example.com",
        summary="",
        body="",
        detail_sections=[],
    )
    base.update(overrides)
    return RankedArticle(**base)


def test_numeric_values_parses_english_and_korean_units():
    assert _numeric_values("more than 5 million interactions") == {5_000_000.0}
    assert _numeric_values("500만 건 이상") == {5_000_000.0}
    assert _numeric_values("roughly 200 projects") == {200.0}
    assert _numeric_values("약 200건") == {200.0}
    assert _numeric_values("250,000 users") == {250_000.0}
    assert _numeric_values("10k tokens") == {10_000.0}
    assert _numeric_values("25만") == {250_000.0}
    assert _numeric_values("5백만") == {5_000_000.0}


def test_grounding_allows_translated_units_without_unmatched():
    article = _article(
        summary=(
            "Customer agent interactions have grown to more than 5 million "
            "since launch."
        ),
        body=(
            "Cooke, a seafood company, used Claude alongside monday to "
            "automate reporting across roughly 200 active and proposed "
            "projects and 130 contracts."
        ),
        detail_sections=[
            {
                "heading": "핵심 브리핑",
                "body": (
                    "출시 뒤 고객의 에이전트 상호작용은 500만 건 이상이다. "
                    "수산기업 Cooke는 Claude와 monday를 함께 써서 약 200건의 "
                    "프로젝트와 130건의 계약 보고를 자동화했다."
                ),
            }
        ],
    )
    flags = grounding_flags([article])
    assert flags == []


def test_grounding_still_flags_invented_numbers():
    article = _article(
        summary="The company shipped a small update with no major numbers.",
        body="The company shipped a small update with no major numbers.",
        detail_sections=[
            {
                "heading": "핵심 브리핑",
                "body": "이번 업데이트로 매출이 999억 원 증가했다고 밝혔다.",
            }
        ],
    )
    flags = grounding_flags([article])
    assert len(flags) == 1
    assert "999" in flags[0]["unmatched_numbers"][0] or any(
        "999" in n for n in flags[0]["unmatched_numbers"]
    )


def test_grounding_rejects_value_that_does_not_match_within_tolerance():
    article = _article(
        summary="more than 5 million interactions",
        body="more than 5 million interactions",
        detail_sections=[
            {
                "heading": "핵심 브리핑",
                "body": "상호작용은 600만 건 이상이다.",  # 6e6 vs source 5e6 — 오차 초과
            }
        ],
    )
    flags = grounding_flags([article])
    assert len(flags) == 1
    assert "600" in flags[0]["unmatched_numbers"]
