"""Tests for the screening orchestration."""

from __future__ import annotations

from wolf.config import RiskSettings
from wolf.detectors import MomentumBreakoutDetector
from wolf.detectors.base import SignalCandidate
from wolf.models import Candle
from wolf.regime import BEARISH, BULLISH, NEUTRAL, UNKNOWN
from wolf.screener import Screener
from wolf.tracker import Tracker


def _breakout_candles() -> list[Candle]:
    candles = [Candle(time=i * 900_000, open=100, high=101, low=99, close=100, volume=100.0) for i in range(60)]
    candles.append(Candle(time=60 * 900_000, open=100, high=108, low=100, close=107, volume=500.0))
    return candles


class _FakeRegime:
    def __init__(self, bias: str) -> None:
        self._bias = bias

    def bias(self) -> str:
        return self._bias


class _FakeAccount:
    def __init__(self, dd: float) -> None:
        self._dd = dd

    def drawdown_pct(self) -> float:
        return self._dd


class _FakeTracker:
    """Minimal tracker exposing only stats() for auto-pause unit tests."""

    def __init__(self, stats: dict) -> None:
        self._stats = stats

    def stats(self) -> dict:
        return self._stats


def _cand(direction="LONG", signal_type="SCREENER", strategy="MOMENTUM") -> SignalCandidate:
    return SignalCandidate(
        symbol="BTCUSDT", signal_type=signal_type, direction=direction,
        entry_price=100, tp=110, sl=95, score=80, strategy=strategy,
        reasons=["x"], tps=[{"level": 1, "price": 110}],
    )


def test_run_cycle_records_signal(store, fake_client, tracker_settings):
    fake_client.klines["BTCUSDT"] = _breakout_candles()
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client, tracker, [MomentumBreakoutDetector()], notifier=None, universe=["BTCUSDT"]
    )
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    assert recorded[0].symbol == "BTCUSDT"
    assert len(tracker.active_signals()) == 1


def test_run_cycle_no_data_records_nothing(store, fake_client, tracker_settings):
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client, tracker, [MomentumBreakoutDetector()], notifier=None, universe=["BTCUSDT"]
    )
    assert screener.run_cycle() == []


def test_scan_symbol_picks_highest_score(store, fake_client, tracker_settings):
    fake_client.klines["BTCUSDT"] = _breakout_candles()
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(fake_client, tracker, [MomentumBreakoutDetector()], universe=["BTCUSDT"])
    candidate = screener.scan_symbol("BTCUSDT")
    assert candidate is not None and candidate.direction == "LONG"


# ── regime filter ───────────────────────────────────────────────────────────
def _momentum_screener(store, fake_client, tracker_settings, **kw):
    fake_client.klines["BTCUSDT"] = _breakout_candles()
    tracker = Tracker(store, fake_client, tracker_settings)
    return Screener(
        fake_client, tracker, [MomentumBreakoutDetector()], universe=["BTCUSDT"], **kw
    ), tracker


def test_regime_bearish_flags_long_in_monitor_mode(store, fake_client, tracker_settings):
    """Campur default: an against-regime LONG is still emitted, flagged + down-scored."""
    screener, tracker = _momentum_screener(store, fake_client, tracker_settings, regime_provider=_FakeRegime(BEARISH))
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    assert recorded[0].against_regime is True
    assert tracker.active_signals() != []


def test_regime_bearish_hard_block_drops_long(store, fake_client, tracker_settings):
    screener, tracker = _momentum_screener(
        store, fake_client, tracker_settings,
        regime_provider=_FakeRegime(BEARISH), risk=RiskSettings(regime_hard_block=True),
    )
    assert screener.run_cycle() == []
    assert tracker.active_signals() == []


def test_regime_flag_down_scores(fake_client):
    screener = Screener(fake_client, _FakeTracker({}), [], universe=[])
    cand = _cand(signal_type="SCREENER")  # trend-following LONG, score 80
    base = cand.score
    assert screener._gate_candidate(cand, BEARISH, set()) is False  # monitor: not blocked
    assert cand.against_regime is True
    assert cand.score < base


def test_regime_bullish_allows_long_unflagged(store, fake_client, tracker_settings):
    screener, _ = _momentum_screener(store, fake_client, tracker_settings, regime_provider=_FakeRegime(BULLISH))
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    assert recorded[0].against_regime is False


def test_regime_neutral_allows_long(store, fake_client, tracker_settings):
    screener, _ = _momentum_screener(store, fake_client, tracker_settings, regime_provider=_FakeRegime(NEUTRAL))
    assert len(screener.run_cycle()) == 1


def test_regime_filter_can_be_disabled(store, fake_client, tracker_settings):
    screener, _ = _momentum_screener(
        store, fake_client, tracker_settings,
        regime_provider=_FakeRegime(BEARISH), risk=RiskSettings(regime_filter_enabled=False),
    )
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    assert recorded[0].against_regime is False


