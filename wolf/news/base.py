"""News sources.

A :class:`NewsSource` fetches recent crypto headlines from one provider and
normalises them into :class:`NewsItem`. Parsing is a pure method so it unit-tests
with canned payloads (the network is not required to verify formatting/dedup).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger("wolf.news")


@dataclass(frozen=True)
class NewsItem:
    id: str
    title: str
    url: str
    source: str = ""
    published_ts: int = 0  # epoch seconds

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "url": self.url,
                "source": self.source, "published_ts": self.published_ts}


class NewsSource(ABC):
    name: str = "base"

    def __init__(self, timeout: float = 10.0, session: Optional[requests.Session] = None) -> None:
        self._timeout = timeout
        self._session = session or requests.Session()

    @abstractmethod
    def _request(self) -> tuple[str, dict]:
        ...

    @abstractmethod
    def parse(self, payload) -> list[NewsItem]:
        """Parse a decoded payload into NewsItems, newest first."""

    def fetch(self) -> list[NewsItem]:
        url, params = self._request()
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout,
                                     headers={"User-Agent": "wolf/1.0"})
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            log.debug("%s news HTTP error: %s", self.name, exc)
            return []
        except ValueError as exc:
            log.debug("%s news invalid JSON: %s", self.name, exc)
            return []
        try:
            return self.parse(payload)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            log.debug("%s news parse failed: %s", self.name, exc)
            return []
