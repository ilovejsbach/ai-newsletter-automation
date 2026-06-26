from ai_newsletter.ranking import rank_articles
from ai_newsletter.sample_data import sample_articles


def test_rank_articles_returns_scored_items() -> None:
    ranked = rank_articles(sample_articles(), limit=2)
    assert len(ranked) == 2
    assert ranked[0].score >= ranked[1].score
    assert ranked[0].reason
