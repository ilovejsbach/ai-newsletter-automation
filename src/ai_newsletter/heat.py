"""Unified heat index: how loudly did the industry actually talk about this story?

Popularity used to enter the ranking three separate times — the `hn_points` boost,
the `social_boost` (which re-collected the *same* Hacker News stories from an RSS
feed with a lower point threshold), and a "corroboration" term that counted
`hn-trending` as an independent outlet even though it only ever links to someone
else's article. One observation, counted three times, on a scale nobody had
calibrated against the 0-100 editorial score.

Heat replaces all three. Every popularity signal is folded into a single 0-100
number, each underlying observation counted exactly once, computed per *story
cluster* rather than per article — because the evidence that a story dominated the
week is spread across all the articles covering it, not concentrated in whichever
one happened to survive dedup.

Heat is deliberately kept as a separate axis from editorial importance. They
answer different questions ("how big is this?" vs "how loud is this?") and the
selector uses each where it belongs: importance ranks, heat guarantees.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

from .identity import publisher_of
from .models import Article

# Tokens too generic to identify a story. Used when matching topic keys to each
# other and to social chatter, so "ai-model-release" doesn't match everything.
_STOP_TOKENS = {
    "ai", "model", "models", "new", "release", "released", "releases", "launch",
    "launches", "update", "updates", "open", "source", "opensource", "weights",
    "incident", "report", "announcement", "announces", "announcing", "preview",
    "the", "and", "for", "with", "from", "its", "this", "that", "2025", "2026",
}

# Point count treated as "the whole industry is talking about this".
_HN_SATURATION = 3000.0
_HN_FLOOR = 50.0

# Repo/model popularity treated as saturated.
_STAR_SATURATION = 20000.0
_DOWNLOAD_SATURATION = 1_000_000.0
_UPVOTE_SATURATION = 250.0

_WEIGHTS = {"hn": 0.45, "cross_source": 0.25, "social": 0.15, "platform": 0.15}

# Feeds that are Hacker News under another name. Their chatter must not be added
# on top of the hn_points it is literally derived from.
_HN_ALIASES = re.compile(r"hacker\s*news|ycombinator|hn\b", re.IGNORECASE)


_SUFFIXES = ("ers", "ing", "ies", "es", "ed", "er", "s")


def _stem(token: str) -> str:
    """Crude suffix strip so 'routers' and 'routing' describe the same thing."""
    if len(token) <= 4 or any(ch.isdigit() for ch in token):
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def topic_tokens(*texts: str) -> set[str]:
    """Meaningful lowercase tokens of a topic key or title.

    Bare version numbers are glued onto the name they qualify ('gpt-5-6' -> 'gpt56')
    rather than kept as separate tokens. Loose digits would otherwise make any two
    same-numbered releases look like one story — 'gemini-3-6' and 'gpt-3-6' share
    two of four tokens, which is exactly the merge threshold.
    """
    joined = " ".join(t for t in texts if t)
    glued: list[str] = []
    for token in re.split(r"[^a-z0-9가-힣]+", joined.lower()):
        if not token:
            continue
        if token.isdigit() and glued:
            glued[-1] = glued[-1] + token
        else:
            glued.append(token)
    tokens = set()
    for token in glued:
        if len(token) < 2 or token in _STOP_TOKENS:
            continue
        stemmed = _stem(token)
        if stemmed not in _STOP_TOKENS:
            tokens.add(stemmed)
    return tokens


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _log_norm(value: float, floor: float, saturation: float) -> float:
    if value < floor:
        return 0.0
    return _clamp01(math.log10(value / floor) / math.log10(saturation / floor))


def _hn_points(article: Article) -> int:
    try:
        return int(article.metrics.get("hn_points") or 0)
    except (TypeError, ValueError):
        return 0


def _platform_popularity(article: Article) -> float:
    """GitHub stars/forks and Hugging Face downloads/likes on one 0-1 scale."""

    def _num(key: str) -> float:
        try:
            return float(article.metrics.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    stars = _num("stars") + _num("forks") * 1.5 + _num("likes") * 2.0
    downloads = _num("downloads")
    return max(
        _log_norm(stars, 50.0, _STAR_SATURATION),
        _log_norm(downloads, 1000.0, _DOWNLOAD_SATURATION),
        # Community upvotes are to a paper what points are to a news story. The
        # week's top paper draws a few hundred; the median draws single digits.
        _log_norm(_num("paper_upvotes"), 5.0, _UPVOTE_SATURATION),
    )


def cluster_heat(
    members: Iterable[Article],
    social_sources: Iterable[str] = (),
) -> tuple[float, dict[str, float]]:
    """Heat (0-100) for one story cluster, plus the component breakdown.

    `social_sources` are the distinct source names of social posts matched to this
    cluster. Hacker News aliases are dropped when the cluster already carries
    `hn_points`, so the same votes are never counted on two axes.
    """
    members = list(members)
    if not members:
        return 0.0, {}

    peak_points = max((_hn_points(a) for a in members), default=0)
    hn = _log_norm(float(peak_points), _HN_FLOOR, _HN_SATURATION)

    publishers = {publisher_of(a) for a in members if publisher_of(a)}
    cross = _clamp01((len(publishers) - 1) / 3.0)

    social_names = {s for s in social_sources if s}
    if peak_points > 0:
        social_names = {s for s in social_names if not _HN_ALIASES.search(s)}
    social = _clamp01(len(social_names) / 3.0)

    platform = max((_platform_popularity(a) for a in members), default=0.0)

    components = {"hn": hn, "cross_source": cross, "social": social, "platform": platform}
    score = sum(_WEIGHTS[key] * value for key, value in components.items()) * 100.0

    breakdown = {key: round(value * 100.0, 1) for key, value in components.items()}
    breakdown["hn_points"] = float(peak_points)
    breakdown["publishers"] = float(len(publishers))
    breakdown["social_sources"] = float(len(social_names))
    return round(score, 2), breakdown


def social_matches(cluster_tokens: set[str], post: Article, min_shared: int = 2) -> bool:
    """Does a social post talk about this cluster?

    Matching is by shared meaningful tokens, not by a hand-maintained entity regex.
    The old regex list had no pattern for kimi/moonshot/grok/olmo, so posts about
    the exact story this subsystem was built to catch fell through to "no
    recognizable entity" and were discarded — the signal looked absent when it was
    merely unkeyed.
    """
    if not cluster_tokens:
        return False
    post_tokens = topic_tokens(post.title, post.summary[:200])
    shared = cluster_tokens & post_tokens
    if len(shared) >= min_shared:
        return True
    # A single highly distinctive token (a version-bearing name like "k3", "gpt5")
    # is enough on its own.
    return any(any(ch.isdigit() for ch in token) and len(token) >= 2 for token in shared)
