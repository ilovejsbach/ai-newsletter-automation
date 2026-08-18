from ai_newsletter.style_lint import lint_selection, lint_text, lint_titles


def _rules(flags):
    return {f.rule_id for f in flags}


def test_strong_signals_flag_on_first_hit():
    text = "이 기능은 사용자에게 보여진다. 성능 향상으로 이어진다. 혁신적인 결과다."
    rules = _rules(lint_text(text, "korean_summary"))
    assert {"A-6", "A-11", "D-4"} <= rules


def test_formula_phrases():
    text = "단순한 도구가 아니라 플랫폼이다. 중요한 것은 속도라는 점이다."
    rules = _rules(lint_text(text, "detail_intro"))
    assert {"K-1", "K-2"} <= rules


def test_weak_signals_need_repetition():
    once = "이 모델에 대해 설명한다."
    assert "A-1" not in _rules(lint_text(once, "body"))
    thrice = "모델에 대해 말한다. 성능에 대한 평가다. 비용에 대해 다룬다."
    assert "A-1" in _rules(lint_text(thrice, "body"))


def test_ending_uniformity():
    text = "모델을 공개했다. 성능을 강조했다. 가격을 낮췄다고 발표했다. 배포를 시작했다."
    assert "E-2" in _rules(lint_text(text, "body"))
    varied = "모델을 공개했다. 성능이 좋다. 가격은 낮아졌다. 배포가 시작됐다."
    assert "E-2" not in _rules(lint_text(varied, "body"))


def test_clean_text_passes():
    text = "앤스로픽이 새 모델을 공개했다. 컨텍스트 창은 100만 토큰이다. 가격은 그대로다."
    assert lint_text(text, "korean_summary") == []


def test_title_formula_concentration():
    same = [f"회사{i}, 새 모델 공개" for i in range(10)]
    rules = _rules(lint_titles(same))
    assert {"C-5b", "C-5c"} <= rules
    varied = ["7배 빨라진 추론 모드", "오라클이 AI 코드를 금지했다", "회사A, 새 모델 공개",
              "30B 모델이 노트북에서 돈다", "영상 제작이 책상 하나로"]
    assert lint_titles(varied) == []


def test_c3b_flags_repeated_single_sentence_paragraphs():
    text = "\n\n".join(f"문장 {i}번이 여기 있다." for i in range(6))
    assert "C-3b" in _rules(lint_text(text, "detail_sections[0]"))


def test_c3b_passes_for_multi_sentence_paragraphs():
    text = "\n\n".join(
        f"문장 {i}-1이 있다. 문장 {i}-2도 있다. 문장 {i}-3까지 이어진다." for i in range(6)
    )
    assert "C-3b" not in _rules(lint_text(text, "detail_sections[0]"))


def test_lint_selection_aggregates():
    rows = [
        {"korean_title": "회사A, 모델 공개", "korean_summary": "결과가 개선으로 이어진다."},
        {"korean_title": "회사B, 도구 공개", "korean_summary": "깨끗한 문장이다."},
    ]
    report = lint_selection(rows)
    assert report["strong_count"] == 1
    assert len(report["articles"]) == 1
