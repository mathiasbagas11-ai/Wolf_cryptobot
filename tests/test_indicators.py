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
