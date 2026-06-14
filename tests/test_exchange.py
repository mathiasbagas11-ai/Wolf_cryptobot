"""Tests for multi-exchange sources and the fallback client."""

from __future__ import annotations

from typing import Optional

from wolf.exchange import (
    BinanceFunding,
    BinanceSource,
    BybitFunding,
    BybitSource,
    GateSource,
    MarketDataClient,
    OKXFunding,
    OKXSource,
)
from wolf.exchange.sources import ExchangeSource
from wolf.models import Candle


# ── symbol / interval normalization ────────────────────────────────────────
def test_okx_symbol_and_interval():
    s = OKXSource()
    assert s._symbol("BTCUSDT") == "BTC-USDT"
    assert s._symbol("1000PEPEUSDT") == "1000PEPE-USDT"
    assert s._interval("15m") == "15m"
    assert s._interval("1h") == "1H"
    assert s._interval("4h") == "4H"


def test_bybit_symbol_and_interval():
    s = BybitSource()
    assert s._symbol("BTCUSDT") == "BTCUSDT"
    assert s._interval("15m") == "15"
    assert s._interval("1h") == "60"
    assert s._interval("4h") == "240"


# ── payload parsing (canned, no network) ───────────────────────────────────
def test_binance_parse():
    payload = [[1000, "10", "12", "9", "11", "100"], [1900, "11", "13", "10", "12", "120"]]
    candles = BinanceSource().parse_klines(payload)
    assert [c.time for c in candles] == [1000, 1900]
    assert candles[0].high == 12.0


def test_okx_parse_reverses_to_ascending():
    # OKX returns newest-first; parser must reverse.
    payload = {"code": "0", "data": [
        ["1900", "11", "13", "10", "12", "120", "0"],
        ["1000", "10", "12", "9", "11", "100", "0"],
    ]}
    candles = OKXSource().parse_klines(payload)
    assert [c.time for c in candles] == [1000, 1900]
    assert candles[1].close == 12.0


def test_bybit_parse_reverses_to_ascending():
    payload = {"retCode": 0, "result": {"list": [
        ["1900", "11", "13", "10", "12", "120", "0"],
        ["1000", "10", "12", "9", "11", "100", "0"],
    ]}}
    candles = BybitSource().parse_klines(payload)
    assert [c.time for c in candles] == [1000, 1900]


def test_okx_parse_price():
    assert OKXSource().parse_price({"data": [{"last": "65000.5"}]}) == 65000.5


def test_bybit_parse_price():
    assert BybitSource().parse_price({"result": {"list": [{"lastPrice": "65000.5"}]}}) == 65000.5


def test_parse_empty_payloads():
    assert OKXSource().parse_klines({"data": []}) == []
    assert BybitSource().parse_klines({"result": {"list": []}}) == []
    assert BinanceSource().parse_klines(None) == []


# ── Gate.io ────────────────────────────────────────────────────────────────
def test_gate_symbol_and_interval():
    s = GateSource()
    assert s._symbol("BTCUSDT") == "BTC_USDT"
    assert s._interval("15m") == "15m"
    assert s._interval("1h") == "1h"


def test_gate_parse_klines():
    # Gate row: [t(s), quote_vol, close, high, low, open, base_vol, closed]
    payload = [
        ["1000", "5000", "11", "12", "9", "10", "450", "true"],
        ["1900", "6000", "12", "13", "10", "11", "500", "true"],
    ]
    candles = GateSource().parse_klines(payload)
    assert [c.time for c in candles] == [1_000_000, 1_900_000]  # seconds -> ms
    assert candles[0].open == 10.0 and candles[0].high == 12.0 and candles[0].close == 11.0
    assert candles[0].volume == 450.0


def test_gate_parse_price():
    assert GateSource().parse_price([{"last": "65000.5"}]) == 65000.5


# ── funding sources ────────────────────────────────────────────────────────
def test_binance_funding_parse():
    assert BinanceFunding().parse({"lastFundingRate": "-0.0006"}) == -0.06


def test_okx_funding_parse_and_inst():
    f = OKXFunding()
    assert f._request("BTCUSDT")[1]["instId"] == "BTC-USDT-SWAP"
    assert f.parse({"data": [{"fundingRate": "0.0009"}]}) == 0.09


def test_bybit_funding_parse():
    assert BybitFunding().parse({"result": {"list": [{"fundingRate": "-0.0005"}]}}) == -0.05
    # Empty funding string -> None, not a crash.
    assert BybitFunding().parse({"result": {"list": [{"fundingRate": ""}]}}) is None


# ── fallback + cache ───────────────────────────────────────────────────────
class FakeSource(ExchangeSource):
    def __init__(self, name: str, candles: Optional[list[Candle]] = None, price: Optional[float] = None):
        self.name = name
        self._candles = candles or []
        self._price = price
        self.kline_calls = 0

    # the abstract hooks are unused (we override get_klines/get_price directly)
    def _symbol(self, symbol): return symbol
    def _interval(self, interval): return interval
    def _klines_request(self, symbol, interval, limit): return "", {}
    def parse_klines(self, payload): return []
    def _price_request(self, symbol): return "", {}
    def parse_price(self, payload): return None

    def get_klines(self, symbol, interval="15m", limit=100):
        self.kline_calls += 1
        return list(self._candles)

    def get_price(self, symbol):
        return self._price


