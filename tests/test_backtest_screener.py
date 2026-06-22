"""Tests for the backtest simulator and the screener's regime/learning gates."""

from __future__ import annotations

from typing import Optional, Sequence

from wolf.backtest import simulate
from wolf.config import LearningSettings, RegimeSettings
from wolf.detectors.base import Detector, SignalCandidate
from wolf.learning import LearningEngine
from wolf.models import Candle
from wolf.screener import Screener
from wolf.tracker import Tracker


def _future(ohlc):
    return [Candle(time=i * 900_000, open=o, high=h, low=l, close=c, volume=100.0)
            for i, (o, h, l, c) in enumerate(ohlc)]


def _candidate(direction="LONG", score=70, entry=100.0):
    return SignalCandidate(
        symbol="BTCUSDT", signal_type="SCREENER", direction=direction,
        entry_price=entry, tp=110, sl=95, score=score, strategy="MOMENTUM",
        reasons=["x"], entry_mode="MOMENTUM_NOW",
        tps=[{"level": 1, "price": 105}, {"level": 2, "price": 110}],
    )


# ── backtest simulate ────────────────────────────────────────────────────────
def test_simulate_full_tp_scale_out():
    sim = simulate(_candidate(), _future([(100, 106, 100, 105), (105, 111, 104, 110)]))
    assert sim.status == "TP"
    assert sim.pnl_pct == 7.5  # 50% @ +5%, 50% @ +10%


def test_simulate_tp1_then_breakeven():
    sim = simulate(_candidate(), _future([(100, 106, 100, 105), (105, 105, 99, 100)]))
    assert sim.status == "SL"
    assert sim.pnl_pct == 2.5  # 50% @ +5%, 50% @ breakeven


def test_simulate_never_activated_returns_none():
    cand = _candidate(entry=90.0)  # RETEST-style entry below; momentum still activates...
    cand.entry_mode = "RETEST_WAIT"
    # Price never trades down to 90 -> never activated.
    sim = simulate(cand, _future([(95, 99, 92, 98), (98, 99, 93, 97)]))
    assert sim is None


# ── screener regime gate ─────────────────────────────────────────────────────
class StubDetector(Detector):
    name = "MOMENTUM"
    min_candles = 0

    def __init__(self, candidate):
        self._candidate = candidate

    def evaluate(self, symbol, candles, context=None) -> Optional[SignalCandidate]:
        return self._candidate


def _uptrend(n=80):
    out, p = [], 100.0
    for i in range(n):
        p += 1.0
        out.append(Candle(time=i * 900_000, open=p - 1, high=p + 0.5, low=p - 1.2, close=p, volume=100.0))
    return out


def _screener(store, fake_client, tracker_settings, candidate, regime=None, learning=None):
    fake_client.klines["BTCUSDT"] = _uptrend()
    tracker = Tracker(store, fake_client, tracker_settings)
    sc = Screener(
        fake_client, tracker, [StubDetector(candidate)], notifier=None,
        universe=["BTCUSDT"], regime=regime, learning=learning, min_publish_score=0,
    )
    return sc, tracker


def test_regime_blocks_low_score_counter_trend(store, fake_client, tracker_settings):
    reg = RegimeSettings(enabled=True, counter_trend_min_score=85)
    # SHORT against a strong uptrend with only score 70 -> filtered out.
    sc, tracker = _screener(store, fake_client, tracker_settings, _candidate("SHORT", 70), regime=reg)
    assert sc.run_cycle() == []


def test_regime_allows_high_score_counter_trend(store, fake_client, tracker_settings):
    reg = RegimeSettings(enabled=True, counter_trend_min_score=85)
    # SHORT against uptrend but score 90 -> high-confluence override lets it through.
    cand = _candidate("SHORT", 90)
    cand.tp, cand.sl = 90, 105  # valid SHORT geometry
    cand.tps = [{"level": 1, "price": 95}, {"level": 2, "price": 90}]
    sc, tracker = _screener(store, fake_client, tracker_settings, cand, regime=reg)
    assert len(sc.run_cycle()) == 1


def test_regime_allows_trend_aligned(store, fake_client, tracker_settings):
    reg = RegimeSettings(enabled=True, counter_trend_min_score=85)
    sc, tracker = _screener(store, fake_client, tracker_settings, _candidate("LONG", 70), regime=reg)
    assert len(sc.run_cycle()) == 1


# ── screener learning gate ───────────────────────────────────────────────────
def test_learning_blacklist_skips_symbol(store, fake_client, tracker_settings):
    learning = LearningEngine(store, LearningSettings(min_samples=3, blacklist_min_trades=4, blacklist_max_winrate=25))
    learning.seed([("MOMENTUM", "BTCUSDT", -4.0, -1.0)] * 6)  # all losers -> blacklist
    sc, tracker = _screener(store, fake_client, tracker_settings, _candidate("LONG", 70), learning=learning)
    assert sc.run_cycle() == []
