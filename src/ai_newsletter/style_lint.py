"""한국어 AI 문체 신호 린터.

nathankim0/humanize-korean(MIT)의 'AI 문체 통합 룰북'에서 강·중 신호를 추려
정규식·구조 검사로 옮겼다. 목적은 두 가지:
1) 프롬프트의 문체 규칙이 실제로 지켜졌는지 LLM 없이 결정적으로 검증 (style_flags)
2) humanize 재작성 패스가 고칠 문장을 고르는 근거 제공

탐지 철학은 룰북과 같다: 단일 등장이 아니라 반복·군집을 근거로 삼는 신호는
count 게이트를 두고, 한 번만으로도 어색한 신호(이중 피동, 상투적 결산)는
즉시 플래그한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# RankedArticle에서 독자에게 노출되는 한국어 텍스트 필드.
TEXT_FIELDS = ("korean_title", "one_liner", "hook", "korean_summary", "why_it_matters", "detail_intro")


@dataclass
class StyleFlag:
    rule_id: str
    severity: str  # "강" | "중"
    label: str
    where: str  # "korean_title" 등 필드명 또는 "detail_sections[2]"
    excerpt: str
    fix_hint: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule_id,
            "severity": self.severity,
            "label": self.label,
            "where": self.where,
            "excerpt": self.excerpt,
            "fix_hint": self.fix_hint,
        }


@dataclass
class _Rule:
    rule_id: str
    severity: str
    label: str
    pattern: re.Pattern[str]
    fix_hint: str
    min_count: int = 1  # 문서(기사) 단위 등장 횟수 게이트


# 강 신호: 한두 번만으로도 어색한 표현.
# 중 신호: 반복될 때 기계적으로 읽히는 표현 (min_count 게이트).
_RULES: list[_Rule] = [
    _Rule("A-6", "강", "이중 피동", re.compile(r"되어지|보여진다|보여지는|보여지고"),
          "단일 피동이나 능동으로"),
    _Rule("A-3", "강", "'~에 있어' 번역투", re.compile(r"에 있어서?[\s,]"),
          "'~에서', '~을 보면'으로"),
    _Rule("A-11", "강", "'~로 이어진다' 만능 결산", re.compile(r"[로으]로 이어(진다|질|지게|졌다|지며|지고)"),
          "실제 인과·주체를 직접 서술"),
    _Rule("D-2", "강", "상투적 총평", re.compile(r"주목할 (만하다|필요가|만한)|시사하는 바|의미가 크다|의의가 크다"),
          "무엇이 어떻게 달라지는지 구체적으로"),
    _Rule("D-4", "강", "근거 없는 과장 수식", re.compile(r"혁신적|획기적|압도적|게임\s?체인저"),
          "수식 대신 원문의 수치·사실로"),
    _Rule("K-1", "강", "'단순한 A가 아니라 B' 격상 공식", re.compile(r"단순한 [^,.!?]{1,25}[이가] 아니라"),
          "격상 틀을 빼고 B를 직접 서술"),
    _Rule("K-2", "강", "'중요한 것은 ~' 초점 공식", re.compile(r"중요한 (것|점|사실)은"),
          "핵심 명제를 주어·서술어로 직접"),
    _Rule("K-3", "강", "'~라는 점에서 의미' 평가 공식", re.compile(r"(라는|다는) (점|측면)에서"),
          "성과와 평가를 직접 연결"),
    _Rule("D-1", "중", "'결론적으로' 류 결산 표지", re.compile(r"결론적으로|요약하(자|)면|정리하(자|)면"),
          "문단 자체가 결론이면 삭제", min_count=1),
    _Rule("A-1", "중", "'~에 대해' 반복", re.compile(r"에 대해서?|에 대한"),
          "목적격 조사나 직접 서술로", min_count=3),
    _Rule("A-2", "중", "'~를 통해' 반복", re.compile(r"[를을] 통해|통하여"),
          "'~로', '~해서'로", min_count=3),
    _Rule("H-1", "중", "문두 접속사 반복", re.compile(r"(?:^|[.!?]\s+)(또한|한편|즉|나아가|아울러|게다가)[\s,]"),
          "문장 관계가 자명한 것부터 삭제", min_count=3),
    _Rule("I-1", "중", "'~는 것이다' 형식명사 종결 반복", re.compile(r"[는은ㄴ] (것|셈)이다"),
          "직접 종결로", min_count=3),
]

# 헤드라인 공식 (C-5): 'X: Y' 콜론 공식, '~의 시대' 류.
_TITLE_COLON_RE = re.compile(r"^[^:]{2,30}:\s")
_TITLE_ERA_RE = re.compile(r"의 시대|새로운 장|이정표")
# '회사, ~했다/공개' 뉴스 헤드라인 틀 자체는 정상 관행 — 전체가 한 틀로 쏠릴 때만 잡는다.
_TITLE_COMMA_RE = re.compile(r"^\S{2,20},\s")

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# 문단 경계는 빈 줄만 인정 — render.py의 paragraphs()와 같은 기준.
_BLOCK_SPLIT_RE = re.compile(r"\n\s*\n")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]


def _ending(sentence: str) -> str:
    """문장 종결 어미의 대략적 지문 (마지막 어절의 끝 2글자)."""
    stripped = sentence.rstrip(".!?\"'」』)")
    return stripped[-2:] if len(stripped) >= 2 else stripped


def lint_text(text: str, where: str) -> list[StyleFlag]:
    """한 텍스트 필드에서 규칙 위반을 찾는다."""
    flags: list[StyleFlag] = []
    if not text:
        return flags
    for rule in _RULES:
        hits = list(rule.pattern.finditer(text))
        if len(hits) < rule.min_count:
            continue
        first = hits[0]
        start = max(0, first.start() - 20)
        excerpt = text[start : first.end() + 20].strip()
        label = rule.label if rule.min_count == 1 else f"{rule.label} ({len(hits)}회)"
        flags.append(StyleFlag(rule.rule_id, rule.severity, label, where, excerpt, rule.fix_hint))

    # E-2: 동일 종결어미 4문장 연속.
    sents = _sentences(text)
    run, prev = 1, ""
    for sent in sents:
        end = _ending(sent)
        if end and end == prev:
            run += 1
            if run == 4:
                flags.append(StyleFlag(
                    "E-2", "중", f"동일 종결어미('{end}') 4문장 연속", where,
                    sent[:40], "문장 일부를 합치거나 어미를 바꿔 리듬 변화",
                ))
        else:
            run = 1
        prev = end

    # C-3b: 문단(빈 줄 기준) 대부분이 문장 하나짜리면, 문장마다 줄바꿈만 하고
    # 실제로는 문단으로 묶지 않은 옛 스타일 — 흐름 문단이 아니라 리스트처럼
    # 읽힌다. 문단이 최소 5개는 있어야 "반복" 신호로 볼 수 있다.
    blocks = [b.strip() for b in _BLOCK_SPLIT_RE.split(text) if b.strip()]
    if len(blocks) >= 5:
        single_sentence = sum(1 for b in blocks if len(_sentences(b)) == 1)
        ratio = single_sentence / len(blocks)
        if ratio > 0.7:
            flags.append(StyleFlag(
                "C-3b", "중", f"한 문장 문단 반복 ({single_sentence}/{len(blocks)}개)", where,
                blocks[0][:40], "2~4문장을 공백으로 이어 하나의 흐름 문단으로 묶어",
            ))
    return flags


def lint_article_fields(row: dict[str, object]) -> list[StyleFlag]:
    """기사 dict(RankedArticle.model_dump 또는 JSON row)의 노출 텍스트를 모두 검사."""
    flags: list[StyleFlag] = []
    for fieldname in TEXT_FIELDS:
        value = row.get(fieldname)
        if isinstance(value, str) and value:
            flags.extend(lint_text(value, fieldname))
    sections = row.get("detail_sections")
    if isinstance(sections, list):
        for i, sec in enumerate(sections):
            if isinstance(sec, dict) and sec.get("body"):
                flags.extend(lint_text(str(sec["body"]), f"detail_sections[{i}]"))
    return flags


def lint_titles(titles: list[str]) -> list[StyleFlag]:
    """헤드라인 목록 전체를 보고 틀 쏠림을 잡는다 (개별 제목이 아니라 분포가 신호)."""
    flags: list[StyleFlag] = []
    titles = [t for t in titles if t]
    if not titles:
        return flags
    for t in titles:
        if _TITLE_COLON_RE.search(t):
            flags.append(StyleFlag("C-5", "중", "'X: Y' 콜론 헤딩 공식", "korean_title", t,
                                   "짧은 평서형 제목으로"))
        if _TITLE_ERA_RE.search(t):
            flags.append(StyleFlag("D-6", "강", "'~의 시대' 류 상투 제목", "korean_title", t,
                                   "사건을 직접 서술"))
    comma_count = sum(1 for t in titles if _TITLE_COMMA_RE.search(t))
    if len(titles) >= 5 and comma_count / len(titles) > 0.7:
        flags.append(StyleFlag(
            "C-5b", "중", f"제목 틀 쏠림 — '회사, ~' 틀이 {comma_count}/{len(titles)}건", "korean_title",
            "; ".join(titles[:3]), "일부를 수치형·결과형 등 다른 틀로",
        ))
    endings = [t.rstrip().split()[-1] for t in titles if t.split()]
    if endings:
        from collections import Counter

        word, count = Counter(endings).most_common(1)[0]
        if len(titles) >= 5 and count / len(titles) > 0.5:
            flags.append(StyleFlag(
                "C-5c", "중", f"제목 끝 단어 쏠림 — '{word}' {count}/{len(titles)}건", "korean_title",
                word, "끝 단어(공개/출시/발표)를 다양화",
            ))
    return flags


def lint_selection(rows: list[dict[str, object]]) -> dict[str, object]:
    """선정 기사 전체를 검사해 generation_report용 요약을 만든다."""
    per_article: list[dict[str, object]] = []
    strong = weak = 0
    for row in rows:
        flags = lint_article_fields(row)
        if flags:
            strong += sum(1 for f in flags if f.severity == "강")
            weak += sum(1 for f in flags if f.severity == "중")
            per_article.append({
                "title": row.get("korean_title") or row.get("title") or "",
                "flags": [f.to_dict() for f in flags],
            })
    title_flags = lint_titles([str(r.get("korean_title") or "") for r in rows])
    strong += sum(1 for f in title_flags if f.severity == "강")
    weak += sum(1 for f in title_flags if f.severity == "중")
    return {
        "strong_count": strong,
        "weak_count": weak,
        "articles": per_article,
        "title_flags": [f.to_dict() for f in title_flags],
    }
