from ai_newsletter.render import _render_data_table, _render_data_table_inline, paragraphs


def test_paragraphs_merges_single_newline_sentences_into_one_p():
    text = "첫 문장이다.\n둘째 문장이다.\n셋째 문장이다."
    html = paragraphs(text)
    assert html.count("<p>") == 1
    assert "첫 문장이다. 둘째 문장이다. 셋째 문장이다." in html


def test_paragraphs_splits_on_blank_line():
    text = "첫 문단 문장이다.\n\n둘째 문단 문장이다."
    html = paragraphs(text)
    assert html.count("<p>") == 2
    assert "첫 문단 문장이다." in html
    assert "둘째 문단 문장이다." in html


def test_paragraphs_keeps_bullet_block_as_separate_ul():
    text = "본문 문장이다.\n- 항목 하나\n- 항목 둘\n\n다음 문단이다."
    html = paragraphs(text)
    assert "<p>본문 문장이다.</p>" in html
    assert "<ul><li>항목 하나</li><li>항목 둘</li></ul>" in html
    assert "<p>다음 문단이다.</p>" in html
    # 불릿 블록은 <p> 안에 흡수되지 않고 <ul>으로 분리되어야 한다.
    assert html.count("<ul>") == 1


def test_render_data_table_builds_thead_and_tbody_with_escaping():
    table = {"columns": ["모델", "점수 <A>"], "rows": [["Grok 4.6", "61"], ["GPT-5.4", "58"]]}
    html = _render_data_table(table)
    assert '<table class="data-table">' in html
    assert "<thead><tr><th>모델</th><th>점수 &lt;A&gt;</th></tr></thead>" in html
    assert "<td>Grok 4.6</td>" in html
    assert "<td>61</td>" in html


def test_render_data_table_returns_empty_for_missing_or_invalid_table():
    assert _render_data_table(None) == ""
    assert _render_data_table({}) == ""
    assert _render_data_table({"columns": ["A"], "rows": []}) == ""
    assert _render_data_table({"columns": [], "rows": [["1"]]}) == ""


def test_render_data_table_inline_uses_inline_styles():
    table = {"columns": ["항목"], "rows": [["값"]]}
    html = _render_data_table_inline(table)
    assert "style=" in html
    assert "<th " in html and "<td " in html
    assert "값" in html
