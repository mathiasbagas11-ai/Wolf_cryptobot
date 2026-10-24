"""Tests for the pure indicator functions."""

from __future__ import annotations

import math

from wolf import indicators as ind
from wolf.models import Candle


def test_ema_length_matches_input():
    vals = [1, 2, 3, 4, 5]
    assert len(ind.ema(vals, 3)) == len(vals)


def test_rsi_all_gains_is_100():
    vals = list(range(1, 30))  # strictly increasing
    assert ind.rsi(vals, 14) == 100.0


def test_rsi_all_losses_is_low():
    vals = list(range(30, 1, -1))  # strictly decreasing
    assert ind.rsi(vals, 14) == 0.0


def test_rsi_insufficient_data_is_nan():
    assert math.isnan(ind.rsi([1, 2, 3], 14))


def test_atr_positive_for_volatile_series():
    candles = [
        Candle(time=i, open=100, high=110, low=90, close=100) for i in range(20)
    ]
    val = ind.atr(candles, 14)
    assert val > 0


def test_macd_returns_triplet():
    vals = [float(i) for i in range(60)]
    line, signal, hist = ind.macd(vals)
    assert not math.isnan(line)
    assert math.isclose(hist, line - signal, rel_tol=1e-9)


def test_volume_ratio_detects_spike():
    candles = [Candle(time=i, open=1, high=1, low=1, close=1, volume=10.0) for i in range(21)]
    candles.append(Candle(time=21, open=1, high=1, low=1, close=1, volume=50.0))
    assert math.isclose(ind.volume_ratio(candles, 20), 5.0, rel_tol=1e-6)


def test_rsi_series_aligned_with_nan_lead():
    vals = list(range(1, 30))
    series = ind.rsi_series(vals, 14)
    assert len(series) == len(vals)
    assert all(math.isnan(x) for x in series[:14])
    assert series[14] == 100.0  # strictly increasing -> RSI 100


def test_bollinger_width_smaller_when_flat():
    flat = [100.0] * 30
    volatile = [100 + (10 if i % 2 else -10) for i in range(30)]
    _, _, _, w_flat = ind.bollinger_bands(flat, 20)
    _, _, _, w_vol = ind.bollinger_bands(volatile, 20)
    assert w_flat < w_vol


def test_bollinger_insufficient_data_nan():
    _, _, _, w = ind.bollinger_bands([1, 2, 3], 20)
    assert math.isnan(w)


def test_vwap_equals_price_when_flat():
    candles = [Candle(time=i, open=100, high=100, low=100, close=100, volume=10.0) for i in range(5)]
    assert math.isclose(ind.vwap(candles), 100.0, rel_tol=1e-9)


def test_vwap_weights_by_volume():
    # Two bars: typical price 10 (vol 1) and 20 (vol 3) → (10*1 + 20*3)/4 = 17.5
    candles = [
        Candle(time=0, open=10, high=10, low=10, close=10, volume=1.0),
        Candle(time=1, open=20, high=20, low=20, close=20, volume=3.0),
    ]
    assert math.isclose(ind.vwap(candles), 17.5, rel_tol=1e-9)


def test_vwap_lookback_restricts_window():
    candles = [Candle(time=i, open=100, high=100, low=100, close=100, volume=10.0) for i in range(5)]
    candles.append(Candle(time=5, open=200, high=200, low=200, close=200, volume=10.0))
    assert math.isclose(ind.vwap(candles, lookback=1), 200.0, rel_tol=1e-9)


def test_vwap_nan_without_volume():
    candles = [Candle(time=i, open=1, high=1, low=1, close=1, volume=0.0) for i in range(3)]
    assert math.isnan(ind.vwap(candles))


# ── FvG detection ─────────────────────────────────────────────────────────
def _c(t, o, h, l, c, v=100.0):
    return Candle(time=t * 900_000, open=o, high=h, low=l, close=c, volume=v)


def test_find_fvgs_bullish():
    """Three-candle gap where c3.low > c1.high is a bullish FvG."""
    cs = [
        _c(0, 100, 101, 99, 100),   # c1: high=101
        _c(1, 102, 103, 101, 102),   # c2: middle (ignored)
        _c(2, 103, 104, 102, 103),   # c3: low=102 > c1.high=101 → gap 101-102
    ]
    gaps = ind.find_fvgs(cs)
    assert len(gaps) == 1
    assert gaps[0]["type"] == "BULL"
    assert gaps[0]["bottom"] == 101
    assert gaps[0]["top"] == 102


def test_find_fvgs_bearish():
    """Three-candle gap where c3.high < c1.low is a bearish FvG."""
    cs = [
        _c(0, 100, 101, 98, 99),    # c1: low=98
        _c(1, 97, 98, 96, 97),       # c2: middle
        _c(2, 95, 97, 94, 95),       # c3: high=97 < c1.low=98 → gap 97-98
    ]
    gaps = ind.find_fvgs(cs)
    assert len(gaps) == 1
    assert gaps[0]["type"] == "BEAR"
    assert gaps[0]["bottom"] == 97
    assert gaps[0]["top"] == 98


def test_price_in_fvg():
    gaps = [{"type": "BULL", "top": 105.0, "bottom": 103.0}]
    assert ind.price_in_fvg(104.0, gaps, "BULL") is True
    assert ind.price_in_fvg(106.0, gaps, "BULL") is False
    assert ind.price_in_fvg(104.0, gaps, "BEAR") is False


def test_find_fvgs_no_gap_when_overlap():
    """No FvG when candles overlap — normal price action."""
    cs = [
        _c(0, 100, 102, 99, 101),
        _c(1, 101, 103, 100, 102),
        _c(2, 102, 104, 101, 103),  # c3.low=101 <= c1.high=102 → no gap
    ]
    assert ind.find_fvgs(cs) == []
