from datetime import datetime, timezone

from bs4 import BeautifulSoup

from ai_newsletter.collectors import Collector, _clean_title, _extract_tables, _find_info_images
from ai_newsletter.models import CollectionOptions, SourceConfig


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


class _FakeResponse:
    """httpx.Response 대역 — .raise_for_status()/.json()/.text만 흉내."""

    def __init__(self, *, json_data=None, text: str = "") -> None:
        self._json = json_data
        self.text = text

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._json


_ARXIV_ATOM = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<feed xmlns="http://www.w3.org/2005/Atom">'
    "<entry><published>2026-08-01T09:30:00Z</published></entry>"
    "</feed>"
)


def test_arxiv_v1_published_at_parses_published_field(monkeypatch):
    collector = Collector()

    def fake_get(url, params=None):
        assert "export.arxiv.org" in url
        assert params == {"id_list": "2401.12345"}
        return _FakeResponse(text=_ARXIV_ATOM)

    monkeypatch.setattr(collector.client, "get", fake_get)
    result = collector._arxiv_v1_published_at("2401.12345")
    assert result == datetime(2026, 8, 1, 9, 30, 0, tzinfo=timezone.utc)
    collector.close()


def test_arxiv_v1_published_at_returns_none_on_failure(monkeypatch):
    collector = Collector()
    monkeypatch.setattr(
        collector.client, "get", lambda url, params=None: (_ for _ in ()).throw(RuntimeError("down"))
    )
    assert collector._arxiv_v1_published_at("2401.12345") is None
    collector.close()


def _hfpapers_daily_payload() -> list[dict]:
    return [
        {
            "publishedAt": "2026-08-20T00:00:00Z",
            "numComments": 3,
            "paper": {
                "id": "2401.12345",
                "title": "Sample Paper",
                "summary": "Sample abstract.",
                "upvotes": 42,
                "publishedAt": "2026-08-20T00:00:00Z",
            },
        }
    ]


def test_collect_hfpapers_prefers_arxiv_v1_published_date(monkeypatch):
    collector = Collector(options=CollectionOptions(require_dates=False, strict_week=False))
    source = SourceConfig(id="hf-daily-papers", name="HF Daily Papers", kind="hfpapers")

    def fake_get(url, params=None):
        if "daily_papers" in url:
            return _FakeResponse(json_data=_hfpapers_daily_payload())
        return _FakeResponse(text=_ARXIV_ATOM)

    monkeypatch.setattr(collector.client, "get", fake_get)
    articles = collector.collect_hfpapers(source, days=7)
    assert len(articles) == 1
    # HF의 publishedAt(8/20)이 아니라 arXiv v1 제출일(8/1)을 써야 한다.
    assert articles[0].published_at == datetime(2026, 8, 1, 9, 30, 0, tzinfo=timezone.utc)
    collector.close()


def test_collect_hfpapers_keeps_hf_date_when_arxiv_lookup_fails(monkeypatch):
    collector = Collector(options=CollectionOptions(require_dates=False, strict_week=False))
    source = SourceConfig(id="hf-daily-papers", name="HF Daily Papers", kind="hfpapers")

    def fake_get(url, params=None):
        if "daily_papers" in url:
            return _FakeResponse(json_data=_hfpapers_daily_payload())
        raise RuntimeError("arxiv down")

    monkeypatch.setattr(collector.client, "get", fake_get)
    articles = collector.collect_hfpapers(source, days=7)
    assert len(articles) == 1
    # arXiv 호출 실패 시 HF의 publishedAt을 그대로 유지해야 한다.
    assert articles[0].published_at == datetime(2026, 8, 20, 0, 0, 0, tzinfo=timezone.utc)
    collector.close()
