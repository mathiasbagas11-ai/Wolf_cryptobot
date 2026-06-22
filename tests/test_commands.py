"""Tests for the analyze service and the Telegram command router."""

from __future__ import annotations

from types import SimpleNamespace

from wolf.analyze import AnalyzeService, normalize_symbol
from wolf.config import RegimeSettings
from wolf.detectors import default_detectors
from wolf.notify.commands import CommandRouter


def _trend_candles(n=150, up=True):
    from wolf.models import Candle
    out, p = [], 100.0
    for i in range(n):
        p += 0.6 if up else -0.6
        out.append(Candle(time=i * 900_000, open=p - 0.6, high=p + 0.5, low=p - 0.8, close=p, volume=100.0))
    return out


class _Client:
    def __init__(self, candles):
        self._candles = candles

    def get_klines(self, symbol, interval="15m", limit=150):
        return list(self._candles)[-limit:]


def test_normalize_symbol():
    assert normalize_symbol("btc") == "BTCUSDT"
    assert normalize_symbol("BTC-USDT") == "BTCUSDT"
    assert normalize_symbol("ETHUSDT") == "ETHUSDT"
    assert normalize_symbol("") == ""


def test_analyze_returns_card():
    svc = AnalyzeService(_Client(_trend_candles()), default_detectors(),
                         regime_settings=RegimeSettings())
    out = svc.analyze("btc")
    assert "ANALYSIS · BTCUSDT" in out
    assert "Regime" in out and "RSI" in out


def test_analyze_handles_unknown_ticker():
    out = AnalyzeService(_Client([]), default_detectors()).analyze("ZZZ")
    assert "Not enough data" in out


# ── command router ───────────────────────────────────────────────────────────
def _fake_app():
    analyze = AnalyzeService(_Client(_trend_candles()), default_detectors(), regime_settings=RegimeSettings())
    tracker = SimpleNamespace(
        stats=lambda: {"wins": 3, "losses": 1, "win_rate": 75.0, "avg_pnl_pct": 1.2,
                       "active": 2, "total_graded": 4},
        active_signals=lambda: [],
    )
    paper = SimpleNamespace(stats=lambda: {"balance": 1050.0, "return_pct": 5.0, "peak": 1060.0,
                                           "max_drawdown_pct": 1.0, "trades": 4, "total_r": 2.5, "avg_r": 0.6})
    learning = SimpleNamespace(snapshot=lambda: {"strategies": {"MOMENTUM": {"win_rate": 60.0, "trades": 5, "avg_r": 0.4}},
                                                 "symbols": {}, "blacklist": ["ZZZUSDT"]})
    return SimpleNamespace(analyze=analyze, tracker=tracker, paper=paper, learning=learning)


def test_router_help():
    assert "Commands" in CommandRouter(_fake_app()).handle("/help")


def test_router_analyze():
    assert "ANALYSIS · BTCUSDT" in CommandRouter(_fake_app()).handle("/analyze btc")


def test_router_bare_ticker_shortcut():
    assert "ANALYSIS · ETHUSDT" in CommandRouter(_fake_app()).handle("/eth")


def test_router_stats_paper_learning():
    r = CommandRouter(_fake_app())
    assert "WR 75.0%" in r.handle("/stats")
    assert "PAPER ACCOUNT" in r.handle("/paper")
    assert "Blacklist" in r.handle("/learning")


def test_router_strips_botname_suffix():
    assert "PAPER ACCOUNT" in CommandRouter(_fake_app()).handle("/paper@WolfBot")


def test_router_unknown():
    assert "Unknown" in CommandRouter(_fake_app()).handle("/wat is this")
