"""Tests for the screening orchestration."""

from __future__ import annotations

import time

from wolf.config import RiskSettings
from wolf.detectors import MomentumBreakoutDetector
from wolf.detectors.base import Detector, SignalCandidate
from wolf.models import Candle
from wolf.regime import BEARISH, BULLISH, NEUTRAL, UNKNOWN
from wolf.regime_composite import (
    EXTREME_FEAR,
    MarketContext,
    UD_RISK_ON,
)
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


def _bucket(total: int, avg_r: float, se_r: float, win_rate: float = 40.0) -> dict:
    """A by_strategy bucket shaped like the one stats() emits."""
    return {
        "total": total, "win_rate": win_rate, "avg_r": avg_r, "se_r": se_r,
        "sd_r": round(se_r * (total ** 0.5), 3), "avg_pnl": 0.0,
    }


def _weak_bucket() -> dict:
    """Confidently losing: -0.50R ± 0.10 → one-sided upper bound -0.34R."""
    return _bucket(total=40, avg_r=-0.50, se_r=0.10, win_rate=30.0)


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


# ── minimal fake detector for orchestration tests ──────────────────────────


class _FixedDetector(Detector):
    """Returns a fixed-score candidate regardless of candle content."""

    min_candles = 1

    def __init__(self, name: str, direction: str, score: int) -> None:
        self._det_name = name
        self._direction = direction
        self._score = score

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._det_name

    def evaluate(self, symbol, candles, context=None, features=None):
        if not candles:
            return None
        entry = candles[-1].close
        if self._direction == "LONG":
            tp, sl = entry * 1.10, entry * 0.95  # R:R ~2:1, clears min_rr=1.5
        else:
            tp, sl = entry * 0.90, entry * 1.05
        return SignalCandidate(
            symbol=symbol,
            signal_type=self._det_name,
            direction=self._direction,
            entry_price=entry,
            tp=tp,
            sl=sl,
            score=self._score,
            strategy=self._det_name,
        )


_SMALL_CANDLES = [
    Candle(time=i * 900_000, open=100, high=101, low=99, close=100, volume=100.0)
    for i in range(10)
]


# ── existing orchestration tests ────────────────────────────────────────────


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
            return {"by_strategy": {"MOMENTUM": _weak_bucket()}}

    tracker = _StatsTracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client, tracker, [MomentumBreakoutDetector()], universe=["BTCUSDT"],
        risk=RiskSettings(autopause_min_trades=30),
    )
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    assert recorded[0].weak_strategy is True


def test_weak_strategy_hard_block_drops_signal(store, fake_client, tracker_settings):
    fake_client.klines["BTCUSDT"] = _breakout_candles()

    class _StatsTracker(Tracker):
        def stats(self):
            return {"by_strategy": {"MOMENTUM": _weak_bucket()}}

    tracker = _StatsTracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client, tracker, [MomentumBreakoutDetector()], universe=["BTCUSDT"],
        risk=RiskSettings(autopause_min_trades=30, autopause_hard_block=True),
    )
    assert screener.run_cycle() == []


def test_confidently_losing_strategy_paused(fake_client):
    # -0.50R with a tight standard error: upper bound -0.34R is still under the
    # floor, so the strategy is losing rather than unlucky.
    stats = {"by_strategy": {"MOMENTUM": _weak_bucket()}}
    screener = Screener(
        fake_client, _FakeTracker(stats), [], universe=[],
        risk=RiskSettings(autopause_min_trades=30),
    )
    assert "MOMENTUM" in screener._weak_strategies()


def test_noisy_negative_average_not_paused(fake_client):
    """The anti-predictive case: a bad-looking average that proves nothing.

    Same -0.50R as the paused strategy, but four times the standard error, so
    the upper bound sits above zero. Gating on the point estimate alone flagged
    ~78% of live signals — and the flagged ones went on to outperform.
    """
    stats = {"by_strategy": {"MOMENTUM": _bucket(total=40, avg_r=-0.50, se_r=0.40)}}
    screener = Screener(
        fake_client, _FakeTracker(stats), [], universe=[],
        risk=RiskSettings(autopause_min_trades=30),
    )
    assert screener._weak_strategies() == set()


