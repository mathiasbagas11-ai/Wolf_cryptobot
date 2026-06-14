"""Tests for the news layer: parsing, dedup service, and Telegram card."""

from __future__ import annotations

from wolf.config import TelegramSettings
from wolf.news import CryptoCompareNews, NewsService, build_news_source
from wolf.news.base import NewsItem, NewsSource
from wolf.notify import TelegramNotifier

from tests.test_telegram import FakeSession


# ── CryptoCompare parsing ──────────────────────────────────────────────────
def test_cryptocompare_parse():
    payload = {"Data": [
        {"id": 1, "title": " BTC breaks 70k ", "url": "http://a", "published_on": 1700000000,
         "source_info": {"name": "CoinDesk"}},
        {"id": 2, "title": "ETH upgrade", "url": "http://b", "published_on": 1700000100,
         "source": "TheBlock"},
    ]}
    items = CryptoCompareNews().parse(payload)
    assert [i.id for i in items] == ["1", "2"]
    assert items[0].title == "BTC breaks 70k"          # stripped
    assert items[0].source == "CoinDesk"               # from source_info
    assert items[1].source == "TheBlock"               # fallback to source


def test_cryptocompare_parse_empty():
    assert CryptoCompareNews().parse({"Data": []}) == []
    assert CryptoCompareNews().parse({}) == []


def test_build_news_source_unknown_is_none():
    assert build_news_source("acme") is None
    assert build_news_source("cryptocompare") is not None


# ── dedup service ──────────────────────────────────────────────────────────
class FakeNewsSource(NewsSource):
    def __init__(self, items):
        self._items = items
    def _request(self): return "", {}
    def parse(self, payload): return []
    def fetch(self): return list(self._items)


def _items(*ids):
    return [NewsItem(id=str(i), title=f"news {i}", url=f"http://{i}", source="x") for i in ids]


def test_service_posts_only_fresh_and_caps(store):
    svc = NewsService(FakeNewsSource(_items(1, 2, 3, 4, 5)), store, max_items=3)
    first = svc.fetch_new()
    assert [i.id for i in first] == ["1", "2", "3"]   # capped to max_items
    # All fetched ids are now seen -> a re-fetch of the same batch yields nothing.
    assert svc.fetch_new() == []


def test_service_surfaces_new_items_next_cycle(store):
    src = FakeNewsSource(_items(1, 2))
    svc = NewsService(src, store, max_items=5)
    assert [i.id for i in svc.fetch_new()] == ["1", "2"]
    src._items = _items(3, 1, 2)   # one genuinely new item
    assert [i.id for i in svc.fetch_new()] == ["3"]


def test_service_empty_source(store):
    assert NewsService(FakeNewsSource([]), store).fetch_new() == []


# ── Telegram news card ─────────────────────────────────────────────────────
def test_notify_news_card_and_route():
    sess = FakeSession()
    n = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="1", news_thread_id="77"), session=sess)
    n.notify_news(_items(1, 2))
    assert sess.calls[0]["message_thread_id"] == "77"
    text = sess.calls[0]["text"]
    assert "CRYPTO NEWS" in text
    assert "news 1" in text and "http://1" in text


def test_notify_news_empty_sends_nothing():
    sess = FakeSession()
    n = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="1"), session=sess)
    n.notify_news([])
    assert sess.calls == []


def test_news_card_escapes_title():
    sess = FakeSession()
    n = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="1"), session=sess)
    n.notify_news([NewsItem(id="1", title="A & B <hack>", url="http://x", source="s")])
    assert "A &amp; B &lt;hack&gt;" in sess.calls[0]["text"]
