"""Hacker News crypto headlines via the Algolia API (free, key-less).

Searches recent HN *stories* matching crypto terms and exposes their engagement
(points + comments) so the news service can rank by what developers actually
upvoted. No API key — just the public ``hn.algolia.com`` search endpoint.
"""

from __future__ import annotations

import time

from wolf.news.base import NewsItem, NewsSource

SEARCH_BY_DATE = "https://hn.algolia.com/api/v1/search_by_date"
DEFAULT_QUERY = "crypto OR bitcoin OR ethereum OR stablecoin"


class HackerNewsNews(NewsSource):
    name = "hackernews"

    def __init__(self, query: str = DEFAULT_QUERY, hits: int = 20,
                 window_hours: int = 48, **kw) -> None:
        super().__init__(**kw)
        self._query = query
        self._hits = hits
        self._window_hours = window_hours

    def _request(self):
        since = int(time.time()) - self._window_hours * 3600
        return SEARCH_BY_DATE, {
            "query": self._query,
            "tags": "story",
            "numericFilters": f"created_at_i>{since}",
            "hitsPerPage": self._hits,
        }

    def parse(self, payload) -> list[NewsItem]:
        hits = payload.get("hits") if isinstance(payload, dict) else None
        if not hits:
            return []
        items: list[NewsItem] = []
        for h in hits:
            title = (h.get("title") or h.get("story_title") or "").strip()
            if not title:
                continue
            obj_id = str(h.get("objectID", ""))
            url = h.get("url") or f"https://news.ycombinator.com/item?id={obj_id}"
            items.append(NewsItem(
                id=f"hn_{obj_id}",
                title=title,
                url=url,
                source="Hacker News",
                published_ts=int(h.get("created_at_i", 0)),
                score=int(h.get("points", 0) or 0) + int(h.get("num_comments", 0) or 0),
            ))
        return items
