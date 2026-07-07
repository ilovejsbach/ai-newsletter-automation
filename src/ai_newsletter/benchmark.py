"""주간 벤치마크: 우리 선별 결과를 공신력 있는 외신 보도와 비교한다.

루브릭의 재귀 개선 루프:
  1. `benchmark <산출물폴더>` — 레퍼런스 패널(config/sources.reference.yaml)의
     최근 1주 보도를 수집해 우리 선정 토픽과의 일치도를 측정
  2. 결과는 산출물의 data/benchmark_report.json + 루트 benchmarks/history.jsonl에 누적
  3. "레퍼런스 다수가 다뤘는데 우리가 놓친 토픽"이 다음 루브릭 수정의 재료
  4. 수정 후 다음 주 다시 측정 — history로 주차별 추이 확인

매칭은 무토큰 휴리스틱: 엔티티 topic_key 일치 + 제목 토큰 겹침(자카드).
"""

from __future__ import annotations

import re
from collections import defaultdict

from .editorial_selection import _heuristic_topic_key
from .models import Article

_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "your", "will",
    "how", "why", "what", "when", "are", "was", "has", "have", "its", "can",
    "new", "more", "after", "over", "just", "now", "says", "said",
    "발표", "공개", "출시", "소개", "정리", "위한", "대한", "관련",
}


def _title_tokens(title: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}|[가-힣]{2,}", title.lower())
        if token not in _STOPWORDS
    }


def _entity_key(article: Article) -> str | None:
    key = _heuristic_topic_key(article)
    return None if key == article.id else key


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def compare_with_reference(
    selected_rows: list[dict],
    reference_articles: list[Article],
    *,
    jaccard_threshold: float = 0.3,
    min_shared_tokens: int = 3,
) -> dict[str, object]:
    """우리 선정(selected_articles.json rows)과 레퍼런스 보도의 토픽 일치도."""
    ref_by_key: dict[str, set[str]] = defaultdict(set)  # entity key -> outlets
    ref_key_sample: dict[str, str] = {}
    ref_rows: list[tuple[set[str], str, str]] = []  # (tokens, outlet, title)
    for ref in reference_articles:
        key = _entity_key(ref)
        if key:
            ref_by_key[key].add(ref.source_id)
            ref_key_sample.setdefault(key, ref.title)
        ref_rows.append((_title_tokens(ref.title), ref.source_id, ref.title))

    matched_keys: set[str] = set()
    aligned: list[dict[str, object]] = []
    ours_only: list[dict[str, object]] = []
    for row in selected_rows:
        title = str(row.get("title") or "")
        tokens = _title_tokens(title)
        key = str(row.get("topic_key") or "")
        outlets: set[str] = set(ref_by_key.get(key, set()))
        if key in ref_by_key:
            matched_keys.add(key)
        for ref_tokens, outlet, _ in ref_rows:
            shared = len(tokens & ref_tokens)
            if shared >= min_shared_tokens or _jaccard(tokens, ref_tokens) >= jaccard_threshold:
                outlets.add(outlet)
        entry = {
            "title": title[:80],
            "topic_key": key,
            "reference_outlets": sorted(outlets),
        }
        (aligned if outlets else ours_only).append(entry)

    # 레퍼런스 2개 이상 매체가 함께 다룬 토픽인데 우리 호에 없는 것 = 놓친 후보
    missed = [
        {
            "topic_key": key,
            "outlets": sorted(outlets),
            "sample_title": ref_key_sample.get(key, "")[:80],
        }
        for key, outlets in sorted(
            ref_by_key.items(), key=lambda kv: len(kv[1]), reverse=True
        )
        if len(outlets) >= 2 and key not in matched_keys
    ]

    total = len(selected_rows)
    alignment = round(len(aligned) / total, 3) if total else 0.0
    return {
        "reference_article_count": len(reference_articles),
        "selected_count": total,
        "alignment_rate": alignment,  # 우리 선정 중 레퍼런스도 다룬 비율
        "aligned": aligned,
        "ours_only": ours_only,  # 우리만 뽑은 것 (차별점일 수도, 과대평가일 수도)
        "missed_hot_topics": missed,  # 루브릭 개선 힌트
        "method": {
            "matching": "entity topic_key 일치 또는 제목 토큰 겹침(공유 3개 이상 / 자카드 0.3)",
            "note": "무토큰 휴리스틱 — 낮은 일치도가 곧 오답은 아님(내부 관점 차별화일 수 있음). "
            "missed_hot_topics를 사람이 보고 루브릭을 조정하는 용도.",
        },
    }