def test_live_swing_sample_is_still_inconclusive(fake_client):
    """SWING's real four-day numbers: worst performer, but not yet provable.

    -0.187R over 60 graded trades with se 0.186 gives an upper bound of +0.12R.
    The honest answer is "keep collecting", not "pause".
    """
    stats = {"by_strategy": {"SWING": _bucket(total=60, avg_r=-0.187, se_r=0.186)}}
    screener = Screener(
        fake_client, _FakeTracker(stats), [], universe=[],
        risk=RiskSettings(autopause_min_trades=30),
    )
    assert screener._weak_strategies() == set()


def test_profitable_low_winrate_strategy_not_paused(fake_client):
    # A low win rate with high reward:risk is a real edge, not a bleed.
    stats = {"by_strategy": {"PREDUMP": _bucket(total=40, avg_r=0.30, se_r=0.10, win_rate=36.9)}}
    screener = Screener(
        fake_client, _FakeTracker(stats), [], universe=[],
        risk=RiskSettings(autopause_min_trades=30, autopause_min_win_rate=38.0),
    )
    assert screener._weak_strategies() == set()


def test_floor_can_demand_a_margin_over_breakeven(fake_client):
    # Fees run roughly 0.2-0.4R, so a floor above zero pauses a strategy that is
    # positive on paper but net-negative once costs are paid.
    stats = {"by_strategy": {"SCALP": _bucket(total=40, avg_r=0.05, se_r=0.05)}}
    live = RiskSettings(autopause_min_trades=30)
    strict = RiskSettings(autopause_min_trades=30, autopause_min_expectancy_r=0.30)
    assert Screener(fake_client, _FakeTracker(stats), [], universe=[], risk=live)._weak_strategies() == set()
    assert "SCALP" in Screener(
        fake_client, _FakeTracker(stats), [], universe=[], risk=strict
    )._weak_strategies()


def test_strategy_not_paused_below_min_trades(fake_client):
    # Too small to judge, however bad it looks.
    stats = {"by_strategy": {"MOMENTUM": _bucket(total=5, avg_r=-0.90, se_r=0.05)}}
    screener = Screener(
        fake_client, _FakeTracker(stats), [], universe=[],
        risk=RiskSettings(autopause_min_trades=30),
    )
    assert screener._weak_strategies() == set()


def test_healthy_strategy_not_paused(fake_client):
    stats = {"by_strategy": {"MOMENTUM": _bucket(total=40, avg_r=0.80, se_r=0.15)}}
    screener = Screener(
        fake_client, _FakeTracker(stats), [], universe=[],
        risk=RiskSettings(autopause_min_trades=30),
    )
    assert screener._weak_strategies() == set()


def test_bucket_without_r_is_not_judged(fake_client):
    # Percent-only buckets carry no risk-adjusted edge to gate on; skip rather
    # than fall back to a metric that flagged the wrong strategies.
    stats = {"by_strategy": {"MOMENTUM": {"total": 40, "win_rate": 30.0, "avg_pnl": -0.73}}}
    screener = Screener(
        fake_client, _FakeTracker(stats), [], universe=[],
        risk=RiskSettings(autopause_min_trades=30),
    )
    assert screener._weak_strategies() == set()


# ── concurrent-position caps ─────────────────────────────────────────────────
def test_cap_per_strategy_rejects_over_limit(fake_client):
    # 4 PREDUMP already open + cap 4 → the 5th PREDUMP candidate is rejected.
    screener = Screener(
        fake_client, _FakeTracker({}), [], universe=[],
        risk=RiskSettings(max_active_per_strategy=4, max_active_per_direction=0),
    )
    cand = _cand(direction="SHORT", signal_type="PREDUMP", strategy="PREDUMP")
    assert screener._capped(cand, {"PREDUMP": 4}, {}) is True
    assert screener._capped(cand, {"PREDUMP": 3}, {}) is False


def test_cap_per_direction_rejects_over_limit(fake_client):
    # 6 SHORTs already open + cap 6 → the 7th SHORT candidate is rejected,
    # regardless of which strategy it belongs to.
    screener = Screener(
        fake_client, _FakeTracker({}), [], universe=[],
        risk=RiskSettings(max_active_per_strategy=0, max_active_per_direction=6),
    )
    cand = _cand(direction="SHORT", signal_type="PREDUMP", strategy="PREDUMP")
    assert screener._capped(cand, {}, {"SHORT": 6}) is True
    assert screener._capped(cand, {}, {"SHORT": 5}) is False


