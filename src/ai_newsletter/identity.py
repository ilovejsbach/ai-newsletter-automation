"""Stable identities for selection: who published a story, and who owns a repo.

Selection caps used to key on `source_id`, which names the *feed an article
arrived through* — not the outlet that published it. Every story discovered via
Hacker News carries `source_id="hn-trending"`, so "at most 2 per source" quietly
discarded every HN-surfaced story after the first two. Measured over 14 past
builds that single mis-keying caused 116 of 203 score inversions, including the
week's biggest stories (Kimi K3 open weights at rank 4, the OpenAI/Hugging Face
incident at rank 5) losing their slots to items scoring 18.

Identity is therefore derived from the article URL: the cap then means what it
says, and the discovery channel stays what it actually is — metadata.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .models import Article

# Suffixes where the registrable domain needs three labels (a.co.uk, not co.uk).
_MULTI_PART_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk",
    "co.kr", "or.kr", "go.kr", "re.kr", "ne.kr", "pe.kr",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
    "com.au", "net.au", "org.au",
    "com.br", "com.cn", "com.tw", "com.hk", "com.sg",
    "co.in", "co.il", "co.nz", "co.za",
}

# Path segments that are the platform's own, not a user/org namespace.
_GITHUB_RESERVED = {
    "orgs", "topics", "trending", "features", "marketplace", "sponsors",
    "collections", "events", "about", "pricing", "explore", "settings",
    "search", "login", "join", "apps", "readme", "site", "enterprise",
}
_HF_RESERVED = {
    "blog", "docs", "papers", "posts", "learn", "spaces", "datasets",
    "models", "collections", "tasks", "pricing", "join", "settings", "chat",
}


def registrable_domain(url: str) -> str:
    """Registrable domain of a URL: 'https://www.theverge.com/ai/1' -> 'theverge.com'."""
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def publisher_of(article: Article) -> str:
    """Who actually published this — the cap key.

    Falls back to `source_id` only when the URL carries no host (rare; malformed
    or relative links), so an article is never silently exempted from the cap.
    """
    domain = registrable_domain(article.url)
    return domain or f"source:{article.source_id}"


def owner_key_of(article: Article) -> str | None:
    """Repo/model namespace, so one prolific author can't take several lookalike slots.

    Keyed off the URL alone. The previous version gated the Hugging Face branch on
    `source_id.startswith("huggingface")`, so an HF model page surfaced via HN got
    no owner key while a GitHub repo surfaced the same way did — and it required a
    trailing slash, so `github.com/org/repo` matched but `github.com/org` did not.
    """
    url = article.url.lower()
    match = re.search(r"github\.com/([^/?#]+)", url)
    if match:
        owner = match.group(1).strip()
        if owner and owner not in _GITHUB_RESERVED:
            return f"github:{owner}"
        return None
    match = re.search(r"huggingface\.co/([^/?#]+)", url)
    if match:
        owner = match.group(1).strip()
        if owner and owner not in _HF_RESERVED:
            return f"hf:{owner}"
    return None


def discovery_channel_of(article: Article) -> str:
    """The feed the article arrived through — reporting only, never a cap key."""
    return article.source_id


# Feeds that surface other people's articles. Their `source_name` describes how we
# found a story, not who published it, so it must never appear as a byline: an
# official Google security post went out credited to "HN Trending (AI)".
DISCOVERY_CHANNEL_IDS = {"hn-trending", "hf-daily-papers"}

# Multi-tenant publishing platforms with no per-author extraction (unlike
# github.com/huggingface.co, which resolve to the repo/model owner below).
# Collapsing these to the bare platform domain would erase who actually wrote
# the piece (e.g. a Substack newsletter becoming plain "Substack"), so the
# curated feed's own name is kept for them instead.
_MULTI_TENANT_DOMAINS_WITHOUT_OWNER = {"substack.com"}

# Bylines for domains we surface through those channels. Anything unmapped falls
# back to the bare domain, which is still accurate — unlike the channel name.
_PUBLISHER_NAMES = {
    "openai.com": "OpenAI",
    "anthropic.com": "Anthropic",
    "blog.google": "Google",
    "google.com": "Google",
    "deepmind.google": "Google DeepMind",
    "huggingface.co": "Hugging Face",
    "github.com": "GitHub",
    "arxiv.org": "arXiv",
    "kimi.com": "Moonshot AI (Kimi)",
    "moonshot.ai": "Moonshot AI",
    "deepseek.com": "DeepSeek",
    "qwen.ai": "Qwen",
    "meta.com": "Meta",
    "microsoft.com": "Microsoft",
    "nvidia.com": "NVIDIA",
    "mistral.ai": "Mistral AI",
    "apnews.com": "AP News",
    "reuters.com": "Reuters",
    "politico.com": "Politico",
    "cnbc.com": "CNBC",
    "theverge.com": "The Verge",
    "techcrunch.com": "TechCrunch",
    "arstechnica.com": "Ars Technica",
    "technologyreview.com": "MIT Technology Review",
    "wired.com": "WIRED",
    "bloomberg.com": "Bloomberg",
    "ft.com": "Financial Times",
    "nytimes.com": "The New York Times",
    "wsj.com": "The Wall Street Journal",
    "marktechpost.com": "MarkTechPost",
    "thenewstack.io": "The New Stack",
    "simonwillison.net": "Simon Willison",
    "jfrog.com": "JFrog",
    "substack.com": "Substack",
    "tailscale.com": "Tailscale",
    "cloudflare.com": "Cloudflare",
    "databricks.com": "Databricks",
    "scale.com": "Scale AI",
    "cohere.com": "Cohere",
    "stability.ai": "Stability AI",
    "allenai.org": "Allen Institute for AI",
    "sakana.ai": "Sakana AI",
    "moonshotai.com": "Moonshot AI",
    "x.ai": "xAI",
    "ycombinator.com": "Y Combinator",
    "openreview.net": "OpenReview",
    "semianalysis.com": "SemiAnalysis",
    "interconnects.ai": "Interconnects",
    "lesswrong.com": "LessWrong",
}


def display_publisher(article: Article) -> str:
    """The byline to print — who published this, not how we found it.

    Discovery channels (HN, HF daily papers) always resolve from the URL,
    since their source_name only describes how the story was found. Curated
    feeds normally keep their configured name ("MarkTechPost", "AlphaSignal"),
    because there the feed *is* the publisher — but when that name is just a
    company's blog under a house label ("Google DeepMind Blog" for
    deepmind.google, "OpenAI News" for openai.com), the URL-resolved name
    wins instead, so the same outlet reads the same way whether the
    newsletter reached it through its own feed or through HN/HF. A fact-check
    of the 8/18 issue flagged exactly this ("Google DeepMind Blog" shown as
    the byline where "Google DeepMind" was the accurate one).
    """
    domain = registrable_domain(article.url)
    # A repo/model host is not the author. Crediting a Y Combinator project to
    # plain "GitHub" (or a curated "GitHub AI Repositories" feed label) names
    # the platform and hides who actually published it.
    if domain in ("github.com", "huggingface.co"):
        owner = owner_key_of(article)
        if owner:
            label = _PUBLISHER_NAMES[domain]
            return f"{label} · {owner.split(':', 1)[1]}"
    if domain and domain not in _MULTI_TENANT_DOMAINS_WITHOUT_OWNER and domain in _PUBLISHER_NAMES:
        return _PUBLISHER_NAMES[domain]
    if article.source_id not in DISCOVERY_CHANNEL_IDS:
        return article.source_name
    return domain or article.source_name
