"""Phase 3 verification — scoring engine, exact rubric (deterministic)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from anomaly.scoring import COMPONENT_MAX, MIN_ROWS, score_coin

N = 90
TS = pd.date_range("2026-01-01", periods=N, freq="D", tz="UTC")


def _df(close, high=None, low=None, volume=None):
    close = np.asarray(close, dtype=float)
    n = len(close)
    high = close * 1.01 if high is None else np.asarray(high, dtype=float)
    low = close * 0.99 if low is None else np.asarray(low, dtype=float)
    volume = np.full(n, 1_000_000.0) if volume is None else np.asarray(volume, dtype=float)
    return pd.DataFrame({"timestamp": TS[:n], "open": close, "high": high,
                         "low": low, "close": close, "volume": volume})


def _meta(**over):
    m = {"id": "coin", "symbol": "coin", "name": "Coin",
         "market_cap": 2e8, "fdv": 3e8, "total_volume": 2e7, "in_dca_sleeve": False}
    m.update(over)
    return m


def _comp(res, key):
    return res["components"][key]


# ── shape & bounds ─────────────────────────────────────────────────────────
def test_shape_and_bounds():
    res = score_coin(_df(100 + np.sin(np.linspace(0, 6, N))), _meta())
    assert set(res) == {"id", "symbol", "name", "in_dca_sleeve",
                        "score", "components", "flagged", "flags", "metrics"}
    assert 0 <= res["score"] <= 100
    for k, v in res["components"].items():
        assert 0.0 <= v <= COMPONENT_MAX[k]
    assert res["score"] == int(sum(res["components"].values()))


# ── red flags ──────────────────────────────────────────────────────────────
def test_redflag_insufficient_ohlc():
    res = score_coin(_df(100 + 0.1 * np.arange(MIN_ROWS - 5)), _meta())
    assert res["flagged"] and any("insufficient_ohlc" in f for f in res["flags"])
    assert res["score"] == 0


def test_redflag_high_fdv_mc():
    res = score_coin(_df(100 + 0.1 * np.arange(N)), _meta(market_cap=1e8, fdv=6e8))  # 6x
    assert res["flagged"] and any("unlock_overhang" in f for f in res["flags"])


def test_redflag_wash_turnover():
    res = score_coin(_df(100 + 0.1 * np.arange(N)), _meta(market_cap=1e8, total_volume=6e8))  # 6x
    assert res["flagged"] and any("wash_volume" in f for f in res["flags"])


def test_redflag_micro_cap():
    res = score_coin(_df(100 + 0.1 * np.arange(N)), _meta(market_cap=1e7))
    assert res["flagged"] and any("micro_cap" in f for f in res["flags"])


def test_healthy_coin_not_flagged():
    res = score_coin(_df(100 + 0.1 * np.arange(N)), _meta(market_cap=2e8, fdv=2.4e8, total_volume=2e7))
    assert not res["flagged"] and res["flags"] == []


# ── component A: ATR(14)/ATR(60) tiers + bonus ─────────────────────────────
def _contracting_df(n=N, early_range=8.0, late_range=0.5):
    """Wide daily range for the first 60 bars, tight for the last 30 → low ATR14/60."""
    close = np.full(n, 100.0)
    rng = np.concatenate([np.full(60, early_range), np.full(n - 60, late_range)])
    high = close + rng / 2
    low = close - rng / 2
    return _df(close, high=high, low=low)


def test_component_a_awards_points_when_contracting():
    a = _comp(score_coin(_contracting_df(), _meta()), "volatility_contraction")
    assert a >= 10                                  # ratio well under 0.70

def test_component_a_zero_when_not_contracting():
    # Constant daily range → ATR14 ~ ATR60 → ratio ~1 → 0 points.
    close = np.full(N, 100.0)
    df = _df(close, high=close + 2.0, low=close - 2.0)
    a = _comp(score_coin(df, _meta()), "volatility_contraction")
    assert a == 0

def test_component_a_capped_at_25():
    a = _comp(score_coin(_contracting_df(late_range=0.05), _meta()), "volatility_contraction")
    assert a <= 25


# ── component B: volume anomaly + close-strength gate ──────────────────────
def _vol_spike(mult, n=N):
    v = np.full(n, 1_000_000.0)
    v[-1] *= mult
    return v


def test_component_b_requires_strong_close():
    close = np.full(N, 100.0)
    # strong close: last candle closes at the very top of its range
    high = close.copy(); low = close.copy()
    high[-1], low[-1], close_arr = 101.0, 99.0, close.copy()
    close_arr[-1] = 100.9                              # (0.9/2)=0.95 > 0.70
    df = _df(close_arr, high=high, low=low, volume=_vol_spike(6))
    b = _comp(score_coin(df, _meta()), "volume_anomaly")
    assert b == 30                                     # vol_ratio > 5 and strong close

def test_component_b_zero_on_weak_close_even_with_spike():
    close = np.full(N, 100.0)
    high = close.copy(); low = close.copy()
    high[-1], low[-1] = 101.0, 99.0
    close_arr = close.copy(); close_arr[-1] = 99.2     # (0.2/2)=0.10 < 0.70 → dump
    df = _df(close_arr, high=high, low=low, volume=_vol_spike(6))
    b = _comp(score_coin(df, _meta()), "volume_anomaly")
    assert b == 0

def test_component_b_tiers():
    def b_for(mult):
        close = np.full(N, 100.0)
        high = close.copy(); low = close.copy()
        high[-1], low[-1] = 101.0, 99.0
        c = close.copy(); c[-1] = 100.9                # strong close
        return _comp(score_coin(_df(c, high=high, low=low, volume=_vol_spike(mult)), _meta()),
                     "volume_anomaly")
    assert b_for(6) == 30 and b_for(4) == 22 and b_for(2.5) == 12 and b_for(1.5) == 0


# ── component C: range position bands ──────────────────────────────────────
def _at_range_position(target, n=N, lo=100.0, hi=120.0):
    """30d range fixed to [lo, hi] via anchor bars; last close at ~target."""
    close = np.full(n, 110.0)
    high = np.full(n, 111.0)
    low = np.full(n, 109.0)
    high[-5] = hi                                     # 30d high anchor
    low[-4] = lo                                      # 30d low anchor
    close[-1] = lo + target * (hi - lo)
    high[-1] = max(close[-1], 111.0)
    low[-1] = min(close[-1], 109.0)
    return _df(close, high=high, low=low)


def test_component_c_bands():
    def c_for(pos):
        return _comp(score_coin(_at_range_position(pos), _meta()), "structure_position")
    assert c_for(0.72) == 25       # 0.60-0.85 ideal
    assert c_for(0.50) == 15       # mid-range
    assert c_for(0.30) == 5        # basing
    assert c_for(0.95) == 0        # extended
    assert c_for(0.88) == 10       # 0.85-0.90 gap fill


# ── component D: FDV tiers + wash penalty ──────────────────────────────────
def test_component_d_fdv_tiers():
    df = _df(100 + 0.1 * np.arange(N))
    def d_for(fdv_mc, turnover=0.1):
        mcap = 2e8
        return _comp(score_coin(df, _meta(market_cap=mcap, fdv=fdv_mc * mcap,
                                          total_volume=turnover * mcap)), "supply_health")
    assert d_for(1.1) == 20 and d_for(1.8) == 12 and d_for(2.6) == 5 and d_for(3.5) == 0

def test_component_d_wash_penalty():
    df = _df(100 + 0.1 * np.arange(N))
    mcap = 2e8
    # FDV/MC 1.1 → 20, turnover 3.5 → -15 → 5 (not red-flagged: turnover <= 5)
    d = _comp(score_coin(df, _meta(market_cap=mcap, fdv=1.1 * mcap,
                                   total_volume=3.5 * mcap)), "supply_health")
    assert d == 5

def test_component_d_missing_fdv_is_neutral():
    df = _df(100 + 0.1 * np.arange(N))
    d = _comp(score_coin(df, _meta(fdv=0)), "supply_health")
    assert d == 12


# ── metrics exposed for logging ────────────────────────────────────────────
def test_metrics_exposed():
    res = score_coin(_contracting_df(), _meta())
    m = res["metrics"]
    for key in ("atr_ratio", "volume_ratio", "range_position", "fdv_mc",
                "turnover", "dist_from_ath_pct"):
        assert key in m


def test_dca_flag_passthrough():
    res = score_coin(_df(100 + 0.1 * np.arange(N)), _meta(in_dca_sleeve=True))
    assert res["in_dca_sleeve"] is True
