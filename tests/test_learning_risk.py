"""Tests for the adaptive learning engine, paper trader, regime filter & backtest."""

from __future__ import annotations

from wolf.config import LearningSettings, RegimeSettings, RiskSettings
from wolf.learning import LearningEngine
from wolf.models import Candle, Signal
from wolf.regime import BULLISH_TREND, RANGING, detect_regime
from wolf.risk import PaperTrader


def _resolved(symbol="BTCUSDT", strategy="MOMENTUM", pnl=5.0, entry=100.0, sl=95.0, activated=True):
    return Signal(
        symbol=symbol, signal_type="SCREENER", direction="LONG",
        entry_price=entry, tp=110, sl=sl, strategy=strategy,
        status="TP_HIT", activated=activated, pnl_pct=pnl, resolved_at="2026-01-01T00:00:00+00:00",
    )


# ── learning engine ─────────────────────────────────────────────────────────
def test_learning_boosts_winning_strategy(store):
    eng = LearningEngine(store, LearningSettings(min_samples=3, max_adjust=15))
    for _ in range(5):
        eng.observe(_resolved(pnl=6.0))  # all wins
    adj = eng.adjustment("BTCUSDT", "MOMENTUM")
    assert adj.delta > 0
    assert not adj.blacklisted


def test_learning_penalises_losing_strategy(store):
    eng = LearningEngine(store, LearningSettings(min_samples=3, max_adjust=15))
    for _ in range(5):
        eng.observe(_resolved(pnl=-4.0))
    adj = eng.adjustment("ETHUSDT", "MOMENTUM")
    assert adj.delta < 0


def test_learning_blacklists_bad_symbol(store):
    eng = LearningEngine(store, LearningSettings(min_samples=3, blacklist_min_trades=8, blacklist_max_winrate=25))
    for i in range(10):
        eng.observe(_resolved(symbol="ZZZUSDT", pnl=6.0 if i < 2 else -4.0))  # 20% win
    adj = eng.adjustment("ZZZUSDT", "MOMENTUM")
    assert adj.blacklisted


def test_learning_ignores_non_activated_and_zero(store):
    eng = LearningEngine(store, LearningSettings(min_samples=1))
    eng.observe(_resolved(activated=False, pnl=5.0))
    eng.observe(_resolved(pnl=0.0))
    assert eng.snapshot()["strategies"] == {}


def test_learning_seed_warmstart(store):
    eng = LearningEngine(store, LearningSettings(min_samples=3, max_adjust=15))
    eng.seed([("SWING", "BTCUSDT", 5.0, 1.5)] * 4)
    snap = eng.snapshot()["strategies"]["SWING"]
    assert snap["trades"] == 4 and snap["win_rate"] == 100.0


# ── paper trader ─────────────────────────────────────────────────────────────
def test_paper_books_r_and_usd(store):
    pt = PaperTrader(store, RiskSettings(starting_balance=1000, risk_pct=2.0))
    # entry 100, sl 95 -> 5% stop. pnl +5% -> R=1.0 -> risk_amount 20 -> +20 USD.
    fill = pt.record(_resolved(pnl=5.0, entry=100, sl=95))
    assert fill is not None
    assert fill.r_multiple == 1.0
    assert fill.pnl_usd == 20.0
    assert fill.balance == 1020.0


def test_paper_skips_non_activated(store):
    pt = PaperTrader(store, RiskSettings())
    assert pt.record(_resolved(activated=False)) is None


def test_paper_stats_drawdown(store):
    pt = PaperTrader(store, RiskSettings(starting_balance=1000, risk_pct=2.0))
    pt.record(_resolved(pnl=5.0, entry=100, sl=95))   # +20 -> 1020 (peak)
    pt.record(_resolved(pnl=-5.0, entry=100, sl=95))  # -R loss
    s = pt.stats()
    assert s["trades"] == 2
    assert s["peak"] == 1020.0
    assert s["max_drawdown_pct"] > 0


# ── regime ───────────────────────────────────────────────────────────────────
def _uptrend_candles(n=80):
    out, p = [], 100.0
    for i in range(n):
        p += 1.0
        out.append(Candle(time=i * 900_000, open=p - 1, high=p + 0.5, low=p - 1.2, close=p, volume=100.0))
    return out


def test_regime_detects_uptrend():
    reg = detect_regime(_uptrend_candles())
    assert reg.label == BULLISH_TREND
    assert reg.aligns_with("LONG") and not reg.aligns_with("SHORT")


def test_regime_ranging_allows_both():
    flat = [Candle(time=i * 900_000, open=100, high=101, low=99, close=100, volume=100.0) for i in range(80)]
    reg = detect_regime(flat)
    assert reg.label == RANGING
    assert reg.aligns_with("LONG") and reg.aligns_with("SHORT")
