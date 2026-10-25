"""Reddit crypto headlines via keyless Atom RSS.

Reads the hot feed of the main crypto subreddits in a single request
(``/r/sub1+sub2+.../hot.rss``) — no API key, no OAuth. RSS doesn't expose
upvote counts, so items carry score 0 and rank by recency; their value is
breadth and freshness (what the community is surfacing right now).
"""

from __future__ import annotations

import logging
from datetime import datetime
from xml.etree import ElementTree as ET

import requests

from wolf.news.base import NewsItem, NewsSource

log = logging.getLogger("wolf.news")

ATOM = "{http://www.w3.org/2005/Atom}"
DEFAULT_SUBS = ("CryptoCurrency", "Bitcoin", "ethereum", "CryptoMarkets")


class RedditNews(NewsSource):
    name = "reddit"

    def __init__(self, subreddits=DEFAULT_SUBS, sort: str = "hot", **kw) -> None:
        super().__init__(**kw)
        self._subs = tuple(subreddits)
        self._sort = sort

    def _request(self):
        feed = "+".join(self._subs)
        return f"https://www.reddit.com/r/{feed}/{self._sort}.rss", {}

    # RSS is XML, not JSON — override the JSON fetch from the base class.
    def fetch(self) -> list[NewsItem]:
        url, params = self._request()
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout,
                                     headers={"User-Agent": "wolf/1.0"})
            resp.raise_for_status()
            text = resp.text
        except requests.RequestException as exc:
            log.debug("reddit news HTTP error: %s", exc)
            return []
        try:
            return self.parse(text)
        except (ET.ParseError, ValueError, TypeError) as exc:
            log.debug("reddit news parse failed: %s", exc)
            return []

    def parse(self, payload) -> list[NewsItem]:
        if not isinstance(payload, str) or not payload.strip():
            return []
        root = ET.fromstring(payload)
        items: list[NewsItem] = []
        for entry in root.findall(f"{ATOM}entry"):
            title = (entry.findtext(f"{ATOM}title") or "").strip()
            if not title:
                continue
            entry_id = (entry.findtext(f"{ATOM}id") or "").strip()
            link_el = entry.find(f"{ATOM}link")
            url = link_el.get("href") if link_el is not None else ""
            items.append(NewsItem(
                id=entry_id or url,
                title=title,
                url=url,
                source="Reddit",
                published_ts=_parse_ts(entry.findtext(f"{ATOM}updated")),
            ))
        return items


def _parse_ts(value) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return 0