def test_cap_zero_disables_both_limits(fake_client):
    screener = Screener(
        fake_client, _FakeTracker({}), [], universe=[],
        risk=RiskSettings(max_active_per_strategy=0, max_active_per_direction=0),
    )
    cand = _cand(direction="SHORT", signal_type="PREDUMP", strategy="PREDUMP")
    assert screener._capped(cand, {"PREDUMP": 99}, {"SHORT": 99}) is False


def test_cap_snapshots_open_positions_and_increments_in_cycle(store, fake_client, tracker_settings):
    # Snapshot reads already-open positions, and freshly-emitted ones count
    # toward the cap for later symbols in the same cycle: 3 PREDUMP already
    # open + cap 4 → only 1 more is recorded across 3 fresh symbols.
    tracker = Tracker(store, fake_client, tracker_settings)
    for sym in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
        tracker.record_signal(
            symbol=sym, signal_type="PREDUMP", direction="SHORT",
            entry_price=100, tp=90, sl=105, score=90, strategy="PREDUMP",
        )
    assert len(tracker.active_signals()) == 3

    fresh = ["S1USDT", "S2USDT", "S3USDT"]
    for sym in fresh:
        fake_client.klines[sym] = _SMALL_CANDLES
    screener = Screener(
        fake_client, tracker, [_short_detector(90)], universe=fresh,
        risk=RiskSettings(max_active_per_strategy=4),
    )
    recorded = screener.run_cycle()
    assert len(recorded) == 1                      # 3 held + 1 new = cap of 4
    assert len(tracker.active_signals()) == 4


# ── composite-regime bounce guard ────────────────────────────────────────────
class _FakeMacro:
    def __init__(self, ctx: MarketContext) -> None:
        self._ctx = ctx

    def snapshot(self) -> MarketContext:
        return self._ctx


def _short_detector(score=90):
    return _FixedDetector("PREDUMP", "SHORT", score)  # counter-trend short


def test_bounce_guard_monitor_flags_short_without_scaling(fake_client):
    # Monitor: extreme fear → SHORT flagged, but nothing actually changes.
    screener = Screener(fake_client, _FakeTracker({}), [], universe=[])
    cand = _cand(direction="SHORT", signal_type="PREDUMP")
    ctx = MarketContext(trend=NEUTRAL, sentiment=EXTREME_FEAR)
    assert screener._apply_bounce_guard(cand, ctx) is False   # never drops in monitor
    assert cand.bounce_flagged is True
    assert cand.risk_scale == 1.0                              # observation only


def test_bounce_guard_ignores_longs(fake_client):
    screener = Screener(fake_client, _FakeTracker({}), [], universe=[])
    cand = _cand(direction="LONG", signal_type="SCREENER")
    ctx = MarketContext(trend=NEUTRAL, sentiment=EXTREME_FEAR)
    assert screener._apply_bounce_guard(cand, ctx) is False
    assert cand.bounce_flagged is False


def test_bounce_guard_noop_without_reversal_risk(fake_client):
    screener = Screener(fake_client, _FakeTracker({}), [], universe=[])
    cand = _cand(direction="SHORT", signal_type="PREDUMP")
    ctx = MarketContext(trend=BEARISH, sentiment="SENT_NEUTRAL")  # no bounce risk
    assert screener._apply_bounce_guard(cand, ctx) is False
    assert cand.bounce_flagged is False


def test_bounce_guard_live_scales_high_score_short(fake_client):
    screener = Screener(
        fake_client, _FakeTracker({}), [], universe=[],
        risk=RiskSettings(bounce_guard_mode="live", bounce_size_factor=0.5, bounce_min_score=88),
    )
    cand = _cand(direction="SHORT", signal_type="PREDUMP")  # score 80... below floor
    cand.score = 90
    ctx = MarketContext(trend=NEUTRAL, usdt_d=UD_RISK_ON)
    assert screener._apply_bounce_guard(cand, ctx) is False  # passes floor, kept
    assert cand.risk_scale == 0.5


