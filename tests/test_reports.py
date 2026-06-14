"""Tests for the market reports (majors, radar, pulse, whale)."""

from __future__ import annotations

from wolf.models import Candle
from wolf.reports import MajorsReporter, MarketPulse, MarketRadar, WhaleTracker


class FakeMarketClient:
    def __init__(self, overview=None, trades=None, klines=None):
        self._overview = overview or []
        self._trades = trades or {}
        self._klines = klines or {}

    def get_market_overview(self):
        return list(self._overview)

    def get_recent_trades(self, symbol, limit=100):
        return list(self._trades.get(symbol, []))

    def get_klines(self, symbol, interval="15m", limit=100):
        return list(self._klines.get(symbol, []))


def _ov(symbol, change, price, vol):
    return {"symbol": symbol, "change_pct": change, "price": price, "quote_volume": vol}


# ── Majors ─────────────────────────────────────────────────────────────────
def test_majors_report():
    client = FakeMarketClient(overview=[
        _ov("BTCUSDT", 2.5, 65000, 1e9), _ov("ETHUSDT", -1.2, 3500, 5e8),
        _ov("SOLUSDT", 4.0, 150, 2e8), _ov("DOGEUSDT", 1.0, 0.1, 1e7),
    ])
    card = MajorsReporter(client).build()
    assert "MAJORS" in card
    assert "BTC" in card and "65,000" in card
    assert "+2.50%" in card and "-1.20%" in card


def test_majors_none_when_no_overview():
    assert MajorsReporter(FakeMarketClient(overview=[])).build() is None


# ── Radar ──────────────────────────────────────────────────────────────────
def test_radar_gainers_losers_volume():
    client = FakeMarketClient(overview=[
        _ov("AAAUSDT", 30.0, 1.0, 1e8),   # top gainer
        _ov("BBBUSDT", -25.0, 2.0, 2e8),  # top loser + volume
        _ov("CCCUSDT", 5.0, 3.0, 9e8),    # volume leader
        _ov("LOWUSDT", 50.0, 0.1, 1000),  # filtered: below min volume
    ])
    card = MarketRadar(client, top_n=2, min_quote_volume=1_000_000).build()
    assert "MARKET RADAR" in card
    assert "AAA" in card and "BBB" in card and "CCC" in card
    assert "LOW" not in card  # filtered out by min volume


def test_radar_none_when_empty():
    assert MarketRadar(FakeMarketClient(overview=[])).build() is None


# ── Pulse ──────────────────────────────────────────────────────────────────
def _trend_candles(up=True, n=120):
    out = []
    p = 100.0
    for i in range(n):
        p += 0.5 if up else -0.5
        out.append(Candle(time=i * 3_600_000, open=p, high=p + 1, low=p - 1, close=p, volume=100.0))
    return out


def test_pulse_detects_bias():
    client = FakeMarketClient(klines={
        "BTCUSDT": _trend_candles(up=True),
        "ETHUSDT": _trend_candles(up=False),
    })
    card = MarketPulse(client).build()
    assert "MARKET PULSE" in card
    assert "BTC" in card and "BULLISH" in card
    assert "ETH" in card and "BEARISH" in card


def test_pulse_none_when_insufficient():
    assert MarketPulse(FakeMarketClient(klines={"BTCUSDT": []})).build() is None


# ── Whale ──────────────────────────────────────────────────────────────────
def _trade(tid, symbol, price, qty, maker=False):
    return {"id": f"{symbol}-{tid}", "symbol": symbol, "price": price, "qty": qty,
            "usd": price * qty, "side": "SELL" if maker else "BUY", "time": tid}


def test_whale_flags_large_trades_and_dedups(store):
    trades = {"BTCUSDT": [
        _trade(1, "BTCUSDT", 65000, 10),   # $650k -> whale
        _trade(2, "BTCUSDT", 65000, 0.1),  # $6.5k -> ignored
        _trade(3, "BTCUSDT", 65000, 5, maker=True),  # $325k -> whale (SELL)
    ]}
    client = FakeMarketClient(trades=trades)
    wt = WhaleTracker(client, store, symbols=["BTCUSDT"], min_usd=250_000)
    card = wt.build()
    assert "WHALE REPORT" in card
    assert "650.00K" in card or "$650" in card
    # Second cycle: same trades are already seen -> nothing.
    assert wt.build() is None


def test_whale_none_when_no_large_trades(store):
    client = FakeMarketClient(trades={"BTCUSDT": [_trade(1, "BTCUSDT", 100, 1)]})
    assert WhaleTracker(client, store, symbols=["BTCUSDT"], min_usd=250_000).build() is None
