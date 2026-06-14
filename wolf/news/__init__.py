"""Crypto news layer: fetch headlines and post fresh ones to Telegram."""

from wolf.news.base import NewsItem, NewsSource
from wolf.news.cryptocompare import CryptoCompareNews
from wolf.news.service import NewsService

#: Registry of available news providers by name.
NEWS_REGISTRY = {
    "cryptocompare": CryptoCompareNews,
}

__all__ = [
    "NewsItem",
    "NewsSource",
    "CryptoCompareNews",
    "NewsService",
    "NEWS_REGISTRY",
]


def build_news_source(provider: str, timeout: float = 10.0):
    """Return a news source for ``provider``, or ``None`` if unknown."""
    factory = NEWS_REGISTRY.get(provider)
    return factory(timeout=timeout) if factory else None