def test_bounce_guard_live_drops_low_score_short(fake_client):
    screener = Screener(
        fake_client, _FakeTracker({}), [], universe=[],
        risk=RiskSettings(bounce_guard_mode="live", bounce_min_score=88),
    )
    cand = _cand(direction="SHORT", signal_type="PREDUMP")  # score 80 < 88
    ctx = MarketContext(trend=NEUTRAL, sentiment=EXTREME_FEAR)
    assert screener._apply_bounce_guard(cand, ctx) is True   # dropped
    assert cand.bounce_flagged is True


def test_bounce_guard_disabled_is_inert(fake_client):
    screener = Screener(
        fake_client, _FakeTracker({}), [], universe=[],
        risk=RiskSettings(composite_regime_enabled=False, bounce_guard_mode="live"),
    )
    cand = _cand(direction="SHORT", signal_type="PREDUMP")
    ctx = MarketContext(trend=NEUTRAL, sentiment=EXTREME_FEAR)
    assert screener._apply_bounce_guard(cand, ctx) is False
    assert cand.bounce_flagged is False


def test_bounce_guard_monitor_counter_trend_short_recorded(store, fake_client, tracker_settings):
    # Blind-spot closure: a counter-trend PREDUMP short (regime-exempt) is still
    # bounce-flagged and emitted at full size in monitor mode, via run_cycle.
    fake_client.klines["BTCUSDT"] = _SMALL_CANDLES
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client, tracker, [_short_detector(90)], universe=["BTCUSDT"],
        macro_provider=_FakeMacro(MarketContext(trend=NEUTRAL, sentiment=EXTREME_FEAR)),
    )
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    assert recorded[0].bounce_flagged is True
    assert recorded[0].risk_scale == 1.0    # monitor: full size


def test_bounce_guard_live_records_scaled_short(store, fake_client, tracker_settings):
    fake_client.klines["BTCUSDT"] = _SMALL_CANDLES
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client, tracker, [_short_detector(90)], universe=["BTCUSDT"],
        risk=RiskSettings(bounce_guard_mode="live", bounce_size_factor=0.5, bounce_min_score=88),
        macro_provider=_FakeMacro(MarketContext(trend=NEUTRAL, usdt_d=UD_RISK_ON)),
    )
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    assert recorded[0].risk_scale == 0.5


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


# ── conflict detection tests ────────────────────────────────────────────────


def test_conflict_detection_skips_choppy_symbol(store, fake_client, tracker_settings):
    """Both a LONG and a SHORT detector trigger on the same symbol → skip."""
    fake_client.klines["ETHUSDT"] = _SMALL_CANDLES
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client,
        tracker,
        [_FixedDetector("A", "LONG", 70), _FixedDetector("B", "SHORT", 65)],
        universe=["ETHUSDT"],
    )
    recorded = screener.run_cycle()
    assert recorded == [], "Conflicting LONG/SHORT signals should be suppressed"


def test_conflict_detection_allows_single_direction(store, fake_client, tracker_settings):
    """Two detectors, both LONG — no conflict, signal should be emitted."""
    fake_client.klines["SOLUSDT"] = _SMALL_CANDLES
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client,
        tracker,
        [_FixedDetector("X", "LONG", 70), _FixedDetector("Y", "LONG", 65)],
        universe=["SOLUSDT"],
    )
    recorded = screener.run_cycle()
    assert len(recorded) == 1


# ── multi-detector confluence bonus tests ───────────────────────────────────


def test_confluence_bonus_raises_score(store, fake_client, tracker_settings):
    """Best candidate (score 70) gets +10 when a second detector agrees."""
    fake_client.klines["SOLUSDT"] = _SMALL_CANDLES
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client,
        tracker,
        [_FixedDetector("X", "LONG", 70), _FixedDetector("Y", "LONG", 65)],
        universe=["SOLUSDT"],
    )
    candidate = screener.scan_symbol("SOLUSDT")
    assert candidate is not None
    assert candidate.score == 80, "Expected base 70 + confluence bonus 10"


def test_confluence_bonus_sets_high_confluence(store, fake_client, tracker_settings):
    candidate = screener_for_long_agreement(fake_client, store, tracker_settings)
    assert candidate is not None
    assert candidate.confluence_level == "HIGH"


