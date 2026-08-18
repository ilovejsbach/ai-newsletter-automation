from bs4 import BeautifulSoup

from ai_newsletter.collectors import _clean_title, _extract_tables, _find_info_images


def test_strips_trailing_date_and_byline():
    assert _clean_title(
        "Nvidia launches a smaller, faster Nemotron model and a router to put it to work "
        "Aug 11th 2026 9:00am, by Frederic Lardinois"
    ) == "Nvidia launches a smaller, faster Nemotron model and a router to put it to work"


def test_strips_trailing_date_without_byline():
    assert _clean_title("Some story Aug 3, 2026") == "Some story"
    assert _clean_title("Some story December 3rd, 2026 11:45 PM") == "Some story"


def test_keeps_titles_that_merely_contain_by_or_year():
    assert _clean_title("Standing by Robots") == "Standing by Robots"
    assert _clean_title("AI in 2026") == "AI in 2026"
    assert _clean_title("Model beats GPT-4 by 12 points") == "Model beats GPT-4 by 12 points"
    assert _clean_title("Interview, by the numbers") == "Interview, by the numbers"


def test_extract_tables_serializes_html_table_with_caption():
    soup = BeautifulSoup(
        """
        <table>
          <caption>Benchmark results</caption>
          <tr><th>Model</th><th>Score</th></tr>
          <tr><td>Grok 4.6</td><td>61</td></tr>
        </table>
        """,
        "html.parser",
    )
    tables = _extract_tables(soup)
    assert len(tables) == 1
    lines = tables[0].splitlines()
    assert lines[0] == "Benchmark results"
    assert "Model | Score" in lines
    assert "Grok 4.6 | 61" in lines


def _tabular_nums_cell(label: str) -> str:
    return f'<div class="tabular-nums px-2 py-1">{label}</div>'


def test_extract_tables_grid_fallback_finds_dense_tabular_nums_cluster():
    cells = "".join(
        _tabular_nums_cell(v)
        for v in ["Model A", "62", "Model B", "56", "AA Index", "61", "GDPVal", "1753", "Extra", "70%"]
    )
    html = f'<div><p>Some unrelated boilerplate text around the page.</p><div class="grid">{cells}</div></div>'
    soup = BeautifulSoup(html, "html.parser")
    tables = _extract_tables(soup)
    assert len(tables) == 1
    assert "1753" in tables[0]
    assert "AA Index" in tables[0]


def test_extract_tables_grid_fallback_skips_below_minimum_cell_count():
    cells = "".join(_tabular_nums_cell(v) for v in ["A", "1", "B", "2", "C", "3", "D"])
    html = f'<div class="grid">{cells}</div>'
    soup = BeautifulSoup(html, "html.parser")
    assert _extract_tables(soup) == []


def test_extract_tables_skips_grid_fallback_when_html_table_present():
    html = """
    <table><tr><td>a</td><td>b</td></tr></table>
    <div>{cells}</div>
    """.format(cells="".join(_tabular_nums_cell(str(i)) for i in range(10)))
    soup = BeautifulSoup(html, "html.parser")
    tables = _extract_tables(soup)
    assert len(tables) == 1
    assert "a | b" in tables[0]


def test_find_info_images_includes_benchmark_alt_and_excludes_author_photo():
    html = """
    <div>
      <img src="https://example.com/staff.jpg" alt="author headshot">
      <img src="https://example.com/chart.png" alt="benchmark chart">
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    body_imgs = soup.select("img[src]")
    found = _find_info_images(body_imgs)
    assert found == ["https://example.com/chart.png"]


def test_find_info_images_matches_on_src_keyword_without_alt_text():
    html = '<img src="https://example.com/results-graph.png">'
    soup = BeautifulSoup(html, "html.parser")
    body_imgs = soup.select("img[src]")
    assert _find_info_images(body_imgs) == ["https://example.com/results-graph.png"]


def test_find_info_images_excludes_small_icon():
    html = '<img src="https://example.com/chart-icon.png" alt="chart" width="100">'
    soup = BeautifulSoup(html, "html.parser")
    body_imgs = soup.select("img[src]")
    assert _find_info_images(body_imgs) == []
