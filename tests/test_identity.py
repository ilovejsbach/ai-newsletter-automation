from ai_newsletter.identity import display_publisher
from ai_newsletter.models import Article


def _article(**kwargs) -> Article:
    defaults = dict(id="1", title="t", authority_tier=1.0)
    defaults.update(kwargs)
    return Article(**defaults)


def test_curated_company_blog_resolves_to_canonical_publisher():
    """8/18호 팩트체크에서 지적된 사례: 큐레이션 피드명('Google DeepMind Blog')이
    아니라 URL 기반 실제 게재처('Google DeepMind')가 표기돼야 한다."""
    article = _article(
        source_id="google-deepmind",
        source_name="Google DeepMind Blog",
        url="https://deepmind.google/blog/some-post",
    )
    assert display_publisher(article) == "Google DeepMind"


def test_curated_feed_matching_publisher_name_unaffected():
    article = _article(
        source_id="marktechpost",
        source_name="MarkTechPost",
        url="https://www.marktechpost.com/2026/08/x",
    )
    assert display_publisher(article) == "MarkTechPost"


def test_multi_tenant_platform_without_owner_keeps_feed_name():
    """substack.com은 필자별 추출이 없어 플랫폼 이름으로 뭉개면 필자 정보를 잃는다."""
    article = _article(
        source_id="alphasignal",
        source_name="AlphaSignal",
        url="https://alphasignalai.substack.com/p/foo",
    )
    assert display_publisher(article) == "AlphaSignal"


def test_curated_github_feed_resolves_to_repo_owner():
    article = _article(
        source_id="github-ai-trending",
        source_name="GitHub AI Repositories",
        url="https://github.com/someorg/somerepo",
    )
    assert display_publisher(article) == "GitHub · someorg"


def test_discovery_channel_never_shows_channel_name():
    article = _article(
        source_id="hn-trending",
        source_name="HN Trending (AI)",
        url="https://openai.com/index/some-security-post",
    )
    assert display_publisher(article) == "OpenAI"


def test_unmapped_domain_falls_back_to_curated_feed_name():
    article = _article(
        source_id="pytorch-kr-blog",
        source_name="PyTorch Korea Blog",
        url="https://pytorch.kr/blog/some-post",
    )
    assert display_publisher(article) == "PyTorch Korea Blog"
