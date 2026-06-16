"""Crypto news layer: fetch headlines from many sources and post fresh ones."""

from wolf.news.aggregate import AggregateNewsSource
from wolf.news.base import NewsItem, NewsSource
from wolf.news.cryptocompare import CryptoCompareNews
from wolf.news.hackernews import HackerNewsNews
from wolf.news.reddit import RedditNews
from wolf.news.service import NewsService
from wolf.news.synthesize import NewsSynthesizer

#: Registry of available news providers by name.
NEWS_REGISTRY = {
    "cryptocompare": CryptoCompareNews,
    "reddit": RedditNews,
    "hackernews": HackerNewsNews,
}

__all__ = [
    "NewsItem",
    "NewsSource",
    "CryptoCompareNews",
    "RedditNews",
    "HackerNewsNews",
    "AggregateNewsSource",
    "NewsService",
    "NewsSynthesizer",
    "NEWS_REGISTRY",
    "build_news_source",
]


def build_news_source(provider: str, timeout: float = 10.0):
    """Build a news source from a provider name or a CSV/list of names.

    A single name returns that source; multiple names return an
    :class:`AggregateNewsSource` fanning out across all of them. Unknown names
    are skipped; returns ``None`` only if nothing resolved.
    """
    names = provider if isinstance(provider, (list, tuple)) else \
        [p.strip() for p in str(provider).split(",") if p.strip()]
    sources = [NEWS_REGISTRY[n](timeout=timeout) for n in names if n in NEWS_REGISTRY]
    if not sources:
        return None
    if len(sources) == 1:
        return sources[0]
    return AggregateNewsSource(sources)
