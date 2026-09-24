"""Aggregate several news sources into one ranked, de-duplicated stream.

Fans out to every configured :class:`~wolf.news.base.NewsSource`, isolating each
one's failure (a dead source never sinks the batch), then:

* **cross-source dedup** by a normalised title key — the same story surfacing on
  Reddit and Hacker News collapses to a single item (highest-engagement wins);
* **ranking** by engagement score, then recency, so the most-upvoted, freshest
  headlines lead.

The result is what the :class:`~wolf.news.service.NewsService` dedups against
its seen-set and posts.
"""

from __future__ import annotations

import logging
import re

from wolf.news.base import NewsItem, NewsSource

log = logging.getLogger("wolf.news")

_WORD = re.compile(r"[a-z0-9]+")


class AggregateNewsSource(NewsSource):
    name = "aggregate"

    def __init__(self, sources: list[NewsSource]) -> None:
        # No HTTP of its own — it only orchestrates child sources.
        self._sources = list(sources)

    def _request(self):  # pragma: no cover - not used; fetch() is overridden
        return "", {}

    def parse(self, payload) -> list[NewsItem]:  # pragma: no cover - unused
        return []

    def fetch(self) -> list[NewsItem]:
        collected: list[NewsItem] = []
        for src in self._sources:
            try:
                items = src.fetch()
            except Exception:  # a misbehaving source must not sink the batch
                log.exception("News source %s failed", getattr(src, "name", "?"))
                continue
            collected.extend(items)
        return rank_and_dedupe(collected)


def _title_key(title: str) -> str:
    """Normalised key for near-duplicate detection: first 8 significant words."""
    words = _WORD.findall(title.lower())
    return " ".join(words[:8])


def rank_and_dedupe(items: list[NewsItem]) -> list[NewsItem]:
    best: dict[str, NewsItem] = {}
    for it in items:
        key = _title_key(it.title)
        if not key:
            continue
        cur = best.get(key)
        if cur is None or it.score > cur.score:
            best[key] = it
    return sorted(best.values(), key=lambda i: (i.score, i.published_ts), reverse=True)
