"""CryptoCompare news source.

Uses the free, key-less CryptoCompare news endpoint, so it works out of the box
without any API key. Returns recent English crypto headlines, newest first.
"""

from __future__ import annotations

from wolf.news.base import NewsItem, NewsSource


class CryptoCompareNews(NewsSource):
    name = "cryptocompare"

    def __init__(self, base_url: str = "https://min-api.cryptocompare.com", **kw) -> None:
        super().__init__(**kw)
        self._base = base_url.rstrip("/")

    def _request(self):
        return f"{self._base}/data/v2/news/", {"lang": "EN", "sortOrder": "latest"}

    def parse(self, payload) -> list[NewsItem]:
        rows = payload.get("Data") if isinstance(payload, dict) else None
        if not rows:
            return []
        items: list[NewsItem] = []
        for r in rows:
            source = ""
            info = r.get("source_info")
            if isinstance(info, dict):
                source = info.get("name", "")
            source = source or r.get("source", "")
            items.append(NewsItem(
                id=str(r["id"]),
                title=r.get("title", "").strip(),
                url=r.get("url", ""),
                source=source,
                published_ts=int(r.get("published_on", 0)),
            ))
        return items