def test_counter_trend_setups_exempt_from_regime(store, fake_client, tracker_settings):
    screener, _ = _momentum_screener(store, fake_client, tracker_settings, regime_provider=_FakeRegime(BEARISH))
    # Trend-following LONG fights a bearish tape; a TRAP/SCALP reversal does not.
    assert screener._fights_regime(_cand(signal_type="SCREENER"), BEARISH) is True
    assert screener._fights_regime(_cand(signal_type="TRAP"), BEARISH) is False
    assert screener._fights_regime(_cand(signal_type="SCALP"), BEARISH) is False
    assert screener._fights_regime(_cand(direction="SHORT", signal_type="PREDUMP"), BULLISH) is False
    assert screener._fights_regime(_cand(signal_type="SCREENER"), NEUTRAL) is False


# ── drawdown throttle (hard block) ──────────────────────────────────────────
def test_deep_drawdown_pauses_all_entries(store, fake_client, tracker_settings):
    screener, tracker = _momentum_screener(
        store, fake_client, tracker_settings,
        account=_FakeAccount(20.0), risk=RiskSettings(drawdown_pause_pct=15.0),
    )
    assert screener.run_cycle() == []
    assert tracker.active_signals() == []


def test_shallow_drawdown_allows_entries(store, fake_client, tracker_settings):
    screener, _ = _momentum_screener(
        store, fake_client, tracker_settings,
        account=_FakeAccount(8.0), risk=RiskSettings(drawdown_pause_pct=15.0),
    )
    assert len(screener.run_cycle()) == 1


# ── auto-pause underperformers ──────────────────────────────────────────────
def test_weak_strategy_flagged_in_monitor_mode(store, fake_client, tracker_settings):
    """Campur default: a weak-strategy signal is emitted, flagged + down-scored."""
    fake_client.klines["BTCUSDT"] = _breakout_candles()

    class _StatsTracker(Tracker):
        def stats(self):
            return {"by_strategy": {"MOMENTUM": {"total": 15, "win_rate": 30.0}}}

    tracker = _StatsTracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client, tracker, [MomentumBreakoutDetector()], universe=["BTCUSDT"],
        risk=RiskSettings(autopause_min_trades=12, autopause_min_win_rate=38.0),
    )
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    assert recorded[0].weak_strategy is True


def test_weak_strategy_hard_block_drops_signal(store, fake_client, tracker_settings):
    fake_client.klines["BTCUSDT"] = _breakout_candles()

    class _StatsTracker(Tracker):
        def stats(self):
            return {"by_strategy": {"MOMENTUM": {"total": 15, "win_rate": 30.0}}}

    tracker = _StatsTracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client, tracker, [MomentumBreakoutDetector()], universe=["BTCUSDT"],
        risk=RiskSettings(autopause_min_trades=12, autopause_min_win_rate=38.0, autopause_hard_block=True),
    )
    assert screener.run_cycle() == []


def test_weak_strategy_detected(fake_client):
    stats = {"by_strategy": {"MOMENTUM": {"total": 15, "win_rate": 30.0}}}
    screener = Screener(
        fake_client, _FakeTracker(stats), [], universe=[],
        risk=RiskSettings(autopause_min_trades=12, autopause_min_win_rate=38.0),
    )
    assert "MOMENTUM" in screener._weak_strategies()


def test_strategy_not_paused_below_min_trades(fake_client):
    stats = {"by_strategy": {"MOMENTUM": {"total": 5, "win_rate": 10.0}}}  # too few trades
    screener = Screener(
        fake_client, _FakeTracker(stats), [], universe=[],
        risk=RiskSettings(autopause_min_trades=12, autopause_min_win_rate=38.0),
    )
    assert screener._weak_strategies() == set()


def test_healthy_strategy_not_paused(fake_client):
    stats = {"by_strategy": {"MOMENTUM": {"total": 30, "win_rate": 55.0}}}
    screener = Screener(
        fake_client, _FakeTracker(stats), [], universe=[],
        risk=RiskSettings(autopause_min_trades=12, autopause_min_win_rate=38.0),
    )
    assert screener._weak_strategies() == set()


# ── dynamic universe ────────────────────────────────────────────────────────
class _FakeUniverse:
    def __init__(self, symbols):
        self._symbols = symbols

    def symbols(self):
        return list(self._symbols)


def test_dynamic_universe_drives_the_scan(store, fake_client, tracker_settings):
    fake_client.klines["WIFUSDT"] = _breakout_candles()  # only the dynamic pick has data
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client, tracker, [MomentumBreakoutDetector()],
        universe=["BTCUSDT"],  # static fallback ignored when a provider yields symbols
        universe_provider=_FakeUniverse(["WIFUSDT"]),
    )
    assert screener.current_universe() == ["WIFUSDT"]
    recorded = screener.run_cycle()
    assert len(recorded) == 1 and recorded[0].symbol == "WIFUSDT"


def test_universe_falls_back_to_static_when_provider_empty(store, fake_client, tracker_settings):
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client, tracker, [MomentumBreakoutDetector()],
        universe=["BTCUSDT"], universe_provider=_FakeUniverse([]),
    )
    assert screener.current_universe() == ["BTCUSDT"]
