"""News service — fetch, deduplicate, and surface only fresh headlines.

Wraps a :class:`~wolf.news.base.NewsSource` and remembers which item IDs have
already been seen (in the :class:`~wolf.state.StateStore`), so the same headline
is never posted twice. The whole batch fetched on a cycle is marked seen so a
backlog isn't dripped out item-by-item — only genuinely new headlines surface on
subsequent cycles.
"""

from __future__ import annotations

import logging

from wolf.news.base import NewsItem, NewsSource
from wolf.state import StateStore

log = logging.getLogger("wolf.news")

SEEN_KEY = "news_seen"
SEEN_CAP = 500


class NewsService:
    def __init__(self, source: NewsSource, store: StateStore, max_items: int = 3) -> None:
        self._source = source
        self._store = store
        self._max_items = max_items

    def fetch_new(self) -> list[NewsItem]:
        items = self._source.fetch()
        if not items:
            return []
        seen_list = self._store.read(SEEN_KEY, default=[])
        seen = set(seen_list)
        fresh = [i for i in items if i.id not in seen]
        to_post = fresh[: self._max_items]
        # Mark the whole fetched batch as seen so the backlog isn't dripped.
        seen_list = (seen_list + [i.id for i in fresh])[-SEEN_CAP:]
        self._store.write(SEEN_KEY, seen_list)
        if to_post:
            log.info("News: %d new headline(s)", len(to_post))
        return to_post
