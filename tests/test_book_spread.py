"""Tests for reading a top-of-book spread from each venue's ticker payload.

The parsers are exercised against the shape each venue documents, because the
venues themselves are not reachable from CI. What is checked here is the part
that can be got wrong without an exception: the arithmetic, the symbol
back-mapping, and the refusal to report a book that is crossed or locked.
"""

from __future__ import annotations

import pytest

from wolf.exchange.sources import (
    BinanceSource, BybitSource, GateSource, OKXSource, spread_bps,
)


# ── the shared arithmetic ───────────────────────────────────────────────────


def test_the_spread_is_basis_points_of_the_mid():
    assert spread_bps(999.5, 1000.5) == pytest.approx(10.0)
    assert spread_bps(99.99, 100.01) == pytest.approx(2.0)


def test_a_crossed_or_locked_book_is_refused():
    """A snapshot caught mid-update must not read as a costless round trip."""
    assert spread_bps(100.0, 100.0) is None      # locked
    assert spread_bps(100.5, 100.0) is None      # crossed
    assert spread_bps(0.0, 100.0) is None        # no bid
    assert spread_bps(-1.0, 100.0) is None


def test_unparseable_quotes_are_refused_rather_than_guessed():
    assert spread_bps(None, 100.0) is None
    assert spread_bps("", "") is None
    assert spread_bps("nonsense", "100") is None


# ── per-venue payloads, in each venue's own shape ───────────────────────────


def test_binance_reads_its_flat_list():
    out = BinanceSource.parse_book_spread([
        {"symbol": "BTCUSDT", "bidPrice": "999.5", "askPrice": "1000.5"},
        {"symbol": "LOCKED", "bidPrice": "10", "askPrice": "10"},
    ])
    assert out == {"BTCUSDT": pytest.approx(10.0)}


def test_okx_maps_its_dashed_instrument_id_back_to_canonical():
    """OKX quotes BTC-USDT; the universe and every stored signal say BTCUSDT."""
    out = OKXSource.parse_book_spread({"data": [
        {"instId": "BTC-USDT", "bidPx": "999.5", "askPx": "1000.5"},
        {"instId": "ETH-USDT", "bidPx": "0", "askPx": "10"},
    ]})
    assert "BTCUSDT" in out
    assert "BTC-USDT" not in out
    assert "ETHUSDT" not in out      # no bid, so no spread
    assert out["BTCUSDT"] == pytest.approx(10.0)


def test_gate_maps_its_underscored_pair_back_to_canonical():
    out = GateSource.parse_book_spread([
        {"currency_pair": "BTC_USDT", "highest_bid": "999.5", "lowest_ask": "1000.5"},
    ])
    assert out == {"BTCUSDT": pytest.approx(10.0)}


def test_bybit_reads_its_nested_result_list():
    out = BybitSource.parse_book_spread({"result": {"list": [
        {"symbol": "BTCUSDT", "bid1Price": "999.5", "ask1Price": "1000.5"},
    ]}})
    assert out == {"BTCUSDT": pytest.approx(10.0)}


@pytest.mark.parametrize("parser", [
    BinanceSource.parse_book_spread,
    OKXSource.parse_book_spread,
    GateSource.parse_book_spread,
    BybitSource.parse_book_spread,
])
def test_every_parser_survives_a_payload_it_did_not_expect(parser):
    """A venue erroring or rate-limiting returns None, not the shape assumed."""
    for junk in (None, {}, [], {"error": "rate limited"}, "text", 42):
        assert parser(junk) == {}


def test_the_venues_agree_on_the_same_book():
    """The same market quoted four ways must price to the same spread.

    Four parsers reading four payload shapes is four chances to divide by the
    wrong thing; they share the arithmetic precisely so they cannot disagree.
    """
    bid, ask = 100.0, 100.2
    results = [
        BinanceSource.parse_book_spread(
            [{"symbol": "XUSDT", "bidPrice": bid, "askPrice": ask}])["XUSDT"],
        OKXSource.parse_book_spread(
            {"data": [{"instId": "X-USDT", "bidPx": bid, "askPx": ask}]})["XUSDT"],
        GateSource.parse_book_spread(
            [{"currency_pair": "X_USDT", "highest_bid": bid, "lowest_ask": ask}])["XUSDT"],
        BybitSource.parse_book_spread(
            {"result": {"list": [{"symbol": "XUSDT", "bid1Price": bid,
                                  "ask1Price": ask}]}})["XUSDT"],
    ]
    assert all(r == pytest.approx(results[0]) for r in results)


def test_a_venue_without_a_book_ticker_yields_nothing_not_an_error():
    """The base capability stays a clean no-op for venues that lack one."""
    from wolf.exchange.sources import ExchangeSource

    assert ExchangeSource.get_book_spread(object()) == {}  # type: ignore[arg-type]
