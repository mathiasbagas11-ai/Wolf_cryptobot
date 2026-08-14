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


# ── multi-source: hackernews, reddit, aggregate ────────────────────────────
from wolf.news import (AggregateNewsSource, HackerNewsNews, NewsSynthesizer,
                       RedditNews, build_news_source)
from wolf.news.aggregate import rank_and_dedupe
from wolf.ai.base import LLMClient


def test_hackernews_parse():
    payload = {"hits": [
        {"objectID": "42", "title": "Bitcoin ETF approved", "url": "http://a",
         "points": 300, "num_comments": 120, "created_at_i": 1700000000},
        {"objectID": "43", "story_title": "ETH upgrade", "points": 50,
         "num_comments": 10, "created_at_i": 1700000100},
    ]}
    items = HackerNewsNews().parse(payload)
    assert items[0].id == "hn_42" and items[0].score == 420
    assert items[0].source == "Hacker News"
    # story without url falls back to the HN item link
    assert items[1].url == "https://news.ycombinator.com/item?id=43"


def test_hackernews_parse_empty():
    assert HackerNewsNews().parse({"hits": []}) == []
    assert HackerNewsNews().parse({}) == []


def test_reddit_parse_atom():
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><title>BTC pumps</title><link href="http://r/1"/>'
        '<id>t3_abc</id><updated>2026-06-16T10:00:00+00:00</updated></entry>'
        '<entry><title>Alt season?</title><link href="http://r/2"/>'
        '<id>t3_def</id><updated>2026-06-16T11:00:00+00:00</updated></entry>'
        '</feed>'
    )
    items = RedditNews().parse(xml)
    assert [i.id for i in items] == ["t3_abc", "t3_def"]
    assert items[0].source == "Reddit" and items[0].url == "http://r/1"
    assert items[1].published_ts > 0


def test_reddit_parse_bad_input():
    assert RedditNews().parse("") == []
    assert RedditNews().parse(None) == []


def test_aggregate_dedupes_and_ranks():
    # Same story on two sources (different score) collapses to the highest.
    a = NewsItem(id="1", title="Bitcoin ETF approved by the SEC in landmark vote", url="http://a",
                 source="Reddit", score=10, published_ts=1)
    b = NewsItem(id="2", title="Bitcoin ETF approved by the SEC in landmark vote today", url="http://b",
                 source="Hacker News", score=400, published_ts=2)
    c = NewsItem(id="3", title="Solana outage hits validators", url="http://c",
                 source="HN", score=50, published_ts=3)
    ranked = rank_and_dedupe([a, b, c])
    # b wins the dedupe (higher score) and leads the ranking.
    assert ranked[0].id == "2"
    assert [i.id for i in ranked] == ["2", "3"]


class _FakeAgg:
    def __init__(self, name, items): self.name = name; self._items = items
    def fetch(self): return list(self._items)


class _BoomSource:
    name = "boom"
    def fetch(self): raise RuntimeError("down")


def test_aggregate_source_isolates_failures():
    good = _FakeAgg("good", [NewsItem(id="1", title="X happens", url="u", score=5)])
    agg = AggregateNewsSource([_BoomSource(), good])
    items = agg.fetch()
    assert [i.id for i in items] == ["1"]   # boom source ignored


def test_build_news_source_multi_returns_aggregate():
    src = build_news_source("cryptocompare,reddit,hackernews")
    assert isinstance(src, AggregateNewsSource)
    assert build_news_source(["reddit"]).name == "reddit"   # single → that source
    assert build_news_source("nope,alsonope") is None


# ── AI synthesis ────────────────────────────────────────────────────────────
class _Narrator(LLMClient):
    def __init__(self, available=True): self._a = available
    @property
    def available(self): return self._a
    def complete(self, system, user, *, max_tokens=1024):
        self.user = user
        return "📰 Bitcoin ETF resmi disetujui — likuiditas masuk."
    def complete_json(self, system, user, schema, *, max_tokens=1024): return {}


def test_synthesizer_builds_brief_from_items():
    items = [NewsItem(id="1", title="Bitcoin ETF approved", url="http://a",
                      source="Hacker News", score=300)]
    synth = NewsSynthesizer(_Narrator())
    text = synth.build(items)
    assert "ETF" in text


def test_synthesizer_none_without_narrator_or_items():
    from wolf.ai.base import NullLLMClient
    assert NewsSynthesizer(NullLLMClient()).build([NewsItem(id="1", title="t", url="u")]) is None
    assert NewsSynthesizer(_Narrator()).build([]) is None


def test_notify_news_digest_routes_and_escapes():
    sess = FakeSession()
    n = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="1", news_thread_id="9"), session=sess)
    n.notify_news_digest("A & B headline")
    assert sess.calls[0]["message_thread_id"] == "9"
    assert "CRYPTO NEWS" in sess.calls[0]["text"]
    assert "A &amp; B" in sess.calls[0]["text"]