def test_confluence_bonus_inserts_reason(store, fake_client, tracker_settings):
    candidate = screener_for_long_agreement(fake_client, store, tracker_settings)
    assert candidate is not None
    assert any("Confluence" in r for r in candidate.reasons)


def test_confluence_score_capped_at_100(store, fake_client, tracker_settings):
    """Score must never exceed 100 even with the confluence bonus."""
    fake_client.klines["BNBUSDT"] = _SMALL_CANDLES
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client,
        tracker,
        [_FixedDetector("X", "LONG", 95), _FixedDetector("Y", "LONG", 90)],
        universe=["BNBUSDT"],
    )
    candidate = screener.scan_symbol("BNBUSDT")
    assert candidate is not None
    assert candidate.score == 100


def test_single_detector_no_confluence_bonus(store, fake_client, tracker_settings):
    """One detector only — no confluence bonus should be applied."""
    fake_client.klines["XRPUSDT"] = _SMALL_CANDLES
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client,
        tracker,
        [_FixedDetector("Z", "LONG", 70)],
        universe=["XRPUSDT"],
    )
    candidate = screener.scan_symbol("XRPUSDT")
    assert candidate is not None
    assert candidate.score == 70
    assert not any("Confluence" in r for r in candidate.reasons)


# helper reused by multiple confluence assertions
def screener_for_long_agreement(fake_client, store, tracker_settings):
    fake_client.klines["SOLUSDT"] = _SMALL_CANDLES
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client,
        tracker,
        [_FixedDetector("X", "LONG", 70), _FixedDetector("Y", "LONG", 65)],
        universe=["SOLUSDT"],
    )
    return screener.scan_symbol("SOLUSDT")


# ── cost-aware stop gate ─────────────────────────────────────────────────
def _candidate(entry: float, sl: float, tp: float):
    from wolf.detectors.base import SignalCandidate

    return SignalCandidate(
        symbol="BTCUSDT", signal_type="SCREENER", direction="LONG",
        entry_price=entry, tp=tp, sl=sl, score=90, strategy="MOMENTUM",
    )


def _screener(store, fake_client, **kw):
    from wolf.config import TrackerSettings
    from wolf.screener import Screener
    from wolf.tracker import Tracker

    return Screener(fake_client, Tracker(store, fake_client, TrackerSettings()), [], **kw)


def test_rejects_a_stop_too_tight_to_survive_costs(store, fake_client):
    """A 1:3 on paper is still a guaranteed loser if fees exceed its risk unit.

    Entry 100 / stop 99.88 is 0.12% of price, so a 20bps round trip costs 1.67R
    — the trade loses before price moves. R:R cannot catch this: it is a ratio,
    and this compares the risk unit to a cost in percent.
    """
    sc = _screener(store, fake_client, round_trip_bps=20.0, max_cost_r=0.5)
    assert sc._too_expensive(_candidate(100.0, 99.88, 100.36)) is True


def test_accepts_a_stop_wide_enough_to_absorb_costs(store, fake_client):
    # 0.80% stop -> costs are 0.25R, comfortably inside the limit.
    sc = _screener(store, fake_client, round_trip_bps=20.0, max_cost_r=0.5)
    assert sc._too_expensive(_candidate(100.0, 99.20, 102.40)) is False


def test_cost_gate_scales_with_the_configured_fee(store, fake_client):
    """A cheaper venue makes the same setup affordable."""
    tight = _candidate(100.0, 99.70, 100.90)          # 0.30% stop
    assert _screener(store, fake_client, round_trip_bps=20.0)._too_expensive(tight) is True
    assert _screener(store, fake_client, round_trip_bps=5.0)._too_expensive(tight) is False


def test_cost_gate_ignores_degenerate_geometry(store, fake_client):
    sc = _screener(store, fake_client)
    assert sc._too_expensive(_candidate(0.0, 0.0, 0.0)) is False
    assert sc._too_expensive(_candidate(100.0, 100.0, 103.0)) is False