def _c(t):
    return Candle(time=t, open=1, high=1, low=1, close=1, volume=1)


def test_falls_back_to_second_source():
    dead = FakeSource("binance", candles=[])         # returns nothing
    alive = FakeSource("okx", candles=[_c(1), _c(2)])
    client = MarketDataClient([dead, alive])
    candles = client.get_klines("BTCUSDT")
    assert len(candles) == 2
    assert dead.kline_calls == 1 and alive.kline_calls == 1


def test_caches_winning_source_per_symbol():
    dead = FakeSource("binance", candles=[])
    alive = FakeSource("okx", candles=[_c(1)])
    client = MarketDataClient([dead, alive])
    client.get_klines("BTCUSDT")          # learns OKX works
    dead.kline_calls = 0                   # reset
    client.get_klines("BTCUSDT")          # should try OKX first now
    assert alive.kline_calls == 2
    assert dead.kline_calls == 0          # dead source skipped


def test_returns_empty_when_all_sources_fail():
    client = MarketDataClient([FakeSource("a", []), FakeSource("b", [])])
    assert client.get_klines("BTCUSDT") == []


def test_price_fallback():
    client = MarketDataClient([FakeSource("a", price=None), FakeSource("b", price=42.0)])
    assert client.get_price("BTCUSDT") == 42.0


def test_funding_delegates_to_futures():
    class Fut:
        def get_funding_rate(self, s): return -0.07
        def get_open_interest_change(self, s, p="5m", l=12): return 2.5

    client = MarketDataClient([FakeSource("a", [_c(1)])], futures=Fut())
    assert client.get_funding_rate("BTCUSDT") == -0.07
    assert client.get_open_interest_change("BTCUSDT") == 2.5


def test_funding_none_without_futures():
    client = MarketDataClient([FakeSource("a", [_c(1)])])
    assert client.get_funding_rate("BTCUSDT") is None


class FakeFunding:
    def __init__(self, rate): self._rate = rate
    def get_funding_rate(self, symbol): return self._rate


def test_funding_falls_back_across_venues():
    # First venue returns None, second returns a rate.
    client = MarketDataClient(
        [FakeSource("a", [_c(1)])],
        funding_sources=[FakeFunding(None), FakeFunding(-0.08)],
    )
    assert client.get_funding_rate("BTCUSDT") == -0.08


def test_funding_sources_take_priority_over_futures():
    class Fut:
        def get_funding_rate(self, s): return 0.01
        def get_open_interest_change(self, s, p="5m", l=12): return 1.0

    client = MarketDataClient(
        [FakeSource("a", [_c(1)])],
        futures=Fut(),
        funding_sources=[FakeFunding(-0.05)],
    )
    # Venue funding wins; futures only used as last resort.
    assert client.get_funding_rate("BTCUSDT") == -0.05


def test_source_names_and_empty_guard():
    import pytest
    assert MarketDataClient([FakeSource("binance", [])]).source_names == ["binance"]
    with pytest.raises(ValueError):
        MarketDataClient([])


# ── 24h overview + trades parsing (for reports) ────────────────────────────
def test_binance_parse_24h():
    payload = [
        {"symbol": "BTCUSDT", "priceChangePercent": "2.5", "lastPrice": "65000", "quoteVolume": "1000000000"},
        {"symbol": "BADROW"},  # missing fields -> skipped
    ]
    rows = BinanceSource().parse_24h(payload)
    assert len(rows) == 1
    assert rows[0] == {"symbol": "BTCUSDT", "change_pct": 2.5, "price": 65000.0, "quote_volume": 1e9}


def test_okx_parse_24h_filters_usdt_and_computes_change():
    payload = {"data": [
        {"instId": "BTC-USDT", "last": "110", "open24h": "100", "volCcy24h": "5000"},
        {"instId": "BTC-USDC", "last": "1", "open24h": "1", "volCcy24h": "1"},  # not USDT
    ]}
    rows = OKXSource().parse_24h(payload)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert round(rows[0]["change_pct"], 2) == 10.0


def test_binance_parse_trades_notional_and_side():
    payload = [
        {"id": 1, "price": "65000", "qty": "2", "time": 100, "isBuyerMaker": False},
        {"id": 2, "price": "65000", "qty": "1", "time": 200, "isBuyerMaker": True},
    ]
    trades = BinanceSource().parse_trades("BTCUSDT", payload)
    assert trades[0]["id"] == "BTCUSDT-1"
    assert trades[0]["usd"] == 130000.0 and trades[0]["side"] == "BUY"
    assert trades[1]["side"] == "SELL"


def test_overview_fallback_across_sources():
    class OverviewSource(FakeSource):
        def get_24h_overview(self):
            return [{"symbol": "BTCUSDT", "change_pct": 1.0, "price": 1, "quote_volume": 1}]

    client = MarketDataClient([FakeSource("a", []), OverviewSource("b", [])])
    assert client.get_market_overview()[0]["symbol"] == "BTCUSDT"


def test_overview_empty_when_unsupported():
    assert MarketDataClient([FakeSource("a", [])]).get_market_overview() == []
