"""Tests for the adaptive learning engine, backtest simulator and /analyze."""

from __future__ import annotations

from types import SimpleNamespace

from wolf.analyze import AnalyzeService, normalize_symbol
from wolf.backtest import simulate
from wolf.config import LearningSettings
from wolf.detectors import default_detectors
from wolf.detectors.base import SignalCandidate
from wolf.learning import LearningEngine
from wolf.models import Candle, Signal
from wolf.notify.commands import CommandRouter


def _resolved(symbol="BTCUSDT", strategy="MOMENTUM", status="TP_HIT", pnl=5.0, entry=100.0, sl=95.0):
    return Signal(symbol=symbol, signal_type="SCREENER", direction="LONG",
                  entry_price=entry, tp=110, sl=sl, strategy=strategy,
                  status=status, activated=True, pnl_pct=pnl,
                  resolved_at="2026-01-01T00:00:00+00:00")


# ── learning ─────────────────────────────────────────────────────────────────
def test_learning_boosts_winner_penalises_loser(store):
    eng = LearningEngine(store, LearningSettings(min_samples=3, max_adjust=15))
    for _ in range(5):
        eng.observe(_resolved(status="TP_HIT", pnl=6.0))
    assert eng.adjustment("BTCUSDT", "MOMENTUM").delta > 0

    for _ in range(5):
        eng.observe(_resolved(symbol="ETHUSDT", status="SL_HIT", pnl=-4.0))
    assert eng.adjustment("ETHUSDT", "MOMENTUM").delta < 0


def test_learning_blacklists_bad_symbol(store):
    eng = LearningEngine(store, LearningSettings(min_samples=3, blacklist_min_trades=8, blacklist_max_winrate=25))
    for i in range(10):
        eng.observe(_resolved(symbol="ZZZUSDT",
                              status="TP_HIT" if i < 2 else "SL_HIT",
                              pnl=6.0 if i < 2 else -4.0))
    assert eng.adjustment("ZZZUSDT", "MOMENTUM").blacklisted


def test_learning_ignores_non_graded(store):
    eng = LearningEngine(store, LearningSettings(min_samples=1))
    eng.observe(_resolved(status="INVALIDATED", pnl=0.0))
    assert eng.snapshot()["strategies"] == {}


def test_learning_seed(store):
    eng = LearningEngine(store, LearningSettings(min_samples=3))
    eng.seed([("SWING", "BTCUSDT", True, 5.0, 1.5)] * 4)
    assert eng.snapshot()["strategies"]["SWING"]["win_rate"] == 100.0


# ── backtest simulate ────────────────────────────────────────────────────────
def _future(ohlc):
    return [Candle(time=i * 900_000, open=o, high=h, low=l, close=c, volume=100.0)
            for i, (o, h, l, c) in enumerate(ohlc)]


def _cand(direction="LONG"):
    return SignalCandidate(symbol="BTCUSDT", signal_type="SCREENER", direction=direction,
                           entry_price=100.0, tp=110, sl=95, score=70, strategy="MOMENTUM",
                           reasons=["x"], entry_mode="MOMENTUM_NOW",
                           tps=[{"level": 1, "price": 105}, {"level": 2, "price": 110}])


def test_simulate_tp_win():
    sim = simulate(_cand(), _future([(100, 106, 100, 105), (105, 111, 104, 110)]))
    assert sim.status == "TP_HIT" and sim.win and sim.pnl_pct == 10.0


def test_simulate_sl_loss():
    sim = simulate(_cand(), _future([(100, 101, 94, 96)]))
    assert sim.status == "SL_HIT" and not sim.win


def test_simulate_never_activated():
    c = _cand()
    c.entry_price, c.entry_mode = 90.0, "RETEST_WAIT"
    assert simulate(c, _future([(95, 99, 92, 98)])) is None


# ── analyze + commands ───────────────────────────────────────────────────────
def _trend_candles(n=150):
    out, p = [], 100.0
    for i in range(n):
        p += 0.6
        out.append(Candle(time=i * 900_000, open=p - 0.6, high=p + 0.5, low=p - 0.8, close=p, volume=100.0))
    return out


class _Client:
    def __init__(self, candles):
        self._candles = candles

    def get_klines(self, symbol, interval="15m", limit=150):
        return list(self._candles)[-limit:]


def test_normalize_symbol():
    assert normalize_symbol("btc") == "BTCUSDT"
    assert normalize_symbol("ETH-USDT") == "ETHUSDT"
    assert normalize_symbol("") == ""


def test_analyze_card():
    out = AnalyzeService(_Client(_trend_candles()), default_detectors()).analyze("btc")
    assert "ANALYSIS · BTCUSDT" in out and "RSI" in out


def test_analyze_unknown():
    assert "Not enough data" in AnalyzeService(_Client([]), default_detectors()).analyze("zzz")


def _fake_app():
    analyze = AnalyzeService(_Client(_trend_candles()), default_detectors())
    tracker = SimpleNamespace(
        stats=lambda: {"wins": 3, "losses": 1, "win_rate": 75.0, "avg_pnl_pct": 1.2, "active": 2, "total_graded": 4},
        active_signals=lambda: [],
    )
    account = SimpleNamespace(summary=lambda: {"balance": 1050.0, "starting_balance": 1000.0, "return_pct": 5.0,
                                               "peak": 1060.0, "max_drawdown_pct": 1.0, "trades": 4, "realized": 50.0})
    learning = SimpleNamespace(snapshot=lambda: {"strategies": {"MOMENTUM": {"win_rate": 60.0, "trades": 5, "avg_r": 0.4}},
                                                 "symbols": {}, "blacklist": ["ZZZUSDT"]})
    return SimpleNamespace(analyze=analyze, tracker=tracker, account=account, learning=learning)


def test_router_commands():
    r = CommandRouter(_fake_app())
    assert "Commands" in r.handle("/help")
    assert "ANALYSIS · BTCUSDT" in r.handle("/analyze btc")
    assert "ANALYSIS · ETHUSDT" in r.handle("/eth")            # bare ticker shortcut
    assert "WR 75.0%" in r.handle("/stats")
    assert "PAPER ACCOUNT" in r.handle("/paper@WolfBot")       # @botname stripped
    assert "Blacklist" in r.handle("/learning")
    assert "Unknown" in r.handle("/wat is this")