# ── per-detector timeframes ──────────────────────────────────────────────
class _TFDetector(Detector):
    """Records which candle series it was handed."""

    min_candles = 1

    def __init__(self, name: str, timeframe: str) -> None:
        self._det_name = name
        self.timeframe = timeframe
        self.seen: list = []

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._det_name

    def evaluate(self, symbol, candles, context=None, features=None):
        self.seen = list(candles)
        entry = candles[-1].close
        return SignalCandidate(
            symbol=symbol, signal_type=self._det_name, direction="LONG",
            entry_price=entry, tp=entry * 1.09, sl=entry * 0.97,
            score=90, strategy=self._det_name, timeframe=self.timeframe,
        )


def _tf_candles(tf: str, price: float) -> list[Candle]:
    return [
        Candle(time=i * 900_000, open=price, high=price + 1, low=price - 1,
               close=price, volume=100.0)
        for i in range(60)
    ]


class _TFClient:
    """Serves a distinct series per interval so mix-ups are visible."""

    def __init__(self) -> None:
        self.requested: list[tuple[str, str]] = []
        self.prices = {"15m": 100.0, "1h": 200.0, "4h": 400.0}

    def get_klines(self, symbol, interval="15m", limit=100):
        self.requested.append((symbol, interval))
        return _tf_candles(interval, self.prices[interval])

    def get_price(self, symbol):
        return None


def test_required_timeframes_dedupes_across_detectors():
    sc = Screener(
        _TFClient(), _FakeTracker({}),
        [_TFDetector("A", "1h"), _TFDetector("B", "1h"), _TFDetector("C", "4h")],
        universe=[],
    )
    assert sc.required_timeframes() == ["1h", "4h"]


def test_each_detector_reads_its_own_timeframe(store, tracker_settings):
    """The bug this prevents: a 4h swing scored off 15m candles.

    Every distance in a setup is an ATR multiple of the series it was found on,
    so handing a detector the wrong interval silently turns a multi-day swing
    into a scalp with a swing's name on it.
    """
    client = _TFClient()
    fast, slow = _TFDetector("FAST", "15m"), _TFDetector("SLOW", "4h")
    sc = Screener(client, Tracker(store, client, tracker_settings), [fast, slow],
                  universe=["BTCUSDT"], notifier=None)
    sc.run_cycle()

    assert {tf for _sym, tf in client.requested} == {"15m", "4h"}
    assert fast.seen[-1].close == 100.0     # 15m series
    assert slow.seen[-1].close == 400.0     # 4h series


def test_signal_carries_the_timeframe_it_was_found_on(store, tracker_settings):
    client = _TFClient()
    tracker = Tracker(store, client, tracker_settings)
    sc = Screener(client, tracker, [_TFDetector("SWING", "4h")],
                  universe=["BTCUSDT"], notifier=None)
    recorded = sc.run_cycle()
    assert recorded and recorded[0].timeframe == "4h"


def test_single_series_callers_still_work(store, fake_client, tracker_settings):
    """A bare candle list keeps working — used by scan_symbol and older tests."""
    det = _TFDetector("FAST", "15m")
    sc = Screener(fake_client, Tracker(store, fake_client, tracker_settings), [det],
                  universe=["BTCUSDT"])
    assert sc._best_candidate("BTCUSDT", _tf_candles("15m", 50.0), None) is not None


# ── forming-candle guard ─────────────────────────────────────────────────
def _bars(interval_ms: int, n: int, last_open_ms: int) -> list[Candle]:
    """``n`` bars ending with one opening at ``last_open_ms``."""
    return [
        Candle(time=last_open_ms - (n - 1 - i) * interval_ms,
               open=100, high=101, low=99, close=100, volume=100.0)
        for i in range(n)
    ]


def test_drops_the_bar_that_has_not_closed_yet():
    """A 4h bar 10 minutes old can still become its own opposite."""
    from wolf.screener import drop_forming

    four_h = 14_400_000
    now = 1_700_000_000_000
    bars = _bars(four_h, 5, last_open_ms=now - 600_000)   # opened 10 min ago
    kept = drop_forming(bars, "4h", now_ms=now)
    assert len(kept) == 4
    assert kept[-1].time == bars[-2].time


def test_keeps_a_bar_that_has_closed():
    from wolf.screener import drop_forming

    four_h = 14_400_000
    now = 1_700_000_000_000
    bars = _bars(four_h, 5, last_open_ms=now - four_h)    # closed exactly now
    assert len(drop_forming(bars, "4h", now_ms=now)) == 5


