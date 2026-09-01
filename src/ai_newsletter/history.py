"""주차 간 이력 참조: 지난 호에 실은 소식의 재탕을 감점하고, 후속 보도는 표시한다.

별도 DB 없이 outputs/<주차>_weekly_ai_newsletter*/data/selected_articles.json
(호당 10건)을 그대로 이력으로 읽는다. build의 --history 옵션으로만 켜지며,
꺼져 있으면(기본값) 이 모듈은 로드조차 되지 않아 기존 선정 동작과 완전히 같다.

대조 규칙 (설계: 완전 배제가 아니라 감점 — 감점을 뚫고 올라올 만큼 다시
뜨거워진 소식이라면 그 자체가 뉴스이고, 판정은 전부 보고서에 남긴다):
  재탕      같은 URL을 다시 실으려 함            → score -30
  유사 재탕 같은 발행사/소유자의 같은 소식        → score -20
  후속 보도 토픽은 겹치지만 지난 게재 이후의 새 기사 → 감점 없이 followup_of 표시
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

from .heat import topic_tokens
from .identity import owner_key_of, publisher_of, registrable_domain
from .models import Article, RankedArticle
from .ranking import _normalize_title

REPEAT_PENALTY = 30.0
SIMILAR_PENALTY = 20.0
# 정규화 제목 유사도(SequenceMatcher) 또는 토픽 토큰 겹침으로 같은 소식을 가린다.
# 불용어를 거르면 고유 토큰(회사명+모델명) 2개만 남는 경우가 흔해서, 같은 발행사
# 안에서는 2개(클러스터 병합과 같은 기준)면 충분하다. 발행사가 다른 후속 판정은
# 오인하면 본문에 잘못된 '지난 호 후속' 문구가 들어가므로 더 강한 근거를 요구한다.
_TITLE_SIM_THRESHOLD = 0.75
_TOKEN_OVERLAP_WEAK = 2   # 같은 발행사/소유자일 때
_TOKEN_OVERLAP_STRONG = 3  # 발행사가 다를 때 (또는 제목 유사도로 대신)

_WEEK_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_weekly_ai_newsletter(?:_v(\d+))?$")

# heat.topic_tokens는 'of'/'to' 같은 두 글자 기능어를 통과시킨다 — 주 내 클러스터링
# 에서는 무해하지만, 주차 간 대조에서는 이런 토큰이 겹침 수를 채워 전혀 다른 소식을
# 묶는다 (실측: 'of/to' 2개 + 'claude'로 정렬 실패 기사가 사이버보안 기사와 매칭).
_FUNCTION_WORDS = {"the", "and", "for", "with", "from", "our", "all", "its", "into", "how", "why", "new"}


def _story_tokens(*texts: str) -> set[str]:
    return {
        t for t in topic_tokens(*texts) if len(t) >= 3 and t not in _FUNCTION_WORDS
    }


@dataclass
class PastArticle:
    week: str  # 게재 주차 (YYYY-MM-DD)
    url: str
    title: str
    korean_title: str
    normalized_title: str
    tokens: set[str]
    publisher: str
    owner: str


@dataclass
class HistoryIndex:
    entries: list[PastArticle] = field(default_factory=list)
    weeks: list[str] = field(default_factory=list)  # 읽어 온 주차 목록 (최신순)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def match(self, article: RankedArticle) -> tuple[str, PastArticle] | None:
        """후보 기사를 이력과 대조해 (판정, 과거 기사)를 돌려준다. 무관하면 None."""
        url = (article.url or "").rstrip("/").lower()
        norm = _normalize_title(article.title)
        tokens = _story_tokens(article.title, article.topic_key)
        best: tuple[float, str, PastArticle] | None = None
        for past in self.entries:
            if url and url == past.url:
                return ("repeat", past)
            ratio = SequenceMatcher(None, norm, past.normalized_title).ratio() if norm else 0.0
            overlap = len(tokens & past.tokens)
            strong = ratio >= _TITLE_SIM_THRESHOLD or overlap >= _TOKEN_OVERLAP_STRONG
            if not strong and overlap < _TOKEN_OVERLAP_WEAK:
                continue
            same_outlet = bool(
                (article.publisher or publisher_of(article)) == past.publisher
                or (past.owner and owner_key_of(article) == past.owner)
            )
            if not same_outlet and not strong:
                continue  # 다른 발행사인데 근거가 약함 — 판정 보류
            published = article.published_at.date() if article.published_at else None
            past_week = date.fromisoformat(past.week)
            if published and published > past_week:
                kind = "followup"  # 지난 게재 이후에 나온 새 기사 — 후속 보도
            elif same_outlet:
                kind = "similar"  # 같은 발행사의 같은 소식이 다시 올라옴 — 유사 재탕
            else:
                continue  # 다른 발행사의 옛 기사가 토픽만 겹침 — 판정 보류
            strength = max(ratio, overlap / 10.0)
            if best is None or strength > best[0]:
                best = (strength, kind, past)
        if best is None:
            return None
        return (best[1], best[2])


def load_history(outputs_dir: Path, *, weeks: int = 4, before: date | None = None) -> HistoryIndex:
    """최근 `weeks`개 주차의 selected_articles.json을 색인으로 읽는다.

    - 같은 날짜의 _v2/_v3 빌드는 최신 버전만 쓴다.
    - `before`(기본: 오늘) 이후 주차는 제외한다 — 같은 날 재빌드에서 직전
      버전이 이력으로 잡혀 선정이 통째로 뒤집히는 것을 막는다.
    - 폴더가 없거나 JSON이 깨져 있으면 그 주차만 조용히 건너뛴다.
    """
    cutoff = before or date.today()
    latest_per_week: dict[str, tuple[int, Path]] = {}
    if outputs_dir.is_dir():
        for entry in outputs_dir.iterdir():
            m = _WEEK_DIR_RE.match(entry.name)
            if not m or not entry.is_dir():
                continue
            week, version = m.group(1), int(m.group(2) or 1)
            try:
                if date.fromisoformat(week) >= cutoff:
                    continue
            except ValueError:
                continue
            current = latest_per_week.get(week)
            if current is None or version > current[0]:
                latest_per_week[week] = (version, entry)

    index = HistoryIndex()
    for week in sorted(latest_per_week, reverse=True)[:weeks]:
        path = latest_per_week[week][1] / "data" / "selected_articles.json"
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rows, list):
            continue
        index.weeks.append(week)
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "")
            title = str(row.get("title") or "")
            if not url and not title:
                continue
            # publisher_of / owner_key_of 는 Article을 받으므로 과거 행을 최소
            # 스텁으로 감싼다 (둘 다 사실상 URL만 본다).
            stub = Article(id="past", source_id="past", source_name="past", title=title, url=url)
            index.entries.append(
                PastArticle(
                    week=week,
                    url=url.rstrip("/").lower(),
                    title=title,
                    korean_title=str(row.get("korean_title") or ""),
                    normalized_title=_normalize_title(title),
                    tokens=_story_tokens(title, str(row.get("topic_key") or "")),
                    publisher=str(row.get("publisher") or "") or registrable_domain(url),
                    owner=owner_key_of(stub) or "",
                )
            )
    return index


def apply_history(ranked: list[RankedArticle], history: HistoryIndex) -> list[dict[str, object]]:
    """이력 판정을 점수와 followup_of에 반영하고, 보고서용 판정 목록을 돌려준다."""
    hits: list[dict[str, object]] = []
    for article in ranked:
        matched = history.match(article)
        if matched is None:
            continue
        kind, past = matched
        if kind == "repeat":
            article.score -= REPEAT_PENALTY
            article.score_breakdown["history_repeat"] = -REPEAT_PENALTY
        elif kind == "similar":
            article.score -= SIMILAR_PENALTY
            article.score_breakdown["history_similar"] = -SIMILAR_PENALTY
        else:  # followup
            article.followup_of = f"{past.week}호 '{past.korean_title or past.title}'"
        hits.append(
            {
                "kind": kind,
                "title": article.title,
                "url": article.url,
                "past_week": past.week,
                "past_title": past.korean_title or past.title,
                "past_url": past.url,
            }
        )
    return hits