def test_unknown_interval_is_left_alone():
    """Dropping a real bar on a guess would be its own bug."""
    from wolf.screener import drop_forming

    bars = _bars(900_000, 3, last_open_ms=1_700_000_000_000)
    assert len(drop_forming(bars, "7m", now_ms=1_700_000_000_000)) == 3
    assert drop_forming([], "4h") == []


def test_detectors_never_see_the_forming_bar(store, tracker_settings):
    """End to end: the series handed to a detector ends on a closed bar."""
    four_h = 14_400_000
    now = int(time.time() * 1000)
    forming_open = now - 600_000            # a 4h bar opened 10 minutes ago

    class _Client:
        def get_klines(self, symbol, interval="15m", limit=100):
            return _bars(four_h, 60, last_open_ms=forming_open)

        def get_price(self, symbol):
            return None

    det = _TFDetector("SWING", "4h")
    client = _Client()
    sc = Screener(client, Tracker(store, client, tracker_settings), [det],
                  universe=["BTCUSDT"], notifier=None)
    sc.run_cycle()
    assert det.seen, "detector was never called"
    assert det.seen[-1].time + four_h <= now      # last bar handed over is closed
    assert det.seen[-1].time != forming_open


# ── re-quoting a market entry at the live price ─────────────────────────────
def _market_cand(entry=100.0, sl=95.0):
    """A 1:3 market entry: 1R = 5, rungs at 1R / 2R / 3R off entry."""
    return SignalCandidate(
        symbol="BTCUSDT", signal_type="SCREENER", direction="LONG",
        entry_price=entry, tp=entry + 15, sl=sl, score=80, strategy="MOMENTUM",
        reasons=["x"], entry_mode="MOMENTUM_NOW", timeframe="1h",
        tps=[
            {"level": 1, "price": entry + 5, "allocation": 0.5, "r_multiple": 1.0},
            {"level": 2, "price": entry + 10, "allocation": 0.3, "r_multiple": 2.0},
            {"level": 3, "price": entry + 15, "allocation": 0.2, "r_multiple": 3.0},
        ],
    )


def test_market_entry_is_requoted_and_the_ladder_follows(fake_client):
    """Price drifted 1 (0.2R) since the bar close the detector read.

    The stop is a level in the market, so it stays at 95; the risk unit widens
    to 6 and every rung moves out to keep the R it was placed at. Leaving the
    ladder where it was would quietly turn a 1:3 into a 1:2.5.
    """
    screener = Screener(fake_client, _FakeTracker({}), [], universe=[])
    fake_client.prices["BTCUSDT"] = 101.0
    cand = _market_cand()
    assert screener._reprice_at_market(cand) is False
    assert cand.entry_price == 101.0
    assert cand.sl == 95
    assert [r["price"] for r in cand.tps] == [107.0, 113.0, 119.0]
    assert cand.tp == 119.0
    rr = (cand.tp - cand.entry_price) / (cand.entry_price - cand.sl)
    assert round(rr, 6) == 3.0


def test_a_setup_that_already_ran_is_not_chased(fake_client):
    """Past half the risk unit the move has begun without us.

    The stop has not moved with price, so entering here pays the same loss for
    a smaller remaining move — the ladder would just be re-drawn further away.
    """
    screener = Screener(fake_client, _FakeTracker({}), [], universe=[], max_chase_r=0.5)
    fake_client.prices["BTCUSDT"] = 103.0   # 0.6R past the 100 quote
    assert screener._reprice_at_market(_market_cand()) is True


def test_a_better_entry_than_quoted_is_taken(fake_client):
    """Drift against the signal is not chasing — it is a cheaper entry."""
    screener = Screener(fake_client, _FakeTracker({}), [], universe=[])
    fake_client.prices["BTCUSDT"] = 98.0
    cand = _market_cand()
    assert screener._reprice_at_market(cand) is False
    assert cand.entry_price == 98.0
    assert [r["price"] for r in cand.tps] == [101.0, 104.0, 107.0]


def test_a_setup_already_through_its_stop_is_dropped(fake_client):
    screener = Screener(fake_client, _FakeTracker({}), [], universe=[])
    fake_client.prices["BTCUSDT"] = 94.0
    assert screener._reprice_at_market(_market_cand()) is True


def test_pending_entries_and_missing_prices_keep_the_quoted_entry(fake_client):
    """A retest level is a decision, not a quote, and must survive untouched.

    So must any entry the exchange could not price — degrading to the last
    close is worse than emitting nothing only if the alternative works.
    """
    screener = Screener(fake_client, _FakeTracker({}), [], universe=[])
    fake_client.prices["BTCUSDT"] = 103.0
    retest = _market_cand()
    retest.entry_mode = "RETEST_WAIT"
    assert screener._reprice_at_market(retest) is False
    assert retest.entry_price == 100.0

    fake_client.prices.pop("BTCUSDT")
    market = _market_cand()
    assert screener._reprice_at_market(market) is False
    assert market.entry_price == 100.0


def test_short_entries_requote_in_the_other_direction(fake_client):
    """Mirror case: for a SHORT, drift *down* is the run that must not be chased."""
    screener = Screener(fake_client, _FakeTracker({}), [], universe=[], max_chase_r=0.5)
    short = SignalCandidate(
        symbol="BTCUSDT", signal_type="SCREENER", direction="SHORT",
        entry_price=100.0, tp=85.0, sl=105.0, score=80, strategy="MOMENTUM",
        reasons=["x"], entry_mode="MOMENTUM_NOW", timeframe="1h",
        tps=[
            {"level": 1, "price": 95.0, "allocation": 0.5, "r_multiple": 1.0},
            {"level": 2, "price": 90.0, "allocation": 0.3, "r_multiple": 2.0},
            {"level": 3, "price": 85.0, "allocation": 0.2, "r_multiple": 3.0},
        ],
    )
    fake_client.prices["BTCUSDT"] = 99.0            # 0.2R in the trade's favour
    assert screener._reprice_at_market(short) is False
    assert short.entry_price == 99.0
    assert [r["price"] for r in short.tps] == [93.0, 87.0, 81.0]

    fake_client.prices["BTCUSDT"] = 97.0            # 0.6R — already gone
    assert screener._reprice_at_market(_short_cand()) is True


def _short_cand():
    return SignalCandidate(
        symbol="BTCUSDT", signal_type="SCREENER", direction="SHORT",
        entry_price=100.0, tp=85.0, sl=105.0, score=80, strategy="MOMENTUM",
        reasons=["x"], entry_mode="MOMENTUM_NOW", timeframe="1h",
        tps=[{"level": 1, "price": 95.0, "allocation": 1.0, "r_multiple": 1.0}],
    )


# ── drawdown throttle ───────────────────────────────────────────────────────
def test_a_deep_drawdown_does_not_pause_scanning_by_default(store, fake_client, tracker_settings):
    """No orders are placed, so the throttle guards nothing and costs a sample.

    Halting on a 40% paper drawdown stops the bot recording signals during the
    stretch whose signals are most worth having, and protects no capital in
    exchange — the ledger is a record of what was emitted, not a position.
    """
    screener, tracker = _momentum_screener(
        store, fake_client, tracker_settings, account=_FakeAccount(40.0),
    )
    assert screener._drawdown_paused() is False
    assert len(screener.run_cycle()) == 1
    assert tracker.active_signals() != []


def test_the_throttle_still_pauses_once_armed(store, fake_client, tracker_settings):
    """Arming it is one env var, for the day the bot places real orders."""
    screener, tracker = _momentum_screener(
        store, fake_client, tracker_settings,
        account=_FakeAccount(20.0), risk=RiskSettings(drawdown_pause_pct=15.0),
    )
    assert screener._drawdown_paused() is True
    assert screener.run_cycle() == []
    assert tracker.active_signals() == []


def test_an_armed_throttle_leaves_a_shallow_drawdown_alone(store, fake_client, tracker_settings):
    screener, _ = _momentum_screener(
        store, fake_client, tracker_settings,
        account=_FakeAccount(9.0), risk=RiskSettings(drawdown_pause_pct=15.0),
    )
    assert screener._drawdown_paused() is False
    assert len(screener.run_cycle()) == 1
